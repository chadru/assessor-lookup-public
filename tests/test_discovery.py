"""Tests that auto-discovery returns normalized registry entries."""

from unittest.mock import patch

import assessor_lookup.discovery as discovery


class TestDiscoveryNormalization:
    def test_spatialest_hit_is_normalized(self, monkeypatch):
        monkeypatch.setattr(
            discovery, "_try_spatialest",
            lambda county, state, timeout: {
                "platform": "spatialest", "slug": "elbert", "state": state,
                "source": "discovered", "tier": 1,
            })
        entry = discovery.discover_county("Elbert", "co")
        assert entry["platform"] == "spatialest"
        assert entry["config"] == {"slug": "elbert"}
        assert entry["jurisdiction"] == {
            "country": "US", "state": "CO", "kind": "county", "name": "Elbert",
        }
        assert entry["tier"] == 1
        assert entry["source"] == "discovered"

    def test_statewide_baseline_normalizes_to_arcgis_driver(self, monkeypatch):
        monkeypatch.setattr(discovery, "_try_spatialest",
                            lambda *a: None)
        monkeypatch.setattr(discovery, "_try_eagleweb",
                            lambda *a: None)
        entry = discovery.discover_county("Boulder", "co")
        assert entry["platform"] == "arcgis"
        assert entry["config"]["driver"] == "us.co.statewide"
        assert entry["tier"] == 2

    def test_statewide_baseline_is_colorado_only(self, monkeypatch):
        monkeypatch.setattr(discovery, "_try_spatialest",
                            lambda *a: None)
        monkeypatch.setattr(discovery, "_try_eagleweb",
                            lambda *a: None)
        assert discovery.discover_county("Boulder", "tx") is None


class TestDiscoveryLadder:
    """Auto-discovery probes API/platform sources in best-data-first order."""

    def test_spatialest_hit_is_tier1(self):
        from assessor_lookup import discovery
        with patch.object(discovery, "_probe",
                          side_effect=lambda url, t, needle=None: (True, "")):
            entry = discovery.discover_county("Weld", "co")
        assert entry["platform"] == "spatialest"
        assert entry["tier"] == 1
        assert entry["config"]["slug"] == "weld"

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
        assert entry["config"]["base"].endswith("clear-creek.co.us/eagleassessor")
        assert entry["tier"] == 1

    def test_co_parcel_baseline_when_no_platform(self):
        from assessor_lookup import discovery
        with patch.object(discovery, "_probe",
                          side_effect=lambda url, t, needle=None: (False, "")):
            entry = discovery.discover_county("Pitkin", "co")
        assert entry["platform"] == "arcgis"
        assert entry["config"]["driver"] == "us.co.statewide"
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


