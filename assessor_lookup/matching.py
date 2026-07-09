"""Deterministic candidate selection shared by assessor clients."""

import re


_ADDRESS_FIELDS = {
    "address", "fulladdress", "propertyaddress", "siteaddress",
    "situsaddress", "situs_address", "situsadd", "concataddr1", "propaddr",
    "unparsedaddress", "locationaddress", "suggest",
}
_PARCEL_FIELDS = {
    "parcel", "parcelid", "parcel_id", "parcelidentifier", "parcelnumber",
    "parcelnb", "par", "pin", "account", "accountnumber", "accountnum",
    "uniquepropertyid",
}
_SUFFIXES = {
    "STREET": "ST", "ST": "ST", "ROAD": "RD", "RD": "RD",
    "AVENUE": "AVE", "AVE": "AVE", "DRIVE": "DR", "DR": "DR",
    "COURT": "CT", "CT": "CT", "LANE": "LN", "LN": "LN",
    "PLACE": "PL", "PL": "PL", "CIRCLE": "CIR", "CIR": "CIR",
    "BOULEVARD": "BLVD", "BLVD": "BLVD", "PARKWAY": "PKWY",
    "PKWY": "PKWY", "TRAIL": "TRL", "TRL": "TRL",
    "HIGHWAY": "HWY", "HWY": "HWY", "TERRACE": "TER", "TER": "TER",
    "WAY": "WAY", "LOOP": "LOOP",
}


def normalize_address(value):
    """Normalize a street address for conservative equality checks."""
    street = str(value or "").split(",", 1)[0].upper()
    street = re.sub(r"#\s*([A-Z0-9-]+)", r" UNIT \1", street)
    tokens = re.findall(r"[A-Z0-9]+", street)
    normalized = []
    for token in tokens:
        if token in {"APARTMENT", "APT", "SUITE", "STE", "UNIT"}:
            normalized.append("UNIT")
        else:
            normalized.append(_SUFFIXES.get(token, token))
    return " ".join(normalized)


def normalize_identifier(value):
    """Normalize parcel/account identifiers without weakening identity."""
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def _attributes(candidate):
    if not isinstance(candidate, dict):
        return {}
    attrs = candidate.get("attributes")
    return attrs if isinstance(attrs, dict) else candidate


def _values(candidate, fields):
    attrs = _attributes(candidate)
    return [value for key, value in attrs.items()
            if str(key).lower().replace(" ", "").replace("-", "") in fields
            and value not in (None, "")]


def select_candidate(candidates, *, address="", parcel=""):
    """Return ``(candidate, status)`` for an exact or safely unique match.

    A sole candidate may be accepted when the upstream response exposes no
    comparable identity. If identity is exposed, even a sole candidate must
    match. Multiple candidates require exactly one explicit match.
    """
    usable = [candidate for candidate in candidates if isinstance(candidate, dict)]
    if not usable:
        return None, "not_found"

    query_address = normalize_address(address)
    query_parcel = normalize_identifier(parcel)
    matches = []
    candidates_with_identity = 0
    for candidate in usable:
        address_values = _values(candidate, _ADDRESS_FIELDS)
        parcel_values = _values(candidate, _PARCEL_FIELDS)
        comparable = address_values if query_address else parcel_values
        if comparable:
            candidates_with_identity += 1
        if query_address and any(normalize_address(v) == query_address
                                 for v in address_values):
            matches.append(candidate)
        elif query_parcel and any(normalize_identifier(v) == query_parcel
                                  for v in parcel_values):
            matches.append(candidate)

    if len(matches) == 1:
        return matches[0], "success"
    if len(matches) > 1:
        return None, "ambiguous"
    if len(usable) == 1 and not candidates_with_identity:
        return usable[0], "success"
    return None, "ambiguous" if len(usable) > 1 else "not_found"
