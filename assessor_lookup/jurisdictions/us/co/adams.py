"""Adams County, CO jurisdiction driver (ArcGIS REST Feature Services)."""

import logging
import urllib.error
import urllib.parse

from ....core.matching import select_candidate
from ....platforms import arcgis

_esc = arcgis.escape_sql_literal

logger = logging.getLogger(__name__)

BASE_URL = "https://services3.arcgis.com/4PNQOtAivErR7nbT/arcgis/rest/services"
PARCELS_URL = f"{BASE_URL}/Parcels/FeatureServer/0/query"
IMPROVEMENTS_URL = f"{BASE_URL}/Property_Improvements/FeatureServer/0/query"
VALUES_URL = f"{BASE_URL}/Property_Values/FeatureServer/0/query"

_ALLOWED_HOSTS = ("services3.arcgis.com",)


def _arcgis_get(url, params=None, timeout=15):
    return arcgis.arcgis_get(url, _ALLOWED_HOSTS, params=params, timeout=timeout)


class AdamsClient:
    """Client for Adams County CO assessor (ArcGIS Feature Services)."""

    capabilities = {"parcel_lookup": True, "building_fields": True}

    def __init__(self, timeout=15, verbose=False):
        self.timeout = timeout
        self.verbose = verbose

    def lookup(self, address, **kwargs):
        """Look up a property on the Adams County assessor.

        Returns standardized dict matching SpatialestClient contract.
        """
        address = (address or "").strip()
        if not address:
            return {"status": "invalid_address", "address": address}

        # Step 1: Search Parcels layer by address components
        try:
            parcel, selection_status = self._search_parcel(address)
        except urllib.error.URLError as e:
            if "timed out" in str(e).lower():
                return {"status": "timeout", "address": address, "error": str(e)}
            return {"status": "api_error", "address": address, "error": str(e)}
        except Exception as e:
            return {"status": "api_error", "address": address, "error": str(e)}

        if not parcel:
            return {"status": selection_status, "address": address}

        try:
            return self._build_result(parcel, address=address)
        except Exception as e:  # noqa: BLE001
            return {"status": "parse_error", "address": address,
                    "error": str(e)}

    def lookup_by_parcel(self, parcel_id, **kwargs):
        """Look up a property by parcel number directly (no geocoding).

        Skips the address-based search and queries the Parcels layer
        with parcelnb='{parcel_id}', then chains to improvements + values
        the same way as `lookup()`.
        """
        parcel_id = (parcel_id or "").strip()
        if not parcel_id:
            return {"status": "invalid_parcel", "parcel_id": parcel_id}

        try:
            parcel = self._fetch_parcel_by_id(parcel_id)
        except urllib.error.URLError as e:
            if "timed out" in str(e).lower():
                return {"status": "timeout", "parcel_id": parcel_id, "error": str(e)}
            return {"status": "api_error", "parcel_id": parcel_id, "error": str(e)}
        except Exception as e:
            return {"status": "api_error", "parcel_id": parcel_id, "error": str(e)}

        if not parcel:
            return {"status": "not_found", "parcel_id": parcel_id}

        try:
            return self._build_result(parcel, address=parcel.get("siteaddress") or "")
        except Exception as e:  # noqa: BLE001
            return {"status": "parse_error", "parcel_id": parcel_id,
                    "error": str(e)}

    def _fetch_parcel_by_id(self, parcel_id):
        """Query Parcels layer by parcel number. Return attributes dict."""
        # Try parcelnb (string field) first, then PIN as fallback
        for field in ("parcelnb", "PARCELNB", "PIN", "pin"):
            result = _arcgis_get(PARCELS_URL, params={
                "where": f"{field}='{_esc(parcel_id)}'",
                "outFields": "*",
                "returnGeometry": "false",
                "resultRecordCount": "1",
                "f": "json",
            }, timeout=self.timeout)
            features = result.get("features", [])
            if features:
                candidate, status = select_candidate(features, parcel=parcel_id)
                if candidate:
                    return candidate.get("attributes", candidate)
                if status == "ambiguous":
                    return None
        return None

    def _build_result(self, parcel, address=""):
        """Build the standardized result dict from a parcel record.

        Shared by `lookup()` (after spatial/address resolution) and
        `lookup_by_parcel()`.
        """
        pin = parcel.get("PIN") or parcel.get("pin") or ""

        building = {}
        if pin:
            try:
                building = self._query_improvements(pin)
            except Exception as e:
                if self.verbose:
                    logger.warning("Adams improvements query failed: %s", e)

        values = {}
        if pin:
            try:
                values = self._query_values(pin)
            except Exception as e:
                if self.verbose:
                    logger.warning("Adams values query failed: %s", e)

        above_grade = _safe_int(building.get("sf"))
        basement_sqft = _safe_int(building.get("bsmntsf"))
        finished_bsmt = _safe_int(building.get("finbsmntsf"))
        year_built = _safe_int(building.get("yrblt"))
        beds = _safe_int(building.get("bedrooms"))
        baths = _safe_float(building.get("baths"))
        style = building.get("bltasdesc") or ""

        owner = (parcel.get("ownernamefull") or parcel.get("ownername1") or "")
        legal = parcel.get("legal") or ""
        parcel_number = parcel.get("PARCELNB") or parcel.get("parcelnb") or ""
        subdivision = parcel.get("subname") or ""

        # Lot size: parcel polygon area from the Parcels FeatureServer
        lot_size_sqft = None
        sa = parcel.get("Shape__Area") or parcel.get("Shape_Area")
        if sa is not None:
            try:
                lot_size_sqft = int(round(float(sa)))
            except (ValueError, TypeError):
                pass

        # Garage area (sqft) — Adams improvements layer exposes garage_sf or gar_sf
        garage_area_sqft = _safe_int(
            building.get("garage_sf") or building.get("gar_sf")
            or building.get("garagesf") or building.get("attgarsf")
        )

        market_value = values.get("acttotalval")
        assessed_value = values.get("asdtotalval")
        mill_levy = _safe_float(values.get("milllevy"))

        tax_amount = ""
        if assessed_value and mill_levy:
            try:
                tax_amount = f"${float(assessed_value) * mill_levy / 1000:,.0f}"
            except (ValueError, TypeError):
                pass

        assessor_url = ""
        if parcel_number:
            assessor_url = (
                f"https://gisapp.adcogov.org/quicksearch/"
                f"?find={urllib.parse.quote(parcel_number)}"
            )

        return {
            "status": "success",
            "address": address or parcel.get("siteaddress") or "",
            "property_id": pin,
            "assessor_url": assessor_url,
            "above_grade_sqft": above_grade,
            "basement_sqft": basement_sqft,
            "finished_basement_sqft": finished_bsmt,
            "year_built": year_built,
            "beds": beds,
            "baths": baths,
            "market_value": _format_currency(market_value),
            "style": style,
            "owner": owner,
            "legal": legal,
            "parcel_number": parcel_number,
            "tax_amount": tax_amount,
            "tax_year": "",
            "assessed_value": _format_currency(assessed_value),
            "neighborhood": subdivision,
            "lot_size_sqft": lot_size_sqft,
            "garage_area_sqft": garage_area_sqft,
        }

    def _search_parcel(self, address):
        """Search parcels and return ``(attributes, status)``."""
        import re
        parts = address.strip().split(None, 1)
        if len(parts) < 2:
            return None, "not_found"
        street_num = parts[0]
        street_rest = parts[1].upper()

        # Extract the core street name (strip suffix, directional prefix)
        suffixes = (r'\bST\b', r'\bSTREET\b', r'\bAVE\b', r'\bAVENUE\b',
                    r'\bDR\b', r'\bDRIVE\b', r'\bRD\b', r'\bROAD\b',
                    r'\bCT\b', r'\bCOURT\b', r'\bLN\b', r'\bLANE\b',
                    r'\bPL\b', r'\bPLACE\b', r'\bBLVD\b', r'\bCIR\b',
                    r'\bWAY\b', r'\bTRL\b', r'\bPKWY\b')
        street_core = street_rest
        for suf in suffixes:
            street_core = re.sub(suf, '', street_core).strip()
        # Strip directional prefixes
        street_core = re.sub(r'^[NSEW]\s+', '', street_core).strip()

        # Try concataddr1 first (most reliable)
        where = (
            f"streetno='{street_num}' AND "
            f"UPPER(concataddr1) LIKE '%{street_core}%'"
        )
        result = _arcgis_get(PARCELS_URL, params={
            "where": where,
            "outFields": "*",
            "returnGeometry": "false",
            "resultRecordCount": "5",
            "f": "json",
        }, timeout=self.timeout)

        features = result.get("features", [])
        if features:
            candidate, status = select_candidate(features, address=address)
            if candidate:
                return candidate.get("attributes", candidate), "success"
            return None, status

        # Fallback: search by streetname field
        where2 = (
            f"streetno='{street_num}' AND "
            f"UPPER(streetname) LIKE '%{street_core}%'"
        )
        result2 = _arcgis_get(PARCELS_URL, params={
            "where": where2,
            "outFields": "*",
            "returnGeometry": "false",
            "resultRecordCount": "5",
            "f": "json",
        }, timeout=self.timeout)

        features2 = result2.get("features", [])
        if features2:
            candidate, status = select_candidate(features2, address=address)
            if candidate:
                return candidate.get("attributes", candidate), "success"
            return None, status

        return None, "not_found"

    def _query_improvements(self, pin):
        """Query Property_Improvements by PIN. Return attributes dict."""
        result = _arcgis_get(IMPROVEMENTS_URL, params={
            "where": f"pin='{_esc(pin)}'",
            "outFields": "*",
            "returnGeometry": "false",
            "f": "json",
        }, timeout=self.timeout)

        features = result.get("features", [])
        if features:
            return features[0].get("attributes", {})
        return {}

    def _query_values(self, pin):
        """Query Property_Values by PIN. Return attributes dict."""
        result = _arcgis_get(VALUES_URL, params={
            "where": f"pin='{_esc(pin)}'",
            "outFields": "*",
            "returnGeometry": "false",
            "f": "json",
        }, timeout=self.timeout)

        features = result.get("features", [])
        if features:
            return features[0].get("attributes", {})
        return {}


def _safe_int(val):
    """Safely convert to int."""
    if val is None:
        return None
    try:
        return int(float(str(val).replace(",", "").strip()))
    except (ValueError, TypeError):
        return None


def _safe_float(val):
    """Safely convert to float."""
    if val is None:
        return None
    try:
        return float(str(val).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def _format_currency(value):
    if value in (None, ""):
        return ""
    try:
        return f"${float(str(value).replace(',', '')):,.0f}"
    except (ValueError, TypeError):
        return str(value)


def build(entry, timeout=15, verbose=False):
    """Driver factory used by the arcgis platform dispatcher."""
    return AdamsClient(timeout=timeout, verbose=verbose)
