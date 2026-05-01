# pytest-web

A lightweight local web UI for running pytest suites.

## Install

```bash
pip install pytest-web
```

Must be installed in the same Python environment as your project's pytest.

## Usage

```bash
cd your-project
pytest-web
```

Opens `http://127.0.0.1:8000` in your browser.

## CLI flags

| Flag | Default | Description |
|---|---|---|
| `--port` | `8000` | Port to listen on |
| `--host` | `127.0.0.1` | Bind host (do not change to 0.0.0.0) |
| `--cwd` | current dir | Project root (where pytest.ini lives) |
| `--no-browser` | — | Skip auto-opening the browser |

## How it works

1. **Fetch Tests** — runs `pytest --collect-only` in your project and lists all test IDs.
2. Select the tests you want and optionally set env vars or extra args.
3. **Run Selected** — executes `pytest <selected ids> -p pytest_web.plugin` and streams pass/fail results to the UI in real time via WebSocket.
4. **Cancel** — kills the pytest process and all xdist workers cleanly.

Your `pytest.ini`, `conftest.py`, fixtures, and plugins are all used as normal — this tool just wraps the command.
