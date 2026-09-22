"""Set values in config.ini without losing its comments.

    python scripts/config_set.py config.ini interface type=none ssh enabled=true

Arguments after the file are sections and key=value pairs: a bare word starts
a section, and each key=value after it is set in that section.

configparser can read the file but would write it back with every comment
stripped, and the comments in example_config.ini are the documentation a
new operator reads. So this edits the text: an existing `key = ...` line in
an active [section] is replaced in place, a missing key is added right after
the section header, and a section that exists only as a commented example
(`# [ssh]`) is appended as a real one at the end.
"""

import re
import sys


def set_values(text: str, section: str, values: dict) -> str:
    lines = text.splitlines()
    header = re.compile(r"^\s*\[([^\]]+)\]\s*$")
    start = end = None
    for index, line in enumerate(lines):
        match = header.match(line)
        if not match:
            continue
        if start is not None:
            end = index
            break
        if match.group(1).strip() == section:
            start = index
    if start is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(f"[{section}]")
        lines.extend(f"{key} = {value}" for key, value in values.items())
        return "\n".join(lines) + "\n"
    if end is None:
        end = len(lines)

    remaining = dict(values)
    for index in range(start + 1, end):
        match = re.match(r"^\s*([A-Za-z0-9_.-]+)\s*[=:]", lines[index])
        if match and match.group(1) in remaining:
            key = match.group(1)
            lines[index] = f"{key} = {remaining.pop(key)}"
    lines[start + 1:start + 1] = [f"{key} = {value}"
                                  for key, value in remaining.items()]
    return "\n".join(lines) + "\n"


def parse_arguments(arguments):
    """[section, k=v, k=v, section, k=v] -> [(section, {k: v})]."""
    updates = []
    for argument in arguments:
        if "=" in argument and updates:
            key, value = argument.split("=", 1)
            updates[-1][1][key.strip()] = value.strip()
        elif "=" not in argument:
            updates.append((argument.strip(), {}))
        else:
            raise SystemExit(f"{argument!r} comes before any section name")
    return updates


def main(argv) -> int:
    if len(argv) < 3:
        print(__doc__.strip())
        return 2
    path = argv[1]
    with open(path, encoding="utf-8", newline="") as handle:
        raw = handle.read()
    crlf = "\r\n" in raw
    text = raw.replace("\r\n", "\n")
    for section, values in parse_arguments(argv[2:]):
        if values:
            text = set_values(text, section, values)
    if crlf:
        text = text.replace("\n", "\r\n")
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
