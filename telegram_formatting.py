"""Safe, shared rendering primitives for Telegram messages.

Controller-owned structured messages use escaped HTML. Provider-authored
Markdown is compiled to Telegram MessageEntity objects so arbitrary model text
is never interpreted through a Telegram parse mode.
"""

from __future__ import annotations

from dataclasses import dataclass
import html
import re
from typing import Any, Optional
from urllib.parse import urlparse


CONTROL_SPEAKER = "🎛 Control"
TELEGRAM_TEXT_CHUNK = 3800

STATUS_ICONS = {
    "success": "✅",
    "warning": "⚠️",
    "error": "❌",
    "cancelled": "⏹",
}


def escape_html(value: object) -> str:
    """Escape dynamic content for a controller-owned Telegram HTML message."""

    return html.escape(str(value), quote=False)


def controller_html_title(title: str, body: str = "") -> str:
    """Build the common controller title/body shape using escaped content."""

    rendered = f"<b>{escape_html(title)}</b>"
    return f"{rendered}\n\n{escape_html(body)}" if body else rendered


def controller_status_html(kind: str, text: str) -> str:
    """Build a one-line controller status with the shared semantic icon."""

    try:
        icon = STATUS_ICONS[kind]
    except KeyError:
        raise ValueError("Unknown controller status kind.") from None
    return f"{icon} <b>{escape_html(text)}</b>"


@dataclass(frozen=True)
class _Span:
    kind: str
    start: int
    end: int
    url: Optional[str] = None
    language: Optional[str] = None


@dataclass(frozen=True)
class _FormattedText:
    text: str
    spans: tuple[_Span, ...] = ()


@dataclass(frozen=True)
class RenderedChunk:
    """One Telegram-safe text chunk and its explicit Bot API entities."""

    text: str
    entities: tuple[dict[str, Any], ...] = ()
    used_plain_fallback: bool = False

    def add_to_params(self, params: dict[str, Any]) -> None:
        params["text"] = self.text
        if self.entities:
            params["entities"] = [dict(entity) for entity in self.entities]


class _MarkdownRenderError(ValueError):
    pass


class _Builder:
    def __init__(self) -> None:
        self.parts: list[str] = []
        self.spans: list[_Span] = []
        self.length = 0

    def append(self, text: str) -> tuple[int, int]:
        start = self.length
        self.parts.append(text)
        self.length += len(text)
        return start, self.length

    def append_formatted(self, formatted: _FormattedText) -> tuple[int, int]:
        start, end = self.append(formatted.text)
        self.spans.extend(
            _Span(
                span.kind,
                span.start + start,
                span.end + start,
                url=span.url,
                language=span.language,
            )
            for span in formatted.spans
        )
        return start, end

    def add_span(
        self,
        kind: str,
        start: int,
        end: int,
        *,
        url: Optional[str] = None,
        language: Optional[str] = None,
    ) -> None:
        if end > start:
            self.spans.append(
                _Span(
                    kind,
                    start,
                    end,
                    url=url,
                    language=language,
                )
            )

    def finish(self) -> _FormattedText:
        return _FormattedText("".join(self.parts), tuple(self.spans))


_FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})([^\r\n]*)\r?\n?$")
_HEADING = re.compile(r"^ {0,3}#{1,6}[ \t]+(.*)$")
_BLOCKQUOTE = re.compile(r"^ {0,3}>[ \t]?(.*)$")
_LIST_ITEM = re.compile(r"^(\s*)([-+*]|\d+[.)])[ \t]+(.*)$")
_TASK_ITEM = re.compile(r"^\[([ xX])\][ \t]+(.*)$")
_DIVIDER = re.compile(r"^ {0,3}((\*|\-|_)[ \t]*){3,}$")
_SAFE_LANGUAGE = re.compile(r"^[A-Za-z0-9_+.-]{1,40}$")
_ESCAPABLE = frozenset(r"\`*_{}[]()#+-.!~>|")
_HTML_TOKEN = re.compile(r"(<[^<>]+>|&(?:#[0-9]+|#x[0-9A-Fa-f]+|[A-Za-z]+);)")
_HTML_TAG = re.compile(r"</?([A-Za-z][A-Za-z0-9-]*)\b[^<>]*>")


def _split_line_ending(line: str) -> tuple[str, str]:
    if line.endswith("\r\n"):
        return line[:-2], "\r\n"
    if line.endswith("\n") or line.endswith("\r"):
        return line[:-1], line[-1]
    return line, ""


def _valid_link_url(url: str) -> bool:
    if any(character.isspace() for character in url):
        return False
    parsed = urlparse(url)
    if parsed.scheme in {"http", "https"}:
        return bool(parsed.netloc)
    if parsed.scheme == "tg":
        return bool(parsed.netloc or parsed.path)
    if parsed.scheme == "mailto":
        return bool(parsed.path)
    return False


def _find_closer(source: str, delimiter: str, start: int) -> int:
    cursor = start
    while True:
        cursor = source.find(delimiter, cursor)
        if cursor < 0:
            return -1
        if cursor == 0 or source[cursor - 1] != "\\":
            return cursor
        cursor += len(delimiter)


def _parse_inline(source: str) -> _FormattedText:
    builder = _Builder()
    index = 0
    while index < len(source):
        if (
            source[index] == "\\"
            and index + 1 < len(source)
            and source[index + 1] in _ESCAPABLE
        ):
            builder.append(source[index + 1])
            index += 2
            continue

        if source[index] == "`":
            run = 1
            while index + run < len(source) and source[index + run] == "`":
                run += 1
            delimiter = "`" * run
            closing = _find_closer(source, delimiter, index + run)
            if closing >= 0:
                content = source[index + run : closing]
                if content.startswith(" ") and content.endswith(" ") and content.strip():
                    content = content[1:-1]
                start, end = builder.append(content)
                builder.add_span("code", start, end)
                index = closing + run
                continue

        matched = False
        for delimiter, kind in (
            ("**", "bold"),
            ("__", "bold"),
            ("~~", "strikethrough"),
        ):
            if not source.startswith(delimiter, index):
                continue
            closing = _find_closer(
                source,
                delimiter,
                index + len(delimiter),
            )
            if closing <= index + len(delimiter):
                continue
            inner_source = source[index + len(delimiter) : closing]
            if inner_source[0].isspace() or inner_source[-1].isspace():
                continue
            inner = _parse_inline(inner_source)
            start, end = builder.append_formatted(inner)
            builder.add_span(kind, start, end)
            index = closing + len(delimiter)
            matched = True
            break
        if matched:
            continue

        if source[index] in {"*", "_"}:
            delimiter = source[index]
            before = source[index - 1] if index else ""
            if not before.isalnum():
                closing = _find_closer(source, delimiter, index + 1)
                if closing > index + 1:
                    after = source[closing + 1] if closing + 1 < len(source) else ""
                    inner_source = source[index + 1 : closing]
                    if (
                        not inner_source[0].isspace()
                        and not inner_source[-1].isspace()
                        and not after.isalnum()
                    ):
                        inner = _parse_inline(inner_source)
                        start, end = builder.append_formatted(inner)
                        builder.add_span("italic", start, end)
                        index = closing + 1
                        continue

        if source[index] == "[":
            label_end = _find_closer(source, "]", index + 1)
            if label_end > index + 1 and source.startswith("(", label_end + 1):
                url_end = _find_closer(source, ")", label_end + 2)
                if url_end > label_end + 2:
                    label_source = source[index + 1 : label_end]
                    url = source[label_end + 2 : url_end]
                    if _valid_link_url(url):
                        label = _parse_inline(label_source)
                        start, end = builder.append_formatted(label)
                        builder.add_span("text_link", start, end, url=url)
                        index = url_end + 1
                        continue

        builder.append(source[index])
        index += 1
    return builder.finish()


def _parse_markdown(source: str) -> _FormattedText:
    builder = _Builder()
    lines = source.splitlines(keepends=True)
    if not lines and source == "":
        return _FormattedText("")
    line_index = 0
    while line_index < len(lines):
        line = lines[line_index]
        content, ending = _split_line_ending(line)
        fence = _FENCE_OPEN.fullmatch(line)
        if fence is not None:
            delimiter = fence.group(1)
            fence_character = delimiter[0]
            language_text = fence.group(2).strip()
            language = (
                language_text
                if language_text and _SAFE_LANGUAGE.fullmatch(language_text)
                else None
            )
            code_parts: list[str] = []
            line_index += 1
            closed = False
            while line_index < len(lines):
                candidate = lines[line_index]
                candidate_content, candidate_ending = _split_line_ending(candidate)
                stripped = candidate_content.strip()
                if (
                    stripped
                    and set(stripped) == {fence_character}
                    and len(stripped) >= len(delimiter)
                ):
                    ending = candidate_ending
                    closed = True
                    break
                code_parts.append(candidate)
                line_index += 1
            if not closed:
                raise _MarkdownRenderError("Unclosed fenced code block.")
            code = "".join(code_parts)
            start, end = builder.append(code)
            builder.add_span("pre", start, end, language=language)
            if ending and not code.endswith(("\n", "\r")):
                builder.append(ending)
            line_index += 1
            continue

        heading = _HEADING.fullmatch(content)
        if heading is not None:
            rendered = _parse_inline(heading.group(1).rstrip(" #"))
            start, end = builder.append_formatted(rendered)
            builder.add_span("bold", start, end)
            builder.append(ending)
            line_index += 1
            continue

        quote = _BLOCKQUOTE.fullmatch(content)
        if quote is not None:
            rendered = _parse_inline(quote.group(1))
            start, end = builder.append_formatted(rendered)
            builder.add_span("blockquote", start, end)
            builder.append(ending)
            line_index += 1
            continue

        list_item = _LIST_ITEM.fullmatch(content)
        if list_item is not None:
            indentation, marker, item_text = list_item.groups()
            task = _TASK_ITEM.fullmatch(item_text)
            if task is not None:
                marker_text = "☑" if task.group(1).lower() == "x" else "☐"
                item_text = task.group(2)
            elif marker[0].isdigit():
                marker_text = marker
            else:
                marker_text = "•"
            builder.append(f"{indentation}{marker_text} ")
            builder.append_formatted(_parse_inline(item_text))
            builder.append(ending)
            line_index += 1
            continue

        if _DIVIDER.fullmatch(content):
            builder.append("────────")
            builder.append(ending)
            line_index += 1
            continue

        builder.append_formatted(_parse_inline(content))
        builder.append(ending)
        line_index += 1
    return builder.finish()


def _chunk_boundaries(text: str, limit: int) -> list[tuple[int, int]]:
    if limit <= 0:
        raise ValueError("Telegram chunk limit must be positive.")
    if text == "":
        return [(0, 0)]
    boundaries: list[tuple[int, int]] = []
    offset = 0
    while offset < len(text):
        low = offset + 1
        high = min(offset + limit, len(text))
        hard_end = low
        while low <= high:
            candidate = (low + high) // 2
            if _utf16_length(text[offset:candidate]) <= limit:
                hard_end = candidate
                low = candidate + 1
            else:
                high = candidate - 1
        if hard_end == len(text):
            end = hard_end
        else:
            newline = text.rfind("\n", offset, hard_end)
            space = text.rfind(" ", offset, hard_end)
            minimum_split = offset + max(1, (hard_end - offset) // 2)
            split_at = newline if newline >= minimum_split else space
            end = (
                hard_end
                if split_at < minimum_split
                else split_at + 1
            )
        boundaries.append((offset, end))
        offset = end
    return boundaries


def _utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _telegram_entities(
    text: str,
    spans: tuple[_Span, ...],
    start: int,
    end: int,
    prefix: str,
) -> tuple[dict[str, Any], ...]:
    entities: list[dict[str, Any]] = []
    prefix_offset = _utf16_length(prefix)
    for span in spans:
        overlap_start = max(span.start, start)
        overlap_end = min(span.end, end)
        if overlap_end <= overlap_start:
            continue
        entity: dict[str, Any] = {
            "type": span.kind,
            "offset": (
                prefix_offset
                + _utf16_length(text[start:overlap_start])
            ),
            "length": _utf16_length(text[overlap_start:overlap_end]),
        }
        if span.url is not None:
            entity["url"] = span.url
        if span.language is not None and span.kind == "pre":
            entity["language"] = span.language
        entities.append(entity)
    entities.sort(key=lambda entity: (entity["offset"], -entity["length"]))
    return tuple(entities)


def render_markdown_chunks(
    source: str,
    *,
    speaker: str = "",
    limit: int = TELEGRAM_TEXT_CHUNK,
) -> list[RenderedChunk]:
    """Compile a safe Markdown subset into chunk-local Telegram entities.

    Unsupported inline syntax remains literal. A structurally invalid document,
    such as an unclosed fenced code block, falls back to the exact source text
    with no provider-authored entities.
    """

    used_plain_fallback = False
    try:
        formatted = _parse_markdown(str(source))
    except (ValueError, UnicodeError):
        formatted = _FormattedText(str(source))
        used_plain_fallback = True

    header = str(speaker)
    prefix = f"{header}\n\n" if header else ""
    body_limit = max(1000, int(limit) - _utf16_length(prefix))
    chunks: list[RenderedChunk] = []
    for start, end in _chunk_boundaries(formatted.text, body_limit):
        body = formatted.text[start:end]
        text = f"{prefix}{body}"
        entities = list(
            _telegram_entities(
                formatted.text,
                formatted.spans,
                start,
                end,
                prefix,
            )
        )
        if header:
            entities.append(
                {
                    "type": "bold",
                    "offset": 0,
                    "length": _utf16_length(header),
                }
            )
        entities.sort(key=lambda entity: (entity["offset"], -entity["length"]))
        chunks.append(
            RenderedChunk(
                text=text or "[empty agent response]",
                entities=tuple(entities),
                used_plain_fallback=used_plain_fallback,
            )
        )
    return chunks


def render_plain_chunks(
    source: str,
    *,
    speaker: str = "",
    limit: int = TELEGRAM_TEXT_CHUNK,
) -> list[RenderedChunk]:
    """Chunk literal text while applying only the trusted speaker label."""

    body = str(source)
    header = str(speaker)
    prefix = f"{header}\n\n" if header else ""
    body_limit = max(1000, int(limit) - _utf16_length(prefix))
    chunks: list[RenderedChunk] = []
    for start, end in _chunk_boundaries(body, body_limit):
        entities: tuple[dict[str, Any], ...] = ()
        if header:
            entities = (
                {
                    "type": "bold",
                    "offset": 0,
                    "length": _utf16_length(header),
                },
            )
        chunks.append(
            RenderedChunk(
                text=f"{prefix}{body[start:end]}" or "[empty agent response]",
                entities=entities,
            )
        )
    return chunks


def render_labeled_markdown_chunks(
    source: str,
    *,
    limit: int = TELEGRAM_TEXT_CHUNK,
) -> list[RenderedChunk]:
    """Render a durable ``speaker\n\nbody`` provider message."""

    text = str(source)
    if "\n\n" not in text:
        return render_markdown_chunks(text, limit=limit)
    speaker, body = text.split("\n\n", 1)
    if "\n" in speaker or not speaker.strip():
        return render_markdown_chunks(text, limit=limit)
    return render_markdown_chunks(body, speaker=speaker, limit=limit)


def _balanced_html_body_chunks(body: str, limit: int) -> list[str]:
    """Split trusted, escaped controller HTML without cutting tags/entities."""

    tokens = [token for token in _HTML_TOKEN.split(body) if token]
    chunks: list[str] = []
    open_tags: list[tuple[str, str]] = []
    current = ""

    def closing_suffix() -> str:
        return "".join(f"</{name}>" for name, _ in reversed(open_tags))

    def reopening_prefix() -> str:
        return "".join(token for _, token in open_tags)

    def flush() -> None:
        nonlocal current
        if current:
            chunks.append(current + closing_suffix())
            current = reopening_prefix()

    for token in tokens:
        is_tag = token.startswith("<")
        tag = _HTML_TAG.fullmatch(token) if is_tag else None
        if is_tag:
            if len(current) + len(token) + len(closing_suffix()) > limit:
                flush()
            current += token
            if tag is not None:
                name = tag.group(1).lower()
                if token.startswith("</"):
                    if open_tags and open_tags[-1][0] == name:
                        open_tags.pop()
                elif not token.endswith("/>") and name not in {"br"}:
                    open_tags.append((name, token))
            continue

        if token.startswith("&"):
            if len(current) + len(token) + len(closing_suffix()) > limit:
                flush()
            current += token
            continue

        remainder = token
        while remainder:
            available = limit - len(current) - len(closing_suffix())
            if available <= 0:
                flush()
                available = limit - len(current) - len(closing_suffix())
            if len(remainder) <= available:
                current += remainder
                break
            split_at = max(
                remainder.rfind("\n", 0, available),
                remainder.rfind(" ", 0, available),
            )
            if split_at < max(1, available // 2):
                split_at = available
            else:
                split_at += 1
            current += remainder[:split_at]
            remainder = remainder[split_at:]
            flush()
    if current:
        chunks.append(current + closing_suffix())
    return chunks or [""]


def render_controller_html_chunks(
    body: str,
    *,
    speaker: str = CONTROL_SPEAKER,
    limit: int = TELEGRAM_TEXT_CHUNK,
) -> list[str]:
    """Chunk controller-owned HTML and repeat a safely escaped speaker label."""

    prefix = (
        f"<b>{escape_html(speaker)}</b>\n\n"
        if speaker
        else ""
    )
    body_limit = max(1000, int(limit) - len(prefix) - 128)
    return [
        f"{prefix}{chunk}"
        for chunk in _balanced_html_body_chunks(str(body), body_limit)
    ]
