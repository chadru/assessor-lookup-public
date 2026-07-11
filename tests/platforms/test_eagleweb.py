"""Tests for platforms/eagleweb.py (Tyler EagleWeb scraper)."""

from unittest.mock import patch

import pytest


class TestEagleWebClient:
    """Tyler EagleWeb HTML-scraping client (Clear Creek CO)."""

    SUMMARY_HTML = """
      <td><strong>Parcel Number</strong> 0000-000-00-001</td>
      <td><strong>Tax Area Id</strong> Test District - 001</td>
      <td><strong>Situs Address</strong> 123 MAIN ST</td>
      <td><strong>Legal Summary</strong> Subdivision: TEST Block: 1 Lot: 1</td>
      <td><b>Owner Name</b> SAMPLE OWNER</td>
      <td><b>Owner Address</b> PO BOX 1 <br>TEST, CO 80000</td>
      <td align="left"><b>Actual</b> (2026)</td><td align="right">$658,180</td>
      <td align="left"><b>School Assessed</b></td><td align="right">$46,400</td>
      <td align="left"><b>Non-School Assessed</b></td><td align="right">$44,760</td>
      <caption><b>Mill Levy School</b>:25.785 <b>Mill Levy Non-School</b>:44.576</caption>
      <a href="account.jsp?accountNum=R000001&doc=DOC100.1">Land</a>
      <a href="account.jsp?accountNum=R000001&doc=DOC101.1">Residential</a>
    """

    DETAIL_HTML = """
      <span class="fieldLabel">Year Built</span><br/>
        <span class="field"><span class="text" >1910&nbsp;</span></span>
      <span class="fieldLabel">Design</span><br/>
        <span class="field"><span class="text" >DUPLEX&nbsp;</span></span>
      <span class="fieldLabel">Bedrooms</span><br/>
        <span class="field"><span class="text" >6&nbsp;</span></span>
      <span class="fieldLabel">Baths</span><br/>
        <span class="field"><span class="text" >3&nbsp;</span></span>
      <span class="fieldLabel">Type</span><br/>
        <span class="field"><span class="text" >TWO STORY&nbsp;</span></span>
      <h3>Abstract Code</h3><table>
        <tr><th>Abstract Code</th><th>Percent</th><th>Override Value</th>
            <th>Acres</th><th>Square Feet</th><th>Units</th></tr>
        <tr><td><span class="text">SINGLE FAM.RES-IMPROVEMTS&nbsp;</span></td>
            <td><span class="text" >100.0&nbsp;</span></td>
            <td><span class="text" >&nbsp;</span></td>
            <td><span class="text" >0&nbsp;</span></td>
            <td><span class="text" >3006&nbsp;</span></td>
            <td><span class="text" >0&nbsp;</span></td></tr>
      </table>
    """

    def _client(self):
        from assessor_lookup.platforms.eagleweb import EagleWebClient
        return EagleWebClient(base="https://example.test/eagleassessor")

    def test_split_address_strips_designation_and_direction(self):
        from assessor_lookup.platforms.eagleweb import _split_address
        assert _split_address("123 Main Blvd") == ("123", "Main")
        assert _split_address("123 W Colorado Blvd") == ("123", "Colorado")
        assert _split_address("400 Soda Creek Rd") == ("400", "Soda Creek")
        assert _split_address("Miner Street") == ("", "Miner")

    def test_parse_summary_fields(self):
        c = self._client()
        rec = c._parse_summary(self.SUMMARY_HTML, "R000001")
        assert rec["parcel_number"] == "0000-000-00-001"
        assert rec["owner"] == "SAMPLE OWNER"
        assert rec["situs_address"] == "123 MAIN ST"
        assert rec["market_value"] == "$658,180"
        assert rec["assessed_value"] == "44,760"
        assert rec["neighborhood"] == "TEST"
        # tax = 46400*25.785/1000 + 44760*44.576/1000 = 3191.64 -> $3,192
        assert rec["tax_amount"] == "$3,192"
        assert rec["tax_year"] == "2026"

    def test_parse_detail_building_fields(self):
        c = self._client()
        rec = c._parse_detail(self.DETAIL_HTML)
        assert rec["year_built"] == 1910
        assert rec["beds"] == 6          # not the two-column "TWO STORY" bleed
        assert rec["baths"] == 3.0
        assert rec["above_grade_sqft"] == 3006
        assert rec["style"] == "DUPLEX"

    def test_find_detail_doc_picks_residential(self):
        c = self._client()
        assert c._find_detail_doc(self.SUMMARY_HTML, "R000001") == "DOC101.1"

    def test_lookup_end_to_end_mocked(self):
        c = self._client()
        c._session_ready = True  # skip guest-login network calls
        results_html = ('<a href="account.jsp?accountNum=R000001">R000001</a>')
        with patch.object(c, "_post", return_value=results_html), \
             patch.object(c, "_get", side_effect=[self.SUMMARY_HTML,
                                                  self.DETAIL_HTML]):
            rec = c.lookup("123 Main St")
        assert rec["status"] == "success"
        assert rec["account_number"] == "R000001"
        assert rec["year_built"] == 1910
        assert rec["above_grade_sqft"] == 3006
        assert rec["owner"] == "SAMPLE OWNER"

    def test_lookup_not_found(self):
        c = self._client()
        c._session_ready = True
        with patch.object(c, "_post", return_value="<html>no matches</html>"):
            rec = c.lookup("99999 Nowhere Rd")
        assert rec["status"] == "not_found"


