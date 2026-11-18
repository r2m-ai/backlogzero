"""终端输出：简单的等宽表格。"""

from __future__ import annotations

from collections.abc import Sequence


def format_table(rows: Sequence[Sequence[object]], headers: Sequence[str]) -> str:
    cells = [[str(cell) for cell in row] for row in rows]
    widths = [
        max(len(header), *(len(row[index]) for row in cells)) if cells else len(header)
        for index, header in enumerate(headers)
    ]
    line = "  ".join(header.ljust(widths[i]) for i, header in enumerate(headers))
    sep = "  ".join("-" * width for width in widths)
    body = ["  ".join(row[i].ljust(widths[i]) for i in range(len(headers))) for row in cells]
    return "\n".join([line, sep, *body])
