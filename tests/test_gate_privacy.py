"""Noise calibration, policy validation and exact decimal budget boundaries (no DB)."""
import copy
import math
import random
import statistics
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.errors import Unprocessable
from app.services import gate_checks, gate_privacy as gp
from tests.test_gate_checks import doc, table


@pytest.mark.parametrize("fields", [
    {"epsilon": "0", "sensitivity": "1"}, {"epsilon": "1"}, {"sensitivity": "1"},
    {"epsilon_limit": "1"}, {"query_limit": "-1"}, {"query_limit": "1.5"}, {"query_limit": True},
    {"query_limit": "2147483648"}, {"epsilon": "NaN", "sensitivity": "1"},
    {"epsilon": "Infinity", "sensitivity": "1"}, {"epsilon": "0.0000001", "sensitivity": "1"},
    {"epsilon": "1", "sensitivity": "0"}, {"epsilon": "1", "sensitivity": "-1"},
    {"epsilon": "1", "sensitivity": "1000000000001"},
    {"epsilon": "1", "sensitivity": "0.000001"},
])
def test_invalid_policy_fails_closed(fields):
    with pytest.raises(Unprocessable) as exc:
        gp.Policy.parse(fields)
    assert exc.value.code == "invalid_gate_policy"


def test_defaults_and_decimal_budget_boundaries():
    assert gp.Policy.parse({}) == gp.Policy()
    cfg = gp.Policy.parse({"epsilon": "0.1", "sensitivity": "1", "epsilon_limit": "0.3", "query_limit": "3"})
    ds = SimpleNamespace(released_count=2, epsilon_spent=Decimal("0.2"))
    assert not gp.exhausted(ds, cfg)  # The final 0.1 release is allowed, no float rounding error
    ds.epsilon_spent += Decimal("0.1")
    assert gp.exhausted(ds, cfg)
    assert gp.exhausted(ds, gp.Policy(query_limit=2))
    assert gp.exhausted(ds, gp.Policy(query_limit=0))
    assert not gp.exhausted(ds, gp.Policy())


def test_laplace_statistics_and_scaling(monkeypatch):
    # Seed ONLY the test sampler. Production uses SystemRandom and offers no seed option.
    monkeypatch.setattr(gp, "RNG", random.Random(7301))
    draws = [gp.laplace(2.5) for _ in range(80000)]
    assert abs(statistics.mean(draws)) < 0.04
    assert statistics.variance(draws) == pytest.approx(2 * 2.5**2, rel=0.03)
    assert statistics.mean(abs(x) for x in draws) == pytest.approx(2.5, rel=0.02)
    assert sum(x > 0 for x in draws) / len(draws) == pytest.approx(0.5, abs=0.01)
    assert sum(abs(x) > 5 for x in draws) / len(draws) == pytest.approx(math.exp(-2), abs=0.005)


def test_every_numeric_cell_and_metric_noised_once_helpers_removed(monkeypatch):
    scales = []

    def fake(scale):
        scales.append(scale)
        return 0.375

    monkeypatch.setattr(gp, "laplace", fake)
    source = doc(table([["public", 12, 7.123, 1.0, None, True]],
                       columns=("label", "n", "sum", "max", "empty", "flag"),
                       max_contribution={"sum": "max"}), metrics={"n": 12, "sum": 7.123})
    original = copy.deepcopy(source)
    cfg = gp.Policy.parse({"epsilon": "0.5", "sensitivity": "3"})
    out = gp.noisy_content(source, cfg)
    assert out["tables"][0]["rows"] == [["public", 12.38, 7.5, None, 1.38]]
    assert out["tables"][0]["columns"] == ["label", "n", "sum", "empty", "flag"]
    assert out["metrics"] == {"n": 12.38, "sum": 7.5}
    assert scales == [6.0] * 5  # One vector's L1 sensitivity, rather than epsilon spent independently per cell
    assert source == original
    assert gp.noisy_content(source, gp.Policy()) == gate_checks.release_content(source)


def test_noise_allows_negative_counts_and_refuses_extreme_numbers(monkeypatch):
    monkeypatch.setattr(gp, "laplace", lambda scale: -15.25)
    cfg = gp.Policy.parse({"epsilon": "1", "sensitivity": "1"})
    assert gp.noisy_content(doc(), cfg)["tables"][0]["rows"][0][1] == -3.25
    with pytest.raises(Unprocessable):
        gp.noisy_content(doc(metrics={"big": 10**13}), cfg)
    monkeypatch.setattr(gp, "laplace", lambda scale: float("inf"))
    with pytest.raises(Unprocessable):
        gp.noisy_content(doc(), cfg)


def test_invalid_stored_policy_does_not_fall_back_to_disabled():
    for text in ("broken", "[]", '{"epsilon": "1"}'):
        with pytest.raises(Unprocessable):
            gp.policy(SimpleNamespace(gate_policy_json=text))
