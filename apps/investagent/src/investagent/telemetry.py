"""Application Insights, configured at start-up and flushed before exit.

Terraform injects `APPLICATIONINSIGHTS_CONNECTION_STRING` into every workload,
but nothing reads it unless this module runs: Container Apps has no codeless
agent, so the variable on its own populates precisely nothing. An absent or
empty string disables telemetry entirely, which is what keeps `docker compose`,
the tests and a bare `investagent agent` offline and free of the SDK's start-up
cost.

Two things are deliberately narrower than the distro's defaults, both because
the workspace runs on `daily_quota_gb = 0.15` and Application Insights writes
into that same workspace:

- **Only the `investagent` logger is exported.** The distro attaches its
  handler to the root logger by default, which would ship every third-party log
  line to Application Insights *as well as* to Log Analytics, where container
  stdout already lands. Our own records are the ones worth paying for twice.
- **Performance counters and live metrics are off.** Both are periodic emissions
  from workloads that are either scale-to-zero or a four-minute job — there is
  no live stream worth watching at 06:00, and Container Apps already reports CPU
  and memory as free platform metrics.
"""

from __future__ import annotations

import logging
import os

from .settings import settings

logger = logging.getLogger(__name__)

_configured = False


def configure(role: str) -> bool:
    """Set up tracing, metrics and log export. Returns whether it was enabled.

    `role` becomes the cloud role name, so the API, the agent and the summary
    job are distinguishable in the portal rather than appearing as one
    application — they share an image and would otherwise be indistinguishable.

    Call this *before* the FastAPI app is imported: the instrumentation patches
    `FastAPI.__init__` to add its middleware, so an app object built first is
    never instrumented. `cli.py` satisfies that by letting uvicorn import the
    module by string after this has run.
    """
    global _configured

    if _configured:
        return True

    connection_string = settings().applicationinsights_connection_string
    if not connection_string:
        return False

    # Read when each instrumentation is applied, so it has to be set first. The
    # API is probed on /healthz every 30s and /readyz every 10s; at six spans a
    # minute for as long as a replica is alive, health checks would be most of
    # what the tight ingestion cap ever bought.
    os.environ.setdefault("OTEL_PYTHON_FASTAPI_EXCLUDED_URLS", "healthz,readyz")

    # Imported here rather than at module scope so that the models, the risk
    # engine and the tests need neither the SDK nor a connection string — the
    # same reason settings.py defers its Azure imports.
    from azure.monitor.opentelemetry import configure_azure_monitor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.sdk.resources import Resource

    configure_azure_monitor(
        connection_string=connection_string,
        logger_name="investagent",
        enable_live_metrics=False,
        enable_performance_counters=False,
        resource=Resource.create(
            {
                "service.name": f"investagent-{role}",
                # The immutable image tag, matching `agent_runs.image_tag`, so a
                # trace and the row it produced name the same build.
                "service.version": os.environ.get("IMAGE_TAG", "unknown"),
                "deployment.environment": settings().environment,
            }
        ),
    )

    # Not in the distro, and it is what turns the Alpaca, news and FX calls into
    # dependency spans. Note this covers `fetch.py` only: the Anthropic SDK
    # speaks `httpx2`, a different package this instrumentation does not patch,
    # so the LLM calls stay invisible here — `agent_runs` records their cost and
    # token counts instead.
    HTTPXClientInstrumentor().instrument()

    _configured = True
    logger.info("application insights enabled for %s", role)
    return True


def flush(timeout_millis: int = 10_000) -> None:
    """Push buffered telemetry before a short-lived process exits.

    The exporters batch, and the agent and summary jobs are processes that run
    for minutes and then stop. Without this a job exits with its spans and logs
    still in the buffer, which looks exactly like telemetry that was never
    configured at all.

    Never raises: losing telemetry must not turn a successful run into a failed
    one, nor mask the exception a job is already exiting on.
    """
    if not _configured:
        return

    from opentelemetry import _logs, metrics, trace

    for provider in (
        trace.get_tracer_provider(),
        _logs.get_logger_provider(),
        metrics.get_meter_provider(),
    ):
        force_flush = getattr(provider, "force_flush", None)
        if force_flush is None:
            continue
        try:
            force_flush(timeout_millis)
        except Exception as exc:
            logger.warning("flushing telemetry failed: %s", exc)
