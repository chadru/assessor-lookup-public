"""Tests for the jurisdictions/ driver layout and platform seam sharing."""

from unittest.mock import patch


class TestDriverLayout:
    def test_drivers_live_under_jurisdictions_us_co(self):
        from assessor_lookup.jurisdictions.us.co.adams import AdamsClient  # noqa: F401
        from assessor_lookup.jurisdictions.us.co.arapahoe import ArapahoeClient  # noqa: F401
        from assessor_lookup.jurisdictions.us.co.statewide import CoParcelClient  # noqa: F401

    def test_build_client_resolves_drivers_to_jurisdiction_modules(self):
        from assessor_lookup.platforms import build_client
        from assessor_lookup.jurisdictions.us.co.adams import AdamsClient

        entry = {
            "jurisdiction": {"country": "US", "state": "CO", "kind": "county",
                             "name": "Adams"},
            "platform": "arcgis",
            "config": {"driver": "us.co.adams"},
        }
        assert isinstance(build_client(entry), AdamsClient)

    def test_drivers_share_the_platform_http_seam(self):
        # Patching the ONE seam must intercept every ArcGIS driver's traffic.
        from assessor_lookup.platforms import arcgis
        from assessor_lookup.jurisdictions.us.co.adams import AdamsClient

        with patch.object(arcgis, "arcgis_get",
                          return_value={"features": []}) as seam:
            result = AdamsClient()._search_parcel("123 Main St")
        assert seam.called
        assert result == (None, "not_found")


class TestWhereClauseEscaping:
    """User-supplied identifiers must arrive escaped in ArcGIS where clauses."""

    def test_adams_parcel_id_is_escaped(self):
        from assessor_lookup.jurisdictions.us.co import adams

        captured = []
        with patch.object(adams, "_arcgis_get",
                          side_effect=lambda url, params=None, timeout=15:
                          captured.append(params) or {"features": []}):
            adams.AdamsClient()._fetch_parcel_by_id("12'34")
        assert captured, "no query issued"
        assert all("12''34" in p["where"] for p in captured)

    def test_arapahoe_parcel_id_is_escaped(self):
        from assessor_lookup.jurisdictions.us.co import arapahoe

        captured = []
        with patch.object(arapahoe, "_arcgis_get",
                          side_effect=lambda url, params=None, timeout=15:
                          captured.append(params) or {"features": []}):
            arapahoe.ArapahoeClient()._query_owner("19'75")
        assert captured, "no query issued"
        assert all("19''75" in p["where"] for p in captured)
