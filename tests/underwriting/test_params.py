"""Params loader -- frozen structure, drift guard.

Spec: docs/specs/underwriting/grading-engine-core.md, R2, section 8.
"""
import dataclasses

import pytest


def test_params_load_and_freeze(params):
    assert params.factor_count == 22
    assert len(params.factors) == 22

    with pytest.raises(dataclasses.FrozenInstanceError):
        params.engine_version = "9.9.9"

    with pytest.raises(dataclasses.FrozenInstanceError):
        params.factors[0].upper = 999.0

    with pytest.raises(TypeError):
        params.rollup["bcq"]["core"]["gross_margin"] = 0.99

    with pytest.raises(TypeError):
        params.company_sizes["micro"]["max"] = 999


def test_params_version_pinned(params):
    # Guards silent YAML drift -- if this ever changes, every test that
    # reconciles against the golden set needs to be re-verified against a
    # freshly-extracted fixture before the version bump is trusted.
    #
    # wb-v0-20260728 -> wb-v1-20260917 (T3, HANDOFF-03): gate 3 enabled at
    # threshold 13, working_days_per_month 21 -> 22. The golden set (10,000
    # rows / 270,000 assertions, test_golden_set.py) was re-verified against
    # this version and passes at 0 failures -- this pin was left stale by
    # that change and is corrected here, not re-litigated.
    #
    # wb-v1-20260917 -> wb-v2-20261005: loan_constraints.max_vnd 1,000,000,000
    # -> 100,000,000. No factor, curve or rollup changed. The golden set is
    # unaffected because tests/underwriting/conftest.py widens max_vnd to 5bn
    # for golden-set reconciliation (its loans are 200M-5bn).
    assert params.params_version == "wb-v2-20261005"
    assert params.engine_version == "1.0.0"


from app.underwriting.grading import load_params


def test_loan_constraints_range():
    # The requested-amount range offered to SMEs: 20,000,000 - 100,000,000 VND.
    # Every API bound (app/rates/schemas.py, app/projects/engine_constraints.py)
    # and the engine's own input validation read from here, so this pin is the
    # single guard against the range drifting.
    # We call load_params() directly rather than the golden-set-widened `params` fixture.
    p = load_params()
    assert int(p.loan_constraints["min_vnd"]) == 20_000_000
    assert int(p.loan_constraints["max_vnd"]) == 100_000_000
