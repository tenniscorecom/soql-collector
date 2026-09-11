from pathlib import Path

import pytest

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
