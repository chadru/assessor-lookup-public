"""Platform registration and dispatch.

A *platform* is vendor software serving many jurisdictions (Spatialest,
Tyler EagleWeb, Aumentum, Esri ArcGIS REST). Platform code carries zero
jurisdiction knowledge; jurisdiction specifics arrive via the normalized
registry entry's ``config`` (and, for bespoke ArcGIS flows, a driver module
under ``jurisdictions/``).

Adding a platform = one module + one PLATFORMS line.
"""

import importlib
import re
from typing import Protocol

_DRIVER_RE = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)*$")


class AssessorAdapter(Protocol):
    """Duck-typed contract every platform client satisfies.

    ``lookup_by_parcel`` is optional in practice — consult the client's
    ``capabilities`` dict rather than relying on ``hasattr``.
    """

    capabilities: dict

    def lookup(self, address, **context) -> dict: ...


def _spatialest(entry, verbose):
    from .spatialest import SpatialestClient
    jur = entry.get("jurisdiction", {})
    return SpatialestClient(
        timeout=10, verbose=verbose,
        county_slug=entry["config"].get("slug", "elpaso"),
        state=jur.get("state", "CO").lower())


def _eagleweb(entry, verbose):
    from .eagleweb import EagleWebClient
    return EagleWebClient(base=entry["config"].get("base"), timeout=30,
                          verbose=verbose)


def _aumentum(entry, verbose):
    from .aumentum import JeffcoClient
    return JeffcoClient(base=entry["config"].get("base"), timeout=30,
                        verbose=verbose)


def _arcgis(entry, verbose):
    # A driver "us.co.adams" is the module jurisdictions/us/co/adams.py;
    # each driver module exposes build(entry, timeout=, verbose=). This
    # importlib call is the ONE sanctioned exception to the one-way
    # dependency rule (jurisdictions import platforms, never the reverse):
    # it is the composition root that wires config to driver modules, and
    # stays lazy so no platform module ever depends on a jurisdiction.
    driver = entry["config"].get("driver", "")
    if not _DRIVER_RE.match(driver):
        raise KeyError(f"invalid arcgis driver {driver!r}")
    try:
        module = importlib.import_module(
            f"assessor_lookup.jurisdictions.{driver}")
    except ImportError as exc:
        raise KeyError(f"unknown arcgis driver {driver!r}") from exc
    return module.build(entry, timeout=15, verbose=verbose)


class _InvalidConfigClient:
    """Stand-in client for a registry entry that cannot be dispatched.

    Keeps the never-raise contract: the misconfiguration surfaces as an
    explicit api_error status on every lookup, distinguishable from a
    property that was genuinely not found.
    """

    capabilities = {"parcel_lookup": True, "building_fields": False}

    def __init__(self, message):
        self._message = message

    def _error(self):
        return {"status": "api_error", "error": self._message}

    def lookup(self, address, **context):
        return self._error()

    def lookup_by_parcel(self, parcel_id, **context):
        return self._error()


PLATFORMS = {
    "spatialest": _spatialest,
    "eagleweb": _eagleweb,
    "aumentum": _aumentum,
    "arcgis": _arcgis,
}


def build_client(entry, verbose=False):
    """Construct the platform client for a normalized registry entry.

    Raises KeyError for an unknown platform, or an invalid/unknown ArcGIS
    driver. checker._get_client converts these into an _InvalidConfigClient
    so misconfiguration never raises through the public lookup API.
    """
    factory = PLATFORMS[entry.get("platform", "spatialest")]
    return factory(entry, verbose)
