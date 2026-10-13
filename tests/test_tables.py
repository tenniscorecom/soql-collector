"""``tables`` のテスト。 ``ReportRecord`` を直接作って、 ``SOQL調査.xlsx`` と
``列名の対応.csv`` の中身を厳密に確かめる ( ネットワークには出ない ) 。
"""

from __future__ import annotations

import csv
import dataclasses
from datetime import UTC, datetime
from pathlib import Path

import pytest
from openpyxl import load_workbook

from src.fetch import ReportRecord
from src.tables import (
    AGGREGATE_COLUMNS,
    COLUMN_NAMES_COLUMNS,
    COLUMN_NAMES_CSV_NAME,
    CONDITION_COLUMNS,
    FIELD_COLUMNS,
    RELATED_COLUMNS,
    SHEET_AGGREGATE,
    SHEET_CONDITION,
    SHEET_FIELDS,
    SHEET_OVERVIEW,
    SHEET_RELATED,
    SHEET_RESOLVED,
    SHEET_SIMILAR,
    SHEET_UNRESOLVED,
    SHEET_WARNING,
    SIMILAR_COLUMNS,
    SURVEY_COLUMNS,
    WARNING_COLUMNS,
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
    """典型的な ``ReportRecord`` を作る。

    参照項目・ポリモーフィック・カスタム項目・選択肢を含む小さい
    ``Opportunity`` describe と、 ``column_map`` を持つ。
    """
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


def _read_xlsx(path: Path) -> dict[str, list[dict[str, str]]]:
    """``SOQL調査.xlsx`` を ``{シート名: [行 dict, ...]}`` で読む。

    空セルは ``None`` ではなく空文字として扱う ( テストで ``in`` や ``==`` を
    使えるようにするため ) 。
    """
    workbook = load_workbook(path)
    result: dict[str, list[dict[str, str]]] = {}
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
                header: ("" if value is None else value)
                for header, value in zip(headers, row, strict=False)
            }
            for row in rows_iter
        ]
    workbook.close()
    return result


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    """CSV を ``(columns, rows)`` で読む。 ``csv.DictReader`` 経由。"""
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
    """xlsx のシート名と順番が指定通り。"""
    output = tmp_path / "output"
    output.mkdir()
    record = _make_record("1001")

    run_tables(output, [record])

    workbook = load_workbook(output / XLSX_NAME)
    sheet_names = workbook.sheetnames
    workbook.close()

    assert sheet_names == [
        SHEET_OVERVIEW,
        SHEET_UNRESOLVED,
        SHEET_RESOLVED,
        SHEET_CONDITION,
        SHEET_AGGREGATE,
        SHEET_SIMILAR,
        SHEET_WARNING,
        SHEET_FIELDS,
        SHEET_RELATED,
    ]


# ── 一覧シート ────────────────────────────────────────────────────────────


def test_survey_basic(tmp_path: Path) -> None:
    """一覧シートに 1 レポート 1 行が出る。 取得できなかった項目数と警告数も出る。"""
    output = tmp_path / "output"
    output.mkdir()
    record = _make_record("1001", main_object="Opportunity")

    run_tables(output, [record])

    sheets = _read_xlsx(output / XLSX_NAME)
    overview = sheets[SHEET_OVERVIEW]
    assert len(overview) == 1
    row = overview[0]
    assert row["管理番号"] == "1001"
    assert row["主オブジェクト"] == "Opportunity"
    assert row["レポートタイプ"] == "Opportunity"
    assert row["形式"] == "TABULAR"
    assert row["取得できなかった項目数"] == "1"
    assert row["警告数"] == "0"
    assert list(row.keys()) == list(SURVEY_COLUMNS)


def test_survey_includes_warning_and_unresolved_counts(tmp_path: Path) -> None:
    """警告と取得できなかった項目があると、 一覧シートに 件数が出る。"""
    output = tmp_path / "output"
    output.mkdir()
    record = _make_record(
        "1001",
        main_object="Opportunity",
        warnings=(
            "オブジェクト Account の describe に失敗（HTTP 500: サーバーエラー）",
            "上限超過",
        ),
    )

    run_tables(output, [record])

    sheets = _read_xlsx(output / XLSX_NAME)
    row = sheets[SHEET_OVERVIEW][0]
    assert row["取得できなかった項目数"] == "2"  # (不明) 列 1 + Account describe 失敗 1
    assert row["警告数"] == "2"


def test_survey_main_object_unknown(tmp_path: Path) -> None:
    """主オブジェクトが ``None`` のときは ``主オブジェクト`` 列が ``(不明)`` になる。"""
    output = tmp_path / "output"
    output.mkdir()
    record = _make_record("1001", main_object=None)
    record.objects["RelatedOnly__c"] = {
        "name": "RelatedOnly__c",
        "label": "関連のみ",
        "custom": True,
        "fields": [{"name": "Name__c", "label": "名前", "type": "string"}],
    }

    run_tables(output, [record])

    sheets = _read_xlsx(output / XLSX_NAME)
    row = sheets[SHEET_OVERVIEW][0]
    assert row["主オブジェクト"] == "(不明)"
    assert row["管理番号"] == "1001"


# ── 取得できなかった項目シート ─────────────────────────────────────────────


def test_unresolved_dedup_by_key_reason(tmp_path: Path) -> None:
    """同じ ``(主オブジェクト, 列キー, 理由)`` が 2 レポートに出ても 1 行、 件数 2。"""
    output = tmp_path / "output"
    output.mkdir()
    record_1001 = _make_record("1001", main_object="Opportunity")
    record_1002 = _make_record("1002", main_object="Opportunity")

    run_tables(output, [record_1001, record_1002])

    sheets = _read_xlsx(output / XLSX_NAME)
    unresolved = sheets[SHEET_UNRESOLVED]
    # 1 件にまとめられている
    opp_unknown_rows = [r for r in unresolved if r["列キー"] == "Opp.Unknown"]
    assert len(opp_unknown_rows) == 1
    assert opp_unknown_rows[0]["件数"] == "2"
    assert opp_unknown_rows[0]["管理番号"] == "1001|1002"
    assert opp_unknown_rows[0]["種別"] == "項目"
    assert opp_unknown_rows[0]["主オブジェクト"] == "Opportunity"
    assert opp_unknown_rows[0]["理由"] == "表示名一致せず"


def test_unresolved_describe_failure_goes_to_unresolved_sheet(tmp_path: Path) -> None:
    """describe 失敗は警告シートではなく取得できなかった項目シートに出る。"""
    output = tmp_path / "output"
    output.mkdir()
    record = _make_record(
        "1001",
        main_object="Opportunity",
        warnings=("オブジェクト Account の describe に失敗（HTTP 500: エラー）",),
    )

    run_tables(output, [record])

    sheets = _read_xlsx(output / XLSX_NAME)
    unresolved = sheets[SHEET_UNRESOLVED]
    obj_rows = [r for r in unresolved if r["種別"] == "オブジェクト"]
    assert len(obj_rows) == 1
    assert obj_rows[0]["主オブジェクト"] == "Account"
    assert "HTTP 500" in obj_rows[0]["理由"]
    # 警告シートには describe 失敗が出ていない
    warning_rows = sheets[SHEET_WARNING]
    assert all("describe に失敗" not in r["警告"] for r in warning_rows)


def test_unresolved_dedup_object_by_name_and_reason(tmp_path: Path) -> None:
    """同じオブジェクトで同じ理由の describe 失敗は 1 行、 件数 2。"""
    output = tmp_path / "output"
    output.mkdir()
    record_1001 = _make_record(
        "1001",
        warnings=("オブジェクト Account の describe に失敗（HTTP 500: エラー）",),
    )
    record_1002 = _make_record(
        "1002",
        warnings=("オブジェクト Account の describe に失敗（HTTP 500: エラー）",),
    )

    run_tables(output, [record_1001, record_1002])

    sheets = _read_xlsx(output / XLSX_NAME)
    unresolved = sheets[SHEET_UNRESOLVED]
    obj_rows = [r for r in unresolved if r["種別"] == "オブジェクト"]
    assert len(obj_rows) == 1
    assert obj_rows[0]["主オブジェクト"] == "Account"
    assert obj_rows[0]["件数"] == "2"
    assert obj_rows[0]["管理番号"] == "1001|1002"


def test_unresolved_duplicate_label_in_same_report(tmp_path: Path) -> None:
    """同じレポート内で表示名がぶつかっているものは、 理由が 「 同じレポート内で表示名が重複 」 で出る。"""
    output = tmp_path / "output"
    output.mkdir()
    record = _make_record("1001", main_object="Opportunity")
    # Name と同じ表示名 「 商談名 」 を持つ列を足す ( 別のフィールド )
    record.column_map.append(
        {
            "列キー": "Opp.OtherName",
            "表示名": "商談名",
            "所属オブジェクト": "Opportunity",
            "対応フィールドAPI名": "OtherName__c",
            "型": "string",
            "参照先オブジェクト": "",
            "リレーション名": "",
            "備考": "",
        }
    )

    run_tables(output, [record])

    sheets = _read_xlsx(output / XLSX_NAME)
    unresolved = sheets[SHEET_UNRESOLVED]
    dup_rows = [r for r in unresolved if r["理由"] == "同じレポート内で表示名が重複"]
    assert len(dup_rows) == 2  # Opp.Name と Opp.OtherName が両方
    keys = {r["列キー"] for r in dup_rows}
    assert keys == {"Opp.Name", "Opp.OtherName"}


def test_unresolved_sorted_by_count_then_kind(tmp_path: Path) -> None:
    """並びは 件数 ( 降順 ) → 種別 → 主オブジェクト → 列キー。"""
    output = tmp_path / "output"
    output.mkdir()
    record_1001 = _make_record(
        "1001",
        warnings=("オブジェクト Account の describe に失敗（HTTP 500: エラー）",),
    )
    record_1002 = _make_record(
        "1002",
        warnings=("オブジェクト Account の describe に失敗（HTTP 500: エラー）",),
    )

    run_tables(output, [record_1001, record_1002])

    sheets = _read_xlsx(output / XLSX_NAME)
    unresolved = sheets[SHEET_UNRESOLVED]
    # describe 失敗が件数 2 で先頭
    counts = [int(r["件数"]) for r in unresolved]
    assert counts == sorted(counts, reverse=True)
    # 種別 「 オブジェクト 」 は 「 項目 」 より前
    kinds = [r["種別"] for r in unresolved]
    if "オブジェクト" in kinds and "項目" in kinds:
        assert kinds.index("オブジェクト") < kinds.index("項目")


# ── 項目の対応シート ──────────────────────────────────────────────────────


def test_resolved_correspondence_dedup_by_main_and_field(tmp_path: Path) -> None:
    """同じ ``(主オブジェクト, SOQL列名)`` は 1 行。"""
    output = tmp_path / "output"
    output.mkdir()
    record_1001 = _make_record("1001", main_object="Opportunity")
    record_1002 = _make_record("1002", main_object="Opportunity")

    run_tables(output, [record_1001, record_1002])

    sheets = _read_xlsx(output / XLSX_NAME)
    resolved = sheets[SHEET_RESOLVED]
    # (Opportunity, AccountId) は 1 行
    account_rows = [r for r in resolved if r["SOQL列名"] == "AccountId"]
    assert len(account_rows) == 1
    assert account_rows[0]["主オブジェクト"] == "Opportunity"
    assert account_rows[0]["使っているレポート数"] == "2"
    assert account_rows[0]["管理番号"] == "1001|1002"


def test_resolved_excludes_unknown_fields(tmp_path: Path) -> None:
    """``(不明)`` のフィールドは 項目の対応 シートに出ない ( 取得できなかった項目シート ) 。"""
    output = tmp_path / "output"
    output.mkdir()
    record = _make_record("1001")

    run_tables(output, [record])

    sheets = _read_xlsx(output / XLSX_NAME)
    resolved = sheets[SHEET_RESOLVED]
    assert all(r["SOQL列名"] != "(不明)" for r in resolved)


def test_resolved_display_name_majority(tmp_path: Path) -> None:
    """表示名は最多 ( 同数なら管理番号の小さいレポートのもの ) 。 別名は 備考に 「 別の表示名あり 」 。"""
    output = tmp_path / "output"
    output.mkdir()
    # 1001 と 1003 で 「 取引先名 」 、 1002 で 「 取引先 」 を使う
    record_1001 = _make_record("1001", main_object="Opportunity")
    record_1002 = _make_record("1002", main_object="Opportunity")
    new_column_map = [
        {**col, "表示名": "取引先"} if col["対応フィールドAPI名"] == "AccountId" else col
        for col in record_1002.column_map
    ]
    record_1002 = dataclasses.replace(record_1002, column_map=new_column_map)
    record_1003 = _make_record("1003", main_object="Opportunity")

    run_tables(output, [record_1001, record_1002, record_1003])

    sheets = _read_xlsx(output / XLSX_NAME)
    resolved = sheets[SHEET_RESOLVED]
    account_rows = [r for r in resolved if r["SOQL列名"] == "AccountId"]
    assert len(account_rows) == 1
    # 「 取引先名 」 が 2 件、 「 取引先 」 が 1 件 → 最多は 「 取引先名 」
    assert account_rows[0]["表示名"] == "取引先名"
    assert "別の表示名あり: 取引先（1002）" in account_rows[0]["備考"]


def test_resolved_display_name_tie_break_by_smallest_key(tmp_path: Path) -> None:
    """同数のときは管理番号の小さいレポートの表示名を選ぶ。"""
    output = tmp_path / "output"
    output.mkdir()
    # 1001 で 「 別名A 」 1002 で 「 別名B 」 → 同数。 1001 を採用
    record_1001 = _make_record("1001", main_object="Opportunity")
    new_columns_1001 = [
        {**col, "表示名": "別名A"} if col["対応フィールドAPI名"] == "AccountId" else col
        for col in record_1001.column_map
    ]
    record_1001 = dataclasses.replace(record_1001, column_map=new_columns_1001)
    record_1002 = _make_record("1002", main_object="Opportunity")
    new_columns_1002 = [
        {**col, "表示名": "別名B"} if col["対応フィールドAPI名"] == "AccountId" else col
        for col in record_1002.column_map
    ]
    record_1002 = dataclasses.replace(record_1002, column_map=new_columns_1002)

    run_tables(output, [record_1001, record_1002])

    sheets = _read_xlsx(output / XLSX_NAME)
    resolved = sheets[SHEET_RESOLVED]
    account_rows = [r for r in resolved if r["SOQL列名"] == "AccountId"]
    assert len(account_rows) == 1
    assert account_rows[0]["表示名"] == "別名A"  # 1001 の表示名 ( tie → smallest 管理番号 )
    assert "別の表示名あり: 別名B（1002）" in account_rows[0]["備考"]


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
    # 4 件
    assert len(rows) == 4
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


# ── 列名の対応.csv のマージ ────────────────────────────────────────────────


def test_column_names_csv_merges_when_other_object_kept(tmp_path: Path) -> None:
    """既存ファイルで別の主オブジェクトの行はそのまま残り、 今回の行は置き換わる。"""
    output = tmp_path / "output"
    output.mkdir()

    # 事前に別オブジェクトの行を入れておく
    existing_path = output / COLUMN_NAMES_CSV_NAME
    existing_path.write_bytes(
        "﻿主オブジェクト,SOQL列名,表示名,備考,更新日\r\n"
        "Case,Case.AccountId,取引先ID,,2025-01-01\r\n".encode()
    )

    record = _make_record("1001", main_object="Opportunity")

    run_tables(output, [record])

    _, rows = _read_csv(existing_path)
    soql_pairs = {(r["主オブジェクト"], r["SOQL列名"]) for r in rows}
    # Case.AccountId は残る
    assert ("Case", "Case.AccountId") in soql_pairs
    # Opportunity 側の AccountId 等も入る
    assert ("Opportunity", "AccountId") in soql_pairs
    assert ("Opportunity", "WhoId") in soql_pairs
    # 並びは 主 → SOQL
    keys_in_order = [(r["主オブジェクト"], r["SOQL列名"]) for r in rows]
    assert keys_in_order == sorted(keys_in_order)
    # 既存ファイルの Case.AccountId はそのまま
    case_row = next(r for r in rows if r["主オブジェクト"] == "Case")
    assert case_row["更新日"] == "2025-01-01"


def test_column_names_csv_replaces_seen_rows(tmp_path: Path) -> None:
    """既存ファイルと同じ (主, SOQL) は今回の内容で置き換わる ( 更新日が変わる ) 。"""
    output = tmp_path / "output"
    output.mkdir()

    existing_path = output / COLUMN_NAMES_CSV_NAME
    existing_path.write_bytes(
        "﻿主オブジェクト,SOQL列名,表示名,備考,更新日\r\n"
        "Opportunity,AccountId,古い表示名,古い備考,2025-01-01\r\n".encode()
    )

    record = _make_record("1001", main_object="Opportunity")

    run_tables(output, [record])

    _, rows = _read_csv(existing_path)
    account_row = next(r for r in rows if r["SOQL列名"] == "AccountId")
    assert account_row["主オブジェクト"] == "Opportunity"
    assert account_row["表示名"] == "取引先名"  # 新しい表示名
    assert account_row["更新日"] == datetime.now(tz=UTC).astimezone().date().isoformat()


def test_column_names_csv_fails_on_old_header(tmp_path: Path) -> None:
    """旧形式の見出しを持つ既存ファイルは CSVError、 既存ファイルは変更されない。"""
    from comken.exceptions import CSVError

    output = tmp_path / "output"
    output.mkdir()

    existing_path = output / COLUMN_NAMES_CSV_NAME
    old_format = "﻿管理番号,概要,SOQL列名,表示名,備考\r\n1001,テスト,AccountId,取引先名,\r\n"
    existing_path.write_bytes(old_format.encode("utf-8"))
    before = existing_path.read_bytes()

    record = _make_record("1001", main_object="Opportunity")

    with pytest.raises(CSVError):
        run_tables(output, [record])

    # 既存ファイルは変更されない
    assert existing_path.read_bytes() == before


def test_column_names_csv_keeps_old_when_no_records(tmp_path: Path) -> None:
    """record が無いときは CSV を何もしない。"""
    output = tmp_path / "output"
    output.mkdir()

    existing_path = output / COLUMN_NAMES_CSV_NAME
    existing_path.write_bytes(
        "﻿主オブジェクト,SOQL列名,表示名,備考,更新日\r\n"
        "Case,Case.AccountId,取引先ID,,2025-01-01\r\n".encode()
    )
    before = existing_path.read_bytes()

    run_tables(output, [])

    # CSV はそのまま
    assert existing_path.read_bytes() == before


# ── 条件 / 集計 / 警告 / 項目一覧 / 関連 / 似たレポート ─────────────


def test_condition_basic(tmp_path: Path) -> None:
    """条件シートに 絞り込み・組立式・日付・クロス・上位N が 1 行ずつ出る。"""
    output = tmp_path / "output"
    output.mkdir()
    record = _make_record("1001", main_object="Opportunity")
    record.report["reportMetadata"]["reportFilters"] = [
        {"column": "Opp.Amount", "operator": "greaterThan", "value": "0"},
        {"column": "Opp.StageName", "operator": "equals", "value": "Closed Won"},
    ]
    record.report["reportMetadata"]["reportBooleanFilter"] = "1 AND 2"
    record.report["reportMetadata"]["standardDateFilter"] = {
        "column": "Opp.CloseDate",
        "durationValue": "LAST_N_DAYS:30",
        "startDate": "2024-01-01",
        "endDate": "2024-01-31",
    }
    record.report["reportMetadata"]["crossFilters"] = [
        {
            "primaryEntityField": "Opp.AccountId",
            "operator": "WITH",
            "relatedEntity": "OpportunityContactRole",
            "relatedEntityJoinField": "OpportunityId",
            "criteria": [],
        }
    ]
    record.report["reportMetadata"]["topRows"] = "10"

    run_tables(output, [record])

    sheets = _read_xlsx(output / XLSX_NAME)
    condition = sheets[SHEET_CONDITION]
    by_kind: dict[str, list[dict[str, str]]] = {}
    for row in condition:
        by_kind.setdefault(row["種別"], []).append(row)

    assert len(by_kind["絞り込み"]) == 2
    assert {r["列キー"] for r in by_kind["絞り込み"]} == {"Opp.Amount", "Opp.StageName"}
    assert by_kind["組立式"][0]["値"] == "1 AND 2"
    assert by_kind["日付"][0]["列キー"] == "Opp.CloseDate"
    assert "WITH" in by_kind["クロス"][0]["値"]
    assert by_kind["上位N"][0]["値"] == "10"


def test_condition_columns_match(tmp_path: Path) -> None:
    """条件シートの列順を確かめる。"""
    output = tmp_path / "output"
    output.mkdir()
    record = _make_record("1001")
    record.report["reportMetadata"]["reportFilters"] = [
        {"column": "X", "operator": "equals", "value": "y"}
    ]

    run_tables(output, [record])

    sheets = _read_xlsx(output / XLSX_NAME)
    headers = list(sheets[SHEET_CONDITION][0].keys())
    assert headers == list(CONDITION_COLUMNS)


def test_aggregate_basic(tmp_path: Path) -> None:
    """集計シートに グルーピング・集計・並び順が出る。"""
    output = tmp_path / "output"
    output.mkdir()
    record = _make_record("1001", main_object="Opportunity")
    record.report["reportMetadata"]["groupingsDown"] = [
        {"name": "Opp.StageName", "sortOrder": "Asc"}
    ]
    record.report["reportMetadata"]["aggregates"] = ["s!Opp.Amount"]
    record.report["reportMetadata"]["sortBy"] = [{"sortColumn": "Opp.Name", "sortOrder": "Desc"}]

    run_tables(output, [record])

    sheets = _read_xlsx(output / XLSX_NAME)
    aggregate = sheets[SHEET_AGGREGATE]
    by_kind = {r["種別"]: r for r in aggregate}
    assert by_kind["形式"]["内容"] == "TABULAR"
    assert by_kind["グルーピング(行)"]["名前"] == "Opp.StageName"
    assert by_kind["集計"]["名前"] == "s!Opp.Amount"
    assert by_kind["並び順"]["名前"] == "Opp.Name"


def test_aggregate_columns_match(tmp_path: Path) -> None:
    """集計シートの列順。"""
    output = tmp_path / "output"
    output.mkdir()
    record = _make_record("1001")

    run_tables(output, [record])

    sheets = _read_xlsx(output / XLSX_NAME)
    headers = list(sheets[SHEET_AGGREGATE][0].keys())
    assert headers == list(AGGREGATE_COLUMNS)


def test_warning_excludes_describe_failures(tmp_path: Path) -> None:
    """警告シートには describe 失敗以外の警告が出る。"""
    output = tmp_path / "output"
    output.mkdir()
    record = _make_record(
        "1001",
        warnings=(
            "オブジェクト Account の describe に失敗（HTTP 500: エラー）",
            "上限超過",
        ),
    )

    run_tables(output, [record])

    sheets = _read_xlsx(output / XLSX_NAME)
    warning = sheets[SHEET_WARNING]
    assert len(warning) == 1
    assert warning[0]["警告"] == "上限超過"


def test_warning_columns_match(tmp_path: Path) -> None:
    """警告シートの列順。"""
    output = tmp_path / "output"
    output.mkdir()
    record = _make_record("1001", warnings=("警告 A",))

    run_tables(output, [record])

    sheets = _read_xlsx(output / XLSX_NAME)
    headers = list(sheets[SHEET_WARNING][0].keys())
    assert headers == list(WARNING_COLUMNS)


def test_field_table_picklist_truncated(tmp_path: Path) -> None:
    """項目一覧シートの ``選択肢`` 列が 30 件超で ``…`` 付き。"""
    output = tmp_path / "output"
    output.mkdir()

    picklist = [f"V{i}" for i in range(31)]
    record = _make_record("1001", main_object="Opportunity")
    for field in record.objects["Opportunity"]["fields"]:
        if field["name"] == "Custom__c":
            field["picklist"] = picklist
            field["picklistTotal"] = 31
            break

    run_tables(output, [record])

    sheets = _read_xlsx(output / XLSX_NAME)
    field = sheets[SHEET_FIELDS]
    custom_row = next(r for r in field if r["項目API名"] == "Custom__c")
    choices = custom_row["選択肢"].split("|")
    assert len(choices) == 31
    assert choices[-1] == "…"


def test_field_columns_match(tmp_path: Path) -> None:
    """項目一覧シートの列順。"""
    output = tmp_path / "output"
    output.mkdir()
    record = _make_record("1001")

    run_tables(output, [record])

    sheets = _read_xlsx(output / XLSX_NAME)
    headers = list(sheets[SHEET_FIELDS][0].keys())
    assert headers == list(FIELD_COLUMNS)


def test_related_orders_by_key(tmp_path: Path) -> None:
    """関連シートは 管理番号 → 段 → 親 の順。"""
    output = tmp_path / "output"
    output.mkdir()

    from src.fetch import RelationRecord

    objects = {
        "Task": {
            "name": "Task",
            "label": "活動",
            "custom": False,
            "fields": [{"name": "Subject", "label": "件名", "type": "string"}],
        },
        "Account": {
            "name": "Account",
            "label": "取引先",
            "custom": False,
            "fields": [{"name": "Name", "label": "取引先名", "type": "string"}],
        },
    }
    record_1001 = ReportRecord(
        key="1001",
        summary="テスト1",
        report_id="00O000000000001",
        url="https://example/r/Report/00O000000000001/view",
        main_object="Task",
        report={},
        objects=objects,
        relations=(
            RelationRecord(
                parent="Task",
                field="WhatId",
                relationship_name="What",
                child="Account",
                depth=1,
            ),
        ),
        column_map=[],
        warnings=(),
    )

    run_tables(output, [record_1001])

    sheets = _read_xlsx(output / XLSX_NAME)
    related = sheets[SHEET_RELATED]
    assert related == [
        {
            "管理番号": "1001",
            "親オブジェクト": "Task",
            "参照項目API名": "WhatId",
            "リレーション名": "What",
            "子オブジェクト": "Account",
            "段": "1",
        }
    ]


def test_related_columns_match(tmp_path: Path) -> None:
    """関連シートの列順。"""
    output = tmp_path / "output"
    output.mkdir()

    from src.fetch import RelationRecord

    record = _make_record("1001")
    record_main = dataclasses.replace(
        record,
        main_object="Task",
        relations=(
            RelationRecord(
                parent="Task",
                field="WhatId",
                relationship_name="What",
                child="Account",
                depth=1,
            ),
        ),
    )

    run_tables(output, [record_main])

    sheets = _read_xlsx(output / XLSX_NAME)
    headers = list(sheets[SHEET_RELATED][0].keys())
    assert headers == list(RELATED_COLUMNS)


def test_similar_sheet_has_only_header_when_no_groups(tmp_path: Path) -> None:
    """似たレポートの該当が無いとシートは見出しだけになる。"""
    output = tmp_path / "output"
    output.mkdir()
    record = _make_record("1001")

    run_tables(output, [record])

    sheets = _read_xlsx(output / XLSX_NAME)
    similar = sheets[SHEET_SIMILAR]
    assert similar == []
    # ヘッダー行だけ
    workbook = load_workbook(output / XLSX_NAME)
    similar_sheet = workbook[SHEET_SIMILAR]
    headers = [cell.value for cell in similar_sheet[1]]
    assert headers == list(SIMILAR_COLUMNS)
    workbook.close()


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


# ── PII 列は出力列のみ ────────────────────────────────────────────────


def test_pii_only_for_output_columns(tmp_path: Path) -> None:
    """絞り込みにだけ使う列は個人情報判定の対象外 ( 既存仕様を引き継ぐ ) 。"""
    output = tmp_path / "output"
    output.mkdir()
    record = _make_record("1001", main_object="Opportunity")
    record.report["reportMetadata"]["reportFilters"] = [
        {"column": "Opp.Missing", "operator": "equals", "value": "B"}
    ]

    run_tables(output, [record])

    sheets = _read_xlsx(output / XLSX_NAME)
    resolved = sheets[SHEET_RESOLVED]
    # Opp.Missing は (不明) のままなので resolved シートには載らない
    soql_names = {r["SOQL列名"] for r in resolved}
    assert "Opp.Missing" not in soql_names
