from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time
import webbrowser

import uvicorn

from . import __version__


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Run the Hello ShinrAI local workbench.")
    value.add_argument(
        "--port", type=int, default=int(os.getenv("HELLO_SHINRAI_PORT", "8765")), help="Loopback port (default: 8765)"
    )
    value.add_argument("--no-browser", action="store_true", help="Do not open a browser automatically")
    value.add_argument("--version", action="version", version=__version__)
    return value


def port_available(port: int) -> bool:
    if not 1 <= port <= 65535:
        return False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def open_browser(url: str) -> None:
    time.sleep(0.7)
    try:
        opened = webbrowser.open(url, new=2)
    except (OSError, webbrowser.Error):
        opened = False
    if not opened:
        print(f"Open {url} in your browser.", file=sys.stderr)


def main(argv: list[str] | None = None):
    args = parser().parse_args(argv)
    if not port_available(args.port):
        parser().error(f"port {args.port} is unavailable; choose another with --port")
    url = f"http://127.0.0.1:{args.port}/"
    if not args.no_browser:
        threading.Thread(target=open_browser, args=(url,), daemon=True).start()
    print(
        f"Hello ShinrAI is running at {url}\nPress Ctrl+C to stop it. Keys and conversation data stay in this process."
    )
    host = os.getenv("HELLO_SHINRAI_BIND", "127.0.0.1")
    if host not in {"127.0.0.1", "0.0.0.0"}:
        parser().error("HELLO_SHINRAI_BIND must be 127.0.0.1 or 0.0.0.0")
    uvicorn.run("hello_shinrai.app:app", host=host, port=args.port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
