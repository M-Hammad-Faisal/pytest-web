import asyncio
import configparser
import json
import os
import re
import shlex
import shutil
import signal
import socket
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
current_run: Optional[dict] = None  # {run_id, totals, test_states}
proc: Optional[asyncio.subprocess.Process] = None
proc_lock: Optional[asyncio.Lock] = None

# ── Allure state (independent of test runner) ─────────────────────
allure_proc: Optional[asyncio.subprocess.Process] = None
allure_lock: Optional[asyncio.Lock] = None

# Regex to extract --alluredir path from pytest addopts
_ALLUREDIR_RE = re.compile(r"--alluredir[= ](\S+)")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global proc_lock, allure_lock
    proc_lock = asyncio.Lock()
    allure_lock = asyncio.Lock()
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


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


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
            sys.executable,
            "-m",
            "pytest",
            "--help",
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
        "general:",
        "reporting:",
        "collection:",
        "test session debugging and configuration:",
        "logging:",
        "[pytest] ini-options in the first pytest.ini|tox.ini|setup.cfg|pyproject.toml file found:",
        "environment variables:",
        "options:",
        "positional arguments:",
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
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-p",
            "pytest_web.plugin",
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
            i += 2  # skip flag and its separate value
        elif arg.startswith(("-k=", "-m=")):
            i += 1  # skip combined -k=expr form
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

        # Always pass -n explicitly so it overrides any -n N baked into the
        # project's pytest.ini addopts. Disabling xdist via "-p no:xdist"
        # would make any "-n" in addopts an unrecognised argument and crash
        # pytest, so we keep xdist enabled and rely on it being a hard dep.
        cmd = [sys.executable, "-m", "pytest", *body.nodeids, *extra_args]
        cmd += ["-n", str(body.workers)]
        cmd += ["-p", "pytest_web.plugin"]

        webhook = f"http://{HOST}:{PORT}/internal/event"
        # Safety: if a key was sent as "NAME=value" split it before applying.
        clean_env: dict[str, str] = {}
        for k, v in body.env.items():
            k = str(k).strip()
            v = str(v).strip().strip('"').strip("'")
            if "=" in k:
                k, _, v = k.partition("=")
                k = k.strip()
                v = v.strip().strip('"').strip("'")
            if k:
                clean_env[k] = v

        env = {
            **os.environ,
            **clean_env,
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
            # Track nodeids we've already counted as started/ended.
            # In xdist, pytest_runtest_logreport fires once in the worker and
            # is then re-fired in the master via report forwarding — without
            # this dedup, every test would be counted twice in the totals.
            "started": set(),
            "finished": set(),
        }

        asyncio.create_task(_stream_proc(p, run_id))

        # Build a readable display command (truncate if many nodeids)
        n = len(body.nodeids)
        if n <= 4:
            id_part = body.nodeids
        else:
            id_part = body.nodeids[:3] + [f"...({n - 3} more)"]

        display_cmd = " ".join(
            ["pytest"]
            + id_part
            + extra_args
            + ["-n", str(body.workers)]
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
                check=False,
                capture_output=True,
            )
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, OSError):
        pass

    return {"cancelled": True}


# ── Allure helpers ────────────────────────────────────────────────


def _detect_allure_dir() -> Optional[str]:
    """Parse pytest config files to find the --alluredir path."""
    cwd = Path(PROJECT_CWD)

    def _search(text: str) -> Optional[str]:
        m = _ALLUREDIR_RE.search(text)
        if not m:
            return None
        # Strip trailing quotes/commas (present when path is inside a TOML list)
        return m.group(1).strip("\"'").rstrip(",")

    # pytest.ini  →  [pytest] addopts
    for ini_path in [cwd / "pytest.ini", cwd / "tox.ini"]:
        if ini_path.exists():
            cp = configparser.ConfigParser()
            try:
                cp.read(ini_path)
                result = _search(cp.get("pytest", "addopts", fallback=""))
                if result:
                    return result
            except Exception:
                pass

    # setup.cfg  →  [tool:pytest] addopts
    cfg = cwd / "setup.cfg"
    if cfg.exists():
        cp = configparser.ConfigParser()
        try:
            cp.read(cfg)
            result = _search(cp.get("tool:pytest", "addopts", fallback=""))
            if result:
                return result
        except Exception:
            pass

    # pyproject.toml  →  scan the full text (handles string & list forms)
    toml = cwd / "pyproject.toml"
    if toml.exists():
        try:
            result = _search(toml.read_text(encoding="utf-8"))
            if result:
                return result
        except Exception:
            pass

    return None


async def _kill_allure() -> None:
    """Kill the current allure process if running. Must be called under allure_lock."""
    global allure_proc
    if allure_proc is None or allure_proc.returncode is not None:
        return
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(allure_proc.pid)],
                check=False,
                capture_output=True,
            )
        else:
            os.killpg(os.getpgid(allure_proc.pid), signal.SIGTERM)
    except (ProcessLookupError, OSError):
        pass
    try:
        await asyncio.wait_for(allure_proc.wait(), timeout=3.0)
    except asyncio.TimeoutError:
        pass
    allure_proc = None


# ── Allure routes ─────────────────────────────────────────────────


@app.get("/allure/status")
async def allure_status():
    return {
        "available": shutil.which("allure") is not None,
        "detected_dir": _detect_allure_dir(),
        "serving": allure_proc is not None and allure_proc.returncode is None,
    }


class AllureOpenRequest(BaseModel):
    results_dir: str


@app.post("/allure/open")
async def allure_open(body: AllureOpenRequest):
    global allure_proc

    allure_bin = shutil.which("allure")
    if not allure_bin:
        raise HTTPException(400, "allure command not found on PATH")

    results_path = Path(body.results_dir)
    if not results_path.is_absolute():
        results_path = Path(PROJECT_CWD) / results_path

    if not results_path.exists():
        raise HTTPException(400, f"Results directory not found: {results_path}")

    report_path = results_path.parent / "allure-report"

    # Hold the lock for the entire kill → generate → start sequence so that
    # a second "Open Report" click cannot race and leak an orphaned process.
    async with allure_lock:
        await _kill_allure()

        # Copy history from previous report → results dir (enables trend graphs)
        history_src = report_path / "history"
        history_dst = results_path / "history"
        if history_src.exists():
            try:
                if history_dst.exists():
                    shutil.rmtree(history_dst)
                shutil.copytree(str(history_src), str(history_dst))
            except Exception:
                pass  # history copy failure is non-fatal

        # Clean the report output dir manually — `--clean` was removed in newer
        # allure versions and causes "Unknown Syntax Error" on allure 3+.
        if report_path.exists():
            try:
                shutil.rmtree(report_path)
            except Exception:
                pass

        # Generate the report (blocking but non-fatal to hold the lock here —
        # allure_lock is only used by allure routes, not the test runner)
        gen = await asyncio.create_subprocess_exec(
            allure_bin,
            "generate",
            str(results_path),
            "-o",
            str(report_path),
            cwd=str(results_path.parent),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        # Allure (Java CLI) writes errors to stdout, not stderr — capture both
        stdout_bytes, stderr_bytes = await gen.communicate()
        if gen.returncode != 0:
            err = (stdout_bytes + stderr_bytes).decode(errors="replace").strip()
            raise HTTPException(
                500, f"allure generate failed: {err or f'exit code {gen.returncode}'}"
            )

        # Serve the generated report with Python's built-in HTTP server.
        # `allure open` was removed in allure 3; using Python's server means
        # identical behaviour on allure 2 and 3, a deterministic URL, and
        # reliable stop/restart without any stdout parsing.
        port = _find_free_port()
        p = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "http.server",
            "--bind",
            "127.0.0.1",
            "--directory",
            str(report_path),
            str(port),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            **_build_subprocess_kwargs(),
        )
        allure_proc = p

    url = f"http://127.0.0.1:{port}/"
    return {"url": url, "serving": True}


@app.post("/allure/stop")
async def allure_stop():
    async with allure_lock:
        if allure_proc is None or allure_proc.returncode is not None:
            return {"stopped": False}
        await _kill_allure()
    return {"stopped": True}


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
        if nodeid in current_run["started"]:
            return {"ok": True}  # xdist duplicate
        current_run["started"].add(nodeid)
        current_run["test_states"][nodeid] = "running"
        totals["running"] = totals.get("running", 0) + 1
        await broadcast({"type": "test_start", "run_id": run_id, "nodeid": nodeid})

    elif event == "test_end":
        nodeid = body["nodeid"]
        if nodeid in current_run["finished"]:
            return {"ok": True}  # xdist duplicate — master re-fires worker reports
        current_run["finished"].add(nodeid)
        outcome = body.get("outcome", "failed")
        current_run["test_states"][nodeid] = outcome
        totals["running"] = max(0, totals.get("running", 0) - 1)
        if outcome in ("passed", "failed", "skipped"):
            totals[outcome] = totals.get(outcome, 0) + 1
        await broadcast(
            {
                "type": "test_end",
                "run_id": run_id,
                "nodeid": nodeid,
                "outcome": outcome,
                "duration": body.get("duration"),
                "longrepr": body.get("longrepr"),
            }
        )

    return {"ok": True}


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    ws_clients.add(websocket)

    # Send a state snapshot so a refreshed browser can rebuild current state
    await websocket.send_json(
        {
            "type": "snapshot",
            "run_id": current_run["run_id"] if current_run else None,
            "running": current_run is not None,
            "totals": current_run["totals"]
            if current_run
            else {
                "total": 0,
                "passed": 0,
                "failed": 0,
                "skipped": 0,
                "running": 0,
            },
            "test_states": current_run["test_states"] if current_run else {},
        }
    )

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
            await broadcast(
                {
                    "type": "log",
                    "run_id": run_id,
                    "stream": stream_name,
                    "line": line.decode(errors="replace").rstrip(),
                }
            )

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

    await broadcast(
        {
            "type": "session_end",
            "run_id": run_id,
            "exit_status": p.returncode,
            "totals": saved_totals or {},
        }
    )
