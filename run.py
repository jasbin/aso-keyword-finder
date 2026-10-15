#!/usr/bin/env python3
"""Run with `python3 run.py`; use --help for launcher options."""

from __future__ import annotations

import argparse
import errno
import importlib.util
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
SOURCE = ROOT / "src"
RUNTIME_MODULES = ("fastapi", "httpx", "pydantic", "pydantic_settings", "rich", "typer", "uvicorn")


def prepare_environment() -> None:
    """Reuse the project environment and always import the working source."""
    python = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.is_file():
        print("Creating the project virtual environment…", flush=True)
        subprocess.run([sys.executable, "-m", "venv", str(VENV)], check=True)  # noqa: S603
    if Path(sys.prefix).resolve() != VENV.resolve():
        os.execv(str(python), [str(python), str(ROOT / "run.py"), *sys.argv[1:]])  # noqa: S606
    if any(importlib.util.find_spec(module) is None for module in RUNTIME_MODULES):
        print("Installing app dependencies (first run only)…", flush=True)
        subprocess.run(  # noqa: S603
            [str(python), "-m", "pip", "install", "--editable", str(ROOT)], check=True
        )
    sys.path.insert(0, str(SOURCE))
    os.chdir(ROOT)


def available_port(host: str, port: int, *, fallback: bool) -> int:
    """Keep an existing server running; try the next ports when none was specified."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    for candidate in range(port, min(port + (20 if fallback else 1), 65536)):
        try:
            with socket.socket(family, socket.SOCK_STREAM) as probe:
                probe.bind((host, candidate))
            return candidate
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE:
                raise
    raise OSError(f"Port {port} is in use. Choose another with --port PORT.")


def open_when_ready(url: str) -> None:
    """Wait for the app before opening its page."""
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            with urlopen(f"{url}/healthz", timeout=1) as response:  # noqa: S310
                if response.status == 200:
                    webbrowser.open(url)
                    return
        except OSError:
            pass
        time.sleep(0.2)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Start ASO Keyword Finder directly from this folder, without Docker.",
        epilog="Missing dependencies are installed into .venv automatically. Stop with Ctrl+C.",
    )
    parser.add_argument("--host", help="Bind address (default: app settings, or 127.0.0.1)")
    parser.add_argument("--port", type=int, help="Port (default: app settings, or the next free port)")
    parser.add_argument("--reload", action="store_true", help="Restart when Python source changes")
    parser.add_argument("--no-browser", action="store_true", help="Start without opening a browser")
    args = parser.parse_args()
    # This entry point also runs before package installation can check the Python version.
    if sys.version_info < (3, 10):  # noqa: UP036
        parser.error("Python 3.10 or newer is required.")
    if args.port is not None and not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535.")

    prepare_environment()

    import uvicorn

    from keyfinder.config import get_settings

    settings = get_settings()
    host = args.host if args.host is not None else settings.host
    requested_port = args.port if args.port is not None else settings.port
    port = available_port(host, requested_port, fallback=args.port is None)
    if port != requested_port:
        print(f"Port {requested_port} is busy; using {port} instead.", flush=True)
    browser_host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)  # noqa: S104
    if ":" in browser_host:
        browser_host = f"[{browser_host}]"
    url = f"http://{browser_host}:{port}"
    print(f"\nASO Keyword Finder: {url}\nPress Ctrl+C to stop.\n", flush=True)
    if not args.no_browser:
        threading.Thread(target=open_when_ready, args=(url,), daemon=True).start()
    uvicorn.run(
        "keyfinder.api:get_app",
        factory=True,
        host=host,
        port=port,
        reload=args.reload,
        reload_dirs=[str(SOURCE)] if args.reload else None,
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"Could not start ASO Keyword Finder: {error}", file=sys.stderr)
        sys.exit(1)
