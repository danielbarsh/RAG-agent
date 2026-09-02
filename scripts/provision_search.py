#!/usr/bin/env python3
"""
Create or update the Azure AI Search index, alias, data source, skillset and
indexer. Idempotent: every call is a PUT, so re-running is a no-op when nothing
changed. Invoked by Terraform (infra/search.tf) and safe to run by hand.

Auth is Entra ID only - the search service is created with API keys disabled.
"""

from __future__ import annotations

import json
import os
import sys
import time

import httpx
from azure.identity import DefaultAzureCredential

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import search_definitions as defs  # noqa: E402

API_VERSION = "2024-11-01-preview"

ENDPOINT = os.environ["SEARCH_ENDPOINT"].rstrip("/")
INDEX = os.environ.get("SEARCH_INDEX", "docs-chunks")
ALIAS = os.environ.get("SEARCH_ALIAS", "docs")
INDEXER = os.environ.get("SEARCH_INDEXER", "docs-indexer")
SKU = os.environ.get("SEARCH_SKU", "free")
OPENAI_ENDPOINT = os.environ["OPENAI_ENDPOINT"]
EMBEDDING_DEPLOYMENT = os.environ["EMBEDDING_DEPLOYMENT"]
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small")
STORAGE_ACCOUNT_ID = os.environ["STORAGE_ACCOUNT_ID"]
DOCUMENTS_CONTAINER = os.environ.get("DOCUMENTS_CONTAINER", "documents")

DATASOURCE = f"{INDEX}-blob"
SKILLSET = f"{INDEX}-skillset"

# Semantic ranker is not available on the free tier.
SEMANTIC = SKU != "free"


def token() -> str:
    cred = DefaultAzureCredential(exclude_interactive_browser_credential=False)
    return cred.get_token("https://search.azure.com/.default").token


def put(client: httpx.Client, path: str, body: dict, label: str) -> None:
    url = f"{ENDPOINT}/{path}?api-version={API_VERSION}"
    # Role assignments can take a minute to propagate on a brand new service,
    # and the service itself answers 403 while it finishes provisioning.
    for attempt in range(12):
        r = client.put(url, json=body)
        if r.status_code in (200, 201, 204):
            print(f"  ok   {label}")
            return
        if r.status_code in (401, 403, 409, 429, 503) and attempt < 11:
            wait = min(5 * (attempt + 1), 30)
            print(f"  wait {label}: {r.status_code}, retrying in {wait}s")
            time.sleep(wait)
            continue
        print(f"  FAIL {label}: {r.status_code}\n{r.text}", file=sys.stderr)
        r.raise_for_status()


def main() -> int:
    print(f"Provisioning search objects on {ENDPOINT} (sku={SKU}, semantic={SEMANTIC})")

    headers = {"Authorization": f"Bearer {token()}", "Content-Type": "application/json"}
    with httpx.Client(headers=headers, timeout=120.0) as client:
        put(client,
            f"indexes/{INDEX}",
            defs.index_definition(INDEX, OPENAI_ENDPOINT, EMBEDDING_DEPLOYMENT,
                                  EMBEDDING_MODEL, SEMANTIC),
            f"index {INDEX}")

        put(client,
            f"aliases/{ALIAS}",
            defs.alias_definition(ALIAS, INDEX),
            f"alias {ALIAS} -> {INDEX}")

        put(client,
            f"datasources/{DATASOURCE}",
            defs.datasource_definition(DATASOURCE, STORAGE_ACCOUNT_ID, DOCUMENTS_CONTAINER),
            f"datasource {DATASOURCE}")

        put(client,
            f"skillsets/{SKILLSET}",
            defs.skillset_definition(SKILLSET, INDEX, OPENAI_ENDPOINT,
                                     EMBEDDING_DEPLOYMENT, EMBEDDING_MODEL),
            f"skillset {SKILLSET}")

        put(client,
            f"indexers/{INDEXER}",
            defs.indexer_definition(INDEXER, DATASOURCE, SKILLSET, INDEX),
            f"indexer {INDEXER}")

        # Kick one run immediately so a fresh deployment with seeded documents
        # is searchable without waiting for the schedule.
        r = client.post(f"{ENDPOINT}/indexers/{INDEXER}/run?api-version={API_VERSION}")
        print(f"  run  {INDEXER}: {r.status_code}")

    print("Search provisioning complete.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"provision_search.py failed: {exc}", file=sys.stderr)
        sys.exit(1)
