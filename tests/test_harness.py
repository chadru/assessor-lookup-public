"""Unit tests for the benchmark/regression harness (no network)."""

import json
import stat

from assessor_lookup import harness
from assessor_lookup.harness import (
    COVERAGE_FIELDS,
    DEFAULT_CASES,
    GOLDEN_PATH,
    STABLE_FIELDS,
    _norm,
    compare_record,
    load_cases,
    load_golden,
    platform_aggregates,
    run_parser_bench,
)

_FULL_RECORD = {
    "status": "success", "above_grade_sqft": 2306, "basement_sqft": 735,
    "beds": 3, "baths": 2.5, "year_built": 1978, "owner": "SAMPLE OWNER",
    "legal": "LOT 1 TEST SUB", "parcel_number": "0000000001",
    "market_value": "$576,169", "assessed_value": "$576,169",
    "tax_amount": "$2,329", "assessor_url": "https://example.test/property/1",
}


def _stub_lookup(record):
    return lambda case: {"status": record.get("status", "success"),
                         "elapsed_s": 0.1, "record": record}


class TestNorm:
    def test_money_string_equals_number(self):
        assert _norm("$658,180") == _norm(658180)

    def test_comma_number_equals_int(self):
        assert _norm("2,199") == _norm(2199)

    def test_none_and_empty_are_equal(self):
        assert _norm(None) == _norm("")

    def test_case_and_space_insensitive(self):
        assert _norm("  Sample  Owner ") == _norm("SAMPLE OWNER")

    def test_float_precision(self):
        assert _norm(2.5) == _norm("2.5")


class TestCompareRecord:
    GOLD = {"parcel_number": "0000-000-00-001", "above_grade_sqft": 3006,
            "beds": 6, "baths": 3.0, "year_built": 1910,
            "owner": "SAMPLE OWNER", "market_value": "$658,180"}

    def test_exact_match_no_findings(self):
        actual = dict(self.GOLD)
        assert compare_record(self.GOLD, actual) == []

    def test_stable_drift_is_fail(self):
        actual = dict(self.GOLD, above_grade_sqft=2500)
        findings = compare_record(self.GOLD, actual)
        assert [f for f in findings if f["level"] == "fail"
                and f["field"] == "above_grade_sqft"]

    def test_volatile_drift_is_warn_only(self):
        actual = dict(self.GOLD, owner="NEW OWNER LLC",
                      market_value="$700,000")
        findings = compare_record(self.GOLD, actual)
        assert findings
        assert all(f["level"] == "warn" for f in findings)

    def test_missing_stable_field_is_fail(self):
        actual = dict(self.GOLD)
        actual["year_built"] = None
        findings = compare_record(self.GOLD, actual)
        assert [f for f in findings if f["level"] == "fail"
                and f["field"] == "year_built"]

    def test_normalized_equivalents_do_not_flag(self):
        actual = dict(self.GOLD, market_value=658180,
                      parcel_number="0000-000-00-001 ")
        assert compare_record(self.GOLD, actual) == []

    def test_none_golden_field_matches_none_actual(self):
        # tier-2 baseline: building fields are pinned as None
        gold = {"above_grade_sqft": None, "beds": None}
        assert compare_record(gold, {"above_grade_sqft": None,
                                     "beds": None}) == []


class TestGoldenFile:
    def test_packaged_cases_are_non_residential(self):
        assert DEFAULT_CASES
        assert all(c.get("fixture_type") == "institutional"
                   for c in DEFAULT_CASES)

    def test_golden_records_exist_for_all_cases(self):
        golden = load_golden()
        assert golden, f"missing {GOLDEN_PATH} — run harness.py --capture"
        missing = [c["id"] for c in DEFAULT_CASES if c["id"] not in golden]
        assert not missing, f"cases without golden records: {missing}"

    def test_golden_records_are_well_formed(self):
        golden = load_golden()
        for cid, g in golden.items():
            assert g.get("platform"), cid
            assert isinstance(g.get("expect"), dict), cid
            # every golden pins at least one stable field key
            assert any(f in g["expect"] for f in STABLE_FIELDS), cid

    def test_golden_file_is_valid_json_on_disk(self):
        with open(GOLDEN_PATH) as f:
            json.load(f)


class TestAggregates:
    def test_platform_aggregates(self):
        rows = [
            {"platform": "spatialest", "status": "success", "elapsed_s": 1.0},
            {"platform": "spatialest", "status": "success", "elapsed_s": 3.0},
            {"platform": "eagleweb", "status": "success", "elapsed_s": 6.0},
            {"platform": "jeffco", "status": "timeout", "elapsed_s": 30.0},
        ]
        aggs = {a["platform"]: a for a in platform_aggregates(rows)}
        assert aggs["spatialest"]["mean_s"] == 2.0
        assert aggs["spatialest"]["max_s"] == 3.0
        assert "jeffco" not in aggs  # failures excluded from latency stats


class TestParserBench:
    def test_parser_bench_runs_offline(self):
        results = run_parser_bench(iterations=5, progress=lambda *_: None)
        assert len(results) == 3
        assert all(r["per_call_ms"] >= 0 for r in results)


class TestCoverageProbe:
    def test_full_coverage_is_check_ready(self, monkeypatch):
        monkeypatch.setattr(harness, "run_lookup_case",
                            _stub_lookup(_FULL_RECORD))
        rep = harness.probe_coverage("El Paso", "co", address="1 Main St")
        assert rep["status"] == "success"
        assert rep["coverage"] == f"{len(COVERAGE_FIELDS)}/{len(COVERAGE_FIELDS)}"
        assert rep["building_coverage"] == "5/5"
        assert rep["check_ready"] is True
        assert rep["missing"] == []

    def test_thin_coverage_flags_not_check_ready(self, monkeypatch):
        thin = {"status": "success", "owner": "CITY OF BOULDER",
                "legal": "L", "parcel_number": "P", "assessed_value": "$1",
                "above_grade_sqft": None, "beds": None, "baths": None,
                "year_built": None, "basement_sqft": None}
        monkeypatch.setattr(harness, "run_lookup_case", _stub_lookup(thin))
        rep = harness.probe_coverage("Boulder", "co", address="1 Broadway")
        assert rep["check_ready"] is False
        assert "above_grade_sqft" in rep["missing"]
        assert rep["building_coverage"] == "0/5"

    def test_resolved_only_without_sample(self):
        rep = harness.probe_coverage("El Paso", "co")
        assert rep["status"] == "resolved_only"
        assert rep["platform"] == "spatialest"


class TestOnboarding:
    def test_onboard_persists_case_and_golden(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ASSESSOR_LOOKUP_HOME", str(tmp_path))
        monkeypatch.setattr(harness, "run_lookup_case",
                            _stub_lookup(_FULL_RECORD))
        rep = harness.onboard_county("El Paso", "co", parcel="0000000001")
        assert rep["onboarded"] is True
        assert rep["case_id"] == "elpaso-parcel"
        # persisted and picked up by load_cases / load_golden
        assert any(c["id"] == "elpaso-parcel" for c in harness.load_user_cases())
        assert "elpaso-parcel" in harness.load_golden()
        assert any(c["id"] == "elpaso-parcel" for c in load_cases())
        assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700
        assert stat.S_IMODE(
            (tmp_path / "user_cases.json").stat().st_mode) == 0o600
        assert stat.S_IMODE(
            (tmp_path / "user_golden.json").stat().st_mode) == 0o600

    def test_loading_user_data_remediates_existing_permissions(
            self, monkeypatch, tmp_path):
        monkeypatch.setenv("ASSESSOR_LOOKUP_HOME", str(tmp_path))
        cases = tmp_path / "user_cases.json"
        golden = tmp_path / "user_golden.json"
        cases.write_text("[]")
        golden.write_text("{}")
        tmp_path.chmod(0o755)
        cases.chmod(0o644)
        golden.chmod(0o644)

        assert harness.load_user_cases() == []
        harness.load_golden()

        assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700
        assert stat.S_IMODE(cases.stat().st_mode) == 0o600
        assert stat.S_IMODE(golden.stat().st_mode) == 0o600

    def test_onboard_no_capture_skips_golden(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ASSESSOR_LOOKUP_HOME", str(tmp_path))
        monkeypatch.setattr(harness, "run_lookup_case",
                            _stub_lookup(_FULL_RECORD))
        rep = harness.onboard_county("El Paso", "co", parcel="1", capture=False)
        assert rep["onboarded"] is False
        assert harness.load_user_cases() == []

    def test_onboard_failed_lookup_not_persisted(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ASSESSOR_LOOKUP_HOME", str(tmp_path))
        monkeypatch.setattr(harness, "run_lookup_case",
                            _stub_lookup({"status": "not_found"}))
        rep = harness.onboard_county("Nowhere", "co", address="1 X")
        assert rep["onboarded"] is False
        assert harness.load_user_cases() == []

    def test_onboard_capture_failure_is_not_reported_as_success(
            self, monkeypatch, tmp_path):
        monkeypatch.setenv("ASSESSOR_LOOKUP_HOME", str(tmp_path))
        responses = iter([
            {"status": "success", "elapsed_s": 0.1, "record": _FULL_RECORD},
            {"status": "api_error", "elapsed_s": 0.1,
             "record": {"status": "api_error", "error": "transient"}},
        ])
        monkeypatch.setattr(harness, "run_lookup_case", lambda case: next(responses))

        rep = harness.onboard_county("El Paso", "co", parcel="0000000001")

        assert rep["onboarded"] is False
        assert rep["status"] == "api_error"
        assert harness.load_user_cases() == []

    def test_load_cases_dedups_default_ids(self, monkeypatch, tmp_path):
        # a user case reusing a default id must not duplicate the default
        monkeypatch.setenv("ASSESSOR_LOOKUP_HOME", str(tmp_path))
        (tmp_path).mkdir(parents=True, exist_ok=True)
        import json as _json
        (tmp_path / "user_cases.json").write_text(_json.dumps(
            [{"id": "elpaso-address", "county": "El Paso", "state": "co"}]))
        ids = [c["id"] for c in load_cases()]
        assert ids.count("elpaso-address") == 1
