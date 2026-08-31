terraform {
  required_version = ">= 1.6"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
    # Transitive dependency of module.naming (Azure/naming/azurerm), declared
    # here explicitly so the module's full provider footprint is visible.
    random = {
      source  = "hashicorp/random"
      version = ">= 3.3.2"
    }
  }
}
