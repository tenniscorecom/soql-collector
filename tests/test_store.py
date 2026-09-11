from pathlib import Path

from soql_collector.store import SHEETS, WorkbookStore


def test_workbook_initializes_and_round_trips(tmp_path: Path) -> None:
    store = WorkbookStore(tmp_path / "collector.xlsx")
    assert set(SHEETS) == {"Master", "Reports", "Filters", "Groupings", "FieldMappings", "Notes"}
    store.upsert("Master", ("レポートID",), {"レポートID": "00O000000000001", "概要": "例"})
    assert store.read_rows("Master")[0]["概要"] == "例"
