from __future__ import annotations

import unicodedata
from typing import Iterable


def normalize_tag(tag: object) -> str:
    """Normalize a tag for storage/comparison: stripped and lower-cased.

    Pure function so tag-list manipulation can be unit-tested without a DB
    or Qt event loop.

    Card data is third-party input, so a tag may be a number, ``None`` or a
    nested list (``"tags": [1, "ok", ["x"]]``). Coerce rather than crash: the
    library-level tag listing runs over *every* card, so a single malformed
    card would otherwise take down the tag sidebar and tag manager.

    The text is normalised to NFC: on a normalising file system two
    differently-normalised spellings of the same tag are the same file, and
    they must not end up as two separate tags.
    """
    if tag is None:
        return ''
    if not isinstance(tag, str):
        try:
            tag = str(tag)
        except Exception:
            return ''
    return unicodedata.normalize('NFC', tag).strip().lower()


def _normalize_all(tags: Iterable[str]) -> list[str]:
    normalized = (normalize_tag(t) for t in tags)
    return [n for n in normalized if n]


def _keep_original(tag: object) -> str:
    """The tag's original spelling (as a string)."""
    return tag if isinstance(tag, str) else str(tag)


def rename_tag(tags: list[str], old: str, new: str) -> list[str]:
    """Return a new tag list with *old* replaced by *new*.

    Case-insensitive match on *old*.  The new tag is normalized.  Duplicates
    introduced by the rename are collapsed (the renamed tag keeps its original
    position; any later duplicate is dropped).  Order is otherwise preserved.

    The tags that are *not* renamed keep their original spelling: a bulk
    rename is applied to every card in the library, and lower-casing every
    other tag would rewrite cards the operation does not concern.
    """
    old_n = normalize_tag(old)
    new_n = normalize_tag(new)
    if not new_n:
        # Renaming to an empty tag is equivalent to removing it.
        return remove_tag(tags, old_n)

    result: list[str] = []
    seen: set[str] = set()
    replaced = False
    found = False
    for tag in tags:
        n = normalize_tag(tag)
        if not n:
            continue
        if n == old_n:
            found = True
            if not replaced:
                if new_n not in seen:
                    result.append(new_n)
                    seen.add(new_n)
                replaced = True
            # drop additional occurrences of old (dedupe)
            continue
        if n not in seen:
            result.append(_keep_original(tag))
            seen.add(n)
    if not found:
        # The tag is not on this list: nothing to do, and nothing to change.
        return list(tags)
    return result


def merge_tag(tags: list[str], source: str, target: str) -> list[str]:
    """Merge *source* into *target*: every occurrence of *source* becomes *target*.

    If *target* already exists in the list, *source* is simply removed.
    Otherwise *source* is replaced in-place by *target* (preserving position).
    Case-insensitive on both.  Duplicates are collapsed.

    The tags that are not merged keep their original spelling (see
    :func:`rename_tag`).
    """
    source_n = normalize_tag(source)
    target_n = normalize_tag(target)
    if not target_n:
        return remove_tag(tags, source_n)
    if source_n == target_n:
        return _dedupe(tags)

    result: list[str] = []
    seen: set[str] = set()
    source_replaced = False
    found = False
    for tag in tags:
        n = normalize_tag(tag)
        if not n:
            continue
        if n == source_n:
            found = True
            if not source_replaced and target_n not in seen:
                result.append(target_n)
                seen.add(target_n)
            source_replaced = True
            continue
        if n not in seen:
            result.append(_keep_original(tag))
            seen.add(n)
    if not found:
        # The source tag is not on this list: the merge does not apply, and
        # the list is left exactly as it was.
        return list(tags)
    return result


def remove_tag(tags: list[str], tag: str) -> list[str]:
    """Return a new tag list with *tag* removed (case-insensitive).

    The remaining tags keep their original spelling; a list that does not
    contain *tag* is returned unchanged.
    """
    tag_n = normalize_tag(tag)
    result: list[str] = []
    seen: set[str] = set()
    found = False
    for t in tags:
        n = normalize_tag(t)
        if not n:
            continue
        if n == tag_n:
            found = True
            continue
        if n not in seen:
            result.append(_keep_original(t))
            seen.add(n)
    if not found:
        return list(tags)
    return result


def _dedupe(tags: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for t in tags:
        n = normalize_tag(t)
        if not n or n in seen:
            continue
        result.append(n)
        seen.add(n)
    return result


def merge_tags(existing: Iterable[str], new: Iterable[str]) -> list[str]:
    """Merge *new* tags into *existing*, deduping case-insensitively.

    Existing order is preserved; new tags are appended in their original order,
    skipping any that already exist.  Pure function.
    """
    return _dedupe([*existing, *new])


def count_tags(tag_lists: Iterable[Iterable[str]]) -> dict[str, int]:
    """Aggregate tag usage counts across many cards' tag lists.

    Pure function: takes any iterable of tag iterables and returns a mapping
    of normalized tag -> number of cards using it.
    """
    counts: dict[str, int] = {}
    for tags in tag_lists:
        # Dedupe within a single card so a tag is only counted once per card.
        unique = set(_normalize_all(tags))
        for tag in unique:
            counts[tag] = counts.get(tag, 0) + 1
    return counts
