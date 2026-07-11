# assessor-lookup architecture

`assessor-lookup` resolves a street address or parcel number to a normalized
county-assessor record and can compare that record with MLS CSV data.

## Runtime layers

1. `registry.py` models jurisdictions with canonical keys
   (`"US/CO/county:el-paso"`; `kind` covers county/parish/independent city)
   and reads both the canonical and legacy `"CO:El Paso"` entry formats,
   merging packaged defaults with the per-user registry. Credentials never
   live in the registry.
2. `checker._get_client(county, state)` resolves an entry (exact, then
   state-scoped name match, then API-first discovery) and constructs the
   client via the `PLATFORMS` dict in `platforms/__init__.py`.
3. Platform clients (`platforms/spatialest.py`, `platforms/eagleweb.py`,
   `platforms/aumentum.py`) carry no jurisdiction knowledge; bespoke ArcGIS
   flows are jurisdiction drivers (`jurisdictions/us/co/adams.py`,
   `arapahoe.py`, `statewide.py`) built on the shared primitives in
   `platforms/arcgis.py`. All return status-bearing dictionaries, never
   transport exceptions, and declare a `capabilities` dict
   (`parcel_lookup`, `building_fields`).
4. `checker.check_public_records` normalizes PPMLS or RESO/REColorado rows,
   prefers parcel lookup, and compares GLA, basement, beds, baths, and year.
5. `harness.py` owns packaged golden regression, discovery checks, parser
   benchmarks, coverage probes, and per-user county onboarding.
6. `mcp_server.py` exposes the same public functions, harness, resources, and
   workflows over stdio MCP without duplicating lookup logic.

## Data-source policy

Use a public JSON API when it supplies the required building fields. Fall back
to a public guest-session HTML workflow only when the API lacks those fields.
Unknown Colorado counties may resolve to the statewide parcel layer, which is
a deliberately thinner owner/legal/value baseline without building data.

## Result contract

Every client returns a dictionary whose `status` is one of `success`,
`not_found`, `ambiguous`, `timeout`, `api_error`, `parse_error`,
`invalid_address`, or `invalid_parcel`. Candidate selection fails closed when
multiple upstream properties cannot be matched deterministically.

## Persistence

Packaged defaults use government or institutional properties, and their
privacy-minimized golden records are read-only. Discovery and onboarding write
only to the per-user assessor-lookup configuration directory
(`ASSESSOR_LOOKUP_HOME` or `~/.config/assessor-lookup`).

The MCP is local stdio only. Its CSV tool resolves files beneath the working
directory or `ASSESSOR_LOOKUP_MCP_DATA_DIR`; resolved symlinks cannot escape
that root. Outbound assessor clients enforce HTTPS and expected fixed hosts.

## Verification

- `pytest -m "not network"` runs deterministic unit and MCP tests.
- `python tests/harness.py` runs live golden and discovery checks.
- `python tests/harness.py --offline` runs parser benchmarks without network.
