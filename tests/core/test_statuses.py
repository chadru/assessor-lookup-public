"""Tests for the assessor_lookup.core package (statuses, moved shared modules)."""

import pytest


class TestStatuses:
    def test_status_constants_match_wire_values(self):
        from assessor_lookup.core import statuses

        assert statuses.SUCCESS == "success"
        assert statuses.NOT_FOUND == "not_found"
        assert statuses.TIMEOUT == "timeout"
        assert statuses.API_ERROR == "api_error"
        assert statuses.PARSE_ERROR == "parse_error"
        assert statuses.INVALID_ADDRESS == "invalid_address"
        assert statuses.AMBIGUOUS == "ambiguous"
        assert statuses.INVALID_PARCEL == "invalid_parcel"

    def test_all_statuses_is_the_closed_set_clients_may_return(self):
        from assessor_lookup.core.statuses import ALL_STATUSES

        assert ALL_STATUSES == {
            "success",
            "not_found",
            "ambiguous",
            "timeout",
            "api_error",
            "parse_error",
            "invalid_address",
            "invalid_parcel",
        }


class TestCoreLayout:
    def test_matching_lives_under_core(self):
        from assessor_lookup.core.matching import select_candidate  # noqa: F401

    def test_network_lives_under_core(self):
        from assessor_lookup.core.network import require_https_url  # noqa: F401
