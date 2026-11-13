mock_provider "azurerm" {}
mock_provider "azapi" {}

variables {
  name                = "appi-pheasant-test"
  location            = "westus3"
  resource_group_name = "rg-test"
  workspace_id        = "/subscriptions/11111111-1111-4111-8111-111111111111/resourceGroups/rg-test/providers/Microsoft.OperationalInsights/workspaces/log-test"
  environment_id      = "/subscriptions/11111111-1111-4111-8111-111111111111/resourceGroups/rg-test/providers/Microsoft.App/managedEnvironments/cae-test"
  container_app_ids = {
    api      = "/subscriptions/11111111-1111-4111-8111-111111111111/resourceGroups/rg-test/providers/Microsoft.App/containerApps/api"
    postgres = "/subscriptions/11111111-1111-4111-8111-111111111111/resourceGroups/rg-test/providers/Microsoft.App/containerApps/postgres"
    nats     = "/subscriptions/11111111-1111-4111-8111-111111111111/resourceGroups/rg-test/providers/Microsoft.App/containerApps/nats"
  }
}

run "safe_bootstrap" {
  command = plan
  assert {
    condition     = azapi_resource_action.agent.method == "PATCH" && azapi_resource_action.agent.body.properties.openTelemetryConfiguration.tracesConfiguration.destinations == ["appInsights"] && azapi_resource_action.agent.body.properties.openTelemetryConfiguration.logsConfiguration.destinations == ["appInsights"]
    error_message = "Use JSON Merge PATCH and enable traces plus logs; never replace existing environment configuration."
  }
  assert {
    condition     = azurerm_application_insights.pheasant.workspace_id == var.workspace_id && azurerm_application_insights.pheasant.local_authentication_enabled
    error_message = "Application Insights must use the same workspace and accept the managed agent."
  }
  assert {
    condition     = length(azurerm_monitor_diagnostic_setting.metrics) == 3 && alltrue([for setting in azurerm_monitor_diagnostic_setting.metrics : length(setting.enabled_log) == 0 && length(setting.enabled_metric) == 1 && setting.log_analytics_workspace_id == var.workspace_id])
    error_message = "Route metrics for every workload without duplicating console/system log ingestion."
  }
}
