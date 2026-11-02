# [23]
output "subscription_alias_id" {
  value = azurerm_subscription.platform.id
}

# [4]
output "management_group_subscription_association_id" {
  value = azurerm_management_group_subscription_association.platform.id
}

# [22]
output "virtual_network_id" {
  value = azurerm_virtual_network.platform.id
}

# [18]
output "foundry_endpoint" {
  value = azurerm_cognitive_account.foundry.endpoint
}

# [8]
output "foundry_project_id" {
  value = azurerm_cognitive_account_project.foundry.id
}

# [9]
output "gpt_deployment_id" {
  value = azurerm_cognitive_deployment.gpt.id
}

# [9]
output "embedding_deployment_id" {
  value = azurerm_cognitive_deployment.embedding.id
}

# [5]
output "log_analytics_workspace_id" {
  value = azurerm_log_analytics_workspace.platform.id
}

# [10]
output "application_insights_id" {
  value = azurerm_application_insights.platform.id
}

# [34]
output "api_management_name" {
  value = azurerm_api_management.gateway.name
}

# [32]
output "foundry_api_id" {
  value = azurerm_api_management_api.foundry.id
}
