"""Shared Esri ArcGIS REST primitives.

Every ArcGIS-backed jurisdiction driver funnels its HTTP traffic through
``arcgis_get`` — it is the one mockable seam for tests, and the SSRF
boundary: callers must declare the hosts they are allowed to reach.
"""

import json
import urllib.parse
import urllib.request

from ..core.network import open_https, require_https_url

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}


def arcgis_get(url, allowed_hosts, params=None, timeout=15):
    """GET an ArcGIS REST endpoint and return parsed JSON."""
    url = require_https_url(url, allowed_hosts=allowed_hosts)
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers=_HEADERS, method="GET")
    with open_https(req, timeout=timeout, allowed_hosts=allowed_hosts) as resp:
        return json.loads(resp.read().decode("utf-8", "ignore"))


def escape_sql_literal(value):
    """Escape a value for an ArcGIS SQL string literal."""
    return str(value or "").replace("'", "''")
