"""Attach to an already-open Chrome and dump the visible review/answer DOM.

This is the supported sidestep around AJIO's Akamai block: the block is on the
automated headless fingerprint, not on a person reading the page. Connecting over
CDP to a Chrome you already started with your own profile is that person-session,
not a spawned stealth browser.

Playwright is **optional** and is **not** in ``requirements.txt``. Collect never
imports this module — a test asserts the extract helpers stay under ``scripts/``.

Windows — close every Chrome window first (otherwise a second start ignores the
flag), then::

    & "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" --remote-debugging-port=9222

Browse the product or thread, scroll the reviews/answers into view, then::

    .venv\\Scripts\\python.exe scripts\\manual_extract\\cdp_extract.py --source ajio
    .venv\\Scripts\\python.exe scripts\\manual_extract\\cdp_extract.py --source quora

The JSON lands in the matching ``data/manual/<dir>/`` file. After that, Collect
is done: ``python -m src.collect.manual`` should exit 0, and a collection run
picks the files up. Do not add more Collect code.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EXTRACT_DIR = Path(__file__).resolve().parent
DEFAULT_CDP = "http://127.0.0.1:9222"

Source = Literal["ajio", "quora"]

EXTRACT_FILES: dict[Source, str] = {
    "ajio": "ajio_extract.js",
    "quora": "quora_extract.js",
}
HOST_HINT: dict[Source, tuple[str, ...]] = {
    "ajio": ("ajio.com",),
    "quora": ("quora.com",),
}
DEST_DIRS: dict[Source, str] = {
    "ajio": "data/manual/ajio",
    "quora": "data/manual/quora",
}

_UNSAFE_NAME = re.compile(r"[^a-zA-Z0-9._-]+")


def extract_script(source: Source) -> str:
    path = EXTRACT_DIR / EXTRACT_FILES[source]
    return path.read_text(encoding="utf-8")


def matching_pages(pages: list[Any], source: Source) -> list[Any]:
    hints = HOST_HINT[source]
    matched = []
    for page in pages:
        try:
            url = page.url or ""
        except Exception:
            continue
        lowered = url.lower()
        if any(hint in lowered for hint in hints):
            matched.append(page)
    return matched


def slug_from_url(url: str, source: Source) -> str:
    if source == "ajio":
        match = re.search(r"/p/(\d{6,})", url)
        if match:
            return match.group(1)
    path = re.sub(r"^https?://[^/]+", "", url).strip("/")
    path = path.split("?")[0].replace("/", "-")[:60]
    slug = _UNSAFE_NAME.sub("-", path).strip("-") or "thread"
    return slug.lower()


def dump_path(dest_dir: Path, source: Source, url: str, stamp: str | None = None) -> Path:
    stamp = stamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return dest_dir / f"{source}-{slug_from_url(url, source)}-{stamp}.json"


def write_payload(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def evaluate_on_page(page: Any, source: Source) -> dict[str, Any]:
    """Run the shared IIFE in the already-open tab.

    Playwright's ``page.evaluate`` on a string is a function body on some
    versions and an expression on others. Wrapping the IIFE as
    ``() => { return <iife>; }`` works either way.
    """
    page.evaluate("window.__AJIO_EXTRACT_VIA__ = 'cdp'")
    script = extract_script(source).rstrip().rstrip(";")
    result = page.evaluate(f"() => {{ return {script}; }}")
    if not isinstance(result, dict):
        return {"documents": [], "warnings": [f"extractor returned {type(result).__name__}"]}
    result.setdefault("documents", [])
    result.setdefault("warnings", [])
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Dump visible AJIO/Quora DOM from an already-open Chrome via CDP."
    )
    parser.add_argument("--source", choices=tuple(EXTRACT_FILES), required=True)
    parser.add_argument("--cdp", default=DEFAULT_CDP, help="Chrome remote-debugging origin")
    parser.add_argument(
        "--dest",
        type=Path,
        default=None,
        help="directory to write JSON into; default is the production import dir",
    )
    parser.add_argument(
        "--url-contains",
        default=None,
        help="only extract tabs whose URL contains this substring",
    )
    args = parser.parse_args(argv)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(
            "Playwright is optional and is not a project dependency.\n"
            "  pip install playwright\n"
            "Then attach to Chrome you already started with --remote-debugging-port=9222,\n"
            "not `playwright install` + a spawned headless browser — that is the fingerprint\n"
            "Akamai blocks.",
            file=sys.stderr,
        )
        return 2

    source: Source = args.source
    dest = Path(args.dest) if args.dest else PROJECT_ROOT / DEST_DIRS[source]
    dest.mkdir(parents=True, exist_ok=True)

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(args.cdp)
            pages = []
            for context in browser.contexts:
                pages.extend(context.pages)
            matched = matching_pages(pages, source)
            if args.url_contains:
                needle = args.url_contains.lower()
                matched = [p for p in matched if needle in (p.url or "").lower()]
            if not matched:
                print(
                    f"No open tab matched {HOST_HINT[source]}. "
                    f"Connected to {args.cdp} but found {len(pages)} page(s). "
                    "Open the product or thread in that Chrome, scroll the reviews "
                    "or answers into view, and run this again.",
                    file=sys.stderr,
                )
                return 1

            written = 0
            empty = 0
            for page in matched:
                payload = evaluate_on_page(page, source)
                docs = payload.get("documents") or []
                warnings = payload.get("warnings") or []
                if not docs:
                    empty += 1
                    print(f"SKIP  {page.url}  — no documents ({'; '.join(warnings) or 'empty DOM'})")
                    continue
                path = dump_path(dest, source, page.url or "page")
                write_payload(path, payload)
                written += 1
                print(f"WROTE {path.name}  {len(docs)} document(s)  {page.url}")
                for warning in warnings:
                    print(f"  warning: {warning}")
    except Exception as exc:
        print(
            f"Could not attach to Chrome at {args.cdp}: {exc}\n"
            "Close every Chrome window, start it with --remote-debugging-port=9222, "
            "then browse the page in that window. Do not spawn a new Chromium from Playwright.",
            file=sys.stderr,
        )
        return 1

    if not written:
        print(
            f"Attached, but every matching tab produced zero documents ({empty} tab(s)). "
            "Scroll reviews/answers into view in the real window and retry.",
            file=sys.stderr,
        )
        return 1
    print(
        f"\n{written} file(s) in {dest}. "
        "python -m src.collect.manual should now see them. "
        "No more Collect code after this — just more threads."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
