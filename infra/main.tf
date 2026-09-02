locals {
  suffix = random_string.suffix.result
  name   = "${var.name_prefix}-${local.suffix}"

  # Containers, queues and tables. "documents" is the indexed source of truth;
  # "staging" holds browser uploads until a confirmed job promotes them, so an
  # upload that is never confirmed never reaches the index.
  documents_container = "documents"
  staging_container   = "staging"
  jobs_queue          = "jobs"
  jobs_poison_queue   = "jobs-poison"
  index_events_queue  = "index-events"
  jobs_table          = "jobs"
  proposals_table     = "proposals"
  sessions_table      = "sessions"

  search_index   = "docs-chunks"
  search_alias   = "docs"
  search_indexer = "docs-indexer"

  image = "${azurerm_container_registry.acr.login_server}/${var.name_prefix}:${var.image_tag}"
}

resource "random_string" "suffix" {
  length  = 6
  lower   = true
  upper   = false
  numeric = true
  special = false
}

resource "azurerm_resource_group" "rg" {
  name     = "rg-${local.name}"
  location = var.location
}

# ---------------------------------------------------------------------------
# Observability. Created first so everything else can point at it.
# ---------------------------------------------------------------------------

resource "azurerm_log_analytics_workspace" "logs" {
  name                = "log-${local.name}"
  location            = azurerm_resource_group.rg.location
  resource_group_name = azurerm_resource_group.rg.name
  sku                 = "PerGB2018"
  retention_in_days   = var.log_retention_days
  daily_quota_gb      = var.log_daily_quota_gb
}

resource "azurerm_application_insights" "appi" {
  name                = "appi-${local.name}"
  location            = azurerm_resource_group.rg.location
  resource_group_name = azurerm_resource_group.rg.name
  workspace_id        = azurerm_log_analytics_workspace.logs.id
  application_type    = "web"
  sampling_percentage = 100
}

# ---------------------------------------------------------------------------
# Identity. One user-assigned identity shared by the API and the worker so that
# role assignments are written once and both roles are identical in effect.
# ---------------------------------------------------------------------------

resource "azurerm_user_assigned_identity" "app" {
  name                = "id-${local.name}"
  location            = azurerm_resource_group.rg.location
  resource_group_name = azurerm_resource_group.rg.name
}

# ---------------------------------------------------------------------------
# Storage: source of truth for files, plus the queue and table that make jobs
# durable. One account, four services, no keys used by application code.
# ---------------------------------------------------------------------------

resource "azurerm_storage_account" "sa" {
  name                            = "st${var.name_prefix}${local.suffix}"
  resource_group_name             = azurerm_resource_group.rg.name
  location                        = azurerm_resource_group.rg.location
  account_tier                    = "Standard"
  account_replication_type        = "LRS"
  account_kind                    = "StorageV2"
  access_tier                     = "Hot"
  allow_nested_items_to_be_public = false
  min_tls_version                 = "TLS1_2"

  # Shared keys stay enabled for exactly one consumer: the KEDA queue-length
  # scale rule on the worker, which cannot use a managed identity through the
  # azurerm provider today. Application code never reads a key.
  shared_access_key_enabled = true

  blob_properties {
    # Native blob soft delete is what lets the Azure AI Search blob indexer
    # notice deletions (NativeBlobSoftDeleteDeletionDetectionPolicy). Without
    # it a deleted file would silently stay in the index.
    delete_retention_policy {
      days = 7
    }
    versioning_enabled = false

    cors_rule {
      allowed_origins    = ["https://ca-${local.name}-api.${azurerm_container_app_environment.env.default_domain}"]
      allowed_methods    = ["GET", "PUT", "POST", "HEAD", "OPTIONS"]
      allowed_headers    = ["*"]
      exposed_headers    = ["*"]
      max_age_in_seconds = 3600
    }
  }
}

resource "azurerm_storage_container" "documents" {
  name                  = local.documents_container
  storage_account_id    = azurerm_storage_account.sa.id
  container_access_type = "private"
}

resource "azurerm_storage_container" "staging" {
  name                  = local.staging_container
  storage_account_id    = azurerm_storage_account.sa.id
  container_access_type = "private"
}

resource "azurerm_storage_queue" "jobs" {
  name               = local.jobs_queue
  storage_account_id = azurerm_storage_account.sa.id
}

resource "azurerm_storage_queue" "jobs_poison" {
  name               = local.jobs_poison_queue
  storage_account_id = azurerm_storage_account.sa.id
}

resource "azurerm_storage_queue" "index_events" {
  name               = local.index_events_queue
  storage_account_id = azurerm_storage_account.sa.id
}

resource "azurerm_storage_table" "jobs" {
  name                 = local.jobs_table
  storage_account_name = azurerm_storage_account.sa.name
}

resource "azurerm_storage_table" "proposals" {
  name                 = local.proposals_table
  storage_account_name = azurerm_storage_account.sa.name
}

resource "azurerm_storage_table" "sessions" {
  name                 = local.sessions_table
  storage_account_name = azurerm_storage_account.sa.name
}

# ---------------------------------------------------------------------------
# Change feed: Blob events -> Event Grid -> storage queue -> worker -> indexer
# run. This is the fast path. The indexer's own 5-minute schedule is the slow
# path that catches up if this one is down.
# ---------------------------------------------------------------------------

resource "azurerm_eventgrid_system_topic" "blobs" {
  name                   = "egst-${local.name}"
  resource_group_name    = azurerm_resource_group.rg.name
  location               = azurerm_resource_group.rg.location
  source_arm_resource_id = azurerm_storage_account.sa.id
  topic_type             = "Microsoft.Storage.StorageAccounts"
}

resource "azurerm_eventgrid_system_topic_event_subscription" "to_queue" {
  name                = "documents-changed"
  system_topic        = azurerm_eventgrid_system_topic.blobs.name
  resource_group_name = azurerm_resource_group.rg.name

  included_event_types = [
    "Microsoft.Storage.BlobCreated",
    "Microsoft.Storage.BlobDeleted",
  ]

  subject_filter {
    subject_begins_with = "/blobServices/default/containers/${local.documents_container}/"
  }

  storage_queue_endpoint {
    storage_account_id = azurerm_storage_account.sa.id
    queue_name         = azurerm_storage_queue.index_events.name
  }

  retry_policy {
    max_delivery_attempts = 30
    event_time_to_live    = 1440
  }

  depends_on = [azurerm_storage_container.documents]
}

# ---------------------------------------------------------------------------
# Azure AI Search. Index, skillset, indexer and data source are not modelled by
# the azurerm provider, so they are created by scripts/provision_search.py,
# driven from Terraform below (see search_provision).
# ---------------------------------------------------------------------------

resource "azurerm_search_service" "search" {
  name                = "srch-${local.name}"
  resource_group_name = azurerm_resource_group.rg.name
  location            = azurerm_resource_group.rg.location
  sku                 = var.search_sku
  replica_count       = 1
  partition_count     = 1

  # API keys off. Everything on the data plane authenticates with Entra ID, so
  # there is no admin key to leak, rotate or accidentally commit.
  local_authentication_enabled = false

  # Semantic ranker is not offered on the free tier.
  semantic_search_sku = var.search_sku == "free" ? null : "free"

  identity {
    type = "SystemAssigned"
  }
}

# ---------------------------------------------------------------------------
# Azure OpenAI: one embedding deployment (indexing + query vectorisation) and
# one chat deployment (the agent).
# ---------------------------------------------------------------------------

resource "azurerm_cognitive_account" "openai" {
  name                  = "aoai-${local.name}"
  resource_group_name   = azurerm_resource_group.rg.name
  location              = var.openai_location
  kind                  = "OpenAI"
  sku_name              = "S0"
  custom_subdomain_name = "aoai-${local.name}" # required for Entra ID auth
}

resource "azurerm_cognitive_deployment" "embedding" {
  name                 = var.embedding_model
  cognitive_account_id = azurerm_cognitive_account.openai.id

  model {
    format  = "OpenAI"
    name    = var.embedding_model
    version = var.embedding_model_version
  }

  sku {
    name     = "GlobalStandard"
    capacity = var.embedding_capacity
  }
}

resource "azurerm_cognitive_deployment" "chat" {
  name                 = var.chat_model
  cognitive_account_id = azurerm_cognitive_account.openai.id

  model {
    format  = "OpenAI"
    name    = var.chat_model
    version = var.chat_model_version
  }

  sku {
    name     = "GlobalStandard"
    capacity = var.chat_capacity
  }
}

# ---------------------------------------------------------------------------
# Container registry. Basic tier, admin user off; images are pulled with the
# app's managed identity and built in the cloud by `az acr build` so nobody
# needs Docker installed.
# ---------------------------------------------------------------------------

resource "azurerm_container_registry" "acr" {
  name                = "acr${var.name_prefix}${local.suffix}"
  resource_group_name = azurerm_resource_group.rg.name
  location            = azurerm_resource_group.rg.location
  sku                 = "Basic"
  admin_enabled       = false
}

# ---------------------------------------------------------------------------
# Role assignments. Every one of these replaces a secret.
# ---------------------------------------------------------------------------

# Search reads blobs and calls the embedding model with its own identity.
resource "azurerm_role_assignment" "search_reads_blobs" {
  scope                = azurerm_storage_account.sa.id
  role_definition_name = "Storage Blob Data Reader"
  principal_id         = azurerm_search_service.search.identity[0].principal_id
}

resource "azurerm_role_assignment" "search_calls_openai" {
  scope                = azurerm_cognitive_account.openai.id
  role_definition_name = "Cognitive Services OpenAI User"
  principal_id         = azurerm_search_service.search.identity[0].principal_id
}

# The application identity: files, queue, tables, search, models, registry.
resource "azurerm_role_assignment" "app_blobs" {
  scope                = azurerm_storage_account.sa.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
}

resource "azurerm_role_assignment" "app_blob_delegator" {
  # Needed to mint user-delegation SAS for direct browser uploads.
  scope                = azurerm_storage_account.sa.id
  role_definition_name = "Storage Blob Delegator"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
}

resource "azurerm_role_assignment" "app_queues" {
  scope                = azurerm_storage_account.sa.id
  role_definition_name = "Storage Queue Data Contributor"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
}

resource "azurerm_role_assignment" "app_tables" {
  scope                = azurerm_storage_account.sa.id
  role_definition_name = "Storage Table Data Contributor"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
}

resource "azurerm_role_assignment" "app_search_data" {
  scope                = azurerm_search_service.search.id
  role_definition_name = "Search Index Data Contributor"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
}

resource "azurerm_role_assignment" "app_search_control" {
  # Lets the worker run and reset the indexer and read its execution history.
  scope                = azurerm_search_service.search.id
  role_definition_name = "Search Service Contributor"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
}

resource "azurerm_role_assignment" "app_openai" {
  scope                = azurerm_cognitive_account.openai.id
  role_definition_name = "Cognitive Services OpenAI User"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
}

resource "azurerm_role_assignment" "app_acr_pull" {
  scope                = azurerm_container_registry.acr.id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
}

# The human running terraform: needed to create the index/skillset/indexer and
# to upload demo PDFs from the CLI.
resource "azurerm_role_assignment" "me_search_control" {
  scope                = azurerm_search_service.search.id
  role_definition_name = "Search Service Contributor"
  principal_id         = data.azurerm_client_config.current.object_id
}

resource "azurerm_role_assignment" "me_search_data" {
  scope                = azurerm_search_service.search.id
  role_definition_name = "Search Index Data Contributor"
  principal_id         = data.azurerm_client_config.current.object_id
}

resource "azurerm_role_assignment" "me_blobs" {
  scope                = azurerm_storage_account.sa.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = data.azurerm_client_config.current.object_id
}

# ---------------------------------------------------------------------------
# Container Apps
# ---------------------------------------------------------------------------

resource "azurerm_container_app_environment" "env" {
  name                       = "cae-${local.name}"
  resource_group_name        = azurerm_resource_group.rg.name
  location                   = azurerm_resource_group.rg.location
  log_analytics_workspace_id = azurerm_log_analytics_workspace.logs.id
}

locals {
  common_env = [
    { name = "AZURE_CLIENT_ID", value = azurerm_user_assigned_identity.app.client_id },
    { name = "STORAGE_ACCOUNT", value = azurerm_storage_account.sa.name },
    { name = "DOCUMENTS_CONTAINER", value = local.documents_container },
    { name = "STAGING_CONTAINER", value = local.staging_container },
    { name = "JOBS_QUEUE", value = local.jobs_queue },
    { name = "JOBS_POISON_QUEUE", value = local.jobs_poison_queue },
    { name = "INDEX_EVENTS_QUEUE", value = local.index_events_queue },
    { name = "JOBS_TABLE", value = local.jobs_table },
    { name = "PROPOSALS_TABLE", value = local.proposals_table },
    { name = "SESSIONS_TABLE", value = local.sessions_table },
    { name = "SEARCH_ENDPOINT", value = "https://${azurerm_search_service.search.name}.search.windows.net" },
    { name = "SEARCH_INDEX", value = local.search_index },
    { name = "SEARCH_ALIAS", value = local.search_alias },
    { name = "SEARCH_INDEXER", value = local.search_indexer },
    { name = "SEARCH_SKU", value = var.search_sku },
    { name = "OPENAI_ENDPOINT", value = azurerm_cognitive_account.openai.endpoint },
    { name = "CHAT_DEPLOYMENT", value = azurerm_cognitive_deployment.chat.name },
    { name = "EMBEDDING_DEPLOYMENT", value = azurerm_cognitive_deployment.embedding.name },
    { name = "APPLICATIONINSIGHTS_CONNECTION_STRING", value = azurerm_application_insights.appi.connection_string },
    { name = "AUTH_ENABLED", value = tostring(var.enable_auth) },
    { name = "ADMIN_ROLE", value = "FileAdmin" },
  ]
}

resource "azurerm_container_app" "api" {
  name                         = "ca-${local.name}-api"
  resource_group_name          = azurerm_resource_group.rg.name
  container_app_environment_id = azurerm_container_app_environment.env.id
  revision_mode                = "Single"

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.app.id]
  }

  registry {
    server   = azurerm_container_registry.acr.login_server
    identity = azurerm_user_assigned_identity.app.id
  }

  dynamic "secret" {
    for_each = var.enable_auth ? [1] : []
    content {
      name  = "aad-client-secret"
      value = azuread_application_password.app[0].value
    }
  }

  ingress {
    external_enabled = true
    target_port      = 8000
    transport        = "auto"

    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }

  template {
    min_replicas = var.api_min_replicas
    max_replicas = 3

    container {
      name   = "api"
      image  = local.image
      cpu    = 0.5
      memory = "1Gi"

      env {
        name  = "ROLE"
        value = "api"
      }

      dynamic "env" {
        for_each = local.common_env
        content {
          name  = env.value.name
          value = env.value.value
        }
      }

      liveness_probe {
        transport = "HTTP"
        port      = 8000
        path      = "/healthz"
      }
    }

    http_scale_rule {
      name                = "http"
      concurrent_requests = 20
    }
  }

  depends_on = [azurerm_role_assignment.app_acr_pull]
}

resource "azurerm_container_app" "worker" {
  name                         = "ca-${local.name}-worker"
  resource_group_name          = azurerm_resource_group.rg.name
  container_app_environment_id = azurerm_container_app_environment.env.id
  revision_mode                = "Single"

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.app.id]
  }

  registry {
    server   = azurerm_container_registry.acr.login_server
    identity = azurerm_user_assigned_identity.app.id
  }

  # The only application secret in the system. KEDA needs a connection string
  # to read queue depth; it is stored as a Container Apps secret, never in git
  # and never in the browser bundle.
  secret {
    name  = "queue-connection"
    value = azurerm_storage_account.sa.primary_connection_string
  }

  template {
    # Scales to zero. A queued job wakes it within ~10-30s.
    min_replicas = 0
    max_replicas = 3

    container {
      name   = "worker"
      image  = local.image
      cpu    = 0.5
      memory = "1Gi"

      env {
        name  = "ROLE"
        value = "worker"
      }

      dynamic "env" {
        for_each = local.common_env
        content {
          name  = env.value.name
          value = env.value.value
        }
      }
    }

    custom_scale_rule {
      name             = "jobs-queue"
      custom_rule_type = "azure-queue"
      metadata = {
        queueName   = local.jobs_queue
        queueLength = "1"
      }
      authentication {
        secret_name       = "queue-connection"
        trigger_parameter = "connection"
      }
    }

    custom_scale_rule {
      name             = "index-events-queue"
      custom_rule_type = "azure-queue"
      metadata = {
        queueName   = local.index_events_queue
        queueLength = "1"
      }
      authentication {
        secret_name       = "queue-connection"
        trigger_parameter = "connection"
      }
    }
  }

  depends_on = [azurerm_role_assignment.app_acr_pull]
}
