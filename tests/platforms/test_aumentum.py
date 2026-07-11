"""Tests for platforms/aumentum.py (Aumentum platform client)."""

from unittest.mock import patch

from assessor_lookup.platforms.aumentum import JeffcoClient


class TestJeffcoMapping:
    def test_parse_legal_includes_subdivision_and_quarter(self):
        client = JeffcoClient()
        legal = client._parse_legal(
            {
                "block": "",
                "lot": "0011",
                "section": "24",
                "township": "03",
                "range": "69",
                "quarterSection": "NW",
                "landSquareFeet": 9669.0,
                "landAcres": 0.222,
            },
            subdivision="136800 CLUB CORNER",
        )
        assert legal == (
            "SECTION 24 TOWNSHIP 03 RANGE 69 QTR NW SUBDIVISIONCD 136800 "
            "SUBDIVISIONNAME CLUB CORNER BLOCK LOT 0011 SIZE: 9669 "
            "TRACT VALUE: .222"
        )

    @patch.object(JeffcoClient, "_get_endpoint")
    def test_fetch_details_prefers_ain_and_subdivision_name(self, mock_get):
        mock_get.side_effect = [
            {
                "propertyDetails": {
                    "pin": "300023827",
                    "ain": "39-242-22-014",
                    "displayName": "SAMPLE OWNER",
                    "neighborhood": "2106 CROWNHILL/WHEAT RIDGE",
                    "subdivision": "136800 CLUB CORNER",
                    "propertyAddress": "123 EXAMPLE RD ",
                }
            },
            {
                "inventorySummaryDetails": {
                    "bld1Design": "1 Story/Ranch",
                    "bld1YearBuilt": 1951,
                    "totalAboveGradeArea": 848,
                    "totalBasementArea": 0,
                    "totalGardenLevelArea": 0,
                    "totalAcres": 0.222,
                }
            },
            {
                "valueDetailsList": [{
                    "taxYear": "2025 payable 2026",
                    "totalActual": 519896.0,
                    "totalAssessed": 32493.0,
                }]
            },
            {
                "legalDescriptionDetailsList": [{
                    "lot": "0011",
                    "section": "24",
                    "township": "03",
                    "range": "69",
                    "quarterSection": "NW",
                    "landSquareFeet": 9669.0,
                    "landAcres": 0.222,
                }]
            },
            {
                "authorityMillLevyDetailsList": [{
                    "totalMillLevy": "88.2390",
                }]
            },
        ]

        client = JeffcoClient()
        result = client._fetch_details("uid-1", address="")

        assert result["parcel_number"] == "39-242-22-014"
        assert result["neighborhood"] == "CLUB CORNER"
        assert result["address"] == "123 Example Rd"


