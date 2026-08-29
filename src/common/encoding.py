"""Encoding boundaries: files a human saved by hand, and our own console output.

Both ends of this pipeline meet the host's legacy codepage, and on Windows that
is cp1252 rather than UTF-8. The two failures look unrelated and share a cause,
so the fixes live together here.

**Input.** Every other file this pipeline reads is machine-written and therefore
reliably UTF-8. The exceptions are the ones a person creates in an editor: the
manual AJIO and Quora imports, and the relevance keyword list. On Windows those
arrive in whatever the tool defaulted to — Notepad and VS Code often prepend a
UTF-8 BOM, and PowerShell's ``>`` and ``Out-File`` produce UTF-16LE. Reading such
a file as plain UTF-8 fails in the worst possible way: it succeeds. A BOM decoded
with ``errors="replace"`` glues an invisible ``\\ufeff`` onto the first line, so
``product: 12345`` stops matching a directive that anchors at the start of the
line, and the parser reports a *content* problem for what is really an encoding
problem. That happened on the first real manual import: three records became
zero, with four warnings that all pointed at the wrong cause.

**Output.** The mirror image, and it fails loudly instead of silently: printing
one character the codepage lacks raises ``UnicodeEncodeError`` from ``print``
itself. That killed a corpus build *after* all 12,702 documents had been written
and committed — every stage's work done, funnel on screen, exit code 1.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

#: Byte-order marks, longest first — UTF-32LE starts with the UTF-16LE mark, so
#: checking the short one first would misidentify it.
_BOMS: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xfe\x00\x00", "utf-32"),
    (b"\x00\x00\xfe\xff", "utf-32"),
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xff\xfe", "utf-16"),
    (b"\xfe\xff", "utf-16"),
)


def detect_encoding(data: bytes) -> str:
    """Name the codec to decode ``data`` with, from its byte-order mark.

    Returns ``"utf-8"`` when there is no BOM, which is both the correct guess for
    machine-written files and the only safe default: guessing a legacy codepage
    from byte statistics would silently corrupt text rather than fail.
    """
    for mark, codec in _BOMS:
        if data.startswith(mark):
            return codec
    return "utf-8"


def read_text_tolerant(path: str | Path) -> str:
    """Read a hand-saved text file, honoring whatever BOM it carries.

    ``errors="replace"`` is kept for genuinely malformed bytes, so a corrupt file
    still yields text; ``RawRecord`` rejects it downstream on the replacement
    character ratio (`edge-case.md` §1.2.6). What this avoids is the *silent* case
    where a perfectly valid file is decoded with the wrong codec.
    """
    data = Path(path).read_bytes()
    text = data.decode(detect_encoding(data), errors="replace")
    # A UTF-16 file written without a BOM, or one that survived a copy-paste, can
    # still leave a leading U+FEFF after decoding. Strip it here rather than in
    # each parser, since the parsers anchor patterns to the start of the line.
    return text.lstrip("\ufeff")


def harden_stream(stream: Any) -> Any:
    """Switch one text stream to UTF-8, tolerating streams that cannot be switched.

    ``pytest``'s capture object and a plain ``StringIO`` have no ``reconfigure``;
    they hold ``str`` and are already Unicode-safe, so there is nothing to do. A
    detached or closed stream raises instead, which is not worth failing a run over
    — the caller is trying to make output *more* robust.
    """
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
        return stream
    try:
        reconfigure(encoding="utf-8", errors="replace")
    except (ValueError, OSError):
        pass
    return stream


def harden_stdio() -> None:
    """Force UTF-8 on this process's ``stdout`` and ``stderr``.

    Call once per entry point, before anything is printed. Without it the streams
    keep the console codepage, and any character outside it — a ``⚠`` in a report
    header, or the ``₹``, emoji and Devanagari that legitimately appear in the
    reviews this pipeline collects — turns a finished run into a traceback.

    ``errors="replace"`` is belt-and-braces: UTF-8 encodes every code point, so it
    only ever engages on lone surrogates (a path Windows handed us, say).
    """
    harden_stream(sys.stdout)
    harden_stream(sys.stderr)
