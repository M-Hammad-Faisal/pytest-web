import asyncio
import json
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from pytest_web import __version__ as PW_VERSION

# ── Config (set by cli.py before uvicorn starts) ──────────────────
HOST = os.environ.get("PYTEST_WEB_HOST", "127.0.0.1")
PORT = int(os.environ.get("PYTEST_WEB_PORT", "8000"))
PROJECT_CWD = os.environ.get("PYTEST_WEB_CWD", os.getcwd())
STATIC_DIR = Path(__file__).parent / "static"

# ── Shared state (single-process, single-run-at-a-time) ──────────
ws_clients: set[WebSocket] = set()
current_run: Optional[dict] = None   # {run_id, totals, test_states}
proc: Optional[asyncio.subprocess.Process] = None
proc_lock: Optional[asyncio.Lock] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global proc_lock
    proc_lock = asyncio.Lock()
    yield


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ── Helpers ───────────────────────────────────────────────────────

async def broadcast(msg: dict) -> None:
    dead: set[WebSocket] = set()
    for client in list(ws_clients):
        try:
            await client.send_json(msg)
        except Exception:
            dead.add(client)
    ws_clients.difference_update(dead)


def _build_subprocess_kwargs() -> dict:
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


# ── Request models ────────────────────────────────────────────────

class DiscoverRequest(BaseModel):
    args: str = ""


class RunRequest(BaseModel):
    nodeids: list[str]
    workers: int = 1
    args: str = ""
    env: dict[str, str] = {}


# ── Routes ────────────────────────────────────────────────────────

@app.get("/")
async def index():
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html.replace("__PW_VERSION__", PW_VERSION))


@app.get("/options")
async def options():
    """Parse `pytest --help` from the user's project to discover custom options
    contributed by conftest.py / installed plugins (e.g. --browser, --headed).
    """
    try:
        p = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "pytest", "--help",
            cwd=PROJECT_CWD,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, _ = await p.communicate()
    except Exception as e:
        return {"options": [], "error": str(e)}

    text = stdout_bytes.decode(errors="replace")
    custom: list[dict] = []
    seen: set[str] = set()

    # `pytest --help` groups options under headers like "custom options:".
    # We pick out option lines belonging to non-pytest-builtin groups.
    builtin_groups = {
        "general:", "reporting:", "collection:", "test session debugging and configuration:",
        "logging:", "[pytest] ini-options in the first pytest.ini|tox.ini|setup.cfg|pyproject.toml file found:",
        "environment variables:", "options:", "positional arguments:",
    }
    in_custom_group = False
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        # Group headers end with ':' and have no leading whitespace
        if stripped.endswith(":") and raw_line and not raw_line.startswith(" "):
            in_custom_group = stripped.lower() not in builtin_groups
            continue
        if not in_custom_group:
            continue
        # Option lines start with whitespace + '-'
        if not raw_line.startswith(("  -", "  --")):
            continue
        # Extract the long option name (first token starting with --)
        for tok in stripped.split():
            tok = tok.rstrip(",")
            if tok.startswith("--") and len(tok) > 2:
                # Strip "=value" or "VALUE" hints — keep just the flag itself
                name = tok.split("=", 1)[0]
                if name in seen:
                    break
                seen.add(name)
                # Heuristic: if next token after the option is uppercase or =VAL, it takes a value
                takes_value = "=" in tok or any(
                    t.isupper() and t.isalpha() for t in stripped.split()[1:2]
                )
                custom.append({"name": name, "type": "value" if takes_value else "flag"})
                break

    return {"options": custom}


@app.post("/discover")
async def discover(body: DiscoverRequest):
    extra_args = shlex.split(body.args) if body.args.strip() else []

    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    collect_file = tmp.name
    tmp.close()

    try:
        cmd = [
            sys.executable, "-m", "pytest",
            "--collect-only", "-q",
            "-p", "pytest_web.plugin",
            *extra_args,
        ]
        env = {**os.environ, "PYTEST_WEB_COLLECT_FILE": collect_file}

        p = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=PROJECT_CWD,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr_bytes = await p.communicate()

        try:
            with open(collect_file) as f:
                nodeids = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            nodeids = []

        # exit 5 = no tests collected (not an error)
        if not nodeids and p.returncode not in (0, 5):
            error_text = stderr_bytes.decode(errors="replace").strip()
            return {"nodeids": [], "error": error_text or f"pytest exited {p.returncode}"}

        return {"nodeids": nodeids}
    finally:
        try:
            os.unlink(collect_file)
        except OSError:
            pass


def _strip_keyword_filters(args: list[str]) -> list[str]:
    """Remove -k / -m flags from a run command.

    When running by explicit node IDs, keyword/marker filters applied on top
    will silently exclude those very tests if the names don't match the
    expression (e.g. fetched with -k 26653 then ran tests named 21901/21906).
    Node IDs already pinpoint exactly which tests to run — filters are noise.
    """
    result: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("-k", "-m"):
            i += 2          # skip flag and its separate value
        elif arg.startswith(("-k=", "-m=")):
            i += 1          # skip combined -k=expr form
        else:
            result.append(arg)
            i += 1
    return result


@app.post("/run")
async def run(body: RunRequest):
    global current_run, proc

    async with proc_lock:
        if current_run is not None:
            raise HTTPException(status_code=409, detail="A run is already in progress")

        extra_args = shlex.split(body.args) if body.args.strip() else []
        extra_args = _strip_keyword_filters(extra_args)
        run_id = uuid.uuid4().hex

        cmd = [sys.executable, "-m", "pytest", *body.nodeids, *extra_args]
        if body.workers > 1:
            cmd += ["-n", str(body.workers)]
        cmd += ["-p", "pytest_web.plugin"]

        webhook = f"http://{HOST}:{PORT}/internal/event"
        env = {
            **os.environ,
            **{str(k): str(v) for k, v in body.env.items()},
            "PYTEST_WEB_WEBHOOK": webhook,
            "PYTEST_WEB_RUN_ID": run_id,
        }

        p = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=PROJECT_CWD,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **_build_subprocess_kwargs(),
        )
        proc = p
        current_run = {
            "run_id": run_id,
            "totals": {
                "total": len(body.nodeids),
                "passed": 0,
                "failed": 0,
                "skipped": 0,
                "running": 0,
            },
            "test_states": {},
        }

        asyncio.create_task(_stream_proc(p, run_id))

        # Build a readable display command (truncate if many nodeids)
        n = len(body.nodeids)
        if n <= 4:
            id_part = body.nodeids
        else:
            id_part = body.nodeids[:3] + [f"...({n - 3} more)"]

        display_cmd = " ".join(
            ["pytest"] + id_part + extra_args
            + (["-n", str(body.workers)] if body.workers > 1 else [])
            + ["-p", "pytest_web.plugin"]
        )
        return {"run_id": run_id, "command": display_cmd}


@app.post("/cancel")
async def cancel():
    global proc
    if proc is None or proc.returncode is not None:
        return {"cancelled": False}

    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                check=False, capture_output=True,
            )
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, OSError):
        pass

    return {"cancelled": True}


@app.post("/internal/event")
async def internal_event(request: Request):
    global current_run
    try:
        body = await request.json()
    except Exception:
        # Plugin timed out and disconnected before server read the body — ignore.
        return {"ok": False}
    run_id = body.get("run_id")
    event = body.get("event")

    if not current_run or current_run.get("run_id") != run_id:
        return {"ok": False}

    totals = current_run["totals"]

    if event == "session_start":
        total = body.get("total", totals["total"])
        totals["total"] = total
        await broadcast({"type": "session_start", "run_id": run_id, "total": total})

    elif event == "test_start":
        nodeid = body["nodeid"]
        current_run["test_states"][nodeid] = "running"
        totals["running"] = totals.get("running", 0) + 1
        await broadcast({"type": "test_start", "run_id": run_id, "nodeid": nodeid})

    elif event == "test_end":
        nodeid = body["nodeid"]
        outcome = body.get("outcome", "failed")
        current_run["test_states"][nodeid] = outcome
        totals["running"] = max(0, totals.get("running", 0) - 1)
        if outcome in ("passed", "failed", "skipped"):
            totals[outcome] = totals.get(outcome, 0) + 1
        await broadcast({
            "type": "test_end",
            "run_id": run_id,
            "nodeid": nodeid,
            "outcome": outcome,
            "duration": body.get("duration"),
            "longrepr": body.get("longrepr"),
        })

    return {"ok": True}


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    ws_clients.add(websocket)

    # Send a state snapshot so a refreshed browser can rebuild current state
    await websocket.send_json({
        "type": "snapshot",
        "run_id": current_run["run_id"] if current_run else None,
        "running": current_run is not None,
        "totals": current_run["totals"] if current_run else {
            "total": 0, "passed": 0, "failed": 0, "skipped": 0, "running": 0,
        },
        "test_states": current_run["test_states"] if current_run else {},
    })

    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_clients.discard(websocket)


# ── Background streaming task ─────────────────────────────────────

async def _stream_proc(p: asyncio.subprocess.Process, run_id: str) -> None:
    global current_run, proc

    async def _read(stream: asyncio.StreamReader, stream_name: str) -> None:
        while True:
            line = await stream.readline()
            if not line:
                break
            await broadcast({
                "type": "log",
                "run_id": run_id,
                "stream": stream_name,
                "line": line.decode(errors="replace").rstrip(),
            })

    await asyncio.gather(
        _read(p.stdout, "stdout"),
        _read(p.stderr, "stderr"),
    )
    await p.wait()

    saved_totals: Optional[dict] = None
    async with proc_lock:
        if current_run and current_run.get("run_id") == run_id:
            saved_totals = dict(current_run["totals"])
            current_run = None
            proc = None

    await broadcast({
        "type": "session_end",
        "run_id": run_id,
        "exit_status": p.returncode,
        "totals": saved_totals or {},
    })
