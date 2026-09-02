"""
Azure clients. One credential, no keys, no connection strings.

DefaultAzureCredential resolves to the user-assigned managed identity in
Container Apps (AZURE_CLIENT_ID is injected by Terraform) and to `az login` on a
developer machine, so the same code path works in both places.
"""

from __future__ import annotations

import functools
import threading
import time

from azure.core.exceptions import ResourceExistsError
from azure.data.tables import TableServiceClient
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient
from azure.storage.queue import QueueClient, TextBase64EncodePolicy, TextBase64DecodePolicy

from . import config


@functools.lru_cache(maxsize=1)
def credential() -> DefaultAzureCredential:
    return DefaultAzureCredential(
        managed_identity_client_id=config.MANAGED_IDENTITY_CLIENT_ID,
        exclude_interactive_browser_credential=True,
    )


@functools.lru_cache(maxsize=1)
def blob_service() -> BlobServiceClient:
    return BlobServiceClient(config.BLOB_ENDPOINT, credential=credential())


@functools.lru_cache(maxsize=1)
def table_service() -> TableServiceClient:
    return TableServiceClient(endpoint=config.TABLE_ENDPOINT, credential=credential())


@functools.lru_cache(maxsize=8)
def table(name: str):
    client = table_service().get_table_client(name)
    try:
        client.create_table()
    except ResourceExistsError:
        pass
    except Exception:  # table already exists or is being created concurrently
        pass
    return client


@functools.lru_cache(maxsize=8)
def queue(name: str, base64_encoded: bool = True) -> QueueClient:
    """
    Our own messages are base64 encoded (the Azure default for the older SDKs and
    what most tooling expects). Event Grid writes plain JSON to `index-events`,
    so that queue is read without a decode policy and handled in worker.py.
    """
    kwargs = {}
    if base64_encoded:
        kwargs["message_encode_policy"] = TextBase64EncodePolicy()
        kwargs["message_decode_policy"] = TextBase64DecodePolicy()
    return QueueClient(
        account_url=config.QUEUE_ENDPOINT,
        queue_name=name,
        credential=credential(),
        **kwargs,
    )


# --- token cache for the REST calls we make by hand (Azure AI Search) ---------

_token_lock = threading.Lock()
_tokens: dict[str, tuple[str, float]] = {}


def bearer(scope: str) -> str:
    now = time.time()
    with _token_lock:
        cached = _tokens.get(scope)
        if cached and cached[1] - 120 > now:
            return cached[0]
    token = credential().get_token(scope)
    with _token_lock:
        _tokens[scope] = (token.token, token.expires_on)
    return token.token


def search_token() -> str:
    return bearer("https://search.azure.com/.default")


def openai_token() -> str:
    return bearer("https://cognitiveservices.azure.com/.default")


@functools.lru_cache(maxsize=1)
def openai_client():
    from openai import AzureOpenAI

    return AzureOpenAI(
        azure_endpoint=config.OPENAI_ENDPOINT,
        api_version=config.OPENAI_API_VERSION,
        azure_ad_token_provider=openai_token,
        max_retries=3,
        timeout=120.0,
    )
