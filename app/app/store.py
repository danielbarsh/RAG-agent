"""
State: files in blob storage, jobs and proposals in Table Storage, work in a
storage queue.

Two ideas do most of the work here.

Proposals (2.4, 2.6). The model never performs a mutation. It can only write a
proposal row naming exactly one resolved file. A proposal becomes a job only when
the signed-in user POSTs its id to /api/jobs/confirm, and only if that user holds
the FileAdmin role and owns the proposal. Text inside a PDF can therefore, at
absolute worst, cause a confirmation card to appear that a human has to read and
click.

Jobs (2.3, 2.7). A job is a row in Table Storage plus a message in a storage
queue. The row is the truth and outlives the browser session; the queue is the
delivery mechanism. Claiming a job is an optimistic ETag transition from queued
to running, which makes duplicate delivery harmless.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from urllib.parse import quote

from azure.core import MatchConditions
from azure.core.exceptions import ResourceModifiedError, ResourceNotFoundError
from azure.storage.blob import BlobSasPermissions, generate_blob_sas

from . import clients, config


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


# --- files -------------------------------------------------------------------

def documents_container():
    return clients.blob_service().get_container_client(config.DOCUMENTS_CONTAINER)


def staging_container():
    return clients.blob_service().get_container_client(config.STAGING_CONTAINER)


def source_path(name: str) -> str:
    """The value Azure AI Search stores in metadata_storage_path for this blob."""
    return f"{config.BLOB_ENDPOINT}/{config.DOCUMENTS_CONTAINER}/{quote(name, safe='/')}"


@dataclass
class DocumentFile:
    name: str
    size: int
    last_modified: str

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "size": self.size,
            "last_modified": self.last_modified,
            "source_path": source_path(self.name),
        }


def list_documents(prefix: str | None = None) -> list[DocumentFile]:
    out: list[DocumentFile] = []
    for blob in documents_container().list_blobs(name_starts_with=prefix):
        out.append(DocumentFile(
            name=blob.name,
            size=blob.size or 0,
            last_modified=blob.last_modified.isoformat() if blob.last_modified else "",
        ))
    return sorted(out, key=lambda f: f.name.lower())


def _score(reference: str, name: str) -> float:
    """
    Deliberately dumb and explainable. Anything cleverer here would make "which
    file did it pick and why" impossible to answer at a review.
    """
    ref = reference.strip().lower()
    low = name.lower()
    if ref == low:
        return 1.0
    if ref == low.rsplit(".", 1)[0]:
        return 0.98
    if ref in low:
        return 0.85
    ref_tokens = {t for t in ref.replace("_", " ").replace("-", " ").split() if len(t) > 2}
    name_tokens = {t for t in low.replace("_", " ").replace("-", " ").replace(".", " ").split() if len(t) > 2}
    if not ref_tokens:
        return 0.0
    overlap = len(ref_tokens & name_tokens) / len(ref_tokens)
    return overlap * 0.8


def resolve_file(reference: str, threshold: float = 0.5) -> dict:
    """
    2.5. Returns exactly one of:
      {"status": "resolved",  "file": {...}}
      {"status": "ambiguous", "candidates": [...]}   -> agent must ask
      {"status": "not_found", "candidates": [...]}   -> agent must say so
    A near-exact match beats a pile of weak ones; several comparable matches are
    reported as ambiguous rather than guessed at.
    """
    files = list_documents()
    if not files:
        return {"status": "not_found", "candidates": []}

    scored = sorted(((_score(reference, f.name), f) for f in files),
                    key=lambda p: p[0], reverse=True)
    best_score, best = scored[0]

    if best_score < threshold:
        return {"status": "not_found",
                "candidates": [f.as_dict() for _, f in scored[:5]]}

    close = [f for s, f in scored if s >= best_score - 0.05]
    if len(close) > 1:
        return {"status": "ambiguous", "candidates": [f.as_dict() for f in close[:8]]}

    return {"status": "resolved", "file": best.as_dict()}


def upload_sas(blob_name: str) -> str:
    """
    2.11. A user-delegation SAS signed with the app's managed identity, scoped to
    one blob in the staging container for 20 minutes, write-only. The browser
    PUTs the bytes straight to storage, so a 50 MB PDF never crosses the API
    container and there is no request-body limit to raise.
    """
    delegation_key = clients.blob_service().get_user_delegation_key(
        key_start_time=datetime.now(timezone.utc) - timedelta(minutes=5),
        key_expiry_time=datetime.now(timezone.utc) + timedelta(minutes=config.UPLOAD_SAS_MINUTES + 5),
    )
    token = generate_blob_sas(
        account_name=config.STORAGE_ACCOUNT,
        container_name=config.STAGING_CONTAINER,
        blob_name=blob_name,
        user_delegation_key=delegation_key,
        permission=BlobSasPermissions(create=True, write=True),
        expiry=datetime.now(timezone.utc) + timedelta(minutes=config.UPLOAD_SAS_MINUTES),
    )
    return f"{config.BLOB_ENDPOINT}/{config.STAGING_CONTAINER}/{quote(blob_name)}?{token}"


# --- proposals ---------------------------------------------------------------

def create_proposal(user_id: str, session_id: str, operation: str, payload: dict) -> dict:
    proposal = {
        "PartitionKey": user_id,
        "RowKey": new_id("prop"),
        "session_id": session_id,
        "operation": operation,
        "payload": json.dumps(payload),
        "created_at": now(),
        "state": "pending",
    }
    clients.table(config.PROPOSALS_TABLE).create_entity(proposal)
    return {
        "proposal_id": proposal["RowKey"],
        "operation": operation,
        "created_at": proposal["created_at"],
        **payload,
    }


def take_proposal(user_id: str, proposal_id: str) -> dict | None:
    """Fetch and mark used. A proposal is single-use, which kills replay."""
    tbl = clients.table(config.PROPOSALS_TABLE)
    try:
        entity = tbl.get_entity(user_id, proposal_id)
    except ResourceNotFoundError:
        return None
    if entity.get("state") != "pending":
        return None
    entity["state"] = "used"
    try:
        tbl.update_entity(entity, mode="merge", etag=entity.metadata["etag"],
                          match_condition=MatchConditions.IfNotModified)
    except ResourceModifiedError:
        return None
    return {
        "proposal_id": proposal_id,
        "session_id": entity.get("session_id", ""),
        "operation": entity["operation"],
        "payload": json.loads(entity["payload"]),
    }


# --- jobs --------------------------------------------------------------------

def create_job(user_id: str, user_name: str, session_id: str,
               operation: str, payload: dict) -> dict:
    job_id = new_id("job")
    entity = {
        "PartitionKey": session_id,
        "RowKey": job_id,
        "user_id": user_id,
        "user_name": user_name,
        "operation": operation,
        "payload": json.dumps(payload),
        "status": "queued",
        "attempts": 0,
        "created_at": now(),
        "updated_at": now(),
        "steps": json.dumps([]),
        "result": "",
    }
    clients.table(config.JOBS_TABLE).create_entity(entity)
    clients.queue(config.JOBS_QUEUE).send_message(
        json.dumps({"job_id": job_id, "session_id": session_id})
    )
    return job_view(entity)


def job_view(entity: Any) -> dict:
    return {
        "job_id": entity["RowKey"],
        "session_id": entity["PartitionKey"],
        "operation": entity["operation"],
        "status": entity["status"],
        "attempts": entity.get("attempts", 0),
        "created_at": entity.get("created_at"),
        "updated_at": entity.get("updated_at"),
        "payload": json.loads(entity.get("payload") or "{}"),
        "steps": json.loads(entity.get("steps") or "[]"),
        "result": entity.get("result") or "",
    }


def get_job(session_id: str, job_id: str) -> dict | None:
    try:
        return job_view(clients.table(config.JOBS_TABLE).get_entity(session_id, job_id))
    except ResourceNotFoundError:
        return None


def list_jobs(session_id: str, limit: int = 25) -> list[dict]:
    tbl = clients.table(config.JOBS_TABLE)
    rows = tbl.query_entities(f"PartitionKey eq '{session_id}'")
    jobs = [job_view(r) for r in rows]
    jobs.sort(key=lambda j: j["created_at"], reverse=True)
    return jobs[:limit]


def claim_job(session_id: str, job_id: str) -> dict | None:
    """
    2.7. queued -> running, guarded by the entity ETag. If a duplicate delivery
    or a second worker gets here first the transition fails and this returns
    None, so the message is simply acknowledged and dropped.
    """
    tbl = clients.table(config.JOBS_TABLE)
    try:
        entity = tbl.get_entity(session_id, job_id)
    except ResourceNotFoundError:
        return None

    if entity["status"] not in ("queued", "retrying"):
        return None

    entity["status"] = "running"
    entity["attempts"] = int(entity.get("attempts", 0)) + 1
    entity["updated_at"] = now()
    try:
        tbl.update_entity(entity, mode="merge", etag=entity.metadata["etag"],
                          match_condition=MatchConditions.IfNotModified)
    except ResourceModifiedError:
        return None
    return job_view(entity)


def append_step(session_id: str, job_id: str, message: str) -> None:
    tbl = clients.table(config.JOBS_TABLE)
    entity = tbl.get_entity(session_id, job_id)
    steps = json.loads(entity.get("steps") or "[]")
    steps.append({"at": now(), "message": message})
    entity["steps"] = json.dumps(steps[-40:])
    entity["updated_at"] = now()
    tbl.update_entity(entity, mode="merge")


def finish_job(session_id: str, job_id: str, status: str, result: str) -> None:
    tbl = clients.table(config.JOBS_TABLE)
    entity = tbl.get_entity(session_id, job_id)
    entity["status"] = status
    entity["result"] = result[:8000]
    entity["updated_at"] = now()
    tbl.update_entity(entity, mode="merge")


def mark_retrying(session_id: str, job_id: str, error: str) -> None:
    tbl = clients.table(config.JOBS_TABLE)
    entity = tbl.get_entity(session_id, job_id)
    entity["status"] = "retrying"
    entity["result"] = error[:2000]
    entity["updated_at"] = now()
    tbl.update_entity(entity, mode="merge")


# --- sessions ----------------------------------------------------------------

def load_session(user_id: str, session_id: str) -> list[dict]:
    try:
        entity = clients.table(config.SESSIONS_TABLE).get_entity(user_id, session_id)
    except ResourceNotFoundError:
        return []
    return json.loads(entity.get("messages") or "[]")


def save_session(user_id: str, session_id: str, messages: list[dict]) -> None:
    trimmed = messages[-config.MAX_HISTORY_MESSAGES:]
    clients.table(config.SESSIONS_TABLE).upsert_entity({
        "PartitionKey": user_id,
        "RowKey": session_id,
        "messages": json.dumps(trimmed)[:60000],
        "updated_at": now(),
    })


def list_sessions(user_id: str, limit: int = 20) -> list[dict]:
    """
    The table also holds upload: rows (see create_upload), which share
    PartitionKey but have no updated_at. $select fills that property as None
    rather than omitting it, so those rows must be dropped before sorting or a
    None-vs-str comparison blows up the sort.
    """
    rows = clients.table(config.SESSIONS_TABLE).query_entities(
        f"PartitionKey eq '{user_id}'", select=["RowKey", "updated_at"])
    sessions = [{"session_id": r["RowKey"], "updated_at": r.get("updated_at") or ""}
                for r in rows if not str(r["RowKey"]).startswith("upload:")]
    sessions.sort(key=lambda s: s["updated_at"], reverse=True)
    return sessions[:limit]


# --- uploads -----------------------------------------------------------------
# An upload record is written when the SAS is issued, so the model only ever
# sees upload ids the server itself minted for this user and session.

def create_upload(user_id: str, session_id: str, filename: str, size: int) -> dict:
    upload_id = new_id("up")
    blob = f"{user_id[:8]}/{upload_id}/{filename}"
    clients.table(config.SESSIONS_TABLE).create_entity({
        "PartitionKey": user_id,
        "RowKey": f"upload:{upload_id}",
        "session_id": session_id,
        "upload_id": upload_id,
        "filename": filename,
        "size": int(size),
        "blob": blob,
        "created_at": now(),
    })
    return {"upload_id": upload_id, "filename": filename, "size": int(size), "blob": blob}


def list_uploads(user_id: str, session_id: str) -> dict[str, dict]:
    rows = clients.table(config.SESSIONS_TABLE).query_entities(
        f"PartitionKey eq '{user_id}' and session_id eq '{session_id}'")
    uploads: dict[str, dict] = {}
    for row in rows:
        if not str(row["RowKey"]).startswith("upload:"):
            continue
        uploads[row["upload_id"]] = {
            "upload_id": row["upload_id"],
            "filename": row["filename"],
            "size": int(row.get("size", 0)),
            "blob": row["blob"],
        }
    return uploads


def read_sas(container: str, blob_name: str, minutes: int = 15) -> str:
    """Short-lived read SAS used as the source URL for server-side blob copy."""
    delegation_key = clients.blob_service().get_user_delegation_key(
        key_start_time=datetime.now(timezone.utc) - timedelta(minutes=5),
        key_expiry_time=datetime.now(timezone.utc) + timedelta(minutes=minutes + 5),
    )
    token = generate_blob_sas(
        account_name=config.STORAGE_ACCOUNT,
        container_name=container,
        blob_name=blob_name,
        user_delegation_key=delegation_key,
        permission=BlobSasPermissions(read=True),
        expiry=datetime.now(timezone.utc) + timedelta(minutes=minutes),
    )
    return f"{config.BLOB_ENDPOINT}/{container}/{quote(blob_name)}?{token}"
