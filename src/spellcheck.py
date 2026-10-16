"""Spell-checking engine used by the UI text widgets.

Word-level checking backed by SymSpell and its bundled English frequency
dictionary. Tokenization masks SillyTavern ``{{macros}}``, HTML-ish tags and
URLs so template and technical text is never flagged. Each user's
added/ignored words persist under ``~/.st-explorer/spellcheck.json``.

Pure logic: no Qt imports so it stays unit-testable and cheap to import.
"""
from __future__ import annotations

import json
import logging
import re
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

from src import vault
from src.app_paths import data_dir

logger = logging.getLogger(__name__)

DICT_FILENAME = 'frequency_dictionary_en_82_765.txt'
MAX_SUGGESTIONS = 5

# Masked spans (replaced with equal-length runs of spaces so every offset in
# the masked text stays valid for the caller): ST macros, HTML-ish tags, URLs.
_MACRO_RE = re.compile(r'\{\{[^{}]*\}\}')
_TAG_RE = re.compile(r'<[^<>]*>')
_URL_RE = re.compile(r'(?:https?://|www\.)\S+')
# Words: letters with internal straight/curly apostrophes (don't, don\u2019t).
_WORD_RE = re.compile(r"[A-Za-z]+(?:['\u2019][A-Za-z]+)*")


def _normalize(word: str) -> str:
    """Canonical key for a word: lowercased with straight apostrophes."""
    return (word or '').strip().lower().replace('\u2019', "'")


def _possessive_stem(word: str) -> str | None:
    """Return the stem of a possessive ``X's`` form (``john's`` -> ``john``)."""
    if len(word) > 3 and word.endswith("'s"):
        return word[:-2]
    return None


def _mask(text: str) -> str:
    """Blank out macro/tag/URL spans, preserving every character offset."""

    def blank(match: re.Match) -> str:
        return ' ' * (match.end() - match.start())

    out = _MACRO_RE.sub(blank, text)
    out = _URL_RE.sub(blank, out)
    return _TAG_RE.sub(blank, out)


def _is_checkable(word: str, masked: str, start: int, end: int) -> bool:
    """Filter tokens that are never worth flagging.

    Skips single letters, all-caps acronyms (``ST``, ``PNG``), and tokens
    glued to digits/underscores/handles (``v2``, ``snake_case``, ``@user``)
    or inside dotted names (``file.txt``, ``init.py``).
    """
    if len(word) < 2 or word.isupper():
        return False
    before = masked[start - 1] if start > 0 else ''
    after = masked[end] if end < len(masked) else ''
    # Note: ``'' in '_@'`` is True in Python — empty neighbours must not match.
    if before and (before.isdigit() or before in ('_', '@')):
        return False
    if after and (after.isdigit() or after in ('_', '@')):
        return False
    if after == '.' and end + 1 < len(masked) and masked[end + 1].isalpha():
        return False
    if before == '.' and start >= 2 and masked[start - 2].isalpha():
        return False
    return True


def iter_words(text: str) -> Iterator[tuple[int, int, str]]:
    """Yield ``(start, end, word)`` for every checkable token in *text*.

    Offsets index into *text* itself (masks are length-preserving), so they
    can be used directly as document cursor positions.
    """
    masked = _mask(text)
    for match in _WORD_RE.finditer(masked):
        word = match.group()
        if _is_checkable(word, masked, match.start(), match.end()):
            yield match.start(), match.end(), word


def _dictionary_path() -> Path | None:
    """Locate the frequency dictionary: bundled copy first, then site-packages."""
    if getattr(sys, 'frozen', False):
        bundled = Path(getattr(sys, '_MEIPASS', '.')) / 'spellcheck_data' / DICT_FILENAME
        if bundled.is_file():
            return bundled
    try:
        import symspellpy
    except ImportError:
        return None
    path = Path(symspellpy.__file__).resolve().parent / DICT_FILENAME
    return path if path.is_file() else None


def _user_lists_path() -> Path:
    return data_dir() / 'spellcheck.json'


# The loaded SymSpell trie is shared by every checker instance: the dictionary
# is immutable and costs ~0.5-1 s to build, while added/ignored word lists are
# per-instance user state.
_sym = None
_sym_loading = False
_sym_ready = False
_sym_lock = threading.Lock()


def _wait_ready() -> None:
    # A load started elsewhere: poll briefly instead of joining a thread
    # handle we may not own.
    for _ in range(200):
        with _sym_lock:
            if _sym_ready:
                return
        time.sleep(0.05)


def _load_dictionary() -> None:
    global _sym, _sym_loading, _sym_ready
    path = _dictionary_path()
    sym = None
    if path is not None:
        from symspellpy import SymSpell

        candidate = SymSpell(max_dictionary_edit_distance=2, prefix_length=7)
        try:
            if candidate.load_dictionary(str(path), term_index=0, count_index=1):
                sym = candidate
        except OSError as exc:
            logger.warning('Failed to load spell dictionary %s: %s', path, exc)
    with _sym_lock:
        _sym = sym
        _sym_ready = True
        _sym_loading = False
    if sym is None:
        logger.warning('Spell checking disabled: no frequency dictionary available')


class SpellChecker:
    """SymSpell-backed checker with per-user added/ignored word lists.

    The dictionary trie is a process-wide resource built once on a background
    thread (~0.5-1 s); ``ensure_loading`` starts that work, ``is_ready``
    reports when checking may begin. A missing dictionary degrades to
    "no word is misspelled" rather than disabling the editors. The
    added/ignored word lists belong to the instance.
    """

    def __init__(self) -> None:
        self._added: set[str] = set()
        self._ignored: set[str] = set()
        self._load_user_lists()

    # -- dictionary lifecycle -------------------------------------------------

    def ensure_loading(self, block: bool = False) -> None:
        """Start the background dictionary load once; optionally wait for it."""
        global _sym_loading
        with _sym_lock:
            if not _sym_loading and not _sym_ready:
                _sym_loading = True
                start = True
            else:
                start = False
        if start:
            thread = threading.Thread(
                target=_load_dictionary, name='spellcheck-load', daemon=True,
            )
            thread.start()
            if block:
                thread.join()
        elif block:
            _wait_ready()

    def is_ready(self) -> bool:
        """True once the dictionary has loaded (or definitively failed to)."""
        with _sym_lock:
            return _sym_ready

    @property
    def available(self) -> bool:
        """True when a dictionary is loaded and checking is meaningful."""
        with _sym_lock:
            return _sym is not None

    # -- queries ---------------------------------------------------------------

    def is_correct(self, word: str) -> bool:
        """True when *word* needs no correction (unknown/unavailable = correct)."""
        key = _normalize(word)
        if not key:
            return True
        if key in self._added or key in self._ignored:
            return True
        with _sym_lock:
            sym = _sym
        if sym is None:
            return True
        if self._exact(sym, key):
            return True
        stem = _possessive_stem(key)
        return stem is not None and self._exact(sym, stem)

    def suggestions(self, word: str, limit: int = MAX_SUGGESTIONS) -> list[str]:
        """Up to *limit* corrections for *word*, original casing transferred."""
        with _sym_lock:
            sym = _sym
        if sym is None or not word or not limit:
            return []
        from symspellpy import Verbosity

        try:
            results = sym.lookup(
                word, Verbosity.CLOSEST,
                max_edit_distance=2, transfer_casing=True, include_unknown=False,
            )
        except ValueError as exc:
            logger.debug('Suggestion lookup failed for %r: %s', word, exc)
            return []
        out: list[str] = []
        seen = {_normalize(word)}
        for item in results:
            key = _normalize(item.term)
            if key in seen:
                continue
            seen.add(key)
            out.append(item.term)
            if len(out) >= limit:
                break
        return out

    def misspellings(self, text: str) -> list[tuple[int, int, str]]:
        """Every misspelled token in *text* as ``(start, end, word)`` spans."""
        return [
            (start, end, word)
            for start, end, word in iter_words(text)
            if not self.is_correct(word)
        ]

    @staticmethod
    def _exact(sym, key: str) -> bool:
        from symspellpy import Verbosity

        return bool(sym.lookup(key, Verbosity.CLOSEST, max_edit_distance=0))

    # -- user word lists ---------------------------------------------------------

    def add_word(self, word: str) -> None:
        """Permit *word* everywhere (and stop ignoring it)."""
        key = _normalize(word)
        if not key:
            return
        self._ignored.discard(key)
        self._added.add(key)
        self._save_user_lists()

    def ignore_word(self, word: str) -> None:
        """Stop flagging *word* without adding it to the dictionary."""
        key = _normalize(word)
        if not key:
            return
        self._added.discard(key)
        self._ignored.add(key)
        self._save_user_lists()

    def added_words(self) -> set[str]:
        return set(self._added)

    def ignored_words(self) -> set[str]:
        return set(self._ignored)

    def _load_user_lists(self) -> None:
        path = _user_lists_path()
        try:
            raw = vault.read_text(path)
        except FileNotFoundError:
            return
        except (OSError, vault.VaultError) as exc:
            logger.warning('Failed to read %s: %s', path, exc)
            return
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.warning('Corrupted spell-check word lists in %s: %s', path, exc)
            return
        if not isinstance(data, dict):
            return
        for name, target in (('added', self._added), ('ignored', self._ignored)):
            words = data.get(name)
            if isinstance(words, list):
                target.update(_normalize(str(w)) for w in words if _normalize(str(w)))

    def _save_user_lists(self) -> None:
        path = _user_lists_path()
        payload = json.dumps(
            {'added': sorted(self._added), 'ignored': sorted(self._ignored)},
            ensure_ascii=False, indent=2,
        )
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            vault.write_bytes(path, payload.encode('utf-8'))
        except (OSError, vault.VaultError) as exc:
            logger.warning('Failed to persist spell-check word lists: %s', exc)


_checker: SpellChecker | None = None
_checker_lock = threading.Lock()


def get_checker() -> SpellChecker:
    """Process-wide checker singleton (created on first use)."""
    global _checker
    with _checker_lock:
        if _checker is None:
            _checker = SpellChecker()
        return _checker


def reset_checker() -> None:
    """Drop the singleton so the next ``get_checker`` reloads user lists.

    Used by tests running under a fresh ``ST_EXPLORER_HOME``.
    """
    global _checker
    with _checker_lock:
        _checker = None
