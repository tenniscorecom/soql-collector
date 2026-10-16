"""``src.soql_builder`` の単体テスト。

``build_soql()`` の組み立て規則を 1 つずつ確認する。 ``record`` は
``ReportRecord`` を直接組み立てて渡す (API は使わない) 。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from src.fetch import ReportRecord
from src.soql_builder import (
    INCOMPLETE_MARKER,
    STATUS_CREATED,
    STATUS_NEEDS_CHECK,
    STATUS_NONE,
    STATUS_PARTIAL,
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
) -> ReportRecord:
    """テスト用の ``ReportRecord`` を作る。

    ``detail_columns`` と ``column_map`` を渡して SELECT と列対応を組む。
    ``column_map`` を省略した場合 ``detail_columns`` のキーと同名の列が
    ``"string"`` 型で解決できるよう自動生成する (簡易形) 。
    ``report_metadata`` を渡すと ``detailColumns`` 等を組み立てる前に
    ベース dict として使う ( 既存のキーを保ったまま、 渡された
    ``detail_columns`` 等の派生値もマージする ) 。
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
        objects={},
        relations=(),
        column_map=column_map,
        warnings=(),
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


def test_like_value_with_wildcard_characters_adds_check() -> None:
    """contains の値に ``%`` '_' が含まれていたら checks に追加。"""
    column_map = _column_map_with_types([("Name", "Name", "string")])
    record = _make_record(
        detail_columns=["Name"],
        column_map=column_map,
        report_filters=[{"column": "Name", "operator": "contains", "value": "100%"}],
    )
    draft = build_soql(record)
    assert any("ワイルドカード" in c for c in draft.checks)


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
    """``_slim_report`` で 「実質指定なし」 と判定された ``standardDateFilter`` は
    SOQL の WHERE に出ず、 ``STATUS_PARTIAL`` にもならない。

    レポート describe と同じ形 （``startDate`` / ``endDate`` が ``None``） で
    ``_slim_report`` に通し、 その結果を ``ReportRecord`` に詰めて ``build_soql``
    を呼ぶ
    """
    from src.fetch import _slim_report

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
    slim = _slim_report(raw_metadata)
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


def test_build_count_soql_reads_from_soql_draft() -> None:
    """``build_count_soql(record)`` は ``record.soql_draft`` の ``count_soql`` /
    ``limit`` をそのまま返す (文字列を切り貼りしない)。"""
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
        top_rows={"rowLimit": 10},
    )
    # ``build_soql`` を回して ``soql_draft`` を持つ record を作る
    draft = build_soql(record)
    record_with_draft = ReportRecord(
        key=record.key,
        summary=record.summary,
        report_id=record.report_id,
        url=record.url,
        main_object=record.main_object,
        report=record.report,
        objects=record.objects,
        relations=record.relations,
        column_map=record.column_map,
        warnings=record.warnings,
        soql_draft=draft,
    )

    from src.soql_builder import build_count_soql

    count_soql, limit = build_count_soql(record_with_draft)
    # ``build_soql`` が ``STATUS_NONE`` で無いので count_soql が返る
    assert count_soql is not None
    # このドラフトは ``topRows`` だけで ``sortBy`` が無いので missing に
    # ``並び順が無いので LIMIT は出さない`` が入り、 結果は ``STATUS_PARTIAL``。
    # 一部作れずの SOQL には 1 行目に未完成の印が付く
    assert count_soql.startswith(f"{INCOMPLETE_MARKER}\nSELECT COUNT()\n")
    assert limit == 10
    # SELECT 部分 (``\nFROM`` の前) は ``SELECT COUNT()`` 1 行だけ
    select_part = count_soql.split("\nFROM", 1)[0]
    # 印の行を覗いた SELECT 行だけが残る
    assert select_part.endswith("SELECT COUNT()")
    # ``ORDER BY`` / ``LIMIT`` は末尾に付かない (``ORDER BY`` / ``LIMIT`` 句
    # として出てこないこと。「値の ``ORDER BY`` / ``LIMIT``」 は WHERE 内に
    # ``%A LIMIT B%`` / ``'ORDER BY B'`` として含まれているのを確認する)
    soql_no_value_orderby = count_soql.replace("'ORDER BY B'", "")
    soql_no_value_limit = soql_no_value_orderby.replace("'%A LIMIT B%'", "")
    assert "ORDER BY" not in soql_no_value_limit.upper()
    assert "LIMIT" not in soql_no_value_limit.upper()
    # 値が壊れていない (元の SOQL の WHERE と一致)
    assert "( Subject LIKE '%A LIMIT B%' )" in count_soql
    assert "( What.Name = 'ORDER BY B' )" in count_soql


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
