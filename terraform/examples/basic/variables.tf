# variables.tf — root config input variables.

variable "environment" {
  description = "Deployment environment. Selected via -var-file=../../environments/<dev|stg|prd>.tfvars."
  type        = string
}
