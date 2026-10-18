"""``tables`` のテスト。 ``ReportRecord`` を直接作って、 ``SOQL調査.xlsx`` と
``列名の対応.csv`` の中身を厳密に確かめる (ネットワークには出ない) 。

シート構成: 3 枚 （一覧 / 列の対応 / 関係図） 、 すべて Excel のテーブル （構造化） 。
"""

from __future__ import annotations

import csv
import dataclasses
from datetime import UTC, datetime
from pathlib import Path

import pytest
from openpyxl import load_workbook

from src.fetch import RelationRecord, ReportRecord
from src.tables import (
    COLUMN_NAMES_COLUMNS,
    COLUMN_NAMES_CSV_NAME,
    COLUMNS_SHEET_COLUMNS,
    RELATION_DIAGRAM_COLUMNS,
    SHEET_COLUMNS,
    SHEET_OVERVIEW,
    SHEET_RELATION_DIAGRAM,
    SURVEY_COLUMNS,
    TABLE_COLUMNS,
    TABLE_DIAGRAM,
    TABLE_OVERVIEW,
    XLSX_NAME,
    run_tables,
)

# ── ヘルパー ─────────────────────────────────────────────────────────────


def _make_record(
    key: str = "1001",
    *,
    main_object: str | None = "Opportunity",
    summary: str | None = None,
    report_id: str = "00O000000000001",
    url: str = "https://example.my.salesforce.com/lightning/r/Report/00O000000000001/view",
    warnings: tuple[str, ...] = (),
) -> ReportRecord:
    """典型的な ``ReportRecord`` を作る。"""
    summary_text = summary if summary is not None else f"テスト{key}"
    objects: dict[str, dict] = {}
    if main_object:
        objects[main_object] = {
            "name": main_object,
            "label": "商談",
            "custom": False,
            "fields": [
                {
                    "name": "AccountId",
                    "label": "取引先",
                    "type": "reference",
                    "custom": False,
                    "referenceTo": ["Account"],
                    "relationshipName": "Account",
                    "picklist": [],
                    "picklistTotal": 0,
                },
                {
                    "name": "WhoId",
                    "label": "担当者",
                    "type": "reference",
                    "custom": False,
                    "referenceTo": ["Contact", "Lead"],
                    "relationshipName": None,
                    "picklist": [],
                    "picklistTotal": 0,
                },
                {
                    "name": "Custom__c",
                    "label": "カスタム項目",
                    "type": "picklist",
                    "custom": True,
                    "referenceTo": [],
                    "relationshipName": None,
                    "picklist": ["A", "B", "D"],
                    "picklistTotal": 3,
                },
                {
                    "name": "Name",
                    "label": "商談名",
                    "type": "string",
                    "custom": False,
                    "referenceTo": [],
                    "relationshipName": None,
                    "picklist": [],
                    "picklistTotal": 0,
                },
            ],
        }
    column_map: list[dict[str, str]] = [
        {
            "列キー": "Opp.Account",
            "表示名": "取引先名",
            "所属オブジェクト": "Opportunity",
            "対応フィールドAPI名": "AccountId",
            "型": "reference",
            "参照先オブジェクト": "",
            "リレーション名": "",
            "備考": "",
        },
        {
            "列キー": "Opp.LeadOrContact",
            "表示名": "担当者名",
            "所属オブジェクト": "Opportunity",
            "対応フィールドAPI名": "WhoId",
            "型": "reference",
            "参照先オブジェクト": "",
            "リレーション名": "",
            "備考": "ポリモーフィック",
        },
        {
            "列キー": "Opp.Custom",
            "表示名": "カスタム",
            "所属オブジェクト": "Opportunity",
            "対応フィールドAPI名": "Custom__c",
            "型": "picklist",
            "参照先オブジェクト": "",
            "リレーション名": "",
            "備考": "",
        },
        {
            "列キー": "Opp.Name",
            "表示名": "商談名",
            "所属オブジェクト": "Opportunity",
            "対応フィールドAPI名": "Name",
            "型": "string",
            "参照先オブジェクト": "",
            "リレーション名": "",
            "備考": "",
        },
        {
            "列キー": "Opp.Unknown",
            "表示名": "不明列",
            "所属オブジェクト": "",
            "対応フィールドAPI名": "(不明)",
            "型": "",
            "参照先オブジェクト": "",
            "リレーション名": "",
            "備考": "表示名一致せず",
        },
    ]
    report = {
        "reportMetadata": {
            "reportType": {"type": main_object or "Opportunity"},
            "reportFormat": "TABULAR",
        },
        "reportExtendedMetadata": {},
    }
    return ReportRecord(
        key=key,
        summary=summary_text,
        report_id=report_id,
        url=url,
        main_object=main_object,
        report=report,
        objects=objects,
        relations=(),
        column_map=column_map,
        warnings=warnings,
    )


def _read_xlsx(path: Path) -> dict[str, list[dict[str, object]]]:
    """``SOQL調査.xlsx`` を ``{シート名: [行 dict, ...]}`` で読む。"""
    workbook = load_workbook(path)
    result: dict[str, list[dict[str, object]]] = {}
    for sheet_name in workbook.sheetnames:
        sheet = workbook[sheet_name]
        rows_iter = sheet.iter_rows(values_only=True)
        try:
            headers = list(next(rows_iter))
        except StopIteration:
            result[sheet_name] = []
            continue
        result[sheet_name] = [
            {
                str(header): ("" if value is None else value)
                for header, value in zip(headers, row, strict=False)
            }
            for row in rows_iter
        ]
    workbook.close()
    return result


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    """CSV を ``(columns, rows)`` で読む。"""
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.reader(file)
        columns = next(reader)
        rows = [dict(zip(columns, line, strict=True)) for line in reader]
    return columns, rows


# ── xlsx の出力ファイル構成 ───────────────────────────────────────────────


def test_output_files_are_only_xlsx_and_column_names_csv(tmp_path: Path) -> None:
    """出力フォルダには ``SOQL調査.xlsx`` と ``列名の対応.csv`` だけができる。"""
    output = tmp_path / "output"
    output.mkdir()
    record = _make_record("1001", main_object="Opportunity")

    run_tables(output, [record])

    files = sorted(p.name for p in output.iterdir() if p.is_file())
    assert files == sorted([COLUMN_NAMES_CSV_NAME, XLSX_NAME])


def test_empty_records_writes_only_column_names_csv(tmp_path: Path) -> None:
    """``record`` が無いと ``SOQL調査.xlsx`` は作らず、 ``列名の対応.csv`` も何もしない。"""
    output = tmp_path / "output"
    output.mkdir()

    run_tables(output, [])

    assert not (output / XLSX_NAME).exists()
    assert not (output / COLUMN_NAMES_CSV_NAME).exists()


# ── シート構成 ─────────────────────────────────────────────────────────────


def test_sheet_names_in_order(tmp_path: Path) -> None:
    """xlsx のシートはちょうど 3 枚で、 指定通りの順。"""
    output = tmp_path / "output"
    output.mkdir()
    record = _make_record("1001")

    run_tables(output, [record])

    workbook = load_workbook(output / XLSX_NAME)
    sheet_names = workbook.sheetnames
    workbook.close()

    assert sheet_names == [
        SHEET_OVERVIEW,
        SHEET_COLUMNS,
        SHEET_RELATION_DIAGRAM,
    ]


def test_each_sheet_has_excel_table(tmp_path: Path) -> None:
    """3 枚それぞれに Excel のテーブル （構造化） がある。"""
    output = tmp_path / "output"
    output.mkdir()
    record = _make_record("1001", main_object="Opportunity")
    record = dataclasses.replace(
        record,
        relations=(
            RelationRecord(
                parent="Opportunity",
                field="AccountId",
                relationship_name="Account",
                child="Account",
                depth=1,
            ),
        ),
    )

    run_tables(output, [record])

    workbook = load_workbook(output / XLSX_NAME)
    table_names: set[str] = set()
    for ws in workbook.worksheets:
        table_names.update(ws.tables.keys())
    workbook.close()

    # comken が ``PY_T_`` プレフィックスを付けるので、 テーブルの有無を
    # 末尾一致でも確認する
    workbook = load_workbook(output / XLSX_NAME)
    for ws in workbook.worksheets:
        for name in ws.tables:
            # name は comken の ``PY_T_<name>`` 形式
            short = name
            if short.startswith("PY_T_"):
                short = short[len("PY_T_") :]
            if short in (TABLE_OVERVIEW, TABLE_COLUMNS, TABLE_DIAGRAM):
                table_names.add(short)
    workbook.close()
    assert TABLE_OVERVIEW in table_names
    assert TABLE_COLUMNS in table_names
    assert TABLE_DIAGRAM in table_names


# ── 一覧シート ────────────────────────────────────────────────────────────


def test_survey_basic(tmp_path: Path) -> None:
    """一覧シートに 1 レポート 1 行が出る。 必要項目が埋まる。"""
    output = tmp_path / "output"
    output.mkdir()
    record = _make_record("1001", main_object="Opportunity")

    run_tables(output, [record])

    workbook = load_workbook(output / XLSX_NAME)
    ws = workbook[SHEET_OVERVIEW]
    # 3 行目から見出し、 4 行目以降にデータ
    rows_iter = ws.iter_rows(min_row=4, values_only=True)
    headers = [c.value for c in ws[3]]
    data_rows = list(rows_iter)
    workbook.close()

    assert headers == list(SURVEY_COLUMNS)
    assert len(data_rows) == 1
    row = dict(zip(headers, data_rows[0], strict=False))
    assert row["管理番号"] == "1001"
    assert row["主オブジェクト"] == "Opportunity"
    assert row["要対応・要確認の列数"] == "1"
    assert row["SOQL資料"] == "SOQL資料/1001.md"


def test_survey_similar_group_text(tmp_path: Path) -> None:
    """似たレポート判定で 1 グループになると 一覧シートの 「似たレポート」 列に
    「グループN: <基準の管理番号> が基準」 の形で出る。"""

    output = tmp_path / "output"
    output.mkdir()
    record_1001 = _make_record("1001", main_object="Opportunity")
    record_1002 = _make_record("1002", main_object="Opportunity")
    # 同じ構造・同じ条件にすれば 「同一」 判定で 1 グループになる
    run_tables(output, [record_1001, record_1002])

    workbook = load_workbook(output / XLSX_NAME)
    ws = workbook[SHEET_OVERVIEW]
    rows_iter = list(ws.iter_rows(min_row=4, values_only=True))
    headers = [c.value for c in ws[3]]
    workbook.close()
    rows = [dict(zip(headers, row, strict=False)) for row in rows_iter]
    for row in rows:
        if row["管理番号"] == "1001":
            assert str(row["似たレポート"]).startswith("グループ")
        else:
            assert str(row["似たレポート"]).startswith("グループ")


def test_survey_main_object_unknown(tmp_path: Path) -> None:
    """主オブジェクトが ``None`` のときは ``主オブジェクト`` 列が ``(不明)`` になる。"""
    output = tmp_path / "output"
    output.mkdir()
    record = _make_record("1001", main_object=None)

    run_tables(output, [record])

    workbook = load_workbook(output / XLSX_NAME)
    ws = workbook[SHEET_OVERVIEW]
    rows_iter = list(ws.iter_rows(min_row=4, values_only=True))
    headers = [c.value for c in ws[3]]
    workbook.close()
    rows = [dict(zip(headers, row, strict=False)) for row in rows_iter]
    assert rows[0]["主オブジェクト"] == "(不明)"


def test_survey_includes_unresolved_count(tmp_path: Path) -> None:
    """要対応・要確認の列数が出る。"""
    output = tmp_path / "output"
    output.mkdir()
    record = _make_record("1001", main_object="Opportunity")

    run_tables(output, [record])

    workbook = load_workbook(output / XLSX_NAME)
    ws = workbook[SHEET_OVERVIEW]
    rows_iter = list(ws.iter_rows(min_row=4, values_only=True))
    headers = [c.value for c in ws[3]]
    workbook.close()
    rows = [dict(zip(headers, row, strict=False)) for row in rows_iter]
    assert rows[0]["要対応・要確認の列数"] == "1"


# ── 列の対応シート ──────────────────────────────────────────────────────


def test_columns_sheet_basic(tmp_path: Path) -> None:
    """列の対応シートに 1 レポート 1 列ずつ出る。 列順は 管理番号 → 列キー。"""
    output = tmp_path / "output"
    output.mkdir()
    record = _make_record("1001", main_object="Opportunity")

    run_tables(output, [record])

    workbook = load_workbook(output / XLSX_NAME)
    ws = workbook[SHEET_COLUMNS]
    rows_iter = list(ws.iter_rows(min_row=4, values_only=True))
    headers = [c.value for c in ws[3]]
    workbook.close()
    rows = [dict(zip(headers, row, strict=False)) for row in rows_iter]

    assert headers == list(COLUMNS_SHEET_COLUMNS)
    # (不明) の列も出る
    unknown_rows = [r for r in rows if r["列キー"] == "Opp.Unknown"]
    assert len(unknown_rows) == 1
    assert unknown_rows[0]["状態"] == "要対応"
    assert unknown_rows[0]["使うSOQL列名"] == "(不明)"
    # 解決できた列
    name_rows = [r for r in rows if r["列キー"] == "Opp.Name"]
    assert len(name_rows) == 1
    assert name_rows[0]["状態"] == "OK"
    assert name_rows[0]["使うSOQL列名"] == "Name"
    assert name_rows[0]["どこから"] == "自動"


def test_columns_sheet_unresolved_status_is_needs_check_for_candidates(tmp_path: Path) -> None:
    """候補が複数ある列は状態=要確認。"""
    output = tmp_path / "output"
    output.mkdir()
    record = _make_record("1001", main_object="Opportunity")
    # 候補ありの注記を足す
    for col in record.column_map:
        if col["列キー"] == "Opp.Unknown":
            col["備考"] = (
                "複数候補あり: Name（Opportunity 商談名）/ Custom__c（Opportunity 商談名）"
            )
            break

    run_tables(output, [record])

    workbook = load_workbook(output / XLSX_NAME)
    ws = workbook[SHEET_COLUMNS]
    rows_iter = list(ws.iter_rows(min_row=4, values_only=True))
    headers = [c.value for c in ws[3]]
    workbook.close()
    rows = [dict(zip(headers, row, strict=False)) for row in rows_iter]
    unknown_rows = [r for r in rows if r["列キー"] == "Opp.Unknown"]
    assert unknown_rows[0]["状態"] == "要確認"
    assert "Name" in str(unknown_rows[0]["候補"])


def test_columns_sheet_user_fixed_soql(tmp_path: Path) -> None:
    """「直したSOQL列名」 由来の値が入ると 「どこから=人が直した」 になる。"""
    output = tmp_path / "output"
    output.mkdir()
    record = _make_record("1001", main_object="Opportunity")
    for col in record.column_map:
        if col["列キー"] == "Opp.Unknown":
            col["対応フィールドAPI名"] = "CustomField__c"
            col["備考"] = "直したSOQL列名（人が直した）"
            break

    run_tables(output, [record])

    workbook = load_workbook(output / XLSX_NAME)
    ws = workbook[SHEET_COLUMNS]
    rows_iter = list(ws.iter_rows(min_row=4, values_only=True))
    headers = [c.value for c in ws[3]]
    workbook.close()
    rows = [dict(zip(headers, row, strict=False)) for row in rows_iter]
    unknown_rows = [r for r in rows if r["列キー"] == "Opp.Unknown"]
    assert unknown_rows[0]["状態"] == "OK"
    assert unknown_rows[0]["どこから"] == "人が直した"
    assert unknown_rows[0]["使うSOQL列名"] == "CustomField__c"
    # 自動の値 列は 人が直して自動と違うときだけ埋める （このケースは同じ） ので空
    assert (unknown_rows[0]["自動の値"] or "") == ""


# ── 関係図シート ──────────────────────────────────────────────────────


def test_relation_diagram_sheet_columns_match(tmp_path: Path) -> None:
    """関係図シートの列順。"""
    output = tmp_path / "output"
    output.mkdir()
    record = _make_record("1001")
    record = dataclasses.replace(
        record,
        main_object="Opportunity",
        relations=(
            RelationRecord(
                parent="Opportunity",
                field="AccountId",
                relationship_name="Account",
                child="Account",
                depth=1,
            ),
        ),
    )

    run_tables(output, [record])

    workbook = load_workbook(output / XLSX_NAME)
    ws = workbook[SHEET_RELATION_DIAGRAM]
    headers = [c.value for c in ws[1]]
    workbook.close()
    assert headers == list(RELATION_DIAGRAM_COLUMNS)


def test_relation_diagram_sheet_writes_tree_with_polymorphic(tmp_path: Path) -> None:
    """Task → Account / Opportunity のポリモーフィックが 1 行ずつ出、 段が上がる。"""
    output = tmp_path / "output"
    output.mkdir()
    record = _make_record("1001")
    record = dataclasses.replace(
        record,
        main_object="Task",
        relations=(
            RelationRecord(
                parent="Task", field="WhatId", relationship_name="What", child="Account", depth=1
            ),
            RelationRecord(
                parent="Task",
                field="WhatId",
                relationship_name="What",
                child="Opportunity",
                depth=1,
            ),
        ),
    )

    run_tables(output, [record])

    workbook = load_workbook(output / XLSX_NAME)
    ws = workbook[SHEET_RELATION_DIAGRAM]
    rows = list(ws.iter_rows(min_row=2, values_only=True))
    headers = [c.value for c in ws[1]]
    workbook.close()
    data = [dict(zip(headers, row, strict=False)) for row in rows]
    task_row = next(r for r in data if r["主オブジェクト"] == "Task" and r["段"] == "0")
    assert task_row["行き先オブジェクト"] == "Task"
    children = [r for r in data if r["主オブジェクト"] == "Task" and r["段"] == "1"]
    targets = {str(r["行き先オブジェクト"]) for r in children}
    assert targets == {"Account", "Opportunity"}


def _task_record_with_account_relation() -> ReportRecord:
    """Task → Account の関係を 1 本持つ record（関係図の Markdown が出る最小の入力）。"""
    return dataclasses.replace(
        _make_record("1001"),
        main_object="Task",
        relations=(
            RelationRecord(
                parent="Task", field="WhatId", relationship_name="What", child="Account", depth=1
            ),
        ),
    )


def test_run_tables_writes_relation_diagram_markdown(tmp_path: Path) -> None:
    """関係図の Markdown が ``関係図/{主オブジェクト}.md`` に書かれ、 資料からリンクされる。"""
    output = tmp_path / "output"
    output.mkdir()

    outcome = run_tables(output, [_task_record_with_account_relation()])

    diagram_path = output / "関係図" / "Task.md"
    assert diagram_path in outcome.wrote
    assert "flowchart LR" in diagram_path.read_text(encoding="utf-8")
    # 資料から関係図への相対リンクが、 実際にあるファイルを指す
    material = (output / "SOQL資料" / "1001.md").read_text(encoding="utf-8")
    assert "../関係図/Task.md" in material
    assert (output / "SOQL資料" / "../関係図/Task.md").resolve().exists()


def test_run_tables_without_main_object_writes_no_diagram_directory(tmp_path: Path) -> None:
    """主オブジェクトが分からない record だけのときは、 関係図のフォルダを作らない。"""
    output = tmp_path / "output"
    output.mkdir()
    record = dataclasses.replace(_make_record("1001"), main_object=None)

    run_tables(output, [record])

    assert not (output / "関係図").exists()


def test_run_tables_uses_configured_markdown_and_diagram_dirs(tmp_path: Path) -> None:
    """``MARKDOWN_DIR`` / ``DIAGRAM_DIR`` の値が、 出力先・リンク・一覧の列に反映される。"""
    output = tmp_path / "output"
    output.mkdir()

    run_tables(
        output,
        [_task_record_with_account_relation()],
        markdown_dir="資料",
        diagram_dir="図",
    )

    assert (output / "図" / "Task.md").exists()
    material = (output / "資料" / "1001.md").read_text(encoding="utf-8")
    assert "../図/Task.md" in material
    # 既定のフォルダ名では書かない
    assert not (output / "関係図").exists()
    assert not (output / "SOQL資料").exists()
    # 一覧シートの「SOQL資料」列も、 設定したフォルダ名の相対パスになる
    workbook = load_workbook(output / XLSX_NAME)
    sheet = workbook[SHEET_OVERVIEW]
    headers = [c.value for c in sheet[3]]  # 見出しは 3 行目（1〜2 行目は注記）
    values = [c.value for c in sheet[4]]
    workbook.close()
    assert dict(zip(headers, values, strict=False))["SOQL資料"] == "資料/1001.md"


# ── Excel テーブル・列幅・固定行 ──────────────────────────────────────


def test_overview_sheet_freeze_panes_and_width(tmp_path: Path) -> None:
    """一覧シートは 3 行目からテーブルで、 見出しが固定され、 列幅が設定される。"""
    output = tmp_path / "output"
    output.mkdir()
    record = _make_record("1001", main_object="Opportunity")

    run_tables(output, [record])

    workbook = load_workbook(output / XLSX_NAME)
    ws = workbook[SHEET_OVERVIEW]
    # freeze_panes が A3 （3 行目からスクロール、 1 行目・ 2 行目が見出し）
    assert ws.freeze_panes == "A3"
    # 列幅が設定されている （何列かが 8 以上）
    widths = [
        ws.column_dimensions[col].width
        for col in ws.column_dimensions
        if ws.column_dimensions[col].width is not None
    ]
    assert any(w >= 8 for w in widths)
    workbook.close()


def test_relation_diagram_sheet_freeze_panes(tmp_path: Path) -> None:
    """関係図シートは 1 行目から見出しで、 2 行目以降をスクロール。"""
    output = tmp_path / "output"
    output.mkdir()
    record = _make_record("1001")
    record = dataclasses.replace(
        record,
        main_object="Opportunity",
        relations=(
            RelationRecord(
                parent="Opportunity",
                field="AccountId",
                relationship_name="Account",
                child="Account",
                depth=1,
            ),
        ),
    )

    run_tables(output, [record])

    workbook = load_workbook(output / XLSX_NAME)
    ws = workbook[SHEET_RELATION_DIAGRAM]
    assert ws.freeze_panes == "A2"
    workbook.close()


# ── 列名の対応.csv ──────────────────────────────────────────────────────


def test_column_names_csv_new_schema(tmp_path: Path) -> None:
    """``列名の対応.csv`` は新しい見出しで出力される。"""
    output = tmp_path / "output"
    output.mkdir()
    record = _make_record("1001")

    run_tables(output, [record])

    columns, rows = _read_csv(output / COLUMN_NAMES_CSV_NAME)
    assert columns == list(COLUMN_NAMES_COLUMNS)
    # (不明) は除く
    assert all(r["SOQL列名"] != "(不明)" for r in rows)
    soql_names = {r["SOQL列名"] for r in rows}
    assert soql_names == {"AccountId", "WhoId", "Custom__c", "Name"}


def test_column_names_csv_updated_date(tmp_path: Path) -> None:
    """``更新日`` 列が ``YYYY-MM-DD`` で埋まる。"""
    output = tmp_path / "output"
    output.mkdir()
    record = _make_record("1001")

    run_tables(output, [record])

    _, rows = _read_csv(output / COLUMN_NAMES_CSV_NAME)
    assert all(r["更新日"] == datetime.now(tz=UTC).astimezone().date().isoformat() for r in rows)


def test_column_names_csv_no_management_key_column(tmp_path: Path) -> None:
    """``列名の対応.csv`` には管理番号の列が無い ( 1 行 = (主, SOQL) ) 。"""
    output = tmp_path / "output"
    output.mkdir()
    record = _make_record("1001")

    run_tables(output, [record])

    columns, _ = _read_csv(output / COLUMN_NAMES_CSV_NAME)
    assert "管理番号" not in columns


def test_column_names_csv_fails_on_old_header(tmp_path: Path) -> None:
    """旧形式の見出しを持つ既存ファイルは CSVError、 既存ファイルは変更されない。"""
    from comken.exceptions import CSVError

    output = tmp_path / "output"
    output.mkdir()

    existing_path = output / COLUMN_NAMES_CSV_NAME
    old_format = "管理番号,概要,SOQL列名,表示名,備考\r\n1001,テスト,AccountId,取引先名,\r\n"
    existing_path.write_bytes(old_format.encode("utf-8"))
    before = existing_path.read_bytes()

    record = _make_record("1001", main_object="Opportunity")

    with pytest.raises(CSVError):
        run_tables(output, [record])

    assert existing_path.read_bytes() == before


def test_column_names_csv_keeps_old_when_no_records(tmp_path: Path) -> None:
    """record が無いときは CSV を何もしない。"""
    output = tmp_path / "output"
    output.mkdir()

    existing_path = output / COLUMN_NAMES_CSV_NAME
    existing_path.write_bytes(
        "主オブジェクト,SOQL列名,表示名,備考,更新日\r\n"
        "Case,Case.AccountId,取引先ID,,2025-01-01\r\n".encode()
    )
    before = existing_path.read_bytes()

    run_tables(output, [])

    assert existing_path.read_bytes() == before


# ── 出力ディレクトリ未作成 ──────────────────────────────────────────────


def test_output_dir_missing_does_not_raise(tmp_path: Path) -> None:
    """``output_dir`` が無くても例外を出さない。"""
    output = tmp_path / "missing"  # 存在しない
    assert not output.exists()
    record = _make_record("1001")

    run_tables(output, [record])

    assert output.exists()
    assert (output / XLSX_NAME).exists()
    assert (output / COLUMN_NAMES_CSV_NAME).exists()
