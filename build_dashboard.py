"""
Renders dashboard.html from web/data.json and web/template.html.

The split matters. web/data.json is the pipeline's output contract -- the only
thing the presentation layer reads -- and web/template.html is the presentation
layer. This script does one thing: inlines the former into the latter.

Inlining rather than fetching is deliberate. A page opened as a local file
(file://) cannot fetch() a sibling JSON file; the browser blocks it as a
cross-origin request. Since the whole point is that Frank can double-click
dashboard.html and see today's numbers, the data ships inside the page. The
architecture is still separated -- swap web/data.json and you get a different
dashboard, which is exactly how the demo build works -- it just doesn't depend
on being served over HTTP.

Usage:
  python3 build_dashboard.py
  python3 build_dashboard.py --data web/demo-data.json --out demo/index.html
"""

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

BASE = Path(__file__).resolve().parent
TEMPLATE = BASE / "web" / "template.html"
DEFAULT_DATA = BASE / "web" / "data.json"
DEFAULT_OUT = BASE / "dashboard.html"

PLACEHOLDER = "/*__DATA__*/ null"


def validate(template_text: str) -> list:
    """Catch the page bugs that a syntax check can't see.

    Both of these shipped once and cost most of the dashboard:

    A duplicate element id is legal HTML and silently wrong in JavaScript.
    ``getElementById`` returns the *first* match, so when a section anchor and an
    inner mount shared the id ``block``, the renderer called replaceChildren on
    the whole section, destroyed a chart's mount, and every section after it
    stopped rendering. Nothing warned -- not the HTML, not the browser, not a
    syntax check.

    A ``$("id")`` that names an element the markup doesn't have returns null and
    throws on first use, with the same blast radius.
    """
    markup = template_text.split("<script>")[0]
    script = template_text[template_text.index("<script>"):]

    problems = []

    ids = re.findall(r'id="([^"]+)"', markup)
    duplicates = [i for i, n in Counter(ids).items() if n > 1]
    if duplicates:
        problems.append(
            f"duplicate element id(s): {', '.join(sorted(duplicates))} — "
            "getElementById returns only the first, so one of them is unreachable")

    referenced = set(re.findall(r'\$\("([^"]+)"\)', script))
    missing = sorted(referenced - set(ids))
    if missing:
        problems.append(f"script references id(s) not in the markup: {', '.join(missing)}")

    return problems


def render(template_text: str, data: dict) -> str:
    if PLACEHOLDER not in template_text:
        raise ValueError(f"Template is missing the data placeholder: {PLACEHOLDER!r}")
    # </script> inside the JSON would close the script tag early; escaping the
    # slash keeps the JSON valid and the tag intact.
    payload = json.dumps(data, default=str).replace("</", "<\\/")
    return template_text.replace(PLACEHOLDER, payload)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", default=str(DEFAULT_DATA), help="Signals JSON to inline")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="Where to write the HTML")
    args = parser.parse_args()

    data_path, out_path = Path(args.data), Path(args.out)

    if not TEMPLATE.exists():
        print(f"Template not found: {TEMPLATE}", file=sys.stderr)
        sys.exit(1)
    if not data_path.exists():
        print(f"No signals file at {data_path} — run signals.py first.", file=sys.stderr)
        sys.exit(1)

    template_text = TEMPLATE.read_text()

    problems = validate(template_text)
    if problems:
        print("Template validation failed:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        sys.exit(1)

    data = json.loads(data_path.read_text())
    # Internal keys the pipeline passes between stages but the page never reads.
    data.pop("_activities", None)
    html = render(template_text, data)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html)

    readiness = (data.get("readiness") or {}).get("score")
    print(f"Dashboard written to {out_path} ({len(html) // 1024}KB)")
    print(f"  data through {data.get('meta', {}).get('data_through')} · readiness {readiness}")


if __name__ == "__main__":
    main()
