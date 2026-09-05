"""What `telemetry.configure()` asks the distro for.

The distro itself is not exercised — that would mean an exporter and a network
— so this asserts the arguments, the same way `test_llm.py` asserts what the
LLM client is called with. The choices being pinned all exist to keep ingestion
inside a 0.15 GB/day workspace quota, and every one of them is a default the
distro would otherwise flip the other way.
"""

from __future__ import annotations

import pytest

from investagent import telemetry

CONNECTION_STRING = "InstrumentationKey=00000000-0000-0000-0000-000000000000"


@pytest.fixture(autouse=True)
def unconfigured():
    """`configure()` is idempotent by a module global, which must not leak."""
    telemetry._configured = False
    yield
    telemetry._configured = False


@pytest.fixture
def captured(monkeypatch):
    """Stand in for the distro and the httpx instrumentation."""
    calls: dict = {}

    def fake_configure(**kwargs):
        calls["kwargs"] = kwargs

    class FakeInstrumentor:
        def instrument(self):
            calls["httpx_instrumented"] = True

    monkeypatch.setattr("azure.monitor.opentelemetry.configure_azure_monitor", fake_configure)
    monkeypatch.setattr(
        "opentelemetry.instrumentation.httpx.HTTPXClientInstrumentor", FakeInstrumentor
    )
    return calls


def test_disabled_without_a_connection_string(monkeypatch):
    """The local stack and the tests must not need the SDK or a credential."""
    assert telemetry.configure("agent") is False
    assert telemetry._configured is False


def test_flush_without_configure_is_a_noop():
    """A job exiting with telemetry disabled must not touch OpenTelemetry."""
    telemetry.flush()


def test_configures_from_the_connection_string(monkeypatch, captured):
    monkeypatch.setenv("APPLICATIONINSIGHTS_CONNECTION_STRING", CONNECTION_STRING)
    monkeypatch.setenv("IMAGE_TAG", "abc1234")

    assert telemetry.configure("agent") is True

    kwargs = captured["kwargs"]
    assert kwargs["connection_string"] == CONNECTION_STRING
    # Root-logger export would ship every third-party record to Application
    # Insights on top of the stdout copy Log Analytics already stores.
    assert kwargs["logger_name"] == "investagent"
    # Both default to True and both are periodic emissions from a workload that
    # is scale-to-zero or a four-minute job.
    assert kwargs["enable_live_metrics"] is False
    assert kwargs["enable_performance_counters"] is False

    attributes = kwargs["resource"].attributes
    # One image, three entrypoints: without a distinct role name they arrive as
    # a single indistinguishable application.
    assert attributes["service.name"] == "investagent-agent"
    # Matches `agent_runs.image_tag`, so a trace names the build that made it.
    assert attributes["service.version"] == "abc1234"

    assert captured["httpx_instrumented"] is True


def test_health_probes_are_excluded(monkeypatch, captured):
    """Six probe requests a minute would be most of what the cap ever bought."""
    monkeypatch.delenv("OTEL_PYTHON_FASTAPI_EXCLUDED_URLS", raising=False)
    monkeypatch.setenv("APPLICATIONINSIGHTS_CONNECTION_STRING", CONNECTION_STRING)

    telemetry.configure("api")

    import os

    assert os.environ["OTEL_PYTHON_FASTAPI_EXCLUDED_URLS"] == "healthz,readyz"


def test_configure_is_idempotent(monkeypatch, captured):
    """The API path calls it once, but a second call must not re-instrument."""
    monkeypatch.setenv("APPLICATIONINSIGHTS_CONNECTION_STRING", CONNECTION_STRING)

    assert telemetry.configure("api") is True
    captured.clear()
    assert telemetry.configure("api") is True
    assert captured == {}
