"""One entrypoint, three commands — `investagent api|agent|summary`.

The three workloads share one image and differ only by the container's `args`,
so this is what Terraform's `command = ["investagent"]` reaches.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
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


def _terminate(signum: int, _frame: object) -> None:
    """Turn SIGTERM into an exception so the job's handler can run.

    Container Apps sends SIGTERM before SIGKILL when a job reaches
    `replica_timeout_in_seconds` or is scaled down. Without this the default
    disposition kills the process outright and the `agent_runs` row is left
    saying `running` for ever — the timeout case being precisely the one worth
    recording. SIGKILL cannot be caught, so the API also flags a run that has
    been `running` longer than the job timeout as stale.
    """
    raise SystemExit(f"terminated by signal {signum}")


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

        signal.signal(signal.SIGTERM, _terminate)
        try:
            # The immutable image tag, so a run records which build produced it.
            agent_job.run(trigger=args.trigger, image_tag=os.environ.get("IMAGE_TAG"))
        except (Exception, SystemExit) as exc:
            # The job has already logged the traceback and closed its
            # `agent_runs` row with the error. Letting the exception escape
            # would print the whole traceback a second time, which is what a
            # scheduled job's logs looked like before this — twice the volume
            # against a tight ingestion cap, and the operational one-liner
            # buried between two copies of the same stack.
            logging.getLogger("investagent").error("agent run failed: %s", exc)
            return 1
        return 0

    if args.command == "summary":
        raise SystemExit("the summary job is not implemented yet (step 6)")

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
