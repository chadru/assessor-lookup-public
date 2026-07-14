"""Tests for jurisdictions/us/co/statewide.py (CO parcel baseline)."""

import json
from io import BytesIO
from unittest.mock import patch


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
        from assessor_lookup.jurisdictions.us.co.statewide import CoParcelClient

        class _Resp:
            status = 200
            def read(self_):
                return payload.encode()
            def __enter__(self_):
                return self_
            def __exit__(self_, *a):
                return False

        with patch("assessor_lookup.platforms.arcgis.open_https",
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


