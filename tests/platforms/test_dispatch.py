"""Tests for platform registration/dispatch (PLATFORMS, build_client,
capabilities, module layout) and checker._get_client routing."""

import pytest

from assessor_lookup import registry as reg


def _entry(name, state, platform, config=None, kind="county"):
    return {
        "jurisdiction": {"country": "US", "state": state.upper(), "kind": kind,
                         "name": name},
        "platform": platform,
        "config": config or {},
        "access": {"mode": "public"},
    }


class TestPlatformsTable:
    def test_true_platforms_are_registered(self):
        from assessor_lookup.platforms import PLATFORMS

        assert {"spatialest", "eagleweb", "aumentum", "arcgis"} <= set(PLATFORMS)

    def test_every_packaged_platform_constructs(self):
        from assessor_lookup.platforms import PLATFORMS, build_client

        registry = reg.load_registry(user_path="/nonexistent")
        assert registry, "packaged registry must not be empty"
        for ckey, entry in registry.items():
            assert entry["platform"] in PLATFORMS, ckey
            client = build_client(entry)
            assert callable(client.lookup), ckey


class TestFactories:
    def test_spatialest(self):
        from assessor_lookup.platforms import build_client
        from assessor_lookup.platforms.spatialest import SpatialestClient

        client = build_client(_entry("El Paso", "co", "spatialest",
                                     {"slug": "elpaso"}))
        assert isinstance(client, SpatialestClient)

    def test_eagleweb_gets_base_from_config(self):
        from assessor_lookup.platforms import build_client
        from assessor_lookup.platforms.eagleweb import EagleWebClient

        client = build_client(_entry("Clear Creek", "co", "eagleweb",
                                     {"base": "https://assessor.example.gov/eagleassessor"}))
        assert isinstance(client, EagleWebClient)
        assert client.base == "https://assessor.example.gov/eagleassessor"

    def test_aumentum(self):
        from assessor_lookup.platforms import build_client
        from assessor_lookup.platforms.aumentum import JeffcoClient

        client = build_client(_entry("Jefferson", "co", "aumentum",
                                     {"base": "https://propertysearch.jeffco.us/api"}))
        assert isinstance(client, JeffcoClient)

    def test_arcgis_driver_resolution(self):
        from assessor_lookup.platforms import build_client
        from assessor_lookup.jurisdictions.us.co.adams import AdamsClient

        client = build_client(_entry("Adams", "co", "arcgis",
                                     {"driver": "us.co.adams"}))
        assert isinstance(client, AdamsClient)

    def test_arcgis_statewide_driver_carries_jurisdiction(self):
        from assessor_lookup.platforms import build_client
        from assessor_lookup.jurisdictions.us.co.statewide import CoParcelClient

        client = build_client(_entry("Gilpin", "co", "arcgis",
                                     {"driver": "us.co.statewide"}))
        assert isinstance(client, CoParcelClient)
        assert client.county == "Gilpin"
        assert client.state == "co"

    def test_unknown_platform_raises_key_error(self):
        from assessor_lookup.platforms import build_client

        with pytest.raises(KeyError):
            build_client(_entry("Nowhere", "co", "wedge"))


class TestGetClientRouting:
    def test_jefferson_routes_to_aumentum(self):
        from assessor_lookup.checker import _get_client
        from assessor_lookup.platforms.aumentum import JeffcoClient

        client, entry = _get_client("Jefferson", "co")
        assert isinstance(client, JeffcoClient)
        assert entry["platform"] == "aumentum"

    def test_clear_creek_routes_to_eagleweb_with_base(self):
        from assessor_lookup.checker import _get_client
        from assessor_lookup.platforms.eagleweb import EagleWebClient

        client, entry = _get_client("Clear Creek", "co")
        assert isinstance(client, EagleWebClient)
        assert client.base.startswith("https://")

    def test_partial_match_is_state_scoped(self, monkeypatch):
        # "Jefferson" in Montana must NOT hit the CO Jefferson entry; with
        # discovery stubbed out it falls through to the Spatialest fallback.
        import assessor_lookup.discovery as discovery
        from assessor_lookup.checker import _get_client
        from assessor_lookup.platforms.spatialest import SpatialestClient

        monkeypatch.setattr(discovery, "discover_county",
                            lambda *a, **k: None)
        client, entry = _get_client("Jefferson", "mt")
        assert isinstance(client, SpatialestClient)
        assert entry["platform"] == "spatialest"


class TestPackagedRegistryMigrated:
    def test_packaged_file_is_canonical_format(self):
        import json
        from assessor_lookup.registry import _PACKAGED_PATH

        raw = json.loads(_PACKAGED_PATH.read_text())
        assert "US/CO/county:el-paso" in raw
        entry = raw["US/CO/county:el-paso"]
        assert entry["jurisdiction"]["kind"] == "county"
        assert entry["config"]["slug"] == "elpaso"
        # no legacy platform aliases remain in the packaged file
        legacy = {"jeffco", "adams", "arapahoe", "co_parcel_api"}
        assert not any(e.get("platform") in legacy for e in raw.values())


class TestModuleLocations:
    def test_spatialest_lives_under_platforms(self):
        from assessor_lookup.platforms.spatialest import SpatialestClient  # noqa: F401

    def test_eagleweb_lives_under_platforms(self):
        from assessor_lookup.platforms.eagleweb import EagleWebClient  # noqa: F401

    def test_aumentum_lives_under_platforms(self):
        from assessor_lookup.platforms.aumentum import JeffcoClient  # noqa: F401


class TestAumentumBaseConfig:
    def test_default_base_is_jeffco(self):
        from assessor_lookup.platforms.aumentum import JeffcoClient

        assert JeffcoClient().base_url == "https://propertysearch.jeffco.us/api"

    def test_base_is_configurable_for_other_deployments(self):
        from assessor_lookup.platforms.aumentum import JeffcoClient

        client = JeffcoClient(base="https://records.example.gov/api")
        assert client.base_url == "https://records.example.gov/api"

    def test_factory_passes_config_base(self):
        from assessor_lookup.platforms import build_client

        entry = {
            "jurisdiction": {"country": "US", "state": "CO", "kind": "county",
                             "name": "Jefferson"},
            "platform": "aumentum",
            "config": {"base": "https://records.example.gov/api"},
        }
        assert build_client(entry).base_url == "https://records.example.gov/api"

    def test_configured_base_must_be_https(self):
        from assessor_lookup.platforms.aumentum import JeffcoClient

        with pytest.raises(ValueError):
            JeffcoClient(base="http://records.example.gov/api")


class TestCapabilities:
    def _clients(self):
        from assessor_lookup.platforms.spatialest import SpatialestClient
        from assessor_lookup.platforms.eagleweb import EagleWebClient
        from assessor_lookup.platforms.aumentum import JeffcoClient
        from assessor_lookup.jurisdictions.us.co.adams import AdamsClient
        from assessor_lookup.jurisdictions.us.co.arapahoe import ArapahoeClient
        from assessor_lookup.jurisdictions.us.co.statewide import CoParcelClient

        return [SpatialestClient, EagleWebClient, JeffcoClient, AdamsClient,
                ArapahoeClient, CoParcelClient]

    def test_every_client_declares_capabilities(self):
        for cls in self._clients():
            caps = cls.capabilities
            assert isinstance(caps.get("parcel_lookup"), bool), cls.__name__
            assert isinstance(caps.get("building_fields"), bool), cls.__name__

    def test_parcel_lookup_capability_matches_implementation(self):
        for cls in self._clients():
            assert cls.capabilities["parcel_lookup"] == hasattr(
                cls, "lookup_by_parcel"), cls.__name__

    def test_statewide_layer_declares_no_building_fields(self):
        from assessor_lookup.jurisdictions.us.co.statewide import CoParcelClient

        assert CoParcelClient.capabilities["building_fields"] is False


def _write_user_registry(entries):
    """Write raw entries into the hermetic per-user registry."""
    import json
    import os
    from pathlib import Path

    path = Path(os.environ["ASSESSOR_LOOKUP_HOME"]) / "county_registry.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries))


class TestBuildClientDriverContract:
    def test_unknown_driver_raises_key_error_not_import_error(self):
        from assessor_lookup.platforms import build_client

        entry = _entry("Nowhere", "co", "arcgis", {"driver": "us.co.nope"})
        with pytest.raises(KeyError):
            build_client(entry)

    @pytest.mark.parametrize("driver", ["", "../evil", "us.co..x", "us co"])
    def test_malformed_driver_rejected(self, driver):
        from assessor_lookup.platforms import build_client

        with pytest.raises(KeyError):
            build_client(_entry("Nowhere", "co", "arcgis", {"driver": driver}))


class TestInvalidConfigNeverRaises:
    def test_unknown_platform_yields_error_status_client(self):
        from assessor_lookup.checker import _get_client

        _write_user_registry({
            "CO:Typoville": {"platform": "spacialest", "slug": "x",
                             "state": "co"},
        })
        client, entry = _get_client("Typoville", "co")
        out = client.lookup("123 Main St")
        assert out["status"] == "api_error"
        assert "spacialest" in out["error"]
        assert client.capabilities["parcel_lookup"] is True
        assert client.lookup_by_parcel("123")["status"] == "api_error"

    def test_unknown_driver_yields_error_status_client(self):
        from assessor_lookup.checker import _get_client

        _write_user_registry({
            "CO:Gone": {"platform": "adams", "state": "co"},
        })
        # sabotage the alias target by pointing driver at a missing module
        _write_user_registry({
            "US/CO/county:gone": {
                "jurisdiction": {"country": "US", "state": "CO",
                                 "kind": "county", "name": "Gone"},
                "platform": "arcgis", "config": {"driver": "us.co.nope"},
            },
        })
        client, _ = _get_client("Gone", "co")
        out = client.lookup("123 Main St")
        assert out["status"] == "api_error"
        assert "us.co.nope" in out["error"]

    def test_top_level_lookup_returns_error_dict(self):
        import assessor_lookup

        _write_user_registry({
            "CO:Typoville": {"platform": "spacialest", "slug": "x",
                             "state": "co"},
        })
        out = assessor_lookup.lookup("123 Main St", county="Typoville",
                                     state="co")
        assert out["status"] == "api_error"

    def test_ambiguous_jurisdiction_yields_error_status_client(self):
        from assessor_lookup.checker import _get_client

        _write_user_registry({
            "US/VA/independent_city:richmond": {
                "jurisdiction": {"country": "US", "state": "VA",
                                 "kind": "independent_city",
                                 "name": "Richmond"},
                "platform": "spatialest", "config": {"slug": "r1"}},
            "US/VA/county:richmond": {
                "jurisdiction": {"country": "US", "state": "VA",
                                 "kind": "county", "name": "Richmond"},
                "platform": "spatialest", "config": {"slug": "r2"}},
        })
        client, _ = _get_client("Richmond", "va")
        out = client.lookup("123 Main St")
        assert out["status"] == "api_error"
        assert "richmond" in out["error"].lower()


class TestDiscoveryHitPersists:
    def test_unregistered_county_discovery_hit_persists_and_builds(self, monkeypatch):
        import json
        import os
        from pathlib import Path
        import assessor_lookup.discovery as discovery
        from assessor_lookup.checker import _get_client
        from assessor_lookup.platforms.spatialest import SpatialestClient

        hit = {
            "jurisdiction": {"country": "US", "state": "CO", "kind": "county",
                             "name": "Elbert"},
            "platform": "spatialest", "config": {"slug": "elbert"},
            "access": {"mode": "public"}, "source": "discovered", "tier": 1,
        }
        monkeypatch.setattr(discovery, "discover_county",
                            lambda *a, **k: dict(hit))
        client, entry = _get_client("Elbert", "co")
        assert isinstance(client, SpatialestClient)
        on_disk = json.loads(
            (Path(os.environ["ASSESSOR_LOOKUP_HOME"]) /
             "county_registry.json").read_text())
        assert "US/CO/county:elbert" in on_disk


class TestSpatialestFactoryBakesConfig:
    def test_slug_and_state_from_entry(self):
        from assessor_lookup.platforms import build_client

        client = build_client(_entry("Denver", "co", "spatialest",
                                     {"slug": "denver"}))
        assert client.county_slug == "denver"
        assert client.state == "co"


class TestCapabilitiesGating:
    class _NoParcelClient:
        capabilities = {"parcel_lookup": False, "building_fields": True}

        def lookup(self, address, **ctx):
            return {"status": "success", "parcel_number": "1",
                    "above_grade_sqft": 1000, "beds": 3, "baths": 2.0,
                    "year_built": 1990, "basement_sqft": 0}

        def lookup_by_parcel(self, parcel_id, **ctx):
            raise AssertionError(
                "lookup_by_parcel called despite parcel_lookup=False")

    def test_top_level_lookup_respects_capability(self, monkeypatch):
        import assessor_lookup

        entry = _entry("X", "co", "spatialest", {"slug": "x"})
        monkeypatch.setattr(
            assessor_lookup, "_get_client",
            lambda county, state="co", verbose=False:
                (self._NoParcelClient(), entry))
        out = assessor_lookup.lookup("123 Main St", parcel="42", county="X")
        assert out["status"] == "success"
