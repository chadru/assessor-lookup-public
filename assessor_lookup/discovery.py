"""Auto-discovery: map an unknown county to an assessor source, API-first.

When an appraiser runs a lookup for a county we don't have in the registry,
``discover_county`` probes for a supported source and returns a registry entry
(the same shape as ``county_registry.json``). The ladder is intentionally
ordered best-data-first:

  Tier 1 — full building data (GLA/beds/baths/year), detectable by URL:
    * Spatialest  : property.spatialest.com/{state}/{slug}/   (national)
    * EagleWeb    : assessor.co.{county}.{state}.us/eagleassessor/  (CO host
                    convention; Tyler's JSP app, scraped)

  Tier 2 — baseline parcel API (owner/legal/value/land, NO building data):
    * CO statewide public-parcel ArcGIS layer                  (Colorado only)

Hits are returned as *normalized* registry entries (canonical jurisdiction +
platform + config). Nothing here writes to disk; callers decide whether to
persist a hit (see ``registry.save_discovered_entry``). Probes are cheap
HEAD/GET requests with a short timeout and fail closed (a probe error just
means "not this platform").
"""

import re
import urllib.error
import urllib.parse
import urllib.request

from .core.network import open_https, require_https_url

_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

# Colorado counties present in the statewide public-parcel composite (tier 2).
# Used only to answer "can the baseline API serve this county?" quickly.
_CO_PARCEL_COUNTIES = {
    "adams", "arapahoe", "archuleta", "boulder", "broomfield", "chaffee",
    "clear creek", "costilla", "custer", "delta", "denver", "douglas", "eagle",
    "el paso", "fremont", "garfield", "gilpin", "grand", "gunnison", "jefferson",
    "lake", "la plata", "larimer", "logan", "mesa", "moffat", "montezuma",
    "montrose", "morgan", "ouray", "park", "pitkin", "pueblo", "rio blanco",
    "routt", "san miguel", "sedgwick", "summit", "teller", "weld",
}


def _slug_variants(county):
    base = county.strip().lower()
    variants = [
        base.replace(" ", ""),        # clearcreek
        base.replace(" ", "-"),       # clear-creek
        base.replace(" ", "_"),       # clear_creek
    ]
    # One-word counties collapse to a single variant — dedup so we don't
    # probe the same host repeatedly (each dead probe costs up to `timeout`).
    seen, out = set(), []
    for v in variants:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _probe(url, timeout, needle=None):
    """GET a URL; return (ok, body). ok is False on any error/non-2xx."""
    try:
        url = require_https_url(
            url, allowed_hosts=("property.spatialest.com",),
            allowed_suffixes=(".us",),
        )
        req = urllib.request.Request(url, headers=_UA)
        host = urllib.parse.urlsplit(url).hostname
        with open_https(req, timeout=timeout, allowed_hosts=(host,)) as r:
            body = r.read(4000).decode("utf-8", "ignore")
            if getattr(r, "status", 200) >= 400:
                return False, ""
            if needle and needle.lower() not in body.lower():
                return False, body
            return True, body
    except urllib.error.HTTPError:
        return False, ""
    except Exception:  # noqa: BLE001  (probe failures are non-fatal)
        return False, ""


def _try_spatialest(county, state, timeout):
    # Spatialest slugs are bare ("clearcreek"), so all variants collapse to
    # one candidate — dedup to a single probe.
    slugs = {v.replace("-", "").replace("_", "") for v in _slug_variants(county)}
    for slug in sorted(slugs):
        url = f"https://property.spatialest.com/{state}/{slug}/"
        ok, _ = _probe(url, timeout)
        if ok:
            return {"platform": "spatialest", "slug": slug, "state": state,
                    "source": "discovered", "tier": 1}
    return None


def _try_eagleweb(county, state, timeout):
    # Colorado county domain convention: co.<hyphenated-county>.<state>.us
    for slug in _slug_variants(county):
        if "-" not in slug and " " in county:
            continue  # want the hyphenated host form
        host = f"assessor.co.{slug.replace('_', '-')}.{state}.us"
        url = f"https://{host}/eagleassessor/web/"
        ok, body = _probe(url, timeout, needle="eagleweb")
        if ok or (body and re.search(r"tyler|eagleassessor", body, re.I)):
            return {"platform": "eagleweb",
                    "base": f"https://{host}/eagleassessor",
                    "state": state, "source": "discovered", "tier": 1}
    return None


def _try_co_parcel(county, state, timeout):
    if state.lower() != "co":
        return None
    if county.strip().lower() not in _CO_PARCEL_COUNTIES:
        return None
    return {"platform": "co_parcel_api", "county": county, "state": state,
            "source": "discovered", "tier": 2}


def discover_county(county, state="co", timeout=8, verbose=False):
    """Probe for an assessor source for ``county``. Returns an entry or None.

    Tier 1 (full building data) is tried before the tier-2 baseline API.
    """
    county = (county or "").strip()
    state = (state or "co").strip().lower()
    if not county:
        return None

    for name, probe in (("spatialest", _try_spatialest),
                        ("eagleweb", _try_eagleweb),
                        ("co_parcel_api", _try_co_parcel)):
        try:
            entry = probe(county, state, timeout)
        except Exception:  # noqa: BLE001
            entry = None
        if entry:
            from .registry import normalize_entry
            _, entry = normalize_entry(f"{state.upper()}:{county}", entry)
            if verbose:
                tier = entry.get("tier")
                print(f"  discovered {county} -> {entry['platform']} (tier {tier})")
            return entry
    return None
