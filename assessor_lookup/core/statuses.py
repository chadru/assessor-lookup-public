"""Status values every assessor client may return in its result dict.

Clients never raise on a failed lookup; they return a dict whose ``status``
is one of these values. Callers branch on ``status``, not exceptions.
"""

SUCCESS = "success"
NOT_FOUND = "not_found"
AMBIGUOUS = "ambiguous"
TIMEOUT = "timeout"
API_ERROR = "api_error"
PARSE_ERROR = "parse_error"
INVALID_ADDRESS = "invalid_address"
INVALID_PARCEL = "invalid_parcel"

ALL_STATUSES = {
    SUCCESS,
    NOT_FOUND,
    AMBIGUOUS,
    TIMEOUT,
    API_ERROR,
    PARSE_ERROR,
    INVALID_ADDRESS,
    INVALID_PARCEL,
}
