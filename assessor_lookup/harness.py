"""Packaged benchmark + regression harness for assessor-lookup.

Runs every registered county against golden records (known properties with
pinned expected values), times each lookup, checks the auto-discovery ladder,
and micro-benchmarks the parsers. Detects when a county website changes out
from under us — the main operational risk of this package.

Usage (from the repo root):

    python tests/harness.py                  # full regression + benchmark run
    python tests/harness.py --capture        # (re)capture golden records live
    python tests/harness.py --filter clear   # only cases matching "clear"
    python tests/harness.py --json out.json  # also write machine-readable results
    python tests/harness.py --offline        # parser micro-bench only, no network

Point it at your own county (locale onboarding):

    # See how a county reacts and what fields it returns:
    python tests/harness.py --probe "El Paso" --address "1675 W Garden of the Gods Rd"
    # Configure a county for repeated use (discover, probe, pin a golden):
    python tests/harness.py --onboard "El Paso" --parcel 5509302001

Onboarded counties are saved to your user config (~/.config/assessor-lookup/)
and re-checked on every subsequent run alongside the packaged defaults.

Field policy:
  * STABLE fields (parcel_number, above_grade_sqft, basement_sqft, beds,
    baths, year_built) must match the golden record exactly -> FAIL on drift.
    Real drift (a remodel) is indistinguishable from a broken parser; a FAIL
    means "look at this county", then re-capture if the world really changed.
  * VOLATILE fields (owner, values, taxes, legal, style, neighborhood) change
    legitimately year to year -> WARN only.

Exit codes: 0 = all pass, 1 = hard regression (lookup failed or stable field
drifted), 2 = warnings only.
"""

import argparse
import json
import os
import re
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
GOLDEN_PATH = HERE / "golden_records.json"
_REPO_ROOT = HERE.parent
HISTORY_DIR = ((_REPO_ROOT / "tests" / ".bench")
               if (_REPO_ROOT / "tests").is_dir()
               else Path.home() / ".cache" / "assessor-lookup" / "bench")

STABLE_FIELDS = ("parcel_number", "above_grade_sqft", "basement_sqft",
                 "beds", "baths", "year_built")
VOLATILE_FIELDS = ("owner", "legal", "style", "market_value",
                   "assessed_value", "tax_amount", "tax_year",
                   "neighborhood", "zoning", "lot_size_sqft",
                   "garage_area_sqft")

# Fields a coverage probe reports on — what an appraiser actually reads.
# The first group are the physical characteristics the discrepancy check
# compares; a county is "check-ready" when those come through.
BUILDING_FIELDS = ("above_grade_sqft", "basement_sqft", "beds", "baths",
                   "year_built")
COVERAGE_FIELDS = BUILDING_FIELDS + (
    "owner", "legal", "parcel_number", "market_value", "assessed_value",
    "tax_amount")

# Input properties per county. Packaged cases intentionally use government or
# other institutional properties so the public regression suite does not
# redistribute private-home addresses. --capture pins only stable fields.
DEFAULT_CASES = [
    {"id": "elpaso-address", "county": "El Paso", "state": "co",
     "address": "1675 W Garden of the Gods Rd", "fixture_type": "institutional"},
    {"id": "denver-parcel", "county": "Denver", "state": "co",
     "parcel": "0503321005000", "address": "10 W 14th Ave Pkwy",
     "fixture_type": "institutional"},
    {"id": "douglas-parcel", "county": "Douglas", "state": "co",
     "parcel": "R0404064", "address": "100 Third St",
     "fixture_type": "institutional"},
    {"id": "denver-address", "county": "Denver", "state": "co",
     "address": "10 W 14th Ave Pkwy", "fixture_type": "institutional"},
    {"id": "jeffco-address", "county": "Jefferson", "state": "co",
     "address": "8101 Ralston Rd", "fixture_type": "institutional"},
    {"id": "arapahoe-address", "county": "Arapahoe", "state": "co",
     "address": "5334 S Prince St", "parcel": "2077-16-2-00-045",
     "fixture_type": "institutional"},
    {"id": "adams-parcel", "county": "Adams", "state": "co",
     "parcel": "0156931101001", "address": "4430 S Adams County Pkwy",
     "fixture_type": "institutional"},
    {"id": "clearcreek-address", "county": "Clear Creek", "state": "co",
     "address": "405 Argentine St", "fixture_type": "institutional"},
    {"id": "clearcreek-account", "county": "Clear Creek", "state": "co",
     "parcel": "R162040", "fixture_type": "institutional"},
    # tier-2 baseline (statewide parcel API, no dedicated client)
    {"id": "boulder-baseline", "county": "Boulder", "state": "co",
     "address": "1777 Broadway", "parcel": "146330357002", "tier2": True,
     "fixture_type": "institutional"},
]

# Discovery-ladder expectations (county -> platform the probe must resolve to).
DISCOVERY_CASES = [
    {"county": "Clear Creek", "state": "co", "platform": "eagleweb"},
    {"county": "El Paso", "state": "co", "platform": "spatialest"},
    {"county": "Boulder", "state": "co", "platform": "arcgis"},
    {"county": "Notacounty", "state": "co", "platform": None},
]


# --------------------------------------------------------------------------
# comparison
# --------------------------------------------------------------------------
def _norm(value):
    """Normalize a value for comparison (case/space/punct-insensitive)."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return round(float(value), 3)
    s = re.sub(r"[\s,]+", " ", str(value)).strip().upper()
    try:
        return round(float(s.replace("$", "").replace(" ", "")), 3)
    except ValueError:
        return s


def compare_record(expected, actual):
    """Compare a lookup record against a golden record.

    Returns a list of finding dicts: {field, level, expected, actual} where
    level is "fail" (stable-field drift) or "warn" (volatile drift).
    """
    findings = []
    for field in STABLE_FIELDS:
        if field not in expected:
            continue
        if _norm(expected[field]) != _norm(actual.get(field)):
            findings.append({"field": field, "level": "fail",
                             "expected": expected[field],
                             "actual": actual.get(field)})
    for field in VOLATILE_FIELDS:
        if field not in expected:
            continue
        if _norm(expected[field]) != _norm(actual.get(field)):
            findings.append({"field": field, "level": "warn",
                             "expected": expected[field],
                             "actual": actual.get(field)})
    return findings


# --------------------------------------------------------------------------
# runners
# --------------------------------------------------------------------------
# Transport-level statuses worth retrying. Definitive answers (not_found,
# ambiguous, value mismatches) are never retried — a golden FAIL must mean
# "the parser broke or the world changed", not "the endpoint hiccuped".
_TRANSIENT_STATUSES = {"timeout", "api_error"}


def run_lookup_case(case, timeout_note=None, attempts=3, retry_delay=2.0):
    """Run one golden case live. Returns {status, elapsed_s, record}."""
    import assessor_lookup
    rec, elapsed = {}, 0.0
    for attempt in range(attempts):
        t0 = time.monotonic()
        try:
            rec = assessor_lookup.lookup(
                address=case.get("address"), parcel=case.get("parcel"),
                county=case["county"], state=case.get("state", "co"))
        except Exception as e:  # noqa: BLE001 — harness must never crash mid-run
            rec = {"status": "harness_error", "error": f"{type(e).__name__}: {e}"}
        elapsed = time.monotonic() - t0
        if rec.get("status") not in _TRANSIENT_STATUSES:
            break
        if attempt < attempts - 1:
            time.sleep(retry_delay)
    return {"status": rec.get("status", "unknown"), "elapsed_s": round(elapsed, 2),
            "record": rec}


def platform_label(entry):
    """Benchmark/golden label for a registry entry: the true platform name,
    with the driver appended for ArcGIS so per-source latency stays visible."""
    platform = entry.get("platform", "?")
    driver = entry.get("config", {}).get("driver")
    return f"{platform}/{driver}" if driver else platform


def run_regression(cases, golden, delay=0.5, progress=print):
    """Run all cases against golden records. Returns list of result dicts."""
    results = []
    for case in cases:
        out = run_lookup_case(case)
        entry = {"id": case["id"], "county": case["county"],
                 "platform": golden.get(case["id"], {}).get("platform", "?"),
                 "status": out["status"], "elapsed_s": out["elapsed_s"]}
        gold = golden.get(case["id"])
        if out["status"] != "success":
            entry["result"] = "FAIL"
            entry["findings"] = [{"field": "status", "level": "fail",
                                  "expected": "success",
                                  "actual": out["status"]}]
            err = out["record"].get("error", "")
            if err:
                entry["error"] = str(err)[:200]
        elif not gold:
            entry["result"] = "NO-GOLDEN"
            entry["findings"] = []
        else:
            findings = compare_record(gold["expect"], out["record"])
            fails = [f for f in findings if f["level"] == "fail"]
            warns = [f for f in findings if f["level"] == "warn"]
            entry["result"] = "FAIL" if fails else ("WARN" if warns else "PASS")
            entry["findings"] = findings
            base = gold.get("elapsed_s")
            if base:
                entry["elapsed_baseline_s"] = base
        results.append(entry)
        progress(_fmt_case_line(entry))
        for f in entry["findings"]:
            progress(f"      {f['level'].upper():4s} {f['field']}: "
                     f"expected {f['expected']!r} got {f['actual']!r}")
        time.sleep(delay)
    return results


def run_discovery_checks(progress=print):
    """Verify the auto-discovery ladder still resolves correctly."""
    from assessor_lookup.discovery import discover_county
    results = []
    for dc in DISCOVERY_CASES:
        t0 = time.monotonic()
        try:
            entry = discover_county(dc["county"], dc["state"], timeout=10)
        except Exception as e:  # noqa: BLE001
            entry = {"platform": f"harness_error: {e}"}
        elapsed = round(time.monotonic() - t0, 2)
        got = entry.get("platform") if entry else None
        ok = got == dc["platform"]
        results.append({"county": dc["county"], "expected": dc["platform"],
                        "actual": got, "elapsed_s": elapsed,
                        "result": "PASS" if ok else "FAIL"})
        progress(f"  {dc['county']:14s} -> {str(got):14s} "
                 f"(want {str(dc['platform'])}) {elapsed:5.2f}s "
                 f"{'PASS' if ok else 'FAIL'}")
    return results


def run_parser_bench(iterations=200, progress=print):
    """Micro-benchmark the HTML/JSON parsers on canned fixtures (offline)."""
    from assessor_lookup.platforms.spatialest import SpatialestClient
    from assessor_lookup.platforms.eagleweb import EagleWebClient

    spat_card = {"parcel": {
        "header": {"marketvalueheader": "$350,000", "par": "R0000001",
                   "legalText": "LOT 1 BLK 1 TEST SUB",
                   "owners_list": '["SAMPLE OWNER"]'},
        "sections": {"Building": {
            "AboveGradeArea": "2250", "TotalBSMT": "924", "FinishedBSMT": "500",
            "YearBlt": "2020", "Beds": "4", "Baths": "2.5",
            "ResStyle": "2-Story", "zone": "R-1 6", "TaxAmount": "$1,298",
            "TaxYear": "2025", "SubdivisionName": "Test Subdivision"}}}}

    ew_summary = ('<td><strong>Parcel Number</strong> 0000-000-00-001</td>'
                  '<td><strong>Situs Address</strong> 123 MAIN ST</td>'
                  '<td><strong>Legal Summary</strong> Subdivision: TEST'
                  ' Block: 1 Lot: 1</td><td><b>Owner Name</b> SAMPLE OWNER</td>'
                  '<td align="left"><b>Actual</b> (2026)</td>'
                  '<td align="right">$658,180</td>'
                  '<td align="left"><b>School Assessed</b></td>'
                  '<td align="right">$46,400</td>'
                  '<td align="left"><b>Non-School Assessed</b></td>'
                  '<td align="right">$44,760</td>'
                  '<caption><b>Mill Levy School</b>:25.785 '
                  '<b>Mill Levy Non-School</b>:44.576</caption>')
    ew_detail = ('<span class="fieldLabel">Year Built</span><br/>'
                 '<span class="field"><span class="text" >1910&nbsp;</span></span>'
                 '<span class="fieldLabel">Bedrooms</span><br/>'
                 '<span class="field"><span class="text" >6&nbsp;</span></span>'
                 '<span class="fieldLabel">Baths</span><br/>'
                 '<span class="field"><span class="text" >3&nbsp;</span></span>'
                 '<h3>Abstract Code</h3><table><tr><th>Abstract Code</th></tr>'
                 '<tr><td><span class="text">SINGLE FAM.RES-IMPROVEMTS</span></td>'
                 '<td><span class="text" >100.0</span></td>'
                 '<td><span class="text" ></span></td>'
                 '<td><span class="text" >0</span></td>'
                 '<td><span class="text" >3006</span></td>'
                 '<td><span class="text" >0</span></td></tr></table>')

    spat = SpatialestClient()
    ew = EagleWebClient(base="https://example.test/eagleassessor")
    benches = [
        ("spatialest._parse_record_card",
         lambda: spat._parse_record_card(spat_card, "R0000001",
                                         "https://example.test/co/demo", "123 Example Ave")),
        ("eagleweb._parse_summary",
         lambda: ew._parse_summary(ew_summary, "R000001")),
        ("eagleweb._parse_detail", lambda: ew._parse_detail(ew_detail)),
    ]
    results = []
    for name, fn in benches:
        t0 = time.perf_counter()
        for _ in range(iterations):
            fn()
        per_call_ms = (time.perf_counter() - t0) / iterations * 1000
        results.append({"parser": name, "per_call_ms": round(per_call_ms, 3),
                        "iterations": iterations})
        progress(f"  {name:34s} {per_call_ms:8.3f} ms/call")
    return results


# --------------------------------------------------------------------------
# capture / persistence
# --------------------------------------------------------------------------
def capture_golden(cases, delay=0.5, progress=print):
    """Run all cases live and pin current records as the golden baseline."""
    from assessor_lookup.checker import _get_client
    golden = {}
    for case in cases:
        out = run_lookup_case(case)
        rec = out["record"]
        if out["status"] != "success":
            progress(f"  {case['id']:22s} SKIP ({out['status']}) "
                     f"{rec.get('error', '')[:80]}")
            continue
        _, entry = _get_client(case["county"], case.get("state", "co"))
        expect = {}
        # Committed/package goldens deliberately contain only parser-critical
        # physical fields. Owner, legal, values, and taxes are public but are
        # unnecessary personal/property data for deterministic regression.
        for field in STABLE_FIELDS:
            if field in rec:
                expect[field] = rec[field]
        golden[case["id"]] = {
            "platform": platform_label(entry),
            "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "elapsed_s": out["elapsed_s"],
            "expect": expect,
        }
        progress(f"  {case['id']:22s} captured ({out['elapsed_s']}s, "
                 f"{entry.get('platform')})")
        time.sleep(delay)
    return golden


def _user_dir():
    """Per-user config dir (same one the registry cache uses)."""
    from assessor_lookup.registry import _user_registry_path
    path = _user_registry_path().parent
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def _private_user_file(name):
    """Return a user-data path and remediate permissions on existing files."""
    path = _user_dir() / name
    if path.exists():
        path.chmod(0o600)
    return path


def _write_private_json(path, data, *, sort_keys=False):
    """Write JSON through a descriptor that is always owner-readable only."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        else:  # pragma: no cover - Windows has no descriptor chmod
            Path(path).chmod(0o600)
        with os.fdopen(fd, "w") as f:
            fd = None
            json.dump(data, f, indent=2, sort_keys=sort_keys)
    finally:
        if fd is not None:
            os.close(fd)


def _load_json(path, default):
    try:
        if Path(path).exists():
            with open(path) as f:
                return json.load(f)
    except (ValueError, OSError):
        pass
    return default


def load_golden(path=GOLDEN_PATH):
    """Repo golden records merged with the user's onboarded-county goldens."""
    golden = _load_json(path, {})
    golden.update(_load_json(_private_user_file("user_golden.json"), {}))
    return golden


def load_user_cases():
    """Cases the user onboarded for their locale (persisted, re-run each time)."""
    return _load_json(_private_user_file("user_cases.json"), [])


def load_cases():
    """All regression cases: packaged defaults + the user's onboarded counties."""
    seen = {c["id"] for c in DEFAULT_CASES}
    extra = [c for c in load_user_cases() if c.get("id") not in seen]
    return list(DEFAULT_CASES) + extra


def _save_user_case(case, golden_entry):
    """Persist an onboarded county so `harness.py` re-checks it every run."""
    d = _user_dir()
    cases = load_user_cases()
    cases = [c for c in cases if c.get("id") != case["id"]] + [case]
    cases_path = _private_user_file("user_cases.json")
    _write_private_json(cases_path, cases)
    golden_path = _private_user_file("user_golden.json")
    golden = _load_json(golden_path, {})
    golden[case["id"]] = golden_entry
    _write_private_json(golden_path, golden, sort_keys=True)


def probe_coverage(county, state="co", address="", parcel=""):
    """Ping a county live and report which fields it actually returns.

    This is the "point at my county and see how it reacts" check: it resolves
    the platform (auto-discovering + caching an unknown county), does one live
    lookup, and reports field coverage, latency, and whether the county is
    ready for the MLS discrepancy check (building fields present).
    """
    from assessor_lookup.checker import _get_client
    result = {"county": county, "state": state}
    try:
        _, entry = _get_client(county, state)
        result["platform"] = platform_label(entry)
        result["tier"] = entry.get("tier")
    except Exception as e:  # noqa: BLE001
        result["platform"] = None
        result["error"] = f"{type(e).__name__}: {e}"

    if not (address or parcel):
        result["status"] = "resolved_only"
        result["note"] = ("Platform resolved. Provide a sample address or "
                          "parcel from your MLS to measure field coverage.")
        return result

    out = run_lookup_case({"county": county, "state": state,
                          "address": address, "parcel": parcel})
    result["status"] = out["status"]
    result["elapsed_s"] = out["elapsed_s"]
    result["query"] = address or parcel
    rec = out["record"]
    if out["status"] != "success":
        result["error"] = str(rec.get("error", ""))[:200]
        return result

    present = [f for f in COVERAGE_FIELDS if rec.get(f) not in (None, "")]
    result["present"] = present
    result["missing"] = [f for f in COVERAGE_FIELDS if f not in present]
    result["coverage"] = f"{len(present)}/{len(COVERAGE_FIELDS)}"
    bld = [f for f in BUILDING_FIELDS if rec.get(f) not in (None, "")]
    result["building_coverage"] = f"{len(bld)}/{len(BUILDING_FIELDS)}"
    result["check_ready"] = len(bld) >= 3  # GLA + a couple more = useful check
    result["sample"] = {f: rec.get(f) for f in COVERAGE_FIELDS}
    result["assessor_url"] = rec.get("assessor_url", "")
    return result


def onboard_county(county, state="co", address="", parcel="", capture=True):
    """Configure a county for repeated use: resolve, probe, and pin a golden.

    Point the tool at a county in your locale and it (1) auto-discovers and
    caches the assessor source, (2) probes field coverage against your sample
    property, and (3) if capture=True and the lookup succeeded, saves a golden
    record + a regression case to your user config so every future
    `harness.py` run re-checks that county.
    """
    probe = probe_coverage(county, state, address=address, parcel=parcel)
    probe["onboarded"] = False
    if probe.get("status") != "success":
        probe["note"] = ("Not onboarded — the sample lookup didn't succeed. "
                         "Try a different sample property or check the county "
                         "name/state.")
        return probe
    if not capture:
        probe["note"] = "Coverage probed; golden not captured (capture=False)."
        return probe

    slug = re.sub(r"[^a-z0-9]+", "", county.lower())
    key = "address" if address else "parcel"
    case = {"id": f"{slug}-{key}", "county": county, "state": state,
            "address": address, "parcel": parcel}
    capture_result = run_lookup_case(case)
    rec = capture_result["record"]
    if capture_result["status"] != "success":
        probe["status"] = capture_result["status"]
        probe["error"] = str(rec.get("error", ""))[:200]
        probe["note"] = ("Not onboarded — golden capture lookup failed; "
                         "no case was persisted.")
        return probe
    expect = {f: rec[f] for f in STABLE_FIELDS if f in rec}
    golden_entry = {
        "platform": probe.get("platform", "?"),
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "elapsed_s": probe.get("elapsed_s"),
        "expect": expect,
        "source": "user-onboarded",
    }
    _save_user_case(case, golden_entry)
    probe["onboarded"] = True
    probe["case_id"] = case["id"]
    probe["note"] = (f"Onboarded as '{case['id']}'. It's now cached for lookups "
                    f"and re-checked on every `harness.py` run.")
    return probe


def _fmt_case_line(e):
    base = e.get("elapsed_baseline_s")
    delta = f" (base {base}s)" if base else ""
    return (f"  {e['id']:22s} {e['county']:12s} {e['platform']:14s} "
            f"{e['status']:12s} {e['elapsed_s']:6.2f}s{delta}  {e['result']}")


def _print_coverage(rep):
    """Human-readable coverage/onboard report."""
    print(f"\n=== {rep['county']} County, {rep['state'].upper()} ===")
    print(f"  platform : {rep.get('platform')}"
          + (f" (tier {rep['tier']})" if rep.get("tier") else ""))
    if rep.get("status") == "resolved_only":
        print(f"  {rep['note']}")
        return
    print(f"  query    : {rep.get('query')}  ->  {rep.get('status')}"
          f"  ({rep.get('elapsed_s')}s)")
    if rep.get("status") != "success":
        print(f"  error    : {rep.get('error')}")
        return
    print(f"  coverage : {rep.get('coverage')} fields  |  "
          f"building {rep.get('building_coverage')}  |  "
          f"check-ready: {'YES' if rep.get('check_ready') else 'NO'}")
    sample = rep.get("sample", {})
    for f in COVERAGE_FIELDS:
        v = sample.get(f)
        mark = "x" if v not in (None, "") else " "
        print(f"    [{mark}] {f:20s} {v if v not in (None, '') else '(missing)'}")
    if rep.get("assessor_url"):
        print(f"  url      : {rep['assessor_url']}")
    if rep.get("note"):
        print(f"  {rep['note']}")


def platform_aggregates(results):
    """Aggregate latency by platform."""
    by = {}
    for r in results:
        if r["status"] == "success":
            by.setdefault(r["platform"], []).append(r["elapsed_s"])
    out = []
    for platform, times in sorted(by.items()):
        out.append({"platform": platform, "n": len(times),
                    "mean_s": round(statistics.mean(times), 2),
                    "max_s": round(max(times), 2)})
    return out


def save_history(payload):
    HISTORY_DIR.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = HISTORY_DIR / f"bench-{ts}.json"
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)
    with open(HISTORY_DIR / "latest.json", "w") as f:
        json.dump(payload, f, indent=2)
    return path


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--capture", action="store_true",
                    help="(Re)capture golden records from live county sites")
    ap.add_argument("--filter", default="",
                    help="Only run cases whose id contains this substring")
    ap.add_argument("--delay", type=float, default=0.5,
                    help="Seconds between live cases (politeness)")
    ap.add_argument("--json", dest="json_out", default="",
                    help="Also write results JSON to this path")
    ap.add_argument("--offline", action="store_true",
                    help="Skip network sections; parser micro-bench only")
    ap.add_argument("--no-discovery", action="store_true",
                    help="Skip the discovery-ladder checks")
    ap.add_argument("--probe", metavar="COUNTY",
                    help="Ping a county live and report field coverage")
    ap.add_argument("--onboard", metavar="COUNTY",
                    help="Configure a county for repeated use (discover, "
                         "probe, and pin a golden record)")
    ap.add_argument("--state", default="co",
                    help="State for --probe/--onboard (default: co)")
    ap.add_argument("--address", default="",
                    help="Sample address for --probe/--onboard")
    ap.add_argument("--parcel", default="",
                    help="Sample parcel/schedule number for --probe/--onboard")
    ap.add_argument("--no-capture", action="store_true",
                    help="With --onboard: probe only, don't pin a golden")
    args = ap.parse_args(argv)

    if args.probe:
        rep = probe_coverage(args.probe, args.state, args.address, args.parcel)
        _print_coverage(rep)
        return 0 if rep.get("status") in ("success", "resolved_only") else 1

    if args.onboard:
        rep = onboard_county(args.onboard, args.state, args.address,
                             args.parcel, capture=not args.no_capture)
        _print_coverage(rep)
        return 0 if rep.get("onboarded") or rep.get("status") == "success" else 1

    if args.capture:
        cap_cases = [c for c in DEFAULT_CASES
                     if args.filter.lower() in c["id"].lower()]
        print(f"Capturing golden records for {len(cap_cases)} case(s)...")
        golden = _load_json(GOLDEN_PATH, {})
        golden.update(capture_golden(cap_cases, delay=args.delay))
        with open(GOLDEN_PATH, "w") as f:
            json.dump(golden, f, indent=2, sort_keys=True)
        print(f"\nWrote {GOLDEN_PATH} ({len(golden)} golden records)")
        return 0

    cases = [c for c in load_cases()
             if args.filter.lower() in c["id"].lower()]
    if not cases and not args.offline:
        print(f"No cases match filter {args.filter!r}")
        return 1

    payload = {"ran_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "sections": {}}
    hard_fail = warned = False

    if not args.offline:
        golden = load_golden()
        if not golden:
            print("No golden records yet — run with --capture first.")
            return 1
        print(f"=== Regression: {len(cases)} golden case(s) ===")
        results = run_regression(cases, golden, delay=args.delay)
        payload["sections"]["regression"] = results
        hard_fail |= any(r["result"] == "FAIL" for r in results)
        warned |= any(r["result"] in ("WARN", "NO-GOLDEN") for r in results)

        aggs = platform_aggregates(results)
        print("\n=== Benchmark: latency by platform ===")
        for a in aggs:
            print(f"  {a['platform']:16s} n={a['n']}  "
                  f"mean {a['mean_s']:6.2f}s  max {a['max_s']:6.2f}s")
        payload["sections"]["latency_by_platform"] = aggs

        if not args.no_discovery:
            print("\n=== Discovery ladder ===")
            disc = run_discovery_checks()
            payload["sections"]["discovery"] = disc
            hard_fail |= any(d["result"] == "FAIL" for d in disc)

    print("\n=== Parser micro-benchmark (offline) ===")
    payload["sections"]["parser_bench"] = run_parser_bench()

    hist = save_history(payload)
    print(f"\nResults saved: {hist}")
    if args.json_out:
        with open(args.json_out, "w") as f:
            json.dump(payload, f, indent=2)

    if hard_fail:
        print("RESULT: FAIL — hard regression(s) above. If the county data "
              "really changed (sale/remodel), re-capture with --capture.")
        return 1
    if warned:
        print("RESULT: PASS with warnings (volatile-field drift).")
        return 2
    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
