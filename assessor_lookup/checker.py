"""Public records checker — compare MLS data against county assessor records."""

import logging
import sys
import time

from .assessor import SpatialestClient, _load_registry

logger = logging.getLogger(__name__)


# Discrepancy thresholds
GLA_THRESHOLD = 50       # sqft difference to flag
BASEMENT_THRESHOLD = 50  # sqft difference to flag


def detect_county(subject_data, comps_data=None):
    """Auto-detect county from CSV 'County' column.

    Returns (county_name, state_code) or (None, None) if not found.
    """
    if subject_data:
        county = (subject_data.get("County")
                  or subject_data.get("CountyOrParish") or "").strip()
        state = (subject_data.get("State")
                 or subject_data.get("StateOrProvince") or "CO").strip().lower()
        if county:
            return county, state

    for comp in (comps_data or []):
        county = (comp.get("County") or comp.get("CountyOrParish") or "").strip()
        if county:
            state = (comp.get("State")
                     or comp.get("StateOrProvince") or "CO").strip().lower()
            return county, state

    return None, None


def _get_client(county, state="co", verbose=False):
    """Return the appropriate assessor client for the given county.

    Returns (client_instance, registry_entry).
    """
    registry = _load_registry()
    key = f"{state.upper()}:{county}"
    entry = registry.get(key)

    if not entry:
        # Try partial match (e.g. "El Paso" in key)
        for k, v in registry.items():
            if county.lower() in k.lower():
                entry = v
                break

    if not entry:
        # Unknown county: try to auto-discover a source (API-first), and cache
        # the hit so subsequent lookups skip the probe.
        from .discovery import discover_county
        from .assessor import save_discovered_entry
        entry = discover_county(county, state, verbose=verbose)
        if entry:
            logger.info("Discovered county %s -> %s (tier %s)",
                        county, entry.get("platform"), entry.get("tier"))
            save_discovered_entry(county, entry, state)

    if not entry:
        # Last resort: assume Spatialest with the county name as slug.
        slug = county.lower().replace(" ", "")
        return SpatialestClient(verbose=verbose), {"platform": "spatialest", "slug": slug, "state": state}

    platform = entry.get("platform", "spatialest")

    if platform == "jeffco":
        from .assessor_jeffco import JeffcoClient
        return JeffcoClient(timeout=30, verbose=verbose), entry
    elif platform == "arapahoe":
        from .assessor_arapahoe import ArapahoeClient
        return ArapahoeClient(timeout=15, verbose=verbose), entry
    elif platform == "adams":
        from .assessor_adams import AdamsClient
        return AdamsClient(timeout=15, verbose=verbose), entry
    elif platform == "eagleweb":
        from .assessor_eagleweb import EagleWebClient
        return EagleWebClient(base=entry.get("base"), timeout=30,
                              verbose=verbose), entry
    elif platform == "co_parcel_api":
        from .assessor_coparcel import CoParcelClient
        return CoParcelClient(county=entry.get("county", county), state=state,
                              timeout=15, verbose=verbose), entry
    else:
        return SpatialestClient(timeout=10, verbose=verbose), entry


def _safe_float(val):
    """Safely convert a value to float. Returns None on failure."""
    if val is None:
        return None
    try:
        return float(str(val).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def _safe_int(val):
    """Safely convert a value to int. Returns None on failure."""
    if val is None:
        return None
    try:
        return int(float(str(val).replace(",", "").strip()))
    except (ValueError, TypeError):
        return None


def _compare_field(mls_val, assessor_val, threshold=None):
    """Compare two values and return a comparison dict.

    Args:
        mls_val: MLS value (numeric).
        assessor_val: Assessor value (numeric).
        threshold: If set, flag only if abs(diff) > threshold.
                   If None, flag on any mismatch.

    Returns:
        Dict with keys: mls, assessor, diff, flag.
    """
    result = {"mls": mls_val, "assessor": assessor_val, "diff": None, "flag": False}

    if mls_val is None or assessor_val is None:
        return result

    if threshold is not None:
        diff = assessor_val - mls_val
        result["diff"] = diff
        result["flag"] = abs(diff) > threshold
    else:
        result["flag"] = mls_val != assessor_val
        if mls_val != assessor_val:
            result["diff"] = assessor_val - mls_val

    return result


def _extract_mls_fields(row, is_subject=True):
    """Extract comparable fields from an MLS CSV row dict.

    Args:
        row: Dict from CSVParser (subject or comp row).
        is_subject: True for subject property.

    Returns:
        Dict with: address, gla, beds, baths, year_built, basement_sqft.
    """
    def first(*names, default=""):
        for name in names:
            value = row.get(name)
            if value not in (None, ""):
                return value
        return default

    address = str(first("Address", "UnparsedAddress", "StreetAddress",
                        "FullAddress")).strip()
    parcel_id = (
        first("Schedule Number", "Parcel Number", "Parcel", "Parcel ID",
              "APN", "ParcelNumber")
    )
    unit_number = first("Unit Number", "Unit", "UnitNumber")

    # GLA = Main SqFt + Upper SqFt
    main_sf = _safe_float(row.get("Main SqFt")) or 0
    upper_sf = _safe_float(row.get("Upper SqFt")) or 0
    if main_sf or upper_sf:
        gla = int(main_sf + upper_sf)
    else:
        gla_value = _safe_float(first(
            "LivingArea", "AboveGradeFinishedArea", "BuildingAreaTotal",
            default=None))
        gla = int(gla_value) if gla_value is not None else None

    # Beds (above grade) = Bedrooms Total - Basement Beds
    total_beds = _safe_int(first("Bedrooms Total", "BedroomsTotal", default=None))
    bsmt_beds = _safe_int(first("Basement Beds", "BedroomsBelowGrade",
                                default=None)) or 0
    beds = (total_beds - bsmt_beds) if total_beds is not None else None

    # Baths (above grade) = Total Baths - Basement Baths
    total_baths = _safe_float(first(
        "Total Baths", "BathroomsTotalDecimal", "BathroomsTotalInteger",
        "BathroomsTotal", default=None))
    bsmt_baths = _safe_float(first("Basement Baths", "BathroomsBelowGrade",
                                   default=None)) or 0
    baths = round(total_baths - bsmt_baths, 1) if total_baths is not None else None

    year_built = _safe_int(first("Year Built", "YearBuilt", default=None))
    basement_sqft = _safe_int(first(
        "Basement SqFt", "BasementAreaTotal", default=None))
    if basement_sqft is None:
        finished_below = _safe_int(first("BelowGradeFinishedArea", default=None))
        unfinished_below = _safe_int(first(
            "BelowGradeUnfinishedArea", default=None))
        if finished_below is not None or unfinished_below is not None:
            basement_sqft = (finished_below or 0) + (unfinished_below or 0)

    return {
        "address": address,
        "parcel_id": str(parcel_id).strip(),
        "unit_number": str(unit_number).strip(),
        "gla": gla,
        "beds": beds,
        "baths": baths,
        "year_built": year_built,
        "basement_sqft": basement_sqft,
    }


def _normalize_address(address):
    """Normalize street suffix abbreviations for assessor lookup.

    PPMLS uses non-standard abbreviations (CR for Circle, etc.) that
    assessor APIs may not recognize.
    """
    import re
    addr = (address or "").strip()
    if not addr:
        return addr
    # Map non-standard PPMLS suffixes to standard USPS abbreviations
    suffix_map = {
        r'\bCR\b': 'Cir', r'\bDR\b': 'Dr', r'\bRD\b': 'Rd',
        r'\bST\b': 'St', r'\bAV\b': 'Ave', r'\bCT\b': 'Ct',
        r'\bLN\b': 'Ln', r'\bPL\b': 'Pl', r'\bBL\b': 'Blvd',
        r'\bPK\b': 'Pkwy', r'\bTR\b': 'Trl', r'\bWY\b': 'Way',
    }
    for pattern, replacement in suffix_map.items():
        addr = re.sub(pattern, replacement, addr, flags=re.IGNORECASE)
    return addr


def _append_unit_to_address(address, unit_number):
    """Append a unit number for assessor searches when the feed splits it."""
    unit_number = str(unit_number or "").strip()
    if not address or not unit_number:
        return address
    if unit_number.lower() in address.lower().split():
        return address
    return f"{address} {unit_number}"


def check_public_records(subject_data, comps_data, county=None,
                         county_slug="elpaso", state="co", verbose=False,
                         existing_results=None, skip_subject=False,
                         time_budget=None, progress=None):
    """Check MLS data against county assessor public records.

    Args:
        subject_data: Subject row dict from CSVParser.
        comps_data: List of comp row dicts from CSVParser.
        county: County name (e.g. "Jefferson"). Auto-detected from CSV if None.
        county_slug: Spatialest county slug (legacy fallback).
        state: State code.
        verbose: Print verbose output.
        existing_results: Optional existing PR results to reuse.
        skip_subject: When True, reuse an existing Subject entry instead of
            querying the assessor for the subject again.
        time_budget: Optional wall-clock seconds. When set, once the cumulative
            lookup time exceeds it the remaining properties are returned as
            "timeout" instead of being queried. This keeps a web request under
            the gunicorn worker timeout when the assessor API is slow (each
            property can take tens of seconds across several requests), so the
            endpoint returns partial results rather than the worker being
            killed mid-check. None (the default, used by the CLI) = no limit.

    Returns:
        List of result dicts, one per property.
    """
    emit = progress or (lambda *_: None)

    # Auto-detect county from CSV data if not provided
    if not county:
        county, detected_state = detect_county(subject_data, comps_data)
        if detected_state:
            state = detected_state

    # Dispatch to the right client
    if county:
        client, entry = _get_client(county, state, verbose=verbose)
        logger.info("Public records: county=%s platform=%s",
                     county, entry.get("platform", "spatialest"))
    else:
        client = SpatialestClient(verbose=verbose)
        entry = {"platform": "spatialest", "slug": county_slug, "state": state}

    results = []
    existing_by_label = {
        r.get("label"): r for r in (existing_results or []) if isinstance(r, dict)
    }

    # Build property list
    properties = []
    if subject_data and not skip_subject:
        mls = _extract_mls_fields(subject_data, is_subject=True)
        properties.append(("Subject", mls))
    elif skip_subject and existing_by_label.get("Subject"):
        results.append(existing_by_label["Subject"])

    if comps_data:
        for i, comp in enumerate(comps_data):
            mls = _extract_mls_fields(comp, is_subject=False)
            properties.append((f"Comp {i + 1}", mls))

    if not properties:
        emit("No properties to check")
        return results

    emit(
        f"Checking public records for {len(results) + len(properties)} properties..."
    )

    start = time.monotonic()
    budget_hit = False
    for label, mls in properties:
        address = _normalize_address(mls["address"])

        # Stop querying once the time budget is spent; return the rest as
        # timeouts so the caller still gets partial results promptly.
        if time_budget is not None and (
                budget_hit or time.monotonic() - start > time_budget):
            budget_hit = True
            emit(f"  Skipped (time budget {time_budget}s exceeded): {address}")
            results.append({
                "label": label,
                "address": address,
                "status": "timeout",
                "error": f"public-records time budget ({time_budget}s) exceeded",
                "has_any_discrepancy": False,
                "fields": {},
            })
            continue

        lookup_address = _append_unit_to_address(
            address, mls.get("unit_number", ""))
        parcel_id = mls.get("parcel_id", "")
        if not address and not parcel_id:
            results.append({
                "label": label,
                "address": "",
                "status": "invalid_address",
                "has_any_discrepancy": False,
                "fields": {},
            })
            continue

        # Look up on assessor
        assessor = None
        if entry.get("platform") == "spatialest":
            if parcel_id:
                assessor = client.lookup_by_parcel(
                    parcel_id,
                    county_slug=entry.get("slug", county_slug),
                    state=state,
                )
            if address and (not assessor or assessor.get("status") != "success"):
                assessor = client.lookup(
                    lookup_address,
                    county_slug=entry.get("slug", county_slug),
                    state=state,
                )
        else:
            if parcel_id and hasattr(client, "lookup_by_parcel"):
                assessor = client.lookup_by_parcel(parcel_id)
            if address and (not assessor or assessor.get("status") != "success"):
                assessor = client.lookup(lookup_address)

        if not assessor:
            assessor = {
                "status": "invalid_address",
                "error": "No address or supported parcel lookup",
            }

        if assessor["status"] != "success":
            status = assessor["status"]
            error_msg = assessor.get("error", "")
            if status == "not_found":
                emit(f"  Not found: {address}")
            elif status == "timeout":
                emit(f"  Timeout: {address}")
            elif status == "api_error":
                emit(f"  Warning: Could not reach assessor for {address}")
                if verbose and error_msg:
                    emit(f"    Error: {error_msg}")
            else:
                emit(f"  Error ({status}): {address}")

            results.append({
                "label": label,
                "address": address,
                "status": status,
                "error": error_msg,
                "has_any_discrepancy": False,
                "fields": {},
            })
            continue

        # Compare fields
        fields = {
            "gla": _compare_field(
                mls["gla"], assessor.get("above_grade_sqft"),
                threshold=GLA_THRESHOLD
            ),
            "beds": _compare_field(
                mls["beds"], assessor.get("beds")
            ),
            "baths": _compare_field(
                mls["baths"], assessor.get("baths")
            ),
            "year_built": _compare_field(
                mls["year_built"], assessor.get("year_built")
            ),
            "basement_sqft": _compare_field(
                mls["basement_sqft"], assessor.get("basement_sqft"),
                threshold=BASEMENT_THRESHOLD
            ),
        }

        has_discrepancy = any(f["flag"] for f in fields.values())

        # Pass through public records data (owner, legal, taxes, etc.).
        # Physical characteristics (sqft/beds/baths/year/style) ride along so
        # the Spark views can backfill a subject that has no MLS listing.
        public_data = {}
        for key in ("owner", "legal", "parcel_number", "tax_amount",
                    "tax_year", "assessed_value", "neighborhood",
                    "zoning",
                    "market_value", "map_ref",
                    "latitude", "longitude",
                    "above_grade_sqft", "basement_sqft",
                    "finished_basement_sqft", "year_built",
                    "beds", "baths", "style",
                    "lot_size_sqft", "garage_area_sqft"):
            val = assessor.get(key, "")
            if val not in ("", None):
                public_data[key] = str(val)

        results.append({
            "label": label,
            "address": address or assessor.get("address", ""),
            "assessor_url": assessor.get("assessor_url", ""),
            "status": "success",
            "has_any_discrepancy": has_discrepancy,
            "fields": fields,
            "public_data": public_data,
        })

    return results


def print_discrepancy_report(results):
    """Print a formatted discrepancy report.

    Args:
        results: List of result dicts from check_public_records().
    """
    print("\n=== Public Records Check ===")

    for r in results:
        label = r["label"]
        address = r["address"]
        status = r["status"]

        if status != "success":
            print(f"\n{label}: {address}  [{status}]")
            continue

        discrepancy_marker = "  **" if r["has_any_discrepancy"] else ""
        print(f"\n{label}: {address}{discrepancy_marker}")

        field_labels = {
            "gla": "GLA",
            "beds": "Beds",
            "baths": "Baths",
            "year_built": "Year",
            "basement_sqft": "Bsmt",
        }

        for field_key, display in field_labels.items():
            f = r["fields"][field_key]
            mls_val = f["mls"]
            asr_val = f["assessor"]

            if mls_val is None and asr_val is None:
                continue

            mls_str = str(mls_val) if mls_val is not None else "N/A"
            asr_str = str(asr_val) if asr_val is not None else "N/A"

            if f["flag"]:
                diff = f["diff"]
                diff_str = f"+{diff}" if diff and diff > 0 else str(diff)
                status_str = f"DIFF {diff_str}"
            else:
                status_str = "OK"

            print(f"  {display + ':':<10} MLS {mls_str:<8} | Assessor {asr_str:<8} | {status_str}")

        if r.get("assessor_url"):
            print(f"  URL: {r['assessor_url']}")

    # Summary
    total = len(results)
    success = sum(1 for r in results if r["status"] == "success")
    flagged = sum(1 for r in results if r["has_any_discrepancy"])
    print(f"\nPublic records check complete. {success}/{total} properties checked.")
    if flagged:
        print(f"{flagged} propert{'y' if flagged == 1 else 'ies'} with discrepancies.")
    else:
        print("All properties match public records.")


def apply_overrides(choices, subject_data, comps_data, public_records=None):
    """Write chosen values back into the CSV row dicts.

    Modifies subject_data and comps_data in place so that
    populate_report() uses the assessor-corrected values.

    Args:
        choices: Dict from resolve_discrepancies: {(label, field_key): value}.
        subject_data: Subject row dict (modified in place).
        comps_data: List of comp row dicts (modified in place).
        public_records: Optional list of public records results (from
            check_public_records) used to match labels to comps by address.
    """
    # Map field_key → CSV column(s) to update
    FIELD_MAP = {
        "gla": ("Main SqFt", "Upper SqFt"),  # special: split across two cols
        "beds": ("Bedrooms Total", "Basement Beds"),  # special: above-grade
        "baths": ("Total Baths", "Total Basement Bath"),  # special: above-grade
        "year_built": ("Year Built",),
        "basement_sqft": ("Basement SqFt",),
    }

    # Build address→comp_index map for safe lookup
    _addr_to_idx = {}
    for i, comp in enumerate(comps_data or []):
        addr = (comp.get("Address") or "").strip().upper()
        if addr:
            _addr_to_idx[addr] = i

    # Build label→address map from public_records (if available)
    _label_addr = {}
    if public_records:
        for r in public_records:
            lbl = r.get("label", "")
            addr = (r.get("address") or "").strip().upper()
            if lbl and addr:
                _label_addr[lbl] = addr

    applied = 0
    for (label, field_key), value in choices.items():
        if value is None:
            continue

        # Find the right row
        if label == "Subject":
            row = subject_data
        else:
            row = None
            # Prefer address-based lookup (safe against reordering)
            pr_addr = _label_addr.get(label, "")
            if pr_addr and pr_addr in _addr_to_idx:
                row = comps_data[_addr_to_idx[pr_addr]]
            else:
                # Fall back to positional: "Comp 1" → index 0
                try:
                    idx = int(label.split()[-1]) - 1
                except (ValueError, IndexError):
                    continue
                if idx < len(comps_data):
                    row = comps_data[idx]
                else:
                    continue

        if row is None:
            continue

        if field_key == "gla":
            # Set Main SqFt to the assessor total, Upper SqFt to 0
            # (populate_report computes GLA = Main + Upper)
            row["Main SqFt"] = str(int(value))
            row["Upper SqFt"] = "0"
            applied += 1
        elif field_key == "beds":
            # Assessor reports total beds; MLS splits above/below grade
            # Set Bedrooms Total = assessor value, keep Basement Beds as-is
            bsmt_beds = _safe_int(row.get("Basement Beds")) or 0
            row["Bedrooms Total"] = str(int(value) + bsmt_beds)
            applied += 1
        elif field_key == "baths":
            # Assessor reports total baths; MLS splits above/below grade
            bsmt_baths = _safe_float(row.get("Total Basement Bath")) or 0
            row["Total Baths"] = str(round(float(value) + bsmt_baths, 1))
            applied += 1
        elif field_key == "year_built":
            row["Year Built"] = str(int(value))
            applied += 1
        elif field_key == "basement_sqft":
            row["Basement SqFt"] = str(int(value))
            applied += 1

    if applied:
        print(f"Applied {applied} assessor override(s) to CSV data.")

    return applied


def resolve_discrepancies(results, auto_mode=None):
    """Interactively resolve discrepancies or auto-resolve.

    Args:
        results: List of result dicts from check_public_records().
        auto_mode: "mls" to always prefer MLS, "assessor" to always
                   prefer assessor, or None for interactive prompts.

    Returns:
        Dict of {(label, field_key): chosen_value} for flagged fields.
    """
    choices = {}

    for r in results:
        if r["status"] != "success" or not r["has_any_discrepancy"]:
            continue

        label = r["label"]
        address = r["address"]

        field_labels = {
            "gla": "GLA",
            "beds": "Beds",
            "baths": "Baths",
            "year_built": "Year Built",
            "basement_sqft": "Basement SqFt",
        }

        for field_key, display in field_labels.items():
            f = r["fields"][field_key]
            if not f["flag"]:
                continue

            mls_val = f["mls"]
            asr_val = f["assessor"]

            if auto_mode == "mls":
                choices[(label, field_key)] = mls_val
                continue
            elif auto_mode == "assessor":
                choices[(label, field_key)] = asr_val
                continue

            # Interactive prompt
            print(f"\n{display} discrepancy for {label} ({address}):")
            print(f"  [1] MLS: {mls_val}")
            print(f"  [2] Public Record: {asr_val}")

            while True:
                try:
                    choice = input("  Choice (1/2): ").strip()
                except (EOFError, KeyboardInterrupt):
                    print("\nSkipping remaining discrepancies.")
                    return choices

                if choice == "1":
                    choices[(label, field_key)] = mls_val
                    break
                elif choice == "2":
                    choices[(label, field_key)] = asr_val
                    break
                else:
                    print("  Please enter 1 or 2.")

    return choices
