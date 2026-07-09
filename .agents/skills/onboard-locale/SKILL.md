---
name: onboard-locale
description: Configure the assessor sources for the counties in an appraiser's area so they are set up once and reused. Use when a user says where they work, lists counties they need, or wants to add their local counties to assessor-lookup.
---

# Onboard a locale

Goal: take the counties an appraiser works in and configure each one for
repeated use — cached source + a pinned golden record — reporting honestly which
are usable for the full discrepancy check.

## Steps
1. **Gather inputs.** Get the county list and one **sample property** per county
   (address or parcel). If the user has an MLS export, pull a sample from it;
   otherwise ask for one per county — coverage can't be measured without a real
   property. Delegate the run to the **county-onboarder** agent if you want it
   handled end-to-end.

2. **Probe each county** with the MCP tool `probe_county(county, state,
   address|parcel)` (or `python tests/harness.py --probe "<County>"
   --address "..."`). Read `check_ready`:
   - **true** → GLA/beds/baths/year come through; full discrepancy check works.
   - **false** → only owner/legal/value available; the county's public data is
     thinner. Say so plainly — it is the source's data, not a defect.

3. **Onboard the keepers** with `onboard_county(...)` (or
   `python tests/harness.py --onboard "<County>" --parcel ...`). This caches the
   assessor source and pins a golden record to the user config
   (`~/.config/assessor-lookup/`), so the county is re-checked on every run.

4. **If a county doesn't resolve at all**, switch to the `add-county` skill.

5. **Verify and report.** Run `run_regression` (or `python tests/harness.py`) to
   show every onboarded county reacting. Present a table:
   **county | platform | check-ready | latency | notes**. Then tell the user
   they can point MLS exports at the `appraisal-check` skill.

## Principle
API-first, and honesty about coverage. Never present a not-check-ready county's
blanks as if the check succeeded.
