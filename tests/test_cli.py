from pathlib import Path

import pytest
from openpyxl import Workbook

from soql_collector.cli import main


@pytest.mark.parametrize(
    "command",
    [
        "import-master",
        "collect",
        "show",
        "list",
        "note",
        "mark",
        "confirm-mapping",
        "build-soql",
        "export-csv",
    ],
)
def test_each_subcommand_help(command: str) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main([command, "--help"])
    assert exc_info.value.code == 0


def test_list_initializes_missing_workbook(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["list"]) == 0
    assert (tmp_path / "data" / "soql_collector.xlsx").exists()


def test_show_missing_returns_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["show", "00O000000000001"]) == 2


def test_note_mark_and_confirm_mapping(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    from soql_collector.store import WorkbookStore

    store = WorkbookStore(tmp_path / "data" / "soql_collector.xlsx")
    store.upsert("Reports", ("レポートID",), {"レポートID": "00O000000000001", "状態": "PENDING"})
    assert main(["note", "00O000000000001", "確認", "待ち"]) == 0
    assert main(["mark", "00O000000000001", "REVIEW"]) == 0
    assert main(["confirm-mapping", "Site", "Account", "A.NAME", "Name", "string"]) == 0
    assert len(store.read_rows("Notes")) == 2


def test_import_master_falls_back_to_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "input" / "master.xlsx"
    source.parent.mkdir()
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.append(["管理番号", "概要", "URL"])
    worksheet.append(["1", "例", "https://example/00O000000000001/view"])
    workbook.save(source)
    (tmp_path / "config.ini").write_text(
        "[FILES]\nMASTER_XLSX_PATH = ./input/master.xlsx\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)

    assert main(["import-master"]) == 0

    from soql_collector.store import WorkbookStore

    store = WorkbookStore(tmp_path / "data" / "soql_collector.xlsx")
    assert store.read_rows("Master")[0]["レポートID"] == "00O000000000001"


def test_import_master_no_path_returns_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["import-master"]) == 2


def test_export_csv_falls_back_to_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "config.ini").write_text(
        "[FILES]\nCSV_PATH = ./output/soql_drafts.csv\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)

    assert main(["export-csv"]) == 0
    assert (tmp_path / "output" / "soql_drafts.csv").exists()


def test_export_csv_no_path_returns_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["export-csv"]) == 2


def test_run_interactive_list_then_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from soql_collector.cli import run_interactive

    monkeypatch.chdir(tmp_path)
    inputs = iter(["3", "", "", "0"])
    monkeypatch.setattr("builtins.input", lambda *_args: next(inputs))
    assert run_interactive() == 0


def test_run_interactive_note_requires_memo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from soql_collector.cli import run_interactive

    monkeypatch.chdir(tmp_path)
    inputs = iter(["5", "00O000000000001", "", "0"])
    monkeypatch.setattr("builtins.input", lambda *_args: next(inputs))
    assert run_interactive() == 0


def test_main_with_empty_argv_runs_interactive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    inputs = iter(["0"])
    monkeypatch.setattr("builtins.input", lambda *_args: next(inputs))
    assert main([]) == 0
