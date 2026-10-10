"""
Fixed-width CLI table reader for `show interface(s) ...` tables.

Columns are located from the table itself, never hard-coded positions:

  * dash-group separator (OS6 style)     Port      Description   Vlan  ...
                                         --------- ------------- ----- ...
    each dash run is one column; the header may span two lines above it (e.g. "Link"
    over "State"), and the words above a column are joined ("Link State").

  * full-width rule (OS10 style)         ----------------------------------------
                                         Port      Description   Status   Speed
                                         ----------------------------------------
    the header words' start positions are the column starts.

A cell is the text between its column start and the next column start. Rows run until a
blank line or the next separator. Column names are matched by a normalized key
(lower-case, letters/digits only), so "Link State", "Link-State" and "LinkState" agree.
"""

import re

_SEPARATOR = re.compile(r"^\s*-{3,}(?:\s+-{2,})*\s*$")


def key(name):
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _dash_groups(line):
    return [(m.start(), m.end()) for m in re.finditer(r"-+", line)]


def _cells(line, starts):
    cells = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else None
        cells.append(line[start:end].strip() if start < len(line) else "")
    return cells


def _header_from_words(line):
    words = [(m.start(), m.group(0)) for m in re.finditer(r"\S+", line)]
    return [start for start, _ in words], [word for _, word in words]


def _rows(lines, start_index, starts, names):
    rows = []
    for line in lines[start_index:]:
        if not line.strip() or _SEPARATOR.match(line):
            break
        cells = _cells(line.rstrip(), starts)
        rows.append({key(name): value for name, value in zip(names, cells)})
    return rows


def parse_tables(text):
    """All tables in `text`: [{"columns": [normalized names], "rows": [{col: cell}]}]."""
    lines = (text or "").replace("\t", " ").splitlines()
    tables = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not _SEPARATOR.match(line):
            i += 1
            continue

        groups = _dash_groups(line)
        if len(groups) >= 2:
            # Dash groups: one per column; header = up to two non-empty lines directly above.
            starts = [start for start, _ in groups]
            header_lines = []
            j = i - 1
            while j >= 0 and len(header_lines) < 2 and lines[j].strip() and not _SEPARATOR.match(lines[j]):
                header_lines.insert(0, lines[j])
                j -= 1
            names = []
            for index, start in enumerate(starts):
                end = starts[index + 1] if index + 1 < len(starts) else None
                words = [h[start:end].strip() for h in header_lines if h[start:end].strip()]
                names.append(" ".join(words))
            if header_lines and any(names):
                rows = _rows(lines, i + 1, starts, names)
                tables.append({"columns": [key(n) for n in names], "rows": rows})
            i += 1
            continue

        # Full-width rule: header is the next line, closed by another rule.
        if i + 2 < len(lines) and lines[i + 1].strip() and _SEPARATOR.match(lines[i + 2]) \
                and len(_dash_groups(lines[i + 2])) == 1:
            starts, names = _header_from_words(lines[i + 1])
            rows = _rows(lines, i + 3, starts, names)
            tables.append({"columns": [key(n) for n in names], "rows": rows})
            i += 3
            continue
        i += 1
    return tables


def first_value(row, *names):
    """First non-empty cell among alias column names (normalized), else None."""
    for name in names:
        value = row.get(key(name))
        if value not in (None, ""):
            return value
    return None
