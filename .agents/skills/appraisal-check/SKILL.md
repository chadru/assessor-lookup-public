---
name: appraisal-check
description: Diff an appraiser's MLS export (subject + comps) against county assessor public records to flag GLA, beds, baths, year-built, and basement discrepancies before a report goes out. Use when given one or more MLS CSV files to check.
---

# Appraisal public-records check

Goal: turn an MLS export into a field-by-field discrepancy report against the
county's public records, so the appraiser fixes mismatches before the report
ships.

## Steps
1. **Identify the county.** It is auto-detected from the CSV `County` column; if
   absent, ask or infer. Confirm it is covered with `list_counties`. If it is
   not, run the `onboard-locale` skill first (a single county is fine).

2. **Run the check** with the MCP tool
   `check_mls_csv(subject_csv, comps_csv, county, state)`. The subject CSV's
   first row is the subject; the comps CSV holds comparables. Standard PPMLS and
   RESO/REColorado column names are both understood. (CLI equivalent:
   `assessor-lookup check subject.csv comps.csv --county "<County>"`.)

3. **Report the discrepancies.** For each property, show the flagged fields.
   Focus on the **stable** fields — GLA, beds, baths, year built, basement —
   where `flag` is true; those are the real discrepancies to reconcile. Values,
   owner, and taxes drift legitimately year to year, so don't alarm on those.
   Include the `assessor_url` for any flagged property so the appraiser can
   verify the record.

4. **Explain the common ones.** Multi-level homes (tri-level, 4-level) routinely
   show a large GLA diff because MLS counts the lower level as basement while
   the assessor counts it above-grade — flag it as expected, not an error.
   Bath counts often differ (assessor total vs MLS above-grade split).

## Principle
Report what the records actually say. Note any property that came back
`not_found` or `timeout` so the appraiser checks it manually — don't silently
drop it.
