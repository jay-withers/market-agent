# Terraform module

<!-- BEGIN_TF_DOCS -->
## Requirements

| Name | Version |
| ---- | ------- |
| <a name="requirement_terraform"></a> [terraform](#requirement\_terraform) | >= 1.6 |
| <a name="requirement_azurerm"></a> [azurerm](#requirement\_azurerm) | ~> 4.0 |
| <a name="requirement_random"></a> [random](#requirement\_random) | >= 3.3.2 |

## Providers

| Name | Version |
| ---- | ------- |
| <a name="provider_azurerm"></a> [azurerm](#provider\_azurerm) | 4.81.0 |

## Modules

| Name | Source | Version |
| ---- | ------ | ------- |
| <a name="module_naming"></a> [naming](#module\_naming) | Azure/naming/azurerm | ~> 0.4 |

## Resources

| Name | Type |
| ---- | ---- |
| [azurerm_resource_group.this](https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/resources/resource_group) | resource |

## Inputs

| Name | Description | Type | Default | Required |
| ---- | ----------- | ---- | ------- | :------: |
| <a name="input_environment"></a> [environment](#input\_environment) | Deployment environment. Drives environment-specific behaviour (naming, sizing, etc.) as the module grows. | `string` | n/a | yes |
| <a name="input_location"></a> [location](#input\_location) | Azure region the resource group is created in. | `string` | `"westeurope"` | no |
| <a name="input_tags"></a> [tags](#input\_tags) | Tags applied to all resources created by this module, merged with (and taking precedence over) the module's default tags (`environment`, `managed-by`). | `map(string)` | `{}` | no |

## Outputs

| Name | Description |
| ---- | ----------- |
| <a name="output_environment"></a> [environment](#output\_environment) | The deployment environment passed to the module. |
| <a name="output_resource_group_id"></a> [resource\_group\_id](#output\_resource\_group\_id) | ID of the created resource group. |
| <a name="output_resource_group_location"></a> [resource\_group\_location](#output\_resource\_group\_location) | Location of the created resource group. |
| <a name="output_resource_group_name"></a> [resource\_group\_name](#output\_resource\_group\_name) | Name of the created resource group. |
<!-- END_TF_DOCS -->
