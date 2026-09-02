"""
Every Azure AI Search object this system needs, as data.

Kept in one file so the review can read the whole retrieval design in one sitting,
and so `terraform_data.search_provision` can hash it and re-apply on change.

Design notes that belong next to the definitions:

Chunking (1.3). SplitSkill in "pages" mode, 2000 characters with 500 of overlap.
  - 2000 characters is roughly 400-500 tokens. That is small enough that a
    retrieved chunk is a quotable passage rather than a page of prose, and large
    enough to keep a clause and its qualifier together, which is what actually
    matters in policy and contract text.
  - 500 characters of overlap (25%) exists because the sentence that answers the
    question is regularly the one that straddles a boundary. 25% is the point
    where recall stops improving noticeably; past it you are paying to embed the
    same words repeatedly.
  - SplitSkill breaks on sentence boundaries where it can, so chunks do not start
    mid-word.
  - Alternatives rejected: one vector per document (a 400-page manual averages to
    nothing useful and cannot be cited precisely); fixed token windows with no
    overlap (cheapest, measurably worse at boundary questions); layout-aware
    semantic chunking via Document Intelligence (better, and the right upgrade
    for tables and multi-column contracts, but it is billed per page with no free
    tier, so it is documented as a seam rather than built).

Retrieval mode (1.4). Hybrid: BM25 over `chunk` plus HNSW vector search over
`text_vector`, fused with RRF, with semantic reranking on top when the tier
allows it. Keyword-only loses paraphrases; vector-only loses exact identifiers
like a contract number or a clause reference, which is exactly what people
search policy libraries for.

Citation (1.4). Every chunk carries `title` and `source_path` back to the exact
blob it came from, and `parent_id` ties every chunk to its parent document key,
which is what makes deletion verifiable (1.5).

Deletion (1.5). The data source uses NativeBlobSoftDeleteDeletionDetectionPolicy,
so a deleted blob is detected on the next indexer run and index projections
remove the child chunks with the parent. The worker additionally sweeps by
`source_path` and verifies the count is zero; see app/app/search.py.

No-op re-processing (1.6). The blob indexer's change detection is the blob's
LastModified high-water mark. An unchanged blob is not re-read, not re-chunked
and not re-embedded. An updated blob is re-projected under the same parent key,
so the old chunks are replaced rather than duplicated.
"""

from __future__ import annotations

CHUNK_CHARS = 2000
CHUNK_OVERLAP_CHARS = 500
EMBEDDING_DIMENSIONS = 1536  # text-embedding-3-small


def index_definition(name: str, openai_endpoint: str, embedding_deployment: str,
                     embedding_model: str, semantic: bool) -> dict:
    definition = {
        "name": name,
        "fields": [
            {
                "name": "chunk_id",
                "type": "Edm.String",
                "key": True,
                "searchable": True,
                "filterable": True,
                "sortable": True,
                "retrievable": True,
                "analyzer": "keyword",
            },
            {
                "name": "parent_id",
                "type": "Edm.String",
                "filterable": True,
                "retrievable": True,
            },
            {
                "name": "title",
                "type": "Edm.String",
                "searchable": True,
                "filterable": True,
                "sortable": True,
                "retrievable": True,
            },
            {
                "name": "source_path",
                "type": "Edm.String",
                "filterable": True,
                "retrievable": True,
            },
            {
                "name": "last_modified",
                "type": "Edm.DateTimeOffset",
                "filterable": True,
                "sortable": True,
                "retrievable": True,
            },
            {
                "name": "chunk",
                "type": "Edm.String",
                "searchable": True,
                "retrievable": True,
            },
            {
                "name": "text_vector",
                "type": "Collection(Edm.Single)",
                "searchable": True,
                "retrievable": False,
                "stored": False,
                "dimensions": EMBEDDING_DIMENSIONS,
                "vectorSearchProfile": "default-profile",
            },
        ],
        "vectorSearch": {
            "algorithms": [
                {
                    "name": "default-hnsw",
                    "kind": "hnsw",
                    "hnswParameters": {
                        "m": 4,
                        "efConstruction": 400,
                        "efSearch": 500,
                        "metric": "cosine",
                    },
                }
            ],
            "vectorizers": [
                {
                    # Query-time vectorisation: the app sends text, Search calls
                    # the same embedding deployment with its own managed
                    # identity. Guarantees query and document vectors can never
                    # drift onto different models.
                    "name": "default-vectorizer",
                    "kind": "azureOpenAI",
                    "azureOpenAIParameters": {
                        "resourceUri": openai_endpoint.rstrip("/"),
                        "deploymentId": embedding_deployment,
                        "modelName": embedding_model,
                    },
                }
            ],
            "profiles": [
                {
                    "name": "default-profile",
                    "algorithm": "default-hnsw",
                    "vectorizer": "default-vectorizer",
                }
            ],
        },
    }

    if semantic:
        definition["semantic"] = {
            "defaultConfiguration": "default-semantic",
            "configurations": [
                {
                    "name": "default-semantic",
                    "prioritizedFields": {
                        "titleField": {"fieldName": "title"},
                        "prioritizedContentFields": [{"fieldName": "chunk"}],
                        "prioritizedKeywordsFields": [],
                    },
                }
            ],
        }

    return definition


def datasource_definition(name: str, storage_account_id: str, container: str) -> dict:
    return {
        "name": name,
        "type": "azureblob",
        "credentials": {
            # ResourceId form = authenticate with the search service's own
            # managed identity. No account key anywhere.
            "connectionString": f"ResourceId={storage_account_id};"
        },
        "container": {"name": container},
        "dataDeletionDetectionPolicy": {
            "@odata.type": "#Microsoft.Azure.Search.NativeBlobSoftDeleteDeletionDetectionPolicy"
        },
    }


def skillset_definition(name: str, index_name: str, openai_endpoint: str,
                        embedding_deployment: str, embedding_model: str) -> dict:
    return {
        "name": name,
        "description": "Split extracted PDF text into overlapping chunks and embed each chunk.",
        "skills": [
            {
                "@odata.type": "#Microsoft.Skills.Text.SplitSkill",
                "name": "split",
                "description": "Overlapping character windows on sentence boundaries.",
                "context": "/document",
                "textSplitMode": "pages",
                "maximumPageLength": CHUNK_CHARS,
                "pageOverlapLength": CHUNK_OVERLAP_CHARS,
                "defaultLanguageCode": "en",
                "inputs": [{"name": "text", "source": "/document/content"}],
                "outputs": [{"name": "textItems", "targetName": "pages"}],
            },
            {
                "@odata.type": "#Microsoft.Skills.Text.AzureOpenAIEmbeddingSkill",
                "name": "embed",
                "context": "/document/pages/*",
                "resourceUri": openai_endpoint.rstrip("/"),
                "deploymentId": embedding_deployment,
                "modelName": embedding_model,
                "dimensions": EMBEDDING_DIMENSIONS,
                "inputs": [{"name": "text", "source": "/document/pages/*"}],
                "outputs": [{"name": "embedding", "targetName": "text_vector"}],
            },
        ],
        "indexProjections": {
            "selectors": [
                {
                    "targetIndexName": index_name,
                    "parentKeyFieldName": "parent_id",
                    "sourceContext": "/document/pages/*",
                    "mappings": [
                        {"name": "chunk", "source": "/document/pages/*"},
                        {"name": "text_vector", "source": "/document/pages/*/text_vector"},
                        {"name": "title", "source": "/document/metadata_storage_name"},
                        {"name": "source_path", "source": "/document/metadata_storage_path"},
                        {"name": "last_modified", "source": "/document/metadata_storage_last_modified"},
                    ],
                }
            ],
            "parameters": {
                # Only chunks land in the index. There is no parent document to
                # keep in sync, so there is one place a stale copy could hide
                # instead of two.
                "projectionMode": "skipIndexingParentDocuments"
            },
        },
    }


def indexer_definition(name: str, datasource: str, skillset: str, index_name: str) -> dict:
    return {
        "name": name,
        "dataSourceName": datasource,
        "skillsetName": skillset,
        "targetIndexName": index_name,
        # Slow path (1.2). Five minutes is the minimum blob indexer interval and
        # is the freshness guarantee: even with Event Grid, the queue, and the
        # worker all dead, the index is never more than ~5 minutes plus one run
        # behind the library. The fast path (Event Grid -> queue -> on-demand
        # run) is what makes the normal case ~15-45 seconds.
        "schedule": {"interval": "PT5M"},
        "parameters": {
            "batchSize": 1,
            # 1.8: a corrupt or password-protected PDF is recorded as an item
            # error and the run continues. It is not silent: the error is in the
            # indexer execution history, surfaced at /api/indexer/status and
            # logged to Application Insights by the worker after every run.
            "maxFailedItems": 100,
            "maxFailedItemsPerBatch": 10,
            "configuration": {
                "dataToExtract": "contentAndMetadata",
                "parsingMode": "default",
                "indexedFileNameExtensions": ".pdf",
                "failOnUnsupportedContentType": False,
                "failOnUnprocessableDocument": False,
                "allowSkillsetToReadFileData": False,
            },
        },
        "fieldMappings": [],
        "outputFieldMappings": [],
    }


def alias_definition(alias: str, index_name: str) -> dict:
    # The application only ever queries the alias. Re-embedding onto a new index
    # is therefore a build-then-swap with no application deploy; see the
    # embedding-model migration answer in the README.
    return {"name": alias, "indexes": [index_name]}
