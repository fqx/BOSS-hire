"""
Test whether zendriver's find()/find_all() recursively searches nested iframes.

Structure:
  outer.html
    -> middle.html  (iframe in outer)
      -> inner.html  (iframe in middle = nested iframe)

We try to find elements in all three levels using tab.find() and tab.find_all().
"""

import asyncio
import os
import zendriver as zd

HTML_DIR = os.path.dirname(os.path.abspath(__file__))
OUTER_URL = f"file://{HTML_DIR}/outer.html"


async def main():
    browser = await zd.start(headless=False)
    tab = await browser.get(OUTER_URL)

    # Give iframes time to load
    await asyncio.sleep(1)

    print("=" * 60)
    print(f"Page URL: {OUTER_URL}")
    print("=" * 60)

    # --- Test 1: find() by text content ---
    print("\n[Test 1] tab.find() by text (returns first match)")

    for label, text in [
        ("outer", "I am in the OUTER frame"),
        ("middle", "I am in the MIDDLE frame"),
        ("inner (nested)", "I am in the INNER frame (nested iframe)"),
    ]:
        try:
            el = await tab.find(text, timeout=3)
            print(f"  FOUND {label}: {el.text!r}")
        except Exception as e:
            print(f"  NOT FOUND {label}: {e}")

    # --- Test 2: find_all() by tag ---
    print("\n[Test 2] tab.find_all('p') - how many <p> elements found?")
    try:
        elements = await tab.find_all("p")
        print(f"  Found {len(elements)} <p> elements:")
        for el in elements:
            print(f"    - {el.text!r}")
    except Exception as e:
        print(f"  Error: {e}")

    # --- Test 3: query_selector_all (no iframe recursion expected) ---
    print("\n[Test 3] tab.query_selector_all('p') via DOM (no iframe recursion)")
    try:
        count = await tab.evaluate("document.querySelectorAll('p').length")
        print(f"  document.querySelectorAll('p').length = {count}")
    except Exception as e:
        print(f"  Error: {e}")

    # --- Test 4: find by id ---
    print("\n[Test 4] tab.find() by element id")
    for el_id in ["outer-text", "middle-text", "inner-text", "inner-btn"]:
        try:
            el = await tab.find(el_id, timeout=3)
            print(f"  FOUND #{el_id}")
        except Exception as e:
            print(f"  NOT FOUND #{el_id}: {type(e).__name__}")

    print("\n" + "=" * 60)
    print("Done. Check results above.")
    print("=" * 60)

    await asyncio.sleep(3)
    await browser.stop()


asyncio.run(main())
