# [35]
terraform {
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = ">= 5.12.0"
    }
    azapi = {
      source  = "Azure/azapi"
      version = ">= 2.13.0"
    }
  }
}

# [35]
provider "azurerm" {
  features {}
  subscription_id = var.platform_subscription_id
}

# [25]
provider "azapi" {}
