# outputs.tf — values exported by this module.

output "environment" {
  description = "The deployment environment passed to the module."
  value       = var.environment
}

output "resource_group_id" {
  description = "ID of the created resource group."
  value       = azurerm_resource_group.this.id
}

output "resource_group_name" {
  description = "Name of the created resource group."
  value       = azurerm_resource_group.this.name
}

output "resource_group_location" {
  description = "Location of the created resource group."
  value       = azurerm_resource_group.this.location
}

# Add further module outputs below.
#
# Example:
#
# output "id" {
#   description = "Identifier of the created resource."
#   value       = resource_type.this.id
# }
