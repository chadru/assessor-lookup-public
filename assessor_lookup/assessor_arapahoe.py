"""Arapahoe County assessor client (ArcGIS REST API)."""

import json
import logging
import urllib.error
import urllib.parse
import urllib.request

from .matching import select_candidate
from .network import open_https, require_https_url

logger = logging.getLogger(__name__)

BASE_URL = "https://gis.arapahoegov.com/arcgis/rest/services"
GEOCODE_URL = f"{BASE_URL}/AddressLocator/GeocodeServer/findAddressCandidates"
PARCEL_LAYER_URL = f"{BASE_URL}/ArapaMAP/MapServer/276/query"       # Parcels - Addressed
OWNER_LAYER_URL = f"{BASE_URL}/ArapaMAP/MapServer/277/query"        # Parcels - Owners
BUILDING_LAYER_URL = f"{BASE_URL}/ArapaMAP/MapServer/286/query"     # Land and Market Value
IMPROVEMENTS_LAYER_URL = f"{BASE_URL}/ArapaMAP/MapServer/167/query" # Parcel Improvements
SUBDIVISION_LAYER_URL = f"{BASE_URL}/ArapaMAP/MapServer/151/query"  # Subdivisions


def _arcgis_get(url, params=None, timeout=15):
    """GET request to ArcGIS REST endpoint, return parsed JSON."""
    url = require_https_url(url, allowed_hosts=("gis.arapahoegov.com",))
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
    }
    req = urllib.request.Request(url, headers=headers, method="GET")
    with open_https(
            req, timeout=timeout,
            allowed_hosts=("gis.arapahoegov.com",)) as resp:
        return json.loads(resp.read())


class ArapahoeClient:
    """Client for Arapahoe County CO assessor (ArcGIS platform)."""

    def __init__(self, timeout=15, verbose=False):
        self.timeout = timeout
        self.verbose = verbose

    def lookup(self, address, **kwargs):
        """Look up a property on the Arapahoe County assessor.

        Returns standardized dict matching SpatialestClient contract.
        """
        address = (address or "").strip()
        if not address:
            return {"status": "invalid_address", "address": address}

        # Step 1: Geocode the address
        try:
            coords, selection_status = self._geocode(address)
        except urllib.error.URLError as e:
            if "timed out" in str(e).lower():
                return {"status": "timeout", "address": address, "error": str(e)}
            return {"status": "api_error", "address": address, "error": str(e)}
        except Exception as e:
            return {"status": "api_error", "address": address, "error": str(e)}

        if not coords:
            return {"status": selection_status, "address": address}

        x, y = coords

        # Step 2: Spatial query Layer 276 for parcel info
        parcel_status = "not_found"
        try:
            parcel, parcel_status = self._query_parcel(x, y, address)
        except Exception as e:
            if self.verbose:
                logger.warning("Arapahoe parcel query failed: %s", e)
            return {"status": "api_error", "address": address,
                    "error": str(e)}

        if not parcel:
            return {"status": parcel_status, "address": address,
                    "error": "no unique parcel matched the geocoded address"}

        parcel_id = parcel.get("PARCEL_ID") or parcel.get("PARCELID") or ""

        # Step 3: Spatial query Layer 277 for real owner (not metro district)
        owner_data = {}
        try:
            owner_data = self._query_owner(x, y)
        except Exception as e:
            if self.verbose:
                logger.warning("Arapahoe owner query failed: %s", e)

        # Step 4: Query Layer 286 for building data
        building = {}
        if parcel_id:
            try:
                building = self._query_building(parcel_id)
            except Exception as e:
                if self.verbose:
                    logger.warning("Arapahoe building query failed: %s", e)

        # Step 5: Query Layer 167 for legal description (try PARCEL_ID then spatial)
        legal_desc = ""
        if parcel_id:
            try:
                improvements = self._query_improvements(parcel_id)
                legal_desc = improvements.get("Legal_Desc") or ""
            except Exception as e:
                if self.verbose:
                    logger.warning("Arapahoe improvements query (by ID) failed: %s", e)
        if not legal_desc:
            try:
                improvements = self._query_improvements_spatial(x, y)
                legal_desc = improvements.get("Legal_Desc") or ""
            except Exception as e:
                if self.verbose:
                    logger.warning("Arapahoe improvements query (spatial) failed: %s", e)

        # Step 6: Spatial query Layer 151 for subdivision name
        subdivision = ""
        try:
            subdiv_attrs = self._query_subdivision(x, y)
            subdiv_name = subdiv_attrs.get("SUBDIVISION") or ""
            subdiv_filing = subdiv_attrs.get("FILING") or ""
            if subdiv_name:
                subdivision = subdiv_name
                if subdiv_filing:
                    subdivision += f" {subdiv_filing}"
        except Exception as e:
            if self.verbose:
                logger.warning("Arapahoe subdivision query failed: %s", e)

        try:
            return self._compose(
                parcel=parcel,
                building=building,
                legal_desc=legal_desc,
                subdivision=subdivision,
                owner_data=owner_data,
                address=address,
            )
        except Exception as e:  # noqa: BLE001
            return {"status": "parse_error", "address": address,
                    "error": str(e)}

    def lookup_by_parcel(self, parcel_id, **kwargs):
        """Look up a property by PARCEL_ID directly (no geocoding).

        Skips Layer 277 spatial owner query and the subdivision spatial
        query (those need coordinates), but populates everything that
        can be derived from the parcel-id keyed layers (276, 286, 167).
        """
        parcel_id = (parcel_id or "").strip()
        if not parcel_id:
            return {"status": "invalid_parcel", "parcel_id": parcel_id}

        # Layer 276: parcel record (owner, value, address)
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

        # Layer 286: building data
        building = {}
        try:
            building = self._query_building(parcel_id)
        except Exception as e:
            if self.verbose:
                logger.warning("Arapahoe building query failed: %s", e)

        # Layer 167: legal description
        legal_desc = ""
        try:
            improvements = self._query_improvements(parcel_id)
            legal_desc = improvements.get("Legal_Desc") or ""
        except Exception as e:
            if self.verbose:
                logger.warning("Arapahoe improvements query failed: %s", e)

        try:
            return self._compose(
                parcel=parcel,
                building=building,
                legal_desc=legal_desc,
                subdivision="",  # spatial-only, not available from parcel id
                owner_data={},
                address=parcel.get("Situs_Address", "").strip() or "",
            )
        except Exception as e:  # noqa: BLE001
            return {"status": "parse_error", "parcel_id": parcel_id,
                    "error": str(e)}

    def _fetch_parcel_by_id(self, parcel_id):
        """Query Layer 276 directly by PARCEL_ID. Return attributes dict."""
        result = _arcgis_get(PARCEL_LAYER_URL, params={
            "where": f"PARCEL_ID='{parcel_id}'",
            "outFields": "*",
            "returnGeometry": "false",
            "f": "json",
        }, timeout=self.timeout)
        features = result.get("features", [])
        if features:
            candidate, _ = select_candidate(features, parcel=parcel_id)
            if candidate:
                return candidate.get("attributes", candidate)
        return None

    def _compose(self, parcel, building, legal_desc, subdivision, owner_data, address):
        """Build the standardized result dict from queried layer data.

        Shared by `lookup()` (spatial path) and `lookup_by_parcel()`.
        """
        above_grade = _safe_int(building.get("Heated_Area") or building.get("HEATED_AREA"))
        basement_sqft = _safe_int(building.get("Basement") or building.get("BSMT_AREA"))
        finished_bsmt = _safe_int(building.get("Basement_Fin") or building.get("BSMT_FIN_AREA"))
        year_built = _safe_int(building.get("Yr_Built") or building.get("YEAR_BUILT"))
        style = building.get("Bld_Style") or building.get("BLDG_STYLE") or ""
        neighborhood = building.get("NBHD_Code") or building.get("NEIGHBORHOOD_CODE") or ""

        # Lot size: parcel polygon area from Layer 276
        lot_size_sqft = None
        sa = parcel.get("Shape_Area") or parcel.get("Shape__Area")
        if sa is not None:
            try:
                lot_size_sqft = int(round(float(sa)))
            except (ValueError, TypeError):
                pass

        # Garage area from Layer 286 (sqft, not space count)
        garage_area_sqft = _safe_int(building.get("Att_Garage"))

        owner = (owner_data.get("Owner") or owner_data.get("OWNER_NAME")
                 or parcel.get("Owner") or parcel.get("OWNER_NAME") or "")
        market_value = (parcel.get("Appr_Value") or parcel.get("Market_Val")
                        or parcel.get("APPRAISED_VALUE") or "")
        assessed_value = parcel.get("Assd_Value") or parcel.get("ASSESSED_VALUE") or ""
        parcel_number = parcel.get("PARCEL_ID") or ""
        if not neighborhood:
            neighborhood = parcel.get("Neighborhood") or ""

        # Derive map reference from parcel ID (first 3 segments: 2071-33-4)
        map_ref = ""
        if parcel_number:
            parts = parcel_number.split("-")
            if len(parts) >= 3:
                map_ref = "-".join(parts[:3])

        assessor_url = ""
        if parcel_number:
            assessor_url = (
                f"https://gis.arapahoegov.com/arapamap/?find="
                f"{urllib.parse.quote(parcel_number)}"
            )

        return {
            "status": "success",
            "address": address,
            "property_id": parcel_number,
            "assessor_url": assessor_url,
            "above_grade_sqft": above_grade,
            "basement_sqft": basement_sqft,
            "finished_basement_sqft": finished_bsmt,
            "year_built": year_built,
            "beds": None,
            "baths": None,
            "market_value": str(market_value) if market_value else "",
            "style": style,
            "owner": owner,
            "legal": legal_desc,
            "parcel_number": parcel_number,
            "tax_amount": "",
            "tax_year": "",
            "assessed_value": str(assessed_value) if assessed_value else "",
            "neighborhood": subdivision or neighborhood,
            "map_ref": map_ref,
            "lot_size_sqft": lot_size_sqft,
            "garage_area_sqft": garage_area_sqft,
        }

    def _geocode(self, address):
        """Geocode an address, returning ``((x, y), status)``."""
        result = _arcgis_get(GEOCODE_URL, params={
            "SingleLine": address,
            "f": "json",
            "outSR": "4326",
            "maxLocations": "5",
        }, timeout=self.timeout)

        candidates = [c for c in result.get("candidates", [])
                      if c.get("score", 0) >= 80]
        candidate, status = select_candidate(candidates, address=address)
        if candidate:
            loc = candidate.get("location", {})
            x = loc.get("x")
            y = loc.get("y")
            if x is not None and y is not None:
                return (x, y), "success"
        return None, status

    def _query_parcel(self, x, y, address=""):
        """Spatial query Layer 276, returning ``(attributes, status)``.

        Uses a small envelope (~50m buffer) around the point to handle
        geocoding imprecision where the point lands on a road.
        """
        buf = 0.0005  # ~50 meters in degrees
        envelope = f"{x - buf},{y - buf},{x + buf},{y + buf}"
        result = _arcgis_get(PARCEL_LAYER_URL, params={
            "geometry": envelope,
            "geometryType": "esriGeometryEnvelope",
            "spatialRel": "esriSpatialRelIntersects",
            "inSR": "4326",
            "outFields": "*",
            "returnGeometry": "false",
            "f": "json",
        }, timeout=self.timeout)

        features = result.get("features", [])
        if features:
            candidate, status = select_candidate(features, address=address)
            if candidate:
                return candidate.get("attributes", candidate), "success"
            return {}, status
        return {}, "not_found"

    def _query_building(self, parcel_id):
        """Query Layer 286 for building data by PARCEL_ID. Return attributes dict."""
        result = _arcgis_get(BUILDING_LAYER_URL, params={
            "where": f"PARCEL_ID='{parcel_id}'",
            "outFields": "PARCEL_ID,Yr_Built,Heated_Area,Basement,Basement_Fin,"
                         "Att_Garage,Quality,Bld_Style,NBHD_Code,"
                         "Market_Val,Sale_Date,Sale_Price",
            "returnGeometry": "false",
            "f": "json",
        }, timeout=self.timeout)

        features = result.get("features", [])
        if features:
            return features[0].get("attributes", {})
        return {}

    def _query_owner(self, x, y):
        """Spatial query Layer 277 for actual owner at (x, y)."""
        buf = 0.0005
        envelope = f"{x - buf},{y - buf},{x + buf},{y + buf}"
        result = _arcgis_get(OWNER_LAYER_URL, params={
            "geometry": envelope,
            "geometryType": "esriGeometryEnvelope",
            "spatialRel": "esriSpatialRelIntersects",
            "inSR": "4326",
            "outFields": "*",
            "returnGeometry": "false",
            "f": "json",
        }, timeout=self.timeout)

        features = result.get("features", [])
        if features:
            return features[0].get("attributes", {})
        return {}

    def _query_improvements(self, parcel_id):
        """Query Layer 167 for legal description by PARCEL_ID."""
        result = _arcgis_get(IMPROVEMENTS_LAYER_URL, params={
            "where": f"PARCEL_ID='{parcel_id}'",
            "outFields": "PARCEL_ID,Legal_Desc,Plat_Override,AIN",
            "returnGeometry": "false",
            "f": "json",
        }, timeout=self.timeout)

        features = result.get("features", [])
        if features:
            return features[0].get("attributes", {})
        return {}

    def _query_improvements_spatial(self, x, y):
        """Spatial query Layer 167 for legal description at (x, y)."""
        buf = 0.0005
        envelope = f"{x - buf},{y - buf},{x + buf},{y + buf}"
        result = _arcgis_get(IMPROVEMENTS_LAYER_URL, params={
            "geometry": envelope,
            "geometryType": "esriGeometryEnvelope",
            "spatialRel": "esriSpatialRelIntersects",
            "inSR": "4326",
            "outFields": "PARCEL_ID,Legal_Desc,Plat_Override,AIN",
            "returnGeometry": "false",
            "f": "json",
        }, timeout=self.timeout)

        features = result.get("features", [])
        if features:
            return features[0].get("attributes", {})
        return {}

    def _query_subdivision(self, x, y):
        """Spatial query Layer 151 for subdivision at (x, y)."""
        buf = 0.0005
        envelope = f"{x - buf},{y - buf},{x + buf},{y + buf}"
        result = _arcgis_get(SUBDIVISION_LAYER_URL, params={
            "geometry": envelope,
            "geometryType": "esriGeometryEnvelope",
            "spatialRel": "esriSpatialRelIntersects",
            "inSR": "4326",
            "outFields": "SUBDIVISION,FILING",
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
