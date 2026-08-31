# locals.tf — module local values.

locals {
  default_tags = {
    environment = var.environment
    managed-by  = "terraform"
  }

  # Applied to every resource in main.tf; var.tags takes precedence on conflicts.
  tags = merge(local.default_tags, var.tags)
}
