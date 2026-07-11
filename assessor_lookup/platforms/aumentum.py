"""Jefferson County assessor client (Aumentum REST API)."""

import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request

from ..core.matching import normalize_identifier, select_candidate
from ..core.network import open_https, require_https_url

logger = logging.getLogger(__name__)

BASE_URL = "https://propertysearch.jeffco.us/api"


def _format_fractional_value(value):
    """Format fractional numeric values the way the reference report does."""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        text = f"{float(text):g}"
    except (ValueError, TypeError):
        pass
    if text.startswith("0."):
        return text[1:]
    if text.startswith("-0."):
        return f"-{text[2:]}"
    return text


def _jeffco_get(url, timeout=10, allowed_hosts=("propertysearch.jeffco.us",)):
    """GET request to an Aumentum API, return parsed JSON."""
    url = require_https_url(url, allowed_hosts=allowed_hosts)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
    }
    req = urllib.request.Request(url, headers=headers, method="GET")
    with open_https(
            req, timeout=timeout, allowed_hosts=allowed_hosts) as resp:
        return json.loads(resp.read())


_STREET_SUFFIXES = re.compile(
    r'\s+(St|Street|Ave|Avenue|Blvd|Boulevard|Dr|Drive|Ln|Lane|Way|'
    r'Ct|Court|Cir|Circle|Pl|Place|Rd|Road|Trl|Trail|Pkwy|Parkway|'
    r'Loop|Run|Path|Ter|Terrace|Pt|Point)\s*$',
    re.IGNORECASE,
)


def _parse_address(address):
    """Split '7580 S Lost Ranger Peak' into (number, street_name).

    Strips directional prefix (N/S/E/W/NE/NW/SE/SW) and street suffix
    (Way, Ct, Dr, etc.) since Jeffco API doesn't use them.
    """
    address = address.strip()
    m = re.match(r'^(\d+)\s+([NSEW]{1,2}\s+)?(.+)$', address, re.IGNORECASE)
    if m:
        street = _STREET_SUFFIXES.sub('', m.group(3).strip())
        return m.group(1), street.strip()
    # Fallback: first token is number, rest is street
    parts = address.split(None, 1)
    if len(parts) == 2 and parts[0].isdigit():
        street = _STREET_SUFFIXES.sub('', parts[1])
        return parts[0], street.strip()
    return None, address


class JeffcoClient:
    """Aumentum platform client. Jefferson County, CO is the default (and
    first) deployment; another Aumentum county supplies its own ``base``."""

    capabilities = {"parcel_lookup": True, "building_fields": True}

    def __init__(self, base=None, timeout=10, verbose=False):
        self.base_url = require_https_url((base or BASE_URL).rstrip("/"))
        self._allowed_hosts = (
            urllib.parse.urlsplit(self.base_url).hostname,)
        self.timeout = timeout
        self.verbose = verbose

    def lookup(self, address, **kwargs):
        """Look up a property on the Jefferson County assessor.

        Returns standardized dict matching SpatialestClient contract.
        """
        address = (address or "").strip()
        if not address:
            return {"status": "invalid_address", "address": address}

        addr_num, street_name = _parse_address(address)
        if not addr_num:
            return {"status": "invalid_address", "address": address}

        # Step 1: Search
        params = urllib.parse.urlencode({
            "addressNumber": addr_num,
            "streetName": street_name,
            "Skip": "0",
            "Take": "10",
        })
        search_url = f"{self.base_url}/address?{params}"

        try:
            if self.verbose:
                logger.info("Jeffco search: %s", search_url)
            result = _jeffco_get(search_url, timeout=self.timeout, allowed_hosts=self._allowed_hosts)
        except urllib.error.URLError as e:
            if "timed out" in str(e).lower():
                return {"status": "timeout", "address": address, "error": str(e)}
            return {"status": "api_error", "address": address, "error": str(e)}
        except Exception as e:
            return {"status": "api_error", "address": address, "error": str(e)}

        # Extract property ID from results
        property_id, selection_status = self._extract_property_id(result, address)
        if not property_id:
            return {"status": selection_status, "address": address}

        # Step 2: Fetch details
        try:
            return self._fetch_details(property_id, address)
        except urllib.error.URLError as e:
            if "timed out" in str(e).lower():
                return {"status": "timeout", "address": address, "error": str(e)}
            return {"status": "api_error", "address": address, "error": str(e)}
        except Exception as e:
            return {"status": "api_error", "address": address, "error": str(e)}

    def lookup_by_parcel(self, parcel_id, **kwargs):
        """Look up a property by Jeffco PIN/Schedule Number directly.

        Aumentum's property endpoint is keyed by uniquePropertyId, which
        for Jeffco is the same value as the PIN/account number after
        URL-encoding. This skips the addressNumber/streetName search
        step and goes straight to `_fetch_details(parcel_id, ...)`.
        """
        parcel_id = (parcel_id or "").strip()
        if not parcel_id:
            return {"status": "invalid_parcel", "parcel_id": parcel_id}

        try:
            result = self._fetch_details(parcel_id, address="")
        except urllib.error.URLError as e:
            if "timed out" in str(e).lower():
                return {"status": "timeout", "parcel_id": parcel_id, "error": str(e)}
            return {"status": "api_error", "parcel_id": parcel_id, "error": str(e)}
        except Exception as e:
            return {"status": "api_error", "parcel_id": parcel_id, "error": str(e)}

        # If the property endpoint returned no usable data the result will
        # have status="success" but every field empty. Treat that as not_found
        # so the caller can fall back to address lookup.
        if (result.get("status") == "success"
                and not result.get("parcel_number")
                and not result.get("above_grade_sqft")
                and not result.get("owner")):
            return {"status": "not_found", "parcel_id": parcel_id}
        if (result.get("status") == "success" and result.get("parcel_number")
                and normalize_identifier(result["parcel_number"])
                != normalize_identifier(parcel_id)):
            return {"status": "not_found", "parcel_id": parcel_id,
                    "error": "assessor returned a different parcel"}
        return result

    def _extract_property_id(self, search_result, address):
        """Extract an exactly matched uniquePropertyId and status."""
        results = []
        if isinstance(search_result, dict):
            results = (search_result.get("items")
                       or search_result.get("results")
                       or search_result.get("searchResults") or [])
        elif isinstance(search_result, list):
            results = search_result

        candidate, status = select_candidate(results, address=address)
        if candidate and candidate.get("uniquePropertyId"):
            return candidate["uniquePropertyId"], "success"
        return None, status

    def _fetch_details(self, property_id, address):
        """Fetch all detail endpoints and build standardized result."""
        encoded_id = urllib.parse.quote(str(property_id), safe='')

        # Fetch endpoints — Jeffco wraps data in nested keys
        prop_raw = self._get_endpoint(f"property/{encoded_id}")
        inv_raw = self._get_endpoint(f"inventorySummary/{encoded_id}")
        val_raw = self._get_endpoint(f"value/{encoded_id}")
        legal_raw = self._get_endpoint(f"legalDescription/{encoded_id}")
        mill_raw = self._get_endpoint(f"authorityMillLevy/{encoded_id}")

        # Unwrap nested Aumentum response wrappers
        prop = prop_raw.get("propertyDetails", prop_raw) if isinstance(prop_raw, dict) else {}
        inv = inv_raw.get("inventorySummaryDetails", inv_raw) if isinstance(inv_raw, dict) else {}
        value_list = val_raw.get("valueDetailsList", []) if isinstance(val_raw, dict) else []
        legal_list = legal_raw.get("legalDescriptionDetailsList", []) if isinstance(legal_raw, dict) else []
        mill_obj = mill_raw.get("authorityMillLevyDetailsList", []) if isinstance(mill_raw, dict) else []

        # Parse building data from inventory
        above_grade = _safe_int(inv.get("totalAboveGradeArea"))
        basement_total = _safe_int(inv.get("totalBasementArea")) or 0
        garden_level = _safe_int(inv.get("totalGardenLevelArea")) or 0
        basement_sqft = (basement_total + garden_level) or None
        year_built = _safe_int(inv.get("bld1YearBuilt"))
        style = inv.get("bld1Design") or inv.get("design") or ""

        # Lot size: prefer landSquareFeet from legalDescription, fall
        # back to totalAcres × 43,560 from inventorySummary
        lot_size_sqft = None
        if legal_list:
            ld = legal_list[0] if isinstance(legal_list[0], dict) else {}
            lsf = _safe_int(ld.get("landSquareFeet"))
            if lsf:
                lot_size_sqft = lsf
            elif ld.get("landAcres"):
                try:
                    lot_size_sqft = int(round(float(ld["landAcres"]) * 43560))
                except (ValueError, TypeError):
                    pass
        if lot_size_sqft is None:
            acres = inv.get("totalAcres")
            if acres:
                try:
                    lot_size_sqft = int(round(float(acres) * 43560))
                except (ValueError, TypeError):
                    pass

        # Garage area (sqft)
        garage_area_sqft = _safe_int(
            inv.get("attachedGarageArea") or inv.get("garageArea")
        )

        # Parse owner / parcel from property
        owner = prop.get("displayName") or ""
        if not owner:
            owners = prop.get("ownerList") or prop.get("owners") or []
            if isinstance(owners, list):
                names = [o.get("displayName", "") if isinstance(o, dict) else str(o) for o in owners]
                owner = "; ".join(n for n in names if n)
        parcel_number = str(prop.get("ain") or prop.get("pin") or "")

        subdivision_raw = prop.get("subdivision") or ""
        subdivision_name = ""
        if subdivision_raw:
            subdivision_name = re.sub(r"^\d+\s*", "", str(subdivision_raw)).strip()
        neighborhood = subdivision_name or prop.get("neighborhood") or ""

        # Parse values (most recent year first)
        market_value = ""
        assessed_value = ""
        tax_year = ""
        if value_list:
            latest = value_list[0]
            market_value = str(latest.get("totalActual", ""))
            assessed_value = str(latest.get("totalAssessed", ""))
            tax_year = str(latest.get("taxYear", ""))

        # Parse legal description
        legal = ""
        if legal_list:
            legal = self._parse_legal(legal_list[0], subdivision=subdivision_raw)

        # Compute tax from mill levy
        tax_amount = ""
        total_mill = 0
        for entry in mill_obj:
            if isinstance(entry, dict):
                ml = entry.get("totalMillLevy")
                if ml:
                    try:
                        total_mill = float(str(ml))
                    except (ValueError, TypeError):
                        pass
        if total_mill and assessed_value:
            try:
                assessed_num = float(str(assessed_value).replace(",", ""))
                tax_amount = f"${assessed_num * total_mill / 1000:,.0f}"
            except (ValueError, TypeError):
                pass

        if not address:
            address = (prop.get("propertyAddress") or "").strip().title()

        property_url = f"https://propertysearch.jeffco.us/propertyDetail/{urllib.parse.quote(str(property_id), safe='')}"

        return {
            "status": "success",
            "address": address,
            "property_id": str(property_id),
            "assessor_url": property_url,
            "above_grade_sqft": above_grade,
            "basement_sqft": basement_sqft,
            "finished_basement_sqft": None,
            "year_built": year_built,
            "beds": None,
            "baths": None,
            "market_value": str(market_value) if market_value else "",
            "style": style,
            "owner": owner,
            "legal": legal,
            "parcel_number": parcel_number,
            "tax_amount": tax_amount,
            "tax_year": tax_year,
            "assessed_value": str(assessed_value) if assessed_value else "",
            "neighborhood": neighborhood,
            "lot_size_sqft": lot_size_sqft,
            "garage_area_sqft": garage_area_sqft,
        }

    def _get_endpoint(self, path):
        """Fetch a Jeffco API endpoint, return parsed JSON or empty dict."""
        url = f"{self.base_url}/{path}"
        try:
            return _jeffco_get(url, timeout=self.timeout, allowed_hosts=self._allowed_hosts)
        except Exception as e:
            if self.verbose:
                logger.warning("Jeffco endpoint %s failed: %s", path, e)
            return {}

    def _parse_values(self, data):
        """Parse value data from a dict response."""
        # Look for most recent year
        years = data.get("values") or data.get("years") or []
        if isinstance(years, list) and years:
            latest = years[0]  # Usually sorted newest first
            actual = latest.get("actualTotal") or latest.get("totalActual") or ""
            assessed = latest.get("assessedTotal") or latest.get("totalAssessed") or ""
            year = str(latest.get("year") or latest.get("taxYear") or "")
            return str(actual), str(assessed), year
        # Flat structure
        actual = data.get("actualTotal") or data.get("totalActual") or ""
        assessed = data.get("assessedTotal") or data.get("totalAssessed") or ""
        year = str(data.get("year") or data.get("taxYear") or "")
        return str(actual), str(assessed), year

    def _parse_values_list(self, data_list):
        """Parse value data from a list response (take most recent)."""
        if not data_list:
            return "", "", ""
        latest = data_list[0]
        if isinstance(latest, dict):
            return self._parse_values(latest)
        return "", "", ""

    def _parse_legal(self, data, subdivision=""):
        """Build legal description string from legal endpoint data."""
        parts = []
        section = data.get("section")
        township = data.get("township")
        range_ = data.get("range")
        quarter = data.get("quarterSection")
        block = data.get("block")
        has_block = isinstance(data, dict) and "block" in data
        lot = data.get("lot")
        tract = data.get("tract")
        land_sqft = data.get("landSquareFeet")
        land_acres = data.get("landAcres")

        if section:
            parts.append(f"SECTION {section}")
        if township:
            parts.append(f"TOWNSHIP {township}")
        if range_:
            parts.append(f"RANGE {range_}")
        if quarter:
            parts.append(f"QTR {quarter}")

        subdivision = str(subdivision or "").strip()
        if subdivision:
            code_match = re.match(r"^(\d+)\s*(.*)$", subdivision)
            if code_match:
                subdiv_code, subdiv_name = code_match.groups()
                parts.append(f"SUBDIVISIONCD {subdiv_code}")
                if subdiv_name:
                    parts.append(f"SUBDIVISIONNAME {subdiv_name.strip().upper()}")
            else:
                parts.append(f"SUBDIVISIONNAME {subdivision.upper()}")

        if has_block:
            parts.append(f"BLOCK {block}" if str(block or "").strip() else "BLOCK")
        if lot:
            parts.append(f"LOT {lot}")
        if tract:
            parts.append(f"TRACT {tract}")
        if land_sqft:
            try:
                parts.append(f"SIZE: {int(float(land_sqft))}")
            except (ValueError, TypeError):
                parts.append(f"SIZE: {land_sqft}")
        if land_acres:
            parts.append(f"TRACT VALUE: {_format_fractional_value(land_acres)}")

        if parts:
            return " ".join(parts)
        # Fallback: look for a text field
        return data.get("legalDescription") or data.get("legal") or ""

    def _compute_tax(self, mill_data, assessed_value_str):
        """Compute tax from mill levy and assessed value."""
        try:
            assessed = float(str(assessed_value_str).replace(",", "").replace("$", ""))
        except (ValueError, TypeError):
            return ""

        total_levy = 0
        levies = []
        if isinstance(mill_data, dict):
            levies = mill_data.get("levies") or mill_data.get("authorities") or []
        elif isinstance(mill_data, list):
            levies = mill_data

        for item in levies:
            if isinstance(item, dict):
                rate = item.get("levy") or item.get("millLevy") or item.get("rate") or 0
                try:
                    total_levy += float(rate)
                except (ValueError, TypeError):
                    continue

        if total_levy > 0:
            tax = assessed * total_levy / 1000
            return f"${tax:,.0f}"
        return ""


def _safe_int(val):
    """Safely convert to int."""
    if val is None:
        return None
    try:
        return int(float(str(val).replace(",", "").strip()))
    except (ValueError, TypeError):
        return None
