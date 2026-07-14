"""assessor-lookup — automated county assessor public-records search.

Look up property records (owner, legal, GLA, beds/baths, year built, taxes,
lat/lon, ...) straight from county assessor public APIs, and diff them against
MLS data to flag discrepancies before an appraisal report goes out.

Quickstart:
    from assessor_lookup import lookup
    rec = lookup("123 Main St", county="El Paso")
    print(rec["owner"], rec["above_grade_sqft"], rec["year_built"])
"""

from .checker import (  # noqa: F401
    check_public_records,
    detect_county,
    print_discrepancy_report,
    _get_client,
)

__version__ = "0.1.0"

__all__ = [
    "lookup",
    "check_public_records",
    "detect_county",
    "print_discrepancy_report",
    "render_assessor_card_pdf",
    "__version__",
]


def lookup(address=None, county="El Paso", state="co", parcel=None,
           verbose=False):
    """Look up one property on its county assessor.

    Args:
        address: Street address (e.g. "123 Main St"). Optional if parcel given.
        county: County name (e.g. "El Paso", "Adams"). See county_registry.json.
        state: Two-letter state code.
        parcel: Parcel / schedule number — preferred over address when known.
        verbose: Print request detail.

    Returns:
        Dict with at least ``status`` ("success", "not_found", "timeout",
        "api_error"); on success also owner, legal, parcel_number,
        above_grade_sqft, basement_sqft, beds, baths, year_built, tax_amount,
        assessed_value, latitude, longitude, assessor_url, and more
        (availability varies by county platform).
    """
    if not address and not parcel:
        raise ValueError("Provide an address or a parcel number")

    client, entry = _get_client(county, state, verbose=verbose)
    capabilities = getattr(client, "capabilities", {})

    result = None
    if parcel and capabilities.get("parcel_lookup"):
        result = client.lookup_by_parcel(parcel)
    if address and (not result or result.get("status") != "success"):
        result = client.lookup(address)
    return result or {"status": "invalid_address",
                      "error": "No address or supported parcel lookup"}


def render_assessor_card_pdf(assessor_url, out_path, timeout_s=60):
    """Print an assessor property page to PDF (requires the [card] extra)."""
    from .card import render_assessor_card_pdf as _render
    return _render(assessor_url, out_path, timeout_s=timeout_s)
