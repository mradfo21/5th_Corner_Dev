"""Parse the client JS with a real JS engine and report syntax errors.

There is no node on this box, but Playwright ships a Chromium, so use its
parser. A syntax error in standalone.js takes the whole client down silently:
the page still serves, the assets still 200, and nothing runs.
"""
import sys

from playwright.sync_api import sync_playwright

FILES = sys.argv[1:] or [
    "static/js/standalone.js",
    "static/js/reactor_renderer.js",
    "static/js/moments.js",
    "static/js/editor_graph.js",
]

CHECK = """
(src) => {
  try { new Function(src); return "OK"; }
  catch (e) { return e.name + ": " + e.message; }
}
"""


def main():
    bad = 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        for path in FILES:
            try:
                src = open(path, encoding="utf-8").read()
            except OSError as e:
                print(f"  SKIP {path}: {e}")
                continue
            res = page.evaluate(CHECK, src)
            if res == "OK":
                print(f"  OK   {path}")
            else:
                bad += 1
                print(f"  FAIL {path}: {res}")
        browser.close()
    print("\nall clean" if not bad else f"\n{bad} file(s) with syntax errors")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
