"""Tests for assessor_lookup.registry: canonical jurisdiction model,
dual-format loading, legacy platform aliases, merging, and resolution."""

import json

import pytest

from assessor_lookup import registry as reg


class TestCanonicalKey:
    def test_defaults_to_us_county(self):
        assert reg.canonical_key("El Paso", "co") == "US/CO/county:el-paso"

    def test_kind_and_punctuation(self):
        key = reg.canonical_key("St. Mary's", "la", kind="parish")
        assert key == "US/LA/parish:st-marys"


class TestNormalizeLegacyEntries:
    def test_legacy_spatialest_entry(self):
        ckey, entry = reg.normalize_entry(
            "CO:El Paso", {"platform": "spatialest", "slug": "elpaso", "state": "co"}
        )
        assert ckey == "US/CO/county:el-paso"
        assert entry["jurisdiction"] == {
            "country": "US", "state": "CO", "kind": "county", "name": "El Paso",
        }
        assert entry["platform"] == "spatialest"
        assert entry["config"] == {"slug": "elpaso"}
        assert entry["access"] == {"mode": "public"}

    def test_legacy_eagleweb_base_moves_into_config(self):
        _, entry = reg.normalize_entry(
            "CO:Clear Creek",
            {"platform": "eagleweb", "base": "https://example.test/eagleassessor",
             "state": "co"},
        )
        assert entry["platform"] == "eagleweb"
        assert entry["config"] == {"base": "https://example.test/eagleassessor"}

    def test_jeffco_alias_becomes_aumentum(self):
        _, entry = reg.normalize_entry("CO:Jefferson", {"platform": "jeffco", "state": "co"})
        assert entry["platform"] == "aumentum"
        assert entry["config"]["base"] == "https://propertysearch.jeffco.us/api"

    @pytest.mark.parametrize("legacy,driver", [
        ("adams", "us.co.adams"),
        ("arapahoe", "us.co.arapahoe"),
        ("co_parcel_api", "us.co.statewide"),
    ])
    def test_arcgis_jurisdiction_aliases(self, legacy, driver):
        _, entry = reg.normalize_entry(
            f"CO:{legacy.title()}", {"platform": legacy, "state": "co"}
        )
        assert entry["platform"] == "arcgis"
        assert entry["config"]["driver"] == driver

    def test_new_format_entry_is_idempotent(self):
        new = {
            "jurisdiction": {"country": "US", "state": "CO", "kind": "county",
                             "name": "Denver"},
            "platform": "spatialest",
            "config": {"slug": "denver"},
            "access": {"mode": "public"},
        }
        ckey, entry = reg.normalize_entry("US/CO/county:denver", new)
        assert ckey == "US/CO/county:denver"
        assert entry == new

    def test_discovery_metadata_survives(self):
        _, entry = reg.normalize_entry(
            "CO:Elbert",
            {"platform": "spatialest", "slug": "elbert", "state": "co",
             "source": "discovered", "tier": 1},
        )
        assert entry["source"] == "discovered"
        assert entry["tier"] == 1


class TestLoadAndMerge:
    def test_user_legacy_entry_overrides_packaged_by_canonical_identity(self, tmp_path):
        packaged = tmp_path / "packaged.json"
        packaged.write_text(json.dumps({
            "US/CO/county:el-paso": {
                "jurisdiction": {"country": "US", "state": "CO", "kind": "county",
                                 "name": "El Paso"},
                "platform": "spatialest", "config": {"slug": "elpaso"},
            },
        }))
        user = tmp_path / "user.json"
        user.write_text(json.dumps({
            "CO:El Paso": {"platform": "spatialest", "slug": "override", "state": "co"},
        }))
        registry = reg.load_registry(packaged_path=packaged, user_path=user)
        assert len(registry) == 1
        assert registry["US/CO/county:el-paso"]["config"]["slug"] == "override"

    def test_corrupt_user_registry_never_breaks_load(self, tmp_path):
        packaged = tmp_path / "packaged.json"
        packaged.write_text(json.dumps({
            "CO:Denver": {"platform": "spatialest", "slug": "denver", "state": "co"},
        }))
        user = tmp_path / "user.json"
        user.write_text("{not json")
        registry = reg.load_registry(packaged_path=packaged, user_path=user)
        assert "US/CO/county:denver" in registry


class TestResolve:
    REGISTRY = {
        "US/CO/county:jefferson": {
            "jurisdiction": {"country": "US", "state": "CO", "kind": "county",
                             "name": "Jefferson"},
            "platform": "aumentum", "config": {},
        },
        "US/LA/parish:orleans": {
            "jurisdiction": {"country": "US", "state": "LA", "kind": "parish",
                             "name": "Orleans"},
            "platform": "spatialest", "config": {"slug": "orleans"},
        },
    }

    def test_exact_name_and_state(self):
        entry = reg.resolve(self.REGISTRY, "Jefferson", "co")
        assert entry["platform"] == "aumentum"

    def test_partial_match_is_state_scoped(self):
        # A same-named county in another state must NOT route to the CO entry.
        assert reg.resolve(self.REGISTRY, "Jefferson", "mt") is None

    def test_name_match_spans_kinds_within_state(self):
        # MCP signature only carries (county, state); parishes must be reachable.
        entry = reg.resolve(self.REGISTRY, "Orleans", "la")
        assert entry["jurisdiction"]["kind"] == "parish"

    def test_partial_name_containment_within_state(self):
        entry = reg.resolve(self.REGISTRY, "orleans parish", "la")
        assert entry["jurisdiction"]["kind"] == "parish"


class TestSaveDiscoveredEntry:
    def test_writes_new_format_readable_by_loader(self, tmp_path):
        user = tmp_path / "user.json"
        key = reg.save_discovered_entry(
            "Elbert",
            {"platform": "spatialest", "slug": "elbert", "state": "co",
             "source": "discovered", "tier": 1},
            state="co", user_path=user,
        )
        assert key == "US/CO/county:elbert"
        on_disk = json.loads(user.read_text())
        assert on_disk[key]["platform"] == "spatialest"
        assert on_disk[key]["config"] == {"slug": "elbert"}
        assert on_disk[key]["jurisdiction"]["name"] == "Elbert"

        packaged = tmp_path / "packaged.json"
        packaged.write_text("{}")
        registry = reg.load_registry(packaged_path=packaged, user_path=user)
        assert key in registry


class TestLoadContainment:
    def test_json_array_file_yields_empty(self, tmp_path):
        bad = tmp_path / "user.json"
        bad.write_text("[]")
        assert reg.load_registry(packaged_path=tmp_path / "none.json",
                                 user_path=bad) == {}

    def test_json_null_file_yields_empty(self, tmp_path):
        bad = tmp_path / "user.json"
        bad.write_text("null")
        assert reg.load_registry(packaged_path=tmp_path / "none.json",
                                 user_path=bad) == {}

    def test_non_dict_entry_is_skipped_others_load(self, tmp_path):
        user = tmp_path / "user.json"
        user.write_text(json.dumps({
            "CO:Denver": {"platform": "spatialest", "slug": "denver",
                          "state": "co"},
            "CO:Broken": "not an entry dict",
        }))
        registry = reg.load_registry(packaged_path=tmp_path / "none.json",
                                     user_path=user)
        assert list(registry) == ["US/CO/county:denver"]


class TestNormalizeErrors:
    def test_unparseable_key_raises_value_error(self):
        with pytest.raises(ValueError):
            reg.normalize_entry("NoColonAnywhere", {"platform": "spatialest"})

    def test_missing_platform_defaults_to_spatialest(self):
        _, entry = reg.normalize_entry("CO:Elbert", {"slug": "elbert"})
        assert entry["platform"] == "spatialest"


class TestResolveGuards:
    RICHMONDS = {
        "US/VA/independent_city:richmond": {
            "jurisdiction": {"country": "US", "state": "VA",
                             "kind": "independent_city", "name": "Richmond"},
            "platform": "spatialest", "config": {}},
        "US/VA/county:richmond": {
            "jurisdiction": {"country": "US", "state": "VA",
                             "kind": "county", "name": "Richmond"},
            "platform": "aumentum", "config": {}},
    }

    def test_empty_and_blank_queries_return_none(self):
        assert reg.resolve(self.RICHMONDS, "", "va") is None
        assert reg.resolve(self.RICHMONDS, "   ", "va") is None

    def test_exact_name_collision_raises_ambiguous(self):
        with pytest.raises(reg.AmbiguousJurisdiction) as exc:
            reg.resolve(self.RICHMONDS, "Richmond", "va")
        assert set(exc.value.candidates) == set(self.RICHMONDS)

    def test_partial_collision_raises_ambiguous(self):
        registry = {
            "US/CO/county:fordham": {
                "jurisdiction": {"country": "US", "state": "CO",
                                 "kind": "county", "name": "Fordham"},
                "platform": "spatialest", "config": {}},
            "US/CO/county:fordyce": {
                "jurisdiction": {"country": "US", "state": "CO",
                                 "kind": "county", "name": "Fordyce"},
                "platform": "spatialest", "config": {}},
        }
        with pytest.raises(reg.AmbiguousJurisdiction):
            reg.resolve(registry, "ford", "co")

    def test_unique_partial_still_resolves(self):
        registry = {k: v for k, v in self.RICHMONDS.items()
                    if v["jurisdiction"]["kind"] == "county"}
        entry = reg.resolve(registry, "richmond county", "va")
        assert entry["platform"] == "aumentum"


class TestSaveDiscoveredMore:
    def test_save_merges_into_existing_user_registry(self, tmp_path):
        user = tmp_path / "user.json"
        reg.save_discovered_entry(
            "Elbert", {"platform": "spatialest", "slug": "elbert",
                       "state": "co"}, state="co", user_path=user)
        reg.save_discovered_entry(
            "Lincoln", {"platform": "spatialest", "slug": "lincoln",
                        "state": "co"}, state="co", user_path=user)
        on_disk = json.loads(user.read_text())
        assert {"US/CO/county:elbert", "US/CO/county:lincoln"} <= set(on_disk)

    def test_save_swallows_write_failure(self, tmp_path):
        blocked = tmp_path / "blocked"
        blocked.mkdir()
        blocked.chmod(0o500)
        try:
            key = reg.save_discovered_entry(
                "Elbert", {"platform": "spatialest", "slug": "elbert",
                           "state": "co"}, state="co",
                user_path=blocked / "user.json")
        finally:
            blocked.chmod(0o700)
        assert key == "US/CO/county:elbert"  # returned despite failed write


class TestWriteRobustness:
    def test_unserializable_entry_swallowed_no_temp_left(self, tmp_path):
        user = tmp_path / "user.json"
        key = reg.save_discovered_entry(
            "Elbert", {"platform": "spatialest", "slug": object(),
                       "state": "co"}, state="co", user_path=user)
        assert key == "US/CO/county:elbert"
        leftovers = [p.name for p in tmp_path.iterdir() if p.name != "user.json"]
        assert leftovers == []

    def test_successful_save_leaves_only_registry_file(self, tmp_path):
        user = tmp_path / "user.json"
        reg.save_discovered_entry(
            "Elbert", {"platform": "spatialest", "slug": "elbert",
                       "state": "co"}, state="co", user_path=user)
        assert [p.name for p in tmp_path.iterdir()] == ["user.json"]


class TestAccessFieldConsumed:
    def test_legacy_access_does_not_leak_into_config(self):
        _, entry = reg.normalize_entry(
            "CO:X", {"platform": "spatialest", "slug": "x",
                     "access": {"mode": "public"}})
        assert "access" not in entry["config"]
        assert entry["access"] == {"mode": "public"}
