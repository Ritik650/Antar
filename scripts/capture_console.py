"""Screenshot the running console into `docs/img/`.

    python tasks.py console        # in one terminal
    python -m scripts.capture_console

A reviewer will not run the code. The README describes a trace paragraph that answers
*"why did you contact this customer at 11:04 on a Tuesday?"* and, until now, never showed
it — which asks the reader to take the most demonstrable thing in the project on trust.

**This captures the real page.** It drives a headless browser against a live Streamlit
process reading the real hash-chained ledger. It is not a mock-up, and it must not become
one: if the console cannot be reached, this script fails rather than producing an image
of something that did not render.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import antar

OUT = Path(antar.__file__).parent.parent / "docs" / "img"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:8599")
    parser.add_argument("--wait-ms", type=int, default=9000)
    args = parser.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright is not installed: pip install playwright && playwright install chromium")
        return 2

    OUT.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1400})
        try:
            page.goto(args.url, wait_until="networkidle", timeout=45_000)
        except Exception as exc:
            print(f"could not reach the console at {args.url}: {exc}")
            print("Start it first: python tasks.py console")
            browser.close()
            return 1

        # Streamlit renders progressively; the ledger scan is the slow part.
        page.wait_for_timeout(args.wait_ms)

        body = page.inner_text("body")
        if "Antar" not in body:
            print("the page loaded but does not look like the console; refusing to save")
            browser.close()
            return 1
        if "no ledger found" in body.lower():
            print(
                "the console reports no ledger. Run `python tasks.py simulate` first - a "
                "screenshot of an empty console is worse than none."
            )
            browser.close()
            return 1

        target = OUT / "console_trace.png"
        page.screenshot(path=str(target), full_page=True)
        print(f"wrote {target}")

        # The trace paragraph is the thing worth showing. Capture it as text too, so the
        # README can quote the exact words the console rendered rather than a paraphrase.
        marker = "The answer, in one paragraph"
        if marker in body:
            after = body.split(marker, 1)[1].strip()
            paragraph = after.split("\n\n")[0].strip()
            (OUT / "console_trace.txt").write_text(paragraph + "\n", encoding="utf-8")
            print(f"wrote {OUT / 'console_trace.txt'}")
        else:
            print("note: trace paragraph not found on the rendered page")

        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
