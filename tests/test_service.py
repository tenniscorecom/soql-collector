import csv
import json
from pathlib import Path

from openpyxl import Workbook

from soql_collector.service import build_saved_report, collect_one, export_csv, import_master
from soql_collector.settings import Settings
from soql_collector.store import WorkbookStore


def test_import_master_and_export_empty_csv(tmp_path: Path) -> None:
    source = tmp_path / "master.xlsx"
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.append(["管理番号", "概要", "URL"])
    worksheet.append(["1", "例", "https://example/00O000000000001/view"])
    workbook.save(source)
    store = WorkbookStore(tmp_path / "data.xlsx")
    assert import_master(store, source) == 1
    assert store.read_rows("Master")[0]["レポートID"] == "00O000000000001"
    output = tmp_path / "empty.csv"
    assert export_csv(store, output) == 0
    with output.open(encoding="utf-8-sig", newline="") as file:
        assert list(csv.reader(file))[0][:2] == [
            "フィルタ詳細(生データ)",
            "集計・グルーピング詳細(生データ)",
        ]


def test_build_saved_report_and_apply(tmp_path: Path) -> None:
    store = WorkbookStore(tmp_path / "data.xlsx")
    metadata_path = tmp_path / "describe.json"
    metadata_path.write_text(
        json.dumps(
            {
                "reportMetadata": {
                    "reportFormat": "TABULAR",
                    "reportType": {"type": "Account"},
                    "detailColumns": ["A.NAME"],
                }
            }
        ),
        encoding="utf-8",
    )
    store.upsert(
        "Reports",
        ("レポートID",),
        {
            "レポートID": "00O000000000001",
            "組織": "Site",
            "主オブジェクト": "Account",
            "JSONパス": str(metadata_path),
            "状態": "PENDING",
        },
    )
    store.upsert(
        "FieldMappings",
        ("サイト", "レポートタイプ", "列キー"),
        {
            "サイト": "Site",
            "レポートタイプ": "Account",
            "列キー": "A.NAME",
            "フィールドAPI名": "Name",
            "型": "string",
            "確認状態": "確認済み",
        },
    )
    draft = build_saved_report(store, "00O000000000001", True)
    assert draft is not None and draft.soql == "SELECT Name FROM Account"
    assert store.find_report("00O000000000001")["状態"] == "READY"


def test_collect_preserves_confirmed_mapping(tmp_path: Path) -> None:
    store = WorkbookStore(tmp_path / "data.xlsx")
    store.upsert(
        "FieldMappings",
        ("サイト", "レポートタイプ", "列キー"),
        {
            "サイト": "Site",
            "レポートタイプ": "Account",
            "列キー": "A.NAME",
            "フィールドAPI名": "ConfirmedName",
            "型": "string",
            "確認状態": "確認済み",
        },
    )
    metadata = {
        "reportMetadata": {
            "reportFormat": "TABULAR",
            "reportType": {"type": "Account"},
            "detailColumns": ["A.NAME"],
            "reportFilters": [{"column": "A.NAME", "operator": "equals", "value": "Acme"}],
            "aggregates": ["RowCount"],
            "groupingsDown": [{"name": "A.TYPE", "sortOrder": "Asc"}],
        }
    }

    class Report:
        def describe(self, report_id: str) -> dict:
            return metadata

        def describe_fields_with_object_status(self, source: dict) -> tuple[list[dict], None]:
            return (
                [
                    {
                        "列キー": "A.NAME",
                        "表示名": "名前",
                        "フィールドAPI名": "AutoName",
                        "型": "string",
                        "備考": "",
                    }
                ],
                None,
            )

    class Client:
        report = Report()

        def __enter__(self) -> "Client":
            return self

        def __exit__(self, *args: object) -> None:
            return None

    settings = Settings(tmp_path / "data.xlsx", tmp_path / "json", None, None, "")
    row = collect_one(settings, store, "00O000000000001", "Site", client_factory=Client)
    assert row["SOQLドラフト"] == ("SELECT ConfirmedName FROM Account WHERE ConfirmedName = 'Acme'")
    assert row["フィルタ詳細(生データ)"] == "A.NAME=equals:Acme"
    assert row["集計・グルーピング詳細(生データ)"] == (
        "aggregates: RowCount | groupingsDown: A.TYPE(Asc)"
    )
    assert store.read_rows("FieldMappings")[0]["フィールドAPI名"] == "ConfirmedName"
    output = tmp_path / "reports.csv"
    assert export_csv(store, output) == 1
    with output.open(encoding="utf-8-sig", newline="") as file:
        exported = next(csv.DictReader(file))
    assert exported["フィルタ詳細(生データ)"] == "A.NAME=equals:Acme"
    assert exported["集計・グルーピング詳細(生データ)"] == (
        "aggregates: RowCount | groupingsDown: A.TYPE(Asc)"
    )
