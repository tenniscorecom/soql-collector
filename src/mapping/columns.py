"""``build_column_map()`` が扱う列キーの収集と表示名ヘルパー。

レポート describe の ``reportMetadata`` から ``build_column_map()`` が解決を
試みる列を取り出す処理と、 表示名関連の補助関数を置く。
"""

from __future__ import annotations

from typing import Any


def filter_only_columns(report_filters: list[dict[str, Any]]) -> list[str]:
    """``reportFilters`` にだけ現れる列キーを返す （SELECT には出ない列） 。"""
    return [
        report_filter["column"] for report_filter in report_filters if report_filter.get("column")
    ]


def grouping_columns(report_metadata: dict[str, Any]) -> list[str]:
    """``groupingsDown`` / ``groupingsAcross`` （``SUMMARY`` / ``MATRIX``）の列キー。"""
    return [
        grouping["name"]
        for grouping_key in ("groupingsDown", "groupingsAcross")
        for grouping in report_metadata.get(grouping_key, [])
        if grouping.get("name")
    ]


def aggregate_field_columns(aggregates: list[str]) -> list[str]:
    """``aggregates`` の集計対象列キーを返す。

    集計キーは ``"s!Amount"`` のように 「関数!列キー」 の形。 ``"!"`` が無い
    もの （``RowCount`` など） は列キーとして扱えないため無視する。
    """
    return [key.split("!", 1)[-1] for key in aggregates if "!" in key]


def date_filter_column(report_metadata: dict[str, Any]) -> str | None:
    """``standardDateFilter`` の列キーを 1 件だけ返す。 無ければ ``None`` 。

    レポート describe では日付条件の対象列は 1 つだけ。 SOQL 組立時に対応を
    取るために列対応表に含める必要がある。
    """
    date_filter = report_metadata.get("standardDateFilter") or {}
    return date_filter.get("column") or None


def sort_by_columns(report_metadata: dict[str, Any]) -> list[str]:
    """``sortBy`` の ``sortColumn`` を取り出して順番通りに返す。

    SELECT には出ないが ``ORDER BY`` で使う列も列対応表に含める
    （``build_column_map()`` がラベルで解決するため） 。
    """
    return [
        entry["sortColumn"]
        for entry in report_metadata.get("sortBy", [])
        if entry.get("sortColumn")
    ]


def collect_describable_columns(report_metadata: dict[str, Any]) -> list[str]:
    """``build_column_map()`` が解決を試みる列キーの一覧を組み立てる （重複除去済み） 。

    出力に出る列 （``detailColumns``） に加え、 SELECT には出ないが
    ``reportFilters`` だけで使う列、 グルーピング列 （``groupingsDown`` /
    ``groupingsAcross``） 、 集計対象列、 ``standardDateFilter`` の列、
    ``sortBy`` の列も含める。 順序と重複除去の規則は
    ``build_column_map()`` 互換。
    """
    columns: list[str] = []
    for column_key in (
        report_metadata.get("detailColumns", [])
        + filter_only_columns(report_metadata.get("reportFilters", []))
        + grouping_columns(report_metadata)
        + aggregate_field_columns(report_metadata.get("aggregates", []))
        + [column for column in [date_filter_column(report_metadata)] if column]
        + sort_by_columns(report_metadata)
    ):
        if column_key not in columns:
            columns.append(column_key)
    return columns


def select_column_keys(report_metadata: dict[str, Any]) -> list[str]:
    """SELECT に入れる列のキー （``detailColumns`` → グルーピング列） 。 重複は除かない。"""
    return report_metadata.get("detailColumns", []) + grouping_columns(report_metadata)


def output_column_keys(report_metadata: dict[str, Any]) -> list[str]:
    """レポートの出力に出る列のキー （SELECT の列 → 集計の対象列） 。 重複は除かない。

    個人情報の判定など 「出力に出る列」 だけを見るときに使う。 絞り込みに
    だけ使う列は含まない。
    """
    return select_column_keys(report_metadata) + aggregate_field_columns(
        report_metadata.get("aggregates", [])
    )


def collect_column_info(metadata: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """``build_column_map()`` の表示名引き当てに使う列情報を組み立てる。

    グルーピング列・集計列の表示名は ``detailColumnInfo`` ではなく
    ``groupingColumnInfo`` / ``aggregateColumnInfo`` 側に入っているため、
    同じ列キー空間としてマージする （キーの重複は無い前提） 。
    """
    extended_metadata: dict[str, dict[str, Any]] = metadata.get("reportExtendedMetadata", {})
    column_info: dict[str, dict[str, Any]] = {}
    for info_key in ("detailColumnInfo", "groupingColumnInfo", "aggregateColumnInfo"):
        column_info.update(extended_metadata.get(info_key, {}))
    return column_info


def field_name_from_soql(soql_name: str) -> str:
    """``"Account.Owner.Name"`` から末端の ``"Name"`` を取り出す。 ``(不明)`` は空文字。"""
    if not soql_name or soql_name == "(不明)":
        return ""
    return soql_name.rsplit(".", 1)[-1]
