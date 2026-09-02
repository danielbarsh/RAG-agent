output "app_url" {
  description = "Open this in a browser. Sign in with the account that ran terraform."
  value       = "https://${azurerm_container_app.api.ingress[0].fqdn}"
}

output "resource_group" {
  value = azurerm_resource_group.rg.name
}

output "registry_name" {
  description = "Used by deploy.sh for `az acr build`."
  value       = azurerm_container_registry.acr.name
}

output "storage_account" {
  value = azurerm_storage_account.sa.name
}

output "documents_container" {
  value = local.documents_container
}

output "search_endpoint" {
  value = "https://${azurerm_search_service.search.name}.search.windows.net"
}

output "search_index" {
  value = local.search_index
}

output "search_indexer" {
  value = local.search_indexer
}

output "openai_endpoint" {
  value = azurerm_cognitive_account.openai.endpoint
}

output "app_registration_client_id" {
  description = "Assign other users to the FileAdmin app role on this application to let them mutate files."
  value       = var.enable_auth ? azuread_application.app[0].client_id : "auth disabled"
}
