"""FastAPI dashboard backend for Git-Blast Live (SPEC.md "server.py").

Endpoints:

* ``GET  /api/graph``  — React Flow nodes + edges from the code graph, with
  modified state.
* ``GET  /api/status`` — repo info + live WebSocket connection count.
* ``POST /api/blast``  — run the full pipeline, streaming WebSocket events.
* ``WS   /ws``         — ``blast_started`` → ``surface_detected`` →
  ``db_query`` (querying, then complete) → ``test_result``.

The built React app in ``static/`` is served at ``/`` when present.

Run it::

    GIT_BLAST_REPO_ROOT=/path GIT_BLAST_REPO_ID=myrepo \\
        python -m uvicorn src.server:app --port 8000
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import parser, runner
from .db import get_db
from .parser import SnapshotError

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

DEFAULT_REPO_ID = "main-repo"


@dataclass
class Settings:
    repo_root: str
    repo_id: str
    db_factory: Callable[[], object]

    @classmethod
    def from_env(cls, repo_root: str | None = None, repo_id: str | None = None,
                db_factory: Callable[[], object] | None = None) -> "Settings":
        return cls(
            repo_root=repo_root or os.environ.get("GIT_BLAST_REPO_ROOT", "."),
            repo_id=repo_id or os.environ.get("GIT_BLAST_REPO_ID", DEFAULT_REPO_ID),
            db_factory=db_factory or get_db,
        )


class ConnectionManager:
    """Tracks live WebSocket clients and broadcasts JSON events to all of them."""

    def __init__(self) -> None:
        self.active: list[WebSocket] = []
        self._lock = asyncio.Lock()

    @property
    def count(self) -> int:
        return len(self.active)

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self.active.append(ws)

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            if ws in self.active:
                self.active.remove(ws)

    async def broadcast(self, event: dict) -> None:
        async with self._lock:
            targets = list(self.active)
        dead: list[WebSocket] = []
        for ws in targets:
            try:
                await ws.send_json(event)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    if ws in self.active:
                        self.active.remove(ws)


def create_app(
    repo_root: str | None = None,
    repo_id: str | None = None,
    *,
    db_factory: Callable[[], object] | None = None,
    static_dir: str | Path | None = None,
) -> FastAPI:
    settings = Settings.from_env(repo_root, repo_id, db_factory)
    static_path = Path(static_dir) if static_dir is not None else STATIC_DIR
    app = FastAPI(title="Git-Blast Live")
    manager = ConnectionManager()
    app.state.settings = settings
    app.state.manager = manager

    def _open_db():
        return settings.db_factory()

    def _close_db(db) -> None:
        # Never close a caller-injected shared instance (tests / embedding).
        if settings.db_factory is get_db:
            db.close()

    @app.get("/api/status")
    async def api_status() -> dict:
        status: dict = {
            "repo_root": settings.repo_root,
            "repo_id": settings.repo_id,
            "connections": manager.count,
            "entire_available": _entire_available(),
        }
        try:
            db = _open_db()
            try:
                status["mappings"] = len(db.all_mappings(settings.repo_id))
                status["db_backend"] = type(db).__name__
            finally:
                _close_db(db)
        except Exception as exc:  # pragma: no cover - defensive
            status["db_error"] = str(exc)
        return status

    @app.get("/api/graph")
    async def api_graph():
        try:
            graph, modified = await run_in_threadpool(_load_graph, settings.repo_root)
        except SnapshotError as exc:
            return JSONResponse(status_code=503, content={"error": str(exc)})
        surface = parser.get_modified_import_surface(
            modified_files=modified, graph=graph, max_depth=0
        )
        dash = parser.get_graph_for_dashboard(
            graph=graph, modified_files=modified, import_surface=surface
        )
        return {
            **dash,
            "modified_files": modified,
            "import_surface": surface,
            "repo_id": settings.repo_id,
            "repo_root": settings.repo_root,
        }

    @app.post("/api/blast")
    async def api_blast():
        loop = asyncio.get_running_loop()

        def progress(event: str, payload: dict) -> None:
            message = {"type": event, **payload}
            fut = asyncio.run_coroutine_threadsafe(manager.broadcast(message), loop)
            try:
                fut.result(timeout=5)
            except Exception:  # pragma: no cover - broadcast is best-effort
                pass

        db = _open_db()
        try:
            result = await run_in_threadpool(
                runner.run_impact_analysis,
                settings.repo_root,
                settings.repo_id,
                db=db,
                progress=progress,
            )
        except SnapshotError as exc:
            await manager.broadcast({"type": "error", "error": str(exc)})
            return JSONResponse(status_code=503, content={"error": str(exc)})
        finally:
            _close_db(db)
        return result

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        await manager.connect(ws)
        try:
            await ws.send_json({"type": "connected", "repo_id": settings.repo_id})
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            await manager.disconnect(ws)

    if static_path.is_dir() and (static_path / "index.html").is_file():
        app.mount("/", StaticFiles(directory=str(static_path), html=True), name="static")
    else:
        @app.get("/")
        async def index() -> dict:
            return {
                "name": "Git-Blast Live",
                "dashboard": "run `cd frontend && npm install && npx vite build`",
                "endpoints": ["/api/status", "/api/graph", "/api/blast", "/ws"],
            }

    return app


def _entire_available() -> bool:
    from shutil import which

    return which(parser.DEFAULT_ENTIRE_BIN) is not None


def _load_graph(repo_root: str):
    graph = parser.build_graph(repo_root, worktree=True)
    modified = parser.get_modified_files(repo_root)
    return graph, modified


app = create_app()
