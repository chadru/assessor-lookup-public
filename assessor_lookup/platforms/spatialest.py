"""Spatialest county assessor API client for public records lookup."""

import json
import re
import urllib.error
import urllib.parse
import urllib.request

from ..core.matching import normalize_address, normalize_identifier, select_candidate
from ..core.network import build_https_opener, open_https, require_https_url


def _spatialest_request(url, data=None, timeout=10):
    """Make an HTTP request to a Spatialest API endpoint.

    Adapted from 3_6_Fannie_builder/assessor_browser.py:208-222.
    """
    url = require_https_url(url, allowed_hosts=("property.spatialest.com",))
    parsed = urllib.parse.urlparse(url)
    referer_root = parsed.path.split("/api/", 1)[0].rstrip("/")
    referer = f"{parsed.scheme}://{parsed.netloc}{referer_root}/#/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": f"{parsed.scheme}://{parsed.netloc}",
        "Referer": referer,
    }
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    else:
        req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with open_https(
                req, timeout=timeout,
                allowed_hosts=("property.spatialest.com",)) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        # Spatialest returns 419 when the request lacks a valid page/CSRF token.
        # The body is sometimes "Page token mismatch" (the v2 search endpoint)
        # and sometimes empty (the v1 recordcard GET), so retry on any 419 by
        # fetching the page token + session cookie and replaying the request.
        if e.code == 419:
            return _spatialest_request_with_page_token(
                url, data=data, timeout=timeout, headers=headers,
            )
        raise


def _spatialest_request_with_page_token(url, data=None, timeout=10,
                                        headers=None):
    """Retry Spatialest requests that require a page CSRF token."""
    parsed = urllib.parse.urlparse(url)
    referer_root = parsed.path.split("/api/", 1)[0].rstrip("/")
    page_url = f"{parsed.scheme}://{parsed.netloc}{referer_root}/#/"
    headers = dict(headers or {})

    opener = build_https_opener(
        ("property.spatialest.com",), urllib.request.HTTPCookieProcessor(),
    )
    page_headers = {
        "User-Agent": headers.get("User-Agent", "Mozilla/5.0"),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    page_req = urllib.request.Request(page_url, headers=page_headers)
    require_https_url(
        page_url, allowed_hosts=("property.spatialest.com",), resolve_host=True,
    )
    with opener.open(page_req, timeout=timeout) as resp:
        page_html = resp.read().decode("utf-8", errors="ignore")

    token_match = re.search(
        r'<meta\s+name=["\']csrf-token["\']\s+content=["\']([^"\']+)',
        page_html,
        flags=re.IGNORECASE,
    )
    if token_match:
        headers["X-CSRF-TOKEN"] = token_match.group(1)
    headers["Referer"] = page_url

    if data is not None:
        body = json.dumps(data).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    else:
        req = urllib.request.Request(url, headers=headers, method="GET")
    require_https_url(
        url, allowed_hosts=("property.spatialest.com",), resolve_host=True,
    )
    with opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _normalize_spatialest_parcel_id(parcel_id, county_slug=""):
    """Normalize assessor parcel IDs for Spatialest free-text search."""
    parcel_id = (parcel_id or "").strip()
    if not parcel_id:
        return parcel_id
    digits = re.sub(r"\D", "", parcel_id)
    if county_slug.lower() == "denver" and len(digits) == 9:
        return f"0{digits}"
    if county_slug.lower() == "denver" and len(digits) in {10, 13}:
        return digits
    return parcel_id


def _extract_all_fields(sections, out):
    """Recursively extract all key-value pairs from Spatialest sections.

    Handles both dict-keyed (El Paso) and list-indexed (Denver) section formats.
    Adapted from 3_6_Fannie_builder/assessor_browser.py:417-431.
    """
    if isinstance(sections, dict):
        for key, val in sections.items():
            if isinstance(val, (dict, list)):
                _extract_all_fields(val, out)
            elif val is not None and str(val).strip():
                out[key] = val
    elif isinstance(sections, list):
        for item in sections:
            if isinstance(item, (dict, list)):
                _extract_all_fields(item, out)


def _parse_sqft(fields, field_names):
    """Try to extract a square footage value from multiple possible field names.

    Adapted from 3_6_Fannie_builder/assessor_browser.py:434-446.
    """
    for name in field_names:
        val = fields.get(name)
        if val:
            cleaned = re.sub(r'[,\s]|sqft|sf', '', str(val), flags=re.IGNORECASE).strip()
            try:
                parsed = int(float(cleaned))
                if parsed > 0:
                    return parsed
            except (ValueError, TypeError):
                continue
    return None


def _parse_lot_area(fields, field_names):
    """Extract lot area in square feet, handling acres / sqft units.

    Spatialest commonly exposes lot size as a display string like
    "19100 SQFT" or "0.44 ACRES". This handles either by parsing the
    number and converting acres → sqft (1 acre = 43,560 sqft).
    """
    for name in field_names:
        val = fields.get(name)
        if not val:
            continue
        s = str(val).strip()
        if not s:
            continue
        # Detect units
        is_acres = bool(re.search(r'\bacres?\b', s, re.IGNORECASE))
        cleaned = re.sub(r'[,\s]|sqft|sf|acres?', '', s, flags=re.IGNORECASE).strip()
        try:
            num = float(cleaned)
        except (ValueError, TypeError):
            continue
        if num <= 0:
            continue
        if is_acres:
            return int(round(num * 43560))
        return int(round(num))
    return None


def _looks_like_taxing_authority(value):
    """Return True for generic district/authority labels, not subdivisions."""
    upper = str(value or "").upper()
    if not upper:
        return False
    tokens = (
        "DISTRICT",
        "COUNTY",
        "CITY OF",
        "ROAD & BRIDGE",
        "CONSERVANCY",
        "AUTHORITY",
        "LIBRARY",
        "SCHOOL",
        "METRO",
    )
    return any(token in upper for token in tokens)


class SpatialestClient:
    """Client for the free Spatialest county assessor API."""

    capabilities = {"parcel_lookup": True, "building_fields": True}

    def __init__(self, timeout=10, verbose=False, county_slug="elpaso",
                 state="co"):
        self.timeout = timeout
        self.verbose = verbose
        self.county_slug = county_slug
        self.state = state

    def lookup(self, address, county_slug=None, state=None):
        """Look up a property on the county assessor.

        Args:
            address: Street address (e.g. "123 Main St").
            county_slug: Spatialest county slug (default: "elpaso").
            state: State code (default: "co").

        Returns:
            Dict with keys: status, above_grade_sqft, basement_sqft,
            finished_basement_sqft, year_built, beds, baths, market_value,
            style, assessor_url. Status is "success", "not_found",
            "timeout", "api_error", or "parse_error".
        """
        county_slug = county_slug or self.county_slug
        state = state or self.state
        address = (address or "").strip()
        if not address:
            return {"status": "invalid_address", "address": address}

        base = f"https://property.spatialest.com/{state}/{county_slug}"

        # Step 1: Search for the property
        search_url = f"{base}/api/v2/search"
        search_payload = {
            "filters": {"term": address, "page": "1"},
            "page": "1",
            "limit": 21,
            "debug": {"currentURL": f"{base}/#/", "previousURL": ""},
        }

        try:
            if self.verbose:
                print(f"  Looking up: {address}")
                print(f"  URL: {search_url}")
            search_result = _spatialest_request(
                search_url, data=search_payload, timeout=self.timeout
            )
        except urllib.error.URLError as e:
            if "timed out" in str(e).lower():
                return {"status": "timeout", "address": address, "error": str(e)}
            return {"status": "api_error", "address": address, "error": str(e)}
        except Exception as e:
            return {"status": "api_error", "address": address, "error": str(e)}

        # Extract property ID
        property_id, selection_status = self._extract_property_id(
            search_result, address, base, is_parcel=False)
        if not property_id:
            return {"status": selection_status, "address": address}

        # Step 2: Fetch record card
        card_url = f"{base}/api/v1/recordcard/{property_id}"
        try:
            card = _spatialest_request(card_url, timeout=self.timeout)
        except urllib.error.URLError as e:
            return {"status": "api_error", "address": address, "error": str(e)}
        except json.JSONDecodeError:
            return {"status": "parse_error", "address": address}
        except Exception as e:
            return {"status": "api_error", "address": address, "error": str(e)}

        # Step 3: Parse building data
        if not isinstance(card, dict):
            return {"status": "parse_error", "address": address}
        parcel_data = card.get("parcel", {})
        header = (parcel_data.get("header", {})
                  if isinstance(parcel_data, dict) else {})
        resolved_address = ""
        if isinstance(header, dict):
            resolved_address = (header.get("address") or header.get("siteaddress")
                                or header.get("propaddr")
                                or header.get("FullAddress") or "")
        if (resolved_address and normalize_address(resolved_address)
                != normalize_address(address)):
            return {"status": "not_found", "address": address,
                    "error": "assessor returned a different address"}
        try:
            return self._parse_record_card(
                card, property_id, base, resolved_address or address)
        except Exception as e:  # noqa: BLE001
            return {"status": "parse_error", "address": address,
                    "error": str(e)}

    def lookup_by_parcel(self, parcel_id, county_slug=None, state=None):
        """Look up a property on Spatialest by parcel/schedule number.

        Spatialest's search endpoint accepts free-text terms, so we
        pass the parcel_id as the term and then fetch the record card
        the same way `lookup()` does.
        """
        county_slug = county_slug or self.county_slug
        state = state or self.state
        parcel_id = (parcel_id or "").strip()
        if not parcel_id:
            return {"status": "invalid_parcel", "parcel_id": parcel_id}
        search_term = _normalize_spatialest_parcel_id(parcel_id, county_slug)

        base = f"https://property.spatialest.com/{state}/{county_slug}"

        search_url = f"{base}/api/v2/search"
        search_payload = {
            "filters": {"term": search_term, "page": "1"},
            "page": "1",
            "limit": 21,
            "debug": {"currentURL": f"{base}/#/", "previousURL": ""},
        }

        try:
            if self.verbose:
                print(f"  Looking up parcel: {search_term}")
            search_result = _spatialest_request(
                search_url, data=search_payload, timeout=self.timeout
            )
        except urllib.error.URLError as e:
            if "timed out" in str(e).lower():
                return {"status": "timeout", "parcel_id": parcel_id, "error": str(e)}
            return {"status": "api_error", "parcel_id": parcel_id, "error": str(e)}
        except Exception as e:
            return {"status": "api_error", "parcel_id": parcel_id, "error": str(e)}

        property_id, selection_status = self._extract_property_id(
            search_result, search_term, base, is_parcel=True)
        if not property_id:
            return {"status": selection_status, "parcel_id": parcel_id}

        card_url = f"{base}/api/v1/recordcard/{property_id}"
        try:
            card = _spatialest_request(card_url, timeout=self.timeout)
        except urllib.error.URLError as e:
            return {"status": "api_error", "parcel_id": parcel_id, "error": str(e)}
        except json.JSONDecodeError:
            return {"status": "parse_error", "parcel_id": parcel_id}
        except Exception as e:
            return {"status": "api_error", "parcel_id": parcel_id, "error": str(e)}

        # _parse_record_card needs an "address" string for its return dict;
        # extract the resolved street address from the record card header.
        parcel = card.get("parcel", {}) if isinstance(card, dict) else {}
        header = parcel.get("header", {}) if isinstance(parcel, dict) else {}
        resolved_address = ""
        if isinstance(header, dict):
            resolved_address = (header.get("address") or header.get("siteaddress")
                                or header.get("propaddr")
                                or header.get("FullAddress") or "")
        if not isinstance(card, dict):
            return {"status": "parse_error", "parcel_id": parcel_id}
        try:
            result = self._parse_record_card(
                card, property_id, base, resolved_address)
        except Exception as e:  # noqa: BLE001
            return {"status": "parse_error", "parcel_id": parcel_id,
                    "error": str(e)}
        returned_parcel = normalize_identifier(result.get("parcel_number"))
        wanted_parcel = normalize_identifier(search_term)
        parcel_matches = returned_parcel == wanted_parcel
        if county_slug.lower() == "denver":
            parcel_matches = (parcel_matches
                              or returned_parcel.rstrip("0")
                              == wanted_parcel.rstrip("0"))
        if returned_parcel and not parcel_matches:
            return {"status": "not_found", "parcel_id": parcel_id,
                    "error": "assessor returned a different parcel"}
        return result

    def _extract_property_id(self, search_result, query, base, is_parcel=False):
        """Extract a deterministically matched property ID and status."""
        if isinstance(search_result, dict):
            if search_result.get("id"):
                candidate, status = select_candidate(
                    [search_result],
                    address="" if is_parcel else query,
                    parcel=query if is_parcel else "",
                )
                return ((search_result["id"], "success") if candidate
                        else (None, status))
            results = (search_result.get("results") or
                       search_result.get("searchResults") or [])
            active = [r for r in results if isinstance(r, dict)
                      and r.get("parcelactive", True)]
            candidate, status = select_candidate(
                active,
                address="" if is_parcel else query,
                parcel=query if is_parcel else "",
            )
            if candidate:
                property_id = (candidate.get("id") or candidate.get("par") or
                               candidate.get("ParcelIdentifier") or
                               candidate.get("order_total__value_par"))
                if property_id:
                    return property_id, "success"
            if active:
                return None, status

        # Suggestions are safe only after an empty primary result.
        suggest_url = f"{base}/api/v2/search/suggestions"
        suggest_payload = {
            "filters": {"term": query},
            "debug": {"currentURL": f"{base}/#/", "previousURL\"": ""},
        }
        try:
            suggest_result = _spatialest_request(
                suggest_url, data=suggest_payload, timeout=self.timeout
            )
            suggestions = suggest_result.get("suggestions", [])
            candidate, status = select_candidate(
                suggestions,
                address="" if is_parcel else query,
                parcel=query if is_parcel else "",
            )
            if candidate and candidate.get("id"):
                return candidate["id"], "success"
            return None, status
        except Exception:
            return None, "not_found"

    def _parse_record_card(self, card, property_id, base, address):
        """Parse building data from a Spatialest record card response."""
        parcel = card.get("parcel", {})
        sections = parcel.get("sections", {})
        header = parcel.get("header", {})

        all_fields = {}
        _extract_all_fields(sections, all_fields)
        if isinstance(header, dict):
            all_fields.update(header)

        # GLA
        above_grade_sqft = _parse_sqft(all_fields, [
            "AboveGradeArea", "SFLA", "BLDG_SQFT", "FirstFlrArea",
            "ACTUALAREA", "TotalArea", "TotalLiving", "LivingArea",
            "FinishedArea", "ActualTotal", "GrossLivArea", "AREA",
            "improvement_sf",
        ])

        # Lot size — Spatialest exposes "DisplayArea" with units
        # ("19100 SQFT" or "0.44 ACRES") on most CO counties
        lot_size_sqft = _parse_lot_area(all_fields, [
            "DisplayArea", "LandSF", "LotSF", "LotSize", "land_sf",
            "land_square_feet", "Land_SF", "LotSqFt",
        ])

        # Garage area (sqft, not space count — assessors typically expose area)
        garage_area_sqft = _parse_sqft(all_fields, [
            "GarageArea_Sum", "GarageArea", "GAR_AREA", "garage_area",
            "GarageSF", "garage_sf",
        ])

        # Basement
        basement_sqft = _parse_sqft(all_fields, [
            "TotalBSMT", "BasementArea", "BsmtArea", "BSMTAREA",
        ])
        finished_bsmt = _parse_sqft(all_fields, [
            "FinishedBSMT", "FINBSMTAREA", "FinBsmt",
        ])

        # Year built
        year_built = (all_fields.get("YearBlt") or all_fields.get("YearBuilt")
                      or all_fields.get("YRBLT") or all_fields.get("ACTUALYEARBUILT"))
        if year_built:
            try:
                year_built = int(year_built)
            except (ValueError, TypeError):
                year_built = None

        # Beds/baths
        beds = (all_fields.get("Beds") or all_fields.get("Bedrooms")
                or all_fields.get("FIXBED") or all_fields.get("bedroom_count"))
        if beds is not None:
            try:
                beds = int(float(beds))
            except (ValueError, TypeError):
                beds = None

        baths = (all_fields.get("Baths") or all_fields.get("Bathrooms")
                 or all_fields.get("FIXBATH") or all_fields.get("FIXFULL")
                 or all_fields.get("bathroom_count"))
        if baths is not None:
            try:
                baths = float(baths)
            except (ValueError, TypeError):
                baths = None

        # Market value
        market_value = ""
        if isinstance(header, dict):
            market_value = header.get("marketvalueheader", "")
        if not market_value:
            market_value = (all_fields.get("ACTUAL_VAL")
                            or all_fields.get("total_value") or "")

        # Style
        style = (all_fields.get("ResStyle") or all_fields.get("STYLEDESCR")
                 or all_fields.get("CLASSDESCR")
                 or all_fields.get("property_type") or "")

        # Owner(s) — may be in header or sections (all_fields)
        owners_list = []
        raw_owners = (header.get("owners_list") if isinstance(header, dict) else None)
        if not raw_owners:
            raw_owners = all_fields.get("owners_list")
        if raw_owners:
            if isinstance(raw_owners, str):
                import json as _json
                try:
                    raw_owners = _json.loads(raw_owners)
                except (ValueError, TypeError):
                    raw_owners = [raw_owners]
            if isinstance(raw_owners, list):
                owners_list = raw_owners
        if not owners_list:
            raw_owner = (all_fields.get("OwnerName")
                         or all_fields.get("OWNERNAME")
                         or all_fields.get("OwnerNames")
                         or all_fields.get("owner_name") or "")
            if raw_owner:
                owners_list = [raw_owner]
        owner = "; ".join(str(o) for o in owners_list) if owners_list else ""

        # Legal description — may be in header or sections
        legal = ""
        if isinstance(header, dict):
            legal = header.get("legalText", "")
        if not legal:
            legal = (all_fields.get("legalText") or all_fields.get("LegalDesc")
                     or all_fields.get("LEGAL")
                     or all_fields.get("LEGAL_DESC1")
                     or all_fields.get("LegalDescription")
                     or all_fields.get("legal_description") or "")

        # Parcel number / schedule number
        parcel_number = ""
        if isinstance(header, dict):
            parcel_number = header.get("par", "")
        if not parcel_number:
            parcel_number = (all_fields.get("par")
                             or all_fields.get("PARID")
                             or all_fields.get("ScheduleNumber")
                             or all_fields.get("PARCEL")
                             or all_fields.get("account_no")
                             or all_fields.get("state_parcel_no") or "")

        # Tax info — compute from assessed value × mill levy
        levy_str = (all_fields.get("parcellevy")
                    or (header.get("parcellevy", "") if isinstance(header, dict) else ""))
        assessed_str = (all_fields.get("TotAssd_NonSchool")
                        or (header.get("TotAssd_NonSchool", "")
                            if isinstance(header, dict) else ""))
        tax_amount = ""
        if levy_str and assessed_str:
            try:
                levy_val = float(str(levy_str).replace(",", ""))
                assessed_val = float(
                    str(assessed_str).replace("$", "").replace(",", ""))
                tax_amount = f"${assessed_val * levy_val / 1000:,.0f}"
            except (ValueError, TypeError):
                pass
        if not tax_amount:
            tax_amount = (all_fields.get("TaxAmount")
                          or all_fields.get("TAXAMT") or "")

        tax_year = ""
        if isinstance(header, dict):
            tax_year = header.get("TaxYear", "")
        if not tax_year:
            tax_year = (all_fields.get("TaxYear")
                        or all_fields.get("TAXYEAR") or "")
        if not tax_year and tax_amount:
            # Default to prior year when we have tax data but no year
            from datetime import date as _date
            tax_year = str(_date.today().year - 1)

        assessed_value = ""
        if isinstance(header, dict):
            assessed_value = (header.get("TotAssd_NonSchool", "")
                              or header.get("TotMkt", ""))
        if not assessed_value:
            assessed_value = (all_fields.get("TotMkt")
                              or all_fields.get("TotAssd_NonSchool")
                              or all_fields.get("ACTUAL_VAL")
                              or all_fields.get("AssessedValue")
                              or all_fields.get("total_assessed_value") or "")

        zoning = ""
        if isinstance(header, dict):
            zoning = header.get("zone", "") or header.get("zoning", "")
        if not zoning:
            zoning = (all_fields.get("zone")
                      or all_fields.get("Zone")
                      or all_fields.get("ZONE1")
                      or all_fields.get("ZONING")
                      or all_fields.get("zoning")
                      or all_fields.get("Zoning") or "")

        # Neighborhood / subdivision name — prefer explicit subdivision keys.
        neighborhood = ""
        if isinstance(header, dict):
            header_name = header.get("name", "")
            if header_name and not _looks_like_taxing_authority(header_name):
                neighborhood = header_name
        if not neighborhood:
            neighborhood = (all_fields.get("Neighborhood")
                            or all_fields.get("NeighborhoodName")
                            or all_fields.get("NBHDNAME")
                            or all_fields.get("NBHD")
                            or all_fields.get("Subdivision")
                            or all_fields.get("SubdivisionName")
                            or all_fields.get("subdivision_name") or "")
        if neighborhood and _looks_like_taxing_authority(neighborhood):
            neighborhood = ""

        property_url = f"{base}/#/property/{property_id}"

        # Latitude / longitude — Spatialest exposes the parcel centroid at the
        # top level of the record card as cty (lat) / ctx (lon). Round to 6
        # decimals to match the precision TOTAL stores.
        latitude = longitude = ""
        for lat_key in ("cty", "lat", "latitude", "Latitude"):
            if card.get(lat_key) not in (None, ""):
                try:
                    latitude = round(float(card.get(lat_key)), 6)
                except (ValueError, TypeError):
                    latitude = ""
                break
        for lon_key in ("ctx", "lon", "lng", "longitude", "Longitude"):
            if card.get(lon_key) not in (None, ""):
                try:
                    longitude = round(float(card.get(lon_key)), 6)
                except (ValueError, TypeError):
                    longitude = ""
                break

        return {
            "status": "success",
            "address": address,
            "property_id": property_id,
            "assessor_url": property_url,
            "latitude": latitude,
            "longitude": longitude,
            "above_grade_sqft": above_grade_sqft,
            "basement_sqft": basement_sqft,
            "finished_basement_sqft": finished_bsmt,
            "year_built": year_built,
            "beds": beds,
            "baths": baths,
            "market_value": market_value,
            "style": style,
            "owner": owner,
            "legal": legal,
            "parcel_number": parcel_number,
            "tax_amount": tax_amount,
            "tax_year": tax_year,
            "assessed_value": assessed_value,
            "neighborhood": neighborhood,
            "zoning": zoning,
            "lot_size_sqft": lot_size_sqft,
            "garage_area_sqft": garage_area_sqft,
        }
