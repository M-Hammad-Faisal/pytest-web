# pytest-web

A lightweight local web UI for discovering, selecting, and running pytest suites — with real-time pass/fail status, filtering, env-var injection, and on-demand Allure reports.

## Install

```bash
pip install pytest-web
```

Must be installed in the **same Python environment** as your project's pytest.

## Quick start

```bash
cd your-project
pytest-web
```

Opens `http://127.0.0.1:8000` in your browser automatically.

## CLI flags

| Flag | Default | Description |
|---|---|---|
| `--port` | `8000` | Port to listen on |
| `--host` | `127.0.0.1` | Bind host |
| `--cwd` | current dir | Project root (where `pytest.ini` lives) |
| `--no-browser` | — | Skip auto-opening the browser |

## Features

### Fetch & select tests

Click **Fetch Tests** (or press Enter in the path/args box) to collect all tests via `pytest --collect-only`. Tests are grouped by file. You can:

- Check/uncheck individual tests or entire files
- Use **Select All** to toggle only what's currently visible
- Type in the **filter box** to search by test name
- Click any counter (**passed / failed / skipped**) to filter to that status — click again to clear

### Run controls

- **Workers** — number of parallel workers (`-n`). Overrides any `-n` in `pytest.ini addopts`.
- **▶ Run Selected** — runs only checked tests that are currently visible
- **■ Cancel** — terminates the pytest process and all xdist workers cleanly

### Param builder

Use the dropdown to add common pytest flags (`-k`, `-m`, `--tb`, `-x`, `--maxfail`, etc.) to the args bar without typing. Project-specific options from your `conftest.py` or installed plugins (e.g. `--browser`, `--headed`) are auto-detected and added to the dropdown.

### Env vars

Inject environment variables for the test run. Type `NAME=value` in the Name field and it auto-splits on blur. Variables are saved to `localStorage` and restored on reload.

### Live status

While tests run, each row shows a pulsing dot (running) that turns green/red/yellow on completion. The counters update in real time and **accumulate across runs** — so if you run a subset, the results from tests not in that run are preserved in the counters.

### Output log

Pytest's stdout/stderr streams in a collapsible panel at the bottom. Opens automatically if stderr has output (e.g. failures). Shows all xdist worker output.

### Command preview

The `$ pytest ...` command preview updates as you change selections, args, or workers. Click **⎘** to copy it.

---

## Allure report (on demand)

If you use [Allure](https://allurereport.org/) for test reporting, pytest-web can generate and serve the Allure report with **history tracking** built in.

### Requirements

Install the Allure CLI globally (separate from the Python package):

```bash
# macOS
brew install allure

# Linux — download from https://github.com/allure-framework/allure2/releases
# and place the `allure` binary on your PATH
```

### Setup

Your project should have `--alluredir` in `pytest.ini` (or `pyproject.toml`):

```ini
# pytest.ini
[pytest]
addopts = --alluredir=test/allure-results --clean-alluredir
```

pytest-web auto-detects this path. You can also enter or override it manually in the **Allure** bar at the bottom of the page.

### Usage

1. Run your tests (they populate the results directory)
2. Click **Open Report** in the Allure bar
3. The report is generated and served — your browser opens automatically
4. The URL is shown as a link in case the browser doesn't open

Click **Stop** to shut down the Allure server.

### History / trends

Every time you click **Open Report**, pytest-web copies the history from the previous report back into the results directory before regenerating. This means Allure's **trend graphs and history tabs work automatically** — no manual setup required.

The report is written to `allure-report/` next to your results directory (e.g. if results are in `test/allure-results`, the report is at `test/allure-report`).

> **Note:** The Allure server is completely independent of the test runner. You can run tests while the Allure server is up, and vice versa.

---

## How it works

1. **Discover** — `pytest --collect-only -p pytest_web.plugin` writes collected node IDs to a temp file and returns them to the UI.
2. **Run** — `pytest <selected-ids> -n <workers> -p pytest_web.plugin` is launched as a subprocess. The plugin fires HTTP webhooks back to the FastAPI server at each test start/end.
3. **Stream** — the server broadcasts those events over WebSocket to all connected browser tabs. The UI updates each row's status dot and counters in real time.
4. **Allure** — `allure generate` builds the HTML report (with history), then `allure open` serves it on a separate port.

Your `pytest.ini`, `conftest.py`, fixtures, and plugins are all used normally — pytest-web just wraps the command.

## Compatibility

- Python 3.9 – 3.13
- pytest 7+
- pytest-xdist 3+ (installed automatically as a dependency)
- Works with any pytest plugin (playwright, django, anyio, etc.)
