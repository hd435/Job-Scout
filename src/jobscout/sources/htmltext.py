"""Small standard-library HTML helpers shared by the HTML-based adapters.

Only targeted extraction is supported: JSON-LD blocks, the first heading, `<time>`
values, and meta tags. No general DOM, selector engine, or third-party parser is needed
for the handful of fields the matcher uses.
"""

from __future__ import annotations

import html
import json
import re
from html.parser import HTMLParser
from typing import Any

_WHITESPACE = re.compile(r"\s+")
_BLOCK_TAGS = {
    "p",
    "div",
    "br",
    "li",
    "ul",
    "ol",
    "tr",
    "td",
    "th",
    "section",
    "article",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._skip_depth:
            self._skip_depth -= 1
        elif tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self._chunks.append(data)

    @property
    def text(self) -> str:
        lines = (_WHITESPACE.sub(" ", chunk).strip() for chunk in "".join(self._chunks).split("\n"))
        return "\n".join(line for line in lines if line)


def strip_html(markup: str | None, *, limit: int | None = None) -> str | None:
    """Return readable text from markup, or None when nothing usable remains.

    Greenhouse serves `content` HTML-escaped, so entities are unescaped first.
    """
    if not markup:
        return None
    unescaped = html.unescape(markup)
    if "<" not in unescaped:
        text = _WHITESPACE.sub(" ", unescaped).strip()
    else:
        parser = _TextExtractor()
        parser.feed(unescaped)
        parser.close()
        text = parser.text
    if not text:
        return None
    if limit is not None and len(text) > limit:
        return text[:limit].rstrip()
    return text


class _DocumentScanner(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.json_ld: list[str] = []
        self.headings: list[str] = []
        self.times: list[str] = []
        self.meta: dict[str, str] = {}
        self._script_type: str | None = None
        self._script_buffer: list[str] = []
        self._capture_heading: str | None = None
        self._heading_buffer: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {name.lower(): value or "" for name, value in attrs}
        if tag == "script":
            script_type = (attributes.get("type") or "").lower()
            self._script_type = script_type if "ld+json" in script_type else None
            self._script_buffer = []
        elif tag in {"h1", "h2"} and self._capture_heading is None:
            self._capture_heading = tag
            self._heading_buffer = []
        elif tag == "time":
            moment = attributes.get("datetime", "").strip()
            if moment:
                self.times.append(moment)
        elif tag == "meta":
            key = (attributes.get("property") or attributes.get("name") or "").strip()
            value = attributes.get("content", "").strip()
            if key and value:
                self.meta.setdefault(key.lower(), value)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._script_type is not None:
            self.json_ld.append("".join(self._script_buffer))
            self._script_type = None
            self._script_buffer = []
        elif tag == self._capture_heading:
            text = _WHITESPACE.sub(" ", "".join(self._heading_buffer)).strip()
            if text:
                self.headings.append(text)
            self._capture_heading = None
            self._heading_buffer = []

    def handle_data(self, data: str) -> None:
        if self._script_type is not None:
            self._script_buffer.append(data)
        elif self._capture_heading is not None:
            self._heading_buffer.append(data)


class HtmlDocument:
    """Targeted extract of the parts of a job page that carry role fields."""

    def __init__(self, markup: str) -> None:
        scanner = _DocumentScanner()
        try:
            scanner.feed(markup)
            scanner.close()
        except Exception:  # pragma: no cover - HTMLParser is tolerant; guard anyway
            pass
        self.json_ld_blocks: tuple[str, ...] = tuple(scanner.json_ld)
        self.headings: tuple[str, ...] = tuple(scanner.headings)
        self.times: tuple[str, ...] = tuple(scanner.times)
        self.meta: dict[str, str] = scanner.meta

    def json_ld_objects(self) -> tuple[dict[str, Any], ...]:
        """Parse JSON-LD blocks, keeping only objects and object lists."""
        objects: list[dict[str, Any]] = []
        for block in self.json_ld_blocks:
            try:
                payload = json.loads(block)
            except (TypeError, ValueError):
                continue
            for candidate in payload if isinstance(payload, list) else [payload]:
                if isinstance(candidate, dict):
                    objects.append(candidate)
                    graph = candidate.get("@graph")
                    if isinstance(graph, list):
                        objects.extend(item for item in graph if isinstance(item, dict))
        return tuple(objects)

    def job_posting(self) -> dict[str, Any] | None:
        """The first schema.org JobPosting object, if the page publishes one."""
        for candidate in self.json_ld_objects():
            kind = candidate.get("@type")
            kinds = kind if isinstance(kind, list) else [kind]
            if any(str(value).lower() == "jobposting" for value in kinds):
                return candidate
        return None
