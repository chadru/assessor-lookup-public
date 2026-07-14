"""Command-line interface for assessor-lookup.

    assessor-lookup lookup "123 Main St" --county "El Paso"
    assessor-lookup lookup --parcel 5227004002 --county Adams
    assessor-lookup counties
    assessor-lookup check subject.csv comps.csv --county "El Paso"
    assessor-lookup card "https://property.spatialest.com/co/elpaso/#/property/..." card.pdf
"""

import argparse
import csv
import json
import sys


def _cmd_lookup(args):
    from . import lookup
    result = lookup(address=args.address, parcel=args.parcel,
                    county=args.county, state=args.state,
                    verbose=args.verbose)
    status = result.get("status")
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        if status != "success":
            print(f"[{status}] {result.get('error', '')}".strip())
            return 1
        order = ("address", "owner", "parcel_number", "legal",
                 "above_grade_sqft", "basement_sqft",
                 "finished_basement_sqft", "beds", "baths", "year_built",
                 "style", "lot_size_sqft", "garage_area_sqft",
                 "tax_amount", "tax_year", "assessed_value", "market_value",
                 "neighborhood", "zoning", "latitude", "longitude",
                 "assessor_url")
        for key in order:
            val = result.get(key)
            if val not in ("", None):
                print(f"{key:24s} {val}")
    return 0 if status == "success" else 1


def _cmd_counties(args):
    from .registry import load_registry as _load_registry
    registry = _load_registry()
    print(f"{'Jurisdiction':<34} {'Platform'}")
    for key, entry in sorted(registry.items()):
        print(f"{key:<34} {entry.get('platform', 'spatialest')}")
    print("\nCounties not listed fall back to Spatialest with the county "
          "name as slug — many Spatialest counties work out of the box.")
    return 0


def _read_rows(path):
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def _cmd_check(args):
    from .checker import check_public_records, print_discrepancy_report
    subject_rows = _read_rows(args.subject_csv)
    subject = subject_rows[0] if subject_rows else {}
    comps = _read_rows(args.comps_csv) if args.comps_csv else []
    results = check_public_records(
        subject, comps, county=args.county, state=args.state,
        verbose=args.verbose,
        progress=None if args.json else print)
    if args.json:
        print(json.dumps(results, indent=2, default=str))
    else:
        print_discrepancy_report(results)
    return 0


def _cmd_discover(args):
    from .discovery import discover_county
    from .registry import save_discovered_entry
    entry = discover_county(args.county, args.state, verbose=True)
    if not entry:
        print(f"No supported assessor source found for {args.county}, "
              f"{args.state.upper()}.")
        print("You can add one by hand in the county registry, or open an issue.")
        return 1
    tier = entry.get("tier")
    print(f"\nFound: {args.county} -> {entry['platform']} (tier {tier})")
    if tier == 2:
        print("  Note: baseline parcel API — owner/legal/value only, "
              "no building data (GLA/beds/baths/year).")
    if not args.no_save:
        key = save_discovered_entry(args.county, entry, args.state)
        print(f"  Saved as '{key}' — future lookups use it automatically.")
    return 0


def _cmd_card(args):
    from .card import render_assessor_card_pdf
    out = render_assessor_card_pdf(args.url, args.out, timeout_s=args.timeout)
    print(f"Saved {out}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="assessor-lookup",
        description="Automated county assessor public-records search "
                    "for real-estate appraisers.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("lookup", help="Look up one property")
    p.add_argument("address", nargs="?", help="Street address")
    p.add_argument("--parcel", help="Parcel / schedule number")
    p.add_argument("--county", default="El Paso")
    p.add_argument("--state", default="co")
    p.add_argument("--json", action="store_true", help="JSON output")
    p.add_argument("--verbose", action="store_true")
    p.set_defaults(func=_cmd_lookup)

    p = sub.add_parser("counties", help="List supported counties")
    p.set_defaults(func=_cmd_counties)

    p = sub.add_parser(
        "discover", help="Auto-detect and cache an assessor source for a county")
    p.add_argument("county", help="County name, e.g. \"Clear Creek\"")
    p.add_argument("--state", default="co")
    p.add_argument("--no-save", action="store_true",
                   help="Probe only; do not cache the result")
    p.set_defaults(func=_cmd_discover)

    p = sub.add_parser(
        "check", help="Diff MLS CSV data against public records")
    p.add_argument("subject_csv", help="CSV with the subject row")
    p.add_argument("comps_csv", nargs="?", help="CSV with comp rows")
    p.add_argument("--county", default=None,
                   help="County (auto-detected from CSV when omitted)")
    p.add_argument("--state", default="co")
    p.add_argument("--json", action="store_true")
    p.add_argument("--verbose", action="store_true")
    p.set_defaults(func=_cmd_check)

    p = sub.add_parser("card", help="Print an assessor page to PDF "
                                    "(needs the [card] extra)")
    p.add_argument("url", help="Assessor property page URL")
    p.add_argument("out", help="Output PDF path")
    p.add_argument("--timeout", type=int, default=60)
    p.set_defaults(func=_cmd_card)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
