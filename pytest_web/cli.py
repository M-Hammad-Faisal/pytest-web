import argparse
import os
import sys
import threading
import time
import webbrowser


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="pytest-web",
        description="Local web UI for pytest — run from your project root",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="Port (default: 8000)")
    parser.add_argument("--cwd", default=None, help="Project directory (default: current dir)")
    parser.add_argument(
        "--no-browser", action="store_true", help="Don't open browser automatically"
    )
    args = parser.parse_args()

    cwd = os.path.abspath(args.cwd or os.getcwd())
    if not os.path.isdir(cwd):
        print(f"error: directory not found: {cwd}", file=sys.stderr)
        sys.exit(1)

    os.environ["PYTEST_WEB_HOST"] = args.host
    os.environ["PYTEST_WEB_PORT"] = str(args.port)
    os.environ["PYTEST_WEB_CWD"] = cwd

    url = f"http://{args.host}:{args.port}"
    print(f"\n  pytest-web  →  {url}   (Ctrl+C to stop)\n")

    if not args.no_browser:

        def _open_browser():
            time.sleep(1.0)
            webbrowser.open(url)

        threading.Thread(target=_open_browser, daemon=True).start()

    try:
        import uvicorn

        uvicorn.run(
            "pytest_web.server:app",
            host=args.host,
            port=args.port,
            log_level="warning",
        )
    except KeyboardInterrupt:
        print("\n  Stopped.\n")
