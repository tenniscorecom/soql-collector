"""Report Describe から保守的に SOQL ドラフトを組み立てる。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_COMPARISON_OPERATORS = {
    "equals": "=",
    "notEqual": "!=",
    "lessThan": "<",
    "greaterThan": ">",
    "lessOrEqual": "<=",
    "greaterOrEqual": ">=",
}
_LIKE_OPERATORS = {"contains", "startsWith"}
_NUMBER_TYPES = {"currency", "double", "int", "long", "percent"}
_STRING_TYPES = {
    "string",
    "textarea",
    "picklist",
    "multipicklist",
    "phone",
    "email",
    "url",
    "id",
    "reference",
}
_TOKEN_RE = re.compile(r"\d+|AND|OR|NOT|\(|\)", re.IGNORECASE)


@dataclass(frozen=True)
class Draft:
    soql: str
    notes: tuple[str, ...]
    is_complete: bool


def _group_entries_by_site(entries: list[object]) -> list[tuple[type, list[object]]]:
    """URL が示す組織ごとに管理表エントリをまとめる。"""
    from comken.toolbox.salesforce.sites import site_for

    grouped: dict[type, list[object]] = {}
    for entry in entries:
        site_class = site_for(str(getattr(entry, "url")))
        grouped.setdefault(site_class, []).append(entry)
    return list(grouped.items())


class _BooleanFilterParser:
    def __init__(self, tokens: list[str], conditions: dict[int, str]) -> None:
        self.tokens = tokens
        self.conditions = conditions
        self.position = 0

    def parse(self) -> str:
        result = self._parse_or()
        if self.position != len(self.tokens):
            raise SyntaxError
        return result

    def _parse_or(self) -> str:
        result = self._parse_and()
        while self._accept("OR"):
            result = f"({result} OR {self._parse_and()})"
        return result

    def _parse_and(self) -> str:
        result = self._parse_factor()
        while self._accept("AND"):
            result = f"({result} AND {self._parse_factor()})"
        return result

    def _parse_factor(self) -> str:
        if self._accept("NOT"):
            return f"(NOT {self._parse_factor()})"
        if self._accept("("):
            result = self._parse_or()
            if not self._accept(")"):
                raise SyntaxError
            return result
        if self.position >= len(self.tokens) or not self.tokens[self.position].isdigit():
            raise SyntaxError
        number = int(self.tokens[self.position])
        self.position += 1
        if number not in self.conditions:
            raise SyntaxError
        return self.conditions[number]

    def _accept(self, expected: str) -> bool:
        if self.position < len(self.tokens) and self.tokens[self.position].upper() == expected:
            self.position += 1
            return True
        return False


def _expand_boolean_filter(expression: str, conditions: dict[int, str]) -> str | None:
    tokens = _TOKEN_RE.findall(expression)
    if "".join(tokens).upper() != re.sub(r"\s+", "", expression).upper():
        return None
    try:
        return _BooleanFilterParser(tokens, conditions).parse()
    except SyntaxError:
        return None


def _quote_value(value: str, field_type: str) -> str | None:
    if field_type in _NUMBER_TYPES and re.fullmatch(r"-?\d+(?:\.\d+)?", value):
        return value
    if field_type == "boolean" and value.lower() in {"true", "false"}:
        return value.lower()
    if field_type in _STRING_TYPES or field_type in {"date", "datetime"}:
        return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"
    return None


def _filter_to_condition(field: str, field_type: str, operator: str, value: str) -> str | None:
    quoted = _quote_value(value, field_type)
    if quoted is None:
        return None
    if operator in _COMPARISON_OPERATORS:
        return f"{field} {_COMPARISON_OPERATORS[operator]} {quoted}"
    if operator in _LIKE_OPERATORS and field_type in _STRING_TYPES:
        raw = value if operator == "startsWith" else f"%{value}%"
        return f"{field} LIKE {_quote_value(raw, field_type)}"
    return None


def _format_raw_filters(report_filters: list[object]) -> str:
    return " | ".join(_format_raw_dict(item) for item in report_filters if isinstance(item, dict))


def _format_raw_cross_filters(cross_filters: list[object]) -> str:
    return " | ".join(_format_raw_dict(item) for item in cross_filters if isinstance(item, dict))


def _format_raw_dict(data: dict) -> str:
    return ", ".join(f"{key}={value}" for key, value in data.items())


def _format_raw_aggregation(report_metadata: dict) -> str:
    parts = []
    for key in ("aggregates", "groupingsDown", "groupingsAcross"):
        value = report_metadata.get(key)
        if value:
            parts.append(f"{key}: {value}")
    return " | ".join(parts)


def _field_map(fields_table: list[dict[str, str]]) -> dict[str, tuple[str, str]]:
    return {
        row["列キー"]: (row.get("フィールドAPI名", ""), row.get("型", "")) for row in fields_table
    }


def _build_select_clause(
    detail_columns: list[str],
    field_map: dict[str, tuple[str, str]],
    notes: list[str],
) -> str:
    fields = []
    for column in detail_columns:
        mapping = field_map.get(column)
        if mapping and mapping[0] not in {"", "(不明)"}:
            fields.append(mapping[0])
        else:
            notes.append(f"列キー未解決: {column}")
    return ", ".join(dict.fromkeys(fields)) or "Id"


def _build_where_clause(
    report_filters: list[dict],
    boolean_filter: object,
    field_map: dict[str, tuple[str, str]],
    notes: list[str],
) -> tuple[str, bool]:
    conditions: dict[int, str] = {}
    is_complete = True
    for index, item in enumerate(report_filters, 1):
        mapping = field_map.get(item.get("column", ""))
        condition = (
            _filter_to_condition(
                mapping[0], mapping[1], item.get("operator", ""), str(item.get("value", ""))
            )
            if mapping
            else None
        )
        if condition is None:
            notes.append(f"フィルタを機械変換できません: {item.get('column', '')}")
            is_complete = False
        else:
            conditions[index] = condition
    if boolean_filter and conditions:
        expanded = _expand_boolean_filter(str(boolean_filter), conditions)
        if expanded is None:
            notes.append("reportBooleanFilterを解釈できません")
            return " AND ".join(conditions.values()), False
        return expanded, is_complete
    return " AND ".join(conditions.values()), is_complete


def _build_date_filter_condition(
    standard_date_filter: object, notes: list[str]
) -> tuple[str, bool]:
    if not standard_date_filter:
        return "", True
    notes.append("standardDateFilterは手動変換要")
    return "", False


def _validate_report_metadata(metadata: object) -> tuple[dict, str] | tuple[None, str]:
    if not isinstance(metadata, dict):
        return None, "describe()の戻り値が不正な形式です"
    report_metadata = metadata.get("reportMetadata")
    if not isinstance(report_metadata, dict):
        return None, "reportMetadataが不正な形式です"
    report_type = report_metadata.get("reportType")
    if not isinstance(report_type, dict) or not report_type.get("type"):
        return None, "reportType.typeから主オブジェクトを特定できません"
    return report_metadata, ""


def _validate_soql(salesforce_client: Any, soql: str) -> str | None:
    """互換用ヘルパー。通常フローでは実機検証を呼ばない。"""
    from comken.exceptions import SalesforceRequestError

    try:
        next(salesforce_client.query_rows(f"{soql} LIMIT 1"), None)
    except SalesforceRequestError as exc:
        return f"SOQL検証失敗: {exc}"
    return None


def _merge_catalog_rows(
    existing_rows: list[dict[str, str]], observed_rows: list[dict[str, str]]
) -> list[dict[str, str]]:
    """確認済み行を保護しながら観測候補をキー単位で統合する。"""
    key_columns = ("サイト", "レポートタイプ", "列キー")
    merged = {tuple(row.get(column, "") for column in key_columns): row for row in existing_rows}
    for observed in observed_rows:
        key = tuple(observed.get(column, "") for column in key_columns)
        current = merged.get(key)
        if current is None or current.get("確認状態") != "確認済み":
            merged[key] = observed
    return list(merged.values())


def _describe_and_build_draft(
    salesforce_client: Any,
    report_id: str,
    field_map: dict[str, tuple[str, str]],
) -> Draft:
    """1 レポートを describe し、ドラフトを返す。"""
    return compose_soql(salesforce_client.report.describe(report_id), field_map)


def compose_soql(metadata: dict, mappings: dict[str, tuple[str, str]]) -> Draft:
    report = metadata.get("reportMetadata", metadata)
    notes: list[str] = []
    report_type = report.get("reportType", {})
    object_name = report_type.get("type", "") if isinstance(report_type, dict) else ""
    if not object_name:
        return Draft("", ("主オブジェクトを特定できません",), False)
    if report.get("reportFormat") != "TABULAR":
        return Draft("", ("GROUP BY 手動組立要",), False)
    fields: list[str] = []
    is_complete = True
    for key in report.get("detailColumns", []):
        mapping = mappings.get(key)
        if mapping and mapping[0] and mapping[0] != "(不明)":
            fields.append(mapping[0])
        else:
            notes.append(f"列キー未解決: {key}")
            is_complete = False
    fields = list(dict.fromkeys(fields)) or ["Id"]
    conditions: dict[int, str] = {}
    for index, item in enumerate(report.get("reportFilters", []), 1):
        mapping = mappings.get(item.get("column", ""))
        condition = (
            _filter_to_condition(
                mapping[0], mapping[1], item.get("operator", ""), str(item.get("value", ""))
            )
            if mapping
            else None
        )
        if condition is None:
            notes.append(
                f"フィルタを機械変換できません: {item.get('column', '')}/{item.get('operator', '')}"
            )
            is_complete = False
        else:
            conditions[index] = condition
    where_clause = " AND ".join(conditions.values())
    boolean_filter = report.get("reportBooleanFilter")
    if boolean_filter and conditions:
        expanded = _expand_boolean_filter(str(boolean_filter), conditions)
        if expanded is None:
            notes.append("reportBooleanFilterを解釈できません")
            is_complete = False
        else:
            where_clause = expanded
    if report.get("standardDateFilter"):
        notes.append("standardDateFilterは手動変換要")
        is_complete = False
    if report.get("crossFilters"):
        notes.append("crossFiltersは個別対応要")
        is_complete = False
    soql = f"SELECT {', '.join(fields)} FROM {object_name}"
    if where_clause:
        soql += f" WHERE {where_clause}"
    return Draft(soql, tuple(notes), is_complete)
