"""The web page and the small JSON API behind it."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from . import timing
from .service import Service
from .store import Store

STATIC = Path(__file__).parent / "static"
LABELS = ("match", "maybe", "no")


def create_app(service: Service, store: Store, run_scheduler: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if run_scheduler:
            service.start()
        yield
        service.stop()

    app = FastAPI(title="Illuminate", lifespan=lifespan, docs_url=None, redoc_url=None)

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/api/jobs")
    def jobs(labels: str = "match,maybe") -> dict:
        wanted = [label for label in labels.split(",") if label in LABELS]
        rows = store.jobs(wanted)
        return {
            "version": store.version,
            "jobs": [
                {
                    "id": row["id"],
                    "title": row["title"],
                    "company": row["company"],
                    "location": row["location"],
                    "url": row["url"],
                    "label": row["label"],
                    "reason": row["reason"],
                    "posted_at": row["posted_at"],
                    # Half-width of the uncertainty, in seconds.
                    "uncertainty": (row["posted_hi"] - row["posted_lo"]) / 2,
                    "precise": timing.is_precise((row["posted_lo"], row["posted_hi"])),
                    "first_seen_at": row["first_seen_at"],
                }
                for row in rows
            ],
        }

    @app.get("/api/status")
    def status() -> dict:
        return service.status()

    @app.post("/api/refresh")
    def refresh() -> dict:
        return {"started": service.trigger()}

    return app
