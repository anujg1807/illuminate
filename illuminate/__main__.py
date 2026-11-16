"""Start Illuminate: `uv run illuminate`."""

from __future__ import annotations

import argparse
import logging
import threading
import webbrowser

import uvicorn

from . import config as config_module
from .classifier import classify
from .service import Service
from .store import Store
from .web import create_app


def main() -> None:
    parser = argparse.ArgumentParser(prog="illuminate", description=__doc__)
    parser.add_argument("--config", default="config.toml", help="settings file (default: config.toml)")
    parser.add_argument("--port", type=int, help="port for the web page (default: from config, 8000)")
    parser.add_argument("--no-browser", action="store_true", help="do not open the page automatically")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
    config = config_module.load(args.config)
    port = args.port or config.port

    store = Store(config.database)
    store.reclassify(classify)
    service = Service(config, store)
    app = create_app(service, store)

    url = f"http://localhost:{port}"
    logging.getLogger("illuminate").info("Illuminate is running at %s  (Ctrl+C to stop)", url)
    if not args.no_browser:
        threading.Timer(1.5, webbrowser.open, args=(url,)).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
