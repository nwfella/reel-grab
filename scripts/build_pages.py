#!/usr/bin/env python3
"""Build the GitHub Pages artefact (docs/index.html) from reel_grab.py's PAGE string.

The published page is the SAME UI as the local one, plus a shim telling it to drive a local
engine instead of its own origin. Generating it here means the two can never drift, and
`--check` fails when the committed file is stale (scripts/verify_app.py runs that check).

    python scripts/build_pages.py            # write docs/index.html
    python scripts/build_pages.py --check    # exit 1 if it is out of date
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "index.html"
ANCHOR = "</div><script>"
SHIM = ("<script>window.RG_STANDALONE=true;"
        "window.RG_BASE='http://127.0.0.1:8730';</script>\n")


def load_page() -> str:
    spec = importlib.util.spec_from_file_location("reel_grab", ROOT / "reel_grab.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(mod)          # main() is guarded: no side effects
    return mod.PAGE


def build(page: str) -> str:
    if ANCHOR not in page:
        raise SystemExit(f"anchor not found in PAGE: {ANCHOR!r}")
    out = page.replace(ANCHOR, "</div>" + SHIM + "<script>", 1)
    if "RG_STANDALONE" not in out:
        raise SystemExit("shim missing from generated output")
    return out


def main() -> int:
    doc = build(load_page())
    if "--check" in sys.argv:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current != doc:
            print("docs/index.html is STALE -- run: python scripts/build_pages.py")
            return 1
        print("docs/index.html matches reel_grab.py PAGE")
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(doc, encoding="utf-8")
    (OUT.parent / ".nojekyll").write_text("", encoding="utf-8")
    print(f"wrote {OUT} ({len(doc)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
