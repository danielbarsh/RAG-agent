# ---------------------------------------------------------------------------
# The azurerm provider models the search *service* but not its index, skillset,
# indexer or data source. Rather than click those together in the portal (which
# 3.2 rules out), they are declared as JSON in scripts/search_definitions.py and
# PUT idempotently by scripts/provision_search.py, driven from here so that
# `terraform apply` really is the whole deployment.
#
# The script authenticates with Entra ID (the search service has API keys
# disabled) using the credentials of whoever runs terraform, who was granted
# Search Service Contributor above.
# ---------------------------------------------------------------------------

resource "terraform_data" "search_provision" {
  triggers_replace = {
    # Re-run whenever anything the definitions depend on changes.
    definitions = filesha256("${path.module}/../scripts/search_definitions.py")
    service     = azurerm_search_service.search.id
    index       = local.search_index
    embedding   = azurerm_cognitive_deployment.embedding.name
    storage     = azurerm_storage_account.sa.id
  }

  provisioner "local-exec" {
    working_dir = "${path.module}/.."
    command     = "python3 scripts/provision_search.py"

    environment = {
      SEARCH_ENDPOINT      = "https://${azurerm_search_service.search.name}.search.windows.net"
      SEARCH_INDEX         = local.search_index
      SEARCH_ALIAS         = local.search_alias
      SEARCH_INDEXER       = local.search_indexer
      SEARCH_SKU           = var.search_sku
      OPENAI_ENDPOINT      = azurerm_cognitive_account.openai.endpoint
      EMBEDDING_DEPLOYMENT = azurerm_cognitive_deployment.embedding.name
      EMBEDDING_MODEL      = var.embedding_model
      STORAGE_ACCOUNT_ID   = azurerm_storage_account.sa.id
      DOCUMENTS_CONTAINER  = local.documents_container
    }
  }

  depends_on = [
    azurerm_role_assignment.me_search_control,
    azurerm_role_assignment.me_search_data,
    azurerm_role_assignment.search_reads_blobs,
    azurerm_role_assignment.search_calls_openai,
    azurerm_cognitive_deployment.embedding,
    azurerm_storage_container.documents,
  ]
}
