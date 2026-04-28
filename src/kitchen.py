"""Kitchen — local agentic work orchestrator.

This is the M1a skeleton. The orchestration loop is a no-op; M3 fills it in.
For now it exposes start()/stop() so serve.py can wire lifecycle calls without
needing to know whether dispatch is implemented yet.

See docs/KITCHEN.md for the full spec. Concurrency model: one global cap and
one per-project cap, both read from WORKFLOW.toml at tick time. Active runs
are derived from the `runs` table (status in {queued,preparing,running,
needs_input}); state is never cached on the ticket itself. The unique partial
index `one_active_run_per_subject` makes double-dispatch impossible.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_started = False
_stop_event: threading.Event | None = None
_thread: threading.Thread | None = None


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def start(get_db: Callable, settings: dict | None = None) -> None:
    """Start the Kitchen orchestrator background thread.

    M1a: thread does nothing. Present so serve.py can wire startup unconditionally.
    M3: this becomes the poll → reconcile → dispatch loop described in docs/KITCHEN.md §8.
    """
    global _started, _stop_event, _thread
    if _started:
        logger.warning("kitchen.start called twice — ignoring")
        return
    _stop_event = threading.Event()
    _thread = threading.Thread(target=_run_loop, args=(get_db, settings or {}, _stop_event),
                               name="kitchen-orchestrator", daemon=True)
    _thread.start()
    _started = True
    logger.info("kitchen started (M1a no-op loop)")


def stop(timeout: float = 5.0) -> None:
    """Signal the orchestrator to stop and wait briefly for it to exit."""
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
    logger.info("kitchen stopped")


# ---------------------------------------------------------------------------
# The poll loop (M1a stub)
# ---------------------------------------------------------------------------

def _run_loop(get_db: Callable, settings: dict, stop_event: threading.Event) -> None:
    """M1a: sleep until asked to stop. M3: implement the tick described in §8."""
    poll_interval_seconds = float(settings.get("kitchen_poll_seconds", 5.0))
    while not stop_event.is_set():
        # M3 will call _tick(get_db) here.
        stop_event.wait(timeout=poll_interval_seconds)
