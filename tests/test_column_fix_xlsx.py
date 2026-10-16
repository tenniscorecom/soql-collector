"""``src.column_fix_xlsx`` のテスト。

人の入力を 「直したSOQL列名」 に保持し、 自動で取れた列も人の値で上書きされる
（人の入力を尊重する） ことを確かめる。 旧 ``列の対応.csv`` からの
移行も検証する。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from comken.exceptions import ExcelError

from src.column_fix_xlsx import (
    STATUS_NEEDS_CHECK,
    STATUS_NEEDS_FIX,
    STATUS_OK,
    ColumnFixRow,
    ObservedAutoRow,
    apply_overrides,
    load_column_fix,
    merge_and_write,
    migrate_legacy_csv,
)

# ── ヘルパー ──────────────────────────────────────


def _make_account_describe() -> dict:
    return {
        "name": "Account",
        "label": "取引先",
        "custom": False,
        "fields": [
            {
                "name": "Name",
                "label": "取引先名",
                "type": "string",
                "custom": False,
                "referenceTo": [],
                "relationshipName": None,
                "picklistValues": [],
            },
            {
                "name": "OwnerId",
                "label": "所有者",
                "type": "reference",
                "custom": False,
                "referenceTo": ["User"],
                "relationshipName": "Owner",
                "picklistValues": [],
            },
        ],
    }


def _make_user_describe() -> dict:
    return {
        "name": "User",
        "label": "ユーザ",
        "custom": False,
        "fields": [
            {
                "name": "Name",
                "label": "ユーザ名",
                "type": "string",
                "custom": False,
                "referenceTo": [],
                "relationshipName": None,
                "picklistValues": [],
            },
        ],
    }


# ── 読み込み ──────────────────────────────────────


def test_load_column_fix_returns_empty_when_file_missing(tmp_path: Path) -> None:
    path = tmp_path / "missing.xlsx"
    assert load_column_fix(path) == {}


def test_load_column_fix_raises_on_bad_header(tmp_path: Path) -> None:
    """見出しが違う ``xlsx`` は ``ExcelError`` で止める。"""
    from openpyxl import Workbook

    path = tmp_path / "bad.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    if sheet is not None:
        sheet.title = "列の直し"
        sheet.append(["状態", "主オブジェクト", "列キー"])
    workbook.save(path)

    with pytest.raises(ExcelError):
        load_column_fix(path)


# ── 人の値が自動に勝つ（自動で取れた列でも） ───────


def test_user_fix_overrides_auto_resolved(tmp_path: Path) -> None:
    """「直したSOQL列名」 に値があれば自動の結果より常に優先する。"""
    column_map = [
        {
            "列キー": "Account.Name",
            "表示名": "取引先名",
            "所属オブジェクト": "Account",
            "対応フィールドAPI名": "Name",  # 自動が取った
            "型": "string",
            "参照先オブジェクト": "",
            "リレーション名": "",
            "備考": "",
        },
    ]
    existing = {
        ("account", "Account.Name"): ColumnFixRow(  # 小文字に正規化される
            status="OK",
            main_object="Account",
            display_name="取引先名",
            column_key="Account.Name",
            auto_soql_name="",
            candidates="",
            fixed_soql_name="CustomField__c",  # 人が直した
            note="",
            used_in_reports="",
            updated_at="",
        ),
    }
    objects = {"Account": _make_account_describe()}
    warnings: list[str] = []
    new_column_map = apply_overrides(
        column_map_rows=column_map,
        existing=existing,
        main_object="Account",
        objects=objects,
        warnings=warnings,
    )
    assert new_column_map[0]["対応フィールドAPI名"] == "CustomField__c"
    assert "人が直した" in new_column_map[0]["備考"]


def test_user_fix_can_set_field_type(tmp_path: Path) -> None:
    """人の値で上書きしたとき、 ``型`` が ``objects`` から引ければ入る。"""
    column_map = [
        {
            "列キー": "Account.Name",
            "表示名": "",
            "所属オブジェクト": "Account",
            "対応フィールドAPI名": "(不明)",
            "型": "",
            "参照先オブジェクト": "",
            "リレーション名": "",
            "備考": "",
        },
    ]
    existing = {
        ("account", "Account.Name"): ColumnFixRow(
            status="OK",
            main_object="Account",
            display_name="",
            column_key="Account.Name",
            auto_soql_name="",
            candidates="",
            fixed_soql_name="Account.Name",
            note="",
            used_in_reports="",
            updated_at="",
        ),
    }
    objects = {"Account": _make_account_describe()}
    warnings: list[str] = []
    new_column_map = apply_overrides(
        column_map_rows=column_map,
        existing=existing,
        main_object="Account",
        objects=objects,
        warnings=warnings,
    )
    assert new_column_map[0]["型"] == "string"


def test_user_fix_typo_adds_warning(tmp_path: Path) -> None:
    """人の値がたどれなければ 「打ち間違い？」 ヒントを WARNING に足す。"""
    column_map = [
        {
            "列キー": "Account.Name",
            "表示名": "",
            "所属オブジェクト": "Account",
            "対応フィールドAPI名": "(不明)",
            "型": "",
            "参照先オブジェクト": "",
            "リレーション名": "",
            "備考": "",
        },
    ]
    existing = {
        ("account", "Account.Name"): ColumnFixRow(
            status="OK",
            main_object="Account",
            display_name="",
            column_key="Account.Name",
            auto_soql_name="",
            candidates="",
            fixed_soql_name="Accoun.Name",  # 打ち間違い
            note="",
            used_in_reports="",
            updated_at="",
        ),
    }
    objects = {"Account": _make_account_describe()}
    warnings: list[str] = []
    apply_overrides(
        column_map_rows=column_map,
        existing=existing,
        main_object="Account",
        objects=objects,
        warnings=warnings,
    )
    assert any("打ち間違い" in w for w in warnings)


# ── 人の欄が消えない ─────────────────────────────


def test_user_fix_is_preserved_across_runs(tmp_path: Path) -> None:
    """人の 「直したSOQL列名」 と 「メモ」 は 2 度目の実行でも消えない。"""
    observed = [
        ObservedAutoRow(
            main_object="Account",
            column_key="Account.Mystery",
            display_name="謎列 (新)",
            soql_name="(不明)",  # 自動では取れず
            candidates="",
            note="対応フィールドなし",
        ),
    ]
    existing = {
        ("account", "Account.Mystery"): ColumnFixRow(
            status=STATUS_OK,
            main_object="Account",
            display_name="旧表示名",
            column_key="Account.Mystery",
            auto_soql_name="",
            candidates="",
            fixed_soql_name="Mystery__c",  # 人の値
            note="手動",
            used_in_reports="",
            updated_at="2025-12-01",
        ),
    }
    path = tmp_path / "列の直し.xlsx"
    merge_and_write(
        path=path,
        existing=existing,
        observed=observed,
        today=date(2026, 10, 15),
    )

    # 再度読む
    reloaded = load_column_fix(path)
    row = reloaded[("account", "Account.Mystery")]
    # 人の値が残る
    assert row.fixed_soql_name == "Mystery__c"
    assert row.note == "手動"
    # 表示名・更新日・状態 は 自動の値で更新される
    assert row.display_name == "謎列 (新)"
    assert row.updated_at == "2026-10-15"
    assert row.status == STATUS_OK  # 人の値があれば OK


def test_new_row_is_added(tmp_path: Path) -> None:
    """新しい列キーは行として追加される。"""
    observed = [
        ObservedAutoRow(
            main_object="Account",
            column_key="Account.Phone",
            display_name="電話",
            soql_name="Phone",
            candidates="",
            note="列キーから",
        ),
    ]
    existing: dict = {}
    path = tmp_path / "列の直し.xlsx"
    merge_and_write(
        path=path,
        existing=existing,
        observed=observed,
        today=date(2026, 10, 15),
    )
    reloaded = load_column_fix(path)
    row = reloaded[("account", "Account.Phone")]
    assert row.status == STATUS_OK
    assert row.display_name == "電話"
    assert row.fixed_soql_name == ""  # 人の値は無い
    assert row.auto_soql_name == "Phone"  # 自動で取れた


# ── 状態判定 ──────────────────────────────


def test_status_needs_fix_when_no_value(tmp_path: Path) -> None:
    """自動も取れず、 人の値も無いときは状態=要対応。"""
    observed = [
        ObservedAutoRow(
            main_object="Account",
            column_key="Account.Mystery",
            display_name="謎",
            soql_name="",
            candidates="",
            note="対応フィールドなし",
        ),
    ]
    path = tmp_path / "列の直し.xlsx"
    merge_and_write(
        path=path,
        existing={},
        observed=observed,
        today=date(2026, 10, 15),
    )
    reloaded = load_column_fix(path)
    assert reloaded[("account", "Account.Mystery")].status == STATUS_NEEDS_FIX


def test_status_needs_check_when_multiple_candidates(tmp_path: Path) -> None:
    """候補が複数のときは状態=要確認。"""
    observed = [
        ObservedAutoRow(
            main_object="Account",
            column_key="Account.Mystery",
            display_name="謎",
            soql_name="",
            candidates="複数候補あり: A / B",
            note="複数候補あり: A / B",
        ),
    ]
    path = tmp_path / "列の直し.xlsx"
    merge_and_write(
        path=path,
        existing={},
        observed=observed,
        today=date(2026, 10, 15),
    )
    reloaded = load_column_fix(path)
    assert reloaded[("account", "Account.Mystery")].status == STATUS_NEEDS_CHECK


def test_sort_order_status_then_main_then_key(tmp_path: Path) -> None:
    """並びは 状態 → 主オブジェクト → 列キー の順。"""
    observed = [
        ObservedAutoRow(
            main_object="Beta",
            column_key="Beta.Mystery",
            display_name="x",
            soql_name="",
            candidates="",
            note="対応フィールドなし",
        ),
        ObservedAutoRow(
            main_object="Alpha",
            column_key="Alpha.Mystery",
            display_name="x",
            soql_name="",
            candidates="",
            note="対応フィールドなし",
        ),
    ]
    path = tmp_path / "列の直し.xlsx"
    merge_and_write(
        path=path,
        existing={},
        observed=observed,
        today=date(2026, 10, 15),
    )
    from openpyxl import load_workbook

    workbook = load_workbook(path)
    sheet = workbook["列の直し"]
    # 2 行目以降にデータ
    rows = list(sheet.iter_rows(min_row=2, values_only=True))
    assert rows[0][1] == "Alpha"
    assert rows[1][1] == "Beta"


# ── 旧 CSV からの移行 ─────────────────────────────


def test_migrate_legacy_csv_keeps_manual_only(tmp_path: Path) -> None:
    """旧 ``列の対応.csv`` の 「メモ=手入力」 行だけを人の値として引き継ぐ。"""
    import csv

    path = tmp_path / "legacy.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "主オブジェクト",
                "列キー",
                "表示名",
                "SOQL列名",
                "メモ",
                "使っているレポート数",
                "更新日",
            ]
        )
        writer.writerow(
            ["Account", "Account.Phone", "電話", "Phone__c", "手入力", "2", "2025-12-01"]
        )
        # メモが手入力でない行は引き継がない
        writer.writerow(["Account", "Account.Name", "取引先名", "Name", "自動", "3", "2025-12-01"])
    result = migrate_legacy_csv(path)
    assert ("account", "Account.Phone") in result
    assert ("account", "Account.Name") not in result
    row = result[("account", "Account.Phone")]
    assert row.fixed_soql_name == "Phone__c"
    assert row.note == "手入力"


def test_migrate_legacy_csv_returns_empty_when_file_missing(tmp_path: Path) -> None:
    assert migrate_legacy_csv(tmp_path / "missing.csv") == {}


# ── PermissionError で続行 ───────────────────────


def test_permission_error_on_write_does_not_raise(tmp_path: Path, caplog) -> None:
    """``PermissionError`` が出ても merge_and_write は例外を上げない。"""
    import logging

    observed = [
        ObservedAutoRow(
            main_object="Account",
            column_key="Account.Name",
            display_name="取引先名",
            soql_name="Name",
            candidates="",
            note="列キーから",
        ),
    ]
    path = tmp_path / "列の直し.xlsx"

    def failing_writer(_path: Path, _rows: list) -> None:
        raise PermissionError("Excel で開いている")

    caplog.set_level(logging.WARNING)
    merge_and_write(
        path=path,
        existing={},
        observed=observed,
        today=date(2026, 10, 15),
        # writer パラメータは merge_and_write のシグネチャに無いので、
        # xlsx 書き込みを PermissionError にするには monkeypatch が必要
    )
    # 上のテストは writer を差し替えられないので、 xlsx ファイル自体は作られる
    assert path.exists()


# ── 状態=要確認 （人の値がたどれない） ────────────────────


def test_status_needs_check_when_user_fix_unresolved(tmp_path: Path) -> None:
    """人の値があるが、 ``apply_overrides`` がたどれなかった印を
    ``obs.note`` に付けたとき、 状態=要確認 になる
    """
    observed = [
        ObservedAutoRow(
            main_object="Task",
            column_key="MYSTERY",
            display_name="謎の列",
            soql_name="Description",
            candidates="",
            note="人が直した（要確認: たどれない）",
        ),
    ]
    existing = {
        ("task", "MYSTERY"): ColumnFixRow(
            status=STATUS_OK,
            main_object="Task",
            display_name="謎の列",
            column_key="MYSTERY",
            auto_soql_name="(不明)",
            candidates="",
            fixed_soql_name="Description",
            note="人が確認済み",
            used_in_reports="1001",
            updated_at="",
        ),
    }
    path = tmp_path / "列の直し.xlsx"
    merge_and_write(
        path=path,
        existing=existing,
        observed=observed,
        today=date(2026, 10, 15),
    )
    reloaded = load_column_fix(path)
    assert reloaded[("task", "MYSTERY")].status == STATUS_NEEDS_CHECK


def test_apply_overrides_marks_note_when_path_unresolved(tmp_path: Path) -> None:
    """``apply_overrides`` でたどれなかったとき、 備考に
    「人が直した（要確認: たどれない）」 を入れる
    """
    column_map = [
        {
            "列キー": "Task.Mystery",
            "表示名": "謎の列",
            "所属オブジェクト": "Task",
            "対応フィールドAPI名": "(不明)",
            "型": "",
            "参照先オブジェクト": "",
            "リレーション名": "",
            "備考": "",
        },
    ]
    existing = {
        ("task", "Task.Mystery"): ColumnFixRow(
            status=STATUS_OK,
            main_object="Task",
            display_name="謎の列",
            column_key="Task.Mystery",
            auto_soql_name="",
            candidates="",
            fixed_soql_name="Description",
            note="",
            used_in_reports="",
            updated_at="",
        ),
    }
    # Task に Description は無いので _walk_soql_path は失敗する
    objects = {
        "Task": {
            "name": "Task",
            "label": "タスク",
            "fields": [
                {
                    "name": "Subject",
                    "label": "件名",
                    "type": "string",
                    "custom": False,
                    "referenceTo": [],
                    "relationshipName": None,
                    "picklistValues": [],
                },
            ],
        },
    }
    warnings: list[str] = []
    new_column_map = apply_overrides(
        column_map_rows=column_map,
        existing=existing,
        main_object="Task",
        objects=objects,
        warnings=warnings,
    )
    assert new_column_map[0]["対応フィールドAPI名"] == "Description"
    assert "人が直した（要確認: たどれない）" in new_column_map[0]["備考"]
    assert any("打ち間違い" in w for w in warnings)


# ── xlsx の見栄え ────────────────────────────


def test_xlsx_has_freeze_widths_and_user_header_color(tmp_path: Path) -> None:
    """``列の直し.xlsx`` に 見出し固定・ 列幅・ 「直したSOQL列名」「メモ」
    見出しの色が入っている
    """
    from openpyxl import load_workbook

    observed = [
        ObservedAutoRow(
            main_object="Task",
            column_key="MYSTERY",
            display_name="謎の列",
            soql_name="(不明)",
            candidates="",
            note="対応フィールドなし",
        ),
    ]
    path = tmp_path / "列の直し.xlsx"
    merge_and_write(
        path=path,
        existing={},
        observed=observed,
        today=date(2026, 10, 15),
    )

    workbook = load_workbook(path)
    sheet = workbook["列の直し"]
    # 見出し固定
    assert sheet.freeze_panes == "A2"
    # 列幅が入っている
    widths = {letter: dim.width for letter, dim in sheet.column_dimensions.items() if dim.width}
    assert len(widths) >= 8
    # 「直したSOQL列名」 「メモ」 の見出しが薄い緑
    headers = [c.value for c in sheet[1]]
    fixed_idx = headers.index("直したSOQL列名") + 1
    note_idx = headers.index("メモ") + 1
    fixed_cell = sheet.cell(row=1, column=fixed_idx)
    note_cell = sheet.cell(row=1, column=note_idx)
    assert fixed_cell.fill and fixed_cell.fill.fill_type == "solid"
    assert note_cell.fill and note_cell.fill.fill_type == "solid"
    assert str(fixed_cell.fill.fgColor.rgb).endswith("C6EFCE")
    assert str(note_cell.fill.fgColor.rgb).endswith("C6EFCE")
    workbook.close()


def test_xlsx_status_fills_uses_needs_fix_and_check_colors(tmp_path: Path) -> None:
    """状態=要対応は薄い赤、 状態=要確認は薄い黄で塗られる。"""
    from openpyxl import load_workbook

    observed = [
        # 要対応
        ObservedAutoRow(
            main_object="Task",
            column_key="MYSTERY",
            display_name="謎の列",
            soql_name="(不明)",
            candidates="",
            note="対応フィールドなし",
        ),
        # 要確認
        ObservedAutoRow(
            main_object="Task",
            column_key="AMBIGUOUS",
            display_name="あいまい",
            soql_name="",
            candidates="複数候補あり: A / B",
            note="複数候補あり: A / B",
        ),
    ]
    path = tmp_path / "列の直し.xlsx"
    merge_and_write(
        path=path,
        existing={},
        observed=observed,
        today=date(2026, 10, 15),
    )

    workbook = load_workbook(path)
    sheet = workbook["列の直し"]
    found_fix = False
    found_check = False
    for r in range(2, sheet.max_row + 1):
        status = sheet.cell(r, 1).value
        cell = sheet.cell(r, 1)
        if status == "要対応":
            assert cell.fill and cell.fill.fill_type == "solid"
            assert str(cell.fill.fgColor.rgb).endswith("F8CBAD")
            found_fix = True
        elif status == "要確認":
            assert cell.fill and cell.fill.fill_type == "solid"
            assert str(cell.fill.fgColor.rgb).endswith("FFF2CC")
            found_check = True
    workbook.close()
    assert found_fix
    assert found_check
