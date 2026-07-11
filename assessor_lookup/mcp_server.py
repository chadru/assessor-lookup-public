"""All-inclusive MCP server for assessor-lookup.

Turns this repository into a self-describing, agent-ready service. A connecting
agent gets, in one place:

  * an operating manual (server ``instructions``) — the coordinator role, the
    agent topology, the API-first data policy, and the standard workflows;
  * tools — property lookup, MLS discrepancy checks, county discovery, and the
    regression/benchmark harness;
  * resources — live architecture, the county registry, golden records, and the
    harness guide;
  * prompts — ready-made workflows for running an appraisal check and for
    adding a brand-new county end-to-end.

Run it (from a clone of the repo):

    pip install -e ".[mcp]"
    assessor-lookup-mcp                 # stdio transport (default)
    # or: python -m assessor_lookup.mcp_server

Register with Claude Code:

    claude mcp add assessor-lookup -- assessor-lookup-mcp

Requires Python 3.10+ (the MCP SDK's floor); the lookup core itself is 3.9+.
"""

import json
import os
from pathlib import Path

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as e:  # pragma: no cover
    raise ImportError(
        "The MCP server needs the MCP SDK. Install with "
        "`pip install -e \".[mcp]\"` (Python 3.10+)."
    ) from e

from . import lookup as _lookup
from .registry import load_registry as _load_registry, save_discovered_entry
from .checker import check_public_records, print_discrepancy_report  # noqa: F401
from .discovery import discover_county as _discover_county

_REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Operating manual — served both as `instructions` and as a readable resource.
# ---------------------------------------------------------------------------
OPERATING_MANUAL = """\
# assessor-lookup — MCP operating manual

You are working with **assessor-lookup**: it pulls a property's public record
(owner, legal, GLA, beds/baths, year built, taxes, assessed/market value,
lat/lon) straight from county assessor systems, and diffs those records against
an appraiser's MLS export to flag discrepancies before a report goes out.

## Your role: coordinator
Act as the **coordinator**. Read this manual and the `assessor://architecture`
resource first, then drive the task with the tools below. Do the small stuff
inline; delegate only genuinely parallel or deep work to sub-agents.

This repo ships **predefined agents and skills** (Claude Code under `.claude/`;
Codex under `.codex/agents/` and `.agents/skills/`) — use them, don't reinvent them:
- Agents: **coordinator** (you), **explorer** (maps an unknown county's site,
  read-only, API-first), **reviewer** (verifies a new client vs the live site +
  golden), **county-onboarder** (probes + onboards a user's counties).
- Skills: **onboard-locale** (set up the user's counties), **appraisal-check**
  (diff an MLS export vs public records), **add-county** (add a new county,
  API-first, verified).
If your client cannot invoke them directly, the source definitions remain the
authoritative playbooks. Keep changes bounded: a new county is one client
module + a registry entry + a golden case. Don't add frameworks.

## The one rule that matters: API-first, scrape as fallback
Before writing any scraper, probe for a JSON API (county/state ArcGIS, or a
vendor JSON platform like Spatialest). BUT verify the API actually carries the
**building characteristics** the check compares (GLA, beds, baths, year built).
Many clean parcel APIs (including Colorado's statewide layer) expose only
owner/legal/value/land and *omit* building data — in that case a scrape of the
assessor's HTML front-end is the only complete source. "Has an API" does not
mean "the API is sufficient." Confirm the fields before choosing.

## Platforms already supported
Spatialest (JSON), Jefferson/Aumentum (JSON), Arapahoe & Adams (ArcGIS JSON),
Clear Creek (Tyler EagleWeb — scraped), and a Colorado statewide parcel API
baseline (owner/legal/value only, no building data). Unknown counties are
auto-discovered (see `discover_county`).

## Standard workflows
**Run an appraisal check** (the daily job):
1. `list_counties` to confirm coverage; if the county is missing, `discover_county`.
2. `check_mls_csv` with the appraiser's subject + comps CSVs, or `lookup_property`
   for a single property.
3. Report discrepancies; stable fields (GLA/beds/baths/year/basement) are the
   ones that matter. Values/owner/taxes drift legitimately year to year.

**Onboard the user's local counties** (do this first when a user tells you where
they work):
1. Ask for (or take from their MLS) one sample address or parcel per county.
2. `probe_county(county, state, address|parcel)` — shows the platform, latency,
   and exactly which fields that county returns. Read `check_ready`: true means
   the building fields (GLA/beds/baths/year) come through and the discrepancy
   check will be useful; false means only owner/legal/value are available (the
   county's public data is thinner) — tell the user plainly.
3. `onboard_county(...)` for each keeper — it caches the source and pins a
   golden record so the county is configured once and re-checked every run.
   The user can then just point their MLS exports at `check_mls_csv`.
4. `run_regression` now includes the onboarded counties; run it any time to see
   how each county is reacting and catch a site that changed.
Field coverage varies by county because each public source exposes a different
subset — that's the source's data, not a bug. Say so honestly.

**Add a new county** (use the `add_new_county` prompt for the full script):
1. `discover_county` — does a supported source already resolve?
2. If not, delegate site-mapping to an explorer sub-agent; **API-first**.
3. Implement a client (`platforms/<name>.py`, or a `jurisdictions/` driver for
   a bespoke ArcGIS flow) returning the standard record dict with a `status`
   key; register it in the `PLATFORMS` dict and the registry.
4. Add a golden case to `assessor_lookup/harness.py` and capture it (`--capture`).
5. `run_regression` to confirm green; have the reviewer sub-agent verify.

## Verifying you didn't break anything
`run_regression` re-fetches pinned golden properties for every county and fails
on **stable-field** drift (parser broke or the site changed). `benchmark` times
the parsers offline. Treat a regression FAIL as "investigate this county"; if the
underlying property genuinely changed, re-capture the golden record.

## Safety
These are public records endpoints — no keys. Respect each county's rate limits
(the regression harness spaces live cases; individual clients do not promise
automatic retry). Records can lag reality; this is a time-saver, not a
substitute for appraiser diligence.

Run this MCP over local stdio only; it is not an authenticated network service.
CSV tools may read only `.csv` files inside the server's working directory, or
inside the directory explicitly set with `ASSESSOR_LOOKUP_MCP_DATA_DIR`.
"""

HARNESS_GUIDE = """\
# Testing & benchmark harness (`tests/harness.py`)

The operational risk of this repo is county websites changing silently. The
harness pins known properties per county as *golden records* and re-checks them.

Commands:
    python tests/harness.py            # full: regression + latency + discovery + parser bench
    python tests/harness.py --capture  # (re)pin golden records after legitimate data changes
    python tests/harness.py --filter clear   # only cases matching a substring
    python tests/harness.py --offline        # parser micro-bench only, no network
    pytest -m network tests/test_golden_live.py   # same goldens as pytest

Field policy:
  * STABLE  (parcel_number, above_grade_sqft, basement_sqft, beds, baths,
    year_built) -> FAIL on drift. A FAIL means the parser broke or the property
    really changed (remodel/sale). Investigate, then re-capture if the world moved.
  * VOLATILE (owner, values, taxes, legal, style, neighborhood) -> WARN only.

Exit codes: 0 pass / 1 hard regression / 2 warnings only.

Via MCP, call the `run_regression` tool (optionally `offline=true` for a fast
parser-only pass, or `filter="clearcreek"` for one county) and the `benchmark`
tool. Golden records are the `assessor://golden-records` resource.
"""


mcp = FastMCP("assessor-lookup", instructions=OPERATING_MANUAL)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _import_harness():
    """Import the packaged operational harness."""
    try:
        from . import harness
        return harness
    except Exception:  # noqa: BLE001
        return None


def _resolve_csv_path(path):
    """Resolve a caller-provided CSV inside the configured MCP data root."""
    root = Path(os.environ.get("ASSESSOR_LOOKUP_MCP_DATA_DIR")
                or Path.cwd()).expanduser().resolve()
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise PermissionError(
            "CSV path must stay inside the configured MCP data directory"
        ) from exc
    if resolved.suffix.lower() != ".csv":
        raise ValueError("MCP input must be a .csv file")
    if not resolved.is_file():
        raise ValueError("MCP CSV input must be a regular file")
    return resolved


def _read_rows(path):
    import csv
    with open(_resolve_csv_path(path), newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------
@mcp.tool(
    description="Look up one property's public record from its county assessor. "
    "Provide an address or a parcel/schedule number (parcel preferred when "
    "known). Returns a dict with a 'status' key ('success', 'not_found', "
    "'timeout', 'api_error', ...) and, on success, owner, legal, GLA "
    "(above_grade_sqft), basement_sqft, beds, baths, year_built, taxes, "
    "assessed/market value, lat/lon and the assessor_url.")
def lookup_property(county: str, address: str = "", parcel: str = "",
                    state: str = "co") -> dict:
    if not address and not parcel:
        return {"status": "invalid_request",
                "error": "Provide an address or a parcel number."}
    return _lookup(address=address or None, parcel=parcel or None,
                   county=county, state=state)


@mcp.tool(
    description="Diff an appraiser's MLS CSV export against county public "
    "records. subject_csv is a CSV whose first row is the subject property; "
    "comps_csv (optional) holds comparable rows. County is auto-detected from "
    "the CSV 'County' column when omitted. Returns one result per property with "
    "per-field {mls, assessor, diff, flag} and has_any_discrepancy. Understands "
    "PPMLS and RESO/REColorado column names.")
def check_mls_csv(subject_csv: str, comps_csv: str = "", county: str = "",
                  state: str = "co") -> dict:
    try:
        subject_rows = _read_rows(subject_csv)
    except (OSError, ValueError) as e:
        return {"error": f"cannot read subject_csv: {e}"}
    subject = subject_rows[0] if subject_rows else {}
    comps = []
    if comps_csv:
        try:
            comps = _read_rows(comps_csv)
        except (OSError, ValueError) as e:
            return {"error": f"cannot read comps_csv: {e}"}
    results = check_public_records(subject, comps, county=county or None,
                                   state=state)
    flagged = [r["label"] for r in results if r.get("has_any_discrepancy")]
    return {"results": results, "count": len(results), "flagged": flagged}


@mcp.tool(
    description="List every county the tool can serve right now: packaged "
    "defaults plus any auto-discovered counties cached for this user. Returns "
    "the registry keyed by '{COUNTRY}/{STATE}/{kind}:{name-slug}' (e.g. "
    "'US/CO/county:el-paso') with each source's platform.")
def list_counties() -> dict:
    reg = _load_registry()
    return {"counties": reg, "count": len(reg)}


@mcp.tool(
    description="Auto-detect an assessor source for a county the tool doesn't "
    "know yet, API-first. Tier 1 (full building data): Spatialest or EagleWeb. "
    "Tier 2 (baseline, no building data): Colorado statewide parcel API. Set "
    "save=true to cache the hit for future lookups. Returns the discovered "
    "registry entry (with its tier) or a not_found note.")
def discover_county(county: str, state: str = "co", save: bool = False) -> dict:
    entry = _discover_county(county, state, verbose=False)
    if not entry:
        return {"status": "not_found", "county": county, "state": state,
                "note": "No supported source detected. Add a client by hand "
                        "(see the add_new_county prompt) or fall back to the "
                        "Spatialest slug guess."}
    saved = None
    if save:
        saved = save_discovered_entry(county, entry, state)
    return {"status": "found", "county": county, "entry": entry,
            "tier": entry.get("tier"), "saved_as": saved}


@mcp.tool(
    description="Run the regression + benchmark harness. offline=true runs only "
    "the fast offline parser micro-benchmark (no network). Otherwise re-fetches "
    "golden properties for every county (or those matching 'filter') and reports "
    "PASS/WARN/FAIL per case plus latency by platform. A FAIL means a county "
    "site changed or a parser regressed.")
def run_regression(offline: bool = False, filter: str = "") -> dict:
    harness = _import_harness()
    if harness is None:
        return {"error": "packaged regression harness unavailable"}
    if offline:
        bench = harness.run_parser_bench(iterations=100, progress=lambda *_: None)
        return {"mode": "offline", "parser_bench": bench}
    golden = harness.load_golden()
    if not golden:
        return {"error": "no golden records — run `python tests/harness.py "
                         "--capture` first"}
    cases = [c for c in harness.load_cases()
             if filter.lower() in c["id"].lower()]
    if not cases:
        return {"error": f"no cases match filter {filter!r}"}
    results = harness.run_regression(cases, golden, delay=0.3,
                                     progress=lambda *_: None)
    summary = {r["result"]: 0 for r in results}
    for r in results:
        summary[r["result"]] = summary.get(r["result"], 0) + 1
    return {"mode": "live", "cases": results,
            "latency_by_platform": harness.platform_aggregates(results),
            "summary": summary,
            "overall": "FAIL" if any(r["result"] == "FAIL" for r in results)
            else ("WARN" if any(r["result"] in ("WARN", "NO-GOLDEN")
                                for r in results) else "PASS")}


@mcp.tool(
    description="Micro-benchmark the record parsers offline (no network). Fast "
    "sanity check that the Spatialest and EagleWeb parsers still run and how "
    "long they take per call.")
def benchmark() -> dict:
    harness = _import_harness()
    if harness is None:
        return {"error": "packaged regression harness unavailable"}
    return {"parser_bench": harness.run_parser_bench(
        iterations=200, progress=lambda *_: None)}


@mcp.tool(
    description="Ping a county live and report how it reacts: which platform "
    "serves it, how long a lookup takes, and — given a sample address or "
    "parcel from that county — exactly which record fields come back "
    "populated. Use this to see whether a county in the user's locale is "
    "usable and 'check-ready' (has the building fields GLA/beds/baths/year). "
    "Resolving an unknown county auto-discovers and caches its source.")
def probe_county(county: str, state: str = "co", address: str = "",
                 parcel: str = "") -> dict:
    harness = _import_harness()
    if harness is None:
        return {"error": "packaged regression harness unavailable"}
    return harness.probe_coverage(county, state, address=address, parcel=parcel)


@mcp.tool(
    description="Onboard a county for repeated use in the user's locale: "
    "auto-discover and cache the assessor source, probe field coverage with a "
    "sample property, and (unless capture=False) pin a golden record so every "
    "future regression run re-checks that county. This is how you 'point the "
    "tool at my county and configure it once.' Provide a sample address or "
    "parcel from that county. Returns the coverage report plus whether it was "
    "onboarded and under what case id.")
def onboard_county(county: str, state: str = "co", address: str = "",
                   parcel: str = "", capture: bool = True) -> dict:
    harness = _import_harness()
    if harness is None:
        return {"error": "packaged regression harness unavailable"}
    return harness.onboard_county(county, state, address=address,
                                  parcel=parcel, capture=capture)


# ---------------------------------------------------------------------------
# resources
# ---------------------------------------------------------------------------
@mcp.resource("assessor://operating-manual", mime_type="text/markdown",
              description="Coordinator role, agent topology, data policy, and "
              "standard workflows (same text as the server instructions).")
def operating_manual() -> str:
    return OPERATING_MANUAL


@mcp.resource("assessor://architecture", mime_type="text/markdown",
              description="Repository architecture: clients, registry/dispatch, "
              "checker, discovery ladder, and the harness (live from the repo).")
def architecture() -> str:
    parts = []
    for name in ("CLAUDE.md", "README.md"):
        p = _REPO_ROOT / name
        if p.exists():
            parts.append(f"<!-- {name} -->\n" + p.read_text(encoding="utf-8"))
    if parts:
        return "\n\n---\n\n".join(parts)
    try:
        from importlib.resources import files
        return files("assessor_lookup").joinpath(
            "architecture.md").read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError):
        return OPERATING_MANUAL


@mcp.resource("assessor://counties", mime_type="application/json",
              description="Current county registry (packaged defaults + "
              "user-discovered), keyed by '{COUNTRY}/{STATE}/{kind}:{name-slug}'.")
def counties_resource() -> str:
    return json.dumps(_load_registry(), indent=2, sort_keys=True)


@mcp.resource("assessor://golden-records", mime_type="application/json",
              description="Pinned golden records the regression harness checks "
              "each county against.")
def golden_resource() -> str:
    try:
        from importlib.resources import files
        return files("assessor_lookup").joinpath(
            "golden_records.json").read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError):
        return "{}"


@mcp.resource("assessor://harness-guide", mime_type="text/markdown",
              description="How to use the benchmark + regression harness and "
              "read its results.")
def harness_guide() -> str:
    return HARNESS_GUIDE


# ---------------------------------------------------------------------------
# prompts
# ---------------------------------------------------------------------------
@mcp.prompt(description="Run a public-records discrepancy check for an "
            "appraiser's subject + comps and summarize what to fix.")
def appraisal_check(subject_csv: str, comps_csv: str = "",
                    county: str = "") -> str:
    return (
        f"Run an assessor public-records check.\n\n"
        f"1. If '{county or '(auto-detect from CSV)'}' isn't in `list_counties`, "
        f"call `discover_county` for it first.\n"
        f"2. Call `check_mls_csv` with subject_csv={subject_csv!r}, "
        f"comps_csv={comps_csv!r}, county={county!r}.\n"
        f"3. Report each property. Focus on STABLE fields — GLA, beds, baths, "
        f"year built, basement — where flag=true; those are real discrepancies "
        f"to reconcile before the report goes out. Note any 'not_found' or "
        f"'timeout' properties the appraiser should check manually.\n"
        f"4. For any flagged property, include the assessor_url so they can "
        f"verify the record."
    )


@mcp.prompt(description="Onboard the counties in an appraiser's locale so they "
            "are configured once and reused. Give a comma-separated county list "
            "and, ideally, a sample address or parcel for each.")
def onboard_locale(counties: str, state: str = "co", samples: str = "") -> str:
    return (
        f"Onboard these {state.upper()} counties for repeated use: {counties}.\n"
        f"Sample properties (address or parcel per county, if provided): "
        f"{samples or '(none — ask the user for one per county, or a recent MLS export)'}.\n\n"
        f"For each county:\n"
        f"1. `probe_county(county, state={state!r}, address|parcel=<sample>)`. "
        f"Report the platform, latency, coverage (X/11 fields), and — key — "
        f"`check_ready`. If true, the building fields (GLA/beds/baths/year) come "
        f"through and the discrepancy check is fully useful. If false, say "
        f"plainly that this county's public data is thinner (owner/legal/value "
        f"only) — that's the county, not a bug.\n"
        f"2. `onboard_county(...)` to cache the source and pin a golden record, "
        f"so the county is configured once and re-checked on every run.\n"
        f"3. After onboarding, `run_regression` to show all their counties "
        f"reacting, then tell them they can point MLS exports at `check_mls_csv`.\n"
        f"Summarize as a coverage table (county, platform, check-ready, notes)."
    )


@mcp.prompt(description="End-to-end coordinator workflow to add support for a "
            "brand-new county, API-first, verified with a golden record.")
def add_new_county(county: str, state: str = "co") -> str:
    return (
        f"Add support for {county} County, {state.upper()}. Act as coordinator; "
        f"read `assessor://architecture` and the operating manual first.\n\n"
        f"1. `discover_county(county={county!r}, state={state!r})`. If it "
        f"resolves to a tier-1 source, you may be done — verify with a real "
        f"lookup and skip to step 5.\n"
        f"2. If not (or only the tier-2 baseline, which lacks building data), "
        f"map the county's assessor site. Delegate the site-mapping to an "
        f"explorer sub-agent. **API-first**: look for a county/state ArcGIS "
        f"service or vendor JSON; confirm it actually carries GLA/beds/baths/"
        f"year built. Only fall back to scraping the search+detail HTML if no "
        f"API has the building fields.\n"
        f"3. Implement the client in `platforms/<name>.py` (or a driver in "
        f"`jurisdictions/<country>/<state>/`) whose `lookup` (and "
        f"ideally `lookup_by_parcel`) returns the standard record dict with a "
        f"`status` key. Mirror an existing client on the same platform family "
        f"(Spatialest JSON, ArcGIS like jurisdictions/us/co/adams.py, or scraped like "
        f"platforms/eagleweb.py).\n"
        f"4. Register the platform with one line in the `PLATFORMS` dict and add a "
        f"`county_registry.json` entry.\n"
        f"5. Add a golden case to `DEFAULT_CASES` in `assessor_lookup/harness.py`, then "
        f"`python tests/harness.py --capture --filter <id>`.\n"
        f"6. `run_regression` to confirm PASS. Have a reviewer sub-agent verify "
        f"the parser against the live site. Keep it bounded: one client module, "
        f"one registry entry, one golden case."
    )


def main():
    mcp.run()


if __name__ == "__main__":
    main()
