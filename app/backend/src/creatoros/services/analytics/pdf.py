"""A small, dependency-free PDF writer for analytics reports.

Why hand-written: the project's rule is deterministic Python and no unnecessary dependency. A report
is a list of text rows; a full PDF library would add ~10 MB and a licence for functionality this file
covers in 180 lines. What it must do correctly is the *container*: object numbering, the cross-reference
table's byte offsets, stream lengths, and text escaping. Those are verified by parsing the produced
file in `tests/test_analytics_report_formats.py` and by rendering it in Chromium for the evidence pack.

Scope, deliberately: PDF 1.4, one built-in font (Helvetica), WinAnsiEncoding, monospaced rows for
tabular text, page numbers, and a footer naming the report id and digest. No images, no embedded
fonts, no compression — a report is a few hundred KB at worst, and an uncompressed file is one a
person can inspect.
"""
from __future__ import annotations

from dataclasses import dataclass, field

#: A4 portrait, in PDF points (1/72 in).
PAGE_WIDTH = 595
PAGE_HEIGHT = 842
MARGIN_LEFT = 46
MARGIN_TOP = 56
MARGIN_BOTTOM = 56
LINE_HEIGHT = 12.0
FONT_SIZE = 8.5
TITLE_SIZE = 15.0
HEADING_SIZE = 11.0
#: Conservative wrap: Helvetica at 8.5 pt averages ~0.5 em per glyph.
MAX_CHARS = 118


def _escape(text: str) -> str:
    """PDF string escaping for a literal inside ``( ... )``.

    Non-Latin-1 characters are replaced, not dropped silently: a report must never contain a glyph the
    chosen encoding cannot represent, because the reader would see a different string than the file.
    """
    out: list[str] = []
    for ch in text:
        if ch in "()\\":
            out.append("\\" + ch)
        elif ch == "\r":
            continue
        elif ch == "\n":
            out.append(" ")
        elif 32 <= ord(ch) <= 255:
            out.append(ch)
        else:
            out.append("?")
    return "".join(out)


def _fit(text: str, width: int = MAX_CHARS) -> list[str]:
    """Wrap on word boundaries; never split a word, never return an empty list for non-empty input."""
    text = " ".join(text.split())
    if not text:
        return [""]
    lines: list[str] = []
    current = ""
    for word in text.split(" "):
        while len(word) > width:
            if current:
                lines.append(current)
                current = ""
            lines.append(word[:width])
            word = word[width:]
        candidate = f"{current} {word}".strip()
        if len(candidate) <= width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or [""]


@dataclass(slots=True)
class _Page:
    lines: list[str] = field(default_factory=list)


@dataclass(slots=True)
class PdfDocument:
    """Accumulates lines, then serialises. ``render()`` is the only public operation."""

    title: str
    footer: str
    pages: list[_Page] = field(default_factory=list)
    _current: _Page | None = None
    _y: float = 0.0
    _body_lines_per_page: int = 0

    def __post_init__(self) -> None:
        self._body_lines_per_page = int((PAGE_HEIGHT - MARGIN_TOP - MARGIN_BOTTOM - TITLE_SIZE * 2) / LINE_HEIGHT)
        self._new_page()

    def _new_page(self) -> None:
        self._current = _Page()
        self.pages.append(self._current)
        self._y = PAGE_HEIGHT - MARGIN_TOP

    def _room(self, needed: int = 1) -> None:
        if self._y - needed * LINE_HEIGHT < MARGIN_BOTTOM:
            self._new_page()

    def heading(self, text: str) -> None:
        self._room(3)
        self._y -= HEADING_SIZE + 4
        self._current.lines.append(f"#H {text}")
        self._y -= 4

    def line(self, text: str) -> None:
        for piece in _fit(text):
            self._room(1)
            self._current.lines.append(piece)
            self._y -= LINE_HEIGHT

    def blank(self) -> None:
        self._room(1)
        self._y -= LINE_HEIGHT * 0.5

    # ------------------------------------------------------------------ serialisation

    def _content_stream(self, index: int) -> bytes:
        parts: list[str] = ["BT"]
        cursor = PAGE_HEIGHT - MARGIN_TOP
        # Title block on the first page only.
        if index == 0:
            parts.append(f"/F1 {TITLE_SIZE} Tf")
            parts.append(f"{MARGIN_LEFT} {cursor} Td")
            parts.append(f"({_escape(self.title)}) Tj")
            cursor -= TITLE_SIZE + 10
        parts.append(f"/F2 {FONT_SIZE} Tf")
        for raw in self.pages[index].lines:
            if raw.startswith("#H "):
                parts.append(f"/F1 {HEADING_SIZE} Tf")
                cursor -= HEADING_SIZE + 4
                parts.append(f"{MARGIN_LEFT} {cursor} Td")
                parts.append(f"({_escape(raw[3:])}) Tj")
                parts.append(f"/F2 {FONT_SIZE} Tf")
                cursor -= 6
                continue
            parts.append(f"{MARGIN_LEFT} {cursor} Td")
            parts.append(f"({_escape(raw)}) Tj")
            cursor -= LINE_HEIGHT
        # Footer: page number and the identity of the document on every page.
        foot = f"{self.footer}  -  page {index + 1} of {len(self.pages)}"
        parts.append("/F2 7.5 Tf")
        parts.append(f"{MARGIN_LEFT} {MARGIN_BOTTOM - 18} Td")
        parts.append(f"({_escape(foot)}) Tj")
        parts.append("ET")
        return "\n".join(parts).encode("latin-1", "replace")

    def render(self, *, producer: str = "CreatorOS analytics reports") -> bytes:
        # Object layout: 1 Catalog, 2 Pages, 3 Font, 4 Font, then (Page, Contents) per page.
        page_count = len(self.pages)
        objects: dict[int, bytes] = {}
        kids: list[int] = []
        next_id = 5
        page_ids: list[tuple[int, int]] = []
        for _ in range(page_count):
            page_ids.append((next_id, next_id + 1))
            next_id += 2
        for page_id, content_id in page_ids:
            kids.append(page_id)
        objects[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
        objects[2] = f"<< /Type /Pages /Count {page_count} /Kids [{' '.join(f'{kid} 0 R' for kid in kids)}] >>".encode()
        objects[3] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"
        objects[4] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>"
        objects[5] = f"<< /Producer ({_escape(producer)}) /Creator ({_escape(producer)}) >>".encode()
        info_id = 5
        for index, (page_id, content_id) in enumerate(page_ids):
            stream = self._content_stream(index)
            objects[page_id] = (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_WIDTH} {PAGE_HEIGHT}] "
                f"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> /Contents {content_id} 0 R >>"
            ).encode()
            objects[content_id] = b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"

        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets: dict[int, int] = {}
        for object_id in sorted(objects):
            offsets[object_id] = len(out)
            out += f"{object_id} 0 obj\n".encode() + objects[object_id] + b"\nendobj\n"
        xref_at = len(out)
        size = max(objects) + 1
        out += f"xref\n0 {size}\n".encode()
        out += b"0000000000 65535 f \n"
        for object_id in range(1, size):
            offset = offsets.get(object_id, 0)
            out += f"{offset:010d} 00000 n \n".encode()
        out += (
            f"trailer\n<< /Size {size} /Root 1 0 R /Info {info_id} 0 R >>\nstartxref\n{xref_at}\n%%EOF\n"
        ).encode()
        return bytes(out)


def render_pdf(title: str, blocks: list[tuple[str, list[str]]], footer: str) -> bytes:
    """Render ``blocks`` of ``(heading, [line, ...])`` into a complete PDF."""
    doc = PdfDocument(title=title, footer=footer)
    for heading, lines in blocks:
        doc.heading(heading)
        for line in lines:
            doc.line(line)
        doc.blank()
    return doc.render()


__all__ = ["PdfDocument", "render_pdf"]
