"""
Everything this system does against Azure AI Search.

Two planes:
  - data plane   : hybrid query, chunk lookup by source, chunk delete by key
  - control plane: run / reset the indexer, read its execution history

Both authenticate with Entra ID; the service has API keys disabled.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from . import clients, config

log = logging.getLogger(__name__)

_AV = config.SEARCH_API_VERSION

# Azure AI Search is eventually consistent: a delete accepted by delete_chunks
# is not guaranteed to be reflected in the very next query. This is how long
# purge_source waits before re-querying to verify remaining == 0, so it isn't
# racing its own write.
_PURGE_CONSISTENCY_DELAY_SECONDS = 1.5


def _client() -> httpx.Client:
    return httpx.Client(
        base_url=config.SEARCH_ENDPOINT,
        headers={
            "Authorization": f"Bearer {clients.search_token()}",
            "Content-Type": "application/json",
        },
        timeout=60.0,
    )


# --- retrieval ---------------------------------------------------------------

def search_chunks(query: str, top: int = 6, source_path: str | None = None) -> list[dict]:
    """
    Hybrid retrieval: BM25 over the chunk text and HNSW over the chunk vector,
    fused with Reciprocal Rank Fusion, then semantically reranked when the tier
    offers it. `k` on the vector leg is deliberately larger than `top` so RRF has
    something to fuse.
    """
    body: dict[str, Any] = {
        "search": query,
        "top": top,
        "select": "chunk_id,parent_id,title,source_path,last_modified,chunk",
        "vectorQueries": [
            {
                "kind": "text",          # Search embeds the query for us
                "text": query,
                "fields": "text_vector",
                "k": max(top * 5, 30),
            }
        ],
    }
    if config.SEMANTIC_RANKER:
        body["queryType"] = "semantic"
        body["semanticConfiguration"] = "default-semantic"
    if source_path:
        body["filter"] = f"source_path eq '{source_path}'"

    with _client() as c:
        r = c.post(f"/indexes/{config.SEARCH_ALIAS}/docs/search?api-version={_AV}", json=body)
        if r.status_code == 404:
            return []
        r.raise_for_status()
        return r.json().get("value", [])


def chunk_ids_for_source(source_path: str) -> list[str]:
    """Every chunk key belonging to one blob. Paged, so a 400-page PDF is complete."""
    keys: list[str] = []
    skip = 0
    page = 1000
    with _client() as c:
        while True:
            body = {
                "search": "*",
                "filter": f"source_path eq '{source_path}'",
                "select": "chunk_id",
                "top": page,
                "skip": skip,
                "count": True,
            }
            r = c.post(f"/indexes/{config.SEARCH_INDEX}/docs/search?api-version={_AV}", json=body)
            if r.status_code == 404:
                return keys
            r.raise_for_status()
            values = r.json().get("value", [])
            keys.extend(v["chunk_id"] for v in values)
            if len(values) < page:
                return keys
            skip += page
            if skip >= 100_000:  # $skip ceiling; see LIMITATIONS.md
                return keys


def delete_chunks(chunk_ids: list[str]) -> int:
    """Delete by key in batches. Returns how many keys were submitted."""
    if not chunk_ids:
        return 0
    sent = 0
    with _client() as c:
        for i in range(0, len(chunk_ids), 500):
            batch = chunk_ids[i:i + 500]
            body = {"value": [{"@search.action": "delete", "chunk_id": k} for k in batch]}
            r = c.post(f"/indexes/{config.SEARCH_INDEX}/docs/index?api-version={_AV}", json=body)
            r.raise_for_status()
            sent += len(batch)
    return sent


def purge_source(source_path: str) -> dict:
    """
    Belt and braces for 1.5. Index projections already remove child chunks when
    the parent blob is detected as deleted, but that depends on an indexer run
    having happened. This makes deletion synchronous with the job, and then
    re-queries to prove the count is zero, which is the answer to "how are you
    certain every chunk went with it".
    """
    keys = chunk_ids_for_source(source_path)
    deleted = delete_chunks(keys)
    time.sleep(_PURGE_CONSISTENCY_DELAY_SECONDS)
    remaining = len(chunk_ids_for_source(source_path))
    return {"found": len(keys), "deleted": deleted, "remaining": remaining}


# --- indexer control ---------------------------------------------------------

def _retry_after_seconds(response: httpx.Response, default: float = 2.0) -> float:
    try:
        return max(float(response.headers.get("Retry-After", default)), 0.5)
    except ValueError:
        return default


def run_indexer() -> str:
    """
    Ask for an on-demand run. 409 means a run is already in flight, which is a
    success for our purposes: that run, or the one the schedule fires next, picks
    up the change. This is what makes the fast path safe to call on every event.

    429 shows up when a job's own explicit call to this function lands within
    the same instant as the index-events loop's call for the blob-write event
    that same job just produced - two callers asking for a run at once. It is
    throttling, not a real failure, so it is worth a couple of short, bounded
    retries before it is reported as an error.
    """
    with _client() as c:
        for attempt in range(3):
            r = c.post(f"/indexers/{config.SEARCH_INDEXER}/run?api-version={_AV}")
            if r.status_code in (202, 204):
                return "started"
            if r.status_code == 409:
                return "already-running"
            if r.status_code == 429 and attempt < 2:
                wait = _retry_after_seconds(r)
                log.info("indexer run throttled (429), retrying in %.1fs", wait)
                time.sleep(wait)
                continue
            log.warning("indexer run returned %s: %s", r.status_code, r.text)
            return f"error-{r.status_code}"


def reset_indexer() -> None:
    """Clears change-detection state so the next run is a full backfill (1.7)."""
    with _client() as c:
        r = c.post(f"/indexers/{config.SEARCH_INDEXER}/reset?api-version={_AV}")
        r.raise_for_status()


def indexer_status() -> dict:
    """Last run plus recent history: what was indexed, skipped and failed (1.9)."""
    with _client() as c:
        r = c.get(f"/indexers/{config.SEARCH_INDEXER}/status?api-version={_AV}")
        r.raise_for_status()
        data = r.json()

    def summarise(run: dict | None) -> dict | None:
        if not run:
            return None
        return {
            "status": run.get("status"),
            "startTime": run.get("startTime"),
            "endTime": run.get("endTime"),
            "itemsProcessed": run.get("itemsProcessed"),
            "itemsFailed": run.get("itemsFailed"),
            "errorMessage": run.get("errorMessage"),
            "errors": [
                {"key": e.get("key"), "name": e.get("name"), "message": e.get("errorMessage")}
                for e in (run.get("errors") or [])[:20]
            ],
            "warnings": [
                {"key": w.get("key"), "message": w.get("message")}
                for w in (run.get("warnings") or [])[:20]
            ],
        }

    return {
        "indexer": config.SEARCH_INDEXER,
        "state": data.get("status"),
        "lastResult": summarise(data.get("lastResult")),
        "history": [summarise(r) for r in (data.get("executionHistory") or [])[:10]],
    }


def document_count() -> int:
    with _client() as c:
        r = c.get(f"/indexes/{config.SEARCH_INDEX}/docs/$count?api-version={_AV}")
        if r.status_code == 404:
            return 0
        r.raise_for_status()
        return int(r.text.strip().lstrip("\ufeff"))
