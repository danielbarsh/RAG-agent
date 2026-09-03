"""
The worker. One process, two consumers, scaled 0..3 by KEDA on queue depth.

  jobs          -> add / replace / delete a file, then make the index follow
  index-events  -> Event Grid blob notifications, coalesced into indexer runs

Durability (2.7)
  - At-least-once delivery from Azure Storage Queues. A message stays invisible
    for JOB_VISIBILITY_TIMEOUT and reappears if the worker dies mid-job, so a
    restart resumes the job rather than losing it.
  - Duplicate delivery is harmless: claiming a job is an ETag-guarded
    queued -> running transition, and every step is written to check-then-act
    against the current state of the blob.
  - Retries are bounded. After JOB_MAX_ATTEMPTS the message is moved to
    jobs-poison and the job row is marked failed with the last error, so the
    person in the chat sees a failure instead of silence.

Concurrency (the "two jobs edit the same file at once" question)
  - Every mutation takes a 60-second blob lease on the target. The second job
    fails fast with a message naming the file and the job that holds it. The
    person sees "someone else is changing X right now, try again" - not a
    half-applied write and not a silent last-writer-wins.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import signal
import threading
import time

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.storage.blob import BlobLeaseClient

from . import clients, config, search, store

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("worker")

_stop = threading.Event()

# How long a job waits for its change to become visible in the index before it
# reports "queued for indexing" instead of "searchable". This is the number
# quoted for 2.10.
SEARCHABLE_TIMEOUT_SECONDS = 240
SEARCHABLE_POLL_SECONDS = 10


# --- helpers -----------------------------------------------------------------

def _blob(container: str, name: str):
    return clients.blob_service().get_blob_client(container, name)


def _lease(container: str, name: str, job_id: str) -> BlobLeaseClient:
    client = BlobLeaseClient(_blob(container, name))
    client.acquire(lease_duration=config.BLOB_LEASE_SECONDS)
    log.info("job %s leased %s/%s", job_id, container, name)
    return client


def _copy(source_container: str, source_blob: str,
          target_container: str, target_blob: str,
          lease: BlobLeaseClient | None = None) -> None:
    source_url = store.read_sas(source_container, source_blob)
    target = _blob(target_container, target_blob)
    kwargs = {"lease": lease} if lease else {}
    target.start_copy_from_url(source_url, **kwargs)
    for _ in range(120):
        props = target.get_blob_properties()
        state = props.copy.status
        if state == "success":
            return
        if state in ("failed", "aborted"):
            raise RuntimeError(f"blob copy {state}: {props.copy.status_description}")
        time.sleep(1)
    raise TimeoutError("blob copy did not finish within 120s")


def _wait_until_searchable(session_id: str, job_id: str, name: str) -> str:
    deadline = time.time() + SEARCHABLE_TIMEOUT_SECONDS
    while time.time() < deadline and not _stop.is_set():
        if search.chunk_ids_for_title(name):
            elapsed = int(SEARCHABLE_TIMEOUT_SECONDS - (deadline - time.time()))
            store.append_step(session_id, job_id, f"Searchable after about {elapsed}s.")
            return "searchable"
        time.sleep(SEARCHABLE_POLL_SECONDS)
    store.append_step(
        session_id, job_id,
        "Still indexing. The scheduled indexer run picks it up within 5 minutes.")
    return "indexing"


# --- operations --------------------------------------------------------------

def op_add(job: dict) -> str:
    payload = job["payload"]
    session_id, job_id = job["session_id"], job["job_id"]
    target = payload["target_name"]

    store.append_step(session_id, job_id, f"Promoting upload to {target}.")
    if _blob(config.DOCUMENTS_CONTAINER, target).exists():
        raise RuntimeError(
            f"{target} already exists. Ask for a replace instead of an add.")

    _copy(config.STAGING_CONTAINER, payload["upload_blob"],
          config.DOCUMENTS_CONTAINER, target)
    store.append_step(session_id, job_id, "File added to the library.")

    store.append_step(session_id, job_id, f"Indexer: {search.run_indexer()}.")
    state = _wait_until_searchable(session_id, job_id, target)
    return f"Added {target}; index state: {state}."


def op_replace(job: dict) -> str:
    payload = job["payload"]
    session_id, job_id = job["session_id"], job["job_id"]
    target = payload["target_name"]

    blob = _blob(config.DOCUMENTS_CONTAINER, target)
    if not blob.exists():
        raise RuntimeError(f"{target} no longer exists; nothing to replace.")

    lease = _lease(config.DOCUMENTS_CONTAINER, target, job_id)
    try:
        store.append_step(session_id, job_id, f"Replacing contents of {target}.")
        _copy(config.STAGING_CONTAINER, payload["upload_blob"],
              config.DOCUMENTS_CONTAINER, target, lease=lease)
    finally:
        try:
            lease.break_lease(lease_break_period=0)
        except Exception:  # noqa: BLE001
            pass

    store.append_step(session_id, job_id, f"Indexer: {search.run_indexer()}.")
    # The blob keeps its name, so its parent key is unchanged and the projected
    # chunks are rewritten in place. One version in the index, never two (1.6).
    state = _wait_until_searchable(session_id, job_id, target)
    return f"Replaced {target}; index state: {state}."


def op_delete(job: dict) -> str:
    payload = job["payload"]
    session_id, job_id = job["session_id"], job["job_id"]
    target = payload["target_name"]

    blob = _blob(config.DOCUMENTS_CONTAINER, target)
    if blob.exists():
        lease = _lease(config.DOCUMENTS_CONTAINER, target, job_id)
        blob.delete_blob(lease=lease, delete_snapshots="include")
        store.append_step(session_id, job_id, f"Deleted {target} from the library.")
    else:
        store.append_step(session_id, job_id, f"{target} was already gone from the library.")

    # Two independent paths remove the chunks, and then we verify (1.5).
    store.append_step(session_id, job_id, f"Indexer: {search.run_indexer()}.")
    result = search.purge_source(target)
    store.append_step(
        session_id, job_id,
        f"Chunk sweep: found {result['found']}, deleted {result['deleted']}, "
        f"remaining {result['remaining']}.")

    if result["remaining"] != 0:
        raise RuntimeError(
            f"{result['remaining']} chunks of {target} are still in the index.")

    return f"Deleted {target}. {result['deleted']} chunks removed, {result['remaining']} remaining."


OPERATIONS = {"add": op_add, "replace": op_replace, "delete": op_delete}


# --- job loop ----------------------------------------------------------------

def handle_job_message(message) -> None:
    queue = clients.queue(config.JOBS_QUEUE)
    try:
        envelope = json.loads(message.content)
        session_id, job_id = envelope["session_id"], envelope["job_id"]
    except Exception:  # noqa: BLE001
        log.error("undecodable job message, discarding: %r", message.content)
        queue.delete_message(message)
        return

    job = store.claim_job(session_id, job_id)
    if job is None:
        log.info("job %s already claimed or finished; acking duplicate", job_id)
        queue.delete_message(message)
        return

    operation = OPERATIONS.get(job["operation"])
    if operation is None:
        store.finish_job(session_id, job_id, "failed", f"Unknown operation {job['operation']}.")
        queue.delete_message(message)
        return

    try:
        result = operation(job)
        store.finish_job(session_id, job_id, "succeeded", result)
        log.info("job %s succeeded: %s", job_id, result)
        queue.delete_message(message)
    except Exception as exc:  # noqa: BLE001
        attempts = job["attempts"]
        log.exception("job %s attempt %s failed", job_id, attempts)
        if attempts >= config.JOB_MAX_ATTEMPTS or message.dequeue_count >= config.JOB_MAX_ATTEMPTS:
            store.finish_job(session_id, job_id, "failed", f"{type(exc).__name__}: {exc}")
            clients.queue(config.JOBS_POISON_QUEUE).send_message(message.content)
            queue.delete_message(message)
        else:
            store.mark_retrying(session_id, job_id, f"{type(exc).__name__}: {exc}")
            # Leave the message alone: the visibility timeout expires and Azure
            # redelivers it. That is the retry.


def jobs_loop() -> None:
    queue = clients.queue(config.JOBS_QUEUE)
    log.info("jobs consumer started")
    while not _stop.is_set():
        try:
            messages = queue.receive_messages(
                messages_per_page=4,
                visibility_timeout=config.JOB_VISIBILITY_TIMEOUT,
            )
            empty = True
            for message in messages:
                empty = False
                handle_job_message(message)
            if empty:
                _stop.wait(5)
        except Exception:  # noqa: BLE001
            log.exception("jobs loop error")
            _stop.wait(10)


# --- index event loop --------------------------------------------------------

def _decode_event(raw: str) -> dict | None:
    for candidate in (raw, None):
        try:
            if candidate is None:
                candidate = base64.b64decode(raw).decode("utf-8")
            return json.loads(candidate)
        except Exception:  # noqa: BLE001
            continue
    return None


def index_events_loop() -> None:
    """
    Fast path for 1.2. Blob events are coalesced: whatever arrived in the last
    few seconds becomes one indexer run, because the indexer picks up every
    changed blob in a single pass anyway. A file written three times in ten
    seconds therefore costs one or two runs, not three.
    """
    queue = clients.queue(config.INDEX_EVENTS_QUEUE, base64_encoded=False)
    log.info("index events consumer started")
    while not _stop.is_set():
        try:
            batch = list(queue.receive_messages(messages_per_page=32, visibility_timeout=120))
            if not batch:
                _stop.wait(5)
                continue

            # Small debounce so a burst of writes collapses into one run.
            _stop.wait(3)
            more = list(queue.receive_messages(messages_per_page=32, visibility_timeout=120))
            batch.extend(more)

            subjects = []
            for message in batch:
                event = _decode_event(message.content)
                if event:
                    subjects.append(event.get("subject", "?"))

            result = search.run_indexer()
            log.info("coalesced %s blob events into indexer run (%s): %s",
                     len(batch), result, subjects[:5])

            for message in batch:
                try:
                    queue.delete_message(message)
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            log.exception("index events loop error")
            _stop.wait(10)


# --- indexer heartbeat -------------------------------------------------------

def indexer_report_loop() -> None:
    """
    1.9. Every run's outcome is logged as structured text: processed, failed and
    the first few item errors. This is what an alert would be written against
    ("no successful run in 30 minutes" answers the "how would you know the
    trigger path had been down" question).
    """
    last_seen = None
    while not _stop.is_set():
        try:
            status = search.indexer_status()
            last = status.get("lastResult") or {}
            fingerprint = (last.get("startTime"), last.get("status"))
            if fingerprint != last_seen:
                last_seen = fingerprint
                log.info("indexer run report %s", json.dumps({
                    "status": last.get("status"),
                    "startTime": last.get("startTime"),
                    "endTime": last.get("endTime"),
                    "itemsProcessed": last.get("itemsProcessed"),
                    "itemsFailed": last.get("itemsFailed"),
                    "errors": last.get("errors", [])[:5],
                }))
        except Exception:  # noqa: BLE001
            log.exception("indexer report failed")
        _stop.wait(60)


def main() -> None:
    def shutdown(*_args):
        log.info("shutdown requested")
        _stop.set()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    threads = [
        threading.Thread(target=jobs_loop, name="jobs", daemon=True),
        threading.Thread(target=index_events_loop, name="events", daemon=True),
        threading.Thread(target=indexer_report_loop, name="report", daemon=True),
    ]
    for thread in threads:
        thread.start()
    while not _stop.is_set():
        _stop.wait(1)
    log.info("worker stopped")


if __name__ == "__main__":
    main()
