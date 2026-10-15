"""``ReportRecord`` から SOQL を組み立てる純粋関数モジュール。

``build_soql(record) -> SoqlDraft`` を提供する。 API は呼ばない。 取れる
部分は SOQL にし、 取れない部分は ``SoqlDraft.missing`` に、 人が実行時に
見て判断するものは ``SoqlDraft.checks`` に書く。 次の段で SOQL の
``COUNT()`` とレポート件数の突き合わせを行うため、 省いた条件は必ず
``missing`` に残す。

組み立て規則は ``docs/仕様書.md`` の 「 SOQL 自動生成 」 節と依頼仕様の
対応表に従う。

対応表 (``column_map``) は ``mapping.build_column_map()`` が
``reportMetadata.detailColumns`` / ``reportFilters`` の列 /
``groupingsDown`` / ``groupingsAcross`` / ``aggregates`` の
列に対して作ったもの。 ``mapping.collect_describable_columns()`` が
``standardDateFilter`` と ``sortBy`` の列も含むように拡張されている前提。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date as _date
from typing import TYPE_CHECKING, Any

from src import mapping
from src.text_utils import coerce_str as _coerce_str

if TYPE_CHECKING:
    from src.fetch import ReportRecord

logger = logging.getLogger(__name__)

# 状態の定数。 ``SoqlDraft.status`` にそのまま入る
STATUS_CREATED = "作成済み"
STATUS_NEEDS_CHECK = "要確認"
STATUS_PARTIAL = "一部作れず"
STATUS_NONE = "作れず"

# 未完成 SOQL の 1 行目に置く印。 ダウンローダーがこの印を見て実行前に止める。
# 変えるときはダウンローダーと同時に。 この行があると Salesforce に投げても
# 必ず構文エラーになり、レポート以上の範囲を取る危険を物理的に防ぐ
INCOMPLETE_MARKER = "【未完成】作れなかった部分があります。このままでは実行できません。"


# 文字列系として '...' で囲む型。 column_map の ``型`` 列の値で判定する。
_STRING_TYPES: frozenset[str] = frozenset(
    {
        "string",
        "picklist",
        "multipicklist",
        "id",
        "reference",
        "email",
        "phone",
        "textarea",
        "url",
        "combobox",
    }
)

# 数値系としてそのまま書く型
_NUMERIC_TYPES: frozenset[str] = frozenset(
    {
        "int",
        "double",
        "currency",
        "percent",
        "number",
    }
)

# 日付として ``YYYY-MM-DD`` / 相対日付リテラルが書ける型
_DATE_TYPES: frozenset[str] = frozenset(
    {
        "date",
    }
)

# 日時として 相対日付リテラル / 日付 → ``YYYY-MM-DDT00:00:00Z`` に変換する型
_DATETIME_TYPES: frozenset[str] = frozenset(
    {
        "datetime",
    }
)

# boolean として true / false を書く型
_BOOLEAN_TYPES: frozenset[str] = frozenset(
    {
        "boolean",
        "checkbox",
    }
)

# 相対日付リテラル (``THIS_MONTH`` / ``LAST_N_DAYS:30`` など)。 ``^[A-Z_]+(:\d+)?$``
_RELATIVE_DATE_PATTERN = re.compile(r"^[A-Z_]+(:\d+)?$")

# 数値リテラルとして許容する形 (``-1.5`` / ``100`` など)。 カンマは含まない
# ( 桁区切りは SOQL 側で分割事故になるので missing に送る ) 。
_NUMERIC_PATTERN = re.compile(r"^-?\d+(\.\d+)?$")

# ``reportBooleanFilter`` の番号 (``1`` / ``10``) を 1 件ずつ区別して置換する
# 正規表現。 ``\b1\b`` だけだと ``10`` も ``1`` / ``0`` に分解されてしまうので、
# 必ず ``(?<!\d)`` / ``(?!\d)`` で挟む
_BOOLEAN_FILTER_NUMBER_PATTERN = re.compile(r"(?<!\d)(\d+)(?!\d)")

# SOQL リテラルのエスケープ ( 単一引用符とバックスラッシュ )
_QUOTE_ESCAPE_PATTERN = re.compile(r"(['\\])")


@dataclass(frozen=True)
class SoqlDraft:
    """``build_soql`` の戻り値。 ``frozen`` で再代入しない。

    Attributes:
        soql: 組み立てた SOQL ( 1 つの文 ) 。 作れなかったときは空文字。
        status: 状態。 ``作成済み`` / ``要確認`` / ``一部作れず`` / ``作れず``
            のいずれか。
        missing: 作れなかった部分 ( 1 件 1 要素 )。 例: 「 列: 商談名
            (Opp.Name) — 対応フィールドなし 」 「 条件 2: Stage equals
            Closed ( 省いたので件数が増える可能性 ) 」 。
        checks: 人が実行前に確認すべき事項 ( 1 件 1 要素 )。 例: 「 ス
            コープ=MyTerritory: 実行ユーザーで結果が変わる。 OwnerId 等
            の条件を足すか確認 」 「 日時の条件をタイムゾーン無しの UTC 0 時
            で置き換えた: CloseDate >= 2025-01-01 」 。
        count_soql: 件数チェック用に ``SELECT COUNT()`` へ書き換えた SOQL
            (``FROM`` / ``WHERE`` は同じ、 ``ORDER BY`` と ``LIMIT`` は
            付けない )。 ``build_soql`` が ``作れず`` のときは空文字。
            ``build_soql`` と同じ ``select_lines`` / ``where_lines`` から
            組み立てるため、 文字列の切り貼りをしない。
        limit: ``reportMetadata.topRows.rowLimit`` の正の整数。 件数
            チェックで ``min(totalSize, limit)`` を計算するための値。 無い
            / 読めないときは ``None`` 。
    """

    soql: str
    status: str
    missing: tuple[str, ...]
    checks: tuple[str, ...]
    count_soql: str = field(default="")
    limit: int | None = field(default=None)


def _escape_string_literal(value: str) -> str:
    """SOQL 文字列リテラル用に ``'`` と ``\\`` をエスケープして ``'...'`` で囲む。

    内部の ``'`` は ``\\'`` に、 ``\\`` は ``\\\\`` に置換する。 ``\\\\n`` 等の
    制御文字はここでは触らない ( 値に入っていない前提 ) 。
    """
    escaped = _QUOTE_ESCAPE_PATTERN.sub(r"\\\1", value)
    return "'" + escaped + "'"


def _looks_like_date(value: str) -> bool:
    """``YYYY-MM-DD`` / ``YYYY/MM/DD`` っぽい形なら True。 厳密には検査しない。"""
    return bool(re.fullmatch(r"\d{4}[-/]\d{2}[-/]\d{2}", value))


def _normalize_date_literal(value: str) -> tuple[str, str | None]:
    """日付リテラルを ``YYYY-MM-DD`` に正規化する。

    ``YYYY/MM/DD`` を ``YYYY-MM-DD`` に直し、 ``datetime.date.fromisoformat``
    で実在性を検査する。

    Returns:
        ``(normalized, missing_reason or None)`` 。
        表記または実在性が不正なときは ``(value, reason)`` 、 正常時は
        ``(normalized, None)`` 。
    """
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}[-/]\d{2}[-/]\d{2}", value):
        return value, f"日付の表記が不正: {value!r}"
    normalized = value.replace("/", "-")
    try:
        _date.fromisoformat(normalized)
    except ValueError:
        return value, f"実在しない日付: {value!r}"
    return normalized, None


def _format_value_for_field(
    value: str,
    field_type: str,
    field_name: str,
) -> tuple[str, list[str], list[str]]:
    """``column_map`` の ``型`` に応じて SOQL の値リテラルを書く。

    Args:
        value: レポート上の値 ( 文字列 ) 。
        field_type: ``column_map.型`` の値 ( 小文字化なし、 そのまま ) 。
        field_name: SOQL 上の項目名 ( エラーメッセージ用 ) 。

    Returns:
        ``(リテラル文字列, 追加 missing, 追加 checks)`` のタプル。
        型が分からない、 もしくは日付/日時/数値の値が解釈できないときは
        ``""`` と共に ``missing`` 側に理由を入れて返し、 呼び出し側で
        さらに「 演算子 N の値が解釈できません: ... 」 を足す。
        解釈できたが注意点がある ( 日時の UTC 化など ) ときは
        ``checks`` 側にメッセージを入れて返す。
    """
    field_type_lower = (field_type or "").strip().lower()
    # 空値
    if value == "":
        return "", [], []

    # 文字列系
    if field_type_lower in _STRING_TYPES:
        return _escape_string_literal(value), [], []

    # boolean
    if field_type_lower in _BOOLEAN_TYPES:
        lowered = value.strip().lower()
        if lowered in ("true", "1", "yes"):
            return "true", [], []
        if lowered in ("false", "0", "no"):
            return "false", [], []
        return (
            "",
            [f"値の解釈不可 ( {field_name}: boolean として解釈できない {value!r} ): missing"],
            [],
        )

    # 数値系
    if field_type_lower in _NUMERIC_TYPES:
        if _NUMERIC_PATTERN.fullmatch(value):
            return value, [], []
        if "," in value:
            return (
                "",
                [
                    f"値の解釈不可 ( {field_name}: 数値の値にカンマが含まれる可能性 "
                    f"{value!r} ): missing"
                ],
                [],
            )
        return "", [f"値の解釈不可 ( {field_name}: 数値として解釈できない {value!r} ): missing"], []

    # date
    if field_type_lower in _DATE_TYPES:
        # 既に相対日付リテラルならそのまま
        if _RELATIVE_DATE_PATTERN.fullmatch(value):
            return value, [], []
        # 日付として解釈できる形なら、 ``YYYY-MM-DD`` に正規化して返す
        # ( 実在しない日付・ ``YYYY/MM/DD`` 表記揺れを吸収 )
        if _looks_like_date(value):
            normalized, missing_msg = _normalize_date_literal(value)
            if missing_msg:
                return (
                    "",
                    [f"値の解釈不可 ( {field_name}: date として解釈できない {value!r} ): missing"],
                    [],
                )
            return normalized, [], []
        return (
            "",
            [f"値の解釈不可 ( {field_name}: date として解釈できない {value!r} ): missing"],
            [],
        )

    # datetime
    if field_type_lower in _DATETIME_TYPES:
        # 相対日付リテラルならそのまま
        if _RELATIVE_DATE_PATTERN.fullmatch(value):
            return value, [], []
        # 日付だけの値なら ``YYYY-MM-DDT00:00:00Z`` に拡張して checks 追加
        # ( ``YYYY/MM/DD`` 表記も ``-`` に正規化してから ``T00:00:00Z`` を付与 )
        if _looks_like_date(value):
            normalized, missing_msg = _normalize_date_literal(value)
            if missing_msg:
                return (
                    "",
                    [
                        f"値の解釈不可 ( {field_name}: datetime として解釈できない "
                        f"{value!r} ): missing"
                    ],
                    [],
                )
            return (
                f"{normalized}T00:00:00Z",
                [],
                [
                    f"日時の条件をタイムゾーン無しの UTC 0 時で置き換えた: "
                    f"{field_name} の {value!r} を {normalized}T00:00:00Z に"
                ],
            )
        return (
            "",
            [f"値の解釈不可 ( {field_name}: datetime として解釈できない {value!r} ): missing"],
            [],
        )

    # 型が分からない
    return "", [f"値の解釈不可 ( {field_name}: 型 {field_type!r} が未知 ): missing"], []


def _split_values(raw: object) -> list[str]:
    """``reportFilters[].value`` をカンマで分割する。

    レポートの値表現は数値でも ``"1,2"`` のように文字列で来ることがあるため、
    まずは文字列化して扱う。
    """
    text = _coerce_str(raw)
    if not text:
        return []
    return [part.strip() for part in text.split(",")]


def _build_column_lookup(column_map: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    """``column_map`` を ``列キー -> 行 dict`` の dict にする。同じ列キーは先頭を採用。"""
    lookup: dict[str, dict[str, str]] = {}
    for entry in column_map:
        lookup.setdefault(entry["列キー"], entry)
    return lookup


def _build_filter_condition(
    operator: str,
    values: list[str],
    field_api: str,
    field_type: str,
) -> tuple[str, list[str], list[str]]:
    """``reportFilters`` の 1 件を SOQL 条件式に組み立てる。

    Returns:
        ``(条件式文字列, 追加 missing, 追加 checks)`` のタプル。
        演算子が未対応 / 値が解釈不可のときは条件式は空文字で missing に
        入った要素を返す。
    """
    missing: list[str] = []
    checks: list[str] = []

    op = operator.strip()

    # 空値のときは ``equals`` / ``notEqual`` だけ ``null`` 比較に変換する
    if not values and op in ("equals", "notEqual"):
        if op == "equals":
            return f"{field_api} = null", missing, checks
        return f"{field_api} != null", missing, checks

    if op in ("equals", "notEqual"):
        # 値の書き方は型で決める
        parts: list[str] = []
        for value in values:
            literal, extra_missing, extra_checks = _format_value_for_field(
                value, field_type, field_api
            )
            if not literal:
                missing.append(f"演算子 {op} の値が解釈できません: {value!r}")
                missing.extend(extra_missing)
                continue
            checks.extend(extra_checks)
            parts.append(literal)
        if not parts:
            return "", missing, checks
        if op == "equals":
            if len(parts) == 1:
                return f"{field_api} = {parts[0]}", missing, checks
            return f"{field_api} IN ({', '.join(parts)})", missing, checks
        if len(parts) == 1:
            return f"{field_api} != {parts[0]}", missing, checks
        return f"{field_api} NOT IN ({', '.join(parts)})", missing, checks

    if op in ("lessThan", "greaterThan", "lessOrEqual", "greaterOrEqual"):
        op_map = {
            "lessThan": "<",
            "greaterThan": ">",
            "lessOrEqual": "<=",
            "greaterOrEqual": ">=",
        }
        symbol = op_map[op]
        if len(values) != 1:
            missing.append(f"演算子 {op} は単一値のみ対応 ( 値: {values!r} )")
            return "", missing, checks
        literal, extra_missing, extra_checks = _format_value_for_field(
            values[0], field_type, field_api
        )
        if not literal:
            missing.append(f"演算子 {op} の値が解釈できません: {values[0]!r}")
            missing.extend(extra_missing)
            return "", missing, checks
        checks.extend(extra_checks)
        return f"{field_api} {symbol} {literal}", missing, checks

    if op in ("contains", "notContain", "startsWith", "doesNotContain"):
        if not values:
            missing.append(f"演算子 {op} の値が無い")
            return "", missing, checks
        if field_type.strip().lower() not in _STRING_TYPES:
            missing.append(f"演算子 {op} は文字列系の項目でのみ使用可 ( 型: {field_type!r} )")
            return "", missing, checks
        pieces: list[tuple[str, str]] = []  # (value, literal)
        for value in values:
            # LIKE 内の '%' '_' はワイルドカードだがエスケープはしない ( そのまま機能する ) 。
            if "%" in value or "_" in value:
                checks.append(f"LIKE のワイルドカード '%' '_' が値に含まれる: {value!r}")
            literal = _escape_string_literal(f"%{value}%")
            pieces.append((value, literal))
        if op == "contains":
            # 複数なら (A OR B)、 単数ならそのまま
            parts_str = " OR ".join(f"{field_api} LIKE {piece[1]}" for piece in pieces)
            if len(pieces) == 1:
                return parts_str, missing, checks
            return f"( {parts_str} )", missing, checks
        if op == "notContain":
            # ``NOT (X LIKE '%v%')`` を AND で並べる
            parts_str = " AND ".join(f"NOT ({field_api} LIKE {piece[1]})" for piece in pieces)
            return f"( {parts_str} )", missing, checks
        if op == "startsWith":
            if len(pieces) != 1:
                missing.append(f"演算子 startsWith は単一値のみ対応 ( 値: {values!r} )")
                return "", missing, checks
            value, literal = pieces[0]
            if "%" in value or "_" in value:
                checks.append(f"LIKE のワイルドカード '%' '_' が値に含まれる: {value!r}")
            # pieces の literal は '%value%' 形式。 startsWith なので '%value' に直す
            starts_literal = _escape_string_literal(f"{value}%")
            return f"{field_api} LIKE {starts_literal}", missing, checks
        # doesNotContain は treats 同じ
        if op == "doesNotContain":
            parts_str = " AND ".join(f"NOT ({field_api} LIKE {piece[1]})" for piece in pieces)
            return f"( {parts_str} )", missing, checks

    if op in ("includes", "excludes"):
        if field_type.strip().lower() not in ("multipicklist", "picklist"):
            missing.append(
                f"演算子 {op} は multipicklist / picklist のみで使用可 ( 型: {field_type!r} )"
            )
            return "", missing, checks
        if not values:
            missing.append(f"演算子 {op} の値が無い")
            return "", missing, checks
        value_literals = ", ".join(_escape_string_literal(v) for v in values)
        if op == "includes":
            return f"{field_api} INCLUDES ({value_literals})", missing, checks
        return f"{field_api} EXCLUDES ({value_literals})", missing, checks

    if op == "within":
        missing.append("演算子 within は未対応 ( 手動で補ってください )")
        return "", missing, checks

    missing.append(f"未対応の演算子: {op!r}")
    return "", missing, checks


def _build_where_clause(
    metadata: dict[str, Any],
    column_lookup: dict[str, dict[str, str]],
) -> tuple[list[str], list[str], list[str]]:
    """``reportFilters`` / ``standardDateFilter`` / ``standardFilters`` /
    ``crossFilters`` から WHERE 句を組み立てる。

    ``reportFilters`` の条件は **1 始まり番号 → 条件式** の対応で持ち、
    組立式 (``reportBooleanFilter``) はその番号で ``( 条件式 )`` に
    置き換える。 日付条件 (``standardDateFilter``) は **別管理** にして、
    組立式の結果 ( 全体を 1 行として扱う ) と AND で並べる。 組立式が
    無いときは ``reportFilters`` の各条件と日付条件を AND でつなぐ。

    組立式の **参照番号** が ``reportFilters`` のどれかに対応しない
    とき ( その条件が ``column_map`` に無く作れなかった等 ) 、 式が壊れる
    ため WHERE は空にして missing に「 組立式の条件を作れない 」 を入れる。
    クロスフィルタや日付条件の ``missing`` だけでは WHERE 全体は捨てない。

    Returns:
        ``(top-level lines, missing, 追加 checks)`` 。 lines は 1 行 1 条件
        を表す文字列のリスト ( 組立式が 1 行にまとまる場合は 1 要素、
        通常は複数要素 )。 WHERE が無いときは空リスト。
    """
    missing: list[str] = []
    checks: list[str] = []

    # 1) reportFilters ( 1 始まり番号 → 条件式 )
    filter_conditions: dict[int, str] = {}
    for index, entry in enumerate(metadata.get("reportFilters", []), start=1):
        column = entry.get("column", "")
        if not column:
            missing.append(f"条件 {index}: 列キーが無い")
            continue
        column_info = column_lookup.get(column)
        if not column_info or column_info["対応フィールドAPI名"] in ("", "(不明)"):
            missing.append(f"条件 {index}: {column} ( 省いたので件数が増える可能性 )")
            continue
        field_api = column_info["対応フィールドAPI名"]
        field_type = column_info["型"]
        operator = entry.get("operator", "")
        raw_value = _coerce_str(entry.get("value"))
        # 数値系のときは value を カンマで分割せず 1 つの値として扱う
        # ( ``1,000`` のような桁区切りが IN で分割事故になる問題 ) 。
        # カンマが含まれている時点で桁区切りと見なして missing に送る。
        if field_type.strip().lower() in _NUMERIC_TYPES:
            if raw_value and "," in raw_value:
                missing.append(
                    f"条件 {index}: 数値の値にカンマが含まれる可能性 {raw_value!r}: missing"
                )
                continue
            values = [raw_value] if raw_value else []
        else:
            values = _split_values(raw_value)
        condition, extra_missing, extra_checks = _build_filter_condition(
            operator, values, field_api, field_type
        )
        if extra_missing:
            for msg in extra_missing:
                missing.append(f"条件 {index}: {msg}")
        if extra_checks:
            checks.extend(extra_checks)
        if condition:
            filter_conditions[index] = condition

    # 2) standardDateFilter ( 日付条件は別管理、 start と end を 1 行にまとめない方針 )
    date_lines: list[str] = []
    date_filter = metadata.get("standardDateFilter")
    if date_filter is not None:
        column = date_filter.get("column", "")
        duration = date_filter.get("durationValue", "")
        start = date_filter.get("startDate", "")
        end = date_filter.get("endDate", "")
        if not column:
            missing.append("日付条件の列が指定されていない")
        else:
            column_info = column_lookup.get(column)
            if not column_info or column_info["対応フィールドAPI名"] in ("", "(不明)"):
                missing.append(f"日付条件の列 {column} に対応が無い ( 省いた )")
            else:
                field_api = column_info["対応フィールドAPI名"]
                field_type = column_info["型"].strip().lower()
                if duration == "CUSTOM":
                    date_parts: list[str] = []
                    if start:
                        normalized_start, start_reason = _normalize_date_literal(start)
                        if start_reason:
                            missing.append(f"日付条件の startDate {start_reason}")
                        elif field_type in _DATETIME_TYPES:
                            date_parts.append(f"{field_api} >= {normalized_start}T00:00:00Z")
                            checks.append(
                                "日時の条件をタイムゾーン無しの UTC 0 時で置き換えた: "
                                f"{field_api} >= {normalized_start}T00:00:00Z"
                            )
                        else:
                            date_parts.append(f"{field_api} >= {normalized_start}")
                    if end:
                        normalized_end, end_reason = _normalize_date_literal(end)
                        if end_reason:
                            missing.append(f"日付条件の endDate {end_reason}")
                        elif field_type in _DATETIME_TYPES:
                            date_parts.append(f"{field_api} <= {normalized_end}T23:59:59Z")
                            checks.append(
                                "日時の条件をタイムゾーン無しの UTC 終端で置き換えた: "
                                f"{field_api} <= {normalized_end}T23:59:59Z"
                            )
                        else:
                            date_parts.append(f"{field_api} <= {normalized_end}")
                    if date_parts:
                        date_lines.append(" AND ".join(date_parts))
                elif duration:
                    # 相対日付リテラル
                    if _RELATIVE_DATE_PATTERN.fullmatch(duration):
                        date_lines.append(f"{field_api} = {duration}")
                    else:
                        missing.append(f"日付条件の durationValue {duration!r} を解釈できない")
                else:
                    # 全期間: 何もしない
                    pass

    # 3) standardFilters → checks に並べる ( 汎用には訳せない )
    standard_filters = metadata.get("standardFilters")
    if isinstance(standard_filters, list) and standard_filters:
        for index, sf in enumerate(standard_filters, start=1):
            checks.append(f"標準フィルタ {index}: {sf!r} ( そのままでは SOQL に翻訳できない )")

    # 4) crossFilters → missing ( 副問い合わせに送ると次の段で扱う )
    cross_filters = metadata.get("crossFilters")
    if isinstance(cross_filters, list) and cross_filters:
        for index, cf in enumerate(cross_filters, start=1):
            missing.append(f"クロスフィルタ {index}: {cf!r} ( 副問い合わせが必要 )")

    # 5) 組立式 ( 番号で ``( filter_conditions[number] )`` に置換、 行は 1 行にまとめる )
    lines: list[str] = []
    bool_filter = metadata.get("reportBooleanFilter", "")
    if bool_filter:
        try:
            combined = _replace_boolean_filter_numbers(bool_filter, filter_conditions)
            lines.append(combined)
        except ValueError as exc:
            missing.insert(0, f"組立式の解釈失敗: {exc}")
            missing.insert(
                0,
                "組立式の条件を作れない ( 条件を省くと式が壊れるため WHERE 全体を出さない )",
            )
            return [], missing, checks
    else:
        # 組立式が無い: 作れた reportFilters を番号順に 1 行ずつ並べる
        for index in sorted(filter_conditions):
            lines.append(filter_conditions[index])

    # 6) 日付条件を末尾に並べる ( 組立式がどれだけ複雑でも日付条件は AND で連結 )
    lines.extend(date_lines)

    return lines, missing, checks


def _replace_boolean_filter_numbers(
    bool_filter: str,
    conditions: dict[int, str],
) -> str:
    """``reportBooleanFilter`` の ``1`` / ``2`` / ``10`` を ``conditions`` で置換する。

    ``1`` と ``10`` がぶつからないように ``(?<!\\d)(\\d+)(?!\\d)`` で
    数字の塊を 1 個ずつ取り出して置換する。 同じ番号が 2 回出現した場合も
    同じ条件式で置き換える ( 2 回目を数字のまま残さない ) 。
    """

    def replace_one(match: re.Match[str]) -> str:
        number = int(match.group(1))
        if number not in conditions:
            raise ValueError(f"組立式に含まれる番号 {number} は条件一覧に無い")
        return f"( {conditions[number]} )"

    return _BOOLEAN_FILTER_NUMBER_PATTERN.sub(replace_one, bool_filter)


def _build_order_by_clause(
    metadata: dict[str, Any],
    column_lookup: dict[str, dict[str, str]],
) -> tuple[str, list[str], list[str]]:
    """``sortBy`` / ``topRows`` から ORDER BY と LIMIT を組み立てる。

    戻り値は ``(末尾に追加する文字列, missing, checks)`` 。 ``末尾に追加する
    文字列`` は ``ORDER BY ...`` 単体か、 ``ORDER BY ... LIMIT N`` 。
    """
    missing: list[str] = []
    checks: list[str] = []
    order_parts: list[str] = []

    for index, sb in enumerate(metadata.get("sortBy", []), start=1):
        sort_column = sb.get("sortColumn", "")
        if not sort_column:
            missing.append(f"並び順 {index}: 列キーが無い")
            continue
        column_info = column_lookup.get(sort_column)
        if not column_info or column_info["対応フィールドAPI名"] in ("", "(不明)"):
            missing.append(f"並び順 {index}: {sort_column} に対応が無い ( 省いた )")
            continue
        field_api = column_info["対応フィールドAPI名"]
        sort_order = sb.get("sortOrder", "").strip().upper()
        direction = "DESC" if sort_order == "DESC" else "ASC"
        order_parts.append(f"{field_api} {direction}")

    row_limit = _extract_top_rows_limit(metadata)
    limit_part = f"LIMIT {row_limit}" if row_limit else ""

    if not order_parts and not limit_part:
        return "", missing, checks
    if not order_parts:
        # LIMIT だけがあっても使えないので missing にして出さない
        missing.append("並び順が無いので LIMIT は出さない")
        return "", missing, checks

    result = "ORDER BY " + ", ".join(order_parts)
    if limit_part:
        result = f"{result}\n{limit_part}"
    return result, missing, checks


def build_soql(record: ReportRecord) -> SoqlDraft:
    """``record`` (``ReportRecord``) から ``SoqlDraft`` を組み立てる ( 純粋関数 ) 。

    規則は依頼仕様 ( ルール詳細 ) に従う。 ``record`` は ``ReportRecord``
    互換 (``key`` / ``summary`` / ``main_object`` / ``report`` / ``column_map``
    ) であれば良い。 ``report`` は ``_slim_report`` で絞った形式
    (``reportMetadata`` / ``reportExtendedMetadata`` のみ ) を想定。

    status の判定ルール:

    - ``作れず`` … 主オブジェクト不明 (``main_object`` が空 / ``None`` ) または
      SELECT に入れられる列が 1 つも無い ( 全列 ``(不明)`` または全列
      ``missing`` のため SELECT から落ちた ) 。
    - ``一部作れず`` … ``missing`` に 1 件以上ある ( SOQL から省いた部分がある )
    - ``要確認`` … ``missing`` は無いが ``checks`` に 1 件以上ある。
    - ``作成済み`` … ``missing`` も ``checks`` も無い。
    """
    main_object = record.main_object
    if not main_object:
        return SoqlDraft(
            soql="",
            status=STATUS_NONE,
            missing=("主オブジェクトが不明のため作れず",),
            checks=(),
        )

    # ``record.report`` は入口（ ``_slim_report`` ）で形をそろえてある
    metadata = record.report["reportMetadata"]
    column_lookup = _build_column_lookup(record.column_map)

    missing: list[str] = []
    checks: list[str] = []

    # ── SELECT 句 ──────────────────────────────────────────────────────
    select_lines: list[str] = []
    seen_fields: set[str] = set()
    for column_key in mapping.select_column_keys(metadata):
        column_info = column_lookup.get(column_key)
        if not column_info:
            missing.append(f"列: {column_key} ( {column_key} ) — 列対応表に無い")
            continue
        field_api = column_info["対応フィールドAPI名"]
        label = column_info["表示名"] or column_key
        reason = column_info["備考"]
        if not field_api or field_api == "(不明)":
            if reason:
                missing.append(f"列: {label} ( {column_key} ) — {reason}")
            else:
                missing.append(f"列: {label} ( {column_key} ) — 対応フィールドなし")
            continue
        if field_api in seen_fields:
            # 重複は除く ( 同一フィールドが detail / groupings 両方に )
            continue
        seen_fields.add(field_api)
        select_lines.append(field_api)

    # バケット・独自計算式・行レベルの数式・集計 (RowCount 以外) は
    # 集計のため missing に並べる
    buckets = metadata.get("buckets")
    if isinstance(buckets, list) and buckets:
        for index, bucket in enumerate(buckets, start=1):
            bucket_dict = bucket if isinstance(bucket, dict) else {}
            label = _coerce_str(bucket_dict.get("label"))
            name = _coerce_str(bucket_dict.get("developerName")) or _coerce_str(
                bucket_dict.get("name")
            )
            missing.append(
                f"バケット {index}: {name or '(名称不明)'} ( {label} ) "
                "( 集計のため SOQL には入れない )"
            )

    custom_summary_formula = metadata.get("customSummaryFormula")
    if isinstance(custom_summary_formula, list) and custom_summary_formula:
        for index, csf in enumerate(custom_summary_formula, start=1):
            csf_dict = csf if isinstance(csf, dict) else {}
            label = _coerce_str(csf_dict.get("label"))
            formula = _coerce_str(csf_dict.get("formula"))
            missing.append(
                f"独自計算式 {index}: {label or '(ラベル不明)'} = {formula} ( SOQL には入れない )"
            )

    # 集計 (``aggregates`` の ``"!"`` 形式) は SOQL にしない ( 集計レポートのため ) 、
    # ``RowCount`` だけは missing にも入れない ( 集計行が無いだけ ) 。
    for agg in metadata.get("aggregates", []):
        if "!" not in agg:
            continue
        target = agg.split("!", 1)[-1]
        # RowCount は集計行が無いだけ。 SELECT にも入れない ( SOQL でも RowCount は
        # 単純な関数で取れない )
        if target == "RowCount":
            continue
        missing.append(f"集計 {agg}: 集計レポートのため SOQL にしない ( 明細として取れない )")

    # 行レベルの数式 (``historicalSnapshotDates`` は時系列レポート用、 詳細は省略 )
    # は今回は SOQL にしない方針 ( missing にも入れない )

    # ``historicalSnapshotDates`` は履歴スナップショットの日付指定で、
    # SOQL に訳せないレポート側の機能。 黙って捨てるとレポート以上の範囲を
    # 取る危険があるため、 missing に理由を入れて ``一部作れず`` に倒す
    # （ 2026-10-15 ） 。
    snapshot_dates = metadata.get("historicalSnapshotDates")
    if isinstance(snapshot_dates, list) and snapshot_dates:
        missing.append(f"履歴スナップショット ({len(snapshot_dates)} 件) の条件は SOQL にできない")

    # ── SELECT 句が 1 列も無い → 作れず ────────────────────────────────
    if not select_lines:
        return SoqlDraft(
            soql="",
            status=STATUS_NONE,
            missing=(*tuple(missing), "SELECT に入れられる列が無い"),
            checks=tuple(checks),
        )

    # ── FROM 句 ( 主オブジェクト ) ──────────────────────────────────────
    # カスタムレポートタイプ (``CustomEntity$Account`` など) の表記揺れに備えて
    # 英数字と ``_`` 以外はそのまま渡す ( ``mapping.report_type_candidates()`` の
    # 結果を使うサンプルもあるが、 SOQL の ``FROM`` には使えないので ``main_object`` を採用 )

    # ── WHERE 句 ───────────────────────────────────────────────────────
    where_lines, where_missing, where_checks = _build_where_clause(metadata, column_lookup)
    missing.extend(where_missing)
    checks.extend(where_checks)

    # ── scope ──────────────────────────────────────────────────────────
    scope = metadata.get("scope", "")
    if scope and scope.lower() != "organization":
        checks.append(
            f"スコープ={scope}: 実行ユーザーで結果が変わる。 OwnerId 等の条件を足すか確認"
        )

    # ── ORDER BY / LIMIT ────────────────────────────────────────────────
    order_by_clause, order_missing, order_checks = _build_order_by_clause(metadata, column_lookup)
    missing.extend(order_missing)
    checks.extend(order_checks)

    # ── SOQL を組み立てる ───────────────────────────────────────────────
    select_block = ",\n  ".join(select_lines)
    lines: list[str] = [f"SELECT {select_block}", f"FROM {main_object}"]
    if where_lines:
        # 条件のリストを直接整形する ( 文字列リテラル内の ' AND ' で改行を入れて値を変えない )
        where_block = "\n  AND\n  ".join(where_lines)
        lines.append(f"WHERE {where_block}")
    if order_by_clause:
        lines.append(order_by_clause)

    soql = "\n".join(lines)

    # ── 件数用 SOQL を組み立てる ─────────────────────────────────────────
    # 同じ ``select_lines`` / ``where_lines`` から組み立てる （ 文字列の
    # 切り貼りをしない ）。 ``ORDER BY`` / ``LIMIT`` は付けない。
    count_lines: list[str] = ["SELECT COUNT()", f"FROM {main_object}"]
    if where_lines:
        where_block_count = "\n  AND\n  ".join(where_lines)
        count_lines.append(f"WHERE {where_block_count}")
    count_soql = "\n".join(count_lines)

    # ── topRows.rowLimit から ``limit`` を抽出 ────────────────────────
    limit: int | None = _extract_top_rows_limit(metadata)

    # ── 状態の決定 ──────────────────────────────────────────────────────
    if missing:
        status = STATUS_PARTIAL
    elif checks:
        status = STATUS_NEEDS_CHECK
    else:
        status = STATUS_CREATED

    # ── 安全策: 一部作れず の SOQL には 1 行目に印を付ける ────────────
    # この行は構文上不正なので、 そのまま Salesforce に投げると必ずエラーになり、
    # レポート以上の範囲を取る危険を物理的に防ぐ。 ダウンローダーはこの印を
    # 見て実行前に止める。 ``要確認`` には付けない （ 印が付くと SOQL として
    # 動かなくなるため、 実行前に人が補う想定のチェックだけの場合は付けない ）
    if status == STATUS_PARTIAL:
        soql = f"{INCOMPLETE_MARKER}\n{soql}"
        count_soql = f"{INCOMPLETE_MARKER}\n{count_soql}"

    return SoqlDraft(
        soql=soql,
        status=status,
        missing=tuple(missing),
        checks=tuple(checks),
        count_soql=count_soql,
        limit=limit,
    )


def build_count_soql(record: ReportRecord) -> tuple[str | None, int | None]:
    """``record.soql_draft`` に組み立て済みの件数用 SOQL と ``limit`` を返す。

    文字列の切り貼りはしない。 ``build_soql(record)`` が同じ部品
    (``select_lines`` / ``where_lines``) から組み立てた ``count_soql`` を
    そのまま返す。 ``fetch.py`` では ``build_soql`` を 1 レポート分につき
    1 回しか呼ばず、 その戻り値を record に持たせておいて、ここで読み出す。

    Args:
        record: ``ReportRecord`` 互換 (``main_object`` / ``report`` /
            ``column_map`` 属性があれば良い )。 ``soql_draft`` 属性があれば
            それを使う。 無ければ ``build_soql(record)`` を呼んで組み立てる。

    Returns:
        ``(count_soql, limit)``:
        - ``count_soql`` … ``SELECT COUNT() FROM ... WHERE ...`` の SOQL 。
          ``build_soql`` が ``作れず`` のときは ``None`` 。
        - ``limit`` … ``topRows.rowLimit`` の値。 無い / 読めないときは ``None`` 。
    """
    soql_draft = record.soql_draft or build_soql(record)

    if soql_draft.status == STATUS_NONE or not soql_draft.count_soql:
        return None, soql_draft.limit
    return soql_draft.count_soql, soql_draft.limit


def _extract_top_rows_limit(metadata: dict[str, Any]) -> int | None:
    """``topRows.rowLimit``（ 入口で正の整数か None にそろえてある ）を返す。"""
    return (metadata.get("topRows") or {}).get("rowLimit")
