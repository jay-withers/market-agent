environment = "stg"

# Key Vault Secrets Officer: the same human as the PostgreSQL Entra admin in
# main.database.tf. Explicit, because CD applies as the pipeline identity.
key_vault_administrator_object_ids = ["b168eef0-d213-406e-a7f9-7b9198d580da"]
