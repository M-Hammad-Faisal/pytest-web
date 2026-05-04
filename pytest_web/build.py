"""
pytest-web-build  —  lint → format → build → verify

Run with:
    pytest-web-build
or:
    python -m pytest_web.build
"""

import subprocess
import sys


def run(cmd: list[str], label: str) -> None:
    print(f"\n  ▶  {label}")
    result = subprocess.run(cmd, capture_output=False)
    if result.returncode != 0:
        _fail(label)
        sys.exit(result.returncode)


def _say(msg: str) -> None:
    try:
        import cowsay

        cowsay.cow(msg)
    except ImportError:
        print(msg)


def _fail(step: str) -> None:
    _say(f"Build failed at: {step}\nFix the errors above and try again.")


def main() -> None:
    _say("Starting pytest-web build pipeline...")

    run([sys.executable, "-m", "ruff", "check", "."], "Lint (ruff check)")
    run([sys.executable, "-m", "ruff", "format", "--check", "."], "Format check (ruff format)")
    run([sys.executable, "-m", "build"], "Build sdist + wheel")
    run([sys.executable, "-m", "twine", "check", "dist/*"], "Verify dist (twine check)")

    _say("Build passed! Lint clean, format clean, dist verified.")


if __name__ == "__main__":
    main()
