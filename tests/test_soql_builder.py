"""``src.soql_builder`` の単体テスト。

``build_soql()`` の組み立て規則を 1 つずつ確認する。 ``record`` は
``ReportRecord`` を直接組み立てて渡す (API は使わない) 。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from src.fetch import ReportRecord, RowCheck
from src.mapping.owner import OWNER_POLYMORPHIC_NOTE
from src.soql_builder import (
    CROSS_FILTER_LINK_DIRECT,
    CROSS_FILTER_LINK_NOT_DIRECT,
    CROSS_FILTER_LINK_UNVERIFIED,
    INCOMPLETE_MARKER,
    STATUS_CREATED,
    STATUS_NEEDS_CHECK,
    STATUS_NONE,
    STATUS_PARTIAL,
    CrossFilterLink,
    SoqlDraft,
    build_soql,
)

# ── ヘルパー ──────────────────────────────────────────────────────────────


def _make_record(
    main_object: str = "Opportunity",
    *,
    detail_columns: list[str] | None = None,
    column_map: list[dict[str, str]] | None = None,
    report_filters: list[dict] | None = None,
    standard_date_filter: dict | None = None,
    standard_filters: list | None = None,
    cross_filters: list | None = None,
    scope: str | None = None,
    sort_by: list | None = None,
    top_rows: dict | None = None,
    groupings_down: list[dict] | None = None,
    groupings_across: list[dict] | None = None,
    aggregates: list[str] | None = None,
    buckets: list | None = None,
    custom_summary_formula: list | None = None,
    report_boolean_filter: str | None = None,
    format_value: str = "TABULAR",
    report_metadata: dict[str, Any] | None = None,
    objects: dict[str, dict[str, Any]] | None = None,
    cross_filter_objects: dict[str, dict[str, Any]] | None = None,
    row_check: RowCheck | None = None,
    exceeds_row_limit: bool | None = None,
) -> ReportRecord:
    """テスト用の ``ReportRecord`` を作る。

    ``detail_columns`` と ``column_map`` を渡して SELECT と列対応を組む。
    ``column_map`` を省略した場合 ``detail_columns`` のキーと同名の列が
    ``"string"`` 型で解決できるよう自動生成する (簡易形) 。
    ``report_metadata`` を渡すと ``detailColumns`` 等を組み立てる前に
    ベース dict として使う ( 既存のキーを保ったまま、 渡された
    ``detail_columns`` 等の派生値もマージする ) 。
    ``row_check`` は 2000 行チェックの結果、 ``exceeds_row_limit`` は管理表の
    「2000件超」列の値。 取り方 ( Bulk / 通常 SOQL ) の判定に使う。
    """
    detail_columns = detail_columns or []
    if column_map is None:
        column_map = [
            {
                "列キー": key,
                "表示名": key,
                "所属オブジェクト": main_object,
                "対応フィールドAPI名": key.split(".")[-1],
                "型": "string",
                "参照先オブジェクト": "",
                "リレーション名": "",
                "備考": "",
            }
            for key in detail_columns
        ]
    if report_metadata is None:
        report_metadata = {
            "reportType": {"type": main_object},
            "reportFormat": format_value,
            "detailColumns": detail_columns,
        }
    else:
        report_metadata = dict(report_metadata)
        report_metadata.setdefault("reportType", {"type": main_object})
        report_metadata.setdefault("reportFormat", format_value)
        report_metadata.setdefault("detailColumns", detail_columns)
    if report_filters is not None:
        report_metadata["reportFilters"] = report_filters
    if standard_date_filter is not None:
        report_metadata["standardDateFilter"] = standard_date_filter
    if standard_filters is not None:
        report_metadata["standardFilters"] = standard_filters
    if cross_filters is not None:
        report_metadata["crossFilters"] = cross_filters
    if scope is not None:
        report_metadata["scope"] = scope
    if sort_by is not None:
        report_metadata["sortBy"] = sort_by
    if top_rows is not None:
        report_metadata["topRows"] = top_rows
    if groupings_down is not None:
        report_metadata["groupingsDown"] = groupings_down
    if groupings_across is not None:
        report_metadata["groupingsAcross"] = groupings_across
    if aggregates is not None:
        report_metadata["aggregates"] = aggregates
    if buckets is not None:
        report_metadata["buckets"] = buckets
    if custom_summary_formula is not None:
        report_metadata["customSummaryFormula"] = custom_summary_formula
    if report_boolean_filter is not None:
        report_metadata["reportBooleanFilter"] = report_boolean_filter

    return ReportRecord(
        key="1001",
        summary="テスト",
        report_id="00O000000000001",
        url="https://example.my.salesforce.com/lightning/r/Report/00O000000000001/view",
        main_object=main_object,
        report={"reportMetadata": report_metadata, "reportExtendedMetadata": {}},
        objects=objects or {},
        relations=(),
        column_map=column_map,
        warnings=(),
        cross_filter_objects=cross_filter_objects or {},
        row_check=row_check,
        exceeds_row_limit=exceeds_row_limit,
    )


def _column_map_with_types(rows: list[tuple[str, str, str]]) -> list[dict[str, str]]:
    """``(列キー, フィールド API 名, 型)`` のリストから ``column_map`` を作る。"""
    return [
        {
            "列キー": key,
            "表示名": key,
            "所属オブジェクト": "Opportunity",
            "対応フィールドAPI名": api,
            "型": field_type,
            "参照先オブジェクト": "",
            "リレーション名": "",
            "備考": "",
        }
        for key, api, field_type in rows
    ]


# ── 状態判定 ────────────────────────────────────────────────────────────


def test_status_none_when_main_object_is_missing() -> None:
    """主オブジェクト不明は ``作れず`` 。"""
    record = _make_record(main_object="")
    detail = record.report["reportMetadata"]["detailColumns"]
    record = ReportRecord(
        key=record.key,
        summary=record.summary,
        report_id=record.report_id,
        url=record.url,
        main_object=None,
        report=record.report,
        objects=record.objects,
        relations=record.relations,
        column_map=record.column_map,
        warnings=record.warnings,
    )
    _ = detail  # 型チェック
    draft = build_soql(record)
    assert draft.status == STATUS_NONE
    assert draft.soql == ""
    assert any("主オブジェクトが不明" in m for m in draft.missing)


def test_status_none_when_no_selectable_columns() -> None:
    """SELECT に入れられる列が無いときは ``作れず`` 。"""
    column_map = [
        {
            "列キー": "X",
            "表示名": "X",
            "所属オブジェクト": "Opportunity",
            "対応フィールドAPI名": "(不明)",
            "型": "",
            "参照先オブジェクト": "",
            "リレーション名": "",
            "備考": "対応フィールドなし",
        }
    ]
    record = _make_record(detail_columns=["X"], column_map=column_map)
    draft = build_soql(record)
    assert draft.status == STATUS_NONE
    assert draft.soql == ""


def test_status_created_when_fully_resolved() -> None:
    """全列解決 + 条件も完全なら ``作成済み`` 。"""
    column_map = _column_map_with_types([("Name", "Name", "string")])
    record = _make_record(
        detail_columns=["Name"],
        column_map=column_map,
        report_filters=[{"column": "Name", "operator": "equals", "value": "Acme"}],
    )
    draft = build_soql(record)
    assert draft.status == STATUS_CREATED
    assert draft.missing == ()
    assert draft.checks == ()


def test_status_needs_check_when_only_checks_present() -> None:
    """SOQL は完全だが checks があるときは ``要確認`` 。"""
    column_map = _column_map_with_types([("Name", "Name", "string")])
    record = _make_record(
        detail_columns=["Name"],
        column_map=column_map,
        scope="MyTerritory",
    )
    draft = build_soql(record)
    assert draft.status == STATUS_NEEDS_CHECK
    assert draft.missing == ()
    assert any("スコープ" in c for c in draft.checks)


def test_status_partial_when_something_missing() -> None:
    """missing がある場合は ``一部作れず`` 。"""
    column_map = _column_map_with_types([("Name", "Name", "string")])
    record = _make_record(
        detail_columns=["Name", "Unknown"],
        column_map=column_map,
    )
    draft = build_soql(record)
    assert draft.status == STATUS_PARTIAL
    assert any("列: Unknown" in m for m in draft.missing)


# ── SELECT 句 ────────────────────────────────────────────────────────────


def test_select_uses_resolved_field_in_detail_columns_order() -> None:
    """``detailColumns`` の順で対応フィールド API 名が入る。"""
    column_map = _column_map_with_types(
        [
            ("Name", "Name", "string"),
            ("StageName", "StageName", "picklist"),
            ("Amount", "Amount", "currency"),
        ]
    )
    record = _make_record(detail_columns=["Name", "StageName", "Amount"], column_map=column_map)
    draft = build_soql(record)
    assert draft.status == STATUS_CREATED
    select_part = draft.soql.split("FROM")[0]
    assert "Name" in select_part
    assert "StageName" in select_part
    assert "Amount" in select_part
    # 順序: Name が StageName より前
    assert select_part.index("Name") < select_part.index("StageName")
    assert select_part.index("StageName") < select_part.index("Amount")


def test_select_excludes_unresolved_columns_and_reports_missing() -> None:
    """対応が取れない列は SELECT から外れて missing に「列: 表示名 (列キー) — 理由」。"""
    column_map = [
        {
            "列キー": "OK",
            "表示名": "OK列",
            "所属オブジェクト": "Opportunity",
            "対応フィールドAPI名": "Name",
            "型": "string",
            "参照先オブジェクト": "",
            "リレーション名": "",
            "備考": "",
        },
        {
            "列キー": "BAD",
            "表示名": "BAD列",
            "所属オブジェクト": "Opportunity",
            "対応フィールドAPI名": "(不明)",
            "型": "",
            "参照先オブジェクト": "",
            "リレーション名": "",
            "備考": "対応フィールドなし",
        },
    ]
    record = _make_record(detail_columns=["OK", "BAD"], column_map=column_map)
    draft = build_soql(record)
    assert draft.status == STATUS_PARTIAL
    assert "Name" in draft.soql
    assert "BAD列" not in draft.soql
    assert any("列: BAD列 ( BAD ) — 対応フィールドなし" == m for m in draft.missing)


def test_select_grouping_columns_appended_for_summary() -> None:
    """``SUMMARY`` / ``MATRIX`` の ``groupingsDown`` / ``groupingsAcross`` の列が
    SELECT の末尾に足される。"""
    column_map = _column_map_with_types(
        [
            ("Name", "Name", "string"),
            ("StageName", "StageName", "picklist"),
        ]
    )
    record = _make_record(
        detail_columns=["Name"],
        column_map=column_map,
        groupings_down=[{"name": "StageName", "sortOrder": "Asc"}],
        format_value="SUMMARY",
    )
    draft = build_soql(record)
    assert "Name" in draft.soql
    assert "StageName" in draft.soql
    # 順: detail (Name) → groupings (StageName)
    assert draft.soql.index("Name") < draft.soql.index("StageName")


def test_select_dedupes_same_field_in_detail_and_grouping() -> None:
    """detail と groupings 両方に同じ列があっても SELECT 内で重複しない。"""
    column_map = _column_map_with_types([("StageName", "StageName", "picklist")])
    record = _make_record(
        detail_columns=["StageName"],
        column_map=column_map,
        groupings_down=[{"name": "StageName"}],
    )
    draft = build_soql(record)
    select_part = draft.soql.split("FROM")[0]
    # SELECT 内で ``StageName`` は 1 回
    assert select_part.count("StageName") == 1


# ── FROM 句 ──────────────────────────────────────────────────────────────


def test_from_clause_is_main_object() -> None:
    """``FROM`` は ``main_object`` 。"""
    column_map = _column_map_with_types([("Name", "Name", "string")])
    record = _make_record(
        main_object="Account",
        detail_columns=["Name"],
        column_map=column_map,
    )
    draft = build_soql(record)
    assert "FROM Account" in draft.soql


# ── WHERE 句: 演算子ごとの組み立て ──────────────────────────────────────


def test_where_equals_single_value_string() -> None:
    """``equals`` 単一値の文字列は ``= '...'`` 。"""
    column_map = _column_map_with_types([("Name", "Name", "string")])
    record = _make_record(
        detail_columns=["Name"],
        column_map=column_map,
        report_filters=[{"column": "Name", "operator": "equals", "value": "Acme"}],
    )
    draft = build_soql(record)
    assert "WHERE Name = 'Acme'" in draft.soql


def test_where_equals_multiple_values_become_in() -> None:
    """``equals`` の値がカンマ区切りで複数なら ``IN (...)`` 。"""
    column_map = _column_map_with_types([("StageName", "StageName", "picklist")])
    record = _make_record(
        detail_columns=["StageName"],
        column_map=column_map,
        report_filters=[
            {"column": "StageName", "operator": "equals", "value": "Closed Won,Closed Lost"}
        ],
    )
    draft = build_soql(record)
    assert "WHERE StageName IN ('Closed Won', 'Closed Lost')" in draft.soql


def test_where_not_equal_single_value() -> None:
    """``notEqual`` 単一値は ``!= '...'`` 。"""
    column_map = _column_map_with_types([("Name", "Name", "string")])
    record = _make_record(
        detail_columns=["Name"],
        column_map=column_map,
        report_filters=[{"column": "Name", "operator": "notEqual", "value": "Acme"}],
    )
    draft = build_soql(record)
    assert "WHERE Name != 'Acme'" in draft.soql


def test_where_not_equal_multiple_become_not_in() -> None:
    """``notEqual`` 複数値は ``NOT IN (...)`` 。"""
    column_map = _column_map_with_types([("StageName", "StageName", "picklist")])
    record = _make_record(
        detail_columns=["StageName"],
        column_map=column_map,
        report_filters=[
            {"column": "StageName", "operator": "notEqual", "value": "Closed Won,Closed Lost"}
        ],
    )
    draft = build_soql(record)
    assert "WHERE StageName NOT IN ('Closed Won', 'Closed Lost')" in draft.soql


def test_where_less_than_greater_than_operators() -> None:
    """``lessThan`` / ``greaterThan`` / ``lessOrEqual`` / ``greaterOrEqual`` 。"""
    column_map = _column_map_with_types([("Amount", "Amount", "currency")])
    cases = [
        ("lessThan", "<"),
        ("greaterThan", ">"),
        ("lessOrEqual", "<="),
        ("greaterOrEqual", ">="),
    ]
    for operator, symbol in cases:
        record = _make_record(
            detail_columns=["Amount"],
            column_map=column_map,
            report_filters=[{"column": "Amount", "operator": operator, "value": "100"}],
        )
        draft = build_soql(record)
        assert f"WHERE Amount {symbol} 100" in draft.soql, operator


def test_where_contains_becomes_like_wildcards() -> None:
    """``contains`` は ``LIKE '%v%'`` 。"""
    column_map = _column_map_with_types([("Name", "Name", "string")])
    record = _make_record(
        detail_columns=["Name"],
        column_map=column_map,
        report_filters=[{"column": "Name", "operator": "contains", "value": "acme"}],
    )
    draft = build_soql(record)
    assert "WHERE Name LIKE '%acme%'" in draft.soql


def test_where_contains_multiple_becomes_or_group() -> None:
    """``contains`` 複数値は ``(A OR B)`` 。"""
    column_map = _column_map_with_types([("Name", "Name", "string")])
    record = _make_record(
        detail_columns=["Name"],
        column_map=column_map,
        report_filters=[{"column": "Name", "operator": "contains", "value": "ac,foo"}],
    )
    draft = build_soql(record)
    assert "( Name LIKE '%ac%' OR Name LIKE '%foo%' )" in draft.soql


def test_where_not_contain_becomes_and_of_not_like() -> None:
    """``notContain`` は ``( NOT X LIKE '%v%')`` を AND で並べる。"""
    column_map = _column_map_with_types([("Name", "Name", "string")])
    record = _make_record(
        detail_columns=["Name"],
        column_map=column_map,
        report_filters=[{"column": "Name", "operator": "notContain", "value": "ac,foo"}],
    )
    draft = build_soql(record)
    # ``( NOT (Name LIKE '%ac%') AND NOT (Name LIKE '%foo%') )`` という括弧付きの表現
    assert "( NOT (Name LIKE '%ac%') AND NOT (Name LIKE '%foo%') )" in draft.soql.replace(
        "\n  AND\n  ", " AND "
    )


def test_where_starts_with_becomes_like_prefix() -> None:
    """``startsWith`` は ``LIKE 'v%'`` 。"""
    column_map = _column_map_with_types([("Name", "Name", "string")])
    record = _make_record(
        detail_columns=["Name"],
        column_map=column_map,
        report_filters=[{"column": "Name", "operator": "startsWith", "value": "Acme"}],
    )
    draft = build_soql(record)
    assert "WHERE Name LIKE 'Acme%'" in draft.soql


def test_where_includes_and_excludes() -> None:
    """``includes`` / ``excludes`` は ``INCLUDES`` / ``EXCLUDES`` 。"""
    column_map = _column_map_with_types([("Hobbies__c", "Hobbies__c", "multipicklist")])
    record = _make_record(
        detail_columns=["Hobbies__c"],
        column_map=column_map,
        report_filters=[
            {"column": "Hobbies__c", "operator": "includes", "value": "Soccer,Baseball"}
        ],
    )
    draft = build_soql(record)
    assert "WHERE Hobbies__c INCLUDES ('Soccer', 'Baseball')" in draft.soql

    record2 = _make_record(
        detail_columns=["Hobbies__c"],
        column_map=column_map,
        report_filters=[{"column": "Hobbies__c", "operator": "excludes", "value": "Soccer"}],
    )
    draft2 = build_soql(record2)
    assert "WHERE Hobbies__c EXCLUDES ('Soccer')" in draft2.soql


def test_where_equals_with_empty_value_becomes_null() -> None:
    """``equals`` / ``notEqual`` で値が空のときは ``null`` 比較。"""
    column_map = _column_map_with_types([("Name", "Name", "string")])
    record = _make_record(
        detail_columns=["Name"],
        column_map=column_map,
        report_filters=[{"column": "Name", "operator": "equals", "value": ""}],
    )
    draft = build_soql(record)
    assert "WHERE Name = null" in draft.soql

    record2 = _make_record(
        detail_columns=["Name"],
        column_map=column_map,
        report_filters=[{"column": "Name", "operator": "notEqual", "value": ""}],
    )
    draft2 = build_soql(record2)
    assert "WHERE Name != null" in draft2.soql


def test_where_within_is_unsupported() -> None:
    """``within`` は未対応で missing に入る。"""
    column_map = _column_map_with_types([("Name", "Name", "string")])
    record = _make_record(
        detail_columns=["Name"],
        column_map=column_map,
        report_filters=[{"column": "Name", "operator": "within", "value": ""}],
    )
    draft = build_soql(record)
    assert draft.status == STATUS_PARTIAL
    assert any("within" in m for m in draft.missing)


# ── WHERE 句: 値の書き方 (型) ─────────────────────────────────────────


def test_value_string_escapes_quotes_and_backslash() -> None:
    """文字列は ``'`` と ``\\`` をエスケープする。"""
    column_map = _column_map_with_types([("Name", "Name", "string")])
    record = _make_record(
        detail_columns=["Name"],
        column_map=column_map,
        report_filters=[{"column": "Name", "operator": "equals", "value": "O'Reilly\\Co"}],
    )
    draft = build_soql(record)
    assert "WHERE Name = 'O\\'Reilly\\\\Co'" in draft.soql


def test_value_boolean_writes_true_false() -> None:
    """boolean は ``true`` / ``false`` 。"""
    column_map = _column_map_with_types([("IsClosed", "IsClosed", "boolean")])
    record = _make_record(
        detail_columns=["IsClosed"],
        column_map=column_map,
        report_filters=[{"column": "IsClosed", "operator": "equals", "value": "true"}],
    )
    draft = build_soql(record)
    assert "WHERE IsClosed = true" in draft.soql


def test_value_numeric_writes_as_is() -> None:
    """数値はそのまま書く。"""
    column_map = _column_map_with_types([("Amount", "Amount", "currency")])
    record = _make_record(
        detail_columns=["Amount"],
        column_map=column_map,
        report_filters=[{"column": "Amount", "operator": "greaterThan", "value": "100.5"}],
    )
    draft = build_soql(record)
    assert "WHERE Amount > 100.5" in draft.soql


def test_value_date_relative_literal_keeps_as_is() -> None:
    """date の相対日付リテラル (``THIS_MONTH`` 等) はそのまま。"""
    column_map = _column_map_with_types([("CloseDate", "CloseDate", "date")])
    record = _make_record(
        detail_columns=["CloseDate"],
        column_map=column_map,
        report_filters=[{"column": "CloseDate", "operator": "equals", "value": "THIS_MONTH"}],
    )
    draft = build_soql(record)
    assert "WHERE CloseDate = THIS_MONTH" in draft.soql


def test_value_date_with_number_keeps_as_is() -> None:
    """date の ``LAST_N_DAYS:30`` のような相対リテラルもそのまま。"""
    column_map = _column_map_with_types([("CloseDate", "CloseDate", "date")])
    record = _make_record(
        detail_columns=["CloseDate"],
        column_map=column_map,
        report_filters=[{"column": "CloseDate", "operator": "equals", "value": "LAST_N_DAYS:30"}],
    )
    draft = build_soql(record)
    assert "WHERE CloseDate = LAST_N_DAYS:30" in draft.soql


def test_value_datetime_becomes_utc_midnight_and_adds_check() -> None:
    """datetime で日付だけの値は ``YYYY-MM-DDT00:00:00Z`` に拡張し checks に追加。"""
    column_map = _column_map_with_types([("CloseDateTime__c", "CloseDateTime__c", "datetime")])
    record = _make_record(
        detail_columns=["CloseDateTime__c"],
        column_map=column_map,
        report_filters=[
            {"column": "CloseDateTime__c", "operator": "greaterOrEqual", "value": "2025-01-01"}
        ],
    )
    draft = build_soql(record)
    assert "WHERE CloseDateTime__c >= 2025-01-01T00:00:00Z" in draft.soql
    assert any("日時の条件をタイムゾーン無しの UTC 0 時で置き換えた" in c for c in draft.checks)


def test_value_datetime_relative_literal_keeps_as_is() -> None:
    """datetime の相対日付リテラルはそのまま。"""
    column_map = _column_map_with_types([("CloseDateTime__c", "CloseDateTime__c", "datetime")])
    record = _make_record(
        detail_columns=["CloseDateTime__c"],
        column_map=column_map,
        report_filters=[
            {"column": "CloseDateTime__c", "operator": "equals", "value": "THIS_MONTH"}
        ],
    )
    draft = build_soql(record)
    assert "WHERE CloseDateTime__c = THIS_MONTH" in draft.soql


def test_like_value_with_wildcard_characters_is_escaped() -> None:
    """contains の値に ``%`` '_' が含まれていたら、 リテラルの中でエスケープする。"""
    column_map = _column_map_with_types([("Name", "Name", "string")])
    record = _make_record(
        detail_columns=["Name"],
        column_map=column_map,
        report_filters=[{"column": "Name", "operator": "contains", "value": "100%"}],
    )
    draft = build_soql(record)
    assert "WHERE Name LIKE '%100\\%%'" in draft.soql
    # ワイルドカードにはしないので、 注意書きは出さない
    assert not any("ワイルドカード" in c for c in draft.checks)


def test_value_unknown_type_goes_to_missing() -> None:
    """型が分からない列の値は missing に入る。"""
    column_map = _column_map_with_types([("Unknown", "Unknown", "unknown_type_xyz")])
    record = _make_record(
        detail_columns=["Unknown"],
        column_map=column_map,
        report_filters=[{"column": "Unknown", "operator": "equals", "value": "X"}],
    )
    draft = build_soql(record)
    assert any("解釈できません" in m for m in draft.missing)


# ── 組立式 ─────────────────────────────────────────────────────────────


def test_report_boolean_filter_replaces_numbers_correctly() -> None:
    """組立式 ``1 AND (2 OR 10)`` で ``1`` と ``10`` を取り違えない。"""
    column_map = _column_map_with_types(
        [
            ("Name", "Name", "string"),
            ("StageName", "StageName", "picklist"),
            ("Amount", "Amount", "currency"),
        ]
    )
    record = _make_record(
        detail_columns=["Name", "StageName", "Amount"],
        column_map=column_map,
        report_filters=[
            {"column": "Name", "operator": "equals", "value": "Acme"},
            {"column": "StageName", "operator": "equals", "value": "Closed Won"},
            {"column": "Amount", "operator": "greaterThan", "value": "100"},
        ],
        report_boolean_filter="1 AND (2 OR 10)",
    )
    draft = build_soql(record)
    # ``1`` → 条件 1 (Acme)、 ``2`` → 条件 2 (StageName)、 ``10`` → 条件 10 は無い
    # 番号 ``10`` は条件 3 (Amount) ではなく「範囲外」になる (ここでの 10 は文字列の 10)
    # …実際に条件は 3 件なので 10 は範囲外。 missing になる。
    # → 範囲外で ``組立式の条件を作れない`` が出て WHERE 全体は出ない
    assert draft.status == STATUS_PARTIAL
    assert any("組立式" in m for m in draft.missing)
    assert "WHERE" not in draft.soql


def test_report_boolean_filter_with_correct_numbers() -> None:
    """組立式 ``1 AND (2 OR 3)`` が条件 1, 2, 3 と対応する。"""
    column_map = _column_map_with_types(
        [
            ("Name", "Name", "string"),
            ("StageName", "StageName", "picklist"),
            ("Amount", "Amount", "currency"),
        ]
    )
    record = _make_record(
        detail_columns=["Name", "StageName", "Amount"],
        column_map=column_map,
        report_filters=[
            {"column": "Name", "operator": "equals", "value": "Acme"},
            {"column": "StageName", "operator": "equals", "value": "Closed Won"},
            {"column": "Amount", "operator": "greaterThan", "value": "100"},
        ],
        report_boolean_filter="1 AND (2 OR 3)",
    )
    draft = build_soql(record)
    soql_normalized = draft.soql.replace("\n  AND\n  ", " AND ")
    assert (
        "( Name = 'Acme' ) AND (( StageName = 'Closed Won' ) OR ( Amount > 100 ))"
        in soql_normalized
    )


def test_report_boolean_filter_distinguishes_1_and_10() -> None:
    """組立式 ``1 AND 10`` が条件 1, 10 と対応する (``1`` と ``10`` を取り違えない) 。

    ``(1)0`` のように ``1`` と ``0`` に分解されると条件 1 が 2 回使われ、
    条件 10 (ここでの ``StageName``) が消える壊れた実装だと WHERE が壊れる。
    """
    rows: list[tuple[str, str, str]] = [(f"Col{i}", f"Field{i}", "string") for i in range(1, 11)]
    column_map = _column_map_with_types(rows)
    report_filters = [
        {"column": f"Col{i}", "operator": "equals", "value": f"V{i}"} for i in range(1, 11)
    ]
    record = _make_record(
        detail_columns=[f"Col{i}" for i in range(1, 11)],
        column_map=column_map,
        report_filters=report_filters,
        report_boolean_filter="1 AND 10",
    )
    draft = build_soql(record)
    soql_normalized = draft.soql.replace("\n  AND\n  ", " AND ")
    # 正しい実装: 条件 1 = Field1, 条件 10 = Field10。 ``Field1`` と ``Field10`` が両方出ること
    assert "( Field1 = 'V1' )" in soql_normalized
    assert "( Field10 = 'V10' )" in soql_normalized
    # 単純な ``str.replace("1", "...").replace("10", "...")`` だと
    # ``1`` を置換したあとに ``10`` が ``"Field1" + "0"`` になるので ``Field1`` が 2 回出ない
    assert soql_normalized.count("Field1 = ") == 1


def test_unresolved_filter_column_omitted_and_reported() -> None:
    """対応が取れない条件は省き missing に「条件 n: ... (省いたので件数が増える可能性)」。"""
    column_map = _column_map_with_types([("Name", "Name", "string")])
    # Unresolved は column_map に無い
    record = _make_record(
        detail_columns=["Name"],
        column_map=column_map,
        report_filters=[
            {"column": "Unknown", "operator": "equals", "value": "X"},
            {"column": "Name", "operator": "equals", "value": "Acme"},
        ],
    )
    draft = build_soql(record)
    assert "Unknown" not in draft.soql
    assert "Name = 'Acme'" in draft.soql
    assert any("条件 1: Unknown" in m and "件数が増える可能性" in m for m in draft.missing)


def test_unresolved_filter_with_boolean_filter_omits_where() -> None:
    """組立式があるときに条件を省くと式が壊れるので WHERE 全体を作らない。"""
    column_map = _column_map_with_types([("Name", "Name", "string")])
    record = _make_record(
        detail_columns=["Name"],
        column_map=column_map,
        report_filters=[
            {"column": "Unknown", "operator": "equals", "value": "X"},
        ],
        report_boolean_filter="1 AND 2",
    )
    draft = build_soql(record)
    assert "WHERE" not in draft.soql
    assert any("組立式の条件を作れない" in m for m in draft.missing)


# ── standardDateFilter ────────────────────────────────────────────────


def test_standard_date_filter_custom_with_both_dates() -> None:
    """``durationValue=CUSTOM`` で start / end 両方あるときは ``>=`` / ``<=`` を AND で。"""
    column_map = _column_map_with_types([("CloseDate", "CloseDate", "date")])
    record = _make_record(
        detail_columns=["CloseDate"],
        column_map=column_map,
        standard_date_filter={
            "column": "CloseDate",
            "durationValue": "CUSTOM",
            "startDate": "2025-01-01",
            "endDate": "2025-12-31",
        },
    )
    draft = build_soql(record)
    # 改行は `` AND `` に正規化して比較
    soql_normalized = draft.soql.replace("\n  AND\n  ", " AND ")
    assert "WHERE CloseDate >= 2025-01-01 AND CloseDate <= 2025-12-31" in soql_normalized


def test_standard_date_filter_custom_with_only_start() -> None:
    """``startDate`` のみ。"""
    column_map = _column_map_with_types([("CloseDate", "CloseDate", "date")])
    record = _make_record(
        detail_columns=["CloseDate"],
        column_map=column_map,
        standard_date_filter={
            "column": "CloseDate",
            "durationValue": "CUSTOM",
            "startDate": "2025-01-01",
            "endDate": "",
        },
    )
    draft = build_soql(record)
    assert "WHERE CloseDate >= 2025-01-01" in draft.soql


def test_standard_date_filter_relative_literal() -> None:
    """``durationValue`` が相対リテラルなら ``=``。"""
    column_map = _column_map_with_types([("CloseDate", "CloseDate", "date")])
    record = _make_record(
        detail_columns=["CloseDate"],
        column_map=column_map,
        standard_date_filter={
            "column": "CloseDate",
            "durationValue": "THIS_MONTH",
            "startDate": "",
            "endDate": "",
        },
    )
    draft = build_soql(record)
    assert "WHERE CloseDate = THIS_MONTH" in draft.soql


def test_standard_date_filter_empty_duration_emits_nothing() -> None:
    """``durationValue`` 空 (全期間) は何も付けない。"""
    column_map = _column_map_with_types([("CloseDate", "CloseDate", "date")])
    record = _make_record(
        detail_columns=["CloseDate"],
        column_map=column_map,
        standard_date_filter={"column": "CloseDate", "durationValue": ""},
    )
    draft = build_soql(record)
    assert "WHERE" not in draft.soql


def test_standard_date_filter_datetime_uses_utc_with_checks() -> None:
    """datetime 型の日付条件は ``YYYY-MM-DDT00:00:00Z`` に拡張し checks に追加。"""
    column_map = _column_map_with_types([("CloseDateTime__c", "CloseDateTime__c", "datetime")])
    record = _make_record(
        detail_columns=["CloseDateTime__c"],
        column_map=column_map,
        standard_date_filter={
            "column": "CloseDateTime__c",
            "durationValue": "CUSTOM",
            "startDate": "2025-01-01",
            "endDate": "2025-01-31",
        },
    )
    draft = build_soql(record)
    assert "CloseDateTime__c >= 2025-01-01T00:00:00Z" in draft.soql
    assert "CloseDateTime__c <= 2025-01-31T23:59:59Z" in draft.soql
    assert any("UTC" in c for c in draft.checks)


def test_standard_date_filter_unresolved_column_omitted() -> None:
    """``standardDateFilter`` の列が column_map に無いときは省いて missing。"""
    column_map = _column_map_with_types([("Name", "Name", "string")])
    record = _make_record(
        detail_columns=["Name"],
        column_map=column_map,
        standard_date_filter={
            "column": "Unknown",
            "durationValue": "THIS_MONTH",
            "startDate": "",
            "endDate": "",
        },
    )
    draft = build_soql(record)
    assert "WHERE" not in draft.soql
    assert any("Unknown" in m for m in draft.missing)


# ── scope / standardFilters / crossFilters ────────────────────────────


def test_scope_organization_adds_nothing() -> None:
    """``scope=organization`` は何も足さない。"""
    column_map = _column_map_with_types([("Name", "Name", "string")])
    record = _make_record(
        detail_columns=["Name"],
        column_map=column_map,
        scope="organization",
    )
    draft = build_soql(record)
    assert draft.status == STATUS_CREATED
    assert draft.checks == ()


def test_scope_mine_adds_check() -> None:
    """``scope=MyTerritory`` は checks に追加。"""
    column_map = _column_map_with_types([("Name", "Name", "string")])
    record = _make_record(
        detail_columns=["Name"],
        column_map=column_map,
        scope="MyTerritory",
    )
    draft = build_soql(record)
    assert draft.status == STATUS_NEEDS_CHECK
    assert any("スコープ" in c and "MyTerritory" in c for c in draft.checks)


def test_standard_filters_go_to_checks() -> None:
    """``standardFilters`` は checks に並ぶ。"""
    column_map = _column_map_with_types([("Name", "Name", "string")])
    record = _make_record(
        detail_columns=["Name"],
        column_map=column_map,
        standard_filters=[{"name": "foo", "value": "bar"}],
    )
    draft = build_soql(record)
    assert draft.status == STATUS_NEEDS_CHECK
    assert any("標準フィルタ" in c for c in draft.checks)


def test_cross_filters_go_to_missing() -> None:
    """``crossFilters`` は missing に並ぶ。"""
    column_map = _column_map_with_types([("Name", "Name", "string")])
    record = _make_record(
        detail_columns=["Name"],
        column_map=column_map,
        cross_filters=[
            {
                "primaryEntityField": "AccountId",
                "operator": "with",
                "relatedEntity": "Contact",
                "relatedEntityJoinField": "AccountId",
            }
        ],
    )
    draft = build_soql(record)
    assert draft.status == STATUS_PARTIAL
    assert any("クロスフィルタ" in m for m in draft.missing)


# ── クロスフィルタの副問い合わせ ────────────────────────────────────


def _main_describe_with_child() -> dict[str, Any]:
    """主オブジェクトの describe （ 子リレーション 1 件つき ） 。"""
    return {
        "name": "Opportunity",
        "label": "商談",
        "custom": False,
        "fields": [
            {"name": "Id", "label": "商机ID", "type": "id"},
            {"name": "Name", "label": "商談名", "type": "string"},
            {"name": "AccountId", "label": "取引先", "type": "reference"},
            {"name": "StageName", "label": "フェーズ", "type": "picklist"},
        ],
        "childRelationships": [
            {
                "childSObject": "OpportunityLineItem",
                "field": "OpportunityId",
                "relationshipName": "OpportunityLineItems",
            }
        ],
    }


def _child_describe() -> dict[str, Any]:
    """子オブジェクトの describe 。"""
    return {
        "name": "OpportunityLineItem",
        "label": "商机明細",
        "custom": False,
        "fields": [
            {"name": "OpportunityId", "label": "商談", "type": "reference"},
            {"name": "Product2Id", "label": "商品", "type": "reference"},
            {"name": "Quantity", "label": "数量", "type": "double"},
            {"name": "Description", "label": "備考", "type": "string"},
            {"name": "Notes", "label": "メモ", "type": "textarea"},
        ],
        "childRelationships": [],
    }


def _cross_filter(**overrides: Any) -> dict[str, Any]:
    """副問い合わせにできるクロスフィルタ。 上書きで欠けた項目を作れる。

    主側の項目は ``id`` 型（ ``Id`` ） にする。 ``reference`` 型は
    「直接でない」 の判定になる。
    """
    base: dict[str, Any] = {
        "criteria": [{"column": "Product2Id", "operator": "equals", "value": "01t000000000001"}],
        "includesObject": True,
        "primaryEntityField": "Id",
        "relatedEntity": "OpportunityLineItem",
        "relatedEntityJoinField": "OpportunityId",
    }
    base.update(overrides)
    return base


def _cross_filter_record(
    cross_filters: list[dict[str, Any]],
    **kwargs: Any,
) -> ReportRecord:
    """副問い合わせの試験用の record を作る。"""
    column_map = _column_map_with_types([("Name", "Name", "string")])
    return _make_record(
        detail_columns=["Name"],
        column_map=column_map,
        cross_filters=cross_filters,
        objects={"Opportunity": _main_describe_with_child()},
        cross_filter_objects={"OpportunityLineItem": _child_describe()},
        **kwargs,
    )


def test_cross_filter_becomes_in_subquery() -> None:
    """``includesObject`` が True のクロスフィルタは ``IN`` の副問い合わせになる。"""
    record = _cross_filter_record([_cross_filter()])
    draft = build_soql(record)

    assert draft.soql == (
        "SELECT Name\n"
        "FROM Opportunity\n"
        "WHERE Id IN (SELECT OpportunityId FROM OpportunityLineItem "
        "WHERE Product2Id = '01t000000000001')"
    )
    assert draft.count_soql == (
        "SELECT COUNT()\n"
        "FROM Opportunity\n"
        "WHERE Id IN (SELECT OpportunityId FROM OpportunityLineItem "
        "WHERE Product2Id = '01t000000000001')"
    )
    assert draft.status == STATUS_NEEDS_CHECK
    assert draft.use_bulk is False
    assert draft.bulk_blockers
    assert any("クロスフィルタを副問い合わせにした" in c for c in draft.checks)


def test_cross_filter_becomes_not_in_subquery() -> None:
    """``includesObject`` が False のクロスフィルタは ``NOT IN`` の副問い合わせになる。"""
    record = _cross_filter_record([_cross_filter(includesObject=False)])
    draft = build_soql(record)

    assert draft.soql == (
        "SELECT Name\n"
        "FROM Opportunity\n"
        "WHERE Id NOT IN (SELECT OpportunityId FROM OpportunityLineItem "
        "WHERE Product2Id = '01t000000000001')"
    )
    assert draft.status == STATUS_NEEDS_CHECK
    assert draft.use_bulk is False


def test_cross_filter_without_criteria_has_no_inner_where() -> None:
    """``criteria`` が空のときは内側の WHERE を出さない。"""
    record = _cross_filter_record([_cross_filter(criteria=[])])
    draft = build_soql(record)

    assert draft.soql == (
        "SELECT Name\nFROM Opportunity\nWHERE Id IN (SELECT OpportunityId FROM OpportunityLineItem)"
    )


@pytest.mark.parametrize(
    "criteria",
    [None, "文字列"],
    ids=["none", "not_a_list"],
)
def test_cross_filter_with_unusable_criteria_goes_to_missing(criteria: Any) -> None:
    """``criteria`` が無い（ キーがない・ ``None`` ） ときは missing にする。

    絞り込みが消えた副問い合わせはレポートより多い件数を取るため、 推測で
    埋めない。 空リスト ``[]`` のときだけ「条件なし」として通す。
    """
    record = _cross_filter_record([_cross_filter(criteria=criteria)])
    draft = build_soql(record)

    assert draft.status == STATUS_PARTIAL
    assert draft.soql.startswith(INCOMPLETE_MARKER)
    assert draft.use_bulk is True
    assert any("クロスフィルタ" in m and "criteria" in m for m in draft.missing)


def test_cross_filter_without_criteria_key_goes_to_missing() -> None:
    """``criteria`` のキー自体が無いときは missing にする。"""
    cross_filter = _cross_filter()
    del cross_filter["criteria"]
    record = _cross_filter_record([cross_filter])
    draft = build_soql(record)

    assert draft.status == STATUS_PARTIAL
    assert draft.use_bulk is True
    assert any("クロスフィルタ" in m and "criteria" in m for m in draft.missing)


def test_cross_filter_criteria_checks_are_kept() -> None:
    """内側の条件の ``checks`` （ ロングテキストの注意 ） は捨てない。"""
    record = _cross_filter_record(
        [
            _cross_filter(
                criteria=[{"column": "Notes", "operator": "equals", "value": "メモ"}],
            )
        ]
    )
    draft = build_soql(record)

    assert "Notes = 'メモ'" in draft.soql
    assert any("ロングテキストは絞り込めない場合がある: Notes" in c for c in draft.checks)


def test_cross_filter_subquery_is_wrapped_with_boolean_filter() -> None:
    """組立式 （ ``1 OR 2`` ） と副問い合わせが並ぶときは 組立式が ``( ... )`` で囲まれる。"""
    column_map = _column_map_with_types(
        [("Name", "Name", "string"), ("StageName", "StageName", "string")]
    )
    record = _make_record(
        detail_columns=["Name"],
        column_map=column_map,
        report_filters=[
            {"column": "Name", "operator": "equals", "value": "A"},
            {"column": "StageName", "operator": "equals", "value": "B"},
        ],
        report_boolean_filter="1 OR 2",
        cross_filters=[_cross_filter()],
        objects={"Opportunity": _main_describe_with_child()},
        cross_filter_objects={"OpportunityLineItem": _child_describe()},
    )
    draft = build_soql(record)

    assert draft.soql == (
        "SELECT Name\n"
        "FROM Opportunity\n"
        "WHERE ( ( Name = 'A' ) OR ( StageName = 'B' ) )\n"
        "  AND\n"
        "  Id IN (SELECT OpportunityId FROM OpportunityLineItem "
        "WHERE Product2Id = '01t000000000001')"
    )


def test_cross_filter_value_is_escaped() -> None:
    """値の ``'`` はエスケープされる。"""
    record = _cross_filter_record(
        [
            _cross_filter(
                criteria=[{"column": "Description", "operator": "equals", "value": "O'Brien"}]
            )
        ]
    )
    draft = build_soql(record)

    assert "Description = 'O\\'Brien'" in draft.soql


@pytest.mark.parametrize(
    ("cross_filter", "reason_part"),
    [
        ({"includesObject": None}, "includesObject"),
        ({"primaryEntityField": "NoSuchField"}, "primaryEntityField"),
        ({"primaryEntityField": "StageName"}, "id でない"),
        ({"relatedEntity": "NoSuchChild"}, "子リレーション"),
        ({"relatedEntityJoinField": "NoSuchField"}, "NoSuchField"),
        (
            {"criteria": [{"column": "NoSuchColumn", "operator": "equals", "value": "x"}]},
            "NoSuchColumn",
        ),
    ],
)
def test_cross_filter_that_cannot_be_converted_goes_to_missing(
    cross_filter: dict[str, Any],
    reason_part: str,
) -> None:
    """確かめられない項目があるときは missing になり、 USE_BULK は True のまま。"""
    record = _cross_filter_record([_cross_filter(**cross_filter)])
    draft = build_soql(record)

    assert draft.status == STATUS_PARTIAL
    assert draft.soql.startswith(INCOMPLETE_MARKER)
    assert draft.use_bulk is True
    assert draft.bulk_blockers == ()
    assert any("クロスフィルタ" in m and reason_part in m for m in draft.missing)


def test_cross_filter_without_child_describe_goes_to_missing() -> None:
    """子オブジェクトの describe が無いときは missing になる。"""
    column_map = _column_map_with_types([("Name", "Name", "string")])
    record = _make_record(
        detail_columns=["Name"],
        column_map=column_map,
        cross_filters=[_cross_filter()],
        objects={"Opportunity": _main_describe_with_child()},
    )
    draft = build_soql(record)

    assert draft.status == STATUS_PARTIAL
    assert draft.use_bulk is True
    assert any("describe が無い" in m for m in draft.missing)


def test_third_cross_filter_goes_to_missing() -> None:
    """副問い合わせは 1 文に 2 個まで。 3 個目は missing になる。"""
    record = _cross_filter_record(
        [
            _cross_filter(),
            _cross_filter(),
            _cross_filter(),
        ]
    )
    draft = build_soql(record)

    assert draft.status == STATUS_PARTIAL
    assert draft.use_bulk is False
    assert draft.soql.count(" IN (SELECT ") == 2
    assert any("2 個まで" in m for m in draft.missing)


def test_cross_filter_primary_field_resolves_by_column_map() -> None:
    """主側の項目は 列対応表の列キーと一致する項目でも解決できる。"""
    column_map = _column_map_with_types([("Name", "Name", "string"), ("OPP_ID", "Id", "id")])
    record = _make_record(
        detail_columns=["Name"],
        column_map=column_map,
        cross_filters=[_cross_filter(primaryEntityField="OPP_ID")],
        objects={"Opportunity": _main_describe_with_child()},
        cross_filter_objects={"OpportunityLineItem": _child_describe()},
    )
    draft = build_soql(record)

    assert "Id IN (SELECT OpportunityId FROM OpportunityLineItem" in draft.soql


# ── クロスフィルタの繋がりの判定 ────────────────────────────────────────


def _main_describe_with_two_contacts() -> dict[str, Any]:
    """同じ子オブジェクト（ Contact ） へ 2 本の参照がある 主オブジェクト。"""
    return {
        "name": "Account",
        "label": "取引先",
        "custom": False,
        "fields": [
            {"name": "Id", "label": "取引先ID", "type": "id"},
            {"name": "Name", "label": "取引先名", "type": "string"},
        ],
        "childRelationships": [
            {
                "childSObject": "Contact",
                "field": "AccountId",
                "relationshipName": "Contacts",
            },
            {
                "childSObject": "Contact",
                "field": "ParentAccount__c",
                "relationshipName": "ParentContacts",
            },
        ],
    }


def _contact_describe() -> dict[str, Any]:
    """子オブジェクト（ Contact ） の describe 。"""
    return {
        "name": "Contact",
        "label": "取引先担当者",
        "custom": False,
        "fields": [
            {"name": "AccountId", "label": "取引先", "type": "reference"},
            {"name": "ParentAccount__c", "label": "親取引先", "type": "reference"},
            {"name": "LastName", "label": "姓", "type": "string"},
        ],
        "childRelationships": [],
    }


def _contact_cross_filter(**overrides: Any) -> dict[str, Any]:
    """Contact への参照を使うクロスフィルタ。"""
    base: dict[str, Any] = {
        "criteria": [],
        "includesObject": True,
        "primaryEntityField": "Id",
        "relatedEntity": "Contact",
        "relatedEntityJoinField": "AccountId",
    }
    base.update(overrides)
    return base


def _contact_record(
    cross_filters: list[dict[str, Any]],
    **kwargs: Any,
) -> ReportRecord:
    """Contact への参照を使うクロスフィルタの試験用の record を作る。"""
    column_map = _column_map_with_types([("Name", "Name", "string")])
    return _make_record(
        main_object="Account",
        detail_columns=["Name"],
        column_map=column_map,
        cross_filters=cross_filters,
        objects={"Account": _main_describe_with_two_contacts()},
        cross_filter_objects={"Contact": _contact_describe()},
        **kwargs,
    )


def test_cross_filter_link_is_direct_and_converted() -> None:
    """直接つながるリレーションは ``直接`` で、 副問い合わせに変換される。"""
    record = _cross_filter_record([_cross_filter()])
    draft = build_soql(record)

    assert draft.cross_filter_links == (
        CrossFilterLink(
            index=1,
            status=CROSS_FILTER_LINK_DIRECT,
            primary_field="Id",
            child_object="OpportunityLineItem",
            join_field="OpportunityId",
            relationship_name="OpportunityLineItems",
            converted=True,
            reason="",
        ),
    )


def test_cross_filter_link_is_not_direct_when_primary_is_reference() -> None:
    """主側の項目が ``reference`` なら 「直接でない」 になる。"""
    record = _cross_filter_record([_cross_filter(primaryEntityField="AccountId")])
    draft = build_soql(record)

    assert draft.status == STATUS_PARTIAL
    assert draft.soql.startswith(INCOMPLETE_MARKER)
    assert draft.use_bulk is True
    link = draft.cross_filter_links[0]
    assert link.status == CROSS_FILTER_LINK_NOT_DIRECT
    assert link.converted is False
    assert link.primary_field == "AccountId"
    assert "id でない" in link.reason
    assert any("クロスフィルタ" in m and "id でない" in m for m in draft.missing)


def test_cross_filter_link_is_not_direct_when_related_entity_is_unknown() -> None:
    """``relatedEntity`` が子リレーションに無いなら 「直接でない」 になる。"""
    record = _cross_filter_record([_cross_filter(relatedEntity="NoSuchChild")])
    draft = build_soql(record)

    link = draft.cross_filter_links[0]
    assert link.status == CROSS_FILTER_LINK_NOT_DIRECT
    assert link.converted is False
    assert "子リレーション" in link.reason


def test_cross_filter_link_is_not_direct_when_join_field_is_not_a_relationship_field() -> None:
    """同じ子へ 2 本の参照があるとき、 違う結合項目は 「直接でない」 になる。

    reason には その子リレーションの実際の参照項目が並ぶ。
    """
    record = _contact_record([_contact_cross_filter(relatedEntityJoinField="Other__c")])
    draft = build_soql(record)

    link = draft.cross_filter_links[0]
    assert link.status == CROSS_FILTER_LINK_NOT_DIRECT
    assert link.converted is False
    assert "Other__c" in link.reason
    assert "AccountId" in link.reason
    assert "ParentAccount__c" in link.reason


@pytest.mark.parametrize(
    ("join_field", "relationship_name"),
    [
        ("AccountId", "Contacts"),
        ("ParentAccount__c", "ParentContacts"),
    ],
)
def test_cross_filter_link_picks_the_relationship_of_the_join_field(
    join_field: str,
    relationship_name: str,
) -> None:
    """同じ子へ 2 本の参照があるとき、 結合項目に合うリレーションが選ばれる。"""
    record = _contact_record([_contact_cross_filter(relatedEntityJoinField=join_field)])
    draft = build_soql(record)

    link = draft.cross_filter_links[0]
    assert link.status == CROSS_FILTER_LINK_DIRECT
    assert link.converted is True
    assert link.relationship_name == relationship_name
    assert link.join_field == join_field
    assert link.child_object == "Contact"
    assert "Id IN (SELECT " in draft.soql


def test_cross_filter_link_is_unverified_when_child_relationships_are_empty() -> None:
    """主オブジェクトの ``childRelationships`` が空なら 「確認できず」 になる。"""
    column_map = _column_map_with_types([("Name", "Name", "string")])
    record = _make_record(
        detail_columns=["Name"],
        column_map=column_map,
        cross_filters=[_cross_filter()],
        objects={
            "Opportunity": {
                "name": "Opportunity",
                "label": "商談",
                "custom": False,
                "fields": [{"name": "Id", "label": "ID", "type": "id"}],
                "childRelationships": [],
            }
        },
        cross_filter_objects={"OpportunityLineItem": _child_describe()},
    )
    draft = build_soql(record)

    link = draft.cross_filter_links[0]
    assert link.status == CROSS_FILTER_LINK_UNVERIFIED
    assert link.converted is False
    assert "childRelationships が空" in link.reason


def test_cross_filter_link_is_unverified_without_child_describe() -> None:
    """子オブジェクトの describe が無いなら 「確認できず」 になる。"""
    column_map = _column_map_with_types([("Name", "Name", "string")])
    record = _make_record(
        detail_columns=["Name"],
        column_map=column_map,
        cross_filters=[_cross_filter()],
        objects={"Opportunity": _main_describe_with_child()},
    )
    draft = build_soql(record)

    link = draft.cross_filter_links[0]
    assert link.status == CROSS_FILTER_LINK_UNVERIFIED
    assert link.converted is False
    assert "describe が無い" in link.reason


def test_third_cross_filter_link_is_direct_but_not_converted() -> None:
    """3 個目も判定する。 判定は 「直接」 だが 副問い合わせには入らない。"""
    record = _cross_filter_record([_cross_filter(), _cross_filter(), _cross_filter()])
    draft = build_soql(record)

    assert len(draft.cross_filter_links) == 3
    third = draft.cross_filter_links[2]
    assert third.index == 3
    assert third.status == CROSS_FILTER_LINK_DIRECT
    assert third.converted is False
    assert "2 個まで" in third.reason


def test_cross_filter_link_without_criteria_is_direct_but_not_converted() -> None:
    """``criteria`` が無いときの判定は 「直接」 だが 副問い合わせには入らない。"""
    cross_filter = _cross_filter()
    del cross_filter["criteria"]
    record = _cross_filter_record([cross_filter])
    draft = build_soql(record)

    link = draft.cross_filter_links[0]
    assert link.status == CROSS_FILTER_LINK_DIRECT
    assert link.converted is False
    assert "criteria" in link.reason
    assert draft.use_bulk is True


def test_cross_filter_link_is_not_converted_when_boolean_filter_is_broken() -> None:
    """組立式が壊れて WHERE 全体を出さないときは、 副問い合わせは入らない。"""
    column_map = _column_map_with_types([("Name", "Name", "string")])
    record = _make_record(
        detail_columns=["Name"],
        column_map=column_map,
        report_filters=[{"column": "Name", "operator": "equals", "value": "A"}],
        report_boolean_filter="1 AND 2",
        cross_filters=[_cross_filter()],
        objects={"Opportunity": _main_describe_with_child()},
        cross_filter_objects={"OpportunityLineItem": _child_describe()},
    )
    draft = build_soql(record)

    link = draft.cross_filter_links[0]
    assert link.status == CROSS_FILTER_LINK_DIRECT
    assert link.converted is False
    assert "WHERE 全体を出さない" in link.reason
    assert draft.use_bulk is True


def test_cross_filter_links_are_kept_when_soql_cannot_be_built() -> None:
    """SELECT に入れられる列が無い（ 作れず ） でも 判定は残る。"""
    column_map = _column_map_with_types([("Name", "(不明)", "string")])
    record = _make_record(
        detail_columns=["Name"],
        column_map=column_map,
        cross_filters=[_cross_filter()],
        objects={"Opportunity": _main_describe_with_child()},
        cross_filter_objects={"OpportunityLineItem": _child_describe()},
    )
    draft = build_soql(record)

    assert draft.status == STATUS_NONE
    assert len(draft.cross_filter_links) == 1
    assert draft.cross_filter_links[0].status == CROSS_FILTER_LINK_DIRECT


def test_cross_filter_links_are_empty_without_cross_filters() -> None:
    """クロスフィルタが無ければ 判定は 0 件。"""
    record = _cross_filter_record([])
    draft = build_soql(record)

    assert draft.cross_filter_links == ()


def test_cross_filter_links_are_empty_when_main_object_is_unknown() -> None:
    """主オブジェクトが不明なら 判定も 0 件。"""
    column_map = _column_map_with_types([("Name", "Name", "string")])
    record = _make_record(
        main_object="",
        detail_columns=["Name"],
        column_map=column_map,
        cross_filters=[_cross_filter()],
    )
    draft = build_soql(record)

    assert draft.status == STATUS_NONE
    assert draft.cross_filter_links == ()


# ── ORDER BY / LIMIT ───────────────────────────────────────────────────


def test_sort_by_with_top_rows_adds_order_by_and_limit() -> None:
    """``sortBy`` と ``topRows`` から ORDER BY と LIMIT。"""
    column_map = _column_map_with_types(
        [("Name", "Name", "string"), ("Amount", "Amount", "currency")]
    )
    record = _make_record(
        detail_columns=["Name"],
        column_map=column_map,
        sort_by=[{"sortColumn": "Amount", "sortOrder": "Desc"}],
        top_rows={"rowLimit": 10, "direction": "Desc"},
    )
    draft = build_soql(record)
    assert "ORDER BY Amount DESC" in draft.soql
    assert "LIMIT 10" in draft.soql


def test_sort_by_unresolved_omitted() -> None:
    """``sortBy`` の列が column_map に無いときは省いて missing。"""
    column_map = _column_map_with_types([("Name", "Name", "string")])
    record = _make_record(
        detail_columns=["Name"],
        column_map=column_map,
        sort_by=[{"sortColumn": "Unknown", "sortOrder": "Asc"}],
    )
    draft = build_soql(record)
    assert "ORDER BY" not in draft.soql
    assert any("Unknown" in m for m in draft.missing)


def test_top_rows_without_sort_by_omits_limit() -> None:
    """``topRows`` だけあっても ``ORDER BY`` が無いので LIMIT は出さない。"""
    column_map = _column_map_with_types([("Name", "Name", "string")])
    record = _make_record(
        detail_columns=["Name"],
        column_map=column_map,
        top_rows={"rowLimit": 10},
    )
    draft = build_soql(record)
    assert "LIMIT" not in draft.soql
    assert any("LIMIT は出さない" in m for m in draft.missing)


# ── buckets / customSummaryFormula / aggregates ──────────────────────


def test_buckets_go_to_missing() -> None:
    """``buckets`` は missing に並ぶ (集計なので SOQL に入れない) 。"""
    column_map = _column_map_with_types([("Name", "Name", "string")])
    record = _make_record(
        detail_columns=["Name"],
        column_map=column_map,
        buckets=[{"developerName": "BucketX", "label": "バケットX"}],
    )
    draft = build_soql(record)
    assert draft.status == STATUS_PARTIAL
    assert any("バケット" in m for m in draft.missing)


def test_custom_summary_formula_go_to_missing() -> None:
    """``customSummaryFormula`` は missing に並ぶ。"""
    column_map = _column_map_with_types([("Name", "Name", "string")])
    record = _make_record(
        detail_columns=["Name"],
        column_map=column_map,
        custom_summary_formula=[{"formula": "1+1", "label": "独自式"}],
    )
    draft = build_soql(record)
    assert draft.status == STATUS_PARTIAL
    assert any("独自計算式" in m for m in draft.missing)


def test_aggregates_non_rowcount_go_to_missing() -> None:
    """``aggregates`` の ``!`` 形式で RowCount 以外は missing。"""
    column_map = _column_map_with_types([("Amount", "Amount", "currency")])
    record = _make_record(
        detail_columns=["Amount"],
        column_map=column_map,
        aggregates=["s!Amount"],
    )
    draft = build_soql(record)
    assert any("集計" in m for m in draft.missing)


def test_aggregates_rowcount_does_not_go_to_missing() -> None:
    """``aggregates`` の ``RowCount`` は missing にも入らない (集計の行数だけ) 。"""
    column_map = _column_map_with_types([("Name", "Name", "string")])
    record = _make_record(
        detail_columns=["Name"],
        column_map=column_map,
        aggregates=["RowCount"],
    )
    draft = build_soql(record)
    assert "集計" not in " ".join(draft.missing)


# ── 出力フォーマット ───────────────────────────────────────────────────


def test_soql_breaks_select_into_one_per_line() -> None:
    """SELECT 列は 1 行 1 列。"""
    column_map = _column_map_with_types(
        [
            ("Name", "Name", "string"),
            ("StageName", "StageName", "picklist"),
            ("Amount", "Amount", "currency"),
        ]
    )
    record = _make_record(detail_columns=["Name", "StageName", "Amount"], column_map=column_map)
    draft = build_soql(record)
    select_part = draft.soql.split("FROM")[0]
    # ``SELECT Name,\n  StageName,\n  Amount\n`` のように改行で並ぶ
    assert "SELECT Name" in select_part
    assert "\n  StageName" in select_part
    assert "\n  Amount" in select_part


def test_where_breaks_into_one_per_condition() -> None:
    """WHERE は条件ごとに改行。"""
    column_map = _column_map_with_types(
        [
            ("Name", "Name", "string"),
            ("StageName", "StageName", "picklist"),
        ]
    )
    record = _make_record(
        detail_columns=["Name"],
        column_map=column_map,
        report_filters=[
            {"column": "Name", "operator": "equals", "value": "Acme"},
            {"column": "StageName", "operator": "equals", "value": "Closed"},
        ],
    )
    draft = build_soql(record)
    where_part = draft.soql.split("WHERE", 1)[1].split("ORDER BY")[0]
    # ``\n  AND\n  `` で条件が区切られる
    assert "\n  AND\n  " in where_part
    assert "Name = 'Acme'" in where_part
    assert "StageName = 'Closed'" in where_part


# ── dataclass 性質の確認 ──────────────────────────────────────────────


def test_soql_draft_is_frozen() -> None:
    """``SoqlDraft`` は frozen で書き換え不可。"""
    draft = SoqlDraft(soql="SELECT 1", status=STATUS_CREATED, missing=(), checks=())
    with pytest.raises((AttributeError, Exception)):
        draft.status = "変えた"  # type: ignore[misc]


# ── 比較用の部品 (「似たSOQL」 判定で使う) ──────────────────────────────


def test_build_soql_fills_comparison_parts() -> None:
    """``select_items`` / ``where_items`` / ``order_by`` に 比較用の部品が入る。"""
    column_map = _column_map_with_types(
        [
            ("Name", "Name", "string"),
            ("StageName", "StageName", "picklist"),
        ]
    )
    record = _make_record(
        detail_columns=["Name"],
        column_map=column_map,
        report_filters=[
            {"column": "Name", "operator": "equals", "value": "Acme"},
            {"column": "StageName", "operator": "equals", "value": "Closed"},
        ],
        sort_by=[{"sortColumn": "Name", "sortOrder": "Asc"}],
        top_rows={"rowLimit": 10},
    )

    draft = build_soql(record)

    assert draft.select_items == ("Name",)
    assert draft.where_items == ("Name = 'Acme'", "StageName = 'Closed'")
    assert draft.order_by == "ORDER BY Name ASC\nLIMIT 10"


def test_build_soql_comparison_parts_are_empty_when_none_status() -> None:
    """``作れず`` のときは 比較用の部品も空。"""
    record = _make_record(main_object="")

    draft = build_soql(record)

    assert draft.status == STATUS_NONE
    assert draft.select_items == ()
    assert draft.where_items == ()
    assert draft.order_by == ""


# ── SOQL 列収集 (mapping.collect_describable_columns) ────────────────


def test_collect_describable_columns_includes_date_and_sort() -> None:
    """``standardDateFilter`` と ``sortBy`` の列も ``collect_describable_columns`` に入る。"""
    from src.mapping import collect_describable_columns

    metadata = {
        "detailColumns": ["A"],
        "standardDateFilter": {"column": "D"},
        "sortBy": [{"sortColumn": "S"}],
        "reportFilters": [{"column": "F"}],
    }
    result = collect_describable_columns(metadata)
    assert "A" in result
    assert "D" in result
    assert "S" in result
    assert "F" in result


# ── バグ修正の回帰テスト ──────────────────────────────────────────────


def test_boolean_filter_with_date_filter_keeps_date_condition() -> None:
    """組立式があるときに ``standardDateFilter`` の条件が黙って消えないこと。"""
    column_map = _column_map_with_types(
        [
            ("Name", "Name", "string"),
            ("StageName", "StageName", "picklist"),
            ("Amount", "Amount", "currency"),
            ("CloseDate", "CloseDate", "date"),
        ]
    )
    record = _make_record(
        detail_columns=["Name"],
        column_map=column_map,
        report_filters=[
            {"column": "Name", "operator": "equals", "value": "Acme"},
            {"column": "StageName", "operator": "equals", "value": "Closed Won"},
            {"column": "Amount", "operator": "greaterThan", "value": "100"},
        ],
        standard_date_filter={
            "column": "CloseDate",
            "durationValue": "CUSTOM",
            "startDate": "2025-01-01",
            "endDate": "2025-12-31",
        },
        report_boolean_filter="1 AND (2 OR 3)",
    )
    draft = build_soql(record)
    soql_normalized = draft.soql.replace("\n  AND\n  ", " AND ")
    assert "CloseDate >= 2025-01-01 AND CloseDate <= 2025-12-31" in soql_normalized


def test_missing_middle_condition_with_boolean_filter_drops_where() -> None:
    """組立式で参照する reportFilters の条件の 1 つでも作れないなら、 WHERE 全体を出さない。"""
    column_map = _column_map_with_types(
        [
            ("Name", "Name", "string"),
            ("Amount", "Amount", "currency"),
            # StageName は column_map に無い → 条件 2 が作れない
        ]
    )
    record = _make_record(
        detail_columns=["Name"],
        column_map=column_map,
        report_filters=[
            {"column": "Name", "operator": "equals", "value": "Acme"},
            {"column": "StageName", "operator": "equals", "value": "X"},
            {"column": "Amount", "operator": "greaterThan", "value": "100"},
        ],
        report_boolean_filter="1 AND (2 OR 3)",
    )
    draft = build_soql(record)
    assert "WHERE" not in draft.soql
    assert any("組立式" in m for m in draft.missing)


def test_missing_middle_condition_without_boolean_filter_keeps_rest() -> None:
    """組立式が無いときは作れた reportFilters と日付条件が WHERE に残る。"""
    column_map = _column_map_with_types(
        [
            ("Name", "Name", "string"),
            ("Amount", "Amount", "currency"),
            ("CloseDate", "CloseDate", "date"),
        ]
    )
    record = _make_record(
        detail_columns=["Name"],
        column_map=column_map,
        report_filters=[
            {"column": "Name", "operator": "equals", "value": "Acme"},
            {"column": "StageName", "operator": "equals", "value": "X"},
            {"column": "Amount", "operator": "greaterThan", "value": "100"},
        ],
        standard_date_filter={
            "column": "CloseDate",
            "durationValue": "CUSTOM",
            "startDate": "2025-01-01",
            "endDate": "",
        },
    )
    draft = build_soql(record)
    soql_normalized = draft.soql.replace("\n  AND\n  ", " AND ")
    assert "Name = 'Acme'" in soql_normalized
    assert "Amount > 100" in soql_normalized
    assert "CloseDate >= 2025-01-01" in soql_normalized
    assert "StageName" not in soql_normalized
    # 条件 2 (StageName) は missing に残る
    assert any("条件 2" in m and "StageName" in m for m in draft.missing)


def test_string_value_with_AND_in_value_preserves_value() -> None:
    """文字列値の中の `` AND `` が改行位置にならない (値が壊れない)。"""
    column_map = _column_map_with_types(
        [
            ("Name", "Name", "string"),
            ("StageName", "StageName", "picklist"),
        ]
    )
    record = _make_record(
        detail_columns=["Name"],
        column_map=column_map,
        report_filters=[
            {"column": "Name", "operator": "equals", "value": "Research AND Development"},
            {"column": "StageName", "operator": "equals", "value": "Closed"},
        ],
    )
    draft = build_soql(record)
    # 値が壊れずそのまま出てくる
    assert "WHERE Name = 'Research AND Development'" in draft.soql
    # 値の途中で改行が入らない
    soql_lines = draft.soql.split("\n")
    value_lines = [line for line in soql_lines if "Research" in line]
    assert value_lines, "値を含む行が無い"
    for line in value_lines:
        assert "Development'" in line and not line.endswith("Research")


def test_date_value_with_slash_is_normalized_to_dash() -> None:
    """``YYYY/MM/DD`` は ``YYYY-MM-DD`` に直して出す。"""
    column_map = _column_map_with_types([("CloseDate", "CloseDate", "date")])
    record = _make_record(
        detail_columns=["CloseDate"],
        column_map=column_map,
        report_filters=[{"column": "CloseDate", "operator": "greaterThan", "value": "2024/01/05"}],
    )
    draft = build_soql(record)
    assert "WHERE CloseDate > 2024-01-05" in draft.soql
    assert "2024/01/05" not in draft.soql


def test_date_value_with_nonexistent_date_goes_to_missing() -> None:
    """実在しない日付 (例: 2024-02-30) は missing。"""
    column_map = _column_map_with_types([("CloseDate", "CloseDate", "date")])
    record = _make_record(
        detail_columns=["CloseDate"],
        column_map=column_map,
        report_filters=[{"column": "CloseDate", "operator": "greaterThan", "value": "2024-02-30"}],
    )
    draft = build_soql(record)
    assert "WHERE CloseDate > 2024-02-30" not in draft.soql
    assert any("解釈できません" in m for m in draft.missing)


def test_datetime_value_with_slash_is_normalized_to_dash() -> None:
    """datetime 値の ``/`` 区切りも ``-`` に直して ``T00:00:00Z`` を付ける。"""
    column_map = _column_map_with_types([("CloseDateTime__c", "CloseDateTime__c", "datetime")])
    record = _make_record(
        detail_columns=["CloseDateTime__c"],
        column_map=column_map,
        report_filters=[
            {
                "column": "CloseDateTime__c",
                "operator": "greaterOrEqual",
                "value": "2025/01/01",
            }
        ],
    )
    draft = build_soql(record)
    assert "CloseDateTime__c >= 2025-01-01T00:00:00Z" in draft.soql
    assert "2025/01/01" not in draft.soql


def test_standard_date_filter_value_with_slash_is_normalized() -> None:
    """standardDateFilter の startDate / endDate も ``/`` を ``-`` に直す。"""
    column_map = _column_map_with_types([("CloseDate", "CloseDate", "date")])
    record = _make_record(
        detail_columns=["CloseDate"],
        column_map=column_map,
        standard_date_filter={
            "column": "CloseDate",
            "durationValue": "CUSTOM",
            "startDate": "2025/01/01",
            "endDate": "2025/12/31",
        },
    )
    draft = build_soql(record)
    assert "CloseDate >= 2025-01-01" in draft.soql
    assert "CloseDate <= 2025-12-31" in draft.soql


def test_build_soql_drops_undated_standard_date_filter() -> None:
    """``make_slim_report`` で 「実質指定なし」 と判定された ``standardDateFilter`` は
    SOQL の WHERE に出ず、 ``STATUS_PARTIAL`` にもならない。

    レポート describe と同じ形 （``startDate`` / ``endDate`` が ``None``） で
    ``make_slim_report`` に通し、 その結果を ``ReportRecord`` に詰めて ``build_soql``
    を呼ぶ
    """
    from src.fetch.report_slim import make_slim_report

    column_map = _column_map_with_types(
        [
            ("Name", "Name", "string"),
            ("CloseDate", "CloseDate", "date"),
        ]
    )
    raw_metadata = {
        "reportMetadata": {
            "reportType": {"type": "Opportunity"},
            "reportFormat": "TABULAR",
            "detailColumns": ["Name", "CloseDate"],
            "standardDateFilter": {
                "column": "CloseDate",
                "durationValue": "CUSTOM",
                "startDate": None,
                "endDate": None,
            },
        },
        "reportExtendedMetadata": {},
    }
    slim = make_slim_report(raw_metadata)
    assert "standardDateFilter" not in slim["reportMetadata"]

    record = _make_record(
        main_object="Opportunity",
        detail_columns=["Name", "CloseDate"],
        column_map=column_map,
        report_metadata=slim["reportMetadata"],
    )
    draft = build_soql(record)
    # 未完成の印が付かない、 ステータスは 「作成済み」 か 「要確認」 （ここでは何も警告が無いので 「作成済み」）
    assert INCOMPLETE_MARKER not in draft.soql
    assert draft.status == STATUS_CREATED
    # 日付条件が WHERE に出ていない。 detail_columns に CloseDate が
    # 入っているので SELECT には出るが、 日付フィルタ由来の 「>=」 / 「<=」 / 「=」 は出ない
    assert "CloseDate >=" not in draft.soql
    assert "CloseDate <=" not in draft.soql
    assert "CloseDate =" not in draft.soql
    assert "WHERE" not in draft.soql


def test_numeric_value_with_text_goes_to_missing() -> None:
    """数値型に非数値 ( ``abc`` ) を渡したら missing。"""
    column_map = _column_map_with_types([("Amount", "Amount", "currency")])
    record = _make_record(
        detail_columns=["Amount"],
        column_map=column_map,
        report_filters=[{"column": "Amount", "operator": "greaterThan", "value": "abc"}],
    )
    draft = build_soql(record)
    assert "WHERE Amount > abc" not in draft.soql
    assert any("数値" in m for m in draft.missing)


def test_numeric_value_with_thousands_comma_goes_to_missing() -> None:
    """数値型に桁区切りカンマ (``1,000``) は missing (カンマで値が分割される問題)。"""
    column_map = _column_map_with_types([("Amount", "Amount", "currency")])
    record = _make_record(
        detail_columns=["Amount"],
        column_map=column_map,
        report_filters=[{"column": "Amount", "operator": "greaterThan", "value": "1,000"}],
    )
    draft = build_soql(record)
    assert "1,000" not in draft.soql
    assert any("カンマ" in m for m in draft.missing)


def test_negative_numeric_value_keeps_as_is() -> None:
    """負数 ``-1.5`` はそのまま。"""
    column_map = _column_map_with_types([("Amount", "Amount", "currency")])
    record = _make_record(
        detail_columns=["Amount"],
        column_map=column_map,
        report_filters=[{"column": "Amount", "operator": "lessThan", "value": "-1.5"}],
    )
    draft = build_soql(record)
    assert "WHERE Amount < -1.5" in draft.soql


def test_boolean_filter_repeats_same_number_substitutes_both() -> None:
    """組立式 ``1 OR (1 AND 2)`` で 2 回出てくる ``1`` も ``( cond1 )`` で置き換わる。"""
    column_map = _column_map_with_types(
        [
            ("Name", "Name", "string"),
            ("StageName", "StageName", "picklist"),
            ("Amount", "Amount", "currency"),
        ]
    )
    record = _make_record(
        detail_columns=["Name"],
        column_map=column_map,
        report_filters=[
            {"column": "Name", "operator": "equals", "value": "Acme"},
            {"column": "StageName", "operator": "equals", "value": "Closed Won"},
            {"column": "Amount", "operator": "greaterThan", "value": "100"},
        ],
        report_boolean_filter="1 OR (1 AND 2)",
    )
    draft = build_soql(record)
    soql_normalized = draft.soql.replace("\n  AND\n  ", " AND ")
    # 両方の ``1`` が同じ条件で置き換わる
    assert soql_normalized.count("( Name = 'Acme' )") == 2
    # ``2`` も置き換わる
    assert "( StageName = 'Closed Won' )" in soql_normalized
    # 数字の ``1`` がそのまま残っていない (`` AND 1 `` 等)
    assert " AND 1 " not in soql_normalized
    assert "( 1 " not in soql_normalized


# ── 件数用 SOQL の組み立て ────────────────────────────────────────────


def test_count_soql_for_three_columns_has_no_columns() -> None:
    """3 列の SOQL で件数用は ``SELECT COUNT()\\nFROM …\\nWHERE …`` になり、 SELECT 句に列名が残らない。

    文字列の切り貼りで 1 行目だけ ``COUNT()`` に置換する実装だと 2 列目以降が
    カンマだけ残って壊れる (``COUNT(),\\n  What.Name,\\n  Owner.Name`` のような形)。
    """
    column_map = _column_map_with_types(
        [
            ("Subject", "Subject", "string"),
            ("What.Name", "What.Name", "string"),
            ("Owner.Name", "Owner.Name", "string"),
        ]
    )
    record = _make_record(
        main_object="Task",
        detail_columns=["Subject", "What.Name", "Owner.Name"],
        column_map=column_map,
        report_filters=[
            {"column": "Subject", "operator": "contains", "value": "A LIMIT B"},
            {"column": "What.Name", "operator": "equals", "value": "B"},
        ],
        report_boolean_filter="1 OR 2",
        sort_by=[{"sortColumn": "Subject", "sortOrder": "ASC"}],
        top_rows={"rowLimit": 10},
    )
    draft = build_soql(record)
    count = draft.count_soql
    # ``SELECT COUNT()`` の直後は改行で ``FROM`` になる
    assert count.startswith("SELECT COUNT()\n")
    # SELECT 部分 (``\nFROM`` の前) は ``SELECT COUNT()`` の 1 行だけ
    select_part = count.split("\nFROM", 1)[0]
    assert select_part == "SELECT COUNT()"
    # 元の SOQL と同じ ``FROM`` / ``WHERE`` を持つ
    assert "FROM Task" in count
    # WHERE 全文がそのまま残っている (`` LIMIT `` を含む値で切れていない)
    soql_normalized = count.replace("\n  AND\n  ", " AND ")
    assert "Subject LIKE '%A LIMIT B%'" in soql_normalized
    assert "What.Name = 'B'" in soql_normalized
    # ``ORDER BY`` / ``LIMIT`` は末尾に付かない ( WHERE 内の ``'A LIMIT B%'``
    # や ``'%A LIMIT B%'`` は値の一部なのでここで除去してから確認する)
    soql_no_value_limit = soql_normalized.replace("'%A LIMIT B%'", "")
    assert "LIMIT" not in soql_no_value_limit.upper()


def test_count_soql_where_not_cut_by_limit_or_order_by_in_value() -> None:
    """値に `` LIMIT `` / `` ORDER BY `` を含むとき、 WHERE が切れていない。

    文字列検索で ``LIMIT`` / ``ORDER BY`` を探して切る実装だと、 値の途中で切れる。
    """
    column_map = _column_map_with_types(
        [
            ("Subject", "Subject", "string"),
            ("What.Name", "What.Name", "string"),
            ("Owner.Name", "Owner.Name", "string"),
        ]
    )
    record = _make_record(
        detail_columns=["Subject", "What.Name", "Owner.Name"],
        column_map=column_map,
        report_filters=[
            {"column": "Subject", "operator": "contains", "value": "A LIMIT B"},
            {"column": "What.Name", "operator": "equals", "value": "ORDER BY B"},
        ],
        report_boolean_filter="1 OR 2",
    )
    draft = build_soql(record)
    count = draft.count_soql
    # 元の SOQL の WHERE は ```` + `` AND `` + ``````
    # ( ``Subject LIKE '%A LIMIT B%'`` ) OR ( ``What.Name = 'ORDER BY B'`` ``) ``
    expected_where = "( Subject LIKE '%A LIMIT B%' ) OR ( What.Name = 'ORDER BY B' )"
    assert expected_where in count
    # 値の途中で切れていない ( 元の SOQL の ``count`` 比較する代わりに
    # 全文が含まれていることで確認 )
    assert count.endswith(expected_where) or expected_where in count
    # ``ORDER BY`` / ``LIMIT`` は無い ( 値は ``'ORDER BY B'`` / ``'%A LIMIT B%'`` の形なので
    # ``count.upper()`` でも大文字小文字を区別せずに出てこない)
    upper_count = count.upper()
    # 値の中の `` ORDER BY `` / `` LIMIT `` はカウントしても 1 個以上ある。
    # 文字列 ``ORDER BY`` 句 (大文字+半角スペース+大文字) を探す: ``\\n ORDER BY`` 由来は無い。
    # 確実に切り貼りしていない証として、 末尾の ``)`` の後に ``ORDER BY`` / ``LIMIT`` が出てこない。
    assert not upper_count.endswith(("ORDER BY", "LIMIT"))


def test_count_soql_with_top_rows_returns_limit_and_no_limit_clause() -> None:
    """``topRows`` があるときは ``limit`` が返り、 件数用 SOQL に ``LIMIT`` が付かない。"""
    column_map = _column_map_with_types([("Name", "Name", "string")])
    record = _make_record(
        detail_columns=["Name"],
        column_map=column_map,
        sort_by=[{"sortColumn": "Name", "sortOrder": "ASC"}],
        top_rows={"rowLimit": 42},
    )
    draft = build_soql(record)
    # 件数用 SOQL に ``LIMIT`` は付かない
    assert "LIMIT" not in draft.count_soql.upper()
    # ``limit`` フィールドに正の整数が入る
    assert draft.limit == 42


def test_count_soql_for_three_columns_and_value_limit_does_not_break_wire() -> None:
    """3 列 + 値に ``LIMIT`` / ``ORDER BY`` を含む条件で ``_FakeClient`` の
    ``/query?q=...`` 側の SOQL 形 assert を通ること自体は、
    ``tests/test_count_check.py`` 側の統合テストが担当する ( そちらの
    ``_FakeClient.request`` が ``SELECT COUNT()`` 形 (``FROM`` の前に
    余計な列名が無い) を assert しているため、文字列の切り貼りで
    1 行目だけ ``COUNT()`` に置換する壊れた実装だと必ず落ちる)。

    ここは個別の ``SoqlDraft`` レベルで 3 列 + ``LIMIT`` / ``ORDER BY`` 値を
    持った case が、 件数用 SOQL の ``count_soql`` にも ``build_soql`` の
    ``soql`` と整合した形で反映されることを保証する薄いテスト。
    """
    column_map = _column_map_with_types(
        [
            ("Subject", "Subject", "string"),
            ("What.Name", "What.Name", "string"),
            ("Owner.Name", "Owner.Name", "string"),
        ]
    )
    record = _make_record(
        detail_columns=["Subject", "What.Name", "Owner.Name"],
        column_map=column_map,
        report_filters=[
            {"column": "Subject", "operator": "contains", "value": "A LIMIT B"},
            {"column": "What.Name", "operator": "equals", "value": "ORDER BY B"},
        ],
        report_boolean_filter="1 OR 2",
    )
    draft = build_soql(record)
    # 件数用 SOQL は ``SELECT COUNT()`` で始まり ``FROM`` の前に何もない
    select_part, sep, _from_and_rest = draft.count_soql.partition("\nFROM")
    assert select_part == "SELECT COUNT()"
    assert sep == "\nFROM"
    # 元の SOQL の ``WHERE`` 全文が件数用 SOQL に残っている ( `` LIMIT `` /
    # `` ORDER BY `` を含む値で切れていない)
    assert "( Subject LIKE '%A LIMIT B%' )" in draft.count_soql
    assert "( What.Name = 'ORDER BY B' )" in draft.count_soql


# ── 安全策: 未完成 SOQL の印 ──────────────────────────────────────────
# 仕様: ``STATUS_PARTIAL`` の SOQL は 1 行目に ``INCOMPLETE_MARKER`` を入れて、
# ダウンローダーが実行前に止める。 ``STATUS_NEEDS_CHECK`` / ``STATUS_CREATED``
# には印を付けない （要確認は人が補えば実行できるため） 。 ``STATUS_NONE``
# はそもそも本文が無い。 組立式が解釈できないケース・ 履歴スナップショット
# があるケースでも、 一部作れずに倒して印を付ける。


def test_incomplete_marker_is_first_line_for_partial_status() -> None:
    """``一部作れず`` の SOQL は 1 行目が ``INCOMPLETE_MARKER`` 。"""
    column_map = _column_map_with_types([("Name", "Name", "string")])
    record = _make_record(detail_columns=["Name", "Unknown"], column_map=column_map)
    draft = build_soql(record)
    assert draft.status == STATUS_PARTIAL
    first_line = draft.soql.split("\n", 1)[0]
    assert first_line == INCOMPLETE_MARKER
    # 本文 (``SELECT ...``) もそのまま残っている
    assert "SELECT" in draft.soql


def test_incomplete_marker_also_on_count_soql_for_partial_status() -> None:
    """``一部作れず`` の件数用 SOQL にも 1 行目に印。"""
    column_map = _column_map_with_types([("Name", "Name", "string")])
    record = _make_record(detail_columns=["Name", "Unknown"], column_map=column_map)
    draft = build_soql(record)
    assert draft.status == STATUS_PARTIAL
    first_line = draft.count_soql.split("\n", 1)[0]
    assert first_line == INCOMPLETE_MARKER
    assert "SELECT COUNT()" in draft.count_soql


def test_incomplete_marker_when_boolean_filter_cannot_be_resolved() -> None:
    """組立式の番号が ``reportFilters`` のどれかに対応しないときは WHERE を
    作らず ``一部作れず`` になる。 その SOQL にも 1 行目に印。"""
    column_map = _column_map_with_types([("Name", "Name", "string")])
    record = _make_record(
        detail_columns=["Name"],
        column_map=column_map,
        report_filters=[{"column": "Unknown", "operator": "equals", "value": "X"}],
        report_boolean_filter="1 AND 2",
    )
    draft = build_soql(record)
    assert draft.status == STATUS_PARTIAL
    assert any("組立式" in m for m in draft.missing)
    first_line = draft.soql.split("\n", 1)[0]
    assert first_line == INCOMPLETE_MARKER


def test_incomplete_marker_when_historical_snapshot_dates_present() -> None:
    """``historicalSnapshotDates`` があると missing に 「SOQL にできない」
    が入って ``一部作れず`` になり、 1 行目に印が付く。"""
    column_map = _column_map_with_types([("Name", "Name", "string")])
    record_metadata = {
        "reportType": {"type": "Opportunity"},
        "reportFormat": "TABULAR",
        "detailColumns": ["Name"],
        "historicalSnapshotDates": ["2025-01-01", "2025-02-01"],
    }
    record = _make_record(
        detail_columns=["Name"],
        column_map=column_map,
        report_metadata=record_metadata,
    )
    draft = build_soql(record)
    assert draft.status == STATUS_PARTIAL
    assert any("履歴スナップショット" in m for m in draft.missing)
    first_line = draft.soql.split("\n", 1)[0]
    assert first_line == INCOMPLETE_MARKER


def test_no_marker_when_status_is_needs_check() -> None:
    """``要確認`` には印を付けない （人が補えば実行できるため） 。"""
    column_map = _column_map_with_types([("Name", "Name", "string")])
    record = _make_record(
        detail_columns=["Name"],
        column_map=column_map,
        scope="MyTerritory",
    )
    draft = build_soql(record)
    assert draft.status == STATUS_NEEDS_CHECK
    assert draft.missing == ()
    # 1 行目は ``SELECT`` で始まる
    assert draft.soql.startswith("SELECT")
    assert INCOMPLETE_MARKER not in draft.soql
    assert INCOMPLETE_MARKER not in draft.count_soql


def test_no_marker_when_status_is_created() -> None:
    """``作成済み`` には印を付けない。"""
    column_map = _column_map_with_types([("Name", "Name", "string")])
    record = _make_record(
        detail_columns=["Name"],
        column_map=column_map,
        report_filters=[{"column": "Name", "operator": "equals", "value": "Acme"}],
    )
    draft = build_soql(record)
    assert draft.status == STATUS_CREATED
    assert draft.soql.startswith("SELECT")
    assert INCOMPLETE_MARKER not in draft.soql
    assert INCOMPLETE_MARKER not in draft.count_soql


def test_no_soql_body_when_status_is_none() -> None:
    """``作れず`` は本文なし （印も付かない） 。"""
    record = _make_record(main_object="")
    record = ReportRecord(
        key=record.key,
        summary=record.summary,
        report_id=record.report_id,
        url=record.url,
        main_object=None,
        report=record.report,
        objects=record.objects,
        relations=record.relations,
        column_map=record.column_map,
        warnings=record.warnings,
    )
    draft = build_soql(record)
    assert draft.status == STATUS_NONE
    assert draft.soql == ""
    assert draft.count_soql == ""
    assert INCOMPLETE_MARKER not in draft.soql


def test_incomplete_marker_constant_value() -> None:
    """``INCOMPLETE_MARKER`` は固定文字列。 ダウンローダーがこの印を見て実行前
    に止めるので、 値を変えるときはダウンローダーと同時に。"""
    assert INCOMPLETE_MARKER == "【未完成】作れなかった部分があります。このままでは実行できません。"


# ── 未完成 SOQL の 1 行目: ダウンローダーとの契約 ────────────────────


def test_partial_soql_first_line_starts_with_marker_from_contract() -> None:
    """``一部作れず`` の SOQL の 1 行目は、 ダウンローダー側
    ``tests/contract/未完成のSOQL.contract.txt`` の 1 行目と同じ印で始まる。

    印の共通固定部分 (``【未完成】``) を見て止める契約のため、 印の文言が
    多少違っていても 「同じ印で始まる」 ことをテストする
    """
    contract_path = Path(__file__).parent / "contract" / "未完成のSOQL.contract.txt"
    expected_first_line = contract_path.read_text(encoding="utf-8").splitlines()[0]
    # 契約の 1 行目の印の共通部分を取り出す。 ``【未完成】`` までは固定
    # （印の文言自体は変えてよい） 。
    marker_prefix = "【未完成】"
    assert expected_first_line.startswith(marker_prefix)

    # missing が 1 件以上ある record を作って ``一部作れず`` を起こす
    record = _make_record(
        main_object="Opportunity",
        detail_columns=["Opp.Name"],
        column_map=[
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
                # 取れない列 (表示名一致せず) を 1 件混ぜて missing を作る
                "列キー": "Opp.Mystery",
                "表示名": "謎列",
                "所属オブジェクト": "Opportunity",
                "対応フィールドAPI名": "(不明)",
                "型": "",
                "参照先オブジェクト": "",
                "リレーション名": "",
                "備考": "対応フィールドなし",
            },
        ],
        report_filters=[
            # 組立式に解釈できない演算子を入れて missing を起こす
            {"column": "Opp.Name", "operator": "custom_op", "value": "x"},
        ],
    )
    draft = build_soql(record)
    assert draft.status == STATUS_PARTIAL
    first_line = draft.soql.split("\n", 1)[0]
    # 契約の見本 1 行目と同じ印 (``【未完成】`` プレフィックス) で始まる
    assert first_line.startswith(marker_prefix)
    # 印として ``INCOMPLETE_MARKER`` と一致 （soql-collector 側の固定値） 、
    # かつ契約の見本の 1 行目も同じ印プレフィックスで始まる
    assert first_line.startswith(INCOMPLETE_MARKER.split("【未完成】", 1)[0])


# ── 壊れた版での動作確認 (一時的に手で壊して実行→戻す) ──────────────
# 仕様: 「壊したコードで一時的に走らせて元に戻す」 。
# 既存のテストが落ちることを report 用に確認する。 テストファイルには
# 残さない (下の 「確認ログ」 節で参照する) 。


# ── Owner がポリモーフィックな列の注意書き ────────────────────────


def test_owner_polymorphic_note_becomes_one_check() -> None:
    """備考に Owner の注意書きがある列は、 checks に 1 列 1 件だけ積む。"""
    column_map = _column_map_with_types([("Owner.Department", "Owner.Department", "string")])
    column_map[0]["備考"] = f"列キーから（{OWNER_POLYMORPHIC_NOTE}）"
    record = _make_record(
        detail_columns=["Owner.Department"],
        column_map=column_map,
        report_filters=[{"column": "Owner.Department", "operator": "equals", "value": "営業"}],
    )
    draft = build_soql(record)
    owner_checks = [c for c in draft.checks if OWNER_POLYMORPHIC_NOTE in c]
    assert len(owner_checks) == 1
    assert "Owner.Department" in owner_checks[0]


def test_owner_polymorphic_note_is_not_added_for_unused_column() -> None:
    """SELECT・絞り込み・並び順で使わない列は、 checks に積まない。"""
    column_map = _column_map_with_types(
        [
            ("Name", "Name", "string"),
            ("Owner.Department", "Owner.Department", "string"),
        ]
    )
    column_map[1]["備考"] = f"列キーから（{OWNER_POLYMORPHIC_NOTE}）"
    record = _make_record(detail_columns=["Name"], column_map=column_map)
    draft = build_soql(record)
    assert not any(OWNER_POLYMORPHIC_NOTE in c for c in draft.checks)


# ── 組立式と日付条件の括弧付け ────────────────────────────────────


def test_boolean_filter_with_date_filter_wraps_whole_expression() -> None:
    """組立式と日付条件が並ぶときは、 組立式全体を ``( ... )`` で囲む。

    囲まないと ``( a ) OR ( b ) AND 日付`` になり、 日付条件が 1 つの条件に
    だけかかる形になってしまう。
    """
    column_map = _column_map_with_types(
        [
            ("Name", "Name", "string"),
            ("StageName", "StageName", "picklist"),
            ("CloseDate", "CloseDate", "date"),
        ]
    )
    record = _make_record(
        detail_columns=["Name", "StageName", "CloseDate"],
        column_map=column_map,
        report_filters=[
            {"column": "Name", "operator": "equals", "value": "Acme"},
            {"column": "StageName", "operator": "equals", "value": "Closed"},
        ],
        standard_date_filter={
            "column": "CloseDate",
            "durationValue": "CUSTOM",
            "startDate": "2025-01-01",
            "endDate": "2025-12-31",
        },
        report_boolean_filter="1 OR 2",
    )
    draft = build_soql(record)
    assert draft.soql == (
        "SELECT Name,\n"
        "  StageName,\n"
        "  CloseDate\n"
        "FROM Opportunity\n"
        "WHERE ( ( Name = 'Acme' ) OR ( StageName = 'Closed' ) )\n"
        "  AND\n"
        "  CloseDate >= 2025-01-01 AND CloseDate <= 2025-12-31"
    )


# ── 組立式の検査 ─────────────────────────────────────────────────


def _boolean_filter_record(expression: str) -> ReportRecord:
    """組立式だけを持つ record を作る。"""
    column_map = _column_map_with_types(
        [
            ("Name", "Name", "string"),
            ("StageName", "StageName", "picklist"),
            ("Amount", "Amount", "currency"),
        ]
    )
    return _make_record(
        detail_columns=["Name"],
        column_map=column_map,
        report_filters=[
            {"column": "Name", "operator": "equals", "value": "Acme"},
            {"column": "StageName", "operator": "equals", "value": "Closed"},
            {"column": "Amount", "operator": "greaterThan", "value": "100"},
        ],
        report_boolean_filter=expression,
    )


def test_boolean_filter_rejects_unknown_word() -> None:
    """使える語以外の語は、 組立式の条件を作れない 経路に入る。"""
    draft = build_soql(_boolean_filter_record("1 XOR 2"))
    assert draft.status == STATUS_PARTIAL
    assert any("組立式の条件を作れない" in m for m in draft.missing)
    assert "WHERE" not in draft.soql


def test_boolean_filter_rejects_unbalanced_parentheses() -> None:
    """括弧が合わない組立式は、 組立式の条件を作れない 経路に入る。"""
    draft = build_soql(_boolean_filter_record("1 AND (2"))
    assert draft.status == STATUS_PARTIAL
    assert any("組立式の条件を作れない" in m for m in draft.missing)
    assert "WHERE" not in draft.soql


def test_boolean_filter_rejects_mixed_and_or_at_same_depth() -> None:
    """同じ深さで ``AND`` と ``OR`` が混在する組立式は作れない。"""
    draft = build_soql(_boolean_filter_record("1 AND 2 OR 3"))
    assert draft.status == STATUS_PARTIAL
    assert any("組立式の条件を作れない" in m for m in draft.missing)
    assert "WHERE" not in draft.soql


def test_boolean_filter_allows_not() -> None:
    """``1 AND NOT 2`` は通す。"""
    draft = build_soql(_boolean_filter_record("1 AND NOT 2"))
    assert draft.status == STATUS_CREATED
    assert "WHERE" in draft.soql


def test_boolean_filter_keywords_are_upper_cased() -> None:
    """組立式の語は、 大文字小文字を無視して大文字にそろえる。"""
    draft = build_soql(_boolean_filter_record("1 and (2 or 3)"))
    soql_normalized = draft.soql.replace("\n  AND\n  ", " AND ")
    assert "( Name = 'Acme' ) AND (( StageName = 'Closed' ) OR ( Amount > 100 ))" in soql_normalized


# ── 演算子名の表記ゆれ ───────────────────────────────────────────


def test_operator_alias_is_normalized() -> None:
    """演算子名の表記ゆれは、 正規名へ寄せて組み立てる。"""
    cases = [
        ("lessorequal", "Amount <= 100"),
        ("lessequal", "Amount <= 100"),
        ("lessthanorequal", "Amount <= 100"),
        ("greaterorequal", "Amount >= 100"),
        ("greaterequal", "Amount >= 100"),
        ("greaterthanorequal", "Amount >= 100"),
        ("equal", "Amount = 100"),
        ("equals", "Amount = 100"),
    ]
    for operator, expected in cases:
        column_map = _column_map_with_types([("Amount", "Amount", "currency")])
        record = _make_record(
            detail_columns=["Amount"],
            column_map=column_map,
            report_filters=[{"column": "Amount", "operator": operator, "value": "100"}],
        )
        draft = build_soql(record)
        assert f"WHERE {expected}" in draft.soql, operator


def test_not_equal_alias_is_normalized() -> None:
    """``notequal`` / ``notequals`` は ``!=`` になる。"""
    for operator in ("notequal", "notequals"):
        column_map = _column_map_with_types([("Name", "Name", "string")])
        record = _make_record(
            detail_columns=["Name"],
            column_map=column_map,
            report_filters=[{"column": "Name", "operator": operator, "value": "Acme"}],
        )
        draft = build_soql(record)
        assert "WHERE Name != 'Acme'" in draft.soql, operator


def test_not_contain_alias_is_normalized() -> None:
    """``notcontains`` / ``doesnotcontain`` は ``notContain`` と同じ形。"""
    for operator in ("notcontain", "notcontains", "doesnotcontain"):
        column_map = _column_map_with_types([("Name", "Name", "string")])
        record = _make_record(
            detail_columns=["Name"],
            column_map=column_map,
            report_filters=[{"column": "Name", "operator": operator, "value": "ac"}],
        )
        draft = build_soql(record)
        assert "NOT (Name LIKE '%ac%')" in draft.soql, operator


def test_unsupported_operator_is_still_missing() -> None:
    """``within`` など未対応の演算子は、 従来どおり missing。"""
    column_map = _column_map_with_types([("Name", "Name", "string")])
    record = _make_record(
        detail_columns=["Name"],
        column_map=column_map,
        report_filters=[{"column": "Name", "operator": "within", "value": ""}],
    )
    draft = build_soql(record)
    assert any("within" in m for m in draft.missing)


# ── 比較演算子の型制限 ───────────────────────────────────────────


def test_comparison_on_picklist_goes_to_missing() -> None:
    """``>`` などの比較は、 選択リスト型の項目では missing。"""
    for field_type in ("picklist", "multipicklist", "boolean", "checkbox", "combobox", "textarea"):
        column_map = _column_map_with_types([("Field", "Field", field_type)])
        record = _make_record(
            detail_columns=["Field"],
            column_map=column_map,
            report_filters=[{"column": "Field", "operator": "greaterThan", "value": "1"}],
        )
        draft = build_soql(record)
        assert "WHERE" not in draft.soql, field_type
        assert any("数値・日付の項目でのみ使用可" in m for m in draft.missing), field_type


def test_comparison_on_number_is_allowed() -> None:
    """数値・日付の項目では、 比較演算子が使える。"""
    column_map = _column_map_with_types([("Amount", "Amount", "long")])
    record = _make_record(
        detail_columns=["Amount"],
        column_map=column_map,
        report_filters=[{"column": "Amount", "operator": "greaterThan", "value": "100"}],
    )
    draft = build_soql(record)
    assert "WHERE Amount > 100" in draft.soql


def test_long_type_is_numeric() -> None:
    """``long`` は数値系として扱う。"""
    column_map = _column_map_with_types([("Count__c", "Count__c", "long")])
    record = _make_record(
        detail_columns=["Count__c"],
        column_map=column_map,
        report_filters=[{"column": "Count__c", "operator": "equals", "value": "12"}],
    )
    draft = build_soql(record)
    assert "WHERE Count__c = 12" in draft.soql


# ── datetime の境界 ──────────────────────────────────────────────


def _datetime_record(operator: str, value: str) -> ReportRecord:
    column_map = _column_map_with_types([("Done__c", "Done__c", "datetime")])
    return _make_record(
        detail_columns=["Done__c"],
        column_map=column_map,
        report_filters=[{"column": "Done__c", "operator": operator, "value": value}],
    )


def test_datetime_comparison_uses_start_or_end_of_day() -> None:
    """日付だけの値は、 演算子に応じて 0 時 / 終端 を使う。"""
    cases = [
        ("greaterOrEqual", "WHERE Done__c >= 2025-01-01T00:00:00Z"),
        ("lessThan", "WHERE Done__c < 2025-01-01T00:00:00Z"),
        ("lessOrEqual", "WHERE Done__c <= 2025-01-01T23:59:59Z"),
        ("greaterThan", "WHERE Done__c > 2025-01-01T23:59:59Z"),
    ]
    for operator, expected in cases:
        draft = build_soql(_datetime_record(operator, "2025-01-01"))
        assert expected in draft.soql, operator


def test_datetime_equals_becomes_day_range() -> None:
    """``equals`` は、 その日の 0 時〜終端の範囲になる。"""
    draft = build_soql(_datetime_record("equals", "2025-01-01"))
    assert "WHERE ( Done__c >= 2025-01-01T00:00:00Z AND Done__c <= 2025-01-01T23:59:59Z )" in (
        draft.soql
    )


def test_datetime_not_equal_becomes_outside_range() -> None:
    """``notEqual`` は、 その日の 0 時〜終端の外側になる。"""
    draft = build_soql(_datetime_record("notEqual", "2025-01-01"))
    assert "WHERE ( Done__c < 2025-01-01T00:00:00Z OR Done__c > 2025-01-01T23:59:59Z )" in (
        draft.soql
    )


def test_datetime_multiple_values_are_joined_with_or() -> None:
    """``equals`` の複数値は OR でつなぐ。"""
    draft = build_soql(_datetime_record("equals", "2025-01-01,2025-02-02"))
    assert (
        "WHERE ( ( Done__c >= 2025-01-01T00:00:00Z AND Done__c <= 2025-01-01T23:59:59Z )"
        " OR ( Done__c >= 2025-02-02T00:00:00Z AND Done__c <= 2025-02-02T23:59:59Z ) )"
        in draft.soql
    )


def test_datetime_not_equal_multiple_values_are_joined_with_and() -> None:
    """``notEqual`` の複数値は AND でつなぐ。"""
    draft = build_soql(_datetime_record("notEqual", "2025-01-01,2025-02-02"))
    assert (
        "WHERE ( ( Done__c < 2025-01-01T00:00:00Z OR Done__c > 2025-01-01T23:59:59Z )"
        " AND ( Done__c < 2025-02-02T00:00:00Z OR Done__c > 2025-02-02T23:59:59Z ) )" in draft.soql
    )


def test_datetime_relative_literal_is_kept_as_is() -> None:
    """相対日付リテラルは、 そのまま書く。"""
    draft = build_soql(_datetime_record("equals", "THIS_MONTH"))
    assert "WHERE Done__c = THIS_MONTH" in draft.soql


# ── LIKE ─────────────────────────────────────────────────────────


def test_like_on_id_or_reference_or_multipicklist_goes_to_missing() -> None:
    """``id`` / ``reference`` / ``multipicklist`` では LIKE を使えない。"""
    for field_type in ("id", "reference", "multipicklist"):
        column_map = _column_map_with_types([("Field", "Field", field_type)])
        record = _make_record(
            detail_columns=["Field"],
            column_map=column_map,
            report_filters=[{"column": "Field", "operator": "contains", "value": "x"}],
        )
        draft = build_soql(record)
        assert "WHERE" not in draft.soql, field_type
        assert any("文字列系の項目でのみ使用可" in m for m in draft.missing), field_type


def test_like_escapes_underscore() -> None:
    """値の中の ``_`` は、 ワイルドカードではなく文字として探す。"""
    column_map = _column_map_with_types([("Name", "Name", "string")])
    record = _make_record(
        detail_columns=["Name"],
        column_map=column_map,
        report_filters=[{"column": "Name", "operator": "contains", "value": "a_b"}],
    )
    draft = build_soql(record)
    assert "WHERE Name LIKE '%a\\_b%'" in draft.soql


def test_like_escapes_backslash_before_wildcard() -> None:
    """先に ``\\`` をエスケープしてから ``%`` を処理する。"""
    column_map = _column_map_with_types([("Name", "Name", "string")])
    record = _make_record(
        detail_columns=["Name"],
        column_map=column_map,
        report_filters=[{"column": "Name", "operator": "contains", "value": "a\\%b"}],
    )
    draft = build_soql(record)
    assert "WHERE Name LIKE '%a\\\\\\%b%'" in draft.soql


def test_like_starts_with_multiple_values_becomes_or_group() -> None:
    """``startsWith`` の複数値は OR で並べる。"""
    column_map = _column_map_with_types([("Name", "Name", "string")])
    record = _make_record(
        detail_columns=["Name"],
        column_map=column_map,
        report_filters=[{"column": "Name", "operator": "startsWith", "value": "Acme,Zenith"}],
    )
    draft = build_soql(record)
    assert "WHERE ( Name LIKE 'Acme%' OR Name LIKE 'Zenith%' )" in draft.soql


@pytest.mark.parametrize(
    ("operator", "expected"),
    [
        ("contains", "Name LIKE '%a\\nb%'"),
        ("startsWith", "Name LIKE 'a\\nb%'"),
        ("notContain", "NOT (Name LIKE '%a\\nb%')"),
    ],
)
def test_like_escapes_newline_in_value(operator: str, expected: str) -> None:
    """値の中の改行は、 リテラルが壊れないエスケープの形にする。"""
    column_map = _column_map_with_types([("Name", "Name", "string")])
    record = _make_record(
        detail_columns=["Name"],
        column_map=column_map,
        report_filters=[{"column": "Name", "operator": operator, "value": "a\nb"}],
    )
    draft = build_soql(record)
    assert expected in draft.soql
    # 生の改行が値の中に残らない
    assert "a\nb" not in draft.soql


def test_like_escapes_tab_in_value() -> None:
    """値の中のタブも、 リテラルが壊れないエスケープの形にする。"""
    column_map = _column_map_with_types([("Name", "Name", "string")])
    record = _make_record(
        detail_columns=["Name"],
        column_map=column_map,
        report_filters=[{"column": "Name", "operator": "contains", "value": "a\tb"}],
    )
    draft = build_soql(record)
    assert "WHERE Name LIKE '%a\\tb%'" in draft.soql
    assert "\t" not in draft.soql


# ── IN が使えない型 ──────────────────────────────────────────────


def test_multipicklist_equals_multiple_values_does_not_use_in() -> None:
    """multipicklist の複数値は、 ``=`` を OR で並べる。"""
    column_map = _column_map_with_types([("Hobbies__c", "Hobbies__c", "multipicklist")])
    record = _make_record(
        detail_columns=["Hobbies__c"],
        column_map=column_map,
        report_filters=[{"column": "Hobbies__c", "operator": "equals", "value": "Soccer,Baseball"}],
    )
    draft = build_soql(record)
    assert "WHERE ( Hobbies__c = 'Soccer' OR Hobbies__c = 'Baseball' )" in draft.soql


def test_multipicklist_not_equal_multiple_values_does_not_use_not_in() -> None:
    """multipicklist の複数値否定は、 ``!=`` を AND で並べる。"""
    column_map = _column_map_with_types([("Hobbies__c", "Hobbies__c", "multipicklist")])
    record = _make_record(
        detail_columns=["Hobbies__c"],
        column_map=column_map,
        report_filters=[
            {"column": "Hobbies__c", "operator": "notEqual", "value": "Soccer,Baseball"}
        ],
    )
    draft = build_soql(record)
    assert "WHERE ( Hobbies__c != 'Soccer' AND Hobbies__c != 'Baseball' )" in draft.soql


def test_boolean_equals_multiple_values_does_not_use_in() -> None:
    """boolean の複数値は、 ``=`` を OR で並べる。"""
    column_map = _column_map_with_types([("IsClosed", "IsClosed", "boolean")])
    record = _make_record(
        detail_columns=["IsClosed"],
        column_map=column_map,
        report_filters=[{"column": "IsClosed", "operator": "equals", "value": "true,false"}],
    )
    draft = build_soql(record)
    assert "WHERE ( IsClosed = true OR IsClosed = false )" in draft.soql


# ── includes / excludes ──────────────────────────────────────────


def test_picklist_includes_becomes_in() -> None:
    """picklist の ``includes`` は ``IN`` になる。"""
    column_map = _column_map_with_types([("StageName", "StageName", "picklist")])
    record = _make_record(
        detail_columns=["StageName"],
        column_map=column_map,
        report_filters=[
            {"column": "StageName", "operator": "includes", "value": "Closed Won,Closed Lost"}
        ],
    )
    draft = build_soql(record)
    assert "WHERE StageName IN ('Closed Won', 'Closed Lost')" in draft.soql


def test_picklist_includes_single_value_becomes_equal() -> None:
    """picklist の ``includes`` が 1 値なら ``=`` になる。"""
    column_map = _column_map_with_types([("StageName", "StageName", "picklist")])
    record = _make_record(
        detail_columns=["StageName"],
        column_map=column_map,
        report_filters=[{"column": "StageName", "operator": "includes", "value": "Closed Won"}],
    )
    draft = build_soql(record)
    assert "WHERE StageName = 'Closed Won'" in draft.soql


def test_picklist_excludes_becomes_not_in() -> None:
    """picklist の ``excludes`` は ``NOT IN`` になる。"""
    column_map = _column_map_with_types([("StageName", "StageName", "picklist")])
    record = _make_record(
        detail_columns=["StageName"],
        column_map=column_map,
        report_filters=[
            {"column": "StageName", "operator": "excludes", "value": "Closed Won,Closed Lost"}
        ],
    )
    draft = build_soql(record)
    assert "WHERE StageName NOT IN ('Closed Won', 'Closed Lost')" in draft.soql


def test_multipicklist_includes_stays_includes() -> None:
    """multipicklist の ``includes`` は ``INCLUDES`` のまま。"""
    column_map = _column_map_with_types([("Hobbies__c", "Hobbies__c", "multipicklist")])
    record = _make_record(
        detail_columns=["Hobbies__c"],
        column_map=column_map,
        report_filters=[
            {"column": "Hobbies__c", "operator": "includes", "value": "Soccer,Baseball"}
        ],
    )
    draft = build_soql(record)
    assert "WHERE Hobbies__c INCLUDES ('Soccer', 'Baseball')" in draft.soql


# ── textarea ─────────────────────────────────────────────────────


def test_textarea_filter_adds_check() -> None:
    """textarea の絞り込みは、 ロングテキストの注意を checks に足す。"""
    column_map = _column_map_with_types([("Description__c", "Description__c", "textarea")])
    record = _make_record(
        detail_columns=["Description__c"],
        column_map=column_map,
        report_filters=[{"column": "Description__c", "operator": "contains", "value": "abc"}],
    )
    draft = build_soql(record)
    assert any("ロングテキストは絞り込めない場合がある" in c for c in draft.checks)


# ── 値の分割・エスケープ ──────────────────────────────────────────


def test_split_values_keeps_quoted_comma_as_one_value() -> None:
    """``"a,b",c`` は 2 値に分ける。"""
    column_map = _column_map_with_types([("Name", "Name", "string")])
    record = _make_record(
        detail_columns=["Name"],
        column_map=column_map,
        report_filters=[{"column": "Name", "operator": "equals", "value": '"a,b",c'}],
    )
    draft = build_soql(record)
    assert "WHERE Name IN ('a,b', 'c')" in draft.soql


def test_string_literal_escapes_newline_and_tab() -> None:
    """文字列リテラルの改行とタブは、 エスケープした形にする。"""
    column_map = _column_map_with_types([("Name", "Name", "string")])
    record = _make_record(
        detail_columns=["Name"],
        column_map=column_map,
        report_filters=[{"column": "Name", "operator": "equals", "value": "a\nb\tc"}],
    )
    draft = build_soql(record)
    assert "WHERE Name = 'a\\nb\\tc'" in draft.soql


# ── 相対日付リテラル ─────────────────────────────────────────────


def test_relative_date_literal_in_allow_list_is_kept() -> None:
    """許可リストにある相対日付リテラルは、 そのまま書く。"""
    for literal in ("THIS_MONTH", "LAST_N_DAYS:30", "30_DAYS_AGO", "NEXT_N_WEEKS:2"):
        column_map = _column_map_with_types([("CloseDate", "CloseDate", "date")])
        record = _make_record(
            detail_columns=["CloseDate"],
            column_map=column_map,
            report_filters=[{"column": "CloseDate", "operator": "equals", "value": literal}],
        )
        draft = build_soql(record)
        assert f"WHERE CloseDate = {literal}" in draft.soql, literal


def test_relative_date_literal_not_in_allow_list_goes_to_missing() -> None:
    """許可リストに無い相対日付リテラルは、 missing。"""
    for literal in ("LAST_MONTHS", "THIS_FORTNIGHT", "LAST_N_DAY:30"):
        column_map = _column_map_with_types([("CloseDate", "CloseDate", "date")])
        record = _make_record(
            detail_columns=["CloseDate"],
            column_map=column_map,
            report_filters=[{"column": "CloseDate", "operator": "equals", "value": literal}],
        )
        draft = build_soql(record)
        assert "WHERE" not in draft.soql, literal
        assert any("相対日付リテラルが未対応" in m for m in draft.missing), literal


def test_duration_value_not_in_allow_list_goes_to_missing() -> None:
    """``durationValue`` も許可リストで判定する。"""
    column_map = _column_map_with_types([("CloseDate", "CloseDate", "date")])
    record = _make_record(
        detail_columns=["CloseDate"],
        column_map=column_map,
        standard_date_filter={
            "column": "CloseDate",
            "durationValue": "THIS_FORTNIGHT",
            "startDate": "",
            "endDate": "",
        },
    )
    draft = build_soql(record)
    assert "WHERE" not in draft.soql
    assert any("durationValue" in m for m in draft.missing)


# ── 相対日付の表記ゆれ ────────────────────────────────────────────

# 仕様表の ( 入力の例, 期待するリテラル ) 。 ``LAST_90_DAYS`` は固定語なので
# そのまま通す （ ``LAST_N_DAYS:90`` と同じ意味 ） 。
_RELATIVE_DATE_TABLE: list[tuple[str, str]] = [
    ("LAST_N_DAYS:60", "LAST_N_DAYS:60"),
    ("LAST N DAYS:60", "LAST_N_DAYS:60"),
    ("LAST_60_DAYS", "LAST_N_DAYS:60"),
    ("LAST 60 DAYS", "LAST_N_DAYS:60"),
    ("過去 60 日間", "LAST_N_DAYS:60"),
    ("過去60日", "LAST_N_DAYS:60"),
    ("過去60日間", "LAST_N_DAYS:60"),
    ("LAST_90_DAYS", "LAST_90_DAYS"),
    ("LAST 90 DAYS", "LAST_N_DAYS:90"),
    ("過去 90 日間", "LAST_N_DAYS:90"),
    ("NEXT 30 DAYS", "NEXT_N_DAYS:30"),
    ("NEXT_30_DAYS", "NEXT_N_DAYS:30"),
    ("次の 30 日間", "NEXT_N_DAYS:30"),
    ("今後 30 日間", "NEXT_N_DAYS:30"),
    ("NEXT 1 DAYS", "NEXT_N_DAYS:1"),
    ("翌 1 日", "NEXT_N_DAYS:1"),
    ("60 DAYS AGO", "N_DAYS_AGO:60"),
    ("N_DAYS_AGO:60", "N_DAYS_AGO:60"),
    ("60 日前", "N_DAYS_AGO:60"),
    ("LAST 3 WEEKS", "LAST_N_WEEKS:3"),
    ("過去 3 週間", "LAST_N_WEEKS:3"),
    ("NEXT 3 WEEKS", "NEXT_N_WEEKS:3"),
    ("次の 3 週間", "NEXT_N_WEEKS:3"),
    ("3 週間前", "N_WEEKS_AGO:3"),
    ("LAST 6 MONTHS", "LAST_N_MONTHS:6"),
    ("過去 6 か月", "LAST_N_MONTHS:6"),
    ("過去 6 ヶ月", "LAST_N_MONTHS:6"),
    ("過去 6 カ月", "LAST_N_MONTHS:6"),
    ("NEXT 6 MONTHS", "NEXT_N_MONTHS:6"),
    ("次の 6 か月", "NEXT_N_MONTHS:6"),
    ("6 か月前", "N_MONTHS_AGO:6"),
    ("LAST 2 YEARS", "LAST_N_YEARS:2"),
    ("過去 2 年間", "LAST_N_YEARS:2"),
    ("過去 2 年", "LAST_N_YEARS:2"),
    ("NEXT 2 YEARS", "NEXT_N_YEARS:2"),
    ("次の 2 年間", "NEXT_N_YEARS:2"),
    ("2 年前", "N_YEARS_AGO:2"),
    ("TODAY", "TODAY"),
    ("今日", "TODAY"),
    ("YESTERDAY", "YESTERDAY"),
    ("昨日", "YESTERDAY"),
    ("TOMORROW", "TOMORROW"),
    ("明日", "TOMORROW"),
    ("THIS WEEK", "THIS_WEEK"),
    ("今週", "THIS_WEEK"),
    ("LAST WEEK", "LAST_WEEK"),
    ("先週", "LAST_WEEK"),
    ("NEXT WEEK", "NEXT_WEEK"),
    ("来週", "NEXT_WEEK"),
    ("THIS MONTH", "THIS_MONTH"),
    ("今月", "THIS_MONTH"),
    ("LAST MONTH", "LAST_MONTH"),
    ("先月", "LAST_MONTH"),
    ("NEXT MONTH", "NEXT_MONTH"),
    ("来月", "NEXT_MONTH"),
    ("THIS QUARTER", "THIS_QUARTER"),
    ("今四半期", "THIS_QUARTER"),
    ("LAST QUARTER", "LAST_QUARTER"),
    ("前四半期", "LAST_QUARTER"),
    ("NEXT QUARTER", "NEXT_QUARTER"),
    ("次四半期", "NEXT_QUARTER"),
    ("THIS YEAR", "THIS_YEAR"),
    ("今年", "THIS_YEAR"),
    ("LAST YEAR", "LAST_YEAR"),
    ("昨年", "LAST_YEAR"),
    ("去年", "LAST_YEAR"),
    ("NEXT YEAR", "NEXT_YEAR"),
    ("来年", "NEXT_YEAR"),
]


def _expected_equality_condition(field_api: str, literal: str) -> str:
    """等号条件の期待値。

    「翌 N 日」 （ ``NEXT_N_DAYS:n`` ） はレポートでは当日を含めて n 日
    （ 当日 〜 n-1 日後 ） だが SOQL では翌日からなので、 ``TODAY`` を足した形
    が期待値になる。 n が 1 のときは当日だけなので ``TODAY`` の 1 条件。
    """
    if literal.startswith("NEXT_N_DAYS:"):
        days = int(literal.split(":", 1)[1])
        if days <= 1:
            return f"{field_api} = TODAY"
        return f"( {field_api} = TODAY OR {field_api} = NEXT_N_DAYS:{days - 1} )"
    return f"{field_api} = {literal}"


@pytest.mark.parametrize(("raw", "literal"), _RELATIVE_DATE_TABLE)
def test_relative_date_notation_is_converted_in_report_filter(raw: str, literal: str) -> None:
    """相対日付の表記ゆれは、 ``reportFilters`` でも SOQL のリテラルになる。"""
    column_map = _column_map_with_types([("CloseDate", "CloseDate", "date")])
    record = _make_record(
        detail_columns=["CloseDate"],
        column_map=column_map,
        report_filters=[{"column": "CloseDate", "operator": "equals", "value": raw}],
    )
    draft = build_soql(record)
    assert f"WHERE {_expected_equality_condition('CloseDate', literal)}" in draft.soql


@pytest.mark.parametrize(("raw", "literal"), _RELATIVE_DATE_TABLE)
def test_relative_date_notation_is_converted_in_datetime_report_filter(
    raw: str, literal: str
) -> None:
    """相対日付の表記ゆれは、 datetime の ``reportFilters`` でもリテラルになる。"""
    column_map = _column_map_with_types([("Done__c", "Done__c", "datetime")])
    record = _make_record(
        detail_columns=["Done__c"],
        column_map=column_map,
        report_filters=[{"column": "Done__c", "operator": "equals", "value": raw}],
    )
    draft = build_soql(record)
    assert f"WHERE {_expected_equality_condition('Done__c', literal)}" in draft.soql


@pytest.mark.parametrize(("raw", "literal"), _RELATIVE_DATE_TABLE)
def test_relative_date_notation_is_converted_in_standard_date_filter(
    raw: str, literal: str
) -> None:
    """相対日付の表記ゆれは、 ``standardDateFilter`` でもリテラルになる。"""
    column_map = _column_map_with_types([("CloseDate", "CloseDate", "date")])
    record = _make_record(
        detail_columns=["CloseDate"],
        column_map=column_map,
        standard_date_filter={
            "column": "CloseDate",
            "durationValue": raw,
            "startDate": "",
            "endDate": "",
        },
    )
    draft = build_soql(record)
    assert f"WHERE {_expected_equality_condition('CloseDate', literal)}" in draft.soql


@pytest.mark.parametrize(("raw", "literal"), _RELATIVE_DATE_TABLE)
def test_relative_date_notation_is_converted_in_datetime_standard_date_filter(
    raw: str, literal: str
) -> None:
    """相対日付の表記ゆれは、 datetime の ``standardDateFilter`` でもリテラルになる。"""
    column_map = _column_map_with_types([("Done__c", "Done__c", "datetime")])
    record = _make_record(
        detail_columns=["Done__c"],
        column_map=column_map,
        standard_date_filter={
            "column": "Done__c",
            "durationValue": raw,
            "startDate": "",
            "endDate": "",
        },
    )
    draft = build_soql(record)
    assert f"WHERE {_expected_equality_condition('Done__c', literal)}" in draft.soql


@pytest.mark.parametrize(("raw", "literal"), _RELATIVE_DATE_TABLE)
def test_relative_date_conversion_adds_check_only_when_changed(raw: str, literal: str) -> None:
    """相対日付を変換したときだけ、 checks に 1 件積む。"""
    column_map = _column_map_with_types([("CloseDate", "CloseDate", "date")])
    record = _make_record(
        detail_columns=["CloseDate"],
        column_map=column_map,
        report_filters=[{"column": "CloseDate", "operator": "equals", "value": raw}],
    )
    draft = build_soql(record)
    converted = [check for check in draft.checks if check.startswith("相対日付を変換した")]
    if raw == literal:
        assert converted == []
    else:
        assert converted == [
            f"相対日付を変換した: {raw} → {literal}（境界はレポートと SOQL で異なる場合あり）"
        ]


def test_relative_date_notation_that_cannot_be_read_goes_to_missing() -> None:
    """解釈できない相対日付は missing になり、 SOQL に出ない。"""
    for raw in ("過去 60 週日", "LAST 0 DAYS", "LAST 1.5 DAYS"):
        column_map = _column_map_with_types([("CloseDate", "CloseDate", "date")])
        record = _make_record(
            detail_columns=["CloseDate"],
            column_map=column_map,
            report_filters=[{"column": "CloseDate", "operator": "equals", "value": raw}],
        )
        draft = build_soql(record)
        assert "WHERE" not in draft.soql, raw
        assert draft.missing, raw


def test_relative_date_notation_in_string_field_is_kept_as_string() -> None:
    """文字列型の項目では、 相対日付の表記も文字列のまま。"""
    column_map = _column_map_with_types([("Name", "Name", "string")])
    record = _make_record(
        detail_columns=["Name"],
        column_map=column_map,
        report_filters=[{"column": "Name", "operator": "equals", "value": "今日"}],
    )
    draft = build_soql(record)
    assert "WHERE Name = '今日'" in draft.soql
    assert not any("相対日付" in check for check in draft.checks)


# ── 「翌 N 日」 は当日を含めて N 日 ─────────────────────────────────


def test_next_n_days_in_standard_date_filter_adds_today() -> None:
    """期間フィルターの ``NEXT_N_DAYS`` は当日を含めて n 日なので、 TODAY を足す。"""
    column_map = _column_map_with_types([("CloseDate", "CloseDate", "date")])
    record = _make_record(
        detail_columns=["CloseDate"],
        column_map=column_map,
        standard_date_filter={
            "column": "CloseDate",
            "durationValue": "NEXT_N_DAYS:60",
            "startDate": "",
            "endDate": "",
        },
    )
    draft = build_soql(record)
    assert "WHERE ( CloseDate = TODAY OR CloseDate = NEXT_N_DAYS:59 )" in draft.soql
    assert any(
        "翌 60 日は当日を含めて 60 日なので TODAY と NEXT_N_DAYS:59 にした: CloseDate" in check
        for check in draft.checks
    )


def test_next_n_days_in_datetime_standard_date_filter_adds_today() -> None:
    """datetime の期間フィルターでも同じ形にする。"""
    column_map = _column_map_with_types([("Done__c", "Done__c", "datetime")])
    record = _make_record(
        detail_columns=["Done__c"],
        column_map=column_map,
        standard_date_filter={
            "column": "Done__c",
            "durationValue": "NEXT_N_DAYS:60",
            "startDate": "",
            "endDate": "",
        },
    )
    draft = build_soql(record)
    assert "WHERE ( Done__c = TODAY OR Done__c = NEXT_N_DAYS:59 )" in draft.soql
    assert any(
        "翌 60 日は当日を含めて 60 日なので TODAY と NEXT_N_DAYS:59 にした: Done__c" in check
        for check in draft.checks
    )


@pytest.mark.parametrize("raw", ["翌60日", "翌 60 日間", "翌60日間"])
def test_next_n_days_in_japanese_report_filter_adds_today(raw: str) -> None:
    """「翌60日」 などは ``NEXT_N_DAYS:60`` として読み、 当日を含める。"""
    column_map = _column_map_with_types([("CloseDate", "CloseDate", "date")])
    record = _make_record(
        detail_columns=["CloseDate"],
        column_map=column_map,
        report_filters=[{"column": "CloseDate", "operator": "equals", "value": raw}],
    )
    draft = build_soql(record)
    assert "WHERE ( CloseDate = TODAY OR CloseDate = NEXT_N_DAYS:59 )" in draft.soql
    assert any(
        "相対日付を変換した: 翌60日 → NEXT_N_DAYS:60" in check
        or "相対日付を変換した: 翌 60 日間 → NEXT_N_DAYS:60" in check
        or "相対日付を変換した: 翌60日間 → NEXT_N_DAYS:60" in check
        for check in draft.checks
    ), draft.checks
    assert any(
        "翌 60 日は当日を含めて 60 日なので TODAY と NEXT_N_DAYS:59 にした: CloseDate" in check
        for check in draft.checks
    )


@pytest.mark.parametrize(
    ("field_api", "field_type"),
    [("CloseDate", "date"), ("Done__c", "datetime")],
)
def test_next_n_days_in_not_equal_report_filter_adds_today(
    field_api: str,
    field_type: str,
) -> None:
    """``notEqual`` のときは TODAY を足した形にして、 AND でつなぐ。"""
    column_map = _column_map_with_types([(field_api, field_api, field_type)])
    record = _make_record(
        detail_columns=[field_api],
        column_map=column_map,
        report_filters=[{"column": field_api, "operator": "notEqual", "value": "NEXT_N_DAYS:60"}],
    )
    draft = build_soql(record)
    assert f"WHERE ( {field_api} != TODAY AND {field_api} != NEXT_N_DAYS:59 )" in draft.soql
    assert any(
        f"翌 60 日は当日を含めて 60 日なので TODAY と NEXT_N_DAYS:59 にした: {field_api}" in check
        for check in draft.checks
    )


@pytest.mark.parametrize(
    ("op", "expected"),
    [("equals", "WHERE CloseDate = TODAY"), ("notEqual", "WHERE CloseDate != TODAY")],
)
def test_next_n_days_one_day_is_today_only(op: str, expected: str) -> None:
    """「翌 1 日」 は当日だけなので、 TODAY の 1 条件にする。"""
    column_map = _column_map_with_types([("CloseDate", "CloseDate", "date")])
    record = _make_record(
        detail_columns=["CloseDate"],
        column_map=column_map,
        report_filters=[{"column": "CloseDate", "operator": op, "value": "NEXT_N_DAYS:1"}],
    )
    draft = build_soql(record)
    assert expected in draft.soql
    assert any(
        "翌 1 日は当日を含めて 1 日なので TODAY だけにした: CloseDate" in check
        for check in draft.checks
    )


def test_next_n_days_with_another_value_keeps_parentheses() -> None:
    """「翌 N 日」 と別の値の 2 値では、 括弧が正しく入れ子になる。"""
    column_map = _column_map_with_types([("CloseDate", "CloseDate", "date")])
    record = _make_record(
        detail_columns=["CloseDate"],
        column_map=column_map,
        report_filters=[
            {"column": "CloseDate", "operator": "equals", "value": "NEXT_N_DAYS:60,2024-01-05"}
        ],
    )
    draft = build_soql(record)
    assert (
        "WHERE ( ( CloseDate = TODAY OR CloseDate = NEXT_N_DAYS:59 ) OR CloseDate = 2024-01-05 )"
        in draft.soql
    )


def test_other_relative_dates_are_not_changed() -> None:
    """「翌 N 日」 以外の相対日付は、 今までと同じ形にする。"""
    for value, expected in (
        ("LAST_N_DAYS:60", "WHERE CloseDate = LAST_N_DAYS:60"),
        ("NEXT_N_WEEKS:2", "WHERE CloseDate = NEXT_N_WEEKS:2"),
    ):
        column_map = _column_map_with_types([("CloseDate", "CloseDate", "date")])
        record = _make_record(
            detail_columns=["CloseDate"],
            column_map=column_map,
            report_filters=[{"column": "CloseDate", "operator": "equals", "value": value}],
        )
        draft = build_soql(record)
        assert expected in draft.soql, value
        assert not any("当日を含めて" in check for check in draft.checks), value


def test_next_n_days_keeps_parentheses_in_whole_where_clause() -> None:
    """他の条件と AND で結ばれても、 括弧が崩れない。"""
    column_map = _column_map_with_types(
        [("Name", "Name", "string"), ("CloseDate", "CloseDate", "date")]
    )
    record = _make_record(
        detail_columns=["Name", "CloseDate"],
        column_map=column_map,
        report_filters=[{"column": "Name", "operator": "equals", "value": "テスト"}],
        standard_date_filter={
            "column": "CloseDate",
            "durationValue": "NEXT_N_DAYS:60",
            "startDate": "",
            "endDate": "",
        },
    )
    draft = build_soql(record)
    assert "WHERE Name = 'テスト'" in draft.soql
    assert "AND" in draft.soql
    assert "( CloseDate = TODAY OR CloseDate = NEXT_N_DAYS:59 )" in draft.soql
    assert draft.soql.count("(") == draft.soql.count(")")


def test_next_n_days_keeps_parentheses_in_boolean_filter() -> None:
    """組立式の中に入っても、 括弧が崩れない。"""
    column_map = _column_map_with_types(
        [("Name", "Name", "string"), ("CloseDate", "CloseDate", "date")]
    )
    record = _make_record(
        detail_columns=["Name", "CloseDate"],
        column_map=column_map,
        report_filters=[
            {"column": "Name", "operator": "equals", "value": "テスト"},
            {"column": "CloseDate", "operator": "equals", "value": "NEXT_N_DAYS:60"},
        ],
        report_boolean_filter="1 OR 2",
    )
    draft = build_soql(record)
    assert "( CloseDate = TODAY OR CloseDate = NEXT_N_DAYS:59 )" in draft.soql
    assert draft.soql.count("(") == draft.soql.count(")")


# ── 日付の表記 ───────────────────────────────────────────────────


def test_date_value_without_zero_padding_is_normalized() -> None:
    """ゼロ埋め無しの日付も、 ``YYYY-MM-DD`` に直す。"""
    column_map = _column_map_with_types([("CloseDate", "CloseDate", "date")])
    record = _make_record(
        detail_columns=["CloseDate"],
        column_map=column_map,
        report_filters=[{"column": "CloseDate", "operator": "equals", "value": "2024/1/5"}],
    )
    draft = build_soql(record)
    assert "WHERE CloseDate = 2024-01-05" in draft.soql


# ── 取り方（ Bulk / 通常 SOQL ） ──────────────────────────────────────────


def _row_check(status: str, rows: int | None = None) -> RowCheck:
    """2000 行チェックの結果を作る。"""
    return RowCheck(status=status, rows=rows, reason=None)


def _simple_record(**kwargs: Any) -> ReportRecord:
    """副問い合わせの無い record を作る。"""
    column_map = _column_map_with_types([("Name", "Name", "string")])
    return _make_record(detail_columns=["Name"], column_map=column_map, **kwargs)


def test_bulk_when_master_says_over_2000_even_if_row_check_is_small() -> None:
    """管理表が ○ なら、 サンドボックスの行数が 50 件でも Bulk で取る。

    # なぜ: サンドボックスの 2000 件チェックは本番の件数ではないため、
    # 管理表の「2000件超」列を先に読む
    """
    record = _simple_record(
        exceeds_row_limit=True,
        row_check=_row_check("超えていない", 50),
    )
    draft = build_soql(record)

    assert draft.use_bulk is True
    assert draft.bulk_blockers == ()


def test_rest_when_master_says_not_over_and_row_count_is_small() -> None:
    """管理表が × で、 サンドボックスの行数が 1000 件以下なら通常 SOQL で取る。"""
    record = _simple_record(
        exceeds_row_limit=False,
        row_check=_row_check("超えていない", 50),
    )
    draft = build_soql(record)

    assert draft.use_bulk is False
    assert len(draft.bulk_blockers) == 1
    reason = draft.bulk_blockers[0]
    assert "1000 件以下" in reason
    # 根拠（ 管理表かサンドボックスか ） も書く
    assert "管理表" in reason
    assert "50 件" in reason


def test_bulk_when_row_count_is_over_1000() -> None:
    """管理表が × でも、 サンドボックスの行数が 1000 件超なら Bulk で取る。"""
    record = _simple_record(
        exceeds_row_limit=False,
        row_check=_row_check("超えていない", 1500),
    )
    draft = build_soql(record)

    assert draft.use_bulk is True
    assert draft.bulk_blockers == ()


def test_bulk_when_row_check_says_over_2000() -> None:
    """管理表が無くても、 2000 行チェックが「超えている」なら Bulk で取る。"""
    record = _simple_record(row_check=_row_check("超えている"))
    draft = build_soql(record)

    assert draft.use_bulk is True
    assert draft.bulk_blockers == ()


def test_bulk_when_master_cross_and_row_check_says_over_2000() -> None:
    """管理表が × でも、 2000 行チェックが「超えている」なら 1000 件超 として Bulk で取る。"""
    record = _simple_record(
        exceeds_row_limit=False,
        row_check=_row_check("超えている"),
    )
    draft = build_soql(record)

    assert draft.use_bulk is True
    assert draft.bulk_blockers == ()


def test_rest_when_only_row_check_says_small() -> None:
    """管理表が無くても、 2000 行チェックの行数が 1000 件以下なら通常 SOQL で取る。"""
    record = _simple_record(row_check=_row_check("超えていない", 50))
    draft = build_soql(record)

    assert draft.use_bulk is False
    assert len(draft.bulk_blockers) == 1
    reason = draft.bulk_blockers[0]
    assert "1000 件以下" in reason
    # 管理表の列が無ければ、 根拠はサンドボックスだけ
    assert "管理表" not in reason
    assert "サンドボックス" in reason


def test_bulk_when_count_is_unknown() -> None:
    """件数が分からないときは Bulk で取る（ 今までどおり ）。"""
    for row_check in (None, _row_check("判定不可")):
        record = _simple_record(row_check=row_check)
        draft = build_soql(record)

        assert draft.use_bulk is True
        assert draft.bulk_blockers == ()


def test_subquery_is_always_rest() -> None:
    """副問い合わせ （ クロスフィルタ ） があるときは、 件数に関係なく通常 SOQL で取る。"""
    record = _cross_filter_record([_cross_filter()], row_check=_row_check("超えていない", 50))
    draft = build_soql(record)

    assert draft.use_bulk is False
    assert any("副問い合わせ" in reason for reason in draft.bulk_blockers)


def test_subquery_with_over_2000_adds_check_to_split_soql() -> None:
    """副問い合わせがあり 2000 件超のときは、 5000 件を超えるなら SOQL を分ける注意を足す。"""
    record = _cross_filter_record([_cross_filter()], exceeds_row_limit=True)
    draft = build_soql(record)

    assert draft.use_bulk is False
    assert any("5000 件を超えるなら SOQL を分ける" in check for check in draft.checks)


def test_subquery_with_unknown_count_adds_check_to_split_soql() -> None:
    """副問い合わせがあり件数が不明のときも、 5000 件に関する注意を足す。"""
    record = _cross_filter_record([_cross_filter()])
    draft = build_soql(record)

    assert draft.use_bulk is False
    assert any("5000 件を超えるなら SOQL を分ける" in check for check in draft.checks)


def test_subquery_with_row_check_over_adds_check_to_split_soql() -> None:
    """副問い合わせがあり 2000 行チェックが「超えている」ときも、 5000 件の注意を足す。"""
    record = _cross_filter_record([_cross_filter()], row_check=_row_check("超えている"))
    draft = build_soql(record)

    assert draft.use_bulk is False
    assert any("5000 件を超えるなら SOQL を分ける" in check for check in draft.checks)


def test_subquery_with_small_count_has_no_split_check() -> None:
    """副問い合わせがあり 1000 件以下のときは、 5000 件の注意は出さない。

    件数が 1000 件以下だと分かっているので、 5000 件を超える可能性は無い
    """
    record = _cross_filter_record(
        [_cross_filter()],
        exceeds_row_limit=False,
        row_check=_row_check("超えていない", 50),
    )
    draft = build_soql(record)

    assert draft.use_bulk is False
    assert not any("5000 件" in check for check in draft.checks)
    # 通常 SOQL にした理由は 副問い合わせ と 件数 の 2 件
    assert len(draft.bulk_blockers) == 2
    assert any("副問い合わせ" in reason for reason in draft.bulk_blockers)
    assert any("1000 件以下" in reason for reason in draft.bulk_blockers)


def test_subquery_with_row_count_over_1000_has_no_split_check() -> None:
    """副問い合わせがあり 1000 件超 2000 件以下のときは、 5000 件の注意は出さない。"""
    record = _cross_filter_record(
        [_cross_filter()],
        row_check=_row_check("超えていない", 1500),
    )
    draft = build_soql(record)

    assert draft.use_bulk is False
    assert not any("5000 件" in check for check in draft.checks)
    # 件数は 1000 件以下ではないので、 通常 SOQL の理由には数えない
    assert len(draft.bulk_blockers) == 1
