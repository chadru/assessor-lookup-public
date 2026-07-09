---
name: add-county
description: Add support for a brand-new county that auto-discovery does not already resolve, API-first, verified with a golden record. Use when a needed county isn't in the registry and probing/discovery can't map it to a usable source.
---

# Add a new county

Goal: a new county client that returns the standard record dict, wired into the
registry, and verified with a golden record — added in a bounded change (one
client module + one registry entry + one golden case).

## Steps
1. **Check discovery first.** `discover_county(county, state)` (MCP) or
   `assessor-lookup discover "<County>" --state <st>`. If it resolves to a
   tier-1 source, verify with a real `lookup_property` and you may be done. If
   it only hits the tier-2 baseline (owner/legal/value, no building data),
   continue.

2. **Map the site.** Delegate to the **explorer** agent with the county and a
   sample property. It returns, API-first, the platform, exact requests, and the
   field-name mapping — and confirms whether the building fields (GLA/beds/baths/
   year) are available. Honor its verdict: prefer a JSON API; scrape only if no
   API carries the building data.

3. **Implement the client** `assessor_lookup/assessor_<slug>.py`: a class whose
   `lookup(address)` (and ideally `lookup_by_parcel(parcel_id)`) returns the
   standard record dict with a `status` key, never raising on failure. Mirror
   the closest existing client: `assessor_adams.py` (ArcGIS JSON), `assessor.py`
   (Spatialest JSON), or `assessor_eagleweb.py` (scrape).

4. **Wire it in.** Add the platform branch to `checker._get_client` and a
   `county_registry.json` entry (`{"platform": "...", ...}`).

5. **Pin a golden.** Add a case to `DEFAULT_CASES` in `assessor_lookup/harness.py`, then
   `python tests/harness.py --capture --filter <case-id>`. Use a real
   single-family home (not a vacant lot or HOA tract) so building fields are
   exercised.

6. **Verify.** Hand off to the **reviewer** agent (live site + golden + parser
   correctness). Then confirm `pytest -m "not network"` and
   `python tests/harness.py --filter <case-id>` are green.

## Principle
Bounded change, API-first, verified against ground truth. Don't add frameworks;
one client module, one registry entry, one golden case.
