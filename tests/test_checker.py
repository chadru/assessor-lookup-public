"""Tests for checker.py: MLS extraction, comparison, reporting, resolve."""

from io import StringIO
from unittest.mock import patch

import pytest

from assessor_lookup.checker import (
    _compare_field,
    _extract_mls_fields,
    _safe_float,
    _safe_int,
    check_public_records,
    print_discrepancy_report,
    resolve_discrepancies,
)


MOCK_SEARCH_RESPONSE = {
    "results": [
        {
            "id": "R0000001",
            "parcelactive": True,
            "par": "R0000001",
        }
    ]
}

MOCK_RECORDCARD_RESPONSE = {
    "parcel": {
        "header": {
            "marketvalueheader": "$350,000",
        },
        "sections": {
            "Building": {
                "AboveGradeArea": "2250",
                "TotalBSMT": "924",
                "FinishedBSMT": "500",
                "YearBlt": "2020",
                "Beds": "4",
                "Baths": "2.5",
                "ResStyle": "2-Story",
                "zone": "R-1 6",
                "legalText": "LOT 1 BLK 1 TEST SUB",
                "owners_list": '["SAMPLE OWNER"]',
                "TaxAmount": "$1,298",
                "TaxYear": "2025",
                "SubdivisionName": "Test Subdivision",
            }
        },
    }
}


MOCK_SUBJECT_ROW = {
    "Address": "100 Test Rd",
    "Main SqFt": "1200",
    "Upper SqFt": "999",
    "Bedrooms Total": "4",
    "Basement Beds": "1",
    "Total Baths": "3.1",
    "Basement Baths": "1.0",
    "Year Built": "2020",
    "Basement SqFt": "924",
}

MOCK_COMP_ROW = {
    "Address": "123 Example Ave",
    "Main SqFt": "1400",
    "Upper SqFt": "1014",
    "Bedrooms Total": "3",
    "Basement Beds": "0",
    "Total Baths": "2.1",
    "Basement Baths": "0",
    "Year Built": "2019",
    "Basement SqFt": "700",
}


# --- SpatialestClient Tests ---

class TestComparisonFlagsGla:
    def test_over_threshold_flags(self):
        """51sf diff → flag=True."""
        result = _compare_field(2199, 2250, threshold=50)
        assert result["flag"] is True
        assert result["diff"] == 51

    def test_within_threshold_no_flag(self):
        """49sf diff → flag=False."""
        result = _compare_field(2199, 2248, threshold=50)
        assert result["flag"] is False

    def test_exact_match_no_flag(self):
        result = _compare_field(2199, 2199, threshold=50)
        assert result["flag"] is False
        assert result["diff"] == 0


class TestComparisonExact:
    def test_match_no_flag(self):
        """Beds match → flag=False."""
        result = _compare_field(3, 3)
        assert result["flag"] is False

    def test_mismatch_flags(self):
        """Beds differ → flag=True."""
        result = _compare_field(3, 4)
        assert result["flag"] is True

    def test_none_values_no_flag(self):
        """None values → no flag."""
        result = _compare_field(None, 3)
        assert result["flag"] is False
        result2 = _compare_field(3, None)
        assert result2["flag"] is False


# --- Error Handling Tests ---

class TestMissingCsvColumns:
    def test_missing_column_returns_none(self):
        """Missing column → skip field, no crash."""
        row = {"Address": "123 Test St"}
        mls = _extract_mls_fields(row)
        assert mls["address"] == "123 Test St"
        assert mls["gla"] is None
        assert mls["beds"] is None
        assert mls["baths"] is None

    def test_schedule_number_is_available_for_assessor_lookup(self):
        row = {
            "Address": "100 Test Street",
            "Schedule Number": "1234-56-789",
            "Unit Number": "1",
        }
        mls = _extract_mls_fields(row)
        assert mls["parcel_id"] == "1234-56-789"
        assert mls["unit_number"] == "1"


class _ParcelFirstAssessor:
    capabilities = {"parcel_lookup": True, "building_fields": True}

    def __init__(self):
        self.parcel_calls = []
        self.address_calls = []

    def lookup_by_parcel(self, parcel_id, **kwargs):
        self.parcel_calls.append((parcel_id, kwargs))
        return {
            "status": "success",
            "address": "100 TEST ST 1",
            "above_grade_sqft": 1131,
            "year_built": 1984,
            "baths": 2,
            "parcel_number": "0123456789000",
        }

    def lookup(self, address, **kwargs):
        self.address_calls.append((address, kwargs))
        return {"status": "not_found", "address": address}


class TestPublicRecordsLookupRouting:
    def test_spatialest_public_records_prefers_parcel_lookup(self):
        assessor = _ParcelFirstAssessor()
        row = {
            "Address": "100 Test Street",
            "County": "Denver",
            "State": "CO",
            "Schedule Number": "1234-56-789",
            "Unit Number": "1",
            "Main SqFt": "1131",
            "Year Built": "1984",
            "Total Baths": "2",
        }
        with patch(
            "assessor_lookup.checker._get_client",
            return_value=(
                assessor,
                {"platform": "spatialest", "slug": "denver", "state": "co"},
            ),
        ):
            results = check_public_records(row, [], county="Denver")

        assert results[0]["status"] == "success"
        assert assessor.parcel_calls[0][0] == "1234-56-789"
        assert assessor.address_calls == []

    def test_county_public_records_prefers_parcel_lookup(self):
        assessor = _ParcelFirstAssessor()
        row = {
            "Address": "100 Test Way",
            "County": "Arapahoe",
            "State": "CO",
            "Parcel Number": "1977-19-4-18-007",
        }
        with patch(
            "assessor_lookup.checker._get_client",
            return_value=(
                assessor,
                {"platform": "arapahoe", "state": "co"},
            ),
        ):
            results = check_public_records(row, [], county="Arapahoe")

        assert results[0]["status"] == "success"
        assert assessor.parcel_calls[0][0] == "1977-19-4-18-007"
        assert assessor.address_calls == []

    def test_public_records_allows_parcel_lookup_without_address(self):
        assessor = _ParcelFirstAssessor()
        row = {
            "Address": "",
            "County": "Douglas",
            "State": "CO",
            "Parcel Number": "R0609804",
        }
        with patch(
            "assessor_lookup.checker._get_client",
            return_value=(
                assessor,
                {"platform": "spatialest", "slug": "douglas", "state": "co"},
            ),
        ):
            results = check_public_records(row, [], county="Douglas")

        assert results[0]["status"] == "success"
        assert results[0]["address"] == "100 TEST ST 1"
        assert assessor.parcel_calls[0][0] == "R0609804"
        assert assessor.address_calls == []


class TestEmptyCompsList:
    @patch("assessor_lookup.platforms.spatialest._spatialest_request")
    def test_only_subject_checked(self, mock_req):
        """No comps → only subject checked."""
        mock_req.side_effect = [
            MOCK_SEARCH_RESPONSE,
            MOCK_RECORDCARD_RESPONSE,
        ]
        results = check_public_records(MOCK_SUBJECT_ROW, [])
        assert len(results) == 1
        assert results[0]["label"] == "Subject"


# --- Report Format Tests ---

class TestReportFormat:
    @patch("assessor_lookup.platforms.spatialest._spatialest_request")
    def test_print_report_runs(self, mock_req, capsys):
        """Verify print_discrepancy_report output format."""
        mock_req.side_effect = [
            MOCK_SEARCH_RESPONSE,
            MOCK_RECORDCARD_RESPONSE,
        ]
        results = check_public_records(MOCK_SUBJECT_ROW, [])
        print_discrepancy_report(results)
        output = capsys.readouterr().out
        assert "Public Records Check" in output
        assert "100 Test Rd" in output


# --- Auto-resolve Tests ---

class TestResolveAutoMls:
    def test_auto_mls_chooses_mls(self):
        """auto_mode='mls' → all MLS values chosen."""
        results = [{
            "label": "Subject",
            "address": "100 Test Rd",
            "status": "success",
            "has_any_discrepancy": True,
            "fields": {
                "gla": {"mls": 2199, "assessor": 2250, "diff": 51, "flag": True},
                "beds": {"mls": 3, "assessor": 3, "flag": False},
                "baths": {"mls": 2.1, "assessor": 2.0, "diff": -0.1, "flag": True},
                "year_built": {"mls": 2020, "assessor": 2020, "flag": False},
                "basement_sqft": {"mls": 924, "assessor": 924, "diff": 0, "flag": False},
            },
        }]
        choices = resolve_discrepancies(results, auto_mode="mls")
        assert choices[("Subject", "gla")] == 2199
        assert choices[("Subject", "baths")] == 2.1


class TestResolveAutoAssessor:
    def test_auto_assessor_chooses_assessor(self):
        """auto_mode='assessor' → all assessor values chosen."""
        results = [{
            "label": "Subject",
            "address": "100 Test Rd",
            "status": "success",
            "has_any_discrepancy": True,
            "fields": {
                "gla": {"mls": 2199, "assessor": 2250, "diff": 51, "flag": True},
                "beds": {"mls": 3, "assessor": 3, "flag": False},
                "baths": {"mls": 2.1, "assessor": 2.0, "diff": -0.1, "flag": True},
                "year_built": {"mls": 2020, "assessor": 2020, "flag": False},
                "basement_sqft": {"mls": 924, "assessor": 924, "diff": 0, "flag": False},
            },
        }]
        choices = resolve_discrepancies(results, auto_mode="assessor")
        assert choices[("Subject", "gla")] == 2250
        assert choices[("Subject", "baths")] == 2.0


# --- Smoke Tests ---

class TestSmoke:
    def test_import_assessor(self):
        from assessor_lookup.platforms.spatialest import SpatialestClient

    def test_import_checker(self):
        from assessor_lookup.checker import check_public_records

    def test_county_registry_loads(self):
        from assessor_lookup.registry import load_registry
        registry = load_registry()
        assert "US/CO/county:el-paso" in registry
        assert registry["US/CO/county:el-paso"]["config"]["slug"] == "elpaso"

    def test_safe_float(self):
        assert _safe_float("2199") == 2199.0
        assert _safe_float("2,199") == 2199.0
        assert _safe_float(None) is None
        assert _safe_float("abc") is None

    def test_safe_int(self):
        assert _safe_int("3") == 3
        assert _safe_int("3.0") == 3
        assert _safe_int(None) is None


