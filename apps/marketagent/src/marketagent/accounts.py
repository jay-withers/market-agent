"""Naming rules shared by everything that is keyed to a pot.

A pot's Key Vault secrets are derived from its name rather than listed, so adding
a pot is a database row, a Terraform map entry and its secrets, with no code
change: `tech` reads `ALPACA-API-KEY-TECH`, `ALPACA-SECRET-KEY-TECH` and
`DEEPSEEK-API-KEY-TECH`.
"""

from __future__ import annotations

import re

# Six characters at most: `caj-<project>-<env>-agent-<pot>` has to fit the 32
# characters a container app job name allows, and with `marketagent-dev` that
# leaves six. Lower-case because job names reject upper-case.
_NAME = re.compile(r"[a-z][a-z0-9-]{0,5}")

# The key the summary and weekly review use. They cover every pot in one call,
# so charging them to a pot's key would inflate that pot's bill.
SHARED_DEEPSEEK_KEY = "DEEPSEEK-API-KEY"


def secret_suffix(name: str) -> str:
    """Upper-case with hyphens removed: `tech` -> `TECH`, `a-b` -> `AB`."""
    if not _NAME.fullmatch(name):
        raise ValueError(f"invalid pot name {name!r}")
    return name.upper().replace("-", "")


def deepseek_key(name: str) -> str:
    return f"DEEPSEEK-API-KEY-{secret_suffix(name)}"
