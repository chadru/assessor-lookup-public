"""Tests for assessor_lookup clients and the discrepancy checker."""

import json
import urllib.error
from io import BytesIO, StringIO
from unittest.mock import MagicMock, patch

import pytest

from assessor_lookup.assessor import (
    SpatialestClient,
    _extract_all_fields,
    _normalize_spatialest_parcel_id,
    _parse_sqft,
    _spatialest_request,
)
from assessor_lookup.checker import (
    _compare_field,
    _extract_mls_fields,
    _safe_float,
    _safe_int,
    check_public_records,
    print_discrepancy_report,
    resolve_discrepancies,
)
from assessor_lookup.assessor_jeffco import JeffcoClient


# --- Fixtures ---

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

class TestSpatialestSearchParsesPropertyId:
    @patch("assessor_lookup.assessor._spatialest_request")
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
    @patch("assessor_lookup.assessor._spatialest_request")
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

    @patch("assessor_lookup.assessor._spatialest_request")
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

    @patch("assessor_lookup.assessor._spatialest_request")
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

class TestPropertyNotFound:
    @patch("assessor_lookup.assessor._spatialest_request")
    def test_empty_search_results(self, mock_req):
        """Empty search results → status='not_found'."""
        mock_req.return_value = {"results": []}
        client = SpatialestClient()
        result = client.lookup("99999 Nonexistent Ave")
        assert result["status"] == "not_found"


class TestApiTimeout:
    @patch("assessor_lookup.assessor._spatialest_request")
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
    @patch("assessor_lookup.assessor.open_https")
    def test_spatialest_request_sets_origin_and_referer(self, mock_open):
        mock_open.return_value = _MockUrlopenResponse(b'{"ok": true}')

        _spatialest_request(
            "https://property.spatialest.com/co/elpaso/api/v1/recordcard/5509302001"
        )

        req = mock_open.call_args.args[0]
        assert req.get_header("Origin") == "https://property.spatialest.com"
        assert req.get_header("Referer") == "https://property.spatialest.com/co/elpaso/#/"

    @patch("assessor_lookup.assessor.require_https_url",
           side_effect=lambda url, **kwargs: url)
    @patch("assessor_lookup.assessor.build_https_opener")
    @patch("assessor_lookup.assessor.open_https")
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


class TestJeffcoMapping:
    def test_parse_legal_includes_subdivision_and_quarter(self):
        client = JeffcoClient()
        legal = client._parse_legal(
            {
                "block": "",
                "lot": "0011",
                "section": "24",
                "township": "03",
                "range": "69",
                "quarterSection": "NW",
                "landSquareFeet": 9669.0,
                "landAcres": 0.222,
            },
            subdivision="136800 CLUB CORNER",
        )
        assert legal == (
            "SECTION 24 TOWNSHIP 03 RANGE 69 QTR NW SUBDIVISIONCD 136800 "
            "SUBDIVISIONNAME CLUB CORNER BLOCK LOT 0011 SIZE: 9669 "
            "TRACT VALUE: .222"
        )

    @patch.object(JeffcoClient, "_get_endpoint")
    def test_fetch_details_prefers_ain_and_subdivision_name(self, mock_get):
        mock_get.side_effect = [
            {
                "propertyDetails": {
                    "pin": "300023827",
                    "ain": "39-242-22-014",
                    "displayName": "SAMPLE OWNER",
                    "neighborhood": "2106 CROWNHILL/WHEAT RIDGE",
                    "subdivision": "136800 CLUB CORNER",
                    "propertyAddress": "123 EXAMPLE RD ",
                }
            },
            {
                "inventorySummaryDetails": {
                    "bld1Design": "1 Story/Ranch",
                    "bld1YearBuilt": 1951,
                    "totalAboveGradeArea": 848,
                    "totalBasementArea": 0,
                    "totalGardenLevelArea": 0,
                    "totalAcres": 0.222,
                }
            },
            {
                "valueDetailsList": [{
                    "taxYear": "2025 payable 2026",
                    "totalActual": 519896.0,
                    "totalAssessed": 32493.0,
                }]
            },
            {
                "legalDescriptionDetailsList": [{
                    "lot": "0011",
                    "section": "24",
                    "township": "03",
                    "range": "69",
                    "quarterSection": "NW",
                    "landSquareFeet": 9669.0,
                    "landAcres": 0.222,
                }]
            },
            {
                "authorityMillLevyDetailsList": [{
                    "totalMillLevy": "88.2390",
                }]
            },
        ]

        client = JeffcoClient()
        result = client._fetch_details("uid-1", address="")

        assert result["parcel_number"] == "39-242-22-014"
        assert result["neighborhood"] == "CLUB CORNER"
        assert result["address"] == "123 Example Rd"


class TestEagleWebClient:
    """Tyler EagleWeb HTML-scraping client (Clear Creek CO)."""

    SUMMARY_HTML = """
      <td><strong>Parcel Number</strong> 0000-000-00-001</td>
      <td><strong>Tax Area Id</strong> Test District - 001</td>
      <td><strong>Situs Address</strong> 123 MAIN ST</td>
      <td><strong>Legal Summary</strong> Subdivision: TEST Block: 1 Lot: 1</td>
      <td><b>Owner Name</b> SAMPLE OWNER</td>
      <td><b>Owner Address</b> PO BOX 1 <br>TEST, CO 80000</td>
      <td align="left"><b>Actual</b> (2026)</td><td align="right">$658,180</td>
      <td align="left"><b>School Assessed</b></td><td align="right">$46,400</td>
      <td align="left"><b>Non-School Assessed</b></td><td align="right">$44,760</td>
      <caption><b>Mill Levy School</b>:25.785 <b>Mill Levy Non-School</b>:44.576</caption>
      <a href="account.jsp?accountNum=R000001&doc=DOC100.1">Land</a>
      <a href="account.jsp?accountNum=R000001&doc=DOC101.1">Residential</a>
    """

    DETAIL_HTML = """
      <span class="fieldLabel">Year Built</span><br/>
        <span class="field"><span class="text" >1910&nbsp;</span></span>
      <span class="fieldLabel">Design</span><br/>
        <span class="field"><span class="text" >DUPLEX&nbsp;</span></span>
      <span class="fieldLabel">Bedrooms</span><br/>
        <span class="field"><span class="text" >6&nbsp;</span></span>
      <span class="fieldLabel">Baths</span><br/>
        <span class="field"><span class="text" >3&nbsp;</span></span>
      <span class="fieldLabel">Type</span><br/>
        <span class="field"><span class="text" >TWO STORY&nbsp;</span></span>
      <h3>Abstract Code</h3><table>
        <tr><th>Abstract Code</th><th>Percent</th><th>Override Value</th>
            <th>Acres</th><th>Square Feet</th><th>Units</th></tr>
        <tr><td><span class="text">SINGLE FAM.RES-IMPROVEMTS&nbsp;</span></td>
            <td><span class="text" >100.0&nbsp;</span></td>
            <td><span class="text" >&nbsp;</span></td>
            <td><span class="text" >0&nbsp;</span></td>
            <td><span class="text" >3006&nbsp;</span></td>
            <td><span class="text" >0&nbsp;</span></td></tr>
      </table>
    """

    def _client(self):
        from assessor_lookup.assessor_eagleweb import EagleWebClient
        return EagleWebClient(base="https://example.test/eagleassessor")

    def test_split_address_strips_designation_and_direction(self):
        from assessor_lookup.assessor_eagleweb import _split_address
        assert _split_address("123 Main Blvd") == ("123", "Main")
        assert _split_address("123 W Colorado Blvd") == ("123", "Colorado")
        assert _split_address("400 Soda Creek Rd") == ("400", "Soda Creek")
        assert _split_address("Miner Street") == ("", "Miner")

    def test_parse_summary_fields(self):
        c = self._client()
        rec = c._parse_summary(self.SUMMARY_HTML, "R000001")
        assert rec["parcel_number"] == "0000-000-00-001"
        assert rec["owner"] == "SAMPLE OWNER"
        assert rec["situs_address"] == "123 MAIN ST"
        assert rec["market_value"] == "$658,180"
        assert rec["assessed_value"] == "44,760"
        assert rec["neighborhood"] == "TEST"
        # tax = 46400*25.785/1000 + 44760*44.576/1000 = 3191.64 -> $3,192
        assert rec["tax_amount"] == "$3,192"
        assert rec["tax_year"] == "2026"

    def test_parse_detail_building_fields(self):
        c = self._client()
        rec = c._parse_detail(self.DETAIL_HTML)
        assert rec["year_built"] == 1910
        assert rec["beds"] == 6          # not the two-column "TWO STORY" bleed
        assert rec["baths"] == 3.0
        assert rec["above_grade_sqft"] == 3006
        assert rec["style"] == "DUPLEX"

    def test_find_detail_doc_picks_residential(self):
        c = self._client()
        assert c._find_detail_doc(self.SUMMARY_HTML, "R000001") == "DOC101.1"

    def test_lookup_end_to_end_mocked(self):
        c = self._client()
        c._session_ready = True  # skip guest-login network calls
        results_html = ('<a href="account.jsp?accountNum=R000001">R000001</a>')
        with patch.object(c, "_post", return_value=results_html), \
             patch.object(c, "_get", side_effect=[self.SUMMARY_HTML,
                                                  self.DETAIL_HTML]):
            rec = c.lookup("123 Main St")
        assert rec["status"] == "success"
        assert rec["account_number"] == "R000001"
        assert rec["year_built"] == 1910
        assert rec["above_grade_sqft"] == 3006
        assert rec["owner"] == "SAMPLE OWNER"

    def test_lookup_not_found(self):
        c = self._client()
        c._session_ready = True
        with patch.object(c, "_post", return_value="<html>no matches</html>"):
            rec = c.lookup("99999 Nowhere Rd")
        assert rec["status"] == "not_found"


class TestDiscoveryLadder:
    """Auto-discovery probes API/platform sources in best-data-first order."""

    def test_spatialest_hit_is_tier1(self):
        from assessor_lookup import discovery
        with patch.object(discovery, "_probe",
                          side_effect=lambda url, t, needle=None: (True, "")):
            entry = discovery.discover_county("Weld", "co")
        assert entry["platform"] == "spatialest"
        assert entry["tier"] == 1
        assert entry["slug"] == "weld"

    def test_eagleweb_hit_when_no_spatialest(self):
        from assessor_lookup import discovery

        def fake_probe(url, timeout, needle=None):
            if "spatialest" in url:
                return (False, "")
            if "eagleassessor" in url:
                return (True, "Enter EagleWeb")
            return (False, "")

        with patch.object(discovery, "_probe", side_effect=fake_probe):
            entry = discovery.discover_county("Clear Creek", "co")
        assert entry["platform"] == "eagleweb"
        assert entry["base"].endswith("clear-creek.co.us/eagleassessor")
        assert entry["tier"] == 1

    def test_co_parcel_baseline_when_no_platform(self):
        from assessor_lookup import discovery
        with patch.object(discovery, "_probe",
                          side_effect=lambda url, t, needle=None: (False, "")):
            entry = discovery.discover_county("Pitkin", "co")
        assert entry["platform"] == "co_parcel_api"
        assert entry["tier"] == 2

    def test_unknown_county_returns_none(self):
        from assessor_lookup import discovery
        with patch.object(discovery, "_probe",
                          side_effect=lambda url, t, needle=None: (False, "")):
            # not in the CO statewide composite -> nothing
            assert discovery.discover_county("Notacounty", "co") is None

    def test_non_co_state_skips_parcel_baseline(self):
        from assessor_lookup import discovery
        with patch.object(discovery, "_probe",
                          side_effect=lambda url, t, needle=None: (False, "")):
            assert discovery.discover_county("Dallas", "tx") is None


class TestCoParcelClient:
    """CO statewide baseline: parcel/owner/value only, building fields None."""

    FEATURE_JSON = json.dumps({"features": [{"attributes": {
        "countyName": "CLEAR CREEK", "parcel_id": "000000000001",
        "account": "R000001", "situsAdd": "123 MAIN ST",
        "owner": "SAMPLE OWNER", "owner2": "",
        "legalDesc": "TEST SUB BLOCK 1 LOT 1", "landSqft": 8286,
        "landAcres": 0.19, "subName": "TEST", "zoningDesc": "RES",
        "salePrice": 200000, "saleDate": "2002-07-29",
        "apprValTot": 658180, "asedValTot": 44760, "URL": ""}}]})

    def _run_lookup(self, payload):
        from assessor_lookup.assessor_coparcel import CoParcelClient

        class _Resp:
            status = 200
            def read(self_):
                return payload.encode()
            def __enter__(self_):
                return self_
            def __exit__(self_, *a):
                return False

        with patch("assessor_lookup.assessor_coparcel.open_https",
                   return_value=_Resp()):
            return CoParcelClient(county="Clear Creek").lookup("123 Main St")

    def test_maps_owner_and_value_but_no_building_data(self):
        rec = self._run_lookup(self.FEATURE_JSON)
        assert rec["status"] == "success"
        assert rec["owner"] == "SAMPLE OWNER"
        assert rec["parcel_number"] == "000000000001"
        assert rec["market_value"] == "$658,180"
        assert rec["assessed_value"] == "$44,760"
        assert rec["lot_size_sqft"] == 8286
        # building characteristics are unavailable from this source
        assert rec["above_grade_sqft"] is None
        assert rec["beds"] is None
        assert rec["year_built"] is None
        assert "no building data" in rec["data_note"]

    def test_empty_result_is_not_found(self):
        rec = self._run_lookup(json.dumps({"features": []}))
        assert rec["status"] == "not_found"


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
    @patch("assessor_lookup.assessor._spatialest_request")
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
    @patch("assessor_lookup.assessor._spatialest_request")
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
        from assessor_lookup.assessor import SpatialestClient

    def test_import_checker(self):
        from assessor_lookup.checker import check_public_records

    def test_county_registry_loads(self):
        from assessor_lookup.assessor import _load_registry
        registry = _load_registry()
        assert "CO:El Paso" in registry
        assert registry["CO:El Paso"]["slug"] == "elpaso"

    def test_safe_float(self):
        assert _safe_float("2199") == 2199.0
        assert _safe_float("2,199") == 2199.0
        assert _safe_float(None) is None
        assert _safe_float("abc") is None

    def test_safe_int(self):
        assert _safe_int("3") == 3
        assert _safe_int("3.0") == 3
        assert _safe_int(None) is None


# --- Integration Test (requires network) ---

@pytest.mark.network
class TestLiveElPasoLookup:
    def test_real_api_call(self):
        """Real API call for known El Paso property with building data."""
        client = SpatialestClient(timeout=20)
        result = client.lookup("1675 W Garden of the Gods Rd", county_slug="elpaso", state="co")
        assert result["status"] == "success"
        assert result["parcel_number"] == "7326201002"
        assert result["year_built"] is not None
        print(f"  Live result: parcel={result['parcel_number']}, "
              f"Year={result['year_built']}")

    def test_lookup_returns_building_field_keys(self):
        """Lookup succeeds and exposes the building-field keys.

        The packaged institutional El Paso fixture does not necessarily expose
        residential characteristics, so the assertion checks that the lookup
        succeeds and the normalized keys are present. This also guards the
        419 page-token retry fix: without it the record-card GET 419s and the
        status would be 'api_error' rather than 'success'.
        """
        client = SpatialestClient(timeout=20)
        result = client.lookup("1675 W Garden of the Gods Rd", county_slug="elpaso", state="co")
        assert result["status"] == "success"
        assert "above_grade_sqft" in result
        if result["above_grade_sqft"] is not None:
            assert result["above_grade_sqft"] > 0
