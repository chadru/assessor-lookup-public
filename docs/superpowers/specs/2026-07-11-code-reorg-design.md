# Code reorganization: jurisdictions, platforms, and adapters

**Date:** 2026-07-11
**Status:** ✅ Implemented, reviewed (multi-agent + codex), and verified live — 2026-07-11

## Problem

The package is organized as one flat module per data source, and the registry
conflates two different concepts under `platform`:

- True platforms (Spatialest, Tyler EagleWeb) — vendor software serving many
  jurisdictions, where a new jurisdiction is pure configuration.
- Jurisdiction names masquerading as platforms (`adams`, `arapahoe`, `jeffco`)
  — counties whose clients are really bespoke drivers on top of a generic
  platform (Esri ArcGIS REST for Adams/Arapahoe/the statewide layer; Aumentum
  for Jefferson).

Consequences: the two ArcGIS clients duplicate query logic; `_get_client` is a
growing if/elif chain; the registry key format (`"CO:El Paso"`) has no room
for country or for non-county assessor jurisdictions (Louisiana parishes,
Virginia independent cities); and there is no seam for future authenticated or
browser-driven sources.

## Goals

- [x] 1. Adding a jurisdiction on a supported platform costs a registry entry
   (config), never code. *(Verified: every Spatialest/EagleWeb/Aumentum county
   is registry data only.)*
- [x] 2. Adding a bespoke jurisdiction costs one driver module in a predictable
   location plus a registry entry. *(Verified: `jurisdictions/us/co/{adams,
   arapahoe,statewide}.py` + `config.driver` references.)*
- [x] 3. Adding a platform costs one module plus one registration line.
   *(Verified: `PLATFORMS` dict in `platforms/__init__.py`; completeness test
   constructs every packaged entry.)*
- [x] 4. Jurisdiction model covers country/state/kind/name. *(Verified:
   canonical keys + `kind`; ambiguity between same-named kinds now fails
   closed via `AmbiguousJurisdiction`.)*
- [x] 5. Clean constructor seam for future auth/transport, nothing built now.
   *(Verified: `access` block + uniform factory construction; no Transport
   classes exist.)*

## Hard constraints

- [x] **MCP contract unchanged** — verified by `tests/test_mcp_stdio.py`
  (subprocess contract surface) and 9/9 live lookups through a reconnected
  Claude Code session.
- [x] **CLI and top-level Python API unchanged** — verified by the suite and
  live CLI/`lookup()` runs.
- [x] **Existing user config keeps working** — verified live: a real legacy
  `"CO:Boulder"` user entry normalized and resolved end-to-end through the
  statewide driver.
- [x] Stdlib-only core — `pyproject.toml` diff adds no runtime deps; wheel
  build inspected.
- [x] Clients never raise on failed lookups — hardened post-review: bad
  platform/driver/ambiguous name now return explicit `api_error` status dicts
  (`_InvalidConfigClient`); registry loader contains malformed files/entries.
- [x] Golden records / harness case ids / benchmark aggregation compatible —
  live harness PASS (10 cases) after the reorg and after the review fixes;
  ids unchanged, platform labels unified via `platform_label()`.
- [x] US-only with reserved country level — `US` fixed in canonical keys.

## Target layout

```
assessor_lookup/
  __init__.py            # unchanged public API
  core/
    statuses.py          # NEW: status constants + standard record-key names
    matching.py          # moved, unchanged
    network.py           # moved, unchanged (security boundary)
  platforms/             # vendor/protocol code — ZERO jurisdiction knowledge
    __init__.py          # PLATFORMS registration dict + AssessorAdapter Protocol
    spatialest.py        # from assessor.py (client parts); jurisdiction = slug config
    eagleweb.py          # from assessor_eagleweb.py; jurisdiction = base-URL config
    aumentum.py          # from assessor_jeffco.py; jurisdiction = base-URL config
    arcgis.py            # NEW: shared Esri REST primitives (query building,
                         #   feature fetch, error normalization, ONE mockable
                         #   HTTP seam replacing the duplicated _arcgis_get)
  jurisdictions/         # bespoke drivers; path mirrors the canonical key
    us/co/
      adams.py           # ArcGIS driver: layers, field map, parcel→improvements→values
      arapahoe.py        # ArcGIS driver: geocode → spatial-query flow
      statewide.py       # from assessor_coparcel.py: CO parcel baseline layer
  registry.py            # NEW: jurisdiction model, canonicalization, load/save/merge
                         #   (extracted from assessor.py + checker.py)
  checker.py, discovery.py, cli.py, mcp_server.py, harness.py, card.py  # stay top-level
  county_registry.json   # packaged data, migrated to the new entry format
```

Rules that keep this manageable at scale:

- Most jurisdictions never get a file. Drivers exist only for bespoke flows;
  config-only jurisdictions live purely in the registry. `jurisdictions/`
  grows with *weird* counties, not all counties.
- Dependency direction is one-way: `jurisdictions/*` import `platforms/*`,
  never the reverse.
- Kind collisions in a state directory (e.g. Virginia's Richmond city vs
  Richmond County) are resolved lazily with a kind suffix in the filename
  (`richmond_city.py`) only when they occur.
- `county_registry.json` stays a single packaged file until unwieldy; the
  loader already merges multiple sources, so sharding to
  `registry/us/co.json` later is mechanical.
- `assessor.py` is removed outright (maintainer decision during
  implementation: no compatibility shims). All internal call sites were
  repointed to `platforms/spatialest.py` / `registry.py`; external code
  importing `assessor_lookup.assessor` must update to the new paths.

## Jurisdiction and registry model

Canonical key: `"{COUNTRY}/{STATE}/{kind}:{name-slug}"`, e.g.
`"US/CO/county:el-paso"`. Slugs are lowercased, punctuation stripped,
spaces to hyphens. Entry shape:

```json
{
  "US/CO/county:el-paso": {
    "jurisdiction": {"country": "US", "state": "CO", "kind": "county", "name": "El Paso"},
    "platform": "spatialest",
    "config": {"slug": "elpaso"},
    "access": {"mode": "public"}
  },
  "US/CO/county:adams": {
    "jurisdiction": {"country": "US", "state": "CO", "kind": "county", "name": "Adams"},
    "platform": "arcgis",
    "config": {"driver": "us.co.adams"}
  },
  "US/CO/county:jefferson": {
    "jurisdiction": {"country": "US", "state": "CO", "kind": "county", "name": "Jefferson"},
    "platform": "aumentum",
    "config": {"base": "https://propertysearch.jeffco.us/api"}
  }
}
```

- `kind` is an open string set: `county`, `parish`, `independent_city`,
  `municipality`, ... It models "unique assessor jurisdiction", not a
  geographic tree.
- `config.driver` is explicit (a dotted path under `jurisdictions/`), not
  auto-derived from the key, because drivers can be shared —
  `us.co.statewide` serves ~40 counties' entries.
- `access` defaults to `{"mode": "public"}`. **Credentials never live in the
  registry.** A future authenticated platform stores an env-var *reference*,
  resolved at client construction.

### Backward compatibility

`registry.py` normalizes both formats at load time:

- Old key `"CO:El Paso"` → canonical `"US/CO/county:el-paso"` with
  `kind: "county"`, `country: "US"`.
- Old platform aliases → new (platform, config):
  `"adams"` → `("arcgis", {"driver": "us.co.adams"})`,
  `"arapahoe"` → `("arcgis", {"driver": "us.co.arapahoe"})`,
  `"jeffco"` → `("aumentum", {"base": <jeffco base>})`,
  `"co_parcel_api"` → `("arcgis", {"driver": "us.co.statewide"})`.
- Packaged + user registries merge by canonical identity (user wins).
- Old format is readable forever. All writes (including
  `save_discovered_entry`) emit the new format immediately; new-format user
  config only breaks downgrades, which don't matter one release in.

### Resolution order (`_get_client`)

1. Exact canonical key (country defaults `US`, kind defaults `county`).
2. Legacy-key normalization of the same input.
3. **State-scoped** partial name match across kinds within the given state
   — fixing the current state-blind `county.lower() in key.lower()` match,
   which would misroute same-named counties once multi-state. Spanning kinds
   is what lets `("Orleans", "la")` find `US/LA/parish:orleans` through the
   unchanged MCP signature.
4. Discovery ladder (unchanged tier semantics; returns normalized entries).
5. Spatialest slug fallback (unchanged).

## Adapter contract

- Runtime stays duck-typed. A `typing.Protocol` in `platforms/__init__.py`
  documents the contract for maintainers and tests:
  `lookup(address, **ctx) -> dict`; optional
  `lookup_by_parcel(parcel_id, **ctx) -> dict`. No ABC — the platforms vary
  too much; an ABC would force artificial methods.
- Class-level `capabilities` dict per client:
  `{"parcel_lookup": bool, "building_fields": bool}`. Replaces implicit
  `hasattr` checks in `checker.py` and makes the statewide layer's "no
  building data" limitation machine-readable (probe/onboard coverage
  reporting can consult it).
- Uniform construction:
  `Client(entry, timeout=..., verbose=..., transport=None)` where `entry` is
  the normalized registry entry. `transport=None` means the existing urllib
  path and **is the entire access-mode seam** — no Transport/AuthStrategy
  classes until a second concrete consumer exists.
- Status dicts unchanged; string literals get named constants in
  `core/statuses.py`, adopted as files move.

## Dispatch

`platforms/__init__.py`:

```python
PLATFORMS = {
    "spatialest": _spatialest_factory,
    "eagleweb":   _eagleweb_factory,
    "aumentum":   _aumentum_factory,
    "arcgis":     _arcgis_factory,   # resolves config["driver"] to a
                                     # jurisdictions.<path> module
}
```

Factories keep today's lazy imports. `checker._get_client` shrinks to
resolve-entry + dict lookup. Legacy platform names resolve via the alias
table in `registry.py`, so existing user configs and the discovery cache
keep working.

The design test the structure encodes: *"another jurisdiction on the same
software" must cost a driver/config, never a platform.* (Concrete example: if
Arapahoe is ever switched to its actual assessor front-end, DEVNET wEdge, that
becomes `platforms/wedge.py` reusable by every wEdge county.)

## Discovery

`discovery.py` keeps its best-data-first ladder, fail-closed probes, and tier
semantics. Changes only:

- Returns normalized registry entries.
- CO-specific probes (statewide layer membership, EagleWeb
  `assessor.co.<county>.co.us` host convention) are guarded by
  `country == "US" and state == "co"`; the structure for another state's
  ladder is obvious but not built.

## Error handling

Unchanged by design: status-dict contract, fail-closed discovery,
`network.py` HTTPS/SSRF guardrails. Adapters keep declaring their allowed
hosts — registry-supplied URLs must not become an SSRF bypass through any
future configurable transport. The one intentional behavior change is the
state-scoped partial match (latent-bug fix).

## Testing and migration safety

- Preserve HTTP-boundary mock seams: `_spatialest_request` moves with its
  module; Adams/Arapahoe tests converge on the single seam in
  `platforms/arcgis.py`. Fixtures unchanged; test imports re-pointed.
- New offline tests: key canonicalization (old → canonical), legacy user
  config file loading, platform alias resolution, merge identity,
  state-scoped resolution, PLATFORMS completeness (every registry platform
  name constructs), capabilities presence per client.
- Golden records, harness case ids (`county` + `state`), user
  cases/goldens: untouched. Canonical jurisdiction id is derived internally
  only; harness platform aggregation keys stay stable via the alias table.
- Verification gate: full `pytest` offline, then `python tests/harness.py`
  live before and after the move to prove behavior-identical output.
- Docs updated in the same change: `CLAUDE.md`,
  `assessor_lookup/architecture.md` (the MCP architecture resource), and the
  `.claude/` agent playbooks that describe module layout.

### Suggested implementation order (each step green before the next)

- [x] 1. `core/` extraction (statuses, matching, network) + import updates.
- [x] 2. `registry.py`: canonical model, dual-format loader, alias table,
   state-scoped resolution; migrate packaged `county_registry.json`.
   *(Packaged-file migration landed with step 3 so the suite stayed green.)*
- [x] 3. `PLATFORMS` dict replacing the if/elif dispatcher.
- [x] 4. `platforms/arcgis.py` shared primitives; Adams/Arapahoe/statewide
   rewritten as `jurisdictions/us/co/` drivers.
- [x] 5. Spatialest/EagleWeb/Aumentum moved into `platforms/` (no shim —
   `assessor.py` deleted; internal imports repointed).
- [x] 6. Discovery normalization + new-format writes.
- [x] 7. Docs + agent playbooks updated; live harness verification (PASS).

### Post-implementation hardening (multi-agent review, same day)

- [x] Never-raise containment: `_InvalidConfigClient` for unknown platform /
  invalid driver / ambiguous jurisdiction; registry loader survives `[]`,
  `null`, and non-dict entries.
- [x] `resolve()` fail-closed ambiguity guard + empty-query rejection.
- [x] `capabilities` adopted (Spatialest slug/state baked in at construction;
  `hasattr`/platform-name branching removed from `checker`/`__init__`).
- [x] `core/statuses.py` completed (`ambiguous`, `invalid_parcel`); docs
  aligned.
- [x] ArcGIS SQL-literal escaping wired into all drivers; atomic registry
  writes; hermetic test suite (`tests/conftest.py`); stale add-county
  docs/prompts corrected.
- Verified by: 229 offline tests, live golden harness PASS, MCP stdio tests,
  and 9/9 live MCP lookups from a Claude Code session.

## Explicitly out of scope

- Transport/AuthStrategy class hierarchies; Playwright/proxy abstractions.
- Plugin entry points for third-party platform packages.
- Declarative ArcGIS county descriptors (revisit after 2–3 genuinely
  cookie-cutter ArcGIS counties exist; C-style data-driven drivers are a
  destination, not the foundation).
- A record dataclass — the permissive dict is the contract.
- International support beyond the reserved country segment.
- Any MCP, CLI, or top-level API signature change.

## Independent review

Design was cross-checked with a Codex (read-only) architecture review, which
concurred with the minimal-split approach, warned against forcing
Adams/Arapahoe into one declarative adapter on day one, and contributed the
resolution-order/state-blindness, harness-identity, SSRF, and mock-seam
cautions folded in above.
