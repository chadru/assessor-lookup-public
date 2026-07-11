"""Jurisdiction model and county registry: canonical keys, dual-format
loading, legacy aliases, merging, and (county, state) resolution.

Canonical key format: "{COUNTRY}/{STATE}/{kind}:{name-slug}", e.g.
"US/CO/county:el-paso". The legacy "CO:El Paso" key format and legacy
platform aliases ("jeffco", "adams", ...) are readable forever; all writes
emit the new format.
"""

import json
import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

_PACKAGED_PATH = Path(__file__).parent / "county_registry.json"

# Legacy platform values that were really jurisdictions on a generic platform.
_LEGACY_PLATFORM_ALIASES = {
    "jeffco": ("aumentum", {"base": "https://propertysearch.jeffco.us/api"}),
    "adams": ("arcgis", {"driver": "us.co.adams"}),
    "arapahoe": ("arcgis", {"driver": "us.co.arapahoe"}),
    "co_parcel_api": ("arcgis", {"driver": "us.co.statewide"}),
}

# Legacy entry fields that describe the jurisdiction/source rather than
# platform config, and therefore do not belong in config.
_TOP_LEVEL_FIELDS = {"source", "tier"}
_CONSUMED_FIELDS = {"platform", "state", "county", "access"}


def slugify(name):
    """Lowercase, drop punctuation, spaces to hyphens: "St. Mary's" -> "st-marys"."""
    slug = re.sub(r"[^a-z0-9\s-]", "", str(name).lower())
    return re.sub(r"[\s_]+", "-", slug.strip())


def canonical_key(name, state, country="US", kind="county"):
    return f"{country.upper()}/{state.upper()}/{kind}:{slugify(name)}"


def _parse_key(key):
    """Parse either key format -> (country, state, kind, display_name) or None."""
    if "/" in key and ":" in key:
        try:
            country, state, rest = key.split("/", 2)
            kind, name = rest.split(":", 1)
            return country.upper(), state.upper(), kind, name
        except ValueError:
            return None
    if ":" in key:
        state, name = key.split(":", 1)
        return "US", state.upper(), "county", name.strip()
    return None


def normalize_entry(key, entry):
    """Return (canonical_key, normalized_entry) for either format.

    Normalized shape:
    {"jurisdiction": {country, state, kind, name}, "platform": ...,
     "config": {...}, "access": {"mode": ...}, [source, tier]}
    """
    parsed = _parse_key(key)
    if parsed is None:
        raise ValueError(f"unparseable registry key: {key!r}")
    country, state, kind, name = parsed

    if "jurisdiction" in entry:  # already new format
        normalized = dict(entry)
        jur = normalized["jurisdiction"]
        normalized.setdefault("config", {})
        normalized.setdefault("access", {"mode": "public"})
        ckey = canonical_key(jur["name"], jur["state"], jur.get("country", "US"),
                             jur.get("kind", "county"))
        return ckey, normalized

    platform = entry.get("platform", "spatialest")
    config = {}
    normalized = {}
    for field, value in entry.items():
        if field in _CONSUMED_FIELDS:
            continue
        if field in _TOP_LEVEL_FIELDS:
            normalized[field] = value
        else:
            config[field] = value

    if platform in _LEGACY_PLATFORM_ALIASES:
        platform, default_config = _LEGACY_PLATFORM_ALIASES[platform]
        for k, v in default_config.items():
            config.setdefault(k, v)

    normalized.update({
        "jurisdiction": {"country": country, "state": state, "kind": kind,
                         "name": name},
        "platform": platform,
        "config": config,
        "access": entry.get("access", {"mode": "public"}),
    })
    return canonical_key(name, state, country, kind), normalized


def _user_registry_path():
    """Honors ASSESSOR_LOOKUP_HOME, else ~/.config/assessor-lookup/."""
    root = os.environ.get("ASSESSOR_LOOKUP_HOME")
    base = Path(root) if root else Path.home() / ".config" / "assessor-lookup"
    return base / "county_registry.json"


def _read_registry_file(path):
    try:
        with open(path) as f:
            raw = json.load(f)
    except (ValueError, OSError):
        return {}  # a corrupt/missing registry file must never break lookups
    if not isinstance(raw, dict):
        logger.warning("Registry file %s is not a JSON object; ignoring", path)
        return {}
    normalized = {}
    for key, entry in raw.items():
        try:
            ckey, norm = normalize_entry(key, entry)
        except (ValueError, KeyError, TypeError, AttributeError):
            logger.warning("Skipping unparseable registry entry: %s", key)
            continue
        normalized[ckey] = norm
    return normalized


def load_registry(packaged_path=None, user_path=None):
    """Packaged defaults + user registry, merged by canonical identity (user wins)."""
    registry = _read_registry_file(packaged_path or _PACKAGED_PATH)
    registry.update(_read_registry_file(user_path or _user_registry_path()))
    return registry


class AmbiguousJurisdiction(Exception):
    """More than one registered jurisdiction matches a (name, state) query.

    Fail closed rather than guess — a silently wrong assessor is worse than
    an explicit error (mirrors core.matching.select_candidate's discipline).
    """

    def __init__(self, name, state, candidates):
        self.candidates = list(candidates)
        super().__init__(
            f"{name!r} in {state.upper()} matches multiple jurisdictions: "
            + ", ".join(sorted(self.candidates)))


def resolve(registry, name, state, country="US"):
    """Resolve (name, state) to a normalized entry, or None.

    Exact slug match first, then partial-name containment — both strictly
    scoped to the given country/state, spanning jurisdiction kinds so a
    parish or independent city is reachable through a plain (county, state)
    input. Raises AmbiguousJurisdiction when more than one entry matches at
    the same tier; an empty query never matches.
    """
    target = slugify(name)
    if not target:
        return None
    country = country.upper()
    state = state.upper()
    in_state = [
        (key, e) for key, e in registry.items()
        if e["jurisdiction"]["country"] == country
        and e["jurisdiction"]["state"] == state
    ]
    exact = [(k, e) for k, e in in_state
             if slugify(e["jurisdiction"]["name"]) == target]
    if len(exact) == 1:
        return exact[0][1]
    if len(exact) > 1:
        raise AmbiguousJurisdiction(name, state, (k for k, _ in exact))
    partial = []
    for key, entry in in_state:
        slug = slugify(entry["jurisdiction"]["name"])
        if slug in target or target in slug:
            partial.append((key, entry))
    if len(partial) == 1:
        return partial[0][1]
    if len(partial) > 1:
        raise AmbiguousJurisdiction(name, state, (k for k, _ in partial))
    return None


def save_discovered_entry(county, entry, state="co", user_path=None):
    """Persist a discovered entry to the per-user registry in the new format.

    Returns the canonical key written. Failures are swallowed (caching is a
    convenience, not a requirement).
    """
    legacy_key = f"{state.upper()}:{county}"
    ckey, normalized = normalize_entry(legacy_key, entry)
    path = Path(user_path) if user_path else _user_registry_path()
    tmp = path.with_suffix(".json.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {}
        if path.exists():
            with open(path) as f:
                data = json.load(f)
        data[ckey] = normalized
        # Serialize first, write to a sibling temp file, then atomically
        # replace — a failure mid-write can never truncate the registry.
        payload = json.dumps(data, indent=2, sort_keys=True)
        with open(tmp, "w") as f:
            f.write(payload)
        os.replace(tmp, path)
    except (OSError, ValueError, TypeError):
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
    return ckey
