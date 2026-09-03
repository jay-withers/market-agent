"""The daily email, via Resend.

Deliberately thin. The summary is *stored* whether or not it sends — the
dashboard renders it from `daily_summaries` — so a mail failure must not lose
the day's write-up or fail the job. Sending is the last thing that happens and
its outcome is recorded rather than raised.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .fetch import FetchError, post_json
from .settings import optional_secret, secret, settings

logger = logging.getLogger(__name__)

RESEND_ENDPOINT = "https://api.resend.com/emails"


@dataclass(frozen=True)
class MailResult:
    """The outcome of one send, for `daily_summaries.email_status`."""

    # pending | sent | failed | skipped, matching the CHECK constraint.
    status: str
    provider_id: str | None = None
    error: str | None = None


def send(subject: str, html: str, text: str, client: Any = None) -> MailResult:
    """Send the summary, returning what happened rather than raising.

    `skipped` when no recipient is configured, which is the default: a job that
    emails on every run during development is worse than one that has to be
    switched on deliberately.
    """
    cfg = settings()
    # From Key Vault rather than the environment Terraform injects: a personal
    # address in a public repository is permanent, and a runtime lookup means
    # changing the recipient needs no redeploy.
    recipient = optional_secret("SUMMARY-EMAIL-TO")
    if not recipient:
        logger.info("no SUMMARY-EMAIL-TO configured, not sending")
        return MailResult(status="skipped")

    try:
        payload = post_json(
            RESEND_ENDPOINT,
            body={
                "from": cfg.summary_email_from,
                # Resend takes a list even for one recipient.
                "to": [address.strip() for address in recipient.split(",") if address.strip()],
                "subject": subject,
                "html": html,
                "text": text,
            },
            headers={
                "Authorization": f"Bearer {secret('RESEND-API-KEY')}",
                "Content-Type": "application/json",
            },
            client=client,
        )
    except FetchError as exc:
        # Not raised: the summary is already composed and about to be stored,
        # and losing it because a mail provider had a bad minute would be a
        # poor trade. The failure is recorded on the row instead.
        logger.warning("summary email failed: %s", exc)
        return MailResult(status="failed", error=str(exc)[:500])

    return MailResult(status="sent", provider_id=(payload or {}).get("id"))
