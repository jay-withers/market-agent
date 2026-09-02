"""Tests for the entrypoint the three workloads share."""

from __future__ import annotations

import pytest

from investagent import cli


def test_no_command_is_an_error():
    with pytest.raises(SystemExit):
        cli.main([])


def test_an_unknown_command_is_an_error():
    with pytest.raises(SystemExit):
        cli.main(["dance"])


def test_the_agent_command_runs_the_job(monkeypatch):
    called = {}
    monkeypatch.setattr(cli, "_configure_logging", lambda: None)
    monkeypatch.setitem(__import__("sys").modules, "investagent.jobs.agent", _FakeJob(called))

    assert cli.main(["agent", "--trigger", "manual"]) == 0
    assert called["trigger"] == "manual"


def test_a_failed_agent_run_exits_non_zero_without_re_raising(monkeypatch, caplog):
    """The job already logged the traceback and closed its run row.

    Letting the exception escape printed the whole stack a second time — twice
    the log volume against a tight ingestion cap, with the operational message
    buried between two copies of it.
    """
    monkeypatch.setattr(cli, "_configure_logging", lambda: None)
    monkeypatch.setitem(
        __import__("sys").modules,
        "investagent.jobs.agent",
        _FakeJob({}, error=RuntimeError("credit balance is too low")),
    )

    assert cli.main(["agent"]) == 1
    assert "agent run failed: credit balance is too low" in caplog.text
    # One report, not a re-raised traceback on top of the job's own.
    assert caplog.text.count("credit balance is too low") == 1


def test_the_summary_command_says_it_is_not_written_yet():
    with pytest.raises(SystemExit, match="not implemented yet"):
        cli.main(["summary"])


class _FakeJob:
    """Stands in for the `investagent.jobs.agent` module."""

    def __init__(self, called: dict, error: Exception | None = None):
        self._called = called
        self._error = error

    def run(self, trigger: str = "schedule", image_tag: str | None = None, **_kwargs):
        self._called["trigger"] = trigger
        if self._error:
            raise self._error
        return 1


def test_sigterm_becomes_an_exception_so_the_run_row_gets_closed():
    """Container Apps sends SIGTERM when a job hits replica_timeout_in_seconds.

    Left to the default disposition it kills the process outright and the
    agent_runs row says `running` for ever — the timeout being exactly the case
    worth recording.
    """
    with pytest.raises(SystemExit, match="terminated by signal 15"):
        cli._terminate(15, None)


def test_a_terminated_agent_run_still_exits_non_zero(monkeypatch, caplog):
    monkeypatch.setattr(cli, "_configure_logging", lambda: None)
    monkeypatch.setitem(
        __import__("sys").modules,
        "investagent.jobs.agent",
        _FakeJob({}, error=SystemExit("terminated by signal 15")),
    )

    assert cli.main(["agent"]) == 1
    assert "terminated by signal 15" in caplog.text
