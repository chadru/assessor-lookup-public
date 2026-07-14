"""Tests for platforms/spatialest.py (Spatialest platform client)."""

import json
import urllib.error
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest

from assessor_lookup.platforms.spatialest import (
    SpatialestClient,
    _extract_all_fields,
    _normalize_spatialest_parcel_id,
    _parse_sqft,
    _spatialest_request,
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

DENVER_RECORDCARD_RESPONSE = {
    "parcel": {
        "header": {
            "PARID": "0123456789000",
            "FullAddress": "100 TEST ST 1",
            "OwnerNames": "SAMPLE OWNER",
            "ACTUAL_VAL": "$363,500",
        },
        "sections": [
            {"BLDG_SQFT": "1,131", "FIXBATH": "2", "ACTUALYEARBUILT": "1984"},
            {
                "LEGAL_DESC1": "TEST SUBDIVISION BLOCK 1 LOT 1",
                "ZONE1": "S-MU-3",
                "TAXYEAR": "2026",
            },
        ],
    }
}

class TestSpatialestSearchParsesPropertyId:
    @patch("assessor_lookup.platforms.spatialest._spatialest_request")
    def test_search_finds_property_id(self, mock_req):
        """Mock POST response, verify property_id extracted."""
        mock_req.side_effect = [
            MOCK_SEARCH_RESPONSE,
            MOCK_RECORDCARD_RESPONSE,
        ]
        client = SpatialestClient()
        result = client.lookup("100 Test Rd")
        assert result["status"] == "success"
        assert result["property_id"] == "R0000001"


class TestSpatialestRecordcardParsesFields:
    @patch("assessor_lookup.platforms.spatialest._spatialest_request")
    def test_recordcard_parses_building_data(self, mock_req):
        """Mock GET response, verify GLA/beds/baths/year extracted."""
        mock_req.side_effect = [
            MOCK_SEARCH_RESPONSE,
            MOCK_RECORDCARD_RESPONSE,
        ]
        client = SpatialestClient()
        result = client.lookup("100 Test Rd")
        assert result["above_grade_sqft"] == 2250
        assert result["basement_sqft"] == 924
        assert result["year_built"] == 2020
        assert result["beds"] == 4
        assert result["baths"] == 2.5

    @patch("assessor_lookup.platforms.spatialest._spatialest_request")
    def test_recordcard_parses_public_record_fields(self, mock_req):
        mock_req.side_effect = [
            MOCK_SEARCH_RESPONSE,
            MOCK_RECORDCARD_RESPONSE,
        ]
        client = SpatialestClient()
        result = client.lookup("100 Test Rd")
        assert result["owner"] == "SAMPLE OWNER"
        assert result["legal"] == "LOT 1 BLK 1 TEST SUB"
        assert result["zoning"] == "R-1 6"
        assert result["neighborhood"] == "Test Subdivision"

    @patch("assessor_lookup.platforms.spatialest._spatialest_request")
    def test_denver_recordcard_parses_public_record_fields(self, mock_req):
        mock_req.side_effect = [
            {"id": "0621323330000"},
            DENVER_RECORDCARD_RESPONSE,
        ]
        client = SpatialestClient()
        result = client.lookup_by_parcel("1234-56-789", county_slug="denver")

        search_payload = mock_req.call_args_list[0].kwargs["data"]
        assert search_payload["filters"]["term"] == "0123456789"
        assert result["status"] == "success"
        assert result["address"] == "100 TEST ST 1"
        assert result["above_grade_sqft"] == 1131
        assert result["year_built"] == 1984
        assert result["baths"] == 2.0
        assert result["owner"] == "SAMPLE OWNER"
        assert result["legal"] == "TEST SUBDIVISION BLOCK 1 LOT 1"
        assert result["parcel_number"] == "0123456789000"
        assert result["zoning"] == "S-MU-3"


class TestDenverParcelNormalization:
    def test_denver_mls_schedule_number_normalizes_for_spatialest(self):
        assert _normalize_spatialest_parcel_id(
            "1234-56-789", "denver") == "0123456789"

    def test_non_denver_parcel_is_unchanged(self):
        assert _normalize_spatialest_parcel_id(
            "5317-10-1023", "elpaso") == "5317-10-1023"


class TestFieldNameVariants:
    def test_parse_sqft_abovegradearea(self):
        """Test _parse_sqft with different county field names."""
        fields = {"AboveGradeArea": "2250"}
        assert _parse_sqft(fields, ["AboveGradeArea", "SFLA"]) == 2250

    def test_parse_sqft_sfla(self):
        fields = {"SFLA": "1800"}
        assert _parse_sqft(fields, ["AboveGradeArea", "SFLA"]) == 1800

    def test_parse_sqft_with_comma(self):
        fields = {"BLDG_SQFT": "2,199"}
        assert _parse_sqft(fields, ["BLDG_SQFT"]) == 2199

    def test_parse_sqft_with_unit(self):
        fields = {"TotalArea": "1500 sqft"}
        assert _parse_sqft(fields, ["TotalArea"]) == 1500

    def test_parse_sqft_none_when_missing(self):
        fields = {"other_field": "999"}
        assert _parse_sqft(fields, ["AboveGradeArea", "SFLA"]) is None


class TestExtractAllFieldsNested:
    def test_flat_dict(self):
        """Test recursive section flattening — flat dict."""
        sections = {"YearBlt": "2020", "Beds": "3"}
        out = {}
        _extract_all_fields(sections, out)
        assert out["YearBlt"] == "2020"
        assert out["Beds"] == "3"

    def test_nested_dict(self):
        """Test recursive section flattening — nested dict."""
        sections = {
            "Building": {
                "AboveGradeArea": "2000",
                "Inner": {"YearBlt": "2015"},
            }
        }
        out = {}
        _extract_all_fields(sections, out)
        assert out["AboveGradeArea"] == "2000"
        assert out["YearBlt"] == "2015"

    def test_list_sections(self):
        """Test recursive section flattening — list format (Denver)."""
        sections = [
            {"Beds": "3", "Baths": "2"},
            {"YearBlt": "2018"},
        ]
        out = {}
        _extract_all_fields(sections, out)
        assert out["Beds"] == "3"
        assert out["YearBlt"] == "2018"

    def test_skips_empty_values(self):
        sections = {"YearBlt": "2020", "Empty": "", "Null": None}
        out = {}
        _extract_all_fields(sections, out)
        assert "YearBlt" in out
        assert "Empty" not in out
        assert "Null" not in out


# --- Comparison Logic Tests ---

class TestPropertyNotFound:
    @patch("assessor_lookup.platforms.spatialest._spatialest_request")
    def test_empty_search_results(self, mock_req):
        """Empty search results → status='not_found'."""
        mock_req.return_value = {"results": []}
        client = SpatialestClient()
        result = client.lookup("99999 Nonexistent Ave")
        assert result["status"] == "not_found"


class TestApiTimeout:
    @patch("assessor_lookup.platforms.spatialest._spatialest_request")
    def test_timeout_returns_status(self, mock_req):
        """URLError → status='timeout', warning printed."""
        mock_req.side_effect = urllib.error.URLError("timed out")
        client = SpatialestClient()
        result = client.lookup("100 Test Rd")
        assert result["status"] == "timeout"


class _MockUrlopenResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeOpener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, req, timeout=None):
        self.requests.append(req)
        return self.responses.pop(0)


class TestSpatialestRequestHeaders:
    @patch("assessor_lookup.platforms.spatialest.open_https")
    def test_spatialest_request_sets_origin_and_referer(self, mock_open):
        mock_open.return_value = _MockUrlopenResponse(b'{"ok": true}')

        _spatialest_request(
            "https://property.spatialest.com/co/elpaso/api/v1/recordcard/5509302001"
        )

        req = mock_open.call_args.args[0]
        assert req.get_header("Origin") == "https://property.spatialest.com"
        assert req.get_header("Referer") == "https://property.spatialest.com/co/elpaso/#/"

    @patch("assessor_lookup.platforms.spatialest.require_https_url",
           side_effect=lambda url, **kwargs: url)
    @patch("assessor_lookup.platforms.spatialest.build_https_opener")
    @patch("assessor_lookup.platforms.spatialest.open_https")
    def test_spatialest_request_retries_with_csrf_on_page_token(
            self, mock_open, mock_build_opener, _mock_validate):
        err = urllib.error.HTTPError(
            "https://property.spatialest.com/co/denver/api/v1/recordcard/1",
            419,
            "unknown status",
            {},
            BytesIO(b'{"message":"Page token mismatch."}'),
        )
        mock_open.side_effect = err
        opener = _FakeOpener([
            _MockUrlopenResponse(
                b'<meta name="csrf-token" content="token-123">'
            ),
            _MockUrlopenResponse(b'{"ok": true}'),
        ])
        mock_build_opener.return_value = opener

        result = _spatialest_request(
            "https://property.spatialest.com/co/denver/api/v1/recordcard/1"
        )

        assert result == {"ok": True}
        retry_req = opener.requests[1]
        assert retry_req.get_header("X-csrf-token") == "token-123"




class TestConstructorConfig:
    """The factory bakes jurisdiction config in; per-call kwargs still win."""

    def _capture_url(self, client, address, **kwargs):
        calls = []

        def fake(url, data=None, timeout=10):
            calls.append(url)
            return {"results": []}

        with patch("assessor_lookup.platforms.spatialest._spatialest_request",
                   side_effect=fake):
            client.lookup(address, **kwargs)
        return calls[0]

    def test_lookup_uses_instance_slug_and_state(self):
        client = SpatialestClient(county_slug="denver", state="co")
        assert "/co/denver/" in self._capture_url(client, "123 Main St")

    def test_explicit_kwarg_overrides_instance(self):
        client = SpatialestClient(county_slug="denver", state="co")
        url = self._capture_url(client, "123 Main St", county_slug="elpaso")
        assert "/co/elpaso/" in url

    def test_default_instance_config_is_elpaso(self):
        client = SpatialestClient()
        assert "/co/elpaso/" in self._capture_url(client, "123 Main St")
