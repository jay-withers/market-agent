# Module tests. The azurerm provider is mocked, so these run with no Azure
# credentials — both locally (`make test`) and in CI (ci-terraform).
#
# Add `assert` blocks as the module grows; see the commented example below.

mock_provider "azurerm" {}

run "plan_with_defaults" {
  # apply (against the mocked provider, so no real Azure calls) rather than
  # plan: the resource group's name comes from the naming module's random
  # suffix, which isn't known until after apply.
  command = apply

  variables {
    environment = "dev"
    location    = "uksouth"
  }

  assert {
    condition     = output.environment == "dev"
    error_message = "output environment did not match the requested environment"
  }

  assert {
    condition     = azurerm_resource_group.this.location == "uksouth"
    error_message = "resource group location did not match the requested location"
  }

  assert {
    condition     = strcontains(azurerm_resource_group.this.name, "dev")
    error_message = "resource group name did not include the environment"
  }

  assert {
    condition     = azurerm_resource_group.this.tags["environment"] == "dev"
    error_message = "resource group did not receive the default environment tag"
  }

  assert {
    condition     = azurerm_resource_group.this.tags["managed-by"] == "terraform"
    error_message = "resource group did not receive the default managed-by tag"
  }
}

run "rejects_unknown_environment" {
  command = plan

  variables {
    environment = "not-a-real-environment"
    location    = "uksouth"
  }

  expect_failures = [var.environment]
}

run "uses_default_location_when_unset" {
  command = apply

  variables {
    environment = "stg"
  }

  assert {
    condition     = azurerm_resource_group.this.location == "westeurope"
    error_message = "resource group did not fall back to the default location"
  }
}

run "custom_tags_merge_with_defaults" {
  command = apply

  variables {
    environment = "dev"
    location    = "uksouth"
    tags = {
      managed-by = "pulumi" # overrides the module's default
      owner      = "platform-team"
    }
  }

  assert {
    condition     = azurerm_resource_group.this.tags["managed-by"] == "pulumi"
    error_message = "caller-supplied tags did not override the default managed-by tag"
  }

  assert {
    condition     = azurerm_resource_group.this.tags["owner"] == "platform-team"
    error_message = "caller-supplied tags were not applied to the resource group"
  }

  assert {
    condition     = azurerm_resource_group.this.tags["environment"] == "dev"
    error_message = "default environment tag should still be present when not overridden"
  }
}

# Add further assert blocks as the module grows; see the commented example
# below.
#
# assert {
#   condition     = output.name == var.name
#   error_message = "output name did not match the requested name"
# }
