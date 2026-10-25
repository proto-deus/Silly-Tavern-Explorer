"""Library integrity checks ("Doctor").

Every class of silent corruption this module detects was, until now, invisible:
a card row pointing at a file that no longer exists, a chat session belonging to
a card that was deleted years ago, a tag list that crashes the tag sidebar, an
API key that can no longer be decrypted, a SillyTavern link whose file has
vanished. Each is reported here with a suggested repair so the user can act
without understanding the storage layout.

Pure logic (no Qt) so it is unit-testable, and safe to run on a worker thread.
Only *read-only* inspection happens here; fixes are applied by the caller.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from src import safe_json

logger = logging.getLogger(__name__)

# Card ids whose gallery images and chat sessions must never be auto-deleted.
# A repair that removes a card row because its PNG vanished keeps the user's
# pictures and conversation history; the registry is what stops the *next*
# run of the orphan checks from deleting them.
_PRESERVED_FILE = 'preserved-orphans.json'


def preserved_registry_path() -> Path:
    """Path of the preserved-orphan registry."""
    from src.app_paths import data_dir
    return data_dir() / _PRESERVED_FILE


def load_preserved_ids() -> set[str]:
    """Card ids whose data the doctor must not delete."""
    path = preserved_registry_path()
    try:
        raw = safe_json.loads_safe(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return set()
    if not isinstance(raw, list):
        return set()
    return {str(item) for item in raw}


def preserve_orphan_ids(ids) -> None:
    """Record *ids* as "keep the data" and persist the registry."""
    current = load_preserved_ids()
    current |= {str(i) for i in ids}
    path = preserved_registry_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(sorted(current), indent=2), encoding='utf-8')
    except OSError:
        logger.exception("Could not write the preserved-orphan registry")


def forget_preserved_ids(ids) -> None:
    """Remove *ids* from the preserved registry (the user asked to delete)."""
    current = load_preserved_ids()
    current -= {str(i) for i in ids}
    # The file is rewritten even when the registry becomes empty: an empty
    # list is a valid registry, and skipping the write left the last
    # forgotten id behind - which kept the gallery folder of a card the user
    # explicitly deleted protected from the orphan sweep forever.
    path = preserved_registry_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(sorted(current), indent=2), encoding='utf-8')
    except OSError:
        logger.exception("Could not write the preserved-orphan registry")


def _file_status(path: str | Path) -> str:
    """Classify a path as ``'ok'``, ``'missing'`` or ``'unreachable'``.

    ``Path.exists()`` collapses "the file is gone" and "I am not allowed to
    look" into the same answer, which made the doctor treat a permission
    error or an unplugged drive as a deleted card.
    """
    try:
        os.stat(str(path))
    except FileNotFoundError:
        return 'missing'
    except NotADirectoryError:
        return 'missing'
    except OSError:
        return 'unreachable'
    return 'ok'


@dataclass
class Finding:
    """One detected problem."""

    category: str          # short machine-ish label, e.g. 'missing-file'
    severity: str          # 'error' | 'warning' | 'info'
    message: str           # human-readable description
    detail: str = ''       # extra context (paths, counts)
    fixable: bool = False  # whether :func:`repair` can resolve it
    card_id: int | None = None
    # The full list of affected paths/names behind ``detail`` (which is only a
    # truncated display string).  Repairs act on this field: re-parsing the
    # display string silently fixed at most 10 items per run and broke on
    # names containing ', '.
    items: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        """Stable identity, used to de-duplicate and to match repairs."""
        return f"{self.category}:{self.card_id}:{self.detail}"


@dataclass
class RepairPlan:
    """One planned repair - what :func:`repair` *would* do.

    Used by the dry-run preview so the user can see every deletion before
    anything is touched.
    """

    finding: Finding
    action: str          # e.g. 'delete-session-folders', 'remove-card-row'
    targets: list[str] = field(default_factory=list)
    description: str = ''

    @property
    def destructive(self) -> bool:
        """True when applying this plan deletes user data."""
        return self.action.startswith('delete')


@dataclass
class DoctorReport:
    findings: list[Finding] = field(default_factory=list)
    # False when the character list could not be read.  Repairs that delete
    # data are skipped in that case: a busy or corrupt database must never be
    # mistaken for an empty library.
    characters_listed: bool = True

    def add(self, finding: Finding) -> None:
        if not any(f.key == finding.key for f in self.findings):
            self.findings.append(finding)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == 'error']

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == 'warning']

    @property
    def ok(self) -> bool:
        return not self.errors


def _iter_characters(db) -> Optional[list[dict]]:
    """All character rows, or ``None`` when the listing could not be read.

    ``None`` must never be confused with an empty list.  An empty library is a
    legitimate state; a *failed* listing (database locked, corrupt, swapped
    mid-scan) is not.  Treating the failure as "the library is empty" made the
    orphan checks report every chat session and gallery folder as belonging to
    a deleted card, and "Apply Automatic Fixes" then deleted all of them.
    """
    try:
        return db.get_all(light=True)
    except Exception:
        logger.exception("Doctor could not list characters")
        return None


def _listing_failed() -> list[Finding]:
    """The finding reported when the character listing is unavailable."""
    return [Finding(
        'check-failed', 'warning',
        'The character list could not be read, so related checks were skipped',
        detail=('The database was unreadable or busy. No changes were made. '
                'Re-run the check once the library is idle.'),
    )]


def check_missing_card_files(db) -> list[Finding]:
    """Cards whose backing PNG/JSON no longer exists on disk."""
    out: list[Finding] = []
    rows = _iter_characters(db)
    if rows is None:
        return _listing_failed()
    for row in rows:
        source = row.get('source_path') or ''
        if not source:
            out.append(Finding(
                'no-source-path', 'error',
                f"Card '{row.get('name', '?')}' has no file path",
                detail=f"id={row.get('id')}", card_id=row.get('id'),
            ))
            continue
        status = _file_status(source)
        if status == 'missing':
            out.append(Finding(
                'missing-file', 'error',
                f"File missing for '{row.get('name', '?')}'",
                detail=source, fixable=True, card_id=row.get('id'),
            ))
        elif status == 'unreachable':
            # A permission error or an unplugged drive looks exactly like a
            # deleted file to ``Path.exists()``; repairing that would drop a
            # perfectly good card (and its gallery) from the library.
            out.append(Finding(
                'unreachable-file', 'warning',
                f"File for '{row.get('name', '?')}' could not be read",
                detail=source, card_id=row.get('id'),
            ))
    return out


def check_unreadable_cards(db) -> list[Finding]:
    """Cards whose file exists but holds no parsable character data.

    The worst kind of silent corruption: the card looks fine in the grid but
    its content is gone (a truncated write, a non-card PNG).
    """
    from src.card_parser import read_card_data

    out: list[Finding] = []
    rows = _iter_characters(db)
    if rows is None:
        return _listing_failed()
    for row in rows:
        source = row.get('source_path') or ''
        if not source or not Path(source).exists():
            continue    # already reported as a missing file
        try:
            if read_card_data(source) is None:
                out.append(Finding(
                    'unreadable-card', 'error',
                    f"No character data in '{row.get('name', '?')}'",
                    detail=source, card_id=row.get('id'),
                ))
        except Exception as exc:
            out.append(Finding(
                'unreadable-card', 'error',
                f"Could not read '{row.get('name', '?')}': {exc}",
                detail=source, card_id=row.get('id'),
            ))
    return out


def check_malformed_tags(db) -> list[Finding]:
    """Cards whose stored tag blob is not a list of strings.

    These used to raise AttributeError inside the tag sidebar and tag manager,
    taking both features down for the whole library.
    """
    from src.database import _decode_tag_blob

    out: list[Finding] = []
    for card_id, blob in _iter_tag_blobs(db):
        tags = _decode_tag_blob(blob)
        bad = [t for t in tags if not isinstance(t, str)]
        if bad:
            out.append(Finding(
                'malformed-tags', 'warning',
                f'{len(bad)} non-text tag value(s) found',
                detail=', '.join(repr(t)[:24] for t in bad[:3]), fixable=True,
                card_id=card_id,
            ))
    return out


def _iter_tag_blobs(db):
    try:
        with db._conn() as conn:
            for row in conn.execute('SELECT id, tags FROM characters').fetchall():
                yield row[0], row[1]
    except Exception:
        logger.exception("Doctor could not read tag blobs")


def check_orphan_files(db, library_dir: str | Path | None = None) -> list[Finding]:
    """Card files in the library folder that no database row refers to."""
    if library_dir is None:
        from src.database import _get_library_dir
        library_dir = _get_library_dir()
    lib = Path(library_dir)
    if not lib.is_dir():
        return []
    rows = _iter_characters(db)
    if rows is None:
        # Without a trustworthy character list every file would look orphaned.
        return _listing_failed()
    known = {str(Path(r['source_path'])) for r in rows
             if r.get('source_path')}
    # Resolve the known paths once: ``Path.resolve()`` hits the filesystem, and
    # doing it per file made this check O(n^2) in syscalls.
    known_resolved: set[str] = set()
    for p in known:
        try:
            known_resolved.add(str(Path(p).resolve()))
        except OSError:
            known_resolved.add(str(Path(p)))
    orphans: list[str] = []
    for path in sorted(lib.iterdir()):
        if not path.is_file() or path.suffix.lower() not in ('.png', '.json'):
            continue
        # Skip our own atomic-write staging files.
        if path.name.startswith('.ste-') or path.name.endswith('.tmp'):
            continue
        try:
            resolved = str(path.resolve())
        except OSError:
            continue
        if resolved not in known_resolved:
            orphans.append(path.name)
    if not orphans:
        return []
    return [Finding(
        'orphan-file', 'warning',
        f'{len(orphans)} file(s) in the library folder are not in the library',
        detail=', '.join(orphans[:10]), fixable=True,
        items=orphans,
    )]


def check_orphan_sessions(db, sessions_dir: str | Path | None = None) -> list[Finding]:
    """Chat-session folders for cards that no longer exist.

    Deleting a card never removed its sessions, so these accumulated forever,
    stayed invisible in the UI, and were still archived into every backup.
    """
    if sessions_dir is None:
        from src.app_paths import data_dir
        sessions_dir = data_dir() / 'sessions'
    root = Path(sessions_dir)
    if not root.is_dir():
        return []
    rows = _iter_characters(db)
    if rows is None:
        # A failed listing means "no card ids" - which would turn every
        # session folder into an orphan and delete the user's chat history.
        return _listing_failed()
    live = {str(r.get('id')) for r in rows}
    preserved = load_preserved_ids()
    orphans = []
    for child in sorted(root.iterdir()):
        if child.is_dir() and child.name.isdigit() and child.name not in live:
            if child.name in preserved:
                continue
            orphans.append(child.name)
    if not orphans:
        return []
    return [Finding(
        'orphan-sessions', 'warning',
        f'{len(orphans)} chat-session folder(s) belong to deleted cards',
        detail=', '.join(orphans[:10]), fixable=True,
        items=orphans,
    )]


def check_gallery_images(db, gallery_dir: str | Path | None = None) -> list[Finding]:
    """Gallery metadata and files that have drifted apart.

    Three failure modes, each with a different safe repair:

    * gallery rows whose file is missing - drop the row (the file is gone);
    * files no row references - report only, never delete user images;
    * ``gallery/<id>/`` folders of deleted cards - remove the folder, like
      the orphaned chat sessions.
    """
    if gallery_dir is None:
        from src.app_paths import data_dir
        gallery_dir = data_dir() / 'gallery'
    root = Path(gallery_dir)
    rows = _iter_characters(db)
    if rows is None:
        # See check_orphan_sessions: a failed listing must not turn every
        # gallery folder into an orphan.
        return _listing_failed()
    live = {str(r.get('id')) for r in rows}
    preserved = load_preserved_ids()
    out: list[Finding] = []

    known_files: dict[str, set[str]] = {}
    try:
        rows = db.list_all_gallery_images()
    except Exception:
        logger.exception("Doctor could not list gallery rows")
        rows = []
    for row in rows:
        char_id = str(row.get('character_id'))
        file_name = str(row.get('file_name') or '')
        known_files.setdefault(char_id, set()).add(file_name)
        path = root / char_id / file_name
        if file_name and not path.is_file():
            out.append(Finding(
                'gallery-missing-file', 'warning',
                f"Gallery image '{file_name}' is missing from disk",
                detail=f'gallery/{char_id}/{file_name}', fixable=True,
                card_id=row.get('character_id'),
                items=[str(row.get('id'))],
            ))

    if not root.is_dir():
        return out

    for child in sorted(root.iterdir()):
        if not child.is_dir() or not child.name.isdigit():
            continue
        if child.name in preserved:
            # Deliberately kept by a previous repair: the card's file is gone
            # but its gallery images are still the user's data.
            continue
        if child.name not in live:
            out.append(Finding(
                'orphan-gallery', 'warning',
                f'Gallery folder of deleted card {child.name}',
                detail=f'gallery/{child.name}', fixable=True,
                items=[child.name],
            ))
            continue
        expected = known_files.get(child.name, set())
        strays = [
            p.name for p in sorted(child.iterdir())
            if p.is_file() and p.name not in expected
            and not p.name.startswith('.ste-') and not p.name.endswith('.tmp')
        ]
        if strays:
            out.append(Finding(
                'gallery-orphan-file', 'info',
                f'{len(strays)} file(s) in the gallery folder are not in the library',
                detail=', '.join(strays[:10]), fixable=True,
                card_id=int(child.name),
                items=strays,
            ))
    return out


def check_backdrop_images(db, backdrops_dir: str | Path | None = None) -> list[Finding]:
    """Global VN backdrop metadata and files that have drifted apart.

    Same two failure modes as the per-character gallery: rows whose file is
    gone (drop the row) and files no row references (report only - never
    delete user images).
    """
    if backdrops_dir is None:
        from src.gallery_store import get_backdrops_dir
        root = get_backdrops_dir()
    else:
        root = Path(backdrops_dir)
    try:
        rows = db.list_backdrop_images()
    except Exception:
        logger.exception("Doctor could not list backdrop rows")
        return []
    out: list[Finding] = []
    known: set[str] = set()
    for row in rows:
        file_name = str(row.get('file_name') or '')
        known.add(file_name)
        if file_name and not (root / file_name).is_file():
            out.append(Finding(
                'backdrop-missing-file', 'warning',
                f"Backdrop image '{file_name}' is missing from disk",
                detail=f'backdrops/{file_name}', fixable=True,
                items=[str(row.get('id'))],
            ))
    if not root.is_dir():
        return out
    strays = [
        p.name for p in sorted(root.iterdir())
        if p.is_file() and p.name not in known
        and not p.name.startswith('.ste-') and not p.name.endswith('.tmp')
    ]
    if strays:
        out.append(Finding(
            'backdrop-orphan-file', 'info',
            f'{len(strays)} file(s) in the backdrops folder are not in the library',
            detail=', '.join(strays[:10]), fixable=True,
            items=strays,
        ))
    return out


def check_stale_links(db, characters_dir: str | Path | None = None) -> list[Finding]:
    """Cards linked to a SillyTavern file that is no longer there."""
    if characters_dir is None:
        from src.settings_manager import load_st_characters_path
        characters_dir = load_st_characters_path()
    if not characters_dir:
        return []
    cdir = Path(characters_dir)
    if not cdir.is_dir():
        return []
    present = {p.name.casefold() for p in cdir.iterdir() if p.is_file()}
    out: list[Finding] = []
    rows = _iter_characters(db)
    if rows is None:
        return _listing_failed()
    for row in rows:
        url = row.get('st_avatar_url')
        if url and str(url).casefold() not in present:
            out.append(Finding(
                'stale-st-link', 'warning',
                f"SillyTavern file missing for '{row.get('name', '?')}'",
                detail=str(url), card_id=row.get('id'),
            ))
    return out


def check_stale_tombstones(db) -> list[Finding]:
    """Deletion tombstones whose SillyTavern file is already gone."""
    try:
        tombstones = db.get_deleted_st_cards()
    except Exception:
        return []
    from src.settings_manager import load_st_characters_path
    path = load_st_characters_path()
    if not path:
        return []
    cdir = Path(path)
    if not cdir.is_dir():
        return []
    present = {p.name.casefold() for p in cdir.iterdir() if p.is_file()}
    stale = [t for t in tombstones
             if str(t.get('st_avatar_url', '')).casefold() not in present]
    if not stale:
        return []
    return [Finding(
        'stale-tombstone', 'info',
        f'{len(stale)} deletion record(s) whose SillyTavern file is already gone',
        detail=', '.join(str(t.get('st_avatar_url')) for t in stale[:10]),
        fixable=True,
        items=[str(t.get('st_avatar_url')) for t in stale],
    )]


def check_undecryptable_keys() -> list[Finding]:
    """Provider API keys that are stored but can no longer be decrypted."""
    try:
        from src.settings_manager import undecryptable_api_key_providers
        providers = undecryptable_api_key_providers()
    except Exception:
        return []
    if not providers:
        return []
    # Report every affected provider (not just the first) and show the bare
    # provider name rather than the internal storage key.
    names = [p.rsplit('/', 1)[-1] for p in providers]
    return [Finding(
        'undecryptable-api-key', 'error',
        f"API key(s) for {', '.join(names)} could not be decrypted",
        detail=('The stored key(s) have been left untouched. Open Settings and '
                're-enter them to replace them.'),
        items=names,
    )]


def check_cleartext_api_url() -> list[Finding]:
    """Provider base URLs that would send an API key over cleartext http."""
    try:
        from src.ai_client import PRESETS, is_cleartext_remote
        from src.settings_manager import load_provider_settings
    except Exception:
        return []
    out: list[Finding] = []
    for name in list(PRESETS.keys()):
        try:
            cfg = load_provider_settings(name)
        except Exception:
            logger.exception("Could not read provider settings for %s", name)
            continue
        if not cfg.get('api_key'):
            continue
        if is_cleartext_remote(cfg.get('base_url') or ''):
            # Only the host is shown: a base URL may carry the API key as a
            # query parameter, and the report can be pasted into a public
            # issue tracker.
            from src.ai_client import _url_host
            host = _url_host(cfg.get('base_url') or '') or '(unknown host)'
            out.append(Finding(
                'cleartext-api-url', 'warning',
                f"Provider '{name}' sends its API key over unencrypted http://",
                detail=(f"{host} - prefer https:// for non-local servers."),
                items=[name],
            ))
    return out


def check_database(db_path: str | Path) -> list[Finding]:
    """Run SQLite's own integrity check (read-only; never creates a DB)."""
    out: list[Finding] = []
    if not Path(db_path).exists():
        # sqlite3.connect would silently CREATE an empty database here,
        # violating this module's read-only contract.
        return [Finding('database-unreadable', 'error',
                        f'Database file not found: {db_path}')]
    try:
        conn = sqlite3.connect(str(db_path), timeout=5.0)
        try:
            row = conn.execute('PRAGMA integrity_check').fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return [Finding('database-unreadable', 'error',
                        f'Could not open the database: {exc}')]
    result = (row[0] if row else 'unknown')
    if result != 'ok':
        out.append(Finding(
            'database-corrupt', 'error',
            'The library database failed its integrity check',
            detail=str(result)[:400],
        ))
    return out


def run_all(db, db_path: str | Path | None = None,
            library_dir: str | Path | None = None,
            sessions_dir: str | Path | None = None,
            gallery_dir: str | Path | None = None,
            st_characters_dir: str | Path | None = None) -> DoctorReport:
    """Run every check and return a combined report."""
    report = DoctorReport()
    # Probe the character list once up front.  Every orphan check depends on
    # it, and a failed listing must disable the data-destroying repairs.
    report.characters_listed = _iter_characters(db) is not None
    # (name, callable) pairs: a bare lambda reports as '<lambda>' and, since
    # Finding.key includes detail, every failed check deduped into ONE
    # useless 'check-failed' finding.
    checks: list[tuple[str, callable]] = [
        ('missing-card-files', lambda: check_missing_card_files(db)),
        ('unreadable-cards', lambda: check_unreadable_cards(db)),
        ('malformed-tags', lambda: check_malformed_tags(db)),
        ('orphan-files', lambda: check_orphan_files(db, library_dir)),
        ('orphan-sessions', lambda: check_orphan_sessions(db, sessions_dir)),
        ('gallery-images', lambda: check_gallery_images(db, gallery_dir)),
        ('backdrop-images', lambda: check_backdrop_images(db)),
        ('stale-links', lambda: check_stale_links(db, st_characters_dir)),
        ('stale-tombstones', lambda: check_stale_tombstones(db)),
        ('undecryptable-keys', check_undecryptable_keys),
        ('cleartext-api-url', check_cleartext_api_url),
    ]
    if db_path is None:
        db_path = getattr(db, 'db_path', None)
    if db_path:
        checks.insert(0, ('database-integrity', lambda: check_database(db_path)))

    for name, check in checks:
        try:
            for finding in check():
                report.add(finding)
        except Exception:
            # A broken check must not abort the whole report.
            logger.exception("Doctor check failed: %s", name)
            report.add(Finding(
                'check-failed', 'warning',
                f"The '{name}' integrity check could not complete",
                detail=name,
            ))
    return report


def plan_repairs(report: DoctorReport) -> list[RepairPlan]:
    """Describe what :func:`repair` *would* do, without doing anything.

    This is the dry-run: the user can see every file that would be deleted
    and every row that would be dropped before anything happens.  Repairs
    that are skipped (because the character list could not be read) are
    reported as such, so the preview matches what the repair really does.
    """
    plans: list[RepairPlan] = []
    for finding in report.findings:
        if not finding.fixable:
            continue
        if finding.category == 'missing-file' and finding.card_id is not None:
            plans.append(RepairPlan(
                finding,
                action='remove-card-row',
                targets=[finding.detail],
                description=(
                    f'Remove the library entry for {finding.detail} (the chat '
                    f'history and gallery images are kept)'
                ),
            ))
        elif finding.category == 'orphan-sessions':
            if not report.characters_listed:
                plans.append(RepairPlan(
                    finding, action='skip', targets=[],
                    description=(
                        'Chat sessions are left alone: the character list could '
                        'not be read.'
                    ),
                ))
                continue
            plans.append(RepairPlan(
                finding, action='delete-session-folders', targets=list(finding.items),
                description=(
                    f'Delete {len(finding.items)} chat-session folder(s) that '
                    f'belong to cards that no longer exist'
                ),
            ))
        elif finding.category == 'orphan-gallery':
            if not report.characters_listed:
                plans.append(RepairPlan(
                    finding, action='skip', targets=[],
                    description=(
                        'Gallery folders are left alone: the character list '
                        'could not be read.'
                    ),
                ))
                continue
            plans.append(RepairPlan(
                finding, action='delete-gallery-folders', targets=list(finding.items),
                description=(
                    f'Delete {len(finding.items)} gallery folder(s) that belong '
                    f'to cards that no longer exist'
                ),
            ))
        elif finding.category == 'gallery-missing-file':
            plans.append(RepairPlan(
                finding, action='drop-gallery-rows', targets=list(finding.items),
                description=(
                    f'Drop {len(finding.items)} gallery row(s) whose image file '
                    f'is already gone (the files are not touched)'
                ),
            ))
        elif finding.category == 'backdrop-missing-file':
            plans.append(RepairPlan(
                finding, action='drop-backdrop-rows', targets=list(finding.items),
                description=(
                    f'Drop {len(finding.items)} backdrop row(s) whose image file '
                    f'is already gone (the files are not touched)'
                ),
            ))
        elif finding.category == 'stale-tombstone':
            plans.append(RepairPlan(
                finding, action='clear-tombstones', targets=list(finding.items),
                description=(
                    f'Clear {len(finding.items)} deletion record(s) whose '
                    f'SillyTavern file is already gone'
                ),
            ))
        elif finding.category == 'malformed-tags':
            plans.append(RepairPlan(
                finding, action='clean-tag-blob', targets=[],
                description='Remove the non-text tag values from this card',
            ))
        elif finding.category in ('orphan-file', 'gallery-orphan-file', 'backdrop-orphan-file'):
            plans.append(RepairPlan(
                finding, action='report-only', targets=list(finding.items),
                description='Nothing is deleted; the files are only reported',
            ))
    return plans


def plan_summary(plans: list[RepairPlan]) -> str:
    """A one-line summary of a repair plan."""
    if not plans:
        return 'Nothing to fix.'
    destructive = [p for p in plans if p.action.startswith('delete')]
    if not destructive:
        return f'{len(plans)} change(s) would be applied; no files are deleted.'
    files = sum(len(p.targets) for p in destructive)
    return (f'{len(plans)} change(s) would be applied, including the deletion '
            f'of {files} folder(s).')


def repair(db, report: DoctorReport,
           plans: list[RepairPlan] | None = None) -> list[str]:
    """Apply the safe, automatic fixes for the findings in *report*.

    Returns a list of human-readable descriptions of what was changed. Nothing
    here deletes a card file: a missing-file row is dropped from the database
    only (the file is already gone), never the other way round.

    Repairs that *delete* data (the orphan session and gallery folders) are
    skipped entirely when the character list could not be read - see
    :func:`_iter_characters`.  A library whose database is briefly busy must
    never be mistaken for an empty library.

    When *plans* is given, only those plans are applied, and each plan is
    limited to its own ``targets``.  This is what makes the dry-run preview
    authoritative: a user who unticks "delete the chat sessions of these 12
    deleted cards" must not have those folders deleted anyway.  Without
    *plans* the full set of automatic repairs is derived from the report.
    """
    if plans is None:
        plans = plan_repairs(report)
    applied: list[str] = []
    for plan in plans:
        try:
            applied.extend(_apply_plan(db, plan))
        except Exception as exc:
            logger.exception("Doctor repair failed for %s", plan.action)
            applied.append(f"Could not apply a fix: {exc}")
    return applied


def _apply_plan(db, plan: RepairPlan) -> list[str]:
    """Apply a single :class:`RepairPlan`."""
    action = plan.action
    finding = plan.finding
    if action == 'remove-card-row':
        return _drop_missing_card(db, finding)
    if action == 'delete-session-folders':
        return _remove_dirs(finding, plan.targets)
    if action == 'delete-gallery-folders':
        return _remove_gallery_dirs(finding, plan.targets)
    if action == 'drop-gallery-rows':
        return _drop_gallery_rows(db, finding, plan.targets)
    if action == 'drop-backdrop-rows':
        return _drop_backdrop_rows(db, finding, plan.targets)
    if action == 'clear-tombstones':
        return _clear_tombstones(db, finding, plan.targets)
    if action == 'clean-tag-blob':
        return _clean_tag_blob(db, finding)
    if action == 'skip':
        # The character list could not be read: the orphans cannot be
        # identified safely, so nothing is touched.
        if finding.category == 'orphan-sessions':
            return [
                'Chat sessions were left alone: the character list could '
                'not be read, so orphaned folders cannot be identified '
                'safely.'
            ]
        if finding.category == 'orphan-gallery':
            return [
                'Gallery folders were left alone: the character list '
                'could not be read, so orphaned folders cannot be '
                'identified safely.'
            ]
        return []
    if action == 'report-only':
        if finding.category == 'gallery-orphan-file':
            return [
                'Files in the gallery folder that are not in the library '
                'were left in place; add them to the gallery if you still '
                'want them.'
            ]
        if finding.category == 'backdrop-orphan-file':
            return [
                'Files in the backdrops folder that are not in the library '
                'were left in place; add them to the backdrops if you still '
                'want them.'
            ]
        return [
            'Files in the library folder that are not in the library '
            'were left in place; import them if you still want them.'
        ]
    return []


def _drop_missing_card(db, finding: Finding) -> list[str]:
    """Remove a ghost card row while keeping the user's pictures and chats.

    The card's PNG is gone, so the row can no longer be used - but the gallery
    images and chat sessions under ``gallery/<id>/`` and ``sessions/<id>/`` are
    independent assets and are still the user's data.  They are preserved (and
    registered so no later run deletes them) instead of being cascaded away.
    """
    card_id = finding.card_id
    db.remove_card(card_id, delete_files=False, record_tombstone=False,
                   delete_sessions=False, preserve_related=True)
    preserve_orphan_ids([card_id])
    return [
        f"Removed the library entry for {finding.detail} "
        "(chat history and gallery images were kept)"
    ]


def _remove_dirs(finding: Finding, targets: list[str] | None = None) -> list[str]:
    import shutil

    from src.app_paths import data_dir
    root = data_dir() / 'sessions'
    removed = []
    for name in (finding.items if targets is None else targets):
        name = str(name).strip()
        if not name:
            continue
        target = root / name
        if not target.is_dir():
            continue
        try:
            shutil.rmtree(target)
            removed.append(f"Deleted the chat sessions of the deleted card {name}")
        except OSError as exc:
            logger.warning("Could not remove %s: %s", target, exc)
    return removed


def _remove_gallery_dirs(finding: Finding, targets: list[str] | None = None) -> list[str]:
    import shutil

    from src.app_paths import data_dir
    root = data_dir() / 'gallery'
    removed = []
    for name in (finding.items if targets is None else targets):
        name = str(name).strip()
        if not name:
            continue
        target = root / name
        if not target.is_dir():
            continue
        try:
            shutil.rmtree(target)
            removed.append(f"Deleted the gallery folder of the deleted card {name}")
        except OSError as exc:
            logger.warning("Could not remove %s: %s", target, exc)
    return removed


def _drop_gallery_rows(db, finding: Finding,
                       targets: list[str] | None = None) -> list[str]:
    """Delete gallery rows whose file is already gone (never a file itself)."""
    dropped = 0
    for item in (finding.items if targets is None else targets):
        try:
            image_id = int(str(item).strip())
        except (TypeError, ValueError):
            continue
        try:
            row = db.delete_gallery_image(image_id)
            if row is not None:
                dropped += 1
        except Exception:
            logger.warning("Could not drop gallery row %s", image_id)
    if not dropped:
        return []
    return [f'Removed {dropped} gallery entr{"y" if dropped == 1 else "ies"} '
            f'whose image file is missing']


def _drop_backdrop_rows(db, finding: Finding,
                        targets: list[str] | None = None) -> list[str]:
    """Delete backdrop rows whose file is already gone (never a file itself)."""
    dropped = 0
    for item in (finding.items if targets is None else targets):
        try:
            image_id = int(str(item).strip())
        except (TypeError, ValueError):
            continue
        try:
            row = db.delete_backdrop_image(image_id)
            if row is not None:
                dropped += 1
        except Exception:
            logger.warning("Could not drop backdrop row %s", image_id)
    if not dropped:
        return []
    return [f'Removed {dropped} backdrop entr{"y" if dropped == 1 else "ies"} '
            f'whose image file is missing']


def _clear_tombstones(db, finding: Finding,
                      targets: list[str] | None = None) -> list[str]:
    cleared = 0
    for url in (finding.items if targets is None else targets):
        url = str(url).strip()
        if not url:
            continue
        try:
            db.clear_deleted_st_card(url)
            cleared += 1
        except Exception:
            logger.warning("Could not clear tombstone %s", url)
    return [f'Cleared {cleared} stale deletion record(s).'] if cleared else []


def _clean_tag_blob(db, finding: Finding) -> list[str]:
    """Rewrite one card's tag blob, keeping only text values.

    The old "repair" only appended a note claiming the values were ignored;
    the blob itself was never cleaned, so the finding came back on every run.
    """
    import json as _json
    if finding.card_id is None:
        return []
    try:
        with db._conn() as conn:
            row = conn.execute(
                'SELECT tags FROM characters WHERE id = ?', (finding.card_id,),
            ).fetchone()
            if row is None:
                return []
            from src.database import _decode_tag_blob
            tags = _decode_tag_blob(row[0])
            kept = [t for t in tags if isinstance(t, str)]
            conn.execute(
                'UPDATE characters SET tags = ? WHERE id = ?',
                (_json.dumps(kept, ensure_ascii=False), finding.card_id),
            )
            conn.commit()
    except Exception as exc:
        logger.warning("Could not clean tag blob for card %s: %s", finding.card_id, exc)
        return [f'Could not clean the tags of card {finding.card_id}: {exc}']
    return [f'Cleaned the tags of card {finding.card_id}.']


def summarize(report: DoctorReport) -> str:
    """One-line summary suitable for the status bar."""
    if not report.findings:
        return 'Library check passed - no problems found.'
    parts = []
    if report.errors:
        parts.append(f"{len(report.errors)} error(s)")
    if report.warnings:
        parts.append(f"{len(report.warnings)} warning(s)")
    info = len(report.findings) - len(report.errors) - len(report.warnings)
    if info:
        parts.append(f'{info} note(s)')
    return 'Library check: ' + ', '.join(parts) + '.'