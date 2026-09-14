"""Shape BBS text for a real terminal: word wrap and colour, SSH only.

The BBS writes for radios -- short lines, no markup -- and the SSH front end
used to pass that straight through. Two things went wrong on a terminal:

  * Long lines (a welcome paragraph, a bulletin, a Nomad answer) were left
    for the terminal to break at its right edge, which it does in the middle
    of a word.
  * Nothing distinguished a menu title from its options, a tip, or an error.

Both are fixed here and only here, on the way out of ssh_server. The text
the BBS produces is unchanged, so a radio never sees a colour code or a
wrapped line.
"""

import re
import unicodedata


RESET = "\x1b[0m"
TITLE = "\x1b[1;36m"     # bold cyan
OPTION = "\x1b[1;33m"    # bold yellow
TIP = "\x1b[2m"          # dim
ERROR = "\x1b[1;31m"     # bold red

# Terminals that cannot show colour, or asked for none.
_PLAIN_TERMINALS = {"", "dumb", "unknown"}

DEFAULT_WIDTH = 80
MIN_WIDTH = 20


def wants_colour(term_type) -> bool:
    return str(term_type or "").strip().casefold() not in _PLAIN_TERMINALS


def char_width(char: str) -> int:
    """Columns one character takes: 0 for combining marks and emoji
    modifiers, 2 for wide characters such as most emoji, else 1."""
    if char in ("‍", "️", "︎") or unicodedata.combining(char):
        return 0
    if unicodedata.category(char) in ("Mn", "Me", "Cf"):
        return 0
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


def display_width(text: str) -> int:
    return sum(char_width(c) for c in str(text))


def _split_long_word(word: str, width: int) -> list:
    pieces, current, used = [], "", 0
    for char in word:
        w = char_width(char)
        if current and used + w > width:
            pieces.append(current)
            current, used = "", 0
        current += char
        used += w
    if current:
        pieces.append(current)
    return pieces


def wrap_line(line: str, width: int) -> list:
    """Break one line at spaces so no row is wider than ``width`` columns.

    Continuation rows keep the line's leading indentation, so an indented
    block stays indented. A word longer than a whole row is split, because
    the alternative is the terminal splitting it anyway.
    """
    width = max(MIN_WIDTH, int(width))
    if display_width(line) <= width:
        return [line]
    stripped = line.lstrip(" ")
    indent = line[:len(line) - len(stripped)]
    if display_width(indent) > width // 2:
        indent = ""
    rows, current = [], indent
    for word in stripped.split(" "):
        candidate = word if current in ("", indent) else " " + word
        if display_width(current) + display_width(candidate) <= width:
            current += candidate
            continue
        if current.strip():
            rows.append(current.rstrip())
            current = indent
        room = width - display_width(indent)
        if display_width(word) > room:
            pieces = _split_long_word(word, room)
            rows.extend(indent + p for p in pieces[:-1])
            current = indent + pieces[-1]
        else:
            current = indent + word
    if current.strip():
        rows.append(current.rstrip())
    return rows or [""]


# A menu title is framed by the same symbol on both sides: "📰BBS Menu📰",
# "🎮 Games 🎮", "💾Bacon BBS💾 (✉️:3)".
_TITLE = re.compile(r"^\s*([^\w\s\[\(])[️]?\s*\S.*?\1")
# [1] [0] [1-4] [N] [#] and the letter-in-brackets style: [S]cores, [H]all.
_OPTION = re.compile(r"\[(?:\d+(?:-\d+)?|[A-Za-z#])\]")
# Only where a line STARTS like a BBS error. Matching "failed" anywhere would
# paint a user's bulletin red for saying the test failed.
_ERROR = re.compile(
    r"^\s*(?:Invalid|Error|\[ERR\]|Too many|Couldn't|Could not|That code has expired"
    r"|The BBS could not|No one is listed|Unknown command|Mail not found"
    r"|That user is not accepting)")


def colour_line(line: str) -> str:
    """Colour one plain line. Lines that are wholly a title, tip or error
    take one colour; anything else has its [option] keys highlighted."""
    if not line.strip():
        return line
    if line.lstrip().startswith("Tip:"):
        return TIP + line + RESET
    if _ERROR.search(line):
        return ERROR + line + RESET
    if _TITLE.match(line):
        return TITLE + line + RESET
    return _OPTION.sub(lambda m: OPTION + m.group(0) + RESET, line)


def render(text: str, width: int = DEFAULT_WIDTH, colour: bool = False) -> str:
    """BBS text as terminal output: wrapped, optionally coloured, CRLF line ends.

    Wraps on the plain text first and colours afterwards, so escape codes
    never count towards a row's width.
    """
    rows = []
    for line in str(text).replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        for row in wrap_line(line, width):
            rows.append(colour_line(row) if colour else row)
    return "\r\n".join(rows)
