# Telegram message style

Telegram Control has two distinct trust boundaries for text. Formatting must
make that distinction visible in code rather than relying on a caller to
remember escaping rules.

## Message classes

### Controller UI

Controller-authored help, status, confirmation, error, and inventory messages
may use Telegram HTML. Dynamic values must pass through `escape_html()` before
they are interpolated. Use the shared helpers in `telegram_formatting.py` and
never concatenate database-, user-, provider-, or filesystem-derived text into
HTML directly.

Controller HTML is chunked by `render_controller_html_chunks()`. It keeps tags
balanced, does not split HTML entities, and repeats the escaped speaker label.
Literal controller messages use `render_plain_chunks()`, which leaves the body
unchanged and applies only the trusted bold speaker entity.

### Provider answers

Claude and Codex return text that may contain Markdown. Provider text is never
sent through Telegram's HTML, Markdown, or MarkdownV2 parse modes. Instead,
`render_markdown_chunks()` compiles a deliberately small Markdown subset to
explicit Telegram `MessageEntity` objects:

- headings;
- bold, italic, and strikethrough;
- inline code and fenced code blocks with optional language names;
- safe `http`, `https`, `tg`, and `mailto` links;
- bullets, numbered lists, and task-list markers;
- block quotes and dividers.

Unsupported inline syntax remains literal. A structurally invalid document,
such as an unclosed fenced code block, is sent as its exact plain source text.
If Telegram rejects an otherwise validated entity payload, the durable sender
removes the entities and retries the same visible text immediately.

Entity offsets and lengths are calculated in UTF-16 code units, as required by
Telegram. Chunking happens before offsets are emitted; spans crossing a chunk
boundary are safely recreated in each resulting message.

### Verbatim data

Transcripts, logs, exception strings, paths, and other arbitrary data are plain
text unless a controller-owned renderer explicitly escapes and wraps them.
Provider source text remains stored unchanged for session history, retries, and
text-to-speech even when its Telegram display uses entities.

## Visual hierarchy

- Use one bold title followed by a blank line for a multi-section controller
  message.
- Use code styling for commands, slugs, model names, provider identifiers,
  paths, and other machine-readable values.
- Separate sections with one blank line. Prefer bullets when there are three or
  more sibling items.
- Keep button labels short, sentence case, and without trailing punctuation.
- Do not add decorative separators when whitespace provides enough hierarchy.

## Status language

Use the shared semantic icons consistently:

- `✅` success;
- `⚠️` warning;
- `❌` failure;
- `⏹` cancelled.

Progress stages may retain their established task-specific icons, but the same
stage must use the same icon and wording across initial sends, edits, retries,
and fallbacks.

## Speaker labels

Use `🎛 Control` for Control-authored turns and the durable project name for
relayed agent turns. Omit an agent label inside that agent's own topic, where
the topic already identifies the speaker. Repeat a required label on every
continuation chunk.

`telegram_formatting.CONTROL_SPEAKER` is the single code constant for the
Control label.

## Testing requirements

Formatting changes require fixtures covering:

- the exact visible text and entity types;
- emoji and other non-BMP characters before entity spans;
- markup crossing chunk boundaries;
- malformed Markdown plain-text fallback;
- rejected-entity delivery fallback;
- escaped dynamic controller content;
- balanced HTML across controller message chunks.

Never adopt a new provider Markdown construct by passing the raw output to a
Telegram parse mode. Extend the renderer and its fixtures instead.
