"""Embedding-backed library search with a keyword fallback.

Semantic search finds cards by meaning rather than by spelling: every card
is embedded once, a query is embedded the same way, and the cards are ranked
by the cosine similarity of the two vectors.  Embeddings come from the
provider the app is already configured for (an OpenAI-compatible
``/embeddings`` endpoint), so no extra account or key is needed.

Not every provider exposes an embedding endpoint, and plenty of setups have
none at all.  The search therefore degrades gracefully: when no embedding is
available the keyword scoring of :func:`src.database.fuzzy_match` is used
instead, so the feature always returns something useful.

The embedder is injectable (``embedder(text) -> list[float] | None``), which
keeps the whole module testable without a network.  Nothing here raises for
an embedding failure - a missing backend returns ``None`` and the caller
falls back.

Pure logic: no Qt, so the module can run on a worker thread.  The provider
settings are read lazily (and only when an embedding is actually requested),
which keeps the Qt-dependent settings backend out of the import path.
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from src.ai_client import redact_secrets
from src.ai_client import is_cleartext_remote
from src.ai_client import _url_host as _url_host

logger = logging.getLogger(__name__)

#: The embedding model requested from the provider.
DEFAULT_EMBEDDING_MODEL = 'text-embedding-3-small'

#: Cards are truncated to this many characters before they are embedded.
#: A full card body can be tens of kilobytes; the first paragraphs carry
#: the identity of the character, the rest is noise for a similarity search.
MAX_EMBED_CHARS = 4000

#: Timeout (seconds) of a single embedding request.
REQUEST_TIMEOUT = 30

#: How many cards are embedded per request.  The embeddings endpoint takes
#: an array, so a whole library is embedded in a handful of requests instead
#: of one per card.
EMBED_BATCH_SIZE = 32


@dataclass
class SemanticIndex:
    """The embeddings of a library snapshot.

    ``card_ids`` lists the indexed cards in insertion order; ``vectors`` maps
    a card id to its embedding vector.
    """

    card_ids: list[int] = field(default_factory=list)
    vectors: dict[int, list[float]] = field(default_factory=dict)


def _is_loopback(base_url: str) -> bool:
    """True when *base_url* points at the local machine.

    Local servers (LM Studio, Ollama) need no API key, so a missing key must
    not disable them.
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
    host = (parsed.hostname or '').lower()
    return host in ('localhost', '127.0.0.1', '::1')


def _extract_embedding(data: Any) -> Optional[list[float]]:
    """Pull the vector out of an embeddings response (None when absent)."""
    if not isinstance(data, dict):
        return None
    items = data.get('data')
    vector = None
    if isinstance(items, list) and items and isinstance(items[0], dict):
        vector = items[0].get('embedding')
    if not isinstance(vector, list):
        vector = data.get('embedding')
    if not isinstance(vector, list) or not vector:
        return None
    try:
        return [float(value) for value in vector]
    except (TypeError, ValueError):
        return None


def _extract_embeddings(data: Any) -> list[Optional[list[float]]]:
    """Pull every vector out of a batched embeddings response.

    The response is ordered like the request, but the ``index`` field is
    used when it is present so a provider that reorders the items still
    lines up.
    """
    if not isinstance(data, dict):
        return []
    items = data.get('data')
    if not isinstance(items, list):
        single = _extract_embedding(data)
        return [single] if single else []
    out: list[Optional[list[float]]] = []
    for position, item in enumerate(items):
        vector = None
        if isinstance(item, dict):
            raw = item.get('embedding')
            if isinstance(raw, list) and raw:
                try:
                    vector = [float(value) for value in raw]
                except (TypeError, ValueError):
                    vector = None
            index = item.get('index')
            if vector is None or not isinstance(index, int) or index != position:
                # Keep the positional order; a mismatching index is rare
                # enough that guessing is better than dropping the batch.
                if vector is not None and isinstance(index, int) and 0 <= index < len(items):
                    continue
        out.append(vector)
    while len(out) < len(items):
        out.append(None)
    return out[:len(items)]


def _embedding_endpoint() -> Optional[tuple[str, dict]]:
    """The configured embedding endpoint and its headers (None when unset)."""
    try:
        from src import settings_manager
    except ImportError:
        logger.debug('Settings backend unavailable; embeddings are disabled.')
        return None
    try:
        saved = settings_manager.load_api_settings()
    except Exception as exc:
        logger.debug('Could not read the provider settings: %s', exc)
        return None
    if not isinstance(saved, dict):
        return None

    base_url = str(saved.get('base_url') or '').strip()
    if not base_url:
        logger.debug('No provider base URL configured; embeddings are disabled.')
        return None
    api_key = str(saved.get('api_key') or '').strip()
    if not api_key and not _is_loopback(base_url):
        logger.debug('No API key configured; embeddings are disabled.')
        return None

    headers = {'Content-Type': 'application/json'}
    if api_key:
        # The same checks the AI client applies: a key with a CR/LF or a
        # non-latin-1 character makes requests raise with the header value
        # in the message, and a key sent over plain http:// to a remote
        # host travels in the clear.
        if '\r' in api_key or '\n' in api_key:
            logger.warning('The API key contains invalid characters; embeddings are disabled.')
            return None
        try:
            api_key.encode('latin-1')
        except UnicodeEncodeError:
            logger.warning('The API key cannot be sent; embeddings are disabled.')
            return None
        if is_cleartext_remote(base_url):
            logger.warning(
                'Sending the API key to %s over unencrypted http:// for embeddings.',
                _url_host(base_url),
            )
        headers['Authorization'] = f'Bearer {api_key}'
    url = f"{base_url.rstrip('/')}/embeddings"
    return url, headers


def embed_texts(
    texts: list[str], model: str = DEFAULT_EMBEDDING_MODEL,
) -> list[Optional[list[float]]]:
    """Embed several texts in one request.

    The OpenAI-compatible ``/embeddings`` endpoint takes an array of inputs,
    so a whole library can be embedded in a handful of requests instead of
    one per card.
    """
    if not texts:
        return []
    endpoint = _embedding_endpoint()
    if endpoint is None:
        return [None] * len(texts)
    url, headers = endpoint
    payload = {'model': model, 'input': list(texts)}
    try:
        import requests
        response = requests.post(
            url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        logger.warning(
            'Embedding request failed: %s', redact_secrets(exc),
        )
        return [None] * len(texts)
    vectors = _extract_embeddings(data)
    if len(vectors) != len(texts):
        vectors = (vectors + [None] * len(texts))[:len(texts)]
    return vectors


def embed_text(text: str, model: str = DEFAULT_EMBEDDING_MODEL) -> Optional[list[float]]:
    """Embed *text* with the app's configured provider.

    Args:
        text: The text to embed.
        model: The embedding model to request.

    Returns:
        The embedding vector, or ``None`` when no provider is configured, the
        provider has no embedding endpoint, or the request failed.  This
        function never raises: a missing embedding backend must not break
        the caller.
    """
    if not (text or '').strip():
        return None
    endpoint = _embedding_endpoint()
    if endpoint is None:
        return None
    url, headers = endpoint
    payload = {'model': model, 'input': text}

    try:
        import requests
        response = requests.post(
            url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        logger.warning(
            'Embedding request failed: %s',
            redact_secrets(exc),
        )
        return None

    vector = _extract_embedding(data)
    if vector is None:
        logger.warning('Embedding response carried no vector; falling back.')
    return vector


def _card_id(card: Any) -> int:
    """The database id of a card (0 when it is unknown)."""
    raw = card.get('id', 0) if isinstance(card, dict) else getattr(card, 'id', 0)
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


def _card_name(card: Any) -> str:
    """The name of a card."""
    if isinstance(card, dict):
        return str(card.get('name', '') or '')
    return str(getattr(card, 'name', '') or '')


def _card_text(card: Any) -> str:
    """The text that represents a card for embedding and keyword matching.

    Accepts both a database row (a dict) and a
    :class:`~src.card_models.CharacterCard`.
    """
    if isinstance(card, dict):
        parts = [
            str(card.get(key, '') or '')
            for key in (
                'name', 'description', 'description_preview', 'personality',
                'scenario', 'creator_notes', 'body_text',
            )
        ]
        tags = card.get('tags')
    else:
        parts = [
            str(getattr(card, key, '') or '')
            for key in (
                'name', 'description', 'personality', 'scenario', 'creator_notes',
            )
        ]
        tags = getattr(card, 'tags', None)
    if isinstance(tags, str):
        try:
            parsed = json.loads(tags)
        except ValueError:
            parsed = None
        if isinstance(parsed, list):
            parts.extend(str(tag) for tag in parsed)
    elif isinstance(tags, (list, tuple)):
        parts.extend(str(tag) for tag in tags)
    return '\n'.join(part for part in parts if part.strip())


def build_index(
    cards: Any,
    *,
    embedder: Optional[Callable[[str], Optional[list[float]]]] = None,
    batch_size: int = EMBED_BATCH_SIZE,
) -> SemanticIndex:
    """Embed every card and return the resulting index.

    Args:
        cards: The cards to index (database rows or
            :class:`~src.card_models.CharacterCard` objects).
        embedder: The embedding callable ``(text) -> list[float] | None``;
            defaults to :func:`embed_text`.  Injecting a fake embedder keeps
            the index buildable without a network.
        batch_size: How many cards are embedded per request when the
            provider is used.  One request per card made a real library
            unusable; the embeddings endpoint takes an array.

    Returns:
        The :class:`SemanticIndex`.  Cards whose embedding failed or is empty
        are skipped, so a partial index is better than an exception.
    """
    index = SemanticIndex()
    if embedder is not None:
        for card in cards or []:
            card_id = _card_id(card)
            text = _card_text(card)
            if not card_id or not text:
                continue
            try:
                vector = embedder(text[:MAX_EMBED_CHARS])
            except Exception as exc:
                logger.warning('Could not embed card %s: %s', card_id, exc)
                continue
            if not vector:
                continue
            try:
                index.vectors[card_id] = [float(value) for value in vector]
            except (TypeError, ValueError):
                logger.warning('Card %s produced an unusable embedding', card_id)
                continue
            index.card_ids.append(card_id)
        return index

    # The provider path: embed in batches.
    pending: list[tuple[int, str]] = []
    for card in cards or []:
        card_id = _card_id(card)
        text = _card_text(card)
        if not card_id or not text:
            continue
        pending.append((card_id, text[:MAX_EMBED_CHARS]))
    size = max(1, int(batch_size))
    for start in range(0, len(pending), size):
        chunk = pending[start:start + size]
        try:
            vectors = embed_texts([text for _card_id, text in chunk])
        except Exception as exc:
            logger.warning('Could not embed %d card(s): %s', len(chunk), exc)
            continue
        for (card_id, _text), vector in zip(chunk, vectors, strict=False):
            if not vector:
                continue
            try:
                index.vectors[card_id] = [float(value) for value in vector]
            except (TypeError, ValueError):
                logger.warning('Card %s produced an unusable embedding', card_id)
                continue
            index.card_ids.append(card_id)
    return index


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """The cosine similarity of two vectors.

    Returns ``0.0`` for empty, mismatched or zero-length vectors instead of
    raising, so a broken embedding cannot break a ranking.
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b, strict=True):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    return dot / math.sqrt(norm_a * norm_b)


def search(
    query: str,
    index: SemanticIndex,
    cards: Any,
    *,
    limit: int = 20,
    embedder: Optional[Callable[[str], Optional[list[float]]]] = None,
) -> list[tuple[int, float]]:
    """Rank the indexed cards against *query* by embedding similarity.

    Args:
        query: The search query.
        index: The index to search (see :func:`build_index`).
        cards: The cards the results are restricted to.  When it is empty the
            whole index is searched.
        limit: The maximum number of results.
        embedder: The embedding callable for the query; defaults to
            :func:`embed_text`.

    Returns:
        ``(card_id, similarity)`` pairs, best match first.  An empty list
        when the query cannot be embedded.
    """
    if not index or not index.vectors:
        return []
    embed = embedder if embedder is not None else embed_text
    try:
        vector = embed(query)
    except Exception as exc:
        logger.warning('Could not embed the query: %s', exc)
        return []
    if not vector:
        return []

    allowed: Optional[set[int]] = None
    card_ids = {_card_id(card) for card in (cards or [])}
    if card_ids:
        allowed = card_ids

    results: list[tuple[int, float]] = []
    for card_id in index.card_ids or list(index.vectors):
        if allowed is not None and card_id not in allowed:
            continue
        other = index.vectors.get(card_id)
        if not other:
            continue
        results.append((card_id, cosine_similarity(vector, other)))
    results.sort(key=lambda pair: pair[1], reverse=True)
    return results[:max(0, int(limit))]


def keyword_search(query: str, cards: Any, *, limit: int = 20) -> list[tuple[int, float]]:
    """Rank *cards* against *query* with the app's keyword matching.

    Uses :func:`src.database.fuzzy_match` (every word of the query must
    appear, in any order) for the name and the card body, and falls back to
    a per-word score so a partial hit still ranks.
    """
    from src.database import fuzzy_match
    words = [word for word in (query or '').split() if word]
    results: list[tuple[int, float]] = []
    for card in cards or []:
        name = _card_name(card)
        text = _card_text(card)
        score = 0.0
        if fuzzy_match(query, name):
            score = 1.0
        elif fuzzy_match(query, text):
            score = 0.6
        elif words and any(word.casefold() in name.casefold() for word in words):
            score = 0.3
        elif words and any(word.casefold() in text.casefold() for word in words):
            score = 0.1
        if score > 0.0:
            results.append((_card_id(card), score))
    results.sort(key=lambda pair: (-pair[1], pair[0]))
    return results[:max(0, int(limit))]


def semantic_or_keyword_search(
    query: str,
    cards: Any,
    index: Optional[SemanticIndex] = None,
    *,
    limit: int = 20,
    embedder: Optional[Callable[[str], Optional[list[float]]]] = None,
) -> list[tuple[int, float]]:
    """Search the library semantically, falling back to keyword matching.

    Args:
        query: The search query.
        cards: The cards to search.
        index: The embedding index; when it is missing or empty the keyword
            search is used.
        limit: The maximum number of results.
        embedder: The embedding callable; defaults to :func:`embed_text`.

    Returns:
        ``(card_id, score)`` pairs, best match first.  The score is a
        similarity for the semantic path and a match weight for the keyword
        path.
    """
    if index is not None and index.vectors:
        results = search(query, index, cards, limit=limit, embedder=embedder)
        if results:
            return results
        logger.debug('No semantic result; falling back to keyword matching.')
    return keyword_search(query, cards, limit=limit)
