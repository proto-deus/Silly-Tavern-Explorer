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


class _RetryableError(Exception):
    """Internal marker: a transient transport/HTTP failure worth retrying."""


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
) -> APIPreset:
    """Return *preset* with per-chat sampling overrides applied.

    ``None`` values keep the preset's setting.  Pure function.
    """
    changes: dict[str, float] = {}
    if temperature is not None and temperature != preset.temperature:
        changes['temperature'] = max(0.0, float(temperature))
    if min_p is not None and min_p != preset.min_p:
        changes['min_p'] = min(1.0, max(0.0, float(min_p)))
    if not changes:
        return preset
    return replace(preset, **changes)


class AIClient:
    def __init__(self, preset: APIPreset):
        self.preset = preset
        self._session: Optional[requests.Session] = None
        self._cancel_requested = False

    def _headers(self) -> dict[str, str]:
        h = {'Content-Type': 'application/json'}
        if self.preset.api_key:
            h['Authorization'] = f'Bearer {self.preset.api_key}'
        return h

    def _url(self) -> str:
        base = self.preset.base_url.rstrip('/')
        return f'{base}/chat/completions'

    def cancel(self) -> None:
        """Request cancellation of any in-progress generation.

        Closes the underlying HTTP session so an active request is interrupted
        rather than waiting for the full timeout to elapse.  The flag is never
        cleared by starting a request: ``cancel()`` may arrive from the UI
        thread before the worker thread has started iterating, and wiping it
        would silently lose the cancellation.  Construct a fresh
        :class:`AIClient` per generation instead of reusing a cancelled one.
        """
        self._cancel_requested = True
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
            return max(0, int(getattr(self.preset, 'retry_attempts', 2)))
        except (TypeError, ValueError):
            return 2

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
        payload = self._build_payload(
            [
                {'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': user_prompt},
            ],
            stream,
        )

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
        payload: dict[str, Any] = {
            'model': p.model,
            'messages': messages,
            'temperature': p.temperature,
            'max_tokens': p.max_tokens,
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
        produced = False
        try:
            try:
                resp = self._session.post(
                    self._url(),
                    headers=self._headers(),
                    json=payload,
                    stream=True,
                    timeout=120,
                )
                resp.raise_for_status()
            except requests.HTTPError as exc:
                if self._http_error_is_retryable(exc):
                    raise _RetryableError(f"API error: {exc}") from exc
                raise
            for line in resp.iter_lines():
                if self._cancel_requested:
                    raise RuntimeError('Generation cancelled')
                if not line:
                    continue
                line_str = line.decode('utf-8', errors='replace')
                # SSE allows an optional single space after "data:".
                if not line_str.startswith('data:'):
                    continue
                data_str = line_str[5:].lstrip(' ')
                if data_str.strip() == '[DONE]':
                    break
                try:
                    chunk = json.loads(data_str)
                    if isinstance(chunk, dict) and chunk.get('error'):
                        err = chunk['error']
                        msg = err.get('message', str(err)) if isinstance(err, dict) else str(err)
                        raise RuntimeError(f"API stream error: {msg}")
                    choices = chunk.get('choices', [])
                    if choices:
                        delta = choices[0].get('delta', {})
                        content = delta.get('content', '')
                        if content:
                            produced = True
                            yield content
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue
        except (requests.ConnectionError, requests.Timeout) as exc:
            if produced:
                raise
            raise _RetryableError(f"Connection failed: {exc}") from exc
        finally:
            session = self._session
            self._session = None
            if session is not None:
                try:
                    session.close()
                except Exception:
                    pass

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
                delay = _backoff_delay(attempt)
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
        full_messages = trim_messages(full_messages, self.preset.context_size, self.preset.max_tokens)
        if post_history and post_history.strip():
            full_messages.append({'role': 'system', 'content': post_history})
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
    budget = context_size - max(0, max_tokens)
    if budget <= 1:
        budget = max(1, context_size // 2)

    system = messages[0] if messages and messages[0].get('role') == 'system' else None
    rest = list(messages[1:]) if system is not None else list(messages)
    if not rest:
        return messages, []
    if system is not None:
        budget -= _message_token_cost(system, token_fn)
        if budget <= 0:
            budget = 1  # always allow at least the most recent message

    kept: list[dict[str, Any]] = [rest[-1]]
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
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if match:
        try:
            obj = json.loads(match.group())
            if isinstance(obj, dict):
                return obj
        except (json.JSONDecodeError, ValueError):
            pass
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
