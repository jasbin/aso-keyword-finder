from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import pytest

from keyfinder.export import CSV_COLUMNS, results_to_csv, run_to_json, write_results
from keyfinder.models import AppSummary, KeywordResult, ResearchParams, ResearchRun, RunStatus


def _results() -> list[KeywordResult]:
    return [
        KeywordResult(
            keyword="yoga for beginners",
            popularity=80,
            difficulty=40,
            opportunity=48,
            chars_typed=3,
            position=2,
            top_apps=[
                AppSummary(app_id="1", title="Yoga Daily", ratings=10),
                AppSummary(app_id="2", title="Down, Dog", ratings=5),
            ],
        ),
        KeywordResult(keyword="yoga mat", popularity=5),
    ]


def _parse(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text)))


def test_csv_header_and_rows() -> None:
    rows = _parse(results_to_csv(_results()))
    assert rows[0] == CSV_COLUMNS
    assert rows[1] == ["yoga for beginners", "80", "40", "48", "3", "2", "Yoga Daily | Down, Dog"]
    assert rows[2] == ["yoga mat", "5", "", "", "", "", ""]


def test_csv_empty() -> None:
    assert _parse(results_to_csv([])) == [CSV_COLUMNS]


@pytest.mark.parametrize("prefix", ["=", "+", "-", "@", "\t", "\r"])
def test_csv_formula_injection_guard(prefix: str) -> None:
    payload = f'{prefix}HYPERLINK("http://evil")'
    result = KeywordResult(
        keyword=payload,
        popularity=10,
        top_apps=[AppSummary(app_id="1", title=payload)],
    )
    row = _parse(results_to_csv([result]))[1]
    assert row[0] == "'" + payload
    assert row[6] == "'" + payload


def test_csv_guard_leaves_normal_text_alone() -> None:
    result = KeywordResult(keyword="a=b", popularity=10, top_apps=[AppSummary(app_id="1", title="x-y")])
    row = _parse(results_to_csv([result]))[1]
    assert row[0] == "a=b"
    assert row[6] == "x-y"


def _run() -> ResearchRun:
    return ResearchRun(
        id="abc123",
        params=ResearchParams(seed="Yoga"),
        status=RunStatus.SUCCEEDED,
        results=_results(),
    )


def test_run_to_json_round_trips() -> None:
    run = _run()
    text = run_to_json(run)
    assert "\n  " in text  # pretty-printed
    assert ResearchRun.model_validate_json(text) == run
    assert json.loads(text)["params"]["seed"] == "yoga"


def test_write_csv(tmp_path: Path) -> None:
    path = tmp_path / "out" / "kw.CSV"
    write_results(path, _results())
    assert path.read_bytes().decode("utf-8") == results_to_csv(_results())
    assert list(path.parent.iterdir()) == [path]  # no temp files left behind


def test_write_json_with_run(tmp_path: Path) -> None:
    path = tmp_path / "run.json"
    run = _run()
    write_results(path, run.results, run)
    assert ResearchRun.model_validate_json(path.read_text(encoding="utf-8")) == run


def test_write_json_without_run(tmp_path: Path) -> None:
    path = tmp_path / "kw.json"
    write_results(path, _results())
    data = json.loads(path.read_text(encoding="utf-8"))
    assert [KeywordResult.model_validate(item) for item in data] == _results()


def test_write_overwrites_existing(tmp_path: Path) -> None:
    path = tmp_path / "kw.csv"
    path.write_text("old", encoding="utf-8")
    write_results(path, [])
    assert path.read_text(encoding="utf-8").startswith("keyword,")


@pytest.mark.parametrize("name", ["kw.txt", "kw", "kw.xlsx"])
def test_write_rejects_unknown_suffix(tmp_path: Path, name: str) -> None:
    with pytest.raises(ValueError, match=r"\.csv or \.json"):
        write_results(tmp_path / name, _results())
    assert not (tmp_path / name).exists()
