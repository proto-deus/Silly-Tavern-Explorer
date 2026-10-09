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

import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


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
class DoctorReport:
    findings: list[Finding] = field(default_factory=list)

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


def _iter_characters(db) -> list[dict]:
    """All character rows, tolerating a minimal/absent get_all()."""
    try:
        return db.get_all()
    except Exception:
        logger.exception("Doctor could not list characters")
        return []


def check_missing_card_files(db) -> list[Finding]:
    """Cards whose backing PNG/JSON no longer exists on disk."""
    out: list[Finding] = []
    for row in _iter_characters(db):
        source = row.get('source_path') or ''
        if not source:
            out.append(Finding(
                'no-source-path', 'error',
                f"Card '{row.get('name', '?')}' has no file path",
                detail=f"id={row.get('id')}", card_id=row.get('id'),
            ))
            continue
        if not Path(source).exists():
            out.append(Finding(
                'missing-file', 'error',
                f"File missing for '{row.get('name', '?')}'",
                detail=source, fixable=True, card_id=row.get('id'),
            ))
    return out


def check_unreadable_cards(db) -> list[Finding]:
    """Cards whose file exists but holds no parsable character data.

    The worst kind of silent corruption: the card looks fine in the grid but
    its content is gone (a truncated write, a non-card PNG).
    """
    from src.card_parser import read_card_data

    out: list[Finding] = []
    for row in _iter_characters(db):
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
    for blob, in _iter_tag_blobs(db):
        tags = _decode_tag_blob(blob)
        bad = [t for t in tags if not isinstance(t, str)]
        if bad:
            out.append(Finding(
                'malformed-tags', 'warning',
                f'{len(bad)} non-text tag value(s) found',
                detail=', '.join(repr(t)[:24] for t in bad[:3]), fixable=True,
            ))
    return out


def _iter_tag_blobs(db):
    try:
        with db._conn() as conn:
            yield from conn.execute('SELECT tags FROM characters').fetchall()
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
    known = {str(Path(r['source_path'])) for r in _iter_characters(db)
             if r.get('source_path')}
    orphans: list[str] = []
    for path in sorted(lib.iterdir()):
        if not path.is_file() or path.suffix.lower() not in ('.png', '.json'):
            continue
        # Skip our own atomic-write staging files.
        if path.name.startswith('.ste-') or path.name.endswith('.tmp'):
            continue
        try:
            if str(path.resolve()) not in {str(Path(p).resolve()) for p in known}:
                orphans.append(path.name)
        except OSError:
            continue
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
    live = {str(r.get('id')) for r in _iter_characters(db)}
    orphans = []
    for child in sorted(root.iterdir()):
        if child.is_dir() and child.name.isdigit() and child.name not in live:
            orphans.append(child.name)
    if not orphans:
        return []
    return [Finding(
        'orphan-sessions', 'warning',
        f'{len(orphans)} chat-session folder(s) belong to deleted cards',
        detail=', '.join(orphans[:10]), fixable=True,
        items=orphans,
    )]


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
    for row in _iter_characters(db):
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
    return [Finding(
        'undecryptable-api-key', 'error',
        f"API key for '{providers[0]}' could not be decrypted",
        detail=('The stored key has been left untouched. Open Settings and '
                're-enter it to replace it.'),
    )]


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
            st_characters_dir: str | Path | None = None) -> DoctorReport:
    """Run every check and return a combined report."""
    report = DoctorReport()
    checks = [
        lambda: check_missing_card_files(db),
        lambda: check_unreadable_cards(db),
        lambda: check_malformed_tags(db),
        lambda: check_orphan_files(db, library_dir),
        lambda: check_orphan_sessions(db, sessions_dir),
        lambda: check_stale_links(db, st_characters_dir),
        lambda: check_stale_tombstones(db),
        check_undecryptable_keys,
    ]
    if db_path is None:
        db_path = getattr(db, 'db_path', None)
    if db_path:
        checks.insert(0, lambda: check_database(db_path))

    for check in checks:
        try:
            for finding in check():
                report.add(finding)
        except Exception:
            # A broken check must not abort the whole report.
            logger.exception("Doctor check failed: %s", getattr(check, '__name__', check))
            report.add(Finding(
                'check-failed', 'warning',
                'An integrity check could not complete',
                detail=getattr(check, '__name__', 'unknown'),
            ))
    return report


def repair(db, report: DoctorReport) -> list[str]:
    """Apply the safe, automatic fixes for the findings in *report*.

    Returns a list of human-readable descriptions of what was changed. Nothing
    here deletes a card file: a missing-file row is dropped from the database
    only (the file is already gone), never the other way round.
    """
    applied: list[str] = []
    for finding in report.findings:
        if not finding.fixable:
            continue
        try:
            if finding.category == 'missing-file' and finding.card_id is not None:
                db.remove_card(finding.card_id, delete_files=False,
                               record_tombstone=False)
                applied.append(f"Removed the library entry for {finding.detail}")
            elif finding.category == 'orphan-sessions':
                applied.extend(_remove_dirs(finding))
            elif finding.category == 'stale-tombstone':
                applied.extend(_clear_tombstones(db, finding))
            elif finding.category == 'malformed-tags':
                applied.append(
                    'Non-text tag values are now ignored rather than breaking '
                    'the tag views.'
                )
            elif finding.category == 'orphan-file':
                applied.append(
                    'Files in the library folder that are not in the library '
                    'were left in place; import them if you still want them.'
                )
        except Exception as exc:
            logger.exception("Doctor repair failed for %s", finding.category)
            applied.append(f"Could not apply a fix: {exc}")
    return applied


def _remove_dirs(finding: Finding) -> list[str]:
    import shutil

    from src.app_paths import data_dir
    root = data_dir() / 'sessions'
    removed = []
    for name in finding.items:
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


def _clear_tombstones(db, finding: Finding) -> list[str]:
    cleared = 0
    for url in finding.items:
        url = str(url).strip()
        if not url:
            continue
        try:
            db.clear_deleted_st_card(url)
            cleared += 1
        except Exception:
            logger.warning("Could not clear tombstone %s", url)
    return [f'Cleared {cleared} stale deletion record(s).'] if cleared else []


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