"""
HTTP surface.

Authentication is handled in front of this process by Container Apps built-in
authentication. By the time a request reaches here it has already been validated
against Entra ID, and the principal arrives in X-MS-CLIENT-PRINCIPAL. This
process never sees a token, never stores one, and there is nothing to leak into
the browser bundle.

Authorisation is ours: signing in gets you read access to the library and the
agent; the `FileAdmin` app role is what lets you confirm a job that changes a
file. Nothing in the request body can grant it.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from . import agent, config, search, store

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("api")

app = FastAPI(title="Document library agent", docs_url=None, redoc_url=None)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


# --- identity ----------------------------------------------------------------

class User(BaseModel):
    user_id: str
    name: str
    is_admin: bool


def current_user(request: Request) -> User:
    if not config.AUTH_ENABLED:
        return User(user_id="dev-user", name="Local developer", is_admin=True)

    encoded = request.headers.get("x-ms-client-principal")
    if not encoded:
        raise HTTPException(status_code=401, detail="Not signed in.")

    try:
        principal = json.loads(base64.b64decode(encoded))
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=401, detail="Malformed sign-in header.")

    claims = {c.get("typ"): c.get("val") for c in principal.get("claims", [])}
    roles = [c.get("val") for c in principal.get("claims", []) if c.get("typ") == "roles"]

    user_id = (claims.get("http://schemas.microsoft.com/identity/claims/objectidentifier")
               or claims.get("oid")
               or request.headers.get("x-ms-client-principal-id", "unknown"))
    name = (claims.get("name")
            or claims.get("preferred_username")
            or request.headers.get("x-ms-client-principal-name", "unknown"))

    return User(user_id=user_id, name=name, is_admin=config.ADMIN_ROLE in roles)


def require_admin(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(
            status_code=403,
            detail=f"You need the {config.ADMIN_ROLE} role to change the library.")
    return user


# --- request models (2.6: every tool argument crosses a validator) -----------

class ChatRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=8000)

    @field_validator("session_id")
    @classmethod
    def clean_session(cls, v: str) -> str:
        if not all(ch.isalnum() or ch in "-_" for ch in v):
            raise ValueError("session_id must be alphanumeric, dash or underscore")
        return v


class UploadRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=64)
    filename: str = Field(min_length=1, max_length=200)
    size: int = Field(ge=1, le=config.MAX_UPLOAD_BYTES)

    @field_validator("filename")
    @classmethod
    def clean_filename(cls, v: str) -> str:
        name = os.path.basename(v.replace("\\", "/")).strip()
        if not name.lower().endswith(".pdf"):
            raise ValueError("only .pdf files are accepted")
        if any(ch in name for ch in ('"', "'", "\n", "\r", "\t")):
            raise ValueError("invalid characters in filename")
        return name


class ConfirmRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=64)
    proposal_id: str = Field(min_length=1, max_length=64)


# --- endpoints ---------------------------------------------------------------

@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "role": config.ROLE}


@app.get("/api/me")
def me(user: User = Depends(current_user)) -> dict:
    return {"name": user.name, "is_admin": user.is_admin, "admin_role": config.ADMIN_ROLE}


@app.get("/api/files")
def files(user: User = Depends(current_user)) -> dict:
    return {"files": [f.as_dict() for f in store.list_documents()]}


@app.post("/api/uploads/sas")
def uploads_sas(body: UploadRequest, user: User = Depends(require_admin)) -> dict:
    record = store.create_upload(user.user_id, body.session_id, body.filename, body.size)
    return {**record, "upload_url": store.upload_sas(record["blob"])}


@app.post("/api/chat")
def chat(body: ChatRequest, request: Request, user: User = Depends(current_user)):
    history = store.load_session(user.user_id, body.session_id)
    uploads = store.list_uploads(user.user_id, body.session_id)

    def generate():
        try:
            yield from agent.stream_turn(
                user_id=user.user_id,
                user_name=user.name,
                session_id=body.session_id,
                is_admin=user.is_admin,
                history=history,
                user_message=body.message,
                uploads=uploads,
            )
        finally:
            # History is persisted even if the browser disconnects mid-stream, so
            # a refresh shows the turn that actually happened.
            store.save_session(user.user_id, body.session_id, history)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/jobs/confirm")
def confirm(body: ConfirmRequest, user: User = Depends(require_admin)) -> dict:
    proposal = store.take_proposal(user.user_id, body.proposal_id)
    if not proposal:
        raise HTTPException(status_code=404,
                            detail="That proposal is unknown, already used, or not yours.")
    job = store.create_job(user.user_id, user.name, body.session_id,
                           proposal["operation"], proposal["payload"])
    log.info("job %s queued: %s %s by %s",
             job["job_id"], proposal["operation"],
             proposal["payload"].get("target_name"), user.name)
    return job


@app.get("/api/jobs")
def jobs(session_id: str, user: User = Depends(current_user)) -> dict:
    return {"jobs": store.list_jobs(session_id)}


@app.get("/api/jobs/{session_id}/{job_id}")
def job(session_id: str, job_id: str, user: User = Depends(current_user)) -> dict:
    found = store.get_job(session_id, job_id)
    if not found:
        raise HTTPException(status_code=404, detail="No such job.")
    return found


@app.get("/api/sessions")
def sessions(user: User = Depends(current_user)) -> dict:
    return {"sessions": store.list_sessions(user.user_id)}


@app.get("/api/session/{session_id}")
def session_history(session_id: str, user: User = Depends(current_user)) -> dict:
    return {"messages": store.load_session(user.user_id, session_id)}


@app.get("/api/indexer/status")
def indexer_status(user: User = Depends(current_user)) -> dict:
    return search.indexer_status()


@app.get("/api/stats")
def stats(user: User = Depends(current_user)) -> dict:
    return {
        "files": len(store.list_documents()),
        "chunks": search.document_count(),
        "search_sku": config.SEARCH_SKU,
        "semantic_ranker": config.SEMANTIC_RANKER,
    }


@app.post("/api/admin/run-indexer")
def admin_run(user: User = Depends(require_admin)) -> dict:
    return {"result": search.run_indexer()}


@app.post("/api/admin/backfill")
def admin_backfill(user: User = Depends(require_admin)) -> dict:
    """1.7: reset change-detection state, then run. Everything is re-read."""
    search.reset_indexer()
    return {"result": search.run_indexer(), "mode": "full-backfill"}


@app.exception_handler(HTTPException)
def http_error(request: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})


# --- static front end --------------------------------------------------------

if WEB_DIR.exists():
    app.mount("/assets", StaticFiles(directory=str(WEB_DIR)), name="assets")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(str(WEB_DIR / "index.html"))
