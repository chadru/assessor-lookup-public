"""Render a county assessor property card to PDF.

Assessor sites (Spatialest, county ArcGIS pages, ...) are client-side SPAs
with no server-side "download record card" endpoint, so the only faithful way
to capture the card is to print the property page from a real browser.
Playwright's headless Chromium does that here.

Requires the optional dependency:  pip install assessor-lookup[card]
then once:  playwright install chromium
"""

from pathlib import Path


def render_assessor_card_pdf(assessor_url, out_path, timeout_s=60):
    """Print the assessor property page to a PDF at out_path.

    Raises on navigation/render failure; the caller decides whether that
    blocks the surrounding workflow.
    """
    url = (assessor_url or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError(f"Not an http(s) assessor URL: {assessor_url!r}")

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise ImportError(
            "The assessor-card PDF feature needs Playwright. Install with "
            "`pip install assessor-lookup[card]` then run "
            "`playwright install chromium`.") from e

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(url, wait_until="networkidle", timeout=timeout_s * 1000)
            # SPAs keep hydrating after networkidle — give photos/maps a beat
            page.wait_for_timeout(2000)
            page.emulate_media(media="print")
            page.pdf(path=str(out), format="Letter", print_background=True)
        finally:
            browser.close()
    return str(out)
