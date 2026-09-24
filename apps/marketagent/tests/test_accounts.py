"""Tests for how a pot's name becomes its secret names."""

from __future__ import annotations

import pytest

from marketagent.accounts import SHARED_DEEPSEEK_KEY, deepseek_key, secret_suffix


@pytest.mark.parametrize(
    ("name", "suffix"), [("tech", "TECH"), ("health", "HEALTH"), ("a-b", "AB")]
)
def test_the_suffix_is_upper_case_without_hyphens(name, suffix):
    assert secret_suffix(name) == suffix


@pytest.mark.parametrize("name", ["", "Tech", "7tech", "dynamic-500", "tech_1", "toolong"])
def test_a_name_that_could_not_be_a_job_name_is_rejected(name):
    """Six lower-case characters at most, because the pot name ends up in a
    container app job name that allows 32 in total."""
    with pytest.raises(ValueError):
        secret_suffix(name)


def test_each_pot_has_its_own_deepseek_key_and_the_shared_one_is_separate():
    assert deepseek_key("tech") == "DEEPSEEK-API-KEY-TECH"
    assert deepseek_key("tech") != SHARED_DEEPSEEK_KEY
