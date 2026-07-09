# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## Commands

```bash
# Install for development (stdlib-only core; extras are optional)
pip install -e ".[dev]"            # + pytest
pip install -e ".[card]"           # + playwright, then: playwright install chromium

# Run the test suite (network tests deselected by default in most workflows)
pytest                             # all tests
pytest -m "not network"            # skip tests that hit live county APIs
pytest -m network                  # only the live-API integration tests
pytest tests/test_assessor.py::TestSpatialestRecordcardParsesFields   # single class
pytest tests/test_assessor.py::TestFieldNameVariants::test_parse_sqft_sfla  # single test

# Benchmark + golden-record regression harness (live county endpoints)
python tests/harness.py            # full run: regression + latency + discovery + parser bench
python tests/harness.py --capture  # (re)pin golden records after legitimate data changes
python tests/harness.py --filter clear   # only cases matching a substring
python tests/harness.py --offline        # parser micro-bench only, no network
pytest -m network tests/test_golden_live.py   # same goldens as pytest

# CLI (installed as the `assessor-lookup` entry point)
assessor-lookup lookup "123 Main St" --county "El Paso"
assessor-lookup lookup --parcel 5227004002 --county Adams --json
assessor-lookup counties
assessor-lookup check subject.csv comps.csv --county "El Paso"
```

There is no build/lint tooling configured — this is a stdlib-only package. All HTTP uses `urllib`; **do not add `requests` or other runtime dependencies** to the core (`dependencies = []` in `pyproject.toml` is intentional). Playwright is confined to the optional `[card]` extra and imported lazily inside `card.py`.

## Architecture

The package turns a street address or parcel number into a **normalized property-record dict**, and diffs that record against MLS CSV data. Two layers:

### 1. Assessor clients (one per county platform)
Each client exposes the same duck-typed contract: `lookup(address, ...)` and optionally `lookup_by_parcel(parcel_id, ...)`, each returning a dict whose `status` is one of `success`, `not_found`, `timeout`, `api_error`, `parse_error`, `invalid_address`. On `success` the dict carries the standard record keys (`owner`, `legal`, `parcel_number`, `above_grade_sqft`, `basement_sqft`, `beds`, `baths`, `year_built`, `tax_amount`, `assessed_value`, `market_value`, `latitude`, `longitude`, `assessor_url`, `neighborhood`, `zoning`, `lot_size_sqft`, `garage_area_sqft`, ...).

- `assessor.py` — `SpatialestClient`, the default. Handles the multi-county Spatialest hosted platform (El Paso, Denver, Douglas, and any unregistered county via slug fallback). Two-step flow: POST `/api/v2/search` → property id, then GET `/api/v1/recordcard/{id}`. **Spatialest returns HTTP 419 when a request lacks a page/CSRF token** — `_spatialest_request` catches 419 and replays via `_spatialest_request_with_page_token`, which scrapes the `csrf-token` meta tag and reuses the session cookie. Field extraction is defensive: `_extract_all_fields` recursively flattens sections (handles both El Paso's dict-keyed and Denver's list-indexed formats), then parsers like `_parse_sqft` / `_parse_lot_area` try a long ordered list of possible field-name aliases because each county names the same datum differently.
- `assessor_jeffco.py` — `JeffcoClient`, Jefferson County's Aumentum REST API.
- `assessor_arapahoe.py` — `ArapahoeClient`, ArcGIS MapServer (geocode → parcel/owner/building layers); a lookup is sequential but callers must still respect rate limits.
- `assessor_adams.py` — `AdamsClient`, ArcGIS FeatureServer. The compact reference example for adding a new ArcGIS-style platform.
- `assessor_eagleweb.py` — `EagleWebClient`, Tyler's "EagleWeb / taxweb" JSP app (Clear Creek). **No JSON API exists** — this drives the public *guest* session (GET `/web/` for a cookie → POST `/web/loginPOST.jsp` with `guest=true` → POST `/taxweb/results.jsp` search → GET `/taxweb/account.jsp`) and parses HTML. Building data (GLA/beds/baths/year) lives only on the per-account **residential improvement** detail page, reached via a *dynamic* `doc=DOC…` id that must be discovered from the summary page, not hardcoded. GLA = the "Square Feet" column of the Abstract-Code/Areas table; tax is computed `assessed × mill levy / 1000` split school/non-school. Registry entry carries a `base` URL. This is the reference for scraping-based platforms.
- `assessor_coparcel.py` — `CoParcelClient`, the Colorado **statewide** public-parcel ArcGIS layer (`gis.colorado.gov`, ~40 counties). Clean JSON but **parcel/owner/legal/value/land only — no building characteristics**, so beds/baths/GLA/year are always `None` with a `data_note`. This is the tier-2 *baseline* for auto-discovery, not a full assessor client.

### Data-source policy (important)
**API-first, scrape as fallback.** Before writing a scraper for a county, probe for a JSON API (county/state ArcGIS, vendor JSON like Spatialest). But note the trap Clear Creek illustrates: the clean statewide ArcGIS API exists yet **lacks the building fields the `check` command compares** (GLA/beds/baths/year) — those often live only in the assessor's HTML front-end. So "has an API" ≠ "API is sufficient"; verify the API carries building data before preferring it over a scraper.

### Auto-discovery (`discovery.py`)
`discover_county(county, state)` maps an unknown county to a registry entry via a best-data-first ladder: **tier 1** = full building data detectable by URL (Spatialest slug probe → EagleWeb CO host convention `assessor.co.<hyphenated-county>.<state>.us/eagleassessor`); **tier 2** = the CO statewide parcel API baseline (membership set `_CO_PARCEL_COUNTIES`). Probes fail closed (any error → "not this platform"). Hits are cached to a per-user registry (`~/.config/assessor-lookup/county_registry.json`, override `ASSESSOR_LOOKUP_HOME`) which `_load_registry` merges over the packaged defaults. `checker._get_client` calls discovery inline for unregistered counties and persists the hit; the `discover` CLI command does the same explicitly.

### 2. Registry + dispatch + checker
- `county_registry.json` — maps `"CO:<County>"` → `{platform, slug, state}`. Packaged as `package-data`. Unregistered counties fall back to Spatialest with `slug = county.lower().replace(" ", "")`.
- `checker._get_client(county, state)` — the dispatcher. Resolves a registry entry (exact key, then partial-name match, then Spatialest fallback) and returns `(client_instance, entry)`. **This is the single place that knows which platform class serves which county** — new platforms are wired in here.
- `checker.check_public_records(subject, comps, county=...)` — the diff engine. Auto-detects county from the CSV `County`/`State` columns when not passed. Extracts MLS fields (`_extract_mls_fields`: GLA = Main+Upper SqFt, beds/baths are above-grade = total − basement), looks each property up (preferring parcel over address), and compares field-by-field. GLA and basement use a **50-sqft threshold** (`GLA_THRESHOLD`/`BASEMENT_THRESHOLD`); beds/baths/year flag on any mismatch. Returns a list of result dicts with `has_any_discrepancy` and per-field `{mls, assessor, diff, flag}`.
- `checker.apply_overrides` / `resolve_discrepancies` — interactive/auto reconciliation that writes chosen values back into the MLS row dicts in place (the `FIELD_MAP` re-splits above-grade beds/baths and GLA back into the MLS column layout).
- `__init__.py` — public API surface: `lookup()`, `check_public_records`, `detect_county`, `print_discrepancy_report`, `render_assessor_card_pdf`. `lookup()` is the convenience wrapper around `_get_client` + client dispatch for a single property.

### MLS CSV assumptions
`check` reads standard MLS exports and understands both **PPMLS** and **RESO/REColorado** column names. `_normalize_address` rewrites PPMLS's non-standard street suffixes (e.g. `CR`→`Cir`) to USPS forms the assessor APIs recognize. Parcel/schedule numbers are read from any of several column aliases (`Schedule Number`, `Parcel Number`, `Parcel`, `Parcel ID`, `APN`).

## Predefined agents & skills (`.codex/` and `.agents/`)
On clone, Codex auto-discovers agent definitions from `.codex/agents/` and skills from `.agents/skills/`, so it can use named roles instead of improvising. Agents: **coordinator** (entry point; routes work, enforces API-first + honest coverage reporting), **explorer** (read-only; maps an unknown county's site, API-first), **reviewer** (adversarial verification against the live site + golden), and **county-onboarder** (probe + onboard counties). Skills: **onboard-locale**, **appraisal-check**, and **add-county**. These mirror the Claude Code definitions under `.claude/` and the MCP prompts. `tests/test_agents_skills.py` validates all three surfaces so casing or metadata mistakes cannot silently make one undiscoverable. The agent files are included in the source distribution, not the runtime wheel.

## MCP server (`assessor_lookup/mcp_server.py`)
The MCP is a local stdio service, not an authenticated network server.
`check_mls_csv` accepts only `.csv` files beneath the process working directory
or explicit `ASSESSOR_LOOKUP_MCP_DATA_DIR`, including after symlink resolution.

`FastMCP` server (optional `[mcp]` extra, Python 3.10+; entry point `assessor-lookup-mcp`, and `python -m assessor_lookup.mcp_server`) that exposes the package as an agent-ready service. It wraps the *same* public functions — `lookup`, `check_public_records`, `_load_registry`, `discover_county`, and the packaged `assessor_lookup.harness` — so there is no logic duplication; the MCP layer is a thin adapter. Tools: `lookup_property`, `check_mls_csv`, `list_counties`, `discover_county`, `probe_county`, `onboard_county`, `run_regression`, `benchmark`. Resources: `assessor://operating-manual`, `assessor://architecture`, `assessor://counties`, `assessor://golden-records`, `assessor://harness-guide`. Prompts: `appraisal_check`, `onboard_locale`, `add_new_county`. The server instructions are the coordinator playbook and API-first policy. Harness tools work from both a clone and an installed wheel; the repo's `tests/harness.py` is only a thin CLI wrapper. Bundled `.mcp.json` lets Codex auto-discover it. Tests: `tests/test_mcp_server.py` (offline, `pytest.importorskip("mcp")`).

## Regression harness (`tests/harness.py`)
Packaged live cases must use government or institutional properties, never
private-home addresses. User-onboarded cases remain in per-user configuration.

The operational risk of this package is county websites changing silently. `assessor_lookup/golden_records.json` pins only parser-critical stable fields for known properties (captured live via `--capture`); `python tests/harness.py` re-fetches each and compares.

**Locale onboarding.** `--probe "County" --address/--parcel <sample>` pings a county live and reports field coverage via `probe_coverage()` — which of `COVERAGE_FIELDS` populate, plus `check_ready` (true when ≥3 of the 5 `BUILDING_FIELDS` come through; that's what the discrepancy check needs). `--onboard` runs `onboard_county()`: resolves+caches the source, probes, and pins a golden **case + record to the user config** (`~/.config/assessor-lookup/user_cases.json` + `user_golden.json`), separate from the committed repo baseline. `load_cases()` = `DEFAULT_CASES` + user cases (deduped by id); `load_golden()` merges repo + user goldens — so onboarded counties re-run on every `harness.py` invocation. This is how an appraiser points the tool at their own area and configures it once. Exposed to agents as the `probe_county`/`onboard_county` MCP tools and the `onboard_locale` prompt. **Stable** fields (`parcel_number`, `above_grade_sqft`, `basement_sqft`, `beds`, `baths`, `year_built`) FAIL on drift — a FAIL means either the parser broke or the property genuinely changed (remodel/sale); investigate, then re-capture if the world changed. **Volatile** fields (owner, values, taxes, legal) WARN only. The run also benchmarks latency per platform (history in `tests/.bench/`, gitignored), verifies the discovery ladder resolves correctly, and micro-benchmarks the parsers offline. Exit codes: 0 pass / 1 hard regression / 2 warnings. `tests/test_golden_live.py` exposes the same cases as `pytest -m network`; `tests/test_harness.py` unit-tests the comparator offline. When adding a county client, add a golden case to `DEFAULT_CASES` in the harness and run `--capture --filter <id>`.

## Adding a county
1. If it's Spatialest-hosted, often just add `{"CO:Name": {"platform": "spatialest", "slug": "...", "state": "co"}}` to `county_registry.json` — or rely on the slug fallback.
2. For a new platform, add a client class implementing `lookup` (and ideally `lookup_by_parcel`) returning the standard `status`+record dict, then wire the platform name into `_get_client`. Mirror `assessor_adams.py`.
3. Tests mock at the HTTP boundary (`_spatialest_request`, `_arcgis_get`, or `JeffcoClient._get_endpoint`) — never hit the network in non-`network` tests. Add fixtures like `MOCK_RECORDCARD_RESPONSE`.

## Conventions
- Clients must **never raise on a failed lookup** — always return a dict with an error `status`. Callers (`checker`, CLI) branch on `status`, not exceptions.
- When parsing assessor JSON, assume any field may be missing, empty, differently-named, or a display string with units/commas. Add aliases to the existing ordered field-name lists rather than branching per county.
- Parcel lookup is preferred over address lookup wherever a parcel/schedule number is available (more reliable than fuzzy address search).
