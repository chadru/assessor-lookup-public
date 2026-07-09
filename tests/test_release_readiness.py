"""Release-gate regressions for public CLI, data mappings, and lookup safety."""

import json
from argparse import Namespace
from unittest.mock import patch
import urllib.request

from assessor_lookup import cli
from assessor_lookup.assessor_adams import AdamsClient
from assessor_lookup.assessor_arapahoe import ArapahoeClient
from assessor_lookup.assessor_coparcel import CoParcelClient
from assessor_lookup.assessor_eagleweb import EagleWebClient
from assessor_lookup.assessor_jeffco import JeffcoClient
from assessor_lookup.assessor import SpatialestClient
from assessor_lookup.checker import _extract_mls_fields, detect_county
from assessor_lookup.matching import select_candidate
from assessor_lookup.network import (
    SameOriginHTTPSRedirectHandler,
    require_https_url,
)


def test_outbound_url_guard_accepts_expected_https_host():
    url = "https://property.spatialest.com/co/demo/api/v2/search"
    assert require_https_url(
        url, allowed_hosts=("property.spatialest.com",)) == url


def test_outbound_url_guard_rejects_unsafe_targets():
    for url in (
        "http://property.spatialest.com/co/demo",
        "https://user:pass@property.spatialest.com/co/demo",
        "https://127.0.0.1/service",
        "https://2130706433/service",
        "https://169.254.169.254/latest/meta-data",
    ):
        try:
            require_https_url(url)
        except ValueError:
            pass
        else:
            raise AssertionError(f"unsafe URL accepted: {url}")


def test_outbound_url_guard_rejects_local_dns_result():
    fake = [(2, 1, 6, "", ("127.0.0.1", 443))]
    with patch("assessor_lookup.network.socket.getaddrinfo",
               return_value=fake):
        try:
            require_https_url("https://public-looking.example/path",
                              resolve_host=True)
        except ValueError as exc:
            assert "private address" in str(exc)
        else:
            raise AssertionError("private DNS result accepted")


def test_redirect_handler_rejects_off_origin_and_downgrade():
    handler = SameOriginHTTPSRedirectHandler(("example.com",))
    request = urllib.request.Request("https://example.com/start")
    for target in (
        "https://169.254.169.254/latest/meta-data",
        "http://example.com/insecure",
        "https://other.example/elsewhere",
    ):
        try:
            handler.redirect_request(request, None, 302, "Found", {}, target)
        except ValueError:
            pass
        else:
            raise AssertionError(f"unsafe redirect accepted: {target}")


def test_eagleweb_rejects_localhost_base():
    try:
        EagleWebClient(base="https://localhost/eagleassessor")
    except ValueError:
        pass
    else:
        raise AssertionError("localhost EagleWeb base accepted")


def test_reso_row_maps_to_normalized_mls_fields():
    row = {
        "UnparsedAddress": "123 Main St Unit 4",
        "LivingArea": "2,048",
        "BedroomsTotal": "4",
        "BathroomsTotalInteger": "3",
        "YearBuilt": "2001",
        "CountyOrParish": "Adams",
        "StateOrProvince": "CO",
        "ParcelNumber": "000000000001",
        "BelowGradeFinishedArea": "600",
        "BelowGradeUnfinishedArea": "400",
    }

    assert detect_county(row) == ("Adams", "co")
    assert _extract_mls_fields(row) == {
        "address": "123 Main St Unit 4",
        "parcel_id": "000000000001",
        "unit_number": "",
        "gla": 2048,
        "beds": 4,
        "baths": 3.0,
        "year_built": 2001,
        "basement_sqft": 1000,
    }


def test_spatialest_selects_exact_normalized_address_match():
    search = {"results": [
        {"id": "wrong", "parcelactive": True, "address": "999 Other St"},
        {"id": "right", "parcelactive": True, "address": "123 Main Street"},
    ]}
    card = {"parcel": {"header": {}, "sections": {}}}
    with patch("assessor_lookup.assessor._spatialest_request",
               side_effect=[search, card]):
        out = SpatialestClient().lookup("123 Main St")

    assert out["status"] == "success"
    assert out["property_id"] == "right"


def test_spatialest_refuses_ambiguous_candidates_without_addresses():
    search = {"results": [
        {"id": "one", "parcelactive": True},
        {"id": "two", "parcelactive": True},
    ]}
    with patch("assessor_lookup.assessor._spatialest_request",
               return_value=search) as request:
        out = SpatialestClient().lookup("123 Main St")

    assert out["status"] == "ambiguous"
    assert request.call_count == 1


def test_spatialest_refuses_direct_id_with_exposed_wrong_address():
    search = {"id": "wrong", "address": "999 Other St"}
    with patch("assessor_lookup.assessor._spatialest_request",
               return_value=search) as request:
        out = SpatialestClient().lookup("123 Main St")
    assert out["status"] == "not_found"
    assert request.call_count == 1


def test_spatialest_validates_identityless_hit_against_card_address():
    card = {"parcel": {"header": {"FullAddress": "999 Other St"},
                       "sections": {}}}
    with patch("assessor_lookup.assessor._spatialest_request",
               side_effect=[{"results": [{"id": "one"}]}, card]):
        out = SpatialestClient().lookup("123 Main St")
    assert out["status"] == "not_found"


def test_spatialest_malformed_card_returns_parse_error():
    with patch("assessor_lookup.assessor._spatialest_request",
               side_effect=[{"id": "one"}, []]):
        out = SpatialestClient().lookup("123 Main St")
    assert out["status"] == "parse_error"


def test_candidate_matching_refuses_single_exposed_mismatch():
    candidate, status = select_candidate(
        [{"id": "wrong", "address": "999 Other St"}],
        address="123 Main St")
    assert candidate is None
    assert status == "not_found"


def test_candidate_matching_accepts_unique_parcel_after_normalization():
    candidate, status = select_candidate(
        [{"parcel_id": "000-000-000-001"}], parcel="000000000001")
    assert candidate is not None
    assert status == "success"


def test_adams_search_selects_exact_candidate():
    response = {"features": [
        {"attributes": {"PIN": "wrong", "concataddr1": "123 Main Ct"}},
        {"attributes": {"PIN": "right", "concataddr1": "123 Main Street"}},
    ]}
    with patch("assessor_lookup.assessor_adams._arcgis_get",
               return_value=response):
        parcel, status = AdamsClient()._search_parcel("123 Main St")
    assert status == "success"
    assert parcel["PIN"] == "right"


def test_jeffco_search_selects_exact_candidate():
    result = {"items": [
        {"uniquePropertyId": "wrong", "propertyAddress": "999 Other St"},
        {"uniquePropertyId": "right", "propertyAddress": "123 Main Street"},
    ]}
    assert JeffcoClient()._extract_property_id(result, "123 Main St") == (
        "right", "success")


def test_arapahoe_geocoder_selects_exact_candidate():
    response = {"candidates": [
        {"address": "999 Other St, Denver, CO", "score": 99,
         "location": {"x": 1, "y": 2}},
        {"address": "123 Main Street, Denver, CO", "score": 95,
         "location": {"x": 3, "y": 4}},
    ]}
    with patch("assessor_lookup.assessor_arapahoe._arcgis_get",
               return_value=response):
        assert ArapahoeClient()._geocode("123 Main St") == ((3, 4), "success")


def test_eagleweb_refuses_multiple_address_accounts():
    client = EagleWebClient(base="https://example.test/eagleassessor")
    client._session_ready = True
    html = ('<a href="account.jsp?accountNum=R1">one</a>'
            '<a href="account.jsp?accountNum=R2">two</a>')
    with patch.object(client, "_post", return_value=html):
        out = client.lookup("123 Main St")
    assert out["status"] == "ambiguous"


def test_eagleweb_validates_single_account_against_fetched_situs():
    client = EagleWebClient(base="https://example.test/eagleassessor")
    client._session_ready = True
    html = '<a href="account.jsp?accountNum=R1">one</a>'
    with patch.object(client, "_post", return_value=html), \
         patch.object(client, "_fetch_account", return_value={
             "status": "success", "account_number": "R1",
             "situs_address": "999 Other St"}):
        out = client.lookup("123 Main St")
    assert out["status"] == "not_found"


def test_coparcel_refuses_multiple_nonmatching_features():
    payload = {"features": [
        {"attributes": {"situsAdd": "123 Main Ct", "parcel_id": "1"}},
        {"attributes": {"situsAdd": "999 Other St", "parcel_id": "2"}},
    ]}
    response = patch("assessor_lookup.assessor_coparcel.open_https")
    with response as urlopen:
        urlopen.return_value.__enter__.return_value.read.return_value = (
            json.dumps(payload).encode())
        out = CoParcelClient(county="Adams").lookup("123 Main St")
    assert out["status"] == "ambiguous"


def test_coparcel_malformed_json_shape_returns_parse_error():
    response = patch("assessor_lookup.assessor_coparcel.open_https")
    with response as urlopen:
        urlopen.return_value.__enter__.return_value.read.return_value = b"[1]"
        out = CoParcelClient(county="Adams").lookup("123 Main St")
    assert out["status"] == "parse_error"


def test_json_lookup_failure_returns_nonzero(capsys):
    args = Namespace(address="x", parcel=None, county="x", state="co",
                     verbose=False, json=True)
    with patch("assessor_lookup.lookup",
               return_value={"status": "not_found", "error": "missing"}):
        status = cli._cmd_lookup(args)

    assert status == 1
    assert json.loads(capsys.readouterr().out)["status"] == "not_found"


def test_json_check_is_machine_readable(capsys, tmp_path):
    empty_csv = tmp_path / "empty.csv"
    empty_csv.write_text("")
    args = Namespace(subject_csv=str(empty_csv), comps_csv=None, county=None,
                     state="co", verbose=False, json=True)
    assert cli._cmd_check(args) == 0
    assert json.loads(capsys.readouterr().out) == []
