from __future__ import annotations

import base64
import json
import logging
import re
import time
from dataclasses import dataclass, field, replace
from typing import Any, Generator, Iterable, Optional
from urllib.parse import urlsplit, urlunsplit

import requests

from src.card_models import CharacterCard
from src import ai_prompts
from src.provider_capabilities import request_key

#: The llama.cpp family's DRY look-back window.  The app's "0 = the whole
#: context" has no direct equivalent there, so it maps to their default.
_DRY_LOOKBACK_DEFAULT = 320

logger = logging.getLogger(__name__)

# HTTP statuses considered transient: rate limiting and server-side errors.
_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})

_BACKOFF_BASE_SECONDS = 1.0
_BACKOFF_MAX_SECONDS = 8.0

# Transport failures that are worth retrying. requests splits these across
# several exception classes; catching only ConnectionError/Timeout missed the
# most common real-world failure - ChunkedEncodingError, raised when a server
# drops the connection mid-response - which then escaped as a raw traceback.
_RETRYABLE_TRANSPORT_ERRORS = (
    requests.ConnectionError,
    requests.Timeout,
    requests.exceptions.ChunkedEncodingError,
    requests.exceptions.ContentDecodingError,
    requests.exceptions.TooManyRedirects,
)

# Bad request *configuration* (malformed URL, illegal header value) is
# permanent: retrying the identical request cannot help. It also must never
# be wrapped verbatim - ``InvalidHeader``'s message embeds the full header
# value, i.e. the API key, which would then land in the plaintext app log.
_PERMANENT_CONFIG_ERRORS = (
    requests.exceptions.InvalidHeader,
    requests.exceptions.InvalidURL,
    # A certificate-verification failure is permanent: the same request will
    # fail the same way every time, so retrying it only delays the error the
    # user needs to see (and the retries cost seconds each).
    requests.exceptions.SSLError,
    # http.client raises a bare UnicodeEncodeError for a header value that
    # isn't latin-1 encodable (e.g. a pasted API key with a smart quote).
    UnicodeEncodeError,
)


def is_cleartext_remote(base_url: str) -> bool:
    """True when *base_url* is plain ``http://`` to a non-loopback host.

    A bearer API key sent to such a host travels unencrypted. Localhost is
    exempt: the local-server presets (LM Studio, Ollama) are exactly that.
    """
    text = (base_url or '').strip()
    if not text:
        return False
    if '://' not in text:
        text = 'http://' + text
    try:
        from urllib.parse import urlparse
        parsed = urlparse(text)
    except ValueError:
        return False
    if parsed.scheme.lower() != 'http':
        return False
    host = (parsed.hostname or '').lower()
    return host not in ('', 'localhost', '127.0.0.1', '::1', '0:0:0:0:0:0:0:1')


def redact_secrets(message: object, extra_secrets: Optional[Iterable[str]] = None) -> str:
    """Return *message* with anything that looks like a credential removed.

    Applied to every transport error string that reaches a log or the UI:
    exception messages can echo request headers, including ``Authorization``,
    and server error prose can echo the key back ("Incorrect API key
    provided: sk-live-...").

    *extra_secrets* holds literals the caller already knows - most importantly
    the configured API key.  Pattern matching alone can never be complete
    (a key can have any format), so the literal value is removed as well.

    The key-shaped heuristics below are deliberately generous: over-redacting
    a word in an error message is a cosmetic problem, while under-redacting
    an API key writes it to the plaintext log and shows it in a dialog.
    """
    text = str(message)
    # 1. The literal secrets we know about.  Done first so the patterns
    #    cannot accidentally mangle the key before it is matched.
    for secret in extra_secrets or ():
        literal = str(secret or '').strip()
        # Very short values would match far too much text; four characters
        # is already the shortest plausible credential.
        if len(literal) >= 4:
            text = text.replace(literal, '[redacted]')
    # 2. Label-shaped values.  The label may be quoted (a JSON body echoes
    #    the key as ``"api_key": "..."``), so the optional quote is part of
    #    the pattern.
    text = re.sub(r'(?i)(bearer\s+)[^\s\'"]+', r'\1[redacted]', text)
    text = re.sub(
        r'(?i)(["\']?(?:api[ _-]?key|authorization|access[ _-]?token|token|secret'
        r'|password|passphrase|credential|bearer)["\']?\s*[:=]\s*)'
        r'[^\s,;\'"}]+',
        r'\1[redacted]', text,
    )
    # Key-shaped tokens anywhere: servers echo keys in prose where the label
    # rule above cannot see them ("api key provided: sk-live-SECRET").
    text = re.sub(
        r'(?i)\b(?:sk|key|token|apikey|api_key)[-_][A-Za-z0-9_\-]{6,}',
        '[redacted]', text,
    )
    # Bare ``key=...`` / ``key: ...`` values (query strings, form bodies).
    text = re.sub(r'(?i)(\bkey\s*[:=]\s*)[^\s,;\'"]+', r'\1[redacted]', text)
    text = re.sub(r'(?i)(header value\s*[:=]?\s*)([\'"]?)[^\s\'"]+', r'\1\2[redacted]', text)
    # 3. Well-known provider key shapes that carry no "sk"/"key" marker.
    text = re.sub(
        r'(?i)\b(?:gsk|hf|r8|csk|nvapi|api|xai|sk)[_-][A-Za-z0-9_\-]{12,}',
        '[redacted]', text,
    )
    text = re.sub(r'\bAIza[A-Za-z0-9_\-]{20,}', '[redacted]', text)
    # 4. Anything long enough to be an opaque credential: at least 20
    #    characters with both letters and digits.  This is what catches
    #    "Incorrect API key provided: gsk_9xYzAbCdEf0123...".
    text = _redact_opaque_tokens(text)
    # 5. Base64-encoded secrets: a key can travel base64-encoded (a config
    #    file, a gateway), and no label rule sees that.
    text = _redact_base64_secrets(text, extra_secrets)
    return text


#: Words that mark a decoded blob as a credential.
_CREDENTIAL_WORDS = (
    'key', 'token', 'secret', 'password', 'passwd', 'credential', 'auth',
)

_BASE64_TOKEN_RE = re.compile(
    r'(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{12,}={0,2}(?![A-Za-z0-9+/=])'
)


def _redact_base64_secrets(text: str, extra_secrets=None) -> str:
    """Redact base64 tokens that decode to something credential-shaped."""

    def repl(match: re.Match) -> str:
        token = match.group(0)
        try:
            decoded = base64.b64decode(token + '===', validate=False).decode('utf-8')
        except Exception:
            return token
        if not decoded or not decoded.isprintable():
            return token
        lowered = decoded.casefold()
        if any(word in lowered for word in _CREDENTIAL_WORDS):
            return '[redacted]'
        for secret in extra_secrets or ():
            literal = str(secret or '').strip()
            if literal and literal in decoded:
                return '[redacted]'
        return token

    return _BASE64_TOKEN_RE.sub(repl, text)


def _redact_opaque_tokens(text: str) -> str:
    """Replace long, mixed alphanumeric tokens with ``[redacted]``."""
    def repl(match: re.Match) -> str:
        token = match.group(0)
        if any(c.isdigit() for c in token) and any(c.isalpha() for c in token):
            return '[redacted]'
        return token

    return re.sub(r'(?<![\w-])[A-Za-z0-9][A-Za-z0-9_.\-]{19,}(?![\w-])', repl, text)


def _permanent_config_message(
    exc: BaseException, secrets: Optional[Iterable[str]] = None,
) -> str:
    """Safe, user-facing message for a permanent request-configuration error."""
    if isinstance(exc, requests.exceptions.InvalidHeader):
        return (
            'The API key (or another request header) contains invalid '
            'characters. Check the key in Settings.'
        )
    if isinstance(exc, UnicodeEncodeError):
        return (
            'The API key contains characters that cannot be sent '
            '(e.g. smart quotes). Re-enter it in Settings.'
        )
    return redact_secrets(exc, secrets)


# Error-message fingerprints of a chat-template / role-shape rejection.  Both
# llama.cpp-derived servers (LM Studio) and vLLM surface Jinja template
# failures either as an HTTP 400 body or as an in-band SSE error chunk; the
# wording always mentions the template or the alternation rule it enforces.
_TEMPLATE_ERROR_MARKERS = (
    'conversation roles must alternate',
    'roles must alternate',
    'raise_exception',
    'unable to generate parser',
    'jinja',
    'chat template',
    'chat_template',
)


def is_chat_template_error(message: object) -> bool:
    """True when *message* looks like a chat-template / role-shape rejection."""
    text = str(message).lower()
    return any(marker in text for marker in _TEMPLATE_ERROR_MARKERS)


class _RetryableError(Exception):
    """Internal marker: a transient transport/HTTP failure worth retrying."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


def _is_complete_json(text: str) -> bool:
    """True when *text* is a self-contained JSON value.

    ``RecursionError`` is caught as well: it is a ``RuntimeError``, not a
    ``ValueError``, so it escaped this guard and aborted the request on a
    deeply nested payload ("[" * 200000).
    """
    try:
        json.loads(text)
    except (json.JSONDecodeError, ValueError, RecursionError):
        return False
    return True


def _url_host(base_url: str) -> str:
    """The host of *base_url* without any userinfo.

    A base URL may carry "https://user:pass@host"; logging the raw
    authority would write the password to the log in cleartext.
    """
    text = (base_url or '').strip()
    if not text:
        return ''
    if '://' not in text:
        text = 'http://' + text
    try:
        parsed = urlsplit(text)
    except ValueError:
        return ''
    return parsed.hostname or parsed.netloc


_MAX_ERROR_BODY_BYTES = 64 * 1024

# Streaming limits.  ``max_tokens`` is only advisory to the server, so the
# client enforces its own ceiling: a runaway or hostile endpoint must not be
# able to exhaust the process's memory.
_MAX_STREAM_LINE_BYTES = 1024 * 1024      # 1 MiB for a single SSE line
_MAX_STREAM_CHARS = 4 * 1024 * 1024       # 4 MiB of decoded text per reply

#: The total time one request may take.  The socket timeout is a per-read
#: deadline, so a server that dribbles out one byte every 100 s would keep
#: the worker thread blocked indefinitely.
_MAX_REQUEST_SECONDS = 600.0


def _bounded_response_text(response) -> str:
    """Read at most :data:`_MAX_ERROR_BODY_BYTES` of an error body.

    The response is opened with ``stream=True``, so ``response.text``
    materialises the *entire* body in memory and then parses it.  A hostile or
    broken endpoint can return gigabytes and exhaust the process before the
    400-character clamp is ever applied.  The raw stream is read with a cap
    instead.
    """
    raw = None
    try:
        candidate = response.raw.read(_MAX_ERROR_BODY_BYTES + 1)
        if isinstance(candidate, (bytes, bytearray)):
            raw = bytes(candidate)
    except Exception:
        raw = None
    if raw:
        truncated = len(raw) > _MAX_ERROR_BODY_BYTES
        text = raw[:_MAX_ERROR_BODY_BYTES].decode(
            getattr(response, 'encoding', None) or 'utf-8', errors='replace',
        )
        return text + ('... [truncated]' if truncated else '')
    # Fallback for response objects without a usable raw stream.  The read
    # stays bounded: ``response.text`` materialises the *whole* body, which
    # is exactly what this function exists to prevent - so it is only used
    # when the body is known to be small.
    try:
        length = response.headers.get('Content-Length')
    except Exception:
        length = None
    try:
        if length is not None and str(length).isdigit() \
                and int(length) > _MAX_ERROR_BODY_BYTES:
            return ''
    except (TypeError, ValueError):
        pass
    try:
        text = response.text
    except Exception:
        return ''
    if not isinstance(text, str):
        return ''
    if len(text) > _MAX_ERROR_BODY_BYTES:
        text = text[:_MAX_ERROR_BODY_BYTES] + '... [truncated]'
    return text


class _CancellableHTTPAdapter(requests.adapters.HTTPAdapter):
    """Adapter that keeps a handle on the live connection.

    ``requests`` only returns the response object *after* the response
    headers have arrived, so ``AIClient.cancel()`` had nothing to close while
    the request was still waiting for them - Cancel appeared to do nothing for
    up to the full request timeout, and the window refused to close.

    The adapter records the urllib3 connection as soon as it is created, which
    makes its socket reachable during the connect and header phases.  The
    patch is applied to the pool *instance*, so it never affects other
    clients in the process; if urllib3's internals ever change, the getattr
    fallbacks keep the old behaviour instead of raising.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.last_connection = None
        self.cancelled = False

    def _capture(self, url: str) -> None:
        """Patch the pool for *url* so it records the connection it uses."""
        try:
            pool = self.poolmanager.connection_from_url(url)
        except Exception:
            return
        if pool is None or getattr(pool, '_ste_capture_patched', False):
            return
        original = getattr(pool, '_make_request', None)
        if original is None:
            return

        def _make_request(conn, *args, **kwargs):
            self.last_connection = conn
            self._instrument_connect(conn)
            return original(conn, *args, **kwargs)

        try:
            pool._make_request = _make_request
            pool._ste_capture_patched = True
        except Exception:
            logger.debug("Could not instrument the connection pool", exc_info=True)

    def _instrument_connect(self, conn) -> None:
        """Make a pending connect abort when the request was cancelled.

        ``cancel()`` can only close a socket that already exists, so a
        cancel that arrives while the connection is still being established
        (DNS, TCP, TLS) was silently ignored.  Hooking ``connect`` lets the
        cancellation be honoured as soon as the connect is about to start.
        """
        try:
            original_connect = getattr(conn, 'connect', None)
            if original_connect is None or getattr(conn, '_ste_connect_patched', False):
                return

            def connect(*args, **kwargs):
                if self.cancelled:
                    raise OSError('The request was cancelled.')
                return original_connect(*args, **kwargs)

            conn.connect = connect
            conn._ste_connect_patched = True
        except Exception:
            logger.debug("Could not instrument connect()", exc_info=True)

    def send(self, request, **kwargs):
        self._capture(request.url)
        return super().send(request, **kwargs)

    def close_connection(self, conn) -> None:
        """Close the socket of the live connection, if there is one."""
        sock = getattr(conn, 'sock', None)
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass
        # Also ask urllib3 to close the connection itself: it marks the
        # connection unusable so a later read fails instead of blocking.
        try:
            closer = getattr(conn, 'close', None)
            if closer is not None:
                closer()
        except Exception:
            pass


def _iter_sse_lines(resp) -> Generator[bytes, None, None]:
    """Yield the response body as lines, with a bounded buffer.

    ``requests``' ``iter_lines()`` keeps buffering until it sees a newline, so
    a server that never sends one grows the buffer without limit.  Reading
    fixed-size chunks and splitting them here keeps the buffer bounded.

    Falls back to ``iter_lines()`` when the response object does not provide
    chunked content.
    """
    buffer = bytearray()
    produced = False
    try:
        chunks = resp.iter_content(chunk_size=8192)
    except (AttributeError, TypeError):
        chunks = None
    if chunks is not None:
        for chunk in chunks:
            if not chunk:
                continue
            produced = True
            buffer.extend(chunk)
            while True:
                index = buffer.find(b'\n')
                if index < 0:
                    break
                line = bytes(buffer[:index])
                del buffer[:index + 1]
                yield line
            if len(buffer) > _MAX_STREAM_LINE_BYTES:
                # A single line longer than the cap: emit it and drop the rest
                # rather than growing the buffer without limit.
                yield bytes(buffer)
                buffer.clear()
        if produced:
            if buffer:
                yield bytes(buffer)
            return
    # No chunked content (an older requests, or a response double).
    yield from resp.iter_lines()


def _http_error_detail(exc: requests.HTTPError) -> str:
    """Build a useful message from an HTTPError, including the response body.

    A bare HTTPError only reports the status line; the body is where
    OpenAI-compatible servers put "model not found", "invalid api key", etc.
    """
    base = str(exc)
    response = getattr(exc, 'response', None)
    if response is None:
        return base
    body = _bounded_response_text(response)
    if not body or not body.strip():
        return base
    body = body.strip()
    try:
        parsed = json.loads(body)
        if isinstance(parsed, dict):
            err = parsed.get('error')
            if isinstance(err, dict) and err.get('message'):
                return str(err['message'])
            if isinstance(err, str) and err:
                return err
    except (json.JSONDecodeError, ValueError):
        pass
    if len(body) > 400:
        body = body[:400] + '...'
    return f"{base}: {body}"


def _parse_retry_after(exc: requests.HTTPError) -> float | None:
    """Return the server's requested Retry-After delay in seconds, if any.

    Retrying a 429 after a fixed 1 s regardless of what the server asked for
    just produces more 429s. Supports both the delta-seconds and HTTP-date
    forms.
    """
    response = getattr(exc, 'response', None)
    if response is None:
        return None
    try:
        headers = response.headers
        raw = headers.get('Retry-After') if headers else None
    except Exception:
        return None
    if not raw or not isinstance(raw, (str, int, float)):
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        pass
    try:
        from email.utils import parsedate_to_datetime
        when = parsedate_to_datetime(str(raw))
    except (TypeError, ValueError):
        return None
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc) if when.tzinfo else datetime.now()
    return max(0.0, (when - now).total_seconds())


def _backoff_delay(attempt: int) -> float:
    """Exponential backoff for *attempt* (0-based): 1s, 2s, 4s, capped."""
    return min(_BACKOFF_BASE_SECONDS * (2 ** max(0, attempt)), _BACKOFF_MAX_SECONDS)


@dataclass
class APIPreset:
    name: str
    base_url: str
    api_key: str
    model: str
    temperature: float = 1.0
    max_tokens: int = 2048
    top_p: float = 1.0
    top_k: int = 0
    min_p: float = 0.0
    context_size: int = 8192
    frequency_penalty: float = 0.0
    presence_penalty: float = 0.0
    seed: int = -1
    retry_attempts: int = 2
    # Custom stop sequences (SillyTavern's "Custom Stopping Strings").
    stop: list[str] = field(default_factory=list)
    # Advanced samplers (llama.cpp-compatible names).  Only the local
    # llama.cpp-family servers implement them, and each spells them slightly
    # differently - see ``src.provider_capabilities``, which maps them onto
    # the key the active provider expects and leaves out the ones it does
    # not know.
    # Mirostat: 0 = off, 1 = Mirostat v1, 2 = Mirostat v2.
    mirostat: int = 0
    mirostat_tau: float = 5.0
    mirostat_eta: float = 0.1
    # DRY ("Don't Repeat Yourself") repetition sampler.
    dry_multiplier: float = 0.0  # 0 = off
    dry_base: float = 1.75
    dry_allowed_length: int = 2
    dry_penalty_range: int = 0  # 0 = full context window
    # XTC ("Exclude Top Choices").
    xtc_threshold: float = 0.1
    xtc_probability: float = 0.0  # 0 = off
    # Typical P (1.0 = off).
    typical_p: float = 1.0
    # Dynamic temperature (0 range = off).
    dynatemp_range: float = 0.0
    dynatemp_exponent: float = 1.0
    # Quadratic / smoothing sampling (0 = off).
    smoothing_factor: float = 0.0
    min_tokens_to_keep: int = 1
    # Repetition penalty (1.0 = off).  The llama.cpp spelling, which is what
    # LM Studio's OpenAI-compatible endpoint accepts; OpenRouter spells the
    # same thing ``repetition_penalty`` and ignores this key.
    repeat_penalty: float = 1.0
    # Rewrite chat messages into a single leading system message followed by
    # strict user/assistant alternation before sending.  Required by Llama
    # 3.x / Gemma chat templates; harmless elsewhere but it does change the
    # wire format (trailing system blocks glue onto the last user turn).
    strict_chat_template: bool = False


PRESETS: dict[str, APIPreset] = {
    # -- Local servers (OpenAI-compatible) ----------------------------------
    'LM Studio': APIPreset(
        name='LM Studio',
        base_url='http://localhost:1234/v1',
        api_key='lm-studio',
        model='',
        temperature=1.0,
        max_tokens=2048,
        min_p=0.05,
    ),
    'Ollama': APIPreset(
        name='Ollama',
        base_url='http://localhost:11434/v1',
        api_key='ollama',
        model='llama3',
        temperature=1.0,
        max_tokens=2048,
        min_p=0.05,
    ),
    'text-generation-webui': APIPreset(
        # Needs the "--api" flag; the OpenAI-compatible API listens on 5000.
        name='text-generation-webui',
        base_url='http://localhost:5000/v1',
        api_key='textgenwebui',
        model='',
        temperature=1.0,
        max_tokens=2048,
    ),
    'KoboldCpp': APIPreset(
        name='KoboldCpp',
        base_url='http://localhost:5001/v1',
        api_key='koboldcpp',
        model='',
        temperature=1.0,
        max_tokens=2048,
    ),
    'llama.cpp': APIPreset(
        # llama-server's OpenAI-compatible endpoint.
        name='llama.cpp',
        base_url='http://localhost:8080/v1',
        api_key='llama-cpp',
        model='',
        temperature=1.0,
        max_tokens=2048,
    ),
    'vLLM': APIPreset(
        name='vLLM',
        base_url='http://localhost:8000/v1',
        api_key='vllm',
        model='',
        temperature=1.0,
        max_tokens=2048,
    ),
    'Jan': APIPreset(
        name='Jan',
        base_url='http://localhost:1337/v1',
        api_key='jan',
        model='',
        temperature=1.0,
        max_tokens=2048,
    ),
    'GPT4All': APIPreset(
        name='GPT4All',
        base_url='http://localhost:4891/v1',
        api_key='gpt4all',
        model='',
        temperature=1.0,
        max_tokens=2048,
    ),
    'LocalAI': APIPreset(
        name='LocalAI',
        base_url='http://localhost:8080/v1',
        api_key='localai',
        model='',
        temperature=1.0,
        max_tokens=2048,
    ),
    # -- Remote APIs ---------------------------------------------------------
    'OpenRouter': APIPreset(
        name='OpenRouter',
        base_url='https://openrouter.ai/api/v1',
        api_key='',
        model='',
        temperature=1.0,
        max_tokens=2048,
        min_p=0.05,
    ),
    'OpenAI': APIPreset(
        # OpenAI rejects unknown parameters such as ``min_p``/``top_k``,
        # so this preset keeps them at their neutral values.
        name='OpenAI',
        base_url='https://api.openai.com/v1',
        api_key='',
        model='gpt-4o-mini',
        temperature=1.0,
        max_tokens=2048,
    ),
    'Gemini': APIPreset(
        name='Gemini',
        base_url='https://generativelanguage.googleapis.com/v1beta/openai',
        api_key='',
        model='gemini-2.0-flash',
        temperature=1.0,
        max_tokens=2048,
    ),
    'Mistral': APIPreset(
        name='Mistral',
        base_url='https://api.mistral.ai/v1',
        api_key='',
        model='mistral-small-latest',
        temperature=1.0,
        max_tokens=2048,
    ),
    'DeepSeek': APIPreset(
        name='DeepSeek',
        base_url='https://api.deepseek.com',
        api_key='',
        model='deepseek-chat',
        temperature=1.0,
        max_tokens=2048,
    ),
    'Groq': APIPreset(
        name='Groq',
        base_url='https://api.groq.com/openai/v1',
        api_key='',
        model='',
        temperature=1.0,
        max_tokens=2048,
    ),
    'xAI Grok': APIPreset(
        name='xAI Grok',
        base_url='https://api.x.ai/v1',
        api_key='',
        model='',
        temperature=1.0,
        max_tokens=2048,
    ),
    'Custom': APIPreset(
        name='Custom',
        base_url='',
        api_key='',
        model='',
        temperature=1.0,
        max_tokens=2048,
    ),
}


def _parse_stop_sequences(value) -> list[str]:
    """Normalise a saved ``stop`` value into a list of non-empty strings.

    Accepts the ``APIPreset`` list form or the comma/semicolon/newline
    separated string a user may type into the settings field.
    """
    if not value:
        return []
    if isinstance(value, str):
        parts = re.split(r'[,;\n]', value)
    elif isinstance(value, (list, tuple)):
        parts = list(value)
    else:
        return []
    out: list[str] = []
    for part in parts:
        text = str(part).strip()
        if text and text not in out:
            out.append(text)
    return out


def preset_from_saved(saved: dict) -> APIPreset:
    """Build an effective :class:`APIPreset` from a saved settings dict.

    Missing/empty saved fields fall back to the matching ``PRESETS`` entry so
    per-provider defaults (base URL, placeholder key, model) are honoured.
    """
    name = saved.get('preset_name') or 'LM Studio'
    base = PRESETS.get(name, PRESETS['LM Studio'])
    return APIPreset(
        name=name,
        base_url=saved.get('base_url') or base.base_url,
        api_key=saved.get('api_key') or base.api_key,
        model=saved.get('model') or base.model,
        temperature=_as_float(saved.get('temperature', base.temperature),
                              base.temperature),
        max_tokens=_as_int(saved.get('max_tokens', base.max_tokens),
                           base.max_tokens),
        top_p=_as_float(saved.get('top_p', base.top_p), base.top_p),
        top_k=_as_int(saved.get('top_k', base.top_k), base.top_k),
        min_p=_as_float(saved.get('min_p', base.min_p), base.min_p),
        context_size=_as_int(saved.get('context_size', base.context_size),
                              base.context_size),
        frequency_penalty=_as_float(
            saved.get('frequency_penalty', base.frequency_penalty),
            base.frequency_penalty,
        ),
        presence_penalty=_as_float(
            saved.get('presence_penalty', base.presence_penalty),
            base.presence_penalty,
        ),
        seed=_as_int(saved.get('seed', base.seed), base.seed),
        retry_attempts=_as_int(saved.get('retry_attempts', base.retry_attempts),
                                base.retry_attempts),
        stop=_parse_stop_sequences(saved.get('stop')),
        mirostat=_as_int(saved.get('mirostat', base.mirostat), base.mirostat),
        mirostat_tau=_as_float(saved.get('mirostat_tau', base.mirostat_tau),
                               base.mirostat_tau),
        mirostat_eta=_as_float(saved.get('mirostat_eta', base.mirostat_eta),
                               base.mirostat_eta),
        dry_multiplier=_as_float(saved.get('dry_multiplier', base.dry_multiplier),
                                 base.dry_multiplier),
        dry_base=_as_float(saved.get('dry_base', base.dry_base), base.dry_base),
        dry_allowed_length=_as_int(
            saved.get('dry_allowed_length', base.dry_allowed_length),
            base.dry_allowed_length,
        ),
        dry_penalty_range=_as_int(
            saved.get('dry_penalty_range', base.dry_penalty_range),
            base.dry_penalty_range,
        ),
        xtc_threshold=_as_float(saved.get('xtc_threshold', base.xtc_threshold),
                                base.xtc_threshold),
        xtc_probability=_as_float(
            saved.get('xtc_probability', base.xtc_probability),
            base.xtc_probability,
        ),
        typical_p=_as_float(saved.get('typical_p', base.typical_p),
                            base.typical_p),
        dynatemp_range=_as_float(saved.get('dynatemp_range', base.dynatemp_range),
                                 base.dynatemp_range),
        dynatemp_exponent=_as_float(
            saved.get('dynatemp_exponent', base.dynatemp_exponent),
            base.dynatemp_exponent,
        ),
        smoothing_factor=_as_float(
            saved.get('smoothing_factor', base.smoothing_factor),
            base.smoothing_factor,
        ),
        min_tokens_to_keep=_as_int(
            saved.get('min_tokens_to_keep', base.min_tokens_to_keep),
            base.min_tokens_to_keep,
        ),
        repeat_penalty=_as_float(saved.get('repeat_penalty', base.repeat_penalty),
                                 base.repeat_penalty),
        strict_chat_template=_as_bool(
            saved.get('strict_chat_template', base.strict_chat_template)
        ),
    )


def _as_bool(value) -> bool:
    """Coerce a saved settings value to bool (QSettings stores 'true'/'false')."""
    if isinstance(value, str):
        return value.strip().lower() in ('true', '1', 'yes', 'on')
    return bool(value)


def _as_int(value, default):
    """Coerce a saved settings value to int, falling back to *default*.

    The saved values come from the registry, but they are also loaded from
    hand-edited config files and from raw dicts; a non-numeric value must
    not raise out of the settings path.
    """
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _as_float(value, default: float) -> float:
    """Coerce a saved settings value to float, falling back to *default*."""
    if value is None:
        return default
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if result != result or result in (float('inf'), float('-inf')):
        return default
    return result


def vary_seed(preset: APIPreset, bump: int) -> APIPreset:
    """Return *preset* with its fixed seed advanced by *bump*.

    Regenerating with an identical payload and a fixed seed replays the exact
    same reply; bumping the seed per regeneration keeps variety while leaving
    random-seed sessions (``seed < 0``) untouched.  Pure function.
    """
    if bump <= 0 or preset.seed is None or preset.seed < 0:
        return preset
    return replace(preset, seed=preset.seed + bump)


def with_sampling_override(
    preset: APIPreset,
    temperature: float | None = None,
    min_p: float | None = None,
    model: str | None = None,
    context_size: int | None = None,
) -> APIPreset:
    """Return *preset* with per-chat overrides applied.

    ``None`` values keep the preset's setting.  Pure function.
    """
    changes: dict = {}
    if temperature is not None and temperature != preset.temperature:
        changes['temperature'] = max(0.0, float(temperature))
    if min_p is not None and min_p != preset.min_p:
        changes['min_p'] = min(1.0, max(0.0, float(min_p)))
    if model is not None and model != preset.model:
        changes['model'] = str(model)
    if context_size is not None and context_size != preset.context_size:
        changes['context_size'] = max(0, int(context_size))
    if not changes:
        return preset
    return replace(preset, **changes)


class AIClient:
    def __init__(self, preset: APIPreset):
        self.preset = preset
        self._session: Optional[requests.Session] = None
        self._response = None   # the in-flight streamed response, for cancel()
        self._adapter = None    # the cancellable adapter, for cancel()
        self._cancel_requested = False
        self._stream_chars = 0  # decoded characters yielded by the current reply
        # The messages of the last request, exactly as they were sent (after
        # trimming and injection splicing).  The usage ledger counts them so
        # the recorded token numbers describe the real prompt.
        self.last_request_messages: list[dict[str, Any]] = []

    def _permanent_config_message(self, exc: BaseException) -> str:
        """Client-aware variant that also strips the configured API key."""
        return _permanent_config_message(exc, self._known_secrets())

    def _redact(self, message: object) -> str:
        """Redact *message*, including this client's literal API key.

        The pattern-based filter cannot know every key format, so the
        configured key is removed by value as well.  Without this a server
        that echoes the key back ("Invalid API key provided: <KEY>") leaks it
        into the plaintext log and into a message box.
        """
        return redact_secrets(message, self._known_secrets())

    def _known_secrets(self) -> list[str]:
        """Literal values that must never appear in a message."""
        secrets = []
        key = getattr(self.preset, 'api_key', '') or ''
        if key.strip():
            secrets.append(key.strip())
        return secrets

    def _headers(self) -> dict[str, str]:
        h = {'Content-Type': 'application/json'}
        if self.preset.api_key:
            # Trim (stray whitespace comes from copy-paste) and refuse values
            # that cannot go on the wire BEFORE requests does: its
            # InvalidHeader exception embeds the whole header value in the
            # message, which would leak the key into logs and the UI.
            key = self.preset.api_key.strip()
            if '\r' in key or '\n' in key:
                raise RuntimeError(
                    'The API key contains invalid characters. '
                    'Check the key in Settings.'
                ) from None
            try:
                key.encode('latin-1')
            except UnicodeEncodeError:
                raise RuntimeError(
                    'The API key contains characters that cannot be sent '
                    '(e.g. smart quotes). Re-enter it in Settings.'
                ) from None
            if is_cleartext_remote(self.preset.base_url):
                # Only the host name is logged: a base URL with userinfo
                # ("https://user:pass@host") would otherwise write the
                # password to the log in cleartext.
                logger.warning(
                    "Sending the API key to %s over unencrypted http://",
                    _url_host(self.preset.base_url),
                )
            h['Authorization'] = f'Bearer {key}'
        return h

    def _url(self) -> str:
        """The chat-completions endpoint for the configured base URL.

        The path is inserted *before* any query string: a base URL like
        "https://host/v1?api-version=2024" used to become
        ".../v1?api-version=2024/chat/completions", which sent the path
        segment into the query and made the request 404.

        Userinfo ("https://user:pass@host") is rejected: requests turns it
        into HTTP Basic auth and *overwrites* the Bearer header, so the API
        key would silently never be sent - and the URL password would become
        the credential on the wire.
        """
        base = (self.preset.base_url or '').strip()
        parsed = urlsplit(base)
        if parsed.username or parsed.password:
            raise RuntimeError(
                'The base URL must not contain a user name or password. '
                'Put the key in the API key field instead.'
            )
        path = parsed.path.rstrip('/')
        url = urlunsplit((parsed.scheme, parsed.netloc, f'{path}/chat/completions',
                          parsed.query, ''))
        return url

    def cancel(self) -> None:
        """Request cancellation of any in-progress generation.

        ``Session.close()`` only discards *idle* pooled connections, so it did
        **not** interrupt a response being read - a server that stalled
        mid-stream kept the request blocked for the full 120 s timeout, leaving
        Send/Cancel disabled the whole time. The in-flight response's socket is
        shut down directly, which does raise out of ``iter_lines()``
        immediately.

        The flag is never cleared by starting a request: ``cancel()`` may arrive
        from the UI thread before the worker thread has started iterating, and
        wiping it would silently lose the cancellation.  Construct a fresh
        :class:`AIClient` per generation instead of reusing a cancelled one.
        """
        self._cancel_requested = True
        response = self._response
        if response is not None:
            # Force-close the socket; without this a stalled read blocks until
            # the request timeout expires.
            try:
                raw = getattr(response, 'raw', None)
                sock = getattr(raw, '_fp', None)
                if sock is not None:
                    sock.close()
            except Exception:
                pass
            try:
                response.close()
            except Exception:
                pass
        # The response object only exists once the headers have arrived, so
        # the connection captured by the adapter is what makes Cancel work
        # while the request is still waiting for them.
        adapter = getattr(self, '_adapter', None)
        if adapter is not None:
            adapter.cancelled = True
            conn = getattr(adapter, 'last_connection', None)
            if conn is not None:
                try:
                    adapter.close_connection(conn)
                except Exception:
                    pass
        session = self._session
        if session is not None:
            try:
                session.close()
            except Exception:
                pass

    @property
    def cancel_requested(self) -> bool:
        return self._cancel_requested

    def _max_retries(self) -> int:
        try:
            value = int(getattr(self.preset, 'retry_attempts', 2))
        except (TypeError, ValueError):
            return 2
        # Clamp: the value comes from user settings, and an unbounded retry
        # count turns a mis-configured preset into an infinite loop.
        return max(0, min(5, value))

    def _cancellable_sleep(self, seconds: float) -> None:
        """Sleep in short slices so cancel() interrupts the backoff promptly."""
        end = time.monotonic() + max(0.0, seconds)
        while True:
            if self._cancel_requested:
                raise RuntimeError('Generation cancelled')
            remaining = end - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.1, remaining))

    @staticmethod
    def _http_error_is_retryable(exc: requests.HTTPError) -> bool:
        status = getattr(exc.response, 'status_code', None)
        return isinstance(status, int) and status in _RETRYABLE_STATUS_CODES

    def generate(self, system_prompt: str, user_prompt: str, stream: bool = False) -> str | Generator[str, None, None]:
        messages = [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_prompt},
        ]
        # This path used to send the prompts untrimmed, so a "Fill Missing
        # Fields" request (which pastes every card field) could exceed the
        # provider's context and be rejected outright.
        messages = trim_messages(messages, self.preset.context_size, self.preset.max_tokens)
        self.last_request_messages = list(messages)
        payload = self._build_payload(messages, stream)

        if stream:
            return self._generate_stream(payload)
        return self._generate_sync(payload)

    def _build_payload(self, messages: list[dict[str, Any]], stream: bool) -> dict[str, Any]:
        """Assemble a chat-completions payload from the preset + messages.

        Standard OpenAI-compatible sampling parameters are always sent;
        the optional ones are only included when they are set to a
        non-neutral value, and only when the provider implements them at
        all - see ``src.provider_capabilities``.  A provider that does not
        know a parameter either ignores it or rejects the request, so
        sending it would be pointless at best.
        """
        p = self.preset
        provider = p.name
        # The settings dialog allows any combination, including a 512-token
        # context with a 32768 output length. Every provider rejects a
        # max_tokens larger than the window, so clamp it to something that can
        # actually be satisfied instead of sending a guaranteed 400.
        max_tokens = max(1, int(p.max_tokens or 1))
        context = max(1, int(p.context_size or 1))
        if max_tokens >= context:
            logger.warning(
                "max_tokens (%d) >= context size (%d); clamping", max_tokens, context,
            )
            max_tokens = max(1, context // 2)
        payload: dict[str, Any] = {
            'model': p.model,
            'messages': messages,
            'temperature': p.temperature,
            'max_tokens': max_tokens,
            'stream': stream,
        }
        if p.top_p < 1.0:
            self._put(payload, provider, 'top_p', p.top_p)
        if p.top_k and p.top_k > 0:
            self._put(payload, provider, 'top_k', p.top_k)
        if p.min_p and p.min_p > 0:
            self._put(payload, provider, 'min_p', p.min_p)
        if p.frequency_penalty:
            self._put(payload, provider, 'frequency_penalty', p.frequency_penalty)
        if p.presence_penalty:
            self._put(payload, provider, 'presence_penalty', p.presence_penalty)
        if p.seed is not None and p.seed >= 0:
            self._put(payload, provider, 'seed', p.seed)
        stops = [s for s in (p.stop or []) if s]
        if stops:
            self._put(payload, provider, 'stop', stops)
        if p.repeat_penalty != 1.0:
            self._put(payload, provider, 'repeat_penalty', p.repeat_penalty)
        # The advanced samplers only the llama.cpp-family servers implement.
        # Each group is switched on by its main setting; the members the
        # provider does not know are left out one by one.
        if p.mirostat and p.mirostat > 0:
            self._put(payload, provider, 'mirostat', p.mirostat)
            self._put(payload, provider, 'mirostat_tau', p.mirostat_tau)
            self._put(payload, provider, 'mirostat_eta', p.mirostat_eta)
        if p.dry_multiplier and p.dry_multiplier > 0:
            self._put(payload, provider, 'dry_multiplier', p.dry_multiplier)
            self._put(payload, provider, 'dry_base', p.dry_base)
            self._put(payload, provider, 'dry_allowed_length', p.dry_allowed_length)
            self._put(
                payload, provider, 'dry_penalty_range',
                self._dry_penalty_range(provider, p),
            )
        if p.xtc_probability and p.xtc_probability > 0:
            self._put(payload, provider, 'xtc_probability', p.xtc_probability)
            self._put(payload, provider, 'xtc_threshold', p.xtc_threshold)
        if p.typical_p < 1.0:
            self._put(payload, provider, 'typical_p', p.typical_p)
        if p.dynatemp_range and p.dynatemp_range > 0:
            self._put(payload, provider, 'dynatemp_range', p.dynatemp_range)
            self._put(payload, provider, 'dynatemp_exponent', p.dynatemp_exponent)
        if p.smoothing_factor and p.smoothing_factor > 0:
            self._put(payload, provider, 'smoothing_factor', p.smoothing_factor)
        if p.min_tokens_to_keep != 1:
            self._put(payload, provider, 'min_tokens_to_keep', p.min_tokens_to_keep)
        return payload

    @staticmethod
    def _put(payload: dict[str, Any], provider: str, setting: str, value: Any) -> None:
        """Put *value* into *payload* under the key *provider* expects.

        A setting the provider does not implement is left out entirely:
        most servers ignore it, but OpenAI, Mistral and GPT4All reject the
        whole request over it.
        """
        key = request_key(provider, setting)
        if key is not None:
            payload[key] = value

    @staticmethod
    def _dry_penalty_range(provider: str, p: APIPreset) -> int:
        """The DRY look-back window in the shape *provider* expects.

        The app's "0 = the whole context" has no direct equivalent in the
        llama.cpp family, which counts the tokens it scans back
        (``dry_penalty_last_n``).  Their own default of 320 is the closest
        thing to "the whole context".
        """
        if request_key(provider, 'dry_penalty_range') == 'dry_penalty_last_n':
            return p.dry_penalty_range or _DRY_LOOKBACK_DEFAULT
        return p.dry_penalty_range

    def _generate_sync(self, payload: dict, rebuild=None) -> str:
        # Route through the streaming path so cancel() can interrupt an
        # in-flight request by closing the session, and so both modes share
        # the same retry/backoff behaviour.  Retryable failures only ever
        # occur before the first chunk, so partial output is never duplicated.
        parts: list[str] = []
        for piece in self._generate_stream(dict(payload, stream=True), rebuild=rebuild):
            parts.append(piece)
        content = ''.join(parts)
        if not content:
            logger.warning("API returned empty content")
        return content

    def _stream_once(self, payload: dict) -> Generator[str, None, None]:
        """Run one streaming request attempt.

        Raises :class:`_RetryableError` for transient failures that happened
        before any content was produced (so the caller may retry), plain
        ``RuntimeError`` for cancellation or in-stream API errors, and
        propagates ``requests.HTTPError`` for permanent HTTP failures.
        """
        self._session = requests.Session()
        # A cancellable adapter so Cancel works while the request is still
        # waiting for the response headers (see _CancellableHTTPAdapter).
        adapter = _CancellableHTTPAdapter()
        self._session.mount('http://', adapter)
        self._session.mount('https://', adapter)
        self._adapter = adapter
        self._response = None
        produced = False
        resp = None
        # A *total* budget for the request.  The socket timeout (120 s) is a
        # per-read deadline: an endpoint that emits one byte every 100 s
        # would keep the worker thread blocked forever.
        deadline = time.monotonic() + _MAX_REQUEST_SECONDS
        try:
            try:
                resp = self._session.post(
                    self._url(),
                    headers=self._headers(),
                    json=payload,
                    stream=True,
                    # The connect timeout is short on purpose: a cancel
                    # cannot close a socket that does not exist yet, so the
                    # window in which the connection is being established
                    # (DNS, TCP, TLS) is one in which Cancel is ineffective.
                    timeout=(10, 120),
                )
                self._response = resp
                if self._cancel_requested:
                    raise RuntimeError('Generation cancelled')
                resp.raise_for_status()
            except requests.HTTPError as exc:
                # Surface the server's explanation: a bare HTTPError string for
                # a 400 is just "400 Client Error", which tells the user
                # nothing about (say) an unknown model.
                detail = _http_error_detail(exc)
                if self._http_error_is_retryable(exc):
                    retry_after = _parse_retry_after(exc)
                    # The original HTTPError is kept as the cause, but its
                    # message carries the full request URL - which may hold
                    # the API key as a query parameter.  Redact it in place so
                    # the chained traceback (written to the log and shown in
                    # the UI) cannot leak the key.
                    exc.args = (self._redact(str(exc)),) + tuple(exc.args[1:])
                    raise _RetryableError(
                        f"API error: {self._redact(detail)}", retry_after=retry_after,
                    ) from exc
                # Keep raising HTTPError (callers and tests rely on the type)
                # but fold the server's explanation into the message, which
                # would otherwise be just "401 Client Error: Unauthorized".
                if detail != str(exc):
                    exc.args = (self._redact(detail),) + tuple(exc.args[1:])
                raise
            except _PERMANENT_CONFIG_ERRORS as exc:
                # Permanent config problem: no retry, and no verbatim message
                # (it can carry the API key). ``from None`` keeps the original
                # exception - and its leaky message - out of every traceback.
                raise RuntimeError(self._permanent_config_message(exc)) from None
            except _RETRYABLE_TRANSPORT_ERRORS as exc:
                if produced:
                    raise
                raise _RetryableError(
                    f"Connection failed: {self._redact(exc)}"
                ) from None

            # SSE: an event may span several "data:" lines which must be joined
            # with a newline. Most providers instead send one complete JSON
            # object per line with no blank separator, so a line that already
            # parses on its own is emitted immediately rather than buffered.
            pending: list[str] = []

            def flush() -> Generator[str, None, None]:
                # ``produced`` must flip here too: content delivered through
                # a multi-line SSE event counts as output, and a later
                # transport error must not retry and duplicate it.
                nonlocal produced
                if pending:
                    joined = '\n'.join(pending)
                    pending.clear()
                    for piece in self._emit_sse_event(joined):
                        produced = True
                        yield piece

            for line in _iter_sse_lines(resp):
                if self._cancel_requested:
                    raise RuntimeError('Generation cancelled')
                if time.monotonic() > deadline:
                    raise RuntimeError(
                        'The request took too long and was stopped after '
                        f'{int(_MAX_REQUEST_SECONDS // 60)} minutes.',
                    )
                if not line:
                    yield from flush()
                    continue
                line_str = line.decode('utf-8', errors='replace')
                if not line_str.startswith('data:'):
                    # A non-data field (event:, id:, comment) terminates an
                    # event per the SSE spec.
                    yield from flush()
                    # Some gateways ignore "stream": true and answer with a
                    # plain (non-SSE) JSON body, one object per line.  Such a
                    # line is a complete reply, not a field of an event, so
                    # it is parsed and emitted instead of being dropped -
                    # otherwise the user's reply vanishes silently.
                    stripped = line_str.strip()
                    if stripped.startswith('{'):
                        yield from flush()
                        for piece in self._emit_sse_event(stripped):
                            produced = True
                            yield piece
                    continue
                data_str = line_str[5:].lstrip(' ')
                if data_str.strip() == '[DONE]':
                    break
                if _is_complete_json(data_str):
                    yield from flush()
                    for piece in self._emit_sse_event(data_str):
                        produced = True
                        yield piece
                else:
                    pending.append(data_str)
            yield from flush()
        except _RETRYABLE_TRANSPORT_ERRORS as exc:
            if produced or self._cancel_requested:
                # A forced socket close during cancel() surfaces here; report it
                # as a cancellation rather than a connection failure (and never
                # retry it).
                if self._cancel_requested:
                    raise RuntimeError('Generation cancelled') from exc
                raise
            raise _RetryableError(
                f"Connection failed: {self._redact(exc)}"
            ) from None
        except requests.HTTPError:
            # Already enriched with the response body above; the type is part
            # of the contract callers rely on.
            raise
        except requests.RequestException as exc:
            # Anything else from requests (protocol error, undecodable body)
            # is a genuine failure, but must not surface as a bare requests
            # type in the UI.
            if produced:
                raise
            raise RuntimeError(
                f"API request failed: {self._redact(exc)}"
            ) from None
        finally:
            # Always release the connection: without this a streamed response
            # holds its socket until the session is collected.
            self._response = None
            if resp is not None:
                try:
                    resp.close()
                except Exception:
                    pass
            session = self._session
            self._session = None
            if session is not None:
                try:
                    session.close()
                except Exception:
                    pass

    def _emit_sse_event(self, data_str: str) -> Generator[str, None, None]:
        """Yield the text content of one assembled SSE event payload."""
        try:
            chunk = json.loads(data_str)
        except (json.JSONDecodeError, RecursionError, ValueError):
            logger.debug("Skipping unparsable SSE payload: %r", data_str[:200])
            return
        if not isinstance(chunk, dict):
            # Some gateways emit a bare array for an error payload; treat it
            # as "no content" instead of raising AttributeError on .get().
            return
        if chunk.get('error'):
            err = chunk['error']
            msg = err.get('message', str(err)) if isinstance(err, dict) else str(err)
            # The server's prose is the one message that routinely echoes the
            # key back ("Invalid API key provided: <KEY>").  It must be
            # redacted like every other server-controlled string.
            raise RuntimeError(f"API stream error: {self._redact(msg)}")
        choices = chunk.get('choices') or []
        if not choices:
            return
        first = choices[0]
        if not isinstance(first, dict):
            return
        # A streamed chunk carries ``delta.content``; a complete (non-SSE)
        # body carries ``message.content``.  Some gateways also use
        # ``text``.  All three are accepted so a server that ignores
        # "stream": true does not silently return an empty reply.
        delta = first.get('delta') or {}
        content = None
        if isinstance(delta, dict):
            content = delta.get('content')
        if not content:
            message = first.get('message') or {}
            if isinstance(message, dict):
                content = message.get('content')
        if not content:
            content = first.get('text')
        if content:
            content = str(content)
            # A local ceiling: ``max_tokens`` is advisory to the server and
            # bounds nothing on the client side.
            self._stream_chars += len(content)
            if self._stream_chars > _MAX_STREAM_CHARS:
                room = max(0, _MAX_STREAM_CHARS - (self._stream_chars - len(content)))
                logger.warning(
                    "The reply exceeded %d characters; truncating.",
                    _MAX_STREAM_CHARS,
                )
                self._stream_chars = _MAX_STREAM_CHARS
                if room:
                    yield content[:room]
                return
            yield content

    def _generate_stream(self, payload: dict, rebuild=None) -> Generator[str, None, None]:
        """Stream one chat completion, retrying transient failures.

        *rebuild* (optional) returns a fresh streaming payload with the
        messages normalised for strict chat templates; it is used at most once
        when the server rejects the message layout (see
        :func:`is_chat_template_error`).
        """
        max_retries = self._max_retries()
        attempt = 0
        template_retry_used = False
        self._stream_chars = 0
        while True:
            if self._cancel_requested:
                raise RuntimeError('Generation cancelled')
            produced = False
            try:
                for piece in self._stream_once(payload):
                    produced = True
                    yield piece
                if not produced:
                    # The server answered, but the reply was empty (or was
                    # formatted in a way the client does not understand).
                    # Saying so is far better than showing the user a blank
                    # message bubble with no explanation.
                    logger.warning(
                        "The API returned an empty reply; the response did not "
                        "contain any content.",
                    )
                return
            except _RetryableError as exc:
                if attempt >= max_retries:
                    raise RuntimeError(str(exc)) from exc
                # Honour Retry-After when the server sent one; fall back to
                # exponential backoff otherwise.
                delay = exc.retry_after if exc.retry_after is not None else _backoff_delay(attempt)
                delay = min(delay, _BACKOFF_MAX_SECONDS * 5)
                logger.warning(
                    "API request failed (%s); retrying in %.0f s (attempt %d/%d)",
                    exc, delay, attempt + 1, max_retries,
                )
                self._cancellable_sleep(delay)
                attempt += 1
            except Exception as exc:
                # The chat template rejected the message layout (Llama 3.x /
                # Gemma require strict user/assistant alternation).  The
                # rejection always arrives before any content, so resending
                # with normalised roles cannot duplicate output.  This does
                # not consume a regular retry attempt: it repairs the request
                # rather than retrying the same one.
                if (
                    rebuild is not None
                    and not template_retry_used
                    and not produced
                    and not self._cancel_requested
                    and is_chat_template_error(exc)
                ):
                    template_retry_used = True
                    logger.warning(
                        "Chat template rejected the message layout (%s); "
                        "resending with normalised user/assistant roles. "
                        "Enable 'strict chat template' for this provider to "
                        "always send the normalised form.",
                        self._redact(exc),
                    )
                    payload = rebuild()
                    continue
                raise

    def generate_tags(self, card: CharacterCard, extra: str = '', length: str = '',
                      style: str = 'Standard') -> list[str]:
        system, user = ai_prompts.build_tags_prompts(card, extra=extra, length=length, style=style)
        result = self.generate(system, user)
        return _parse_tags(result)

    def generate_missing_tags(self, card: CharacterCard, extra: str = '', length: str = '',
                              style: str = 'Standard') -> list[str]:
        system, user = ai_prompts.build_missing_tags_prompts(card, extra=extra, length=length, style=style)
        result = self.generate(system, user)
        return _parse_tags(result)

    def generate_summary(self, card: CharacterCard, extra: str = '', length: str = '',
                         style: str = 'Standard') -> str:
        system, user = ai_prompts.build_summary_prompts(card, extra=extra, length=length, style=style)
        return self.generate(system, user)

    def generate_character(self, concept: str, extra: str = '', length: str = '',
                           style: str = 'Standard') -> CharacterCard:
        system, user = ai_prompts.build_character_prompts(concept, extra, length=length, style=style)
        result = self.generate(system, user)
        raw = _extract_json(result)
        if raw is None:
            raise ValueError("AI did not return valid JSON")
        return CharacterCard.from_spec_dict(raw)

    def generate_chat(
        self,
        messages: list[dict[str, str]],
        system: str,
        stream: bool = False,
        post_history: str = '',
        jailbreak: str = '',
        examples_block: str = '',
        injections: Optional[list] = None,
        conversation_start_header: Optional[str] = None,
    ) -> str | Generator[str, None, None]:
        """Generate a chat completion over a full message history.

        Unlike :meth:`generate`, this accepts an arbitrary message list so the
        chat-preview dialog can accumulate a multi-turn conversation.  The
        system message is prepended to *messages*.  Trailing system blocks are
        appended after the history in this order: *examples_block* (few-shot
        example dialogue), *post_history* (the card's post-history
        instructions), *jailbreak* (the user's own trailing instructions).
        Each is injected at depth 0, mirroring SillyTavern: instructions
        placed after the history steer the model far more strongly than the
        same text buried in the system prompt.

        *injections* are depth-targeted messages (Author's Note, ``at_depth``
        world info — see ``chat_builder.PromptInjection``) spliced into the
        history *after* trimming at ``depth`` messages from the end.  Their
        tokens are reserved from the trim budget so the payload cannot
        overshoot the context window.

        *conversation_start_header* labels the system fold-in of a leading
        assistant greeting when strict chat-template normalisation runs (the
        context template's header; defaults to the classic one).
        """
        from src.chat_builder import CONVERSATION_START_HEADER
        header = conversation_start_header or CONVERSATION_START_HEADER
        full_messages: list[dict[str, Any]] = []
        if system and system.strip():
            full_messages.append({'role': 'system', 'content': system})
        full_messages.extend(messages)
        trailing = [
            block for block in (examples_block, post_history, jailbreak)
            if block and block.strip()
        ]
        for block in trailing:
            full_messages.append({'role': 'system', 'content': block})
        # Trim with the trailing blocks already in place: they used to be
        # appended afterwards, so unbounded card-controlled PHI text was never
        # charged against the context budget and the request could overshoot
        # by its full length. trim_messages keeps a trailing system message.
        injections = list(injections or [])
        from src.token_counter import count_tokens
        reserve = sum(
            _message_token_cost(
                {'role': _injection_role(inj), 'content': _injection_content(inj)},
                count_tokens,
            )
            for inj in injections
        )
        full_messages = trim_messages(
            full_messages, self.preset.context_size, self.preset.max_tokens,
            reserve=reserve,
        )
        if injections:
            # Depth is counted from the end of the CONVERSATION, not from the
            # end of the payload: the trailing blocks (examples /
            # post-history / jailbreak) are depth-0 blocks themselves, and
            # counting from after them landed every injection that many
            # messages closer to the end than the Context Inspector shows
            # (and closer than SillyTavern places it).
            trailing_count = len(trailing)
            if trailing_count:
                head, tail = (
                    full_messages[:-trailing_count],
                    full_messages[-trailing_count:],
                )
                full_messages = splice_injections(head, injections) + tail
            else:
                full_messages = splice_injections(full_messages, injections)
        # Strict chat templates (Llama 3.x / Gemma) only accept one system
        # message followed by user/assistant alternation.  With the
        # per-provider flag on, repair the layout up front; otherwise send it
        # as assembled and let the retry in ``_generate_stream`` normalise on
        # the fly when the server rejects it (see ``is_chat_template_error``).
        rebuild = None
        if getattr(self.preset, 'strict_chat_template', False):
            from src.chat_builder import normalize_chat_roles
            full_messages = normalize_chat_roles(
                full_messages, conversation_start_header=header,
            )
        else:
            def _rebuild() -> dict[str, Any]:
                # Always rebuilds the streaming form: the retry lives inside
                # ``_generate_stream``, which posts stream=True payloads.
                from src.chat_builder import normalize_chat_roles
                return self._build_payload(
                    normalize_chat_roles(
                        full_messages, conversation_start_header=header,
                    ),
                    True,
                )

            rebuild = _rebuild
        # Remember the exact payload messages: the usage ledger counts them
        # so the recorded token numbers describe the prompt that was sent.
        self.last_request_messages = list(full_messages)
        payload = self._build_payload(full_messages, stream)
        if stream:
            return self._generate_stream(payload, rebuild=rebuild)
        return self._generate_sync(payload, rebuild=rebuild)


def _injection_parts(inj: Any) -> tuple[str, str, int]:
    """Normalise an injection (dict or ``PromptInjection``-like) to its fields."""
    if isinstance(inj, dict):
        return (
            str(inj.get('role', 'system')),
            str(inj.get('content', '') or ''),
            int(inj.get('depth', 0) or 0),
        )
    return (
        str(getattr(inj, 'role', 'system')),
        str(getattr(inj, 'content', '') or ''),
        int(getattr(inj, 'depth', 0) or 0),
    )


def _injection_role(inj: Any) -> str:
    return _injection_parts(inj)[0]


def _injection_content(inj: Any) -> str:
    return _injection_parts(inj)[1]


def _injection_title(inj: Any) -> str:
    if isinstance(inj, dict):
        return str(inj.get('title', '') or '')
    return str(getattr(inj, 'title', '') or '')


def splice_injections(
    messages: list[dict[str, Any]],
    injections: Optional[list] = None,
    with_titles: bool = False,
) -> list[dict[str, Any]]:
    """Return *messages* with each injection spliced at its depth from the end.

    Depth counts the messages that follow the insertion point (0 = appended
    after everything, 4 = four messages from the end — SillyTavern's
    Author's Note / ``at_depth`` world-info semantics).  Deeper injections
    are placed first so shallower ones stay anchored to the end.  A leading
    system message keeps its position at the front.  With *with_titles* the
    spliced dicts carry an extra ``_title`` key (for the context inspector;
    ``history_for_api`` drops it).  Pure function.
    """
    injections = list(injections or [])
    if not injections:
        return list(messages)
    lead: list[dict[str, Any]] = []
    rest = list(messages)
    if rest and rest[0].get('role') == 'system':
        lead = [rest.pop(0)]
    by_depth: dict[int, list] = {}
    for inj in injections:
        depth = max(0, _injection_parts(inj)[2])
        by_depth.setdefault(depth, []).append(inj)
    for depth in sorted(by_depth, reverse=True):
        pos = min(max(0, len(rest) - depth), len(rest))
        chunk: list[dict[str, Any]] = []
        for inj in by_depth[depth]:
            msg: dict[str, Any] = {
                'role': _injection_role(inj),
                'content': _injection_content(inj),
            }
            if with_titles:
                msg['_title'] = _injection_title(inj) or f'Injection (depth {depth})'
            chunk.append(msg)
        rest[pos:pos] = chunk
    return lead + rest


def _content_text(content: Any) -> str:
    """Extract tokenizable text from a message ``content`` field.

    Handles both plain-string content and OpenAI-style multimodal list content
    (concatenating only the ``text`` parts, ignoring ``image_url`` parts).
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get('type') == 'text':
                parts.append(item.get('text', '') or '')
        return '\n'.join(parts)
    return ''


# Flat token estimate charged per image part during context trimming.
# Vision models bill images at roughly 800–2000+ tokens; without charging
# anything, a multi-image conversation would silently overflow the
# provider's context window even though trim_messages believed it fit.
IMAGE_PART_TOKEN_ESTIMATE = 1024


def _message_token_cost(msg: dict, token_fn) -> int:
    """Token cost of a message, including a flat estimate per image part."""
    content = msg.get('content', '')
    cost = token_fn(_content_text(content) or '')
    if isinstance(content, list):
        cost += sum(
            IMAGE_PART_TOKEN_ESTIMATE
            for item in content
            if isinstance(item, dict) and item.get('type') == 'image_url'
        )
    return cost


def _truncate_to_tokens(msg: dict, budget: int, token_fn):
    """Return *msg* with its text trimmed to roughly *budget* tokens.

    Used only when the system prompt alone would overflow the context window.
    Returns None when the message has no trimmable text (e.g. multimodal), so
    the caller can leave it untouched.
    """
    content = msg.get('content')
    if not isinstance(content, str) or not content:
        return None
    if budget <= 0:
        return None
    # Binary-search the character budget: tokens/char is roughly constant.
    text = content
    if token_fn(text) <= budget:
        return msg
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if token_fn(text[:mid]) <= budget:
            lo = mid
        else:
            hi = mid - 1
    if lo <= 0:
        return None
    out = dict(msg)
    out['content'] = text[:lo] + '\n[... system prompt truncated to fit the context window ...]'
    return out


def split_for_context(
    messages: list[dict[str, Any]],
    context_size: int,
    max_tokens: int = 0,
    token_fn=None,
    reserve: int = 0,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split *messages* into ``(kept, dropped)`` for the context window.

    The system message (first element, if its role is ``system``) is always
    kept, as is the most recent message even if it alone exceeds the budget.
    Older messages are dropped from the front.  *reserve* subtracts tokens
    from the budget up-front (for depth injections spliced in later) so the
    final payload cannot overshoot.  :func:`trim_messages` returns just the
    kept half; callers that need to know *what* was evicted (e.g. to
    summarize it into memory) use this function directly.
    """
    if not messages or not context_size or context_size <= 0:
        return messages, []
    if token_fn is None:
        from src.token_counter import count_tokens as token_fn
    # max_tokens must fit inside the context: a preset with a 512 context and a
    # 32768 output length is a guaranteed 400 from every provider, and left
    # unclamped it made the budget calculation below meaningless.
    reserved = max(0, min(max_tokens, max(1, context_size - 1)))
    budget = context_size - reserved - max(0, int(reserve or 0))
    if budget <= 1:
        budget = max(1, context_size // 2)

    system = messages[0] if messages and messages[0].get('role') == 'system' else None
    rest = list(messages[1:]) if system is not None else list(messages)
    if not rest:
        return messages, []
    kept: list[dict[str, Any]] = [rest[-1]]
    if system is not None:
        system_cost = _message_token_cost(system, token_fn)
        # A system prompt (or the newest message) that alone exceeds the
        # window still has to be sent, or the request is meaningless - but the
        # output allowance then has to shrink, otherwise the *sum* provably
        # exceeds the context. Trim the system prompt as a last resort so the
        # payload stays inside the window.
        if system_cost > budget - _message_token_cost(kept[0], token_fn):
            trimmed = _truncate_to_tokens(system, budget, token_fn)
            if trimmed is not None:
                system = trimmed
                system_cost = _message_token_cost(system, token_fn)
        budget = max(1, budget - system_cost)

    total = _message_token_cost(kept[0], token_fn)
    dropped: list[dict[str, Any]] = []
    for msg in reversed(rest[:-1]):
        cost = _message_token_cost(msg, token_fn)
        if total + cost > budget:
            # ``kept`` holds rest[j+1:]; everything up to and including the
            # message that failed to fit is evicted from the front.
            dropped = rest[:len(rest) - len(kept)]
            break
        kept.insert(0, msg)
        total += cost

    # Trim normalization: when older messages were evicted and the kept
    # history now opens on an assistant reply whose turn is gone, drop that
    # orphan too so the history starts on a user turn (an assistant-first
    # fragment reads like the character speaking unprompted).  The newest
    # message always survives.
    if dropped:
        moved: list[dict[str, Any]] = []
        while len(kept) > 1 and kept[0].get('role') == 'assistant':
            moved.append(kept.pop(0))
        if moved:
            dropped = dropped + moved

    result = ([system] + kept) if system is not None else kept
    return result, dropped


def trim_messages(
    messages: list[dict[str, Any]],
    context_size: int,
    max_tokens: int = 0,
    token_fn=None,
    reserve: int = 0,
) -> list[dict[str, Any]]:
    """Trim a message list so it fits within *context_size* tokens.

    See :func:`split_for_context` for the exact retention rules.  Pure
    function (token counting injectable via *token_fn* for tests).
    """
    kept, _ = split_for_context(
        messages, context_size, max_tokens, token_fn, reserve=reserve,
    )
    return kept


#: Longest tag accepted from a model reply.  Anything longer is prose, not
#: a tag, and writing it into a card would pollute the tag filter.
_TAG_MAX_LEN = 64

#: Characters that never occur in a tag.  Their presence means the model
#: returned a JSON fragment instead of a tag.
_TAG_FORBIDDEN = '{}[]"\'`'


def _parse_tags(text: str) -> list[str]:
    """Extract a list of tags from a model reply.

    The reply is normally a JSON array, but models also answer with
    ``{"tags": [...]}``, with a list of objects, or with plain prose.  A
    value that is not a clean tag (a JSON fragment, an object, a very long
    string) is dropped rather than written into the card.
    """
    from src.ai_output import clean_tags

    if not text:
        return []
    value = _parse_json_value(text)
    if isinstance(value, list):
        return clean_tags(value)
    if isinstance(value, dict):
        for key in ('tags', 'tag', 'keywords', 'result', 'results', 'data', 'output'):
            inner = value.get(key)
            if isinstance(inner, list):
                return clean_tags(inner)
            if isinstance(inner, str):
                return clean_tags([inner])
        flat = [v for v in value.values() if isinstance(v, (str, int, float))]
        if flat:
            return clean_tags(flat)
    quoted = re.findall(r'"([^"]+)"', text)
    if quoted:
        return clean_tags(quoted)
    return clean_tags(text.split(','))


def _extract_json(text: str, expected_keys: set[str] | None = None) -> Optional[dict]:
    """Extract a JSON object from a model reply.

    Handles markdown code fences (with or without a language tag, in any
    case), JSON embedded in prose, and a one-element array of objects -
    several models answer a "return a JSON object" request with
    ``[{"...": "..."}]``.
    """
    value = _parse_json_value(text)
    if isinstance(value, list):
        objects = [item for item in value if isinstance(item, dict)]
        if not objects:
            return None
        merged: dict = {}
        for item in objects:
            for key, item_value in item.items():
                merged.setdefault(key, item_value)
        value = merged
    if not isinstance(value, dict):
        return None
    if expected_keys:
        value = _unwrap_nested(value, expected_keys)
    return value


def _strip_code_fences(text: str) -> str:
    """Remove a markdown code fence wrapped around the payload.

    The language tag is ignored (models write `````JSON`` as often as
    `````json``), and an unclosed fence is tolerated.
    """
    stripped = text.strip()
    if not stripped.startswith('```'):
        return text
    opening = re.match(r'```[^\n`]*\n?', stripped)
    if opening is None:
        return text
    body = stripped[opening.end():]
    if body.rstrip().endswith('```'):
        body = body.rstrip()[:-3]
    return body.strip()


def _parse_json_value(text: str):
    """Parse *text* into a JSON value (dict, list or scalar), or ``None``.

    Tries the whole text first, then scans for the first balanced JSON
    object and the first balanced JSON array - a greedy ``\\[.*\\]`` match
    would span from the first bracket to the LAST one, so trailing prose
    (or a second object) after the JSON made the whole reply unparsable.
    """
    if not text:
        return None
    candidate = _strip_code_fences(text).strip()
    if not candidate:
        return None
    try:
        return json.loads(candidate)
    except (json.JSONDecodeError, ValueError, RecursionError):
        pass
    # One linear scan finds every balanced span.  Scanning to the end of the
    # string once per opening bracket is quadratic: a reply of N unmatched
    # "{" characters (which a hostile endpoint, or a model that was told to
    # "output JSON" and then hallucinated braces, can produce) cost minutes
    # of CPU for a few hundred KB - and the reply cap is 4 MiB.
    spans = _balanced_spans(candidate)
    for want in ('{', '['):
        for span in spans:
            if not span.startswith(want):
                continue
            try:
                return json.loads(span)
            except (json.JSONDecodeError, ValueError, RecursionError):
                continue
    return None


def _try_parse_json(text: str) -> Optional[dict]:
    value = _parse_json_value(text)
    if isinstance(value, dict):
        return value
    return None


#: How many balanced spans are considered.  A reply with thousands of
#: sibling objects would otherwise mean thousands of ``json.loads`` calls.
_MAX_JSON_SPANS = 64


def _balanced_spans(text: str) -> list[str]:
    """Every bracket-balanced JSON value in *text*, in order of appearance.

    A single linear scan with an explicit stack.  String literals and their
    escapes are skipped so a brace inside a value does not end the object
    early.  Only the first :data:`_MAX_JSON_SPANS` spans are returned.
    """
    stack: list[tuple[str, int]] = []
    found: list[tuple[int, str]] = []
    in_string = False
    escaped = False
    for i, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == '\\':
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in '{[':
            stack.append((ch, i))
        elif ch in '}]':
            if not stack:
                continue
            opener, start = stack.pop()
            if (opener, ch) not in (('{', '}'), ('[', ']')):
                continue
            found.append((start, text[start:i + 1]))
            if len(found) >= _MAX_JSON_SPANS:
                break
    found.sort(key=lambda pair: pair[0])
    return [span for _start, span in found]


def _balanced_span(text: str, start: int, opener: str, closer: str) -> Optional[str]:
    """Return the bracket-balanced JSON value starting at *start*, or None.

    String literals and their escapes are skipped so a '}' inside a value
    doesn't end the object early.
    """
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == '\\':
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _balanced_object(text: str, start: int) -> Optional[str]:
    """Return the brace-balanced JSON object starting at *start*, or None."""
    return _balanced_span(text, start, '{', '}')


def _unwrap_nested(data: dict, expected_keys: set[str]) -> dict:
    lower_expected = {k.lower() for k in expected_keys}
    if lower_expected & {k.lower() for k in data.keys()}:
        return _normalize_keys(data)
    for value in data.values():
        if isinstance(value, dict) and lower_expected & {k.lower() for k in value.keys()}:
            return _normalize_keys(value)
    return _normalize_keys(data)


def _normalize_keys(data: dict) -> dict:
    return {k.lower(): v for k, v in data.items()}
