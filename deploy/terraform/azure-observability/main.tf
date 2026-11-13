terraform {
  required_version = ">= 1.11.0"
  required_providers {
    azurerm = { source = "hashicorp/azurerm", version = ">= 5.12.0" }
    azapi   = { source = "Azure/azapi", version = "~> 2.13" }
  }
}

variable "name" { type = string }
variable "location" { type = string }
variable "resource_group_name" { type = string }
variable "workspace_id" { type = string }
variable "environment_id" { type = string }
variable "container_app_ids" { type = map(string) }
variable "tags" {
  type    = map(string)
  default = {}
}

resource "azurerm_application_insights" "pheasant" {
  name                         = var.name
  location                     = var.location
  resource_group_name          = var.resource_group_name
  workspace_id                 = var.workspace_id
  application_type             = "web"
  retention_in_days            = 30
  sampling_percentage          = 100
  local_authentication_enabled = true # Required by the ACA managed OTLP agent.
  tags                         = var.tags
}

# Patch only telemetry properties: retain existing networking, logs and profiles.
# ACA injects OTEL_EXPORTER_OTLP_ENDPOINT/PROTOCOL=grpc into application replicas.
resource "azapi_resource_action" "agent" {
  type        = "Microsoft.App/managedEnvironments@2026-07-01"
  method      = "PATCH"
  when        = "apply"
  resource_id = var.environment_id
  body = {
    location = var.location
    properties = {
      openTelemetryConfiguration = {
        tracesConfiguration = { destinations = ["appInsights"] }
        logsConfiguration   = { destinations = ["appInsights"] }
      }
    }
  }
  sensitive_body = {
    properties = {
      appInsightsConfiguration = {
        connectionString = azurerm_application_insights.pheasant.connection_string
      }
    }
  }
  sensitive_body_version = {
    "properties.appInsightsConfiguration.connectionString" = azurerm_application_insights.pheasant.app_id
  }
  response_export_values = []
}

# Console/system logs already use the environment's Log Analytics destination.
# Do not duplicate those logs with a second diagnostic route. Native platform
# metrics bypass the OTel agent (App Insights accepts traces and logs only).
resource "azurerm_monitor_diagnostic_setting" "metrics" {
  for_each                   = var.container_app_ids
  name                       = "pheasant-platform-metrics"
  target_resource_id         = each.value
  log_analytics_workspace_id = var.workspace_id
  enabled_metric { category = "AllMetrics" }
}

output "application_insights_id" { value = azurerm_application_insights.pheasant.id }
output "application_insights_name" { value = azurerm_application_insights.pheasant.name }

