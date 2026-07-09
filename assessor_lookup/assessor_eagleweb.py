"""EagleWeb (Tyler Technologies) county assessor client.

Tyler's "EagleWeb / taxweb" is a session-based JSP app used by many smaller
counties (a lot of rural Colorado counties, plus others nationwide). Unlike the
JSON platforms, there is no API: we drive the public "guest" search the same way
a browser does and parse the resulting HTML.

Flow (all under one ``base`` like
``https://assessor.co.clear-creek.co.us/eagleassessor``):

1. GET  ``/web/``                     -> establish a JSESSIONID cookie
2. POST ``/web/loginPOST.jsp``        -> guest login (submit=Login, guest=true)
3. POST ``/taxweb/results.jsp``       -> address / parcel / account search
4. GET  ``/taxweb/account.jsp?accountNum=...``            -> owner/legal/values
5. GET  ``/taxweb/account.jsp?accountNum=...&doc=DOC...`` -> building detail

The registry entry supplies the ``base`` URL, e.g.::

    "CO:Clear Creek": {"platform": "eagleweb",
                        "base": "https://assessor.co.clear-creek.co.us/eagleassessor",
                        "state": "co"}
"""

import re
import urllib.error
import urllib.parse
import urllib.request

from .network import build_https_opener, require_https_url
import http.cookiejar

from .matching import normalize_address, normalize_identifier

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# Street-type designations EagleWeb stores separately from the street name.
_DESIGNATIONS = {
    "ALY", "ALLEY", "AVE", "AVENUE", "BLVD", "BOULEVARD", "CIR", "CIRCLE",
    "CT", "COURT", "DR", "DRIVE", "HWY", "HIGHWAY", "LN", "LANE", "LOOP",
    "PKWY", "PARKWAY", "PL", "PLACE", "PATH", "RD", "ROAD", "SQ", "SQUARE",
    "ST", "STREET", "TER", "TERRACE", "TRL", "TRAIL", "WAY",
}
_DIRECTIONS = {"N", "S", "E", "W", "NE", "NW", "SE", "SW"}


def _split_address(address):
    """Split "1526 W Colorado Blvd" into (house_number, street_name).

    EagleWeb indexes the street *name* only ("COLORADO"), with the leading
    house number, any directional, and the trailing designation stored in
    separate columns. We pass house number + street name (a "starts with"
    match), which is selective enough without over-constraining.
    """
    address = (address or "").strip()
    if not address:
        return "", ""
    tokens = address.split()
    house = ""
    if tokens and re.match(r"^\d+[A-Za-z]?$", tokens[0]):
        house = tokens[0]
        tokens = tokens[1:]
    # drop a leading directional (N, W, ...)
    if tokens and tokens[0].upper().strip(".") in _DIRECTIONS:
        tokens = tokens[1:]
    # drop a trailing designation (Blvd, St, ...) and trailing directional
    while tokens and (tokens[-1].upper().strip(".") in _DESIGNATIONS
                      or tokens[-1].upper().strip(".") in _DIRECTIONS):
        tokens = tokens[:-1]
    return house, " ".join(tokens).strip()


def _clean(text):
    """Collapse whitespace / &nbsp; and strip."""
    if text is None:
        return ""
    text = text.replace("&nbsp;", " ").replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def _to_int(text):
    if text is None:
        return None
    m = re.search(r"-?\d[\d,]*", str(text))
    if not m:
        return None
    try:
        return int(m.group(0).replace(",", ""))
    except ValueError:
        return None


def _to_float(text):
    if text is None:
        return None
    m = re.search(r"-?\d[\d,]*\.?\d*", str(text))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


class EagleWebClient:
    """Client for Tyler EagleWeb / taxweb public assessor search."""

    def __init__(self, base=None, timeout=30, verbose=False):
        # base ends at the app root, e.g. ".../eagleassessor"
        self.base = (base or "").rstrip("/")
        if self.base:
            self.base = require_https_url(self.base)
        self._allowed_hosts = ((urllib.parse.urlsplit(self.base).hostname,)
                               if self.base else ())
        self.timeout = timeout
        self.verbose = verbose
        self._jar = http.cookiejar.CookieJar()
        self._opener = build_https_opener(
            self._allowed_hosts, urllib.request.HTTPCookieProcessor(self._jar))
        self._session_ready = False

    # -- HTTP helpers ----------------------------------------------------
    def _get(self, url):
        require_https_url(
            url, allowed_hosts=self._allowed_hosts, resolve_host=True,
        )
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with self._opener.open(req, timeout=self.timeout) as r:
            return r.read().decode("utf-8", "ignore")

    def _post(self, url, data):
        require_https_url(
            url, allowed_hosts=self._allowed_hosts, resolve_host=True,
        )
        body = urllib.parse.urlencode(data).encode()
        req = urllib.request.Request(
            url, data=body,
            headers={"User-Agent": _UA,
                     "Content-Type": "application/x-www-form-urlencoded"},
            method="POST")
        with self._opener.open(req, timeout=self.timeout) as r:
            return r.read().decode("utf-8", "ignore")

    def _ensure_session(self):
        if self._session_ready:
            return
        if not self.base:
            raise ValueError("EagleWebClient requires a 'base' URL")
        self._get(f"{self.base}/web/")
        self._post(f"{self.base}/web/loginPOST.jsp",
                   {"submit": "Login", "guest": "true"})
        self._session_ready = True

    # -- public API ------------------------------------------------------
    def lookup(self, address, **kwargs):
        """Look up a property by street address."""
        address = (address or "").strip()
        if not address:
            return {"status": "invalid_address", "address": address}
        house, street = _split_address(address)
        if not street:
            return {"status": "invalid_address", "address": address}
        payload = {"AllTypes": "ALL", "docTypeTotal": "4",
                   "SitusIDStreetName": street}
        if house:
            payload["SitusIDHouseNumber"] = house
        return self._search_and_fetch(payload, address=address)

    def lookup_by_parcel(self, parcel_id, **kwargs):
        """Look up by assessor account (R/M-number) or parcel/schedule number."""
        parcel_id = (parcel_id or "").strip()
        if not parcel_id:
            return {"status": "invalid_parcel", "parcel_id": parcel_id}
        payload = {"AllTypes": "ALL", "docTypeTotal": "4"}
        if re.match(r"^[A-Za-z]\d+$", parcel_id):
            payload["AccountNumID"] = parcel_id.upper()
        else:
            payload["ParcelNumberID"] = parcel_id
        return self._search_and_fetch(payload, parcel_id=parcel_id)

    # -- internals -------------------------------------------------------
    def _search_and_fetch(self, payload, address="", parcel_id=""):
        ident = {"address": address} if address else {"parcel_id": parcel_id}
        try:
            self._ensure_session()
            html = self._post(f"{self.base}/taxweb/results.jsp", payload)
        except urllib.error.URLError as e:
            status = "timeout" if "timed out" in str(e).lower() else "api_error"
            return {"status": status, "error": str(e), **ident}
        except Exception as e:  # noqa: BLE001
            return {"status": "api_error", "error": str(e), **ident}

        accounts = self._parse_result_accounts(html)
        if not accounts:
            return {"status": "not_found", **ident}
        if len(accounts) > 1:
            exact = []
            if parcel_id:
                wanted = normalize_identifier(parcel_id)
                exact = [acct for acct in accounts
                         if normalize_identifier(acct) == wanted]
            if len(exact) == 1:
                accounts = exact
            else:
                return {"status": "ambiguous",
                        "error": f"assessor returned {len(accounts)} candidates",
                        **ident}
        try:
            result = self._fetch_account(accounts[0], address=address)
            if address:
                returned = result.get("situs_address") or result.get("address")
                if (returned and normalize_address(returned)
                        != normalize_address(address)):
                    return {"status": "not_found", "address": address,
                            "error": "assessor returned a different address"}
            if parcel_id:
                wanted = normalize_identifier(parcel_id)
                identities = {
                    normalize_identifier(result.get("parcel_number")),
                    normalize_identifier(result.get("account_number")),
                }
                identities.discard("")
                if identities and wanted not in identities:
                    return {"status": "not_found", "parcel_id": parcel_id,
                            "error": "assessor returned a different parcel"}
            return result
        except urllib.error.URLError as e:
            status = "timeout" if "timed out" in str(e).lower() else "api_error"
            return {"status": status, "error": str(e), **ident}
        except Exception as e:  # noqa: BLE001
            return {"status": "api_error", "error": str(e), **ident}

    @staticmethod
    def _parse_result_accounts(html):
        """Return account numbers from a results page (order preserved).

        account.jsp?accountNum=R000001 may be the results list, or — when the
        search matched exactly one property — the page redirects straight to
        the account view, which also contains the accountNum in links.
        """
        seen, out = set(), []
        for acct in re.findall(r'accountNum=([A-Za-z0-9]+)', html):
            if acct not in seen:
                seen.add(acct)
                out.append(acct)
        return out

    def _fetch_account(self, acct, address=""):
        summary = self._get(
            f"{self.base}/taxweb/account.jsp?accountNum={acct}")
        rec = self._parse_summary(summary, acct)

        # Find the residential/improvement detail doc and parse building data.
        detail_doc = self._find_detail_doc(summary, acct)
        if detail_doc:
            detail = self._get(
                f"{self.base}/taxweb/account.jsp?accountNum={acct}&doc={detail_doc}")
            rec.update(self._parse_detail(detail))

        rec["status"] = "success"
        rec["account_number"] = acct
        rec.setdefault("address", address or rec.get("situs_address", ""))
        rec["assessor_url"] = (
            f"{self.base}/taxweb/account.jsp?accountNum={acct}")
        return rec

    @staticmethod
    def _find_detail_doc(summary_html, acct):
        """Pick the residential improvement detail doc id from the sidebar."""
        # links look like: account.jsp?accountNum=R000001&doc=DOC123.1">Residential
        links = re.findall(
            r'accountNum=' + re.escape(acct) + r'&doc=(DOC[0-9.]+)"[^>]*>\s*([^<]+)',
            summary_html)
        for doc, name in links:
            if re.search(r"resid|dwell|town|improv|manufact|mobile", name, re.I):
                return doc
        return None

    def _parse_summary(self, html, acct):
        c = re.sub(r">\s+<", "><", html)

        def strong(label):
            m = re.search(
                r"<(?:strong|b)>\s*" + re.escape(label) +
                r"\s*</(?:strong|b)>\s*([^<]*)", c)
            return _clean(m.group(1)) if m else ""

        def assessed(label):
            m = re.search(
                r"<(?:strong|b)>\s*" + re.escape(label) +
                r"\s*</(?:strong|b)>\s*</td>\s*<td[^>]*>\s*([^<]*)", c)
            return _clean(m.group(1)) if m else ""

        parcel_number = strong("Parcel Number")
        situs = strong("Situs Address")
        legal = strong("Legal Summary")
        owner = strong("Owner Name")

        market_value = ""
        m = re.search(r"<(?:strong|b)>\s*Actual\s*</(?:strong|b)>\s*"
                      r"(?:\([^)]*\))?\s*</td>\s*<td[^>]*>\s*([^<]*)", c)
        if m:
            market_value = _clean(m.group(1))

        school_assessed = _to_float(assessed("School Assessed"))
        nonschool_assessed = _to_float(assessed("Non-School Assessed"))

        def levy(label):
            m = re.search(r"<(?:strong|b)>\s*" + re.escape(label) +
                          r"\s*</(?:strong|b)>\s*:?\s*([\d.]+)", c)
            return _to_float(m.group(1)) if m else None

        school_levy = levy("Mill Levy School")
        nonschool_levy = levy("Mill Levy Non-School")

        # Colorado tax = assessed x mill levy / 1000, split school/non-school.
        tax_amount = ""
        parts = []
        if school_assessed and school_levy:
            parts.append(school_assessed * school_levy / 1000)
        if nonschool_assessed and nonschool_levy:
            parts.append(nonschool_assessed * nonschool_levy / 1000)
        if parts:
            tax_amount = f"${sum(parts):,.0f}"

        tax_year = ""
        m = re.search(r"Actual\s*</(?:strong|b)>\s*\((\d{4})\)", c)
        if m:
            tax_year = m.group(1)

        # subdivision / neighborhood out of the legal summary
        neighborhood = ""
        m = re.search(r"Subdivision:\s*([^<]+?)\s*(?:Block:|Lot:|<|$)", legal)
        if m:
            neighborhood = _clean(m.group(1))

        return {
            "parcel_number": parcel_number,
            "situs_address": _clean(situs.split("<br>")[0]),
            "legal": _clean(legal.replace("<br>", " ")),
            "owner": owner,
            "market_value": market_value,
            "assessed_value": (f"{int(nonschool_assessed):,}"
                               if nonschool_assessed else ""),
            "tax_amount": tax_amount,
            "tax_year": tax_year,
            "neighborhood": neighborhood,
        }

    def _parse_detail(self, html):
        c = re.sub(r">\s+<", "><", html)

        # label/value pairs inside the improvement panel
        pairs = {}
        for m in re.finditer(
                r'fieldLabel[^>]*>\s*([^<]+?)\s*</span>\s*<br\s*/?>\s*'
                r'<span class="field"[^>]*>\s*<span class="text"[^>]*>\s*([^<]*)',
                c):
            pairs[_clean(m.group(1))] = _clean(m.group(2))

        def geti(label):
            return _to_int(pairs.get(label))

        def getf(label):
            return _to_float(pairs.get(label))

        year_built = geti("Year Built")
        beds = geti("Bedrooms")
        baths = getf("Baths")
        style = pairs.get("Design") or pairs.get("Type") or ""

        # Areas / Abstract Code table -> square footage by line.
        above_grade = basement = garage = None
        area_table = ""
        m = re.search(r"Abstract Code.*?</table>", c, re.S)
        if m:
            area_table = m.group(0)
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", area_table, re.S):
            cells = [_clean(x) for x in re.findall(
                r'<span class="text"[^>]*>\s*([^<]*)', row)]
            if len(cells) < 5:
                continue
            code = cells[0].upper()
            sqft = _to_int(cells[4])  # column order: code,%,override,acres,sqft,units
            if not sqft:
                continue
            if "BASEMENT" in code or "BSMT" in code:
                basement = (basement or 0) + sqft
            elif "GARAGE" in code:
                garage = (garage or 0) + sqft
            elif "RES" in code or "IMPROV" in code or "DWELL" in code:
                above_grade = (above_grade or 0) + sqft

        return {
            "above_grade_sqft": above_grade,
            "basement_sqft": basement,
            "finished_basement_sqft": None,
            "garage_area_sqft": garage,
            "year_built": year_built,
            "beds": beds,
            "baths": baths,
            "style": _clean(style),
        }
