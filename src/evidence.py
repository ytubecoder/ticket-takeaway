"""Evidence rotation pipeline (Kitchen M5).

Each run can produce evidence under ~/.claude/ticket-takeaway/evidence/{run_id}/.
The runs row stays forever (small SQLite, valuable history); only on-disk
artifacts age out via the live → summarised → pruned ladder defined in
docs/KITCHEN.md §13.

  live (0–live_days)             everything raw
  summarised (live → live+sum)   summary.md + gzipped transcript + index of pruned files
  pruned (past summarised)       only summary.md remains

A daily background thread in serve.py calls rotate_evidence(); the function
itself is pure-DB-and-filesystem and safe to call from anywhere.
"""

from __future__ import annotations

import gzip
import logging
import shutil
import sqlite3
import threading
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from constants import DASHBOARD_DIR

logger = logging.getLogger(__name__)

EVIDENCE_ROOT = DASHBOARD_DIR / "evidence"

# Filenames inside the evidence dir.
SUMMARY_NAME = "summary.md"
TRANSCRIPT_CANDIDATES = ("transcript.txt", "agent.log", "stdout.log")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def rotate_evidence(
    get_db: Callable[[], sqlite3.Connection],
    live_days: int = 30,
    summarised_days: int = 60,
    now: datetime | None = None,
) -> dict:
    """Sweep all runs and age their on-disk evidence.

    Args:
      get_db: connection factory (so this can run in a thread).
      live_days: runs whose finished_at is older than this become 'summarised'.
      summarised_days: ADDITIONAL days a 'summarised' row stays before pruning.
      now: clock seam for tests (defaults to datetime.now(UTC)).

    Returns counts: {summarised: N, pruned: N, skipped: N, errors: N}.
    """
    counts = {"summarised": 0, "pruned": 0, "skipped": 0, "errors": 0}
    if now is None:
        now = datetime.now(timezone.utc)
    summarise_threshold = now - timedelta(days=live_days)
    prune_threshold = now - timedelta(days=live_days + summarised_days)

    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, project_id, subject_type, subject_id, status, "
            "       summary, error_class, error_message, finished_at, "
            "       evidence_dir, evidence_status, runner_kind "
            "FROM runs "
            "WHERE finished_at IS NOT NULL "
            "  AND evidence_status IN ('live', 'summarised')"
        ).fetchall()
    finally:
        conn.close()

    for r in rows:
        try:
            finished_at = _parse_iso(r["finished_at"])
        except Exception:
            counts["skipped"] += 1
            continue
        if finished_at is None:
            counts["skipped"] += 1
            continue

        evidence_dir = Path(r["evidence_dir"]) if r["evidence_dir"] else None
        try:
            if r["evidence_status"] == "live" and finished_at < summarise_threshold:
                if evidence_dir is not None:
                    _summarise(evidence_dir, dict(r))
                _set_evidence_status(get_db, r["id"], "summarised")
                counts["summarised"] += 1
            elif r["evidence_status"] == "summarised" and finished_at < prune_threshold:
                if evidence_dir is not None:
                    _prune(evidence_dir)
                _set_evidence_status(get_db, r["id"], "pruned")
                counts["pruned"] += 1
            else:
                counts["skipped"] += 1
        except Exception:
            logger.exception("rotate_evidence failed for run %s", r["id"])
            counts["errors"] += 1
    return counts


# ---------------------------------------------------------------------------
# Summarise step — write summary.md + gzip large text artifacts.
# ---------------------------------------------------------------------------


def _summarise(evidence_dir: Path, run_row: dict) -> None:
    """Reduce a live evidence dir to its summarised form.

    Steps (each is best-effort and skipped if the artifact isn't present):
      1. Write summary.md (overwrites any existing).
      2. gzip any plaintext transcript file (.txt/.log) > 1 KiB.
      3. Build an index.txt listing every file in the dir at this point.
    """
    if not evidence_dir.exists():
        # Even if the dir is gone, write an empty summary alongside-of-nothing
        # so the audit trail still has something to read later. Skip if even
        # creating the parent fails.
        try:
            evidence_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return

    # 1. summary.md
    md = _build_summary_markdown(run_row)
    try:
        (evidence_dir / SUMMARY_NAME).write_text(md, encoding="utf-8")
    except OSError:
        pass

    # 2. gzip transcripts (skips already-gzipped files; skips small ones).
    for child in list(evidence_dir.iterdir()):
        if not child.is_file():
            continue
        if child.suffix == ".gz":
            continue
        if child.name == SUMMARY_NAME or child.name == "index.txt":
            continue
        if child.suffix.lower() in (".txt", ".log") and child.stat().st_size > 1024:
            try:
                with (
                    open(child, "rb") as src,
                    gzip.open(str(child) + ".gz", "wb") as dst,
                ):
                    shutil.copyfileobj(src, dst)
                child.unlink()
            except OSError:
                continue

    # 3. index.txt
    try:
        names = sorted(p.name for p in evidence_dir.iterdir() if p.name != "index.txt")
        (evidence_dir / "index.txt").write_text(
            "\n".join(names) + "\n", encoding="utf-8"
        )
    except OSError:
        pass


def _build_summary_markdown(run_row: dict) -> str:
    """Build a human-readable summary.md from the runs row + a manifest of artifacts."""
    rid = run_row.get("id")
    status = run_row.get("status", "")
    runner = run_row.get("runner_kind", "")
    subject_type = run_row.get("subject_type", "")
    subject_id = run_row.get("subject_id", "")
    finished_at = run_row.get("finished_at", "")
    summary = (run_row.get("summary") or "").strip()
    err_class = run_row.get("error_class") or ""
    err_msg = (run_row.get("error_message") or "").strip()
    lines = [
        f"# Run #{rid} — {runner}",
        "",
        f"- **Status:** `{status}`",
        f"- **Subject:** `{subject_type}/{subject_id}`",
        f"- **Finished:** {finished_at}",
    ]
    if summary:
        lines += ["", "## Summary", "", summary]
    if err_class or err_msg:
        lines += ["", "## Error", ""]
        if err_class:
            lines.append(f"- **Class:** `{err_class}`")
        if err_msg:
            lines += ["", "```", err_msg[:2000], "```"]
    lines += ["", "_Evidence summarised by Kitchen rotation pipeline (M5)._"]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Prune step — keep only summary.md.
# ---------------------------------------------------------------------------


def _prune(evidence_dir: Path) -> None:
    """Drop everything in evidence_dir except summary.md."""
    if not evidence_dir.exists():
        return
    for child in list(evidence_dir.iterdir()):
        if child.name == SUMMARY_NAME:
            continue
        try:
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _set_evidence_status(
    get_db: Callable[[], sqlite3.Connection], run_id: int, status: str
) -> None:
    conn = get_db()
    try:
        conn.execute(
            "UPDATE runs SET evidence_status = ? WHERE id = ?", (status, run_id)
        )
        conn.commit()
    finally:
        conn.close()


def _parse_iso(s: str) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------------------
# Background daemon — daily tick.
# ---------------------------------------------------------------------------

_started = False
_stop_event: threading.Event | None = None
_thread: threading.Thread | None = None
DEFAULT_INTERVAL_S = 24 * 3600  # daily


def start_rotation_daemon(
    get_db: Callable[[], sqlite3.Connection],
    interval_s: int = DEFAULT_INTERVAL_S,
    live_days: int = 30,
    summarised_days: int = 60,
) -> None:
    """Spawn a background thread that calls rotate_evidence on a fixed cadence."""
    global _started, _stop_event, _thread
    if _started:
        return
    _stop_event = threading.Event()

    def _loop():
        # First tick after a small delay so startup isn't blocked by IO.
        if not _stop_event.wait(timeout=30):
            try:
                rotate_evidence(
                    get_db, live_days=live_days, summarised_days=summarised_days
                )
            except Exception:
                logger.exception("evidence rotation tick failed")
        while not _stop_event.is_set():
            if _stop_event.wait(timeout=interval_s):
                return
            try:
                rotate_evidence(
                    get_db, live_days=live_days, summarised_days=summarised_days
                )
            except Exception:
                logger.exception("evidence rotation tick failed")

    _thread = threading.Thread(
        target=_loop, name="kitchen-evidence-rotation", daemon=True
    )
    _thread.start()
    _started = True


def stop_rotation_daemon(timeout: float = 2.0) -> None:
    global _started, _stop_event, _thread
    if not _started:
        return
    if _stop_event:
        _stop_event.set()
    if _thread:
        _thread.join(timeout=timeout)
    _started = False
    _stop_event = None
    _thread = None
