"""Configuration. Everything arrives as an environment variable set by Terraform."""

from __future__ import annotations

import os


def _get(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise RuntimeError(f"Missing required environment variable {name}")
    return value


ROLE = os.environ.get("ROLE", "api")

MANAGED_IDENTITY_CLIENT_ID = os.environ.get("AZURE_CLIENT_ID") or None

STORAGE_ACCOUNT = _get("STORAGE_ACCOUNT", "devstorage")
BLOB_ENDPOINT = f"https://{STORAGE_ACCOUNT}.blob.core.windows.net"
QUEUE_ENDPOINT = f"https://{STORAGE_ACCOUNT}.queue.core.windows.net"
TABLE_ENDPOINT = f"https://{STORAGE_ACCOUNT}.table.core.windows.net"

DOCUMENTS_CONTAINER = _get("DOCUMENTS_CONTAINER", "documents")
STAGING_CONTAINER = _get("STAGING_CONTAINER", "staging")

JOBS_QUEUE = _get("JOBS_QUEUE", "jobs")
JOBS_POISON_QUEUE = _get("JOBS_POISON_QUEUE", "jobs-poison")
INDEX_EVENTS_QUEUE = _get("INDEX_EVENTS_QUEUE", "index-events")

JOBS_TABLE = _get("JOBS_TABLE", "jobs")
PROPOSALS_TABLE = _get("PROPOSALS_TABLE", "proposals")
SESSIONS_TABLE = _get("SESSIONS_TABLE", "sessions")

SEARCH_ENDPOINT = _get("SEARCH_ENDPOINT", "https://localhost").rstrip("/")
SEARCH_INDEX = _get("SEARCH_INDEX", "docs-chunks")
SEARCH_ALIAS = _get("SEARCH_ALIAS", "docs")
SEARCH_INDEXER = _get("SEARCH_INDEXER", "docs-indexer")
SEARCH_SKU = os.environ.get("SEARCH_SKU", "free")
SEARCH_API_VERSION = "2024-11-01-preview"
SEMANTIC_RANKER = SEARCH_SKU != "free"

OPENAI_ENDPOINT = _get("OPENAI_ENDPOINT", "https://localhost").rstrip("/")
OPENAI_API_VERSION = "2024-12-01-preview"
CHAT_DEPLOYMENT = _get("CHAT_DEPLOYMENT", "gpt-4o-mini")
EMBEDDING_DEPLOYMENT = _get("EMBEDDING_DEPLOYMENT", "text-embedding-3-small")

AUTH_ENABLED = os.environ.get("AUTH_ENABLED", "true").lower() == "true"
ADMIN_ROLE = os.environ.get("ADMIN_ROLE", "FileAdmin")

# Conversation history kept per session. Table Storage caps a string property at
# 32k UTF-16 chars, so history is trimmed rather than allowed to grow.
MAX_HISTORY_MESSAGES = 24

MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # 100 MB; 2.11 asks for 50 MB
UPLOAD_SAS_MINUTES = 20

# Job lifecycle
JOB_VISIBILITY_TIMEOUT = 300   # seconds a job is invisible while a worker holds it
JOB_MAX_ATTEMPTS = 5
BLOB_LEASE_SECONDS = 60
