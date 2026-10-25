"""`marketagent selfcheck`: prove a job's container could do a real run, and do nothing.

After every deploy, `make smoke` (scripts/smoke.sh) starts one run of each job
— every pot's agent and sync, the summary and the weekly review — with its args
swapped for `selfcheck`. That run gets everything a real one does: the image,
the managed identity, the env and the Key Vault access. It checks what a real
run needs before it does anything: the settings load, PostgreSQL answers over
the Entra token path, and every secret the jobs read resolves, the per-pot
ones for each active pot. It calls no LLM, places no order, reads nothing from
Alpaca and sends nothing.

Exits 1 if any check fails, naming each failure but never a secret's value. A
failure fails the smoke test, which rolls the deploy back.

The pot isn't known here (the args that carried `--portfolio` are replaced),
so every active pot's secrets are checked in every run. Add a secret here when
a job starts reading one with `secret()`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from .accounts import SHARED_DEEPSEEK_KEY, deepseek_key, secret_suffix

logger = logging.getLogger(__name__)

# The summary and weekly review's DeepSeek key, and mailer.py's.
SHARED_SECRETS = (SHARED_DEEPSEEK_KEY, "RESEND-API-KEY")


def pot_secrets(pot: str) -> tuple[str, ...]:
    """What alpaca_api.py and the agent's DeepSeek provider read for one pot."""
    suffix = secret_suffix(pot)
    return (f"ALPACA-API-KEY-{suffix}", f"ALPACA-SECRET-KEY-{suffix}", deepseek_key(pot))


def active_pots() -> list[str]:
    from . import db

    with db.pool().connection() as conn:
        rows = conn.execute("SELECT name FROM portfolio WHERE is_active ORDER BY id").fetchall()
    return [row[0] for row in rows]


def checks() -> list[tuple[str, Callable[[], object]]]:
    from .settings import optional_secret, secret, settings

    def present(name: str) -> None:
        if not secret(name):
            raise ValueError("resolved to an empty value")

    found: list[tuple[str, Callable[[], object]]] = [("settings", settings)]
    try:
        pots = active_pots()
    except Exception as exc:
        error = exc

        def database() -> None:
            raise error

        found.append(("database", database))
        pots = []
    else:
        found.append(("database", lambda: pots or _no_pots()))
    names = [*SHARED_SECRETS, *(name for pot in pots for name in pot_secrets(pot))]
    found += [(f"secret {name}", lambda name=name: present(name)) for name in names]
    # Optional: absent means the summary skips its email, which is not a fault.
    found.append(
        ("secret SUMMARY-EMAIL-TO (optional)", lambda: optional_secret("SUMMARY-EMAIL-TO"))
    )
    return found


def _no_pots() -> None:
    raise LookupError("no active pot in the portfolio table")


def run() -> int:
    failed = 0
    for label, check in checks():
        try:
            check()
        except Exception as exc:
            failed += 1
            logger.error("selfcheck: FAIL %s: %s: %s", label, type(exc).__name__, str(exc)[:300])
        else:
            logger.info("selfcheck: ok %s", label)
    if failed:
        logger.error("selfcheck: %d check(s) failed", failed)
        return 1
    logger.info("selfcheck: all checks passed")
    return 0
