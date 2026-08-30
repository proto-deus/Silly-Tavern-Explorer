from __future__ import annotations

from typing import Iterable


def normalize_tag(tag: str) -> str:
    """Normalize a tag for storage/comparison: stripped and lower-cased.

    Pure function so tag-list manipulation can be unit-tested without a DB
    or Qt event loop.
    """
    return (tag or '').strip().lower()


def _normalize_all(tags: Iterable[str]) -> list[str]:
    return [normalize_tag(t) for t in tags if normalize_tag(t)]


def rename_tag(tags: list[str], old: str, new: str) -> list[str]:
    """Return a new tag list with *old* replaced by *new*.

    Case-insensitive match on *old*.  The new tag is normalized.  Duplicates
    introduced by the rename are collapsed (the renamed tag keeps its original
    position; any later duplicate is dropped).  Order is otherwise preserved.
    """
    old_n = normalize_tag(old)
    new_n = normalize_tag(new)
    if not new_n:
        # Renaming to an empty tag is equivalent to removing it.
        return remove_tag(tags, old_n)

    result: list[str] = []
    seen: set[str] = set()
    replaced = False
    for tag in tags:
        n = normalize_tag(tag)
        if not n:
            continue
        if n == old_n:
            if not replaced:
                if new_n not in seen:
                    result.append(new_n)
                    seen.add(new_n)
                replaced = True
            # drop additional occurrences of old (dedupe)
            continue
        if n not in seen:
            result.append(n)
            seen.add(n)
    return result


def merge_tag(tags: list[str], source: str, target: str) -> list[str]:
    """Merge *source* into *target*: every occurrence of *source* becomes *target*.

    If *target* already exists in the list, *source* is simply removed.
    Otherwise *source* is replaced in-place by *target* (preserving position).
    Case-insensitive on both.  Duplicates are collapsed.
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
    for tag in tags:
        n = normalize_tag(tag)
        if not n:
            continue
        if n == source_n:
            if not source_replaced and target_n not in seen:
                result.append(target_n)
                seen.add(target_n)
            source_replaced = True
            continue
        if n not in seen:
            result.append(n)
            seen.add(n)
    # If source was never present but target isn't either, don't add target
    # (merge only affects cards that had the source tag).
    return result


def remove_tag(tags: list[str], tag: str) -> list[str]:
    """Return a new tag list with *tag* removed (case-insensitive)."""
    tag_n = normalize_tag(tag)
    result: list[str] = []
    seen: set[str] = set()
    for t in tags:
        n = normalize_tag(t)
        if not n or n == tag_n:
            continue
        if n not in seen:
            result.append(n)
            seen.add(n)
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
