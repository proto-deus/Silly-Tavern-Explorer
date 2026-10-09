from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, replace
from typing import Any, Generator, Optional

import requests

from src.card_models import CharacterCard
from src import ai_prompts

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
    requests.exceptions.SSLError,
)

# Bad request *configuration* (malformed URL, illegal header value) is
# permanent: retrying the identical request cannot help. It also must never
# be wrapped verbatim - ``InvalidHeader``'s message embeds the full header
# value, i.e. the API key, which would then land in the plaintext app log.
_PERMANENT_CONFIG_ERRORS = (
    requests.exceptions.InvalidHeader,
    requests.exceptions.InvalidURL,
    # http.client raises a bare UnicodeEncodeError for a header value that
    # isn't latin-1 encodable (e.g. a pasted API key with a smart quote).
    UnicodeEncodeError,
)


def redact_secrets(message: object) -> str:
    """Return *message* with anything that looks like a credential removed.

    Applied to every transport error string that reaches a log or the UI:
    exception messages can echo request headers, including ``Authorization``.
    """
    text = str(message)
    text = re.sub(r'(?i)(bearer\s+)[^\s\'"]+', r'\1[redacted]', text)
    text = re.sub(r'(?i)((?:api[ _-]?key|authorization|token)\s*[:=]\s*)[^\s,;\'"]+',
                  r'\1[redacted]', text)
    text = re.sub(r'(?i)(header value\s*[:=]?\s*)([\'"]?)[^\s\'"]+',
                  r'\1\2[redacted]', text)
    return text


def _permanent_config_message(exc: BaseException) -> str:
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
    return redact_secrets(exc)


class _RetryableError(Exception):
    """Internal marker: a transient transport/HTTP failure worth retrying."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


def _is_complete_json(text: str) -> bool:
    """True when *text* is a self-contained JSON value."""
    try:
        json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return False
    return True


def _http_error_detail(exc: requests.HTTPError) -> str:
    """Build a useful message from an HTTPError, including the response body.

    A bare HTTPError only reports the status line; the body is where
    OpenAI-compatible servers put "model not found", "invalid api key", etc.
    """
    base = str(exc)
    response = getattr(exc, 'response', None)
    if response is None:
        return base
    try:
        body = response.text
    except Exception:
        return base
    if not isinstance(body, str) or not body.strip():
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


PRESETS: dict[str, APIPreset] = {
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
    'Custom': APIPreset(
        name='Custom',
        base_url='',
        api_key='',
        model='',
        temperature=1.0,
        max_tokens=2048,
    ),
}


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
        temperature=saved.get('temperature', base.temperature),
        max_tokens=saved.get('max_tokens', base.max_tokens),
        top_p=saved.get('top_p', base.top_p),
        top_k=saved.get('top_k', base.top_k),
        min_p=saved.get('min_p', base.min_p),
        context_size=saved.get('context_size', base.context_size),
        frequency_penalty=saved.get('frequency_penalty', base.frequency_penalty),
        presence_penalty=saved.get('presence_penalty', base.presence_penalty),
        seed=saved.get('seed', base.seed),
        retry_attempts=saved.get('retry_attempts', base.retry_attempts),
    )


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
        self._cancel_requested = False

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
            h['Authorization'] = f'Bearer {key}'
        return h

    def _url(self) -> str:
        base = self.preset.base_url.rstrip('/')
        return f'{base}/chat/completions'

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
        payload = self._build_payload(messages, stream)

        if stream:
            return self._generate_stream(payload)
        return self._generate_sync(payload)

    def _build_payload(self, messages: list[dict[str, Any]], stream: bool) -> dict[str, Any]:
        """Assemble a chat-completions payload from the preset + messages.

        Standard OpenAI-compatible sampling parameters are always sent;
        non-standard/optional ones (``top_k``, ``min_p``, penalties, seed)
        are only included when they are set to a non-neutral value so they
        don't trip up backends that reject unknown keys.
        """
        p = self.preset
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
            payload['top_p'] = p.top_p
        if p.top_k and p.top_k > 0:
            payload['top_k'] = p.top_k
        if p.min_p and p.min_p > 0:
            payload['min_p'] = p.min_p
        if p.frequency_penalty:
            payload['frequency_penalty'] = p.frequency_penalty
        if p.presence_penalty:
            payload['presence_penalty'] = p.presence_penalty
        if p.seed is not None and p.seed >= 0:
            payload['seed'] = p.seed
        return payload

    def _generate_sync(self, payload: dict) -> str:
        # Route through the streaming path so cancel() can interrupt an
        # in-flight request by closing the session, and so both modes share
        # the same retry/backoff behaviour.  Retryable failures only ever
        # occur before the first chunk, so partial output is never duplicated.
        parts: list[str] = []
        for piece in self._generate_stream(dict(payload, stream=True)):
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
        self._response = None
        produced = False
        resp = None
        try:
            try:
                resp = self._session.post(
                    self._url(),
                    headers=self._headers(),
                    json=payload,
                    stream=True,
                    timeout=120,
                )
                self._response = resp
                resp.raise_for_status()
            except requests.HTTPError as exc:
                # Surface the server's explanation: a bare HTTPError string for
                # a 400 is just "400 Client Error", which tells the user
                # nothing about (say) an unknown model.
                detail = _http_error_detail(exc)
                if self._http_error_is_retryable(exc):
                    retry_after = _parse_retry_after(exc)
                    raise _RetryableError(
                        f"API error: {redact_secrets(detail)}", retry_after=retry_after,
                    ) from exc
                # Keep raising HTTPError (callers and tests rely on the type)
                # but fold the server's explanation into the message, which
                # would otherwise be just "401 Client Error: Unauthorized".
                if detail != str(exc):
                    exc.args = (redact_secrets(detail),) + tuple(exc.args[1:])
                raise
            except _PERMANENT_CONFIG_ERRORS as exc:
                # Permanent config problem: no retry, and no verbatim message
                # (it can carry the API key). ``from None`` keeps the original
                # exception - and its leaky message - out of every traceback.
                raise RuntimeError(_permanent_config_message(exc)) from None
            except _RETRYABLE_TRANSPORT_ERRORS as exc:
                if produced:
                    raise
                raise _RetryableError(
                    f"Connection failed: {redact_secrets(exc)}"
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

            for line in resp.iter_lines():
                if self._cancel_requested:
                    raise RuntimeError('Generation cancelled')
                if not line:
                    yield from flush()
                    continue
                line_str = line.decode('utf-8', errors='replace')
                if not line_str.startswith('data:'):
                    # A non-data field (event:, id:, comment) terminates an
                    # event per the SSE spec.
                    yield from flush()
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
                f"Connection failed: {redact_secrets(exc)}"
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
                f"API request failed: {redact_secrets(exc)}"
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

    @staticmethod
    def _emit_sse_event(data_str: str) -> Generator[str, None, None]:
        """Yield the text content of one assembled SSE event payload."""
        try:
            chunk = json.loads(data_str)
        except json.JSONDecodeError:
            logger.debug("Skipping unparsable SSE payload: %r", data_str[:200])
            return
        if not isinstance(chunk, dict):
            # Some gateways emit a bare array for an error payload; treat it
            # as "no content" instead of raising AttributeError on .get().
            return
        if chunk.get('error'):
            err = chunk['error']
            msg = err.get('message', str(err)) if isinstance(err, dict) else str(err)
            raise RuntimeError(f"API stream error: {msg}")
        choices = chunk.get('choices') or []
        if not choices:
            return
        delta = choices[0].get('delta') or {}
        if isinstance(delta, dict) and delta.get('content'):
            yield delta['content']

    def _generate_stream(self, payload: dict) -> Generator[str, None, None]:
        max_retries = self._max_retries()
        attempt = 0
        while True:
            if self._cancel_requested:
                raise RuntimeError('Generation cancelled')
            try:
                yield from self._stream_once(payload)
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

    def generate_tags(self, card: CharacterCard, extra: str = '', length: str = '') -> list[str]:
        system, user = ai_prompts.build_tags_prompts(card, extra=extra, length=length)
        result = self.generate(system, user)
        return _parse_tags(result)

    def generate_missing_tags(self, card: CharacterCard, extra: str = '', length: str = '') -> list[str]:
        system, user = ai_prompts.build_missing_tags_prompts(card, extra=extra, length=length)
        result = self.generate(system, user)
        return _parse_tags(result)

    def generate_summary(self, card: CharacterCard, extra: str = '', length: str = '') -> str:
        system, user = ai_prompts.build_summary_prompts(card, extra=extra, length=length)
        return self.generate(system, user)

    def generate_character(self, concept: str, extra: str = '', length: str = '') -> CharacterCard:
        system, user = ai_prompts.build_character_prompts(concept, extra, length=length)
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
    ) -> str | Generator[str, None, None]:
        """Generate a chat completion over a full message history.

        Unlike :meth:`generate`, this accepts an arbitrary message list so the
        chat-preview dialog can accumulate a multi-turn conversation.  The
        system message is prepended to *messages*.  When *post_history* is
        non-empty it is appended as a final system-level message *after*
        trimming, mirroring SillyTavern's depth-0 injection: instructions
        placed after the history steer the model far more strongly than the
        same text buried in the system prompt.
        """
        full_messages: list[dict[str, Any]] = []
        if system and system.strip():
            full_messages.append({'role': 'system', 'content': system})
        full_messages.extend(messages)
        if post_history and post_history.strip():
            full_messages.append({'role': 'system', 'content': post_history})
        # Trim with the post-history text already in place: it used to be
        # appended afterwards, so unbounded card-controlled PHI text was never
        # charged against the context budget and the request could overshoot
        # by its full length. trim_messages keeps a trailing system message.
        full_messages = trim_messages(
            full_messages, self.preset.context_size, self.preset.max_tokens,
        )
        payload = self._build_payload(full_messages, stream)
        if stream:
            return self._generate_stream(payload)
        return self._generate_sync(payload)


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
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split *messages* into ``(kept, dropped)`` for the context window.

    The system message (first element, if its role is ``system``) is always
    kept, as is the most recent message even if it alone exceeds the budget.
    Older messages are dropped from the front.  :func:`trim_messages` returns
    just the kept half; callers that need to know *what* was evicted (e.g. to
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
    budget = context_size - reserved
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

    result = ([system] + kept) if system is not None else kept
    return result, dropped


def trim_messages(
    messages: list[dict[str, Any]],
    context_size: int,
    max_tokens: int = 0,
    token_fn=None,
) -> list[dict[str, Any]]:
    """Trim a message list so it fits within *context_size* tokens.

    See :func:`split_for_context` for the exact retention rules.  Pure
    function (token counting injectable via *token_fn* for tests).
    """
    kept, _ = split_for_context(messages, context_size, max_tokens, token_fn)
    return kept


def _parse_tags(text: str) -> list[str]:
    try:
        tags = json.loads(text)
        if isinstance(tags, list):
            return [str(t).strip().lower() for t in tags if t]
    except json.JSONDecodeError:
        pass
    matches = re.findall(r'"([^"]+)"', text)
    if matches:
        return [m.strip().lower() for m in matches]
    return [t.strip().lower() for t in text.split(',') if t.strip()]


def _extract_json(text: str, expected_keys: set[str] | None = None) -> Optional[dict]:
    text = text.strip()
    if text.startswith('```'):
        text = re.sub(r'^```(?:json)?\s*', '', text)
        text = re.sub(r'\s*```$', '', text)
        text = text.strip()
    result = _try_parse_json(text)
    if result is not None and expected_keys:
        result = _unwrap_nested(result, expected_keys)
    return result


def _try_parse_json(text: str) -> Optional[dict]:
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except (json.JSONDecodeError, ValueError):
        pass
    # Scan for the first balanced JSON object instead of a greedy
    # ``\{.*\}``: the greedy match spans from the first '{' to the LAST '}', so
    # any trailing prose (or a second object) after the JSON made it
    # unparsable and the whole generation was discarded.
    for start in (i for i, ch in enumerate(text) if ch == '{'):
        candidate = _balanced_object(text, start)
        if candidate is None:
            continue
        try:
            obj = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            return obj
    return None


def _balanced_object(text: str, start: int) -> Optional[str]:
    """Return the brace-balanced JSON object starting at *start*, or None.

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
        elif ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


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
