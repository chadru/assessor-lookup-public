"""Colorado statewide public-parcel API client (ArcGIS FeatureServer).

This is the tier-2 *baseline* source used by auto-discovery when a county has
no dedicated assessor client. It is a single statewide ArcGIS layer that covers
~40 Colorado counties and returns clean JSON — but only the parcel/owner/legal/
value/land attributes. It has **no building characteristics** (GLA, beds, baths,
year built, basement), so those come back as None and the discrepancy check
shows them as N/A. Its assessed/actual values also lag the county's live system,
so treat them as approximate.

For full building data a county needs a real client (Spatialest, EagleWeb, ...).
"""

import urllib.error

from ....core.matching import select_candidate
from ....platforms import arcgis

_ALLOWED_HOSTS = ("gis.colorado.gov",)

# Colorado_Public_Parcel_Composite, layer 0
QUERY_URL = ("https://gis.colorado.gov/public/rest/services/Address_and_Parcel/"
             "Colorado_Public_Parcels/FeatureServer/0/query")

# Fields we lift out of the layer (see module docstring for the full schema).
_OUT_FIELDS = ("countyName,parcel_id,account,situsAdd,sitAddCty,sitAddZip,owner,"
               "owner2,legalDesc,landSqft,landAcres,subName,zoningDesc,saleDate,"
               "salePrice,apprValTot,asedValTot,URL")

_NOTE = ("Baseline parcel API (CO statewide) — no building data (GLA/beds/baths/"
         "year); values approximate. Add a county client for full records.")


_esc = arcgis.escape_sql_literal


class CoParcelClient:
    """Baseline client backed by the CO statewide public-parcel layer."""

    capabilities = {"parcel_lookup": True, "building_fields": False}

    def __init__(self, county="", state="co", timeout=15, verbose=False):
        self.county = county
        self.state = state
        self.timeout = timeout
        self.verbose = verbose

    # -- API -------------------------------------------------------------
    def lookup(self, address, **kwargs):
        address = (address or "").strip()
        if not address:
            return {"status": "invalid_address", "address": address}
        tokens = address.split()
        house = tokens[0] if tokens and tokens[0][:1].isdigit() else ""
        # first alphabetic street token, for a lenient LIKE
        street = next((t for t in tokens[1:] if t[:1].isalpha()), "")
        like = f"{_esc(house)}%{_esc(street).upper()}%" if house else f"%{_esc(street).upper()}%"
        where = f"situsAdd LIKE '{like}'"
        if self.county:
            where += f" AND UPPER(countyName) LIKE '%{_esc(self.county).upper()}%'"
        return self._query(where, ident={"address": address})

    def lookup_by_parcel(self, parcel_id, **kwargs):
        parcel_id = (parcel_id or "").strip()
        if not parcel_id:
            return {"status": "invalid_parcel", "parcel_id": parcel_id}
        digits = "".join(ch for ch in parcel_id if ch.isalnum())
        where = (f"parcel_id='{_esc(parcel_id)}' OR parcel_id='{_esc(digits)}' "
                 f"OR account='{_esc(parcel_id.upper())}'")
        if self.county:
            where = (f"({where}) AND "
                     f"UPPER(countyName) LIKE '%{_esc(self.county).upper()}%'")
        return self._query(where, ident={"parcel_id": parcel_id})

    # -- internals -------------------------------------------------------
    def _query(self, where, ident):
        try:
            if self.verbose:
                print(f"  CO parcel API: {where}")
            data = arcgis.arcgis_get(QUERY_URL, _ALLOWED_HOSTS, params={
                "where": where, "outFields": _OUT_FIELDS,
                "resultRecordCount": "10", "f": "json",
            }, timeout=self.timeout)
        except urllib.error.URLError as e:
            status = "timeout" if "timed out" in str(e).lower() else "api_error"
            return {"status": status, "error": str(e), **ident}
        except Exception as e:  # noqa: BLE001
            return {"status": "api_error", "error": str(e), **ident}

        if not isinstance(data, dict):
            return {"status": "parse_error",
                    "error": "unexpected assessor response shape", **ident}
        if data.get("error"):
            return {"status": "api_error", "error": str(data["error"]), **ident}
        feats = data.get("features", [])
        if not feats:
            return {"status": "not_found", **ident}

        candidate, status = select_candidate(
            feats, address=ident.get("address", ""),
            parcel=ident.get("parcel_id", ""))
        if not candidate:
            return {"status": status, **ident}
        try:
            return self._to_record(candidate.get("attributes", candidate), ident)
        except Exception as e:  # noqa: BLE001
            return {"status": "parse_error", "error": str(e), **ident}

    def _to_record(self, a, ident):
        owner = a.get("owner") or ""
        if a.get("owner2"):
            owner = f"{owner}; {a['owner2']}".strip("; ")

        lot_sqft = a.get("landSqft")
        if not lot_sqft and a.get("landAcres"):
            try:
                lot_sqft = int(round(float(a["landAcres"]) * 43560))
            except (ValueError, TypeError):
                lot_sqft = None

        def money(v):
            try:
                return f"${float(str(v).replace(',', '')):,.0f}" if v not in (None, "") else ""
            except (ValueError, TypeError):
                return str(v or "")

        return {
            "status": "success",
            "address": ident.get("address") or a.get("situsAdd", ""),
            "situs_address": a.get("situsAdd", ""),
            "parcel_number": a.get("parcel_id") or a.get("account") or "",
            "account_number": a.get("account", ""),
            "owner": owner,
            "legal": a.get("legalDesc", ""),
            "neighborhood": a.get("subName", ""),
            "zoning": a.get("zoningDesc", ""),
            "lot_size_sqft": lot_sqft,
            "market_value": money(a.get("apprValTot")),
            "assessed_value": money(a.get("asedValTot")),
            "sale_price": money(a.get("salePrice")),
            "sale_date": a.get("saleDate", ""),
            "assessor_url": a.get("URL", ""),
            # No building data from this source:
            "above_grade_sqft": None,
            "basement_sqft": None,
            "finished_basement_sqft": None,
            "beds": None,
            "baths": None,
            "year_built": None,
            "style": "",
            "garage_area_sqft": None,
            "data_note": _NOTE,
        }


def build(entry, timeout=15, verbose=False):
    """Driver factory: the statewide layer serves many CO counties, so the
    county name comes from the entry's jurisdiction."""
    jur = entry["jurisdiction"]
    return CoParcelClient(county=jur["name"], state=jur["state"].lower(),
                          timeout=timeout, verbose=verbose)
