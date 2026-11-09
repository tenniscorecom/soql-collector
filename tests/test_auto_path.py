"""列パスの自動確定（ 最短経路の候補を自動で採用する ） のテスト。

``mapping.build_column_map`` が同じ表示名の候補が複数あるとき、 主オブジェクトに
最も近い候補 （ 経路の段数が最少 ） を自動採用し、 状態は要確認にする ことを
確かめる。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src import column_fix_xlsx, mapping
from src.fetch import ReportRecord
from src.mapping.row_status import AUTO_PATH_NOTE
from src.soql_builder import build_soql

# ── ヘルパー ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Relation:
    """``RelationRecord`` 互換。"""

    parent: str
    field: str
    relationship_name: str | None
    child: str
    depth: int


def _metadata(
    detail_columns: list[str],
    labels: dict[str, str],
    *,
    report_filters: list[dict[str, Any]] | None = None,
    sort_by: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """レポート describe 相当を作る。"""
    report_metadata: dict[str, Any] = {
        "reportType": {"type": "Opportunity"},
        "reportFormat": "TABULAR",
        "detailColumns": detail_columns,
    }
    if report_filters is not None:
        report_metadata["reportFilters"] = report_filters
    if sort_by is not None:
        report_metadata["sortBy"] = sort_by
    return {
        "reportMetadata": report_metadata,
        "reportExtendedMetadata": {
            "detailColumnInfo": {key: {"label": labels.get(key, key)} for key in detail_columns}
        },
    }


def _describe(name: str, fields: list[dict[str, Any]]) -> dict[str, Any]:
    """Object Describe 相当を作る。"""
    return {"name": name, "fields": fields}


def _field(name: str, label: str, field_type: str = "Text") -> dict[str, Any]:
    """項目の dict を作る。"""
    return {"name": name, "label": label, "type": field_type}


# ── 主の項目と関連の項目 ──────────────────────────────────────────────────


def test_main_object_wins_over_related_object() -> None:
    """主の項目と関連の項目が同じ表示名なら、 主を採用する。"""
    metadata = _metadata(["NAME"], {"NAME": "名前"})
    main = _describe("Opportunity", [_field("Name", "名前")])
    rows = mapping.build_column_map(
        metadata,
        main,
        None,
        related_describes={"Account": _describe("Account", [_field("Name", "名前")])},
        relations=[_Relation("Opportunity", "AccountId", "Account", "Account", 1)],
        main_object="Opportunity",
    )
    row = next(r for r in rows if r["列キー"] == "NAME")
    assert row["対応フィールドAPI名"] == "Name"
    assert row["備考"].startswith(AUTO_PATH_NOTE)
    assert "採用 Name" in row["備考"]
    assert "他の候補" in row["備考"]
    # 状態は要確認
    assert mapping.column_status(row) == "要確認"
    # 候補の列には他の候補が出る
    assert mapping.column_candidates(row) != ""


def test_shortest_path_is_adopted() -> None:
    """段数 1 と 2 の候補なら、 段数 1 を採用する。"""
    metadata = _metadata(["NAME"], {"NAME": "名前"})
    main = _describe("Opportunity", [])
    rows = mapping.build_column_map(
        metadata,
        main,
        None,
        related_describes={
            "Account": _describe("Account", [_field("Name", "名前")]),
            "User": _describe("User", [_field("Name", "名前")]),
        },
        relations=[
            _Relation("Opportunity", "AccountId", "Account", "Account", 1),
            _Relation("Account", "OwnerId", "Owner", "User", 2),
        ],
        main_object="Opportunity",
    )
    row = next(r for r in rows if r["列キー"] == "NAME")
    assert row["対応フィールドAPI名"] == "Account.Name"
    assert row["備考"].startswith(AUTO_PATH_NOTE)


def test_adopted_candidate_is_not_in_the_other_candidates() -> None:
    """採用した候補は、 「他の候補」 に出ない。"""
    metadata = _metadata(["NAME"], {"NAME": "名前"})
    main = _describe("Opportunity", [_field("Name", "名前")])
    rows = mapping.build_column_map(
        metadata,
        main,
        None,
        related_describes={"Account": _describe("Account", [_field("Name", "名前")])},
        relations=[_Relation("Opportunity", "AccountId", "Account", "Account", 1)],
        main_object="Opportunity",
    )
    row = next(r for r in rows if r["列キー"] == "NAME")
    assert row["備考"] == f"{AUTO_PATH_NOTE}: 採用 Name / 他の候補: Account.Name（Account 名前）"
    assert mapping.column_candidates(row) == "Account.Name"


def test_same_depth_keeps_multiple_candidates() -> None:
    """同じ段数の候補が並んだら、 従来どおり 「複数候補あり」 で止める。"""
    metadata = _metadata(["NAME"], {"NAME": "名前"})
    main = _describe("Opportunity", [])
    rows = mapping.build_column_map(
        metadata,
        main,
        None,
        related_describes={
            "Account": _describe("Account", [_field("Name", "名前")]),
            "Contact": _describe("Contact", [_field("Name", "名前")]),
        },
        relations=[
            _Relation("Opportunity", "AccountId", "Account", "Account", 1),
            _Relation("Opportunity", "ContactId", "Contact", "Contact", 1),
        ],
        main_object="Opportunity",
    )
    row = next(r for r in rows if r["列キー"] == "NAME")
    assert row["対応フィールドAPI名"] == "(不明)"
    assert row["備考"].startswith("複数候補あり: ")
    assert AUTO_PATH_NOTE not in row["備考"]


# ── 列の直しと checks ─────────────────────────────────────────────────────


def _record(column_map: list[dict[str, str]]) -> ReportRecord:
    """列対応表を持つ record を作る。"""
    metadata = {
        "reportType": {"type": "Opportunity"},
        "reportFormat": "TABULAR",
        "detailColumns": [row["列キー"] for row in column_map],
    }
    return ReportRecord(
        key="1001",
        summary="テスト",
        report_id="00O000000000001",
        url="https://example.my.salesforce.com/lightning/r/Report/00O000000000001/view",
        main_object="Opportunity",
        report={"reportMetadata": metadata, "reportExtendedMetadata": {}},
        objects={},
        relations=(),
        column_map=column_map,
        warnings=(),
    )


def _auto_path_column_map() -> list[dict[str, str]]:
    """自動で確定した行の列対応表。"""
    return [
        {
            "列キー": "NAME",
            "表示名": "名前",
            "所属オブジェクト": "Opportunity",
            "対応フィールドAPI名": "Name",
            "型": "Text",
            "参照先オブジェクト": "",
            "リレーション名": "",
            "備考": f"{AUTO_PATH_NOTE}: 採用 Name / 他の候補: Account.Name（Account 名前）",
        }
    ]


def test_check_is_added_for_auto_path_column() -> None:
    """自動で確定した列は checks に 1 件出る。"""
    draft = build_soql(_record(_auto_path_column_map()))
    assert any("列 名前 は候補から自動で確定した" in check for check in draft.checks)
    assert any("採用 Name" in check for check in draft.checks)
    assert any("他の候補" in check for check in draft.checks)
    assert any("レポートと同じか確かめる" in check for check in draft.checks)


def test_no_check_for_unrelated_column() -> None:
    """自動で確定していない列には checks が出ない。"""
    column_map = _auto_path_column_map()
    column_map[0]["備考"] = ""
    draft = build_soql(_record(column_map))
    assert not any("自動で確定" in check for check in draft.checks)


def test_human_value_wins_over_auto_path() -> None:
    """列の直しの人の値が、 自動確定より優先される。"""
    column_map = _auto_path_column_map()
    overrides = {
        ("opportunity", "NAME"): column_fix_xlsx.ColumnFixRow(
            status=column_fix_xlsx.STATUS_OK,
            main_object="Opportunity",
            display_name="名前",
            column_key="NAME",
            auto_soql_name="Name",
            candidates="",
            fixed_soql_name="CustomName__c",
            note="手で直した",
            used_in_reports="1001",
            updated_at="2026-11-01",
        )
    }
    applied = column_fix_xlsx.apply_overrides(
        column_map_rows=column_map,
        existing=overrides,
        main_object="Opportunity",
        objects={"Opportunity": _describe("Opportunity", [_field("CustomName__c", "名前")])},
        warnings=[],
    )
    assert applied[0]["対応フィールドAPI名"] == "CustomName__c"
    assert AUTO_PATH_NOTE not in applied[0]["備考"]
    assert mapping.column_status(applied[0]) == "OK"


def test_column_fix_xlsx_marks_auto_path_as_needs_check() -> None:
    """列の直し.xlsx では、 自動で確定した行は要確認になる。"""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "列の直し.xlsx"
        column_fix_xlsx.merge_and_write_after_run(
            path=path,
            existing={},
            records=[_record(_auto_path_column_map())],
            today=column_fix_xlsx.today_local(),
        )
        rows = column_fix_xlsx.load_column_fix(path)
        row = rows[("opportunity", "NAME")]
        assert row.status == column_fix_xlsx.STATUS_NEEDS_CHECK
        assert "Account.Name" in row.candidates
