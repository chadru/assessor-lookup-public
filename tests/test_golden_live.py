"""Live golden-record regression as pytest (requires network).

Runs the same golden cases as tests/harness.py, one test per county case, so
CI or a dev can do:

    pytest -m network tests/test_golden_live.py

A failure here means a county website changed (or the parser broke) — run
``python tests/harness.py`` for the full report, and re-capture with
``--capture`` if the underlying property data legitimately changed.
"""

import pytest

from assessor_lookup.harness import (
    DEFAULT_CASES,
    compare_record,
    load_golden,
    run_lookup_case,
)

GOLDEN = load_golden()


@pytest.mark.network
@pytest.mark.parametrize("case", DEFAULT_CASES, ids=lambda c: c["id"])
def test_golden_case(case):
    gold = GOLDEN.get(case["id"])
    assert gold, f"no golden record for {case['id']} — run harness.py --capture"

    out = run_lookup_case(case)
    assert out["status"] == "success", (
        f"{case['id']}: lookup failed with {out['status']} "
        f"({out['record'].get('error', '')})")

    findings = compare_record(gold["expect"], out["record"])
    fails = [f for f in findings if f["level"] == "fail"]
    assert not fails, (
        f"{case['id']}: stable-field drift {fails} — county site changed or "
        f"parser regressed; if the data is legitimately different, re-capture.")


@pytest.mark.network
def test_discovery_ladder_live():
    from assessor_lookup.discovery import discover_county
    assert discover_county("Clear Creek", "co")["platform"] == "eagleweb"
    assert discover_county("El Paso", "co")["platform"] == "spatialest"
    assert discover_county("Boulder", "co")["platform"] == "co_parcel_api"
    assert discover_county("Notacounty", "co") is None
