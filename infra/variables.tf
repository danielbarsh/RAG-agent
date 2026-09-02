variable "name_prefix" {
  description = "Short lowercase prefix used for every resource name. 3-8 chars."
  type        = string
  default     = "sprag"

  validation {
    condition     = can(regex("^[a-z][a-z0-9]{2,7}$", var.name_prefix))
    error_message = "name_prefix must be 3-8 lowercase alphanumeric chars, starting with a letter."
  }
}

variable "location" {
  description = <<-EOT
    Region for everything except Azure OpenAI. Must offer Azure AI Search and
    Container Apps. swedencentral and eastus2 are both fine.
  EOT
  type        = string
  default     = "swedencentral"
}

variable "openai_location" {
  description = <<-EOT
    Region for the Azure OpenAI account. Kept separate because model
    availability and quota are per-region and are the single most common reason
    an apply fails. swedencentral and eastus2 both carry
    text-embedding-3-small and gpt-4o-mini at the time of writing.
  EOT
  type        = string
  default     = "swedencentral"
}

variable "search_sku" {
  description = <<-EOT
    "free" costs nothing and is the default for the demo: 50 MB of index, 3
    indexes/indexers/skillsets, no semantic ranker, no SLA, shared compute and
    an indexer run capped at a few minutes. "basic" is the smallest tier you
    would run for real. See ARCHITECTURE.md 3.4 for the trade-off.
  EOT
  type        = string
  default     = "free"

  validation {
    condition     = contains(["free", "basic", "standard"], var.search_sku)
    error_message = "search_sku must be one of: free, basic, standard."
  }
}

variable "chat_model" {
  description = "Azure OpenAI chat model. Deployment name is kept identical to the model name."
  type        = string
  default     = "gpt-5-mini"
}

variable "chat_model_version" {
  type    = string
  default = "2025-08-07"
}

variable "chat_capacity" {
  description = "Thousands of tokens per minute for the chat deployment."
  type        = number
  default     = 30
}

variable "embedding_model" {
  type    = string
  default = "text-embedding-3-small"
}

variable "embedding_model_version" {
  type    = string
  default = "1"
}

variable "embedding_capacity" {
  description = <<-EOT
    Thousands of tokens per minute for the embedding deployment. This is the
    ceiling on backfill speed: at 30k TPM a 1,000-PDF backfill takes roughly
    7-8 minutes of wall clock. Raise it before a large backfill, lower it after.
  EOT
  type        = number
  default     = 30
}

variable "image_tag" {
  description = "Container image tag. deploy.sh sets this to the git short sha."
  type        = string
  default     = "bootstrap"
}

variable "enable_auth" {
  description = <<-EOT
    Turns on Container Apps built-in authentication against Entra ID. Leave it
    on. Set false only if your tenant forbids you from creating an app
    registration; the API then trusts a dev header and MUST NOT be left public.
  EOT
  type        = bool
  default     = true
}

variable "api_min_replicas" {
  description = <<-EOT
    0 means the API scales to zero and the first request after an idle period
    pays a cold start of roughly 5-15 seconds. 1 removes the cold start and
    costs about 12 USD/month once the Container Apps free grant is used up.
  EOT
  type    = number
  default = 0
}

variable "log_retention_days" {
  type    = number
  default = 30
}

variable "log_daily_quota_gb" {
  description = "Hard cap on Log Analytics ingestion. 0.2 GB/day is about 17 USD/month worst case; realistic usage here is far below it."
  type        = number
  default     = 0.2
}
