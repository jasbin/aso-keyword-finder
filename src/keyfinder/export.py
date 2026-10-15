"""Export research results as CSV or JSON."""

from __future__ import annotations

import csv
import io
import json
import os
import tempfile
from collections.abc import Sequence
from pathlib import Path

from keyfinder.models import KeywordResult, ResearchRun

__all__ = ["CSV_COLUMNS", "results_to_csv", "run_to_json", "write_results"]

CSV_COLUMNS = ["keyword", "popularity", "difficulty", "opportunity", "chars_typed", "position", "top_apps"]

# Leading characters that make spreadsheet apps evaluate a cell as a formula (CSV injection).
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _safe_cell(value: str | int | None) -> str | int:
    """Render one CSV cell; untrusted text that would start a formula is prefixed with ``'``."""
    if value is None:
        return ""
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value


def results_to_csv(results: Sequence[KeywordResult]) -> str:
    """CSV with :data:`CSV_COLUMNS`; ``top_apps`` holds the app titles joined by ``" | "``."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(CSV_COLUMNS)
    for result in results:
        row: list[str | int | None] = [
            result.keyword,
            result.popularity,
            result.difficulty,
            result.opportunity,
            result.chars_typed,
            result.position,
            " | ".join(app.title for app in result.top_apps),
        ]
        writer.writerow([_safe_cell(cell) for cell in row])
    return buffer.getvalue()


def run_to_json(run: ResearchRun) -> str:
    """The whole run (params, status, progress, results) as pretty-printed JSON."""
    return run.model_dump_json(indent=2)


def _results_to_json(results: Sequence[KeywordResult]) -> str:
    return json.dumps([result.model_dump(mode="json") for result in results], indent=2, ensure_ascii=False)


def _atomic_write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` via a temp file + rename, so readers never see a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def write_results(path: Path, results: Sequence[KeywordResult], run: ResearchRun | None = None) -> None:
    """Write results to ``path``, choosing the format from its suffix (``.csv`` or ``.json``).

    JSON output is the full run when ``run`` is given, otherwise a list of keyword results.
    Raises ``ValueError`` for any other suffix.
    """
    suffix = path.suffix.lower()
    if suffix == ".csv":
        text = results_to_csv(results)
    elif suffix == ".json":
        text = run_to_json(run) if run is not None else _results_to_json(results)
    else:
        raise ValueError(f"unsupported export format {path.suffix or '(none)'!r}: use a .csv or .json file")
    _atomic_write(path, text)
