# [23]
resource "azurerm_subscription" "platform" {
  alias             = var.subscription_alias
  subscription_name = var.subscription_name
  subscription_id   = var.platform_subscription_id
}

# [4]
data "azurerm_subscription" "platform" {
  subscription_id = var.platform_subscription_id
}

# [4]
resource "azurerm_management_group_subscription_association" "platform" {
  management_group_id = var.management_group_id
  subscription_id     = data.azurerm_subscription.platform.id
}

# [15]
resource "azurerm_resource_group" "platform" {
  name     = var.resource_group_name
  location = var.location
}

# [22]
resource "azurerm_virtual_network" "platform" {
  name                = var.virtual_network_name
  resource_group_name = azurerm_resource_group.platform.name
  location            = azurerm_resource_group.platform.location
  address_space       = var.virtual_network_address_space
}

# [26]
resource "azurerm_subnet" "workloads" {
  name                 = var.workload_subnet_name
  resource_group_name  = azurerm_resource_group.platform.name
  virtual_network_name = azurerm_virtual_network.platform.name
  address_prefixes     = var.workload_subnet_prefixes
}

# [26]
resource "azurerm_subnet" "private_endpoints" {
  name                            = var.private_endpoint_subnet_name
  resource_group_name             = azurerm_resource_group.platform.name
  virtual_network_name            = azurerm_virtual_network.platform.name
  address_prefixes                = var.private_endpoint_subnet_prefixes
  private_endpoint_network_policies = "Disabled"
}

# [29]
resource "azurerm_private_dns_zone" "foundry" {
  name                = var.foundry_private_dns_zone_name
  resource_group_name = azurerm_resource_group.platform.name
}

# [29]
resource "azurerm_private_dns_zone" "key_vault" {
  name                = var.key_vault_private_dns_zone_name
  resource_group_name = azurerm_resource_group.platform.name
}

# [29]
resource "azurerm_private_dns_zone" "storage" {
  name                = var.storage_private_dns_zone_name
  resource_group_name = azurerm_resource_group.platform.name
}

# [6]
resource "azurerm_private_dns_zone_virtual_network_link" "foundry" {
  name                  = var.foundry_dns_link_name
  private_dns_zone_id   = azurerm_private_dns_zone.foundry.id
  virtual_network_id    = azurerm_virtual_network.platform.id
}

# [6]
resource "azurerm_private_dns_zone_virtual_network_link" "key_vault" {
  name                  = var.key_vault_dns_link_name
  private_dns_zone_id   = azurerm_private_dns_zone.key_vault.id
  virtual_network_id    = azurerm_virtual_network.platform.id
}

# [6]
resource "azurerm_private_dns_zone_virtual_network_link" "storage" {
  name                  = var.storage_dns_link_name
  private_dns_zone_id   = azurerm_private_dns_zone.storage.id
  virtual_network_id    = azurerm_virtual_network.platform.id
}

# [14]
resource "azurerm_user_assigned_identity" "platform" {
  name                = var.identity_name
  location            = azurerm_resource_group.platform.location
  resource_group_name = azurerm_resource_group.platform.name
}

# [28]
resource "azurerm_key_vault" "platform" {
  name                       = var.key_vault_name
  location                   = azurerm_resource_group.platform.location
  resource_group_name        = azurerm_resource_group.platform.name
  tenant_id                  = var.tenant_id
  sku_name                   = "standard"
  rbac_authorization_enabled = true
}

# [16]
resource "azurerm_storage_account" "platform" {
  name                     = var.storage_account_name
  resource_group_name      = azurerm_resource_group.platform.name
  location                 = azurerm_resource_group.platform.location
  account_kind             = "StorageV2"
  account_tier             = "Standard"
  account_replication_type = "LRS"
}

# [18]
resource "azurerm_cognitive_account" "foundry" {
  name                  = var.foundry_account_name
  location              = azurerm_resource_group.platform.location
  resource_group_name   = azurerm_resource_group.platform.name
  kind                  = "AIServices"
  sku_name              = "S0"
  custom_subdomain_name = var.foundry_custom_subdomain_name
  project_management_enabled = true

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.platform.id]
  }
}

# [8]
resource "azurerm_cognitive_account_project" "foundry" {
  name                 = var.foundry_project_name
  cognitive_account_id = azurerm_cognitive_account.foundry.id
  location             = azurerm_resource_group.platform.location
  display_name         = var.foundry_project_display_name

  identity {
    type        = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.platform.id]
  }
}

# [9]
resource "azurerm_cognitive_deployment" "gpt" {
  name                 = var.gpt_deployment_name
  cognitive_account_id = azurerm_cognitive_account.foundry.id

  model {
    format  = "OpenAI"
    name    = var.gpt_deployment_name
    version = var.gpt_model_version
  }

  sku {
    name = "GlobalStandard"
  }
}

# [9]
resource "azurerm_cognitive_deployment" "embedding" {
  name                 = var.embedding_deployment_name
  cognitive_account_id = azurerm_cognitive_account.foundry.id

  model {
    format  = "OpenAI"
    name    = var.embedding_deployment_name
    version = var.embedding_model_version
  }

  sku {
    name = "GlobalStandard"
  }
}

# [13]
resource "azurerm_role_assignment" "key_vault_identity" {
  scope                = azurerm_key_vault.platform.id
  role_definition_name = var.key_vault_role_definition_name
  principal_id         = azurerm_user_assigned_identity.platform.principal_id
}

# [13]
resource "azurerm_role_assignment" "storage_identity" {
  scope                = azurerm_storage_account.platform.id
  role_definition_name = var.storage_role_definition_name
  principal_id         = azurerm_user_assigned_identity.platform.principal_id
}

# [12]
resource "azurerm_private_endpoint" "foundry" {
  name                = var.foundry_private_endpoint_name
  location            = azurerm_resource_group.platform.location
  resource_group_name = azurerm_resource_group.platform.name
  subnet_id           = azurerm_subnet.private_endpoints.id

  private_service_connection {
    name                           = var.foundry_private_connection_name
    private_connection_resource_id = azurerm_cognitive_account.foundry.id
    subresource_names              = [var.foundry_subresource_name]
    is_manual_connection           = false
  }

  private_dns_zone_group {
    name                 = "default"
    private_dns_zone_ids = [azurerm_private_dns_zone.foundry.id]
  }
}

# [12]
resource "azurerm_private_endpoint" "key_vault" {
  name                = var.key_vault_private_endpoint_name
  location            = azurerm_resource_group.platform.location
  resource_group_name = azurerm_resource_group.platform.name
  subnet_id           = azurerm_subnet.private_endpoints.id

  private_service_connection {
    name                           = var.key_vault_private_connection_name
    private_connection_resource_id = azurerm_key_vault.platform.id
    subresource_names              = [var.key_vault_subresource_name]
    is_manual_connection           = false
  }

  private_dns_zone_group {
    name                 = "default"
    private_dns_zone_ids = [azurerm_private_dns_zone.key_vault.id]
  }
}

# [12]
resource "azurerm_private_endpoint" "storage" {
  name                = var.storage_private_endpoint_name
  location            = azurerm_resource_group.platform.location
  resource_group_name = azurerm_resource_group.platform.name
  subnet_id           = azurerm_subnet.private_endpoints.id

  private_service_connection {
    name                           = var.storage_private_connection_name
    private_connection_resource_id = azurerm_storage_account.platform.id
    subresource_names              = [var.storage_subresource_name]
    is_manual_connection           = false
  }

  private_dns_zone_group {
    name                 = "default"
    private_dns_zone_ids = [azurerm_private_dns_zone.storage.id]
  }
}

# [5]
resource "azurerm_log_analytics_workspace" "platform" {
  name                = var.log_analytics_workspace_name
  location            = azurerm_resource_group.platform.location
  resource_group_name = azurerm_resource_group.platform.name
  sku                 = "PerGB2018"
  retention_in_days   = 30
}

# [10]
resource "azurerm_application_insights" "platform" {
  name                = var.application_insights_name
  location            = azurerm_resource_group.platform.location
  resource_group_name = azurerm_resource_group.platform.name
  workspace_id        = azurerm_log_analytics_workspace.platform.id
  application_type    = "web"
}

# [19]
resource "azurerm_monitor_diagnostic_setting" "foundry" {
  name                       = "foundry-to-workspace"
  target_resource_id         = azurerm_cognitive_account.foundry.id
  log_analytics_workspace_id = azurerm_log_analytics_workspace.platform.id
  log_analytics_destination_type = "Dedicated"

  enabled_log {
    category = var.foundry_log_category
  }
}

# [19]
resource "azurerm_monitor_diagnostic_setting" "key_vault" {
  name                       = "key-vault-to-workspace"
  target_resource_id         = azurerm_key_vault.platform.id
  log_analytics_workspace_id = azurerm_log_analytics_workspace.platform.id

  enabled_log {
    category = var.key_vault_log_category
  }
}

# [19]
resource "azurerm_monitor_diagnostic_setting" "storage" {
  name                       = "storage-to-workspace"
  target_resource_id         = azurerm_storage_account.platform.id
  log_analytics_workspace_id = azurerm_log_analytics_workspace.platform.id
  log_analytics_destination_type = "Dedicated"

  enabled_log {
    category = var.storage_log_category
  }
}

# [34]
resource "azurerm_api_management" "gateway" {
  name                = var.api_management_name
  location            = azurerm_resource_group.platform.location
  resource_group_name = azurerm_resource_group.platform.name
  publisher_name      = var.publisher_name
  publisher_email     = var.publisher_email
  sku_name            = "StandardV2_1"

  # [11]
  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.platform.id]
  }
}

# [9][11][29]
resource "azurerm_role_assignment" "apim_foundry_identity" {
  scope                = azurerm_cognitive_account.foundry.id
  role_definition_name = var.foundry_role_definition_name
  principal_id         = azurerm_user_assigned_identity.platform.principal_id
}

# [31]
resource "azurerm_api_management_backend" "primary" {
  name                = "primary"
  resource_group_name = azurerm_resource_group.platform.name
  api_management_name = azurerm_api_management.gateway.name
  protocol            = "http"
  url                 = azurerm_cognitive_account.foundry.endpoint

  circuit_breaker_rule {
    name                       = "primary-breaker"
    trip_duration              = "PT1H"
    accept_retry_after_enabled = true

    failure_condition {
      count            = 3
      interval_duration = "PT1H"

      status_code_range {
        min = 500
        max = 599
      }
    }
  }
}

# [31]
resource "azurerm_api_management_backend" "secondary" {
  name                = "secondary"
  resource_group_name = azurerm_resource_group.platform.name
  api_management_name = azurerm_api_management.gateway.name
  protocol            = "http"
  url                 = var.secondary_backend_url

  circuit_breaker_rule {
    name                       = "secondary-breaker"
    trip_duration              = "PT1H"
    accept_retry_after_enabled = true

    failure_condition {
      count            = 3
      interval_duration = "PT1H"

      status_code_range {
        min = 500
        max = 599
      }
    }
  }
}

# [3][5][25]
resource "azapi_resource" "failover_pool" {
  type      = "Microsoft.ApiManagement/service/backends@2024-05-01"
  name      = var.backend_pool_name
  parent_id = azurerm_api_management.gateway.id

  body = {
    properties = {
      type = "Pool"
      pool = {
        services = [
          {
            id       = azurerm_api_management_backend.primary.id
            priority = var.primary_backend_priority
            weight   = var.primary_backend_weight
          },
          {
            id       = azurerm_api_management_backend.secondary.id
            priority = var.secondary_backend_priority
            weight   = var.secondary_backend_weight
          }
        ]
      }
    }
  }
}

# [32]
resource "azurerm_api_management_api" "foundry" {
  name                = var.api_name
  resource_group_name = azurerm_resource_group.platform.name
  api_management_name = azurerm_api_management.gateway.name
  revision            = var.api_revision
  display_name        = var.api_name
  path                = var.api_path
  protocols           = ["https"]
  api_type            = "http"

  import {
    content_format = "openapi"
    content_value  = var.openapi_definition
  }
}

# [3][30][7][24][11]
resource "azurerm_api_management_api_policy" "foundry" {
  api_name            = azurerm_api_management_api.foundry.name
  api_management_name = azurerm_api_management.gateway.name
  resource_group_name = azurerm_resource_group.platform.name

  xml_content = <<-XML
    <policies>
      <inbound>
        <base />
        <!-- [3] -->
        <authentication-managed-identity
          resource="https://cognitiveservices.azure.com"
          client-id="${azurerm_user_assigned_identity.platform.client_id}" />
        <!-- [7] -->
        <set-backend-service backend-id="${azapi_resource.failover_pool.name}" />
        <!-- [24] -->
        <llm-token-limit
          counter-key="@(context.Subscription.Id)"
          tokens-per-minute="${var.tokens_per_minute}"
          estimate-prompt-tokens="false" />
        <!-- [11] -->
        <llm-emit-token-metric namespace="${var.metric_namespace}">
          <dimension name="API ID" />
        </llm-emit-token-metric>
      </inbound>
      <backend>
        <base />
      </backend>
      <outbound>
        <base />
      </outbound>
      <on-error>
        <base />
      </on-error>
    </policies>
  XML
}

# [20]
resource "azurerm_api_management_logger" "application_insights" {
  name                = "application-insights"
  api_management_name = azurerm_api_management.gateway.name
  resource_group_name = azurerm_resource_group.platform.name
  resource_id         = azurerm_application_insights.platform.id

  application_insights {
    connection_string = azurerm_application_insights.platform.connection_string
  }
}

# [37]
resource "azurerm_api_management_api_diagnostic" "application_insights" {
  identifier               = "applicationinsights"
  resource_group_name      = azurerm_resource_group.platform.name
  api_management_name      = azurerm_api_management.gateway.name
  api_name                 = azurerm_api_management_api.foundry.name
  api_management_logger_id = azurerm_api_management_logger.application_insights.id
  metrics_enabled          = true
}

# [19]
resource "azurerm_monitor_diagnostic_setting" "api_management" {
  name                       = "api-management-to-workspace"
  target_resource_id         = azurerm_api_management.gateway.id
  log_analytics_workspace_id = azurerm_log_analytics_workspace.platform.id
  log_analytics_destination_type = "Dedicated"

  enabled_log {
    category = var.apim_log_category
  }
}

# [10][11][25]
resource "azapi_resource" "foundry_apim_connection" {
  type      = "Microsoft.CognitiveServices/accounts/connections@2026-05-01"
  name      = var.foundry_connection_name
  parent_id = azurerm_cognitive_account.foundry.id

  body = {
    properties = {
      category = "ApiManagement"
      target   = "${azurerm_api_management.gateway.gateway_url}/${azurerm_api_management_api.foundry.path}"
      authType = "ManagedIdentity"
    }
  }
}
