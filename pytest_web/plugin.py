"""
Pytest plugin injected via -p pytest_web.plugin during /discover and /run.
Reads env vars set by the FastAPI server; all behaviour is a no-op when they
are absent so the plugin is safe to load in any pytest run.
"""

import json
import os
import threading
import urllib.request

COLLECT_FILE = os.environ.get("PYTEST_WEB_COLLECT_FILE")
WEBHOOK = os.environ.get("PYTEST_WEB_WEBHOOK")
RUN_ID = os.environ.get("PYTEST_WEB_RUN_ID")

_session = None
_item_count: int = 0


def _post(payload: dict) -> None:
    """Fire-and-forget POST to the FastAPI webhook. Never blocks pytest."""
    if not WEBHOOK:
        return
    body = json.dumps({**payload, "run_id": RUN_ID}).encode()
    req = urllib.request.Request(
        WEBHOOK,
        data=body,
        headers={"Content-Type": "application/json"},
    )

    def _send() -> None:
        try:
            urllib.request.urlopen(req, timeout=2.0)
        except Exception:
            pass  # server not reachable — never crash the user's test run

    threading.Thread(target=_send, daemon=True).start()


# ── Collection hooks ──────────────────────────────────────────────


def pytest_sessionstart(session) -> None:
    global _session
    _session = session


def pytest_collection_finish() -> None:
    """
    Fires after ALL pytest_collection_modifyitems hooks have run (including
    pytest's own trylast -k / -m filters). Reading session.items here gives
    the final filtered list, so -k / -m args are correctly respected.
    """
    global _item_count
    if not _session:
        return

    items = list(_session.items)
    _item_count = len(items)

    if COLLECT_FILE:
        with open(COLLECT_FILE, "w") as f:
            json.dump([item.nodeid for item in items], f)

    # Synchronous POST — safe here because no tests have started yet.
    # Guarantees session_start arrives before the first test_start event.
    if WEBHOOK:
        body = json.dumps(
            {
                "event": "session_start",
                "total": _item_count,
                "run_id": RUN_ID,
            }
        ).encode()
        req = urllib.request.Request(
            WEBHOOK, data=body, headers={"Content-Type": "application/json"}
        )
        try:
            urllib.request.urlopen(req, timeout=1.0)
        except Exception:
            pass


# ── Run hooks ─────────────────────────────────────────────────────


def pytest_runtest_logstart(nodeid: str, location) -> None:
    _post({"event": "test_start", "nodeid": nodeid})


def pytest_runtest_logreport(report) -> None:
    if report.when == "call":
        _post(
            {
                "event": "test_end",
                "nodeid": report.nodeid,
                "outcome": report.outcome,
                "duration": round(report.duration, 4),
                "longrepr": str(report.longrepr) if report.failed else None,
            }
        )
    elif report.when == "setup" and report.skipped:
        # Skipped tests never reach "call" phase
        _post(
            {
                "event": "test_end",
                "nodeid": report.nodeid,
                "outcome": "skipped",
                "duration": 0.0,
                "longrepr": None,
            }
        )


def pytest_sessionfinish(session, exitstatus) -> None:
    _post({"event": "session_end", "exit_status": int(exitstatus)})
