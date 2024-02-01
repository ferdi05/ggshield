from typing import Iterable, List, TextIO


def print_markdown_table(
    out: TextIO, rows: List[List[str]], headers: List[str], alignments: str
) -> None:
    """
    Prints a Markdown table.

    `alignments` is a string of one character per column. The character must be L or R,
    defining left or right alignment.
    """
    assert len(headers) == len(alignments)

    widths = [0] * len(headers)
    for row in [headers, *rows]:
        for idx, cell in enumerate(row):
            width = len(cell)
            widths[idx] = max(widths[idx], width)

    def print_row(row: Iterable[str]) -> None:
        for cell, alignment, width in zip(row, alignments, widths):
            if alignment == "R":
                cell = cell.rjust(width)
            else:
                cell = cell.ljust(width)
            out.write(f"| {cell} ")
        out.write("|\n")

    # print rows
    print_row(headers)
    separator_row = [
        "-" * (w - 1) + (":" if a == "R" else "-") for a, w in zip(alignments, widths)
    ]
    print_row(separator_row)

    for row in rows:
        print_row(row)
