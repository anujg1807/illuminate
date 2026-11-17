"""The web page and the small JSON API behind it."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import json
import os
from urllib.parse import unquote

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

from . import scoring, timing
from .resume import ResumeError, extract_text
from .service import Service
from .store import Store

STATIC = Path(__file__).parent / "static"


def _overlap(row: dict) -> dict | None:
    if row["score_status"] is None:
        return None
    detail = json.loads(row["score_detail"])
    if row["score_status"] == "failed":
        return {"status": "failed", "error": detail.get("error", "")}
    return {"status": "done", "score": round(row["score"]), **detail}
LABELS = ("match", "maybe", "no")


def save_env_value(path: Path, name: str, value: str) -> None:
    """Set NAME=value in the .env file, keeping its other lines; readable only by you."""
    lines = path.read_text().splitlines() if path.exists() else []
    lines = [line for line in lines if not line.strip().startswith(f"{name}=")]
    lines.append(f"{name}={value}")
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o600)


def create_app(
    service: Service, store: Store, run_scheduler: bool = True,
    env_path: Path = Path(".env"), check_key=None,
) -> FastAPI:
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
                    "logo_url": row["logo_url"],
                    # Resume Overlap: None until scored; then done or failed.
                    "overlap": _overlap(row),
                }
                for row in rows
            ],
        }

    @app.get("/api/status")
    def status() -> dict:
        return service.status()

    @app.post("/api/resume")
    async def upload_resume(request: Request):
        """The file is sent as the request body, its name in the X-Filename header."""
        filename = unquote(request.headers.get("x-filename", "resume"))
        try:
            text = extract_text(filename, await request.body())
        except ResumeError as error:
            return JSONResponse({"error": str(error)}, status_code=400)
        store.save_resume(filename, text)
        service.resume_changed()
        return {"filename": filename, "characters": len(text)}

    @app.delete("/api/resume")
    def remove_resume() -> dict:
        store.remove_resume()
        service.resume_changed()
        return {"removed": True}

    @app.post("/api/key")
    async def save_key(request: Request):
        """Save the Anthropic API key to .env (git ignores it) after checking it works."""
        key = str((await request.json()).get("key", "")).strip()
        if not key.startswith("sk-ant-"):
            return JSONResponse({"error": "That does not look like an Anthropic API key (it starts with sk-ant-)."}, status_code=400)
        try:
            (check_key or scoring.check_api_key)(key)
        except scoring.ScoringUnavailable as error:
            return JSONResponse({"error": str(error)}, status_code=400)
        save_env_value(env_path, "ANTHROPIC_API_KEY", key)
        os.environ["ANTHROPIC_API_KEY"] = key
        service.scorer.reset()
        service.resume_changed()
        return {"saved": True}

    @app.post("/api/refresh")
    def refresh() -> dict:
        return {"started": service.trigger()}

    return app
