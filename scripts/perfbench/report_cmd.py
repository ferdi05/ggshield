import csv
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median, stdev
from typing import Dict, Iterable, List, Optional, Tuple

import click
from markdown_utils import print_markdown_table
from perfbench_utils import RawReport, get_raw_report_path, work_dir_option


# Do not report changes if the delta is less than this duration
DEFAULT_MIN_DELTA_SECS = 1

# Report a failure if delta is more than this duration
DEFAULT_MAX_DELTA_SECS = 3


@dataclass
class ReportRow:
    command: str
    dataset: str
    # Mapping of version => [durations]
    durations_for_versions: Dict[str, List[float]] = field(default_factory=dict)


@dataclass
class Duration:
    """The duration of benchmark entry. `deviation` can be None if there was only one
    run."""

    value: float
    deviation: Optional[float]


def create_duration_list(
    sorted_versions: List[str], durations_for_versions: Dict[str, List[float]]
) -> List[Duration]:
    """Create a list of Duration for all entries in durations_for_versions.
    The list is ordered according to `sorted_versions`."""
    lst: List[Duration] = []
    for version in sorted_versions:
        durations_for_version = durations_for_versions[version]
        value = median(durations_for_version)

        if len(durations_for_version) > 1:
            deviation = stdev(durations_for_version)
        else:
            deviation = None
        lst.append(Duration(value, deviation))

    return lst


def print_csv_output(
    sorted_versions: List[str],
    rows: Iterable[ReportRow],
):
    writer = csv.writer(sys.stdout)

    # Header row
    headers = ["command", "dataset"]
    for version in sorted_versions:
        headers.extend([version, f"{version} (deviation)"])
    writer.writerow(headers)

    # Data
    for row in sorted(rows, key=lambda x: (x.command, x.dataset)):
        durations = create_duration_list(sorted_versions, row.durations_for_versions)
        table_row = [row.command, row.dataset]
        for duration in durations:
            table_row.append(str(duration.value))
            table_row.append(str(duration.deviation))
        writer.writerow(table_row)


def create_markdown_cells(
    durations: List[Duration],
    min_delta: float,
    max_delta: float,
) -> Tuple[List[str], bool]:
    """Returns a list of cells, and a bool indicating whether we noticed a delta
    higher than MAX_DELTA_SECS"""

    cells: List[str] = []
    reference: Optional[float] = None
    fail = False

    for duration in durations:
        cell = f"{duration.value:.2f}s"

        if duration.deviation:
            cell += f" ±{duration.deviation:.2f}"
        cells.append(cell)

        if reference is None:
            reference = duration.value
        else:
            delta = duration.value - reference
            if abs(delta) > min_delta:
                if delta > max_delta:
                    symbol = "▲" * 3
                    fail = True
                elif delta > 0:
                    symbol = "▲"
                else:
                    symbol = "▼"
            else:
                symbol = "≈"

            cells.append(f"{delta:+.2f} {symbol}")
    return cells, fail


def print_markdown_output(
    sorted_versions: List[str],
    rows: Iterable[ReportRow],
    min_delta: float,
    max_delta: float,
):
    # Create table rows
    table_rows = []
    has_failed = False
    for row in sorted(rows, key=lambda x: (x.command, x.dataset)):
        durations = create_duration_list(sorted_versions, row.durations_for_versions)
        duration_cells, fail = create_markdown_cells(durations, min_delta, max_delta)
        has_failed |= fail
        table_rows.append([row.command, row.dataset, *duration_cells])

    # Create headers (no delta column for reference)
    version_headers = [sorted_versions[0]]
    for version in sorted_versions[1:]:
        version_headers.extend([version, "delta"])

    headers = ["command", "dataset", *version_headers]
    print_markdown_table(
        sys.stdout,
        table_rows,
        headers=headers,
        alignments="LL" + "R" * len(version_headers),
    )

    sys.exit(1 if has_failed else 0)


@click.command()
@click.option(
    "--min-delta",
    type=float,
    help="If the duration difference with the reference run is less than this number of seconds,"
    " do not report a change.",
    default=DEFAULT_MIN_DELTA_SECS,
)
@click.option(
    "--max-delta",
    type=float,
    help="If the duration difference with the reference run is *more* than this number of seconds,"
    " exit with error.",
    default=DEFAULT_MAX_DELTA_SECS,
)
@click.option(
    "--csv",
    "use_csv",
    is_flag=True,
)
@work_dir_option
def report_cmd(
    min_delta: float, max_delta: float, use_csv: bool, work_dir: Path
) -> None:
    """
    Generate a report from a benchmark run
    """
    report_path = get_raw_report_path(work_dir)
    if not report_path.exists():
        logging.error(
            "Raw report not found (%s does not exist). Use the `run` command first",
            report_path,
        )

    # Load raw report file, group report rows by command and dataset
    row_dict: Dict[Tuple[str, str], ReportRow] = {}

    with report_path.open() as fp:
        raw_report = RawReport.load(fp)

    for entry in raw_report.entries:
        row = row_dict.setdefault(
            (entry.command, entry.dataset),
            ReportRow(entry.command, entry.dataset),
        )
        durations = row.durations_for_versions.setdefault(entry.version, [])
        durations.append(entry.duration)

    sorted_versions = raw_report.versions

    if use_csv:
        print_csv_output(sorted_versions, row_dict.values())
    else:
        print_markdown_output(sorted_versions, row_dict.values(), min_delta, max_delta)
