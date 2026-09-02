terraform {
  required_version = ">= 1.6.0"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.14"
    }
    azuread = {
      source  = "hashicorp/azuread"
      version = "~> 3.0"
    }
    azapi = {
      source  = "Azure/azapi"
      version = "~> 2.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # State is local by default so that a stranger can clone and apply with no
  # pre-existing storage. `terraform.tfstate` is gitignored: it contains the
  # Entra client secret and the storage connection string used by the KEDA
  # scale rule. For anything beyond a personal demo, move it to a remote
  # backend (see README, "State").
}

provider "azurerm" {
  features {
    resource_group {
      prevent_deletion_if_contains_resources = false
    }
    cognitive_account {
      purge_soft_delete_on_destroy = true
    }
  }
  # Speeds up apply on a fresh subscription; set to true if your account is not
  # allowed to register providers and ask an owner to register them for you.
  resource_provider_registrations = "core"
}

provider "azuread" {}

provider "azapi" {}

data "azurerm_client_config" "current" {}
data "azuread_client_config" "current" {}
