"""Tests for the shared ArcGIS REST primitives (platforms/arcgis.py) and the
jurisdiction driver layout (jurisdictions/us/co/)."""

import json
from unittest.mock import patch

import pytest


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class TestArcgisGet:
    def test_encodes_params_and_parses_json(self):
        from assessor_lookup.platforms import arcgis

        captured = {}

        def fake_open(req, timeout, allowed_hosts):
            captured["url"] = req.full_url
            captured["timeout"] = timeout
            captured["allowed_hosts"] = allowed_hosts
            return _FakeResponse(json.dumps({"features": [1, 2]}).encode())

        with patch.object(arcgis, "open_https", side_effect=fake_open):
            out = arcgis.arcgis_get(
                "https://services3.arcgis.com/x/query",
                ("services3.arcgis.com",),
                params={"where": "PARCEL='42'", "f": "json"},
                timeout=7,
            )
        assert out == {"features": [1, 2]}
        assert "where=PARCEL%3D%2742%27" in captured["url"]
        assert captured["timeout"] == 7
        assert captured["allowed_hosts"] == ("services3.arcgis.com",)

    def test_rejects_disallowed_host(self):
        from assessor_lookup.platforms import arcgis

        with pytest.raises(ValueError):
            arcgis.arcgis_get("https://evil.example/query",
                              ("services3.arcgis.com",))


class TestEscapeSqlLiteral:
    def test_escapes_single_quote(self):
        from assessor_lookup.platforms import arcgis

        assert arcgis.escape_sql_literal("O'Brien") == "O''Brien"

    def test_none_and_empty(self):
        from assessor_lookup.platforms import arcgis

        assert arcgis.escape_sql_literal(None) == ""
        assert arcgis.escape_sql_literal("") == ""
