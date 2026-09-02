# ---------------------------------------------------------------------------
# Sign-in. Container Apps built-in authentication ("Easy Auth") in front of the
# API container: unauthenticated browsers are redirected to Entra ID before a
# single line of application code runs, and the app reads the validated
# principal from an injected header. No token handling in our code, no tokens
# in the browser bundle.
#
# The chicken-and-egg between "the app registration needs the app's URL" and
# "the app needs the client secret" is broken by adding the redirect URI as a
# separate resource after the container app exists.
# ---------------------------------------------------------------------------

resource "random_uuid" "app_uri" {}
resource "random_uuid" "file_admin_role" {}

resource "azuread_application" "app" {
  count            = var.enable_auth ? 1 : 0
  display_name     = "${local.name}-agent"
  owners           = [data.azuread_client_config.current.object_id]
  sign_in_audience = "AzureADMyOrg"

  app_role {
    id                   = random_uuid.file_admin_role.result
    allowed_member_types = ["User"]
    display_name         = "File administrator"
    description          = "May confirm jobs that add, replace or delete files in the library."
    value                = "FileAdmin"
    enabled              = true
  }

  web {
    implicit_grant {
      id_token_issuance_enabled = true
    }
  }

  feature_tags {
    enterprise = true
  }

    lifecycle {
    ignore_changes = [identifier_uris, web[0].redirect_uris]
  }
}

resource "azuread_application_identifier_uri" "app" {
  count          = var.enable_auth ? 1 : 0
  application_id = azuread_application.app[0].id
  identifier_uri = "api://${azuread_application.app[0].client_id}"
}

resource "azuread_service_principal" "app" {
  count                        = var.enable_auth ? 1 : 0
  client_id                    = azuread_application.app[0].client_id
  app_role_assignment_required = false
  owners                       = [data.azuread_client_config.current.object_id]
}

resource "azuread_application_password" "app" {
  count          = var.enable_auth ? 1 : 0
  application_id = azuread_application.app[0].id
  display_name   = "container-apps-easy-auth"
  end_date       = timeadd(timestamp(), "8760h") # 1 year

  lifecycle {
    ignore_changes = [end_date]
  }
}

# Give whoever ran terraform the ability to mutate files. Everyone else in the
# tenant can sign in and search but cannot confirm a job.
resource "azuread_app_role_assignment" "me_file_admin" {
  count               = var.enable_auth ? 1 : 0
  app_role_id         = random_uuid.file_admin_role.result
  principal_object_id = data.azuread_client_config.current.object_id
  resource_object_id  = azuread_service_principal.app[0].object_id
}

resource "azuread_application_redirect_uris" "web" {
  count          = var.enable_auth ? 1 : 0
  application_id = azuread_application.app[0].id
  type           = "Web"

  redirect_uris = [
    "https://${azurerm_container_app.api.ingress[0].fqdn}/.auth/login/aad/callback",
  ]
}

resource "azapi_resource" "auth_config" {
  count     = var.enable_auth ? 1 : 0
  type      = "Microsoft.App/containerApps/authConfigs@2024-03-01"
  name      = "current"
  parent_id = azurerm_container_app.api.id

  body = {
    properties = {
      platform = {
        enabled = true
      }
      globalValidation = {
        unauthenticatedClientAction = "RedirectToLoginPage"
        redirectToProvider          = "azureactivedirectory"
        excludedPaths               = ["/healthz"]
      }
      identityProviders = {
        azureActiveDirectory = {
          enabled = true
          registration = {
            openIdIssuer            = "https://login.microsoftonline.com/${data.azurerm_client_config.current.tenant_id}/v2.0"
            clientId                = azuread_application.app[0].client_id
            clientSecretSettingName = "aad-client-secret"
          }
          validation = {
            allowedAudiences = [
              "api://${azuread_application.app[0].client_id}",
              azuread_application.app[0].client_id,
            ]
          }
        }
      }
      login = {
        tokenStore = {
          enabled = false
        }
      }
    }
  }

  depends_on = [azuread_application_redirect_uris.web]
}