# [23]
variable "platform_subscription_id" {
  type        = string
  description = "Subscription ID to manage and associate with the management group."
  default     = "<your-platform-subscription-id>"
}

# [23]
variable "subscription_name" {
  type        = string
  description = "Subscription display name."
  default     = "<your-subscription-name>"
}

# [23]
variable "subscription_alias" {
  type        = string
  description = "Alias for the subscription."
  default     = "<your-subscription-alias>"
}

# [4]
variable "management_group_id" {
  type        = string
  description = "Management group resource ID."
  default     = "<your-management-group-resource-id>"
}

# [15]
variable "location" {
  type        = string
  description = "Azure location for the platform resources."
  default     = "<your-azure-location>"
}

# [15]
variable "resource_group_name" {
  type        = string
  description = "Platform resource group name."
  default     = "<your-platform-resource-group>"
}

# [22]
variable "virtual_network_name" {
  type        = string
  description = "Virtual network name."
  default     = "<your-virtual-network>"
}

# [22]
variable "virtual_network_address_space" {
  type        = list(string)
  description = "Virtual network address space."
  default     = ["<your-vnet-cidr>"]
}

# [26]
variable "workload_subnet_name" {
  type        = string
  description = "Workload subnet name."
  default     = "<your-workload-subnet>"
}

# [26]
variable "workload_subnet_prefixes" {
  type        = list(string)
  description = "Workload subnet address prefixes."
  default     = ["<your-workload-subnet-cidr>"]
}

# [26]
variable "private_endpoint_subnet_name" {
  type        = string
  description = "Private endpoint subnet name."
  default     = "<your-private-endpoint-subnet>"
}

# [26]
variable "private_endpoint_subnet_prefixes" {
  type        = list(string)
  description = "Private endpoint subnet address prefixes."
  default     = ["<your-private-endpoint-subnet-cidr>"]
}

# [29]
variable "foundry_private_dns_zone_name" {
  type        = string
  description = "Private DNS zone name for the Foundry account."
  default     = "<your-foundry-private-dns-zone>"
}

# [29]
variable "key_vault_private_dns_zone_name" {
  type        = string
  description = "Private DNS zone name for Key Vault."
  default     = "<your-key-vault-private-dns-zone>"
}

# [29]
variable "storage_private_dns_zone_name" {
  type        = string
  description = "Private DNS zone name for the storage service used by clients."
  default     = "<your-storage-private-dns-zone>"
}

# [6]
variable "foundry_dns_link_name" {
  type        = string
  description = "Virtual network link name for the Foundry private DNS zone."
  default     = "<your-foundry-dns-link>"
}

# [6]
variable "key_vault_dns_link_name" {
  type        = string
  description = "Virtual network link name for the Key Vault private DNS zone."
  default     = "<your-key-vault-dns-link>"
}

# [6]
variable "storage_dns_link_name" {
  type        = string
  description = "Virtual network link name for the storage private DNS zone."
  default     = "<your-storage-dns-link>"
}

# [18]
variable "foundry_account_name" {
  type        = string
  description = "Foundry account name."
  default     = "<your-foundry-account>"
}

# [18]
variable "foundry_custom_subdomain_name" {
  type        = string
  description = "Custom subdomain name for the Foundry account."
  default     = "<your-foundry-subdomain>"
}

# [8]
variable "foundry_project_name" {
  type        = string
  description = "Foundry project name."
  default     = "<your-foundry-project>"
}

# [8]
variable "foundry_project_display_name" {
  type        = string
  description = "Foundry project display name."
  default     = "<your-foundry-project-display-name>"
}

# [14]
variable "identity_name" {
  type        = string
  description = "User-assigned identity name."
  default     = "<your-platform-identity>"
}

# [28]
variable "tenant_id" {
  type        = string
  description = "Microsoft Entra tenant ID."
  default     = "<your-tenant-id>"
}

# [28]
variable "key_vault_name" {
  type        = string
  description = "Key Vault name."
  default     = "<your-key-vault>"
}

# [16]
variable "storage_account_name" {
  type        = string
  description = "Globally unique storage account name."
  default     = "<your-storage-account>"
}

# [12]
variable "foundry_private_endpoint_name" {
  type        = string
  description = "Foundry private endpoint name."
  default     = "<your-foundry-private-endpoint>"
}

# [12]
variable "key_vault_private_endpoint_name" {
  type        = string
  description = "Key Vault private endpoint name."
  default     = "<your-key-vault-private-endpoint>"
}

# [12]
variable "storage_private_endpoint_name" {
  type        = string
  description = "Storage private endpoint name."
  default     = "<your-storage-private-endpoint>"
}

# [12]
variable "foundry_private_connection_name" {
  type        = string
  description = "Foundry private service connection name."
  default     = "<your-foundry-private-connection>"
}

# [12]
variable "key_vault_private_connection_name" {
  type        = string
  description = "Key Vault private service connection name."
  default     = "<your-key-vault-private-connection>"
}

# [12]
variable "storage_private_connection_name" {
  type        = string
  description = "Storage private service connection name."
  default     = "<your-storage-private-connection>"
}

# [12][36]
variable "foundry_subresource_name" {
  type        = string
  description = "Foundry private endpoint subresource name."
  default     = "account"
}

# [12]
variable "key_vault_subresource_name" {
  type        = string
  description = "Key Vault private endpoint subresource name."
  default     = "<your-key-vault-subresource-name>"
}

# [12]
variable "storage_subresource_name" {
  type        = string
  description = "Storage private endpoint subresource name."
  default     = "<your-storage-subresource-name>"
}

# [13]
variable "foundry_role_definition_name" {
  type        = string
  description = "Built-in role name for the identity on the Foundry account."
  default     = "<your-foundry-account-role>"
}

# [13]
variable "key_vault_role_definition_name" {
  type        = string
  description = "Built-in role name for the identity on Key Vault."
  default     = "<your-key-vault-role>"
}

# [13]
variable "storage_role_definition_name" {
  type        = string
  description = "Built-in role name for the identity on the storage account."
  default     = "<your-storage-role>"
}

# [9]
variable "gpt_deployment_name" {
  type        = string
  description = "GPT model deployment name."
  default     = "gpt-6-luna"
}

# [9]
variable "gpt_model_version" {
  type        = string
  description = "Version of the GPT model to deploy."
  default     = "<your-gpt-model-version>"
}

# [9]
variable "embedding_deployment_name" {
  type        = string
  description = "Embedding model deployment name."
  default     = "text-embedding-3-small"
}

# [9]
variable "embedding_model_version" {
  type        = string
  description = "Version of the embedding model to deploy."
  default     = "<your-embedding-model-version>"
}

# [5]
variable "log_analytics_workspace_name" {
  type        = string
  description = "Log Analytics workspace name."
  default     = "<your-log-analytics-workspace>"
}

# [10]
variable "application_insights_name" {
  type        = string
  description = "Application Insights component name."
  default     = "<your-application-insights>"
}

# [19]
variable "foundry_log_category" {
  type        = string
  description = "Diagnostic log category supported by the Foundry account."
  default     = "<your-foundry-log-category>"
}

# [19]
variable "key_vault_log_category" {
  type        = string
  description = "Diagnostic log category supported by Key Vault."
  default     = "<your-key-vault-log-category>"
}

# [19]
variable "storage_log_category" {
  type        = string
  description = "Diagnostic log category supported by the storage account."
  default     = "<your-storage-log-category>"
}

# [19]
variable "apim_log_category" {
  type        = string
  description = "Diagnostic log category supported by API Management."
  default     = "<your-apim-log-category>"
}

# [34]
variable "api_management_name" {
  type        = string
  description = "API Management instance name."
  default     = "<your-api-management-instance>"
}

# [34]
variable "publisher_name" {
  type        = string
  description = "API Management publisher name."
  default     = "<your-publisher-name>"
}

# [34]
variable "publisher_email" {
  type        = string
  description = "API Management publisher email."
  default     = "<your-publisher-email>"
}

# [32]
variable "api_name" {
  type        = string
  description = "API Management API name."
  default     = "<your-api-name>"
}

# [32]
variable "api_revision" {
  type        = string
  description = "API Management API revision."
  default     = "1"
}

# [32]
variable "api_path" {
  type        = string
  description = "Relative URL path for the API."
  default     = "<your-api-path>"
}

# [32]
variable "openapi_definition" {
  type        = string
  description = "Valid OpenAPI document defining the Foundry API operations."
  default     = "<paste-your-valid-openapi-document>"
}

# [31]
variable "secondary_backend_url" {
  type        = string
  description = "URL for the second compatible model backend."
  default     = "<your-secondary-backend-url>"
}

# [5]
variable "primary_backend_priority" {
  type        = number
  description = "Priority for the first backend pool member; lower priority number is preferred."
  default     = 1
}

# [5]
variable "secondary_backend_priority" {
  type        = number
  description = "Priority for the second backend pool member; higher priority number makes it failover."
  default     = 2
}

# [5]
variable "primary_backend_weight" {
  type        = number
  description = "Weight for the first backend pool member."
  default     = 100
}

# [5]
variable "secondary_backend_weight" {
  type        = number
  description = "Weight for the second backend pool member."
  default     = 100
}

# [3]
variable "backend_pool_name" {
  type        = string
  description = "API Management backend pool name."
  default     = "<your-backend-pool-name>"
}

# [24]
variable "tokens_per_minute" {
  type        = number
  description = "Token rate limit for the API."
  default     = 500
}

# [11]
variable "metric_namespace" {
  type        = string
  description = "Application Insights custom metric namespace."
  default     = "<your-llm-metric-namespace>"
}

# [27]
variable "foundry_connection_name" {
  type        = string
  description = "Foundry connection name for API Management."
  default     = "<your-foundry-apim-connection>"
}
