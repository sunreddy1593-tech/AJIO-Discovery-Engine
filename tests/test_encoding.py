"""Both encoding boundaries: hand-saved input files, and our own console output.

Input arrives in whatever encoding the editor chose, and the bug that guards
against is not a crash — it is a success. A UTF-8 BOM read as plain UTF-8 decodes
cleanly and glues an invisible U+FEFF onto the first line, which broke the first
real manual import: three records became zero, with four warnings that all blamed
the file's *content*.

Output fails the opposite way, loudly and late: one character the console codepage
lacks raises from ``print``, which took down a corpus build after every document
had been committed.
"""

from __future__ import annotations

import io
import sys

import pytest

from src.collect.ajio_manual import parse_file as parse_ajio
from src.collect.quora_manual import parse_file as parse_quora
from src.common.encoding import (
    detect_encoding,
    harden_stdio,
    harden_stream,
    read_text_tolerant,
)
from src.store.relevance import load_keywords

AJIO_FILE = """product: 469558637
title: Test AJIO Product

## Q&A

Q: Does this product fit true to size?
A: Yes, it fits true to size.

Q: Is the fabric comfortable for daily wear?
A: Yes, according to the answer provided.

## Reviews

[4] This product fits well and the fabric feels comfortable for regular use.
- by testuser, 20 August 2026
"""

QUORA_FILE = """Why do I keep saving dresses on Ajio and never actually buying them?

Honestly I do this all the time. I save things when I am bored at night and by
the morning I have completely lost interest in most of them.

For me it is the sizing. I never trust that a medium here is the same medium as
anywhere else, so the item just sits there until it goes out of stock.
"""

KEYWORDS_FILE = "wishlist\nsize chart\n# a comment\n\nreturn policy\n"

#: The five ways these files actually show up on Windows.
ENCODINGS = [
    ("utf-8", "utf-8"),
    ("utf-8-sig", "utf-8 with BOM (VS Code, Notepad)"),
    ("utf-16", "utf-16 with BOM (PowerShell Out-File)"),
    ("utf-16-le", "utf-16LE, BOM written explicitly"),
    ("utf-16-be", "utf-16BE"),
]


def write(path, text: str, codec: str):
    if codec == "utf-16-le":
        path.write_bytes(b"\xff\xfe" + text.encode("utf-16-le"))
    elif codec == "utf-16-be":
        path.write_bytes(b"\xfe\xff" + text.encode("utf-16-be"))
    else:
        path.write_bytes(text.encode(codec))
    return path


@pytest.mark.parametrize("codec,label", ENCODINGS)
def test_round_trips_every_encoding(tmp_path, codec, label):
    path = write(tmp_path / "f.md", AJIO_FILE, codec)
    assert read_text_tolerant(path) == AJIO_FILE, label


@pytest.mark.parametrize("codec,label", ENCODINGS)
def test_ajio_manual_parses_under_every_encoding(tmp_path, codec, label):
    """The regression: two Q&A plus one review, and no warnings, regardless of BOM."""
    path = write(tmp_path / "import.md", AJIO_FILE, codec)
    records, warnings = parse_ajio(path)
    assert warnings == [], f"{label}: {warnings}"
    assert len(records) == 3, label
    assert sorted(r["meta"]["content_type"] for r in records) == ["qa", "qa", "review"]


@pytest.mark.parametrize("codec,label", ENCODINGS)
def test_quora_manual_parses_under_every_encoding(tmp_path, codec, label):
    """The same fix has to cover Quora: it is the other hand-saved pre-purchase source."""
    path = write(tmp_path / "thread.md", QUORA_FILE, codec)
    records = parse_quora(path)
    assert len(records) == 2, label
    assert all("Ajio" in r["meta"]["question"] for r in records), label


@pytest.mark.parametrize("codec,label", ENCODINGS)
def test_keyword_vocabulary_loads_under_every_encoding(tmp_path, codec, label):
    """A BOM here disables the first keyword silently, with nothing in the log."""
    path = write(tmp_path / "kw.txt", KEYWORDS_FILE, codec)
    assert load_keywords(path) == ["wishlist", "size chart", "return policy"], label


def test_a_bom_would_otherwise_survive_strip():
    """Why `term.strip()` was not already enough: U+FEFF is not whitespace in Python."""
    assert "\ufeffwishlist".strip() == "\ufeffwishlist"
    assert not "\ufeff".isspace()


def test_plain_utf8_is_unchanged():
    """No BOM means no guessing: UTF-8 stays the default rather than sniffing bytes."""
    assert detect_encoding("product: 1".encode()) == "utf-8"


def test_utf32_is_not_mistaken_for_utf16():
    """UTF-32LE opens with the UTF-16LE mark, so BOM order matters."""
    assert detect_encoding(b"\xff\xfe\x00\x00abc") == "utf-32"
    assert detect_encoding(b"\xff\xfea\x00") == "utf-16"


def test_corrupt_bytes_still_yield_text(tmp_path):
    """Genuinely malformed input degrades to replacement chars for RawRecord to reject."""
    path = tmp_path / "broken.md"
    path.write_bytes(b"product: 1\n\xff\xfe\xfa garbled")
    assert "\ufffd" in read_text_tolerant(path)


# --- output side: the console codepage ------------------------------------------


def cp1252_stream() -> io.TextIOWrapper:
    """Stand in for a piped stdout on this machine: strict, and not UTF-8."""
    return io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")


#: Characters the collectors are specified to preserve, none of them in cp1252.
UNPRINTABLE_IN_CP1252 = ["\u26a0", "\U0001f44d", "\u092e\u0941\u091d\u0947"]


@pytest.mark.parametrize("char", UNPRINTABLE_IN_CP1252)
def test_cp1252_stdout_would_raise_without_hardening(char):
    """The bug itself, so the fix below is measured against a real failure."""
    with pytest.raises(UnicodeEncodeError):
        print(char, file=cp1252_stream())


@pytest.mark.parametrize("char", UNPRINTABLE_IN_CP1252)
def test_hardened_stream_prints_the_same_characters(char):
    stream = harden_stream(cp1252_stream())
    print(char, file=stream)
    stream.flush()
    assert stream.encoding == "utf-8"
    assert char in stream.buffer.getvalue().decode("utf-8")


def test_harden_stdio_covers_stderr_too(monkeypatch):
    """Tracebacks and warnings go to stderr; ₹ in a logged review must not raise there."""
    out, err = cp1252_stream(), cp1252_stream()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    harden_stdio()
    print("\u20b91299", file=sys.stdout)
    print("\u20b91299", file=sys.stderr)
    assert (sys.stdout.encoding, sys.stderr.encoding) == ("utf-8", "utf-8")


def test_harden_stream_leaves_unreconfigurable_streams_alone():
    """pytest's capture object and StringIO hold str already; hardening must not fail."""
    buf = io.StringIO()
    assert harden_stream(buf) is buf
    print("\u26a0", file=buf)
    assert "\u26a0" in buf.getvalue()


def test_funnel_report_survives_a_cp1252_console(monkeypatch):
    """The regression: the build wrote and committed 12,702 documents, then exited 1.

    The warning branch is the one that crashed, so the summary here is shaped to
    trigger it (zero pre-purchase documents, below the floor).
    """
    from src.store.build_corpus import _print_funnel

    summary = dict.fromkeys(
        (
            "raw_loaded",
            "malformed_skipped",
            "normalized",
            "excluded_too_short",
            "excluded_contains_emoji",
            "excluded_hindi_language",
            "duplicates_marked",
            "relevant",
        ),
        0,
    )
    monkeypatch.setattr(sys, "stdout", cp1252_stream())
    harden_stdio()
    _print_funnel(summary, [])
    sys.stdout.flush()
    printed = sys.stdout.buffer.getvalue().decode("utf-8")
    assert "pre-purchase floor NOT met" in printed


def test_setup_logging_hardens_stdout(tmp_path, monkeypatch):
    """Every entry point calls setup_logging first, which is why it does the hardening."""
    from src.common.logging import new_run_id, setup_logging

    monkeypatch.setattr(sys, "stdout", cp1252_stream())
    setup_logging(new_run_id("test"), tmp_path)
    assert sys.stdout.encoding == "utf-8"
