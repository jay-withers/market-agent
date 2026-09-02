"""One entrypoint, three commands — `investagent api|agent|summary`.

The three workloads share one image and differ only by the container's `args`,
so this is what Terraform's `command = ["investagent"]` reaches.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from .settings import settings


def _configure_logging() -> None:
    """Structured-ish logging to stdout.

    The Container Apps environment already ships stdout to Log Analytics, and
    `daily_quota_gb = 0.15` there is genuinely tight — so this stays plain and
    the volume stays low. No OpenTelemetry distro yet; the connection string is
    already injected, so adding one later is a code change only.
    """
    logging.basicConfig(
        level=getattr(logging, settings().log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
        stream=sys.stdout,
    )
    # These are chatty at INFO and say nothing useful about the run.
    for noisy in (
        "httpx",
        "httpcore",
        "azure.core.pipeline.policies.http_logging_policy",
        "azure.identity",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="investagent")
    sub = parser.add_subparsers(dest="command", required=True)

    api = sub.add_parser("api", help="serve the read-only API")
    # Binds all interfaces because it runs behind Container Apps ingress, which
    # reaches the container over the pod network.
    api.add_argument("--host", default="0.0.0.0")
    api.add_argument("--port", type=int, default=8000)
    api.add_argument("--reload", action="store_true")

    agent = sub.add_parser("agent", help="run the agent once")
    agent.add_argument("--trigger", default="manual", choices=["manual", "schedule"])

    sub.add_parser("summary", help="produce and send the daily summary")

    args = parser.parse_args(argv)
    _configure_logging()

    if args.command == "api":
        import uvicorn

        uvicorn.run(
            "investagent.api.main:app",
            host=args.host,
            port=args.port,
            reload=args.reload,
            # Access logs are pure noise against a tight ingestion cap, and the
            # dashboard polls.
            access_log=False,
        )
        return 0

    if args.command == "agent":
        from .jobs import agent as agent_job

        # The immutable image tag, so a run records which build produced it.
        agent_job.run(trigger=args.trigger, image_tag=os.environ.get("IMAGE_TAG"))
        return 0

    if args.command == "summary":
        raise SystemExit("the summary job is not implemented yet (step 6)")

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
