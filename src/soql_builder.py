"""``ReportRecord`` から SOQL を組み立てる純粋関数モジュール。

``build_soql(record) -> SoqlDraft`` を提供する。 API は呼ばない。 取れる
部分は SOQL にし、 取れない部分は ``SoqlDraft.missing`` に、 人が実行時に
見て判断するものは ``SoqlDraft.checks`` に書く。 次の段で SOQL の
``COUNT()`` とレポート件数の突き合わせを行うため、 省いた条件は必ず
``missing`` に残す。

組み立て規則は ``docs/仕様書.md`` の 「SOQL 自動生成」 節と依頼仕様の
対応表に従う。

対応表 (``column_map``) は ``mapping.build_column_map()`` が
``reportMetadata.detailColumns`` / ``reportFilters`` の列 /
``groupingsDown`` / ``groupingsAcross`` / ``aggregates`` の
列に対して作ったもの。 ``mapping.collect_describable_columns()`` が
``standardDateFilter`` と ``sortBy`` の列も含むように拡張されている前提。
"""

from __future__ import annotations

import csv
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from dataclasses import replace as dc_replace
from datetime import date as _date
from typing import TYPE_CHECKING, Any

from src import mapping
from src.mapping.owner import OWNER_POLYMORPHIC_NOTE
from src.mapping.row_status import auto_path_parts
from src.row_size import (
    BULK_MIN_ROWS,
    REST_MAX_ROWS_WITH_SUBQUERY,
    RowSize,
    decide_row_size,
)
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


# 副問い合わせは 1 文に 2 個まで。 Salesforce の SOQL の制限に合わせる。
# 3 個目以降のクロスフィルタは missing にする（省くとレポート以上の範囲を
# 取る危険があるため）。
CROSS_FILTER_MAX_SUBQUERIES: int = 2

# 副問い合わせの SOQL を Bulk API 2.0 で取れるか確かめていないため、 REST に
# するときの理由。 ``SoqlDraft.bulk_blockers`` に入る。
BULK_BLOCKER_SUBQUERY: str = (
    "WHERE の副問い合わせを Bulk API 2.0 で動作確認していないため REST で取る"
)

# 件数が 1000 件以下なので、 通常 SOQL で取るときの理由。 根拠（ 管理表か
# サンドボックスか ） も一緒に書く。 ``SoqlDraft.bulk_blockers`` に入る。
BULK_BLOCKER_SMALL_ROWS: str = "件数が {} 件以下のため"

# 副問い合わせ （ クロスフィルタ ） があるとき、 本番の件数が
# ``REST_MAX_ROWS_WITH_SUBQUERY`` を超えるなら SOQL を分ける必要があるときの
# 注意。 ``SoqlDraft.checks`` に入る。
SUBQUERY_ROWS_CHECK: str = (
    f"通常 SOQL で取るが、 本番の件数が {REST_MAX_ROWS_WITH_SUBQUERY} 件を超えるなら SOQL を分ける"
)

# クロスフィルタを副問い合わせにしたときの checks メッセージの頭
CROSS_FILTER_CHECK_PREFIX: str = (
    "クロスフィルタを副問い合わせにした（公式仕様の形から作った。レポートと件数を実物で確かめる）"
)

# クロスフィルタの「主オブジェクトと子オブジェクトの繋がり」 の判定。 3 種
# - ``直接`` … 直接つながるリレーション。 副問い合わせに入れてよい
# - ``直接でない`` … 情報はそろっているが直接つながっていない
# - ``確認できず`` … 情報が足りないので確認できない
CROSS_FILTER_LINK_DIRECT: str = "直接"
CROSS_FILTER_LINK_NOT_DIRECT: str = "直接でない"
CROSS_FILTER_LINK_UNVERIFIED: str = "確認できず"

# FROM を子オブジェクトに切り替えたレポートで、 クロスフィルタを副問い合わせに
# できない理由
CROSS_FILTER_CHILD_BASE_REASON: str = "子から取るレポートのクロスフィルタは未対応"

# 手入力 （ ``input/子リレーション.csv`` ） の子リレーションを使ったときの、
# 判定理由の頭に付ける言葉
CROSS_FILTER_MANUAL_REASON: str = "手入力の子リレーション"

# 組立式が壊れて WHERE 全体を出さないときの、 副問い合わせに入れなかった理由
BROKEN_BOOLEAN_FILTER_REASON: str = "組立式の条件が壊れたため WHERE 全体を出さない"

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
        "long",
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

# 相対日付リテラル候補の形 （ 英大文字・数字・_ と :数字 ）。
# 候補かどうかだけを判定し、 実際に出してよい語は下の許可リストで見る
_RELATIVE_DATE_PATTERN = re.compile(r"^[A-Z0-9_]+(:\d+)?$")

# 出してよい相対日付リテラル （ 数値なし ）
_RELATIVE_DATE_LITERAL_NAMES: frozenset[str] = frozenset(
    {
        "YESTERDAY",
        "TODAY",
        "TOMORROW",
        "LAST_WEEK",
        "THIS_WEEK",
        "NEXT_WEEK",
        "LAST_MONTH",
        "THIS_MONTH",
        "NEXT_MONTH",
        "LAST_90_DAYS",
        "NEXT_90_DAYS",
        "THIS_QUARTER",
        "LAST_QUARTER",
        "NEXT_QUARTER",
        "THIS_YEAR",
        "LAST_YEAR",
        "NEXT_YEAR",
        "THIS_FISCAL_QUARTER",
        "LAST_FISCAL_QUARTER",
        "NEXT_FISCAL_QUARTER",
        "THIS_FISCAL_YEAR",
        "LAST_FISCAL_YEAR",
        "NEXT_FISCAL_YEAR",
    }
)

# ``LAST_N_DAYS:30`` の形 （ N の部分是数字 ） で出してよい相対日付リテラル
_RELATIVE_DATE_N_PREFIX_PATTERN = re.compile(
    r"^(?:LAST|NEXT)_N_"
    r"(?:DAYS|WEEKS|MONTHS|QUARTERS|YEARS|FISCAL_QUARTERS|FISCAL_YEARS)"
    r"(?::\d+)?$"
)

# ``30_DAYS_AGO`` の形 （ N の部分は数字 ） で出してよい相対日付リテラル
_RELATIVE_DATE_N_SUFFIX_PATTERN = re.compile(
    r"^(?:\d+_(?:DAYS|WEEKS|MONTHS|QUARTERS|YEARS|FISCAL_QUARTERS|FISCAL_YEARS)_AGO(?::\d+)?"
    r"|N_(?:DAYS|WEEKS|MONTHS|QUARTERS|YEARS|FISCAL_QUARTERS|FISCAL_YEARS)_AGO:\d+)$"
)

# 「翌 N 日」 の相対日付リテラル （ 日単位だけ ）。 レポートの 「翌 N 日」 は
# 当日を含めて N 日 （ 当日 〜 N-1 日後 ） だが、 SOQL の ``NEXT_N_DAYS:N`` は
# 翌日から N 日なので、 ``TODAY`` を足した形に組み立て直す
_NEXT_N_DAYS_LITERAL_PATTERN = re.compile(r"^NEXT_N_DAYS:(?P<days>\d+)$")

# 相対日付の表記ゆれを吸収する変換表。 全角の ASCII 文字 （ 全角数字や
# 全角空白を含む） を半角にする
_RELATIVE_DATE_FULLWIDTH_TABLE = str.maketrans(
    {chr(code): chr(code - 0xFEE0) for code in range(0xFF01, 0xFF5F)} | {0x3000: " "}
)

# 日本語の 「か月」 のゆれ （ か月 / ヶ月 / ヵ月 / カ月 / ケ月 ） を
# 「月」 にそろえる
_JA_MONTH_VARIANTS: tuple[str, ...] = ("ヶ月", "ヵ月", "カ月", "ケ月", "か月")

# 相対日付の固定語。 キーは照合用の形 （ 小文字・空白なし・``_`` なし ） 。
# 値は SOQL のリテラル
_RELATIVE_DATE_FIXED_LITERALS: dict[str, str] = {
    "today": "TODAY",
    "今日": "TODAY",
    "yesterday": "YESTERDAY",
    "昨日": "YESTERDAY",
    "tomorrow": "TOMORROW",
    "明日": "TOMORROW",
    "thisweek": "THIS_WEEK",
    "今週": "THIS_WEEK",
    "lastweek": "LAST_WEEK",
    "先週": "LAST_WEEK",
    "nextweek": "NEXT_WEEK",
    "来週": "NEXT_WEEK",
    "thismonth": "THIS_MONTH",
    "今月": "THIS_MONTH",
    "lastmonth": "LAST_MONTH",
    "先月": "LAST_MONTH",
    "nextmonth": "NEXT_MONTH",
    "来月": "NEXT_MONTH",
    "thisquarter": "THIS_QUARTER",
    "今四半期": "THIS_QUARTER",
    "lastquarter": "LAST_QUARTER",
    "前四半期": "LAST_QUARTER",
    "nextquarter": "NEXT_QUARTER",
    "次四半期": "NEXT_QUARTER",
    "thisyear": "THIS_YEAR",
    "今年": "THIS_YEAR",
    "lastyear": "LAST_YEAR",
    "昨年": "LAST_YEAR",
    "去年": "LAST_YEAR",
    "nextyear": "NEXT_YEAR",
    "来年": "NEXT_YEAR",
    "thisfiscalquarter": "THIS_FISCAL_QUARTER",
    "lastfiscalquarter": "LAST_FISCAL_QUARTER",
    "nextfiscalquarter": "NEXT_FISCAL_QUARTER",
    "thisfiscalyear": "THIS_FISCAL_YEAR",
    "lastfiscalyear": "LAST_FISCAL_YEAR",
    "nextfiscalyear": "NEXT_FISCAL_YEAR",
}

# 相対日付の単位。 キーは照合用の形、 値は SOQL のリテラルに使う名前
_RELATIVE_DATE_UNITS: dict[str, str] = {
    "days": "DAYS",
    "weeks": "WEEKS",
    "months": "MONTHS",
    "quarters": "QUARTERS",
    "years": "YEARS",
    "fiscalquarters": "FISCAL_QUARTERS",
    "fiscalyears": "FISCAL_YEARS",
    "日間": "DAYS",
    "日": "DAYS",
    "週間": "WEEKS",
    "週": "WEEKS",
    "月": "MONTHS",
    "年間": "YEARS",
    "年": "YEARS",
}

# 相対日付の表記パターン。 すべて照合用の形 （ 小文字・空白なし・``_`` なし ）
# に対して使う。 2 番目は方向 （ "last" / "next" / "ago" ） 。 数量は 1 以上
_RELATIVE_DATE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # ``lastndays:60`` / ``nextndays:60``
    (re.compile(r"lastn(?P<unit>[a-z]+):(?P<count>\d+)"), "last"),
    (re.compile(r"nextn(?P<unit>[a-z]+):(?P<count>\d+)"), "next"),
    (re.compile(r"n(?P<unit>[a-z]+)ago:(?P<count>\d+)"), "ago"),
    # ``last60days`` / ``next60days`` / ``60daysago``
    (re.compile(r"last(?P<count>\d+)(?P<unit>[a-z]+)"), "last"),
    (re.compile(r"next(?P<count>\d+)(?P<unit>[a-z]+)"), "next"),
    (re.compile(r"(?P<count>\d+)(?P<unit>[a-z]+)ago"), "ago"),
    # 日本語: ``過去60日間`` / ``次の30日間`` / ``翌60日間`` / ``60日前``
    (re.compile(r"過去(?P<count>\d+)(?P<unit>[^0-9]+)"), "last"),
    (re.compile(r"(?:次の|今後|翌)(?P<count>\d+)(?P<unit>[^0-9]+)"), "next"),
    (re.compile(r"(?P<count>\d+)(?P<unit>[^0-9]+)前"), "ago"),
)

# LIKE を使える型。 ``id`` / ``reference`` / ``multipicklist`` は LIKE では
# 絞れないので missing にする
_LIKE_TYPES: frozenset[str] = _STRING_TYPES - {"id", "reference", "multipicklist"}

# ``IN`` / ``NOT IN`` を使えない型。 複数値は ``=`` / ``!=`` を OR / AND で並べる
_NO_IN_TYPES: frozenset[str] = frozenset({"multipicklist", "boolean", "checkbox"})

# 比較演算子 （ ``<`` ``<=`` ``>`` ``>=`` ） を使えない型。 レポートの
# 選択リスト順と SOQL の比較は違うため、 数値・日付の項目に限る
_NO_COMPARISON_TYPES: frozenset[str] = frozenset(
    {
        "boolean",
        "checkbox",
        "picklist",
        "multipicklist",
        "combobox",
        "textarea",
    }
)

# レポート側の演算子名の表記ゆれ。 大文字小文字を無視して正規名へ寄せる
_OPERATOR_ALIASES: dict[str, str] = {
    "lessorequal": "lessOrEqual",
    "lessequal": "lessOrEqual",
    "lessthanorequal": "lessOrEqual",
    "greaterorequal": "greaterOrEqual",
    "greaterequal": "greaterOrEqual",
    "greaterthanorequal": "greaterOrEqual",
    "notequal": "notEqual",
    "notequals": "notEqual",
    "equal": "equals",
    "equals": "equals",
    "notcontain": "notContain",
    "notcontains": "notContain",
    "doesnotcontain": "notContain",
}

# 組立式 (``reportBooleanFilter``) で使える語
_BOOL_FILTER_KEYWORDS: frozenset[str] = frozenset({"AND", "OR", "NOT"})

# 組立式の語 （ 番号 / キーワード / 括弧 ） を拾う正規表現
_BOOL_FILTER_TOKEN_PATTERN = re.compile(
    r"(?P<number>\d+)|(?P<word>[A-Za-z_][A-Za-z_0-9]*)|(?P<open>\()|(?P<close>\))"
)

# 日付の表記。 ゼロ埋めは任意 （ ``2024/1/5`` も受け取る ）
_DATE_PATTERN = re.compile(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})$")

# 文字列リテラルの中の制御文字。 SOQL のリテラルにそのまま入れると
# 構文が壊れるので、 エスケープした文字列に置き換える
_CONTROL_CHAR_ESCAPES: dict[str, str] = {
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}
_CONTROL_CHAR_PATTERN = re.compile(r"[\n\r\t]")

# 数値リテラルとして許容する形 (``-1.5`` / ``100`` など)。 カンマは含まない
# (桁区切りは SOQL 側で分割事故になるので missing に送る) 。
_NUMERIC_PATTERN = re.compile(r"^-?\d+(\.\d+)?$")

# ``reportBooleanFilter`` の番号 (``1`` / ``10``) を 1 件ずつ区別して置換する
# 正規表現。 ``\b1\b`` だけだと ``10`` も ``1`` / ``0`` に分解されてしまうので、
# 必ず ``(?<!\d)`` / ``(?!\d)`` で挟む
_BOOLEAN_FILTER_NUMBER_PATTERN = re.compile(r"(?<!\d)(\d+)(?!\d)")

# SOQL リテラルのエスケープ (単一引用符とバックスラッシュ)
_QUOTE_ESCAPE_PATTERN = re.compile(r"(['\\])")


@dataclass(frozen=True)
class CrossFilterLink:
    """``crossFilters`` の 1 個と、 主オブジェクトと子オブジェクトの繋がりの判定。

    Attributes:
        index: レポートの ``crossFilters`` の中の順番 （ 1 始まり ）。
        status: 判定。 ``直接`` / ``直接でない`` / ``確認できず`` のいずれか。
        primary_field: 主オブジェクト側の項目 API 名。 判定できなかったときは
            空文字。
        child_object: 子オブジェクト名。
        join_field: 子オブジェクト側の結合項目 API 名。
        relationship_name: 子リレーション名。
        converted: 副問い合わせとして SOQL に入れたか。
        reason: 判定の理由。 ``直接`` でも ``converted`` が ``False`` のときは
            副問い合わせにできなかった理由 （ 副問い合わせの上限 など ） 。
    """

    index: int
    status: str
    primary_field: str
    child_object: str
    join_field: str
    relationship_name: str
    converted: bool
    reason: str


def format_cross_filter_link_comment(link: CrossFilterLink, *, main_object: str = "") -> str:
    """クロスフィルタ 1 個のコメント行を組み立てる。

    ``直接`` で SOQL に入れたときは 繋がり （ リレーション名: 子オブジェクト.
    結合項目 → 主側項目 ） を、 入れていないときは 理由 を入れる。
    入れたときの理由 （ 手入力の子リレーションなど ） も有れば 添える。

    Args:
        link: 判定結果。
        main_object: 主オブジェクト名。 分かっているときは主側項目を
            ``<主オブジェクト>.<項目>`` の形で出す。

    Returns:
        コメント行の文字列 （ 改行は含まない ） 。
    """
    if link.status == CROSS_FILTER_LINK_DIRECT and link.converted:
        primary = link.primary_field
        if main_object and primary:
            primary = f"{main_object}.{primary}"
        detail = f"{link.relationship_name or link.child_object}: "
        detail += f"{link.child_object}.{link.join_field} → {primary}"
        text = f"# クロスフィルタ {link.index}: {link.status}（{detail}）SOQL に入れた"
        if link.reason:
            text += f"（{link.reason}）"
        return text
    reason = link.reason or "理由不明"
    return f"# クロスフィルタ {link.index}: {link.status}（{reason}）SOQL に入れていない"


@dataclass(frozen=True)
class SoqlDraft:
    """``build_soql`` の戻り値。 ``frozen`` で再代入しない。

    Attributes:
        soql: 組み立てた SOQL (1 つの文) 。 作れなかったときは空文字。
        status: 状態。 ``作成済み`` / ``要確認`` / ``一部作れず`` / ``作れず``
            のいずれか。
        missing: 作れなかった部分 (1 件 1 要素)。 例: 「列: 商談名
            (Opp.Name) — 対応フィールドなし」 「条件 2: Stage equals
            Closed (省いたので件数が増える可能性)」 。
        checks: 人が実行前に確認すべき事項 (1 件 1 要素)。 例: 「ス
            コープ=MyTerritory: 実行ユーザーで結果が変わる。 OwnerId 等
            の条件を足すか確認」 「日時の条件をタイムゾーン無しの UTC 0 時
            で置き換えた: CloseDate >= 2025-01-01」 。
        count_soql: 件数チェック用に ``SELECT COUNT()`` へ書き換えた SOQL
            (``FROM`` / ``WHERE`` は同じ、 ``ORDER BY`` と ``LIMIT`` は
            付けない )。 ``build_soql`` が ``作れず`` のときは空文字。
            ``build_soql`` と同じ ``select_lines`` / ``where_lines`` から
            組み立てるため、 文字列の切り貼りをしない。
        limit: ``reportMetadata.topRows.rowLimit`` の正の整数。 件数
            チェックで ``min(totalSize, limit)`` を計算するための値。 無い
            / 読めないときは ``None`` 。
        select_items: SELECT 句に入る列のリスト ( ``select_lines`` そのもの) 。
            「似た SOQL」 の判定に使う。
        where_items: WHERE 句に入る条件のリスト ( ``where_lines`` そのもの) 。
            「似た SOQL」 の判定に使う。
        order_by: ``ORDER BY ...`` （ 末尾に ``LIMIT N`` を含む ） の文字列。
            無ければ空文字。 「似た SOQL」 の判定に使う。
        use_bulk: 取り方。 ``True`` = Bulk API 2.0、 ``False`` = REST の
            ``query()`` 。 副問い合わせ （ クロスフィルタ ） を出したとき、
            または件数が 1000 件以下のときに ``False`` になる。 SOQL 文字列を
            後からパースして判定しない。
        bulk_blockers: REST にした理由 （ 1 件 1 要素 ）。 ``use_bulk`` が
            ``True`` のときは空。 理由には件数の根拠（ 管理表かサンドボックスか ）
            を書く。
        cross_filter_links: クロスフィルタ 1 個ずつの繋がりの判定。
            ``crossFilters`` が無ければ空のタプル。
    """

    soql: str
    status: str
    missing: tuple[str, ...]
    checks: tuple[str, ...]
    count_soql: str = field(default="")
    limit: int | None = field(default=None)
    select_items: tuple[str, ...] = field(default=())
    where_items: tuple[str, ...] = field(default=())
    order_by: str = field(default="")
    use_bulk: bool = True
    bulk_blockers: tuple[str, ...] = field(default=())
    cross_filter_links: tuple[CrossFilterLink, ...] = field(default=())


def _escape_string_literal(value: str) -> str:
    """SOQL 文字列リテラル用に ``'`` と ``\\`` をエスケープして ``'...'`` で囲む。

    内部の ``'`` は ``\\'`` に、 ``\\`` は ``\\\\`` に置換する。 改行・復帰・
    タブは SOQL のリテラルにそのまま入れると構文が壊れるので、 ``\\n``
    ``\\r`` ``\\t`` の形に置き換える。
    """
    escaped = _QUOTE_ESCAPE_PATTERN.sub(r"\\\1", value)
    escaped = _CONTROL_CHAR_PATTERN.sub(lambda match: _CONTROL_CHAR_ESCAPES[match.group()], escaped)
    return "'" + escaped + "'"


def _escape_like_value(value: str) -> str:
    """LIKE の中の値をエスケープする。

    先に ``\\`` と ``'`` をエスケープしてから、 改行・復帰・タブを ``\\n``
    ``\\r`` ``\\t`` に、 ``%`` と ``_`` を ``\\%`` ``\\_`` にして文字どおり
    探す。 順序を逆にすると、 値が ``\\%`` のときエスケープした ``\\`` が
    二重になってしまう。
    """
    escaped = _QUOTE_ESCAPE_PATTERN.sub(r"\\\1", value)
    escaped = _CONTROL_CHAR_PATTERN.sub(lambda match: _CONTROL_CHAR_ESCAPES[match.group()], escaped)
    return escaped.replace("%", "\\%").replace("_", "\\_")


def _looks_like_date(value: str) -> bool:
    """``YYYY-MM-DD`` / ``YYYY/MM/DD`` っぽい形なら True。 厳密には検査しない。"""
    return bool(_DATE_PATTERN.fullmatch(value))


def _normalize_date_literal(value: str) -> tuple[str, str | None]:
    """日付リテラルを ``YYYY-MM-DD`` に正規化する。

    ``YYYY/MM/DD`` と ゼロ埋めなし （ ``2024/1/5`` ） を ``YYYY-MM-DD`` に
    直し、 ``datetime.date`` で実在性を検査する。

    Returns:
        ``(normalized, missing_reason or None)`` 。
        表記または実在性が不正なときは ``(value, reason)`` 、 正常時は
        ``(normalized, None)`` 。
    """
    match = _DATE_PATTERN.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        return value, f"日付の表記が不正: {value!r}"
    year, month, day = (int(part) for part in match.groups())
    try:
        normalized = _date(year, month, day).isoformat()
    except ValueError:
        return value, f"実在しない日付: {value!r}"
    return normalized, None


def _is_relative_date_candidate(value: str) -> bool:
    """相対日付リテラル候補 （ 英大文字・数字・_ と :数字 ） かどうか。"""
    return bool(_RELATIVE_DATE_PATTERN.fullmatch(value))


def _is_allowed_relative_date_literal(value: str) -> bool:
    """許可リストにある相対日付リテラルかどうか。"""
    if not _is_relative_date_candidate(value):
        return False
    if value in _RELATIVE_DATE_LITERAL_NAMES:
        return True
    return bool(
        _RELATIVE_DATE_N_PREFIX_PATTERN.fullmatch(value)
        or _RELATIVE_DATE_N_SUFFIX_PATTERN.fullmatch(value)
    )


def _canonical_relative_date_text(value: str) -> str:
    """相対日付の表記を照合用の形 （ 小文字・空白なし・``_`` なし ） にする。

    全角の英数字と全角空白を半角にし、 日本語の 「か月」 のゆれを
    「月」 にそろえる。 大文字小文字と空白・``_`` は区別しない。
    """
    text = value.translate(_RELATIVE_DATE_FULLWIDTH_TABLE)
    for variant in _JA_MONTH_VARIANTS:
        text = text.replace(variant, "月")
    return re.sub(r"\s+", "", text).lower().replace("_", "")


def _build_relative_date_literal(direction: str, unit: str, count: int) -> str | None:
    """方向と単位と数量から SOQL の相対日付リテラルを作る。

    Args:
        direction: ``"last"`` / ``"next"`` / ``"ago"`` 。
        unit: 単位 （ 照合用の形 ） 。
        count: 数量。 1 未満なら ``None`` 。

    Returns:
        SOQL のリテラル。 単位が分からなければ ``None`` 。
    """
    soql_unit = _RELATIVE_DATE_UNITS.get(unit)
    if soql_unit is None:
        return None
    if count < 1:
        return None
    if direction == "last":
        return f"LAST_N_{soql_unit}:{count}"
    if direction == "next":
        return f"NEXT_N_{soql_unit}:{count}"
    return f"N_{soql_unit}_AGO:{count}"


def _match_relative_date_pattern(canonical: str) -> str | None:
    """照合用の形から相対日付リテラルを作る。 一致しなければ ``None`` 。"""
    for pattern, direction in _RELATIVE_DATE_PATTERNS:
        match = pattern.fullmatch(canonical)
        if match is None:
            continue
        literal = _build_relative_date_literal(
            direction,
            match.group("unit"),
            int(match.group("count")),
        )
        if literal is not None:
            return literal
    return None


def _normalize_relative_date(value: str) -> str | None:
    """相対日付の表記を SOQL のリテラルに正規化する。 解釈できなければ ``None`` 。

    前後の空白・全角空白・全角数字・英字の大文字小文字・空白と ``_`` の
    ゆれを吸収し、 出力は許可リスト （ ``_is_allowed_relative_date_literal`` ）
    にあるリテラルだけにする。 既にリテラルならそのまま返す。

    Args:
        value: レポート上の値 (文字列) 。

    Returns:
        SOQL のリテラル。 相対日付として解釈できなければ ``None`` 。
    """
    stripped = value.strip()
    if _is_allowed_relative_date_literal(stripped):
        return stripped
    canonical = _canonical_relative_date_text(stripped)
    if not canonical:
        return None
    literal = _RELATIVE_DATE_FIXED_LITERALS.get(canonical)
    if literal is None:
        literal = _match_relative_date_pattern(canonical)
    if literal is None:
        return None
    if not _is_allowed_relative_date_literal(literal):
        return None
    return literal


def _relative_date_checks(raw_value: str, literal: str) -> list[str]:
    """相対日付を変換したときの checks メッセージ。 変換していなければ空。"""
    if literal == raw_value.strip():
        return []
    return [
        f"相対日付を変換した: {raw_value} → {literal}（境界はレポートと SOQL で異なる場合あり）"
    ]


def _next_n_days_count(literal: str) -> int | None:
    """リテラルが 「翌 N 日」 （ ``NEXT_N_DAYS:n`` ） なら n を返す。 違えば ``None`` 。"""
    match = _NEXT_N_DAYS_LITERAL_PATTERN.fullmatch(literal)
    if match is None:
        return None
    return int(match.group("days"))


def _is_next_n_days(literal: str) -> bool:
    """リテラルが 「翌 N 日」 （ ``NEXT_N_DAYS:n`` ） かどうか。 日単位だけ。"""
    return _next_n_days_count(literal) is not None


def _build_relative_date_condition(
    field_api: str,
    literal: str,
    op: str,
) -> tuple[str, list[str]]:
    """相対日付リテラルを等号・不等号の条件式にする。

    「翌 N 日」 （ ``NEXT_N_DAYS:n`` ） はレポートでは当日を含めて n 日
    （ 当日 〜 n-1 日後 ） である。 SOQL の ``NEXT_N_DAYS:n`` は翌日から n 日
    なので、 当日を表す ``TODAY`` を足した形にする。 n が 2 以上のときは
    ``( f = TODAY OR f = NEXT_N_DAYS:n-1 )`` 、 n が 1 のときは当日だけなので
    ``f = TODAY`` （ 括弧なし ） にする。 それ以外のリテラルはそのまま
    ``f = リテラル`` にする。

    Args:
        field_api: SOQL 上の項目名。
        literal: 正規化済みの相対日付リテラル。
        op: ``equals`` / ``notEqual`` 。 等号か不等号かを決める。

    Returns:
        ``(条件式文字列, 追加 checks)`` のタプル。
    """
    symbol = "=" if op == "equals" else "!="
    days = _next_n_days_count(literal)
    if days is None:
        return f"{field_api} {symbol} {literal}", []
    if days <= 1:
        # 当日だけなので、 括弧は付けない
        condition = f"{field_api} {symbol} TODAY"
        return condition, [
            f"翌 {days} 日は当日を含めて {days} 日なので TODAY だけにした: {field_api}"
        ]
    rest = f"NEXT_N_DAYS:{days - 1}"
    joiner = " OR " if op == "equals" else " AND "
    condition = f"( {field_api} {symbol} TODAY{joiner}{field_api} {symbol} {rest} )"
    return condition, [
        f"翌 {days} 日は当日を含めて {days} 日なので TODAY と {rest} にした: {field_api}"
    ]


def _format_value_for_field(
    value: str,
    field_type: str,
    field_name: str,
    operator: str = "",
) -> tuple[str, list[str], list[str]]:
    """``column_map`` の ``型`` に応じて SOQL の値リテラルを書く。

    Args:
        value: レポート上の値 (文字列) 。
        field_type: ``column_map.型`` の値 (小文字化なし、 そのまま) 。
        field_name: SOQL 上の項目名 (エラーメッセージ用) 。
        operator: 演算子名。 日時項目の境界 （ 0 時 / 終端 ） を決めるのに使う。

    Returns:
        ``(リテラル文字列, 追加 missing, 追加 checks)`` のタプル。
        型が分からない、 もしくは日付/日時/数値の値が解釈できないときは
        ``""`` と共に ``missing`` 側に理由を入れて返し、 呼び出し側で
        さらに「演算子 N の値が解釈できません: ...」 を足す。
        解釈できたが注意点がある (日時の UTC 化など) ときは
        ``checks`` 側にメッセージを入れて返す。
    """
    field_type_lower = (field_type or "").strip().lower()
    # 空値
    if value == "":
        return "", [], []

    if field_type_lower in _STRING_TYPES:
        return _format_string_value(value)
    if field_type_lower in _BOOLEAN_TYPES:
        return _format_boolean_value(value, field_name)
    if field_type_lower in _NUMERIC_TYPES:
        return _format_numeric_value(value, field_name)
    if field_type_lower in _DATE_TYPES:
        return _format_date_value(value, field_name)
    if field_type_lower in _DATETIME_TYPES:
        return _format_datetime_value(value, field_name, operator)

    # 型が分からない
    return "", [f"値の解釈不可 ({field_name}: 型 {field_type!r} が未知): missing"], []


def _format_string_value(value: str) -> tuple[str, list[str], list[str]]:
    """文字列系の値を ``'...'`` で囲んだ SOQL リテラルにする。"""
    return _escape_string_literal(value), [], []


def _format_boolean_value(value: str, field_name: str) -> tuple[str, list[str], list[str]]:
    """boolean 系の値を ``true`` / ``false`` に解釈する。 解釈できなければ missing。"""
    lowered = value.strip().lower()
    if lowered in ("true", "1", "yes"):
        return "true", [], []
    if lowered in ("false", "0", "no"):
        return "false", [], []
    return (
        "",
        [f"値の解釈不可 ({field_name}: boolean として解釈できない {value!r}): missing"],
        [],
    )


def _format_numeric_value(value: str, field_name: str) -> tuple[str, list[str], list[str]]:
    """数値系の値をそのままの表記で返す。 カンマを含む値や数値として解釈できない値は missing。"""
    if _NUMERIC_PATTERN.fullmatch(value):
        return value, [], []
    if "," in value:
        return (
            "",
            [f"値の解釈不可 ({field_name}: 数値の値にカンマが含まれる可能性 {value!r}): missing"],
            [],
        )
    return "", [f"値の解釈不可 ({field_name}: 数値として解釈できない {value!r}): missing"], []


def _format_date_value(value: str, field_name: str) -> tuple[str, list[str], list[str]]:
    """date 系の値を ``YYYY-MM-DD`` または相対日付リテラルにして返す。 解釈できなければ missing。"""
    # 相対日付 （ 表記ゆれを含む ） は SOQL のリテラルに直す
    literal = _normalize_relative_date(value)
    if literal is not None:
        return literal, [], _relative_date_checks(value, literal)
    # 相対日付リテラル候補 （ 許可リストに無い語 ） は missing
    if _is_relative_date_candidate(value):
        return (
            "",
            [f"値の解釈不可 ({field_name}: 相対日付リテラルが未対応 {value!r}): missing"],
            [],
        )
    # 日付として解釈できる形なら、 ``YYYY-MM-DD`` に正規化して返す
    # (実在しない日付・ ``YYYY/MM/DD`` 表記揺れを吸収)
    if _looks_like_date(value):
        normalized, missing_msg = _normalize_date_literal(value)
        if missing_msg:
            return (
                "",
                [f"値の解釈不可 ({field_name}: date として解釈できない {value!r}): missing"],
                [],
            )
        return normalized, [], []
    return (
        "",
        [f"値の解釈不可 ({field_name}: date として解釈できない {value!r}): missing"],
        [],
    )


def _datetime_utc_check(field_name: str, raw_value: str, literal: str, boundary: str) -> str:
    """日時の値を UTC 境界に置き換えたときの checks メッセージ。"""
    return (
        f"日時の条件をタイムゾーン無しの {boundary}で置き換えた: "
        f"{field_name} の {raw_value!r} を {literal} に"
    )


def _format_datetime_value(
    value: str,
    field_name: str,
    operator: str = "",
) -> tuple[str, list[str], list[str]]:
    """datetime 系の値を ``YYYY-MM-DDT00:00:00Z`` または相対日付リテラルにして返す。

    日付だけの値は UTC 境界に拡張して checks にメッセージを足す。 境界は
    演算子で決める（ ``<=`` と ``>`` は日の終端、 それ以外は 0 時 ） 。
    解釈できなければ missing。
    """
    # 相対日付 （ 表記ゆれを含む ） は SOQL のリテラルに直す
    literal = _normalize_relative_date(value)
    if literal is not None:
        return literal, [], _relative_date_checks(value, literal)
    # 相対日付リテラル候補 （ 許可リストに無い語 ） は missing
    if _is_relative_date_candidate(value):
        return (
            "",
            [f"値の解釈不可 ({field_name}: 相対日付リテラルが未対応 {value!r}): missing"],
            [],
        )
    # 日付だけの値なら UTC 境界に拡張して checks 追加
    # (``YYYY/MM/DD`` 表記も ``-`` に正規化してから時刻を付ける)
    if _looks_like_date(value):
        normalized, missing_msg = _normalize_date_literal(value)
        if missing_msg:
            return (
                "",
                [f"値の解釈不可 ({field_name}: datetime として解釈できない {value!r}): missing"],
                [],
            )
        if operator in ("lessOrEqual", "greaterThan"):
            literal = f"{normalized}T23:59:59Z"
            boundary = "UTC 終端"
        else:
            literal = f"{normalized}T00:00:00Z"
            boundary = "UTC 0 時"
        return literal, [], [_datetime_utc_check(field_name, value, literal, boundary)]
    return (
        "",
        [f"値の解釈不可 ({field_name}: datetime として解釈できない {value!r}): missing"],
        [],
    )


def _split_values(raw: object) -> list[str]:
    """``reportFilters[].value`` をカンマで分割する。

    レポートの値表現は数値でも ``"1,2"`` のように文字列で来ることがあるため、
    まずは文字列化して扱う。 引用符で囲んだ値の中にカンマがある
    （ ``"a,b",c`` ） ときは 2 値として扱う。
    """
    text = _coerce_str(raw)
    if not text:
        return []
    try:
        rows = list(csv.reader([text], skipinitialspace=True))
    except csv.Error:
        # 引用符が閉じていない等の壊れた値は、 従来どおりカンマで切る
        return [part.strip() for part in text.split(",")]
    if not rows:
        return []
    return [part.strip() for part in rows[0]]


def _build_column_lookup(column_map: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    """``column_map`` を ``列キー -> 行 dict`` の dict にする。同じ列キーは先頭を採用。"""
    lookup: dict[str, dict[str, str]] = {}
    for entry in column_map:
        lookup.setdefault(entry["列キー"], entry)
    return lookup


def _build_equality_condition(
    op: str,
    values: list[str],
    field_api: str,
    field_type: str,
) -> tuple[str, list[str], list[str]]:
    """``equals`` / ``notEqual`` を ``=`` / ``!=`` または ``IN`` / ``NOT IN`` に組み立てる。"""
    field_type_lower = field_type.strip().lower()
    if field_type_lower in _DATETIME_TYPES:
        return _build_datetime_equality_condition(op, values, field_api)
    if field_type_lower in _DATE_TYPES:
        return _build_date_equality_condition(op, values, field_api)
    missing: list[str] = []
    checks: list[str] = []
    parts: list[str] = []
    for value in values:
        literal, extra_missing, extra_checks = _format_value_for_field(value, field_type, field_api)
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
        if field_type_lower in _NO_IN_TYPES:
            joined = " OR ".join(f"{field_api} = {part}" for part in parts)
            return f"( {joined} )", missing, checks
        return f"{field_api} IN ({', '.join(parts)})", missing, checks
    if len(parts) == 1:
        return f"{field_api} != {parts[0]}", missing, checks
    if field_type_lower in _NO_IN_TYPES:
        joined = " AND ".join(f"{field_api} != {part}" for part in parts)
        return f"( {joined} )", missing, checks
    return f"{field_api} NOT IN ({', '.join(parts)})", missing, checks


def _build_date_equality_condition(
    op: str,
    values: list[str],
    field_api: str,
) -> tuple[str, list[str], list[str]]:
    """date 項目の ``equals`` / ``notEqual`` を組み立てる。

    「翌 N 日」 （ ``NEXT_N_DAYS:n`` ） を含むときは、 当日を表す ``TODAY``
    を足した式 （ 当日を含めて n 日なら ``NEXT_N_DAYS:n-1`` も足す ） にして、
    他の値と OR / AND で並べる（ 等号なら OR、 不等号なら AND ） 。 「翌 N 日」
    が無いときは従来どおり ``IN`` / ``NOT IN`` にする。
    """
    missing: list[str] = []
    checks: list[str] = []
    # (「翌 N 日」で展開したか, リテラルまたは条件式)
    terms: list[tuple[bool, str]] = []
    for value in values:
        literal, extra_missing, extra_checks = _format_value_for_field(value, "date", field_api)
        if not literal:
            missing.append(f"演算子 {op} の値が解釈できません: {value!r}")
            missing.extend(extra_missing)
            continue
        checks.extend(extra_checks)
        if _is_next_n_days(literal):
            condition, next_n_days_checks = _build_relative_date_condition(field_api, literal, op)
            checks.extend(next_n_days_checks)
            terms.append((True, condition))
        else:
            terms.append((False, literal))
    if not terms:
        return "", missing, checks
    if not any(expanded for expanded, _text in terms):
        # 「翌 N 日」 が無いので従来どおり IN / NOT IN にする
        literals = [text for _expanded, text in terms]
        if op == "equals":
            if len(literals) == 1:
                return f"{field_api} = {literals[0]}", missing, checks
            return f"{field_api} IN ({', '.join(literals)})", missing, checks
        if len(literals) == 1:
            return f"{field_api} != {literals[0]}", missing, checks
        return f"{field_api} NOT IN ({', '.join(literals)})", missing, checks
    # 「翌 N 日」 があるので OR / AND で並べる
    symbol = "=" if op == "equals" else "!="
    joiner = " OR " if op == "equals" else " AND "
    parts = [text if expanded else f"{field_api} {symbol} {text}" for expanded, text in terms]
    if len(parts) == 1:
        return parts[0], missing, checks
    return f"( {joiner.join(parts)} )", missing, checks


def _build_datetime_equality_condition(
    op: str,
    values: list[str],
    field_api: str,
) -> tuple[str, list[str], list[str]]:
    """datetime 項目の ``equals`` / ``notEqual`` を組み立てる。

    日付だけの値は 1 日の区間として扱う（ 開始は 0 時、 終了は 23:59:59 ） 。
    複数値は equals なら OR、 notEqual なら AND でつなぐ。 相対日付リテラルは
    そのまま書く。
    """
    missing: list[str] = []
    checks: list[str] = []
    parts: list[str] = []
    for value in values:
        part, extra_missing, extra_checks = _datetime_equality_part(op, value, field_api)
        if extra_missing:
            missing.extend(extra_missing)
        if extra_checks:
            checks.extend(extra_checks)
        if part:
            parts.append(part)
    if not parts:
        return "", missing, checks
    if len(parts) == 1:
        return parts[0], missing, checks
    joiner = " OR " if op == "equals" else " AND "
    return f"( {joiner.join(parts)} )", missing, checks


def _datetime_equality_part(
    op: str,
    value: str,
    field_api: str,
) -> tuple[str, list[str], list[str]]:
    """datetime の ``equals`` / ``notEqual`` の 1 値ぶんの式。"""
    literal = _normalize_relative_date(value)
    if literal is not None:
        condition, extra_checks = _build_relative_date_condition(field_api, literal, op)
        return condition, [], _relative_date_checks(value, literal) + extra_checks
    if _is_relative_date_candidate(value):
        return (
            "",
            [f"値の解釈不可 ({field_api}: 相対日付リテラルが未対応 {value!r}): missing"],
            [],
        )
    normalized, missing_msg = _normalize_date_literal(value)
    if missing_msg:
        return (
            "",
            [f"値の解釈不可 ({field_api}: datetime として解釈できない {value!r}): missing"],
            [],
        )
    start = f"{normalized}T00:00:00Z"
    end = f"{normalized}T23:59:59Z"
    checks = [_datetime_utc_check(field_api, value, f"{start} 〜 {end}", "UTC 0 時と終端")]
    if op == "equals":
        return f"( {field_api} >= {start} AND {field_api} <= {end} )", [], checks
    return f"( {field_api} < {start} OR {field_api} > {end} )", [], checks


def _build_comparison_condition(
    op: str,
    values: list[str],
    field_api: str,
    field_type: str,
) -> tuple[str, list[str], list[str]]:
    """``lessThan`` / ``greaterThan`` / ``lessOrEqual`` / ``greaterOrEqual`` を組み立てる。"""
    missing: list[str] = []
    checks: list[str] = []
    if len(values) != 1:
        missing.append(f"演算子 {op} は単一値のみ対応 (値: {values!r})")
        return "", missing, checks
    if field_type.strip().lower() in _NO_COMPARISON_TYPES:
        missing.append(f"演算子 {op} は数値・日付の項目でのみ使用可 (型: {field_type!r})")
        return "", missing, checks
    literal, extra_missing, extra_checks = _format_value_for_field(
        values[0], field_type, field_api, op
    )
    if not literal:
        missing.append(f"演算子 {op} の値が解釈できません: {values[0]!r}")
        missing.extend(extra_missing)
        return "", missing, checks
    checks.extend(extra_checks)
    symbol = _COMPARISON_OP_SYMBOLS[op]
    return f"{field_api} {symbol} {literal}", missing, checks


_COMPARISON_OP_SYMBOLS: dict[str, str] = {
    "lessThan": "<",
    "greaterThan": ">",
    "lessOrEqual": "<=",
    "greaterOrEqual": ">=",
}


def _build_like_condition(
    op: str,
    values: list[str],
    field_api: str,
    field_type: str,
) -> tuple[str, list[str], list[str]]:
    """``contains`` / ``notContain`` / ``startsWith`` を組み立てる。

    全て LIKE 系で、 文字列系の項目でのみ使える。 ``id`` / ``reference`` /
    ``multipicklist`` は LIKE では絞れないので missing にする。 値が無い、
    または型が LIKE で絞れないときは missing。

    値の中の ``%`` と ``_`` はワイルドカードではなく文字そのものとして
    探すので、 ``\\%`` ``\\_`` にエスケープする。 値に ``%`` があっても
    ワイルドカードにはしないので、 checks は出さない。 改行・復帰・タブは
    ``\\n`` ``\\r`` ``\\t`` の形にして、 リテラルが壊れないようにする。
    """
    missing: list[str] = []
    checks: list[str] = []
    if not values:
        missing.append(f"演算子 {op} の値が無い")
        return "", missing, checks
    if field_type.strip().lower() not in _LIKE_TYPES:
        missing.append(f"演算子 {op} は文字列系の項目でのみ使用可 (型: {field_type!r})")
        return "", missing, checks
    pieces: list[tuple[str, str]] = []  # (元の値, エスケープ済みの値)
    for value in values:
        pieces.append((value, _escape_like_value(value)))
    return _format_like_expression(op, pieces, field_api, missing, checks)


def _format_like_expression(
    op: str,
    pieces: list[tuple[str, str]],
    field_api: str,
    missing: list[str],
    checks: list[str],
) -> tuple[str, list[str], list[str]]:
    """組み立て済みの ``(元の値, エスケープ済みの値)`` 群から SOQL の LIKE 式にする。"""
    if op == "startsWith":
        # 前方一致なので、 値の後ろにだけワイルドカードを置く
        parts = [f"{field_api} LIKE '{escaped}%'" for _value, escaped in pieces]
        if len(parts) == 1:
            return parts[0], missing, checks
        return f"( {' OR '.join(parts)} )", missing, checks
    if op == "contains":
        # 前後にワイルドカードを置く
        parts = [f"{field_api} LIKE '%{escaped}%'" for _value, escaped in pieces]
        if len(parts) == 1:
            return parts[0], missing, checks
        return f"( {' OR '.join(parts)} )", missing, checks
    # notContain は NOT (X LIKE '%v%') を AND で並べる
    parts = [f"NOT ({field_api} LIKE '%{escaped}%')" for _value, escaped in pieces]
    return f"( {' AND '.join(parts)} )", missing, checks


def _build_picklist_inclusion_condition(
    op: str,
    values: list[str],
    field_api: str,
    field_type: str,
) -> tuple[str, list[str], list[str]]:
    """``includes`` / ``excludes`` を組み立てる。

    multipicklist は ``INCLUDES`` / ``EXCLUDES`` のまま。 picklist は
    ``IN`` / ``NOT IN`` （ 単一値は ``=`` / ``!=`` ） にする。
    """
    missing: list[str] = []
    checks: list[str] = []
    field_type_lower = field_type.strip().lower()
    if field_type_lower not in ("multipicklist", "picklist"):
        missing.append(f"演算子 {op} は multipicklist / picklist のみで使用可 (型: {field_type!r})")
        return "", missing, checks
    if not values:
        missing.append(f"演算子 {op} の値が無い")
        return "", missing, checks
    value_literals = ", ".join(_escape_string_literal(v) for v in values)
    if field_type_lower == "multipicklist":
        if op == "includes":
            return f"{field_api} INCLUDES ({value_literals})", missing, checks
        return f"{field_api} EXCLUDES ({value_literals})", missing, checks
    if op == "includes":
        if len(values) == 1:
            return f"{field_api} = {value_literals}", missing, checks
        return f"{field_api} IN ({value_literals})", missing, checks
    if len(values) == 1:
        return f"{field_api} != {value_literals}", missing, checks
    return f"{field_api} NOT IN ({value_literals})", missing, checks


# 演算子ごとのディスパッチ表。 ``_build_filter_condition`` から 1 度だけ
# 引いて、 各演算子の組み立ては専用関数に任せる。 ``equals`` / ``notEqual``
# の空値 (``null``) への置換は本体側で先に済ませてあるので、 各関数では
# 通常パスだけを扱う
_FILTER_OP_HANDLERS: dict[
    str,
    Callable[[str, list[str], str, str], tuple[str, list[str], list[str]]],
] = {
    "equals": _build_equality_condition,
    "notEqual": _build_equality_condition,
    "lessThan": _build_comparison_condition,
    "greaterThan": _build_comparison_condition,
    "lessOrEqual": _build_comparison_condition,
    "greaterOrEqual": _build_comparison_condition,
    "contains": _build_like_condition,
    "notContain": _build_like_condition,
    "startsWith": _build_like_condition,
    "includes": _build_picklist_inclusion_condition,
    "excludes": _build_picklist_inclusion_condition,
}


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
    op = _normalize_operator(operator)
    # 空値のときは ``equals`` / ``notEqual`` だけ ``null`` 比較に変換する
    if not values and op in ("equals", "notEqual"):
        symbol = "=" if op == "equals" else "!="
        return f"{field_api} {symbol} null", [], []

    handler = _FILTER_OP_HANDLERS.get(op)
    if handler is None:
        return "", [f"未対応の演算子: {op!r}"], []
    condition, extra_missing, extra_checks = handler(op, values, field_api, field_type)
    if condition and field_type.strip().lower() == "textarea":
        extra_checks.append(f"ロングテキストは絞り込めない場合がある: {field_api}")
    return condition, extra_missing, extra_checks


def _normalize_operator(operator: str) -> str:
    """演算子名を正規名へ寄せる。 大文字小文字は区別しない。

    レポート側の表記ゆれ （ ``lessorequal`` と ``lessOrEqual`` など ） を
    同じ扱いにする。 対応表に無い演算子はそのまま返す（ 未対応として
    missing に入る）。
    """
    stripped = operator.strip()
    return _OPERATOR_ALIASES.get(stripped.lower(), stripped)


def _build_where_clause(
    metadata: dict[str, Any],
    column_lookup: dict[str, dict[str, str]],
    *,
    judgements: list[_CrossFilterJudgement],
    allow_cross_filter_conversion: bool = True,
) -> tuple[list[str], list[str], list[str], bool, list[CrossFilterLink]]:
    """``reportFilters`` / ``standardDateFilter`` / ``standardFilters`` /
    ``crossFilters`` から WHERE 句を組み立てる。

    ``reportFilters`` の条件は **1 始まり番号 → 条件式** の対応で持ち、
    組立式 (``reportBooleanFilter``) はその番号で ``(条件式)`` に
    置き換える。 日付条件 (``standardDateFilter``) は **別管理** にして、
    組立式の結果 (全体を 1 行として扱う) と AND で並べる。 組立式が
    無いときは ``reportFilters`` の各条件と日付条件を AND でつなぐ。

    組立式の **参照番号** が ``reportFilters`` のどれかに対応しない
    とき (その条件が ``column_map`` に無く作れなかった等) 、 式が壊れる
    ため WHERE は空にして missing に「組立式の条件を作れない」 を入れる。
    クロスフィルタや日付条件の ``missing`` だけでは WHERE 全体は捨てない。

    クロスフィルタは副問い合わせ （ ``IN`` / ``NOT IN`` ） にして 1 行ずつ
    足す。 作れないものは ``missing`` にして、 従来どおり WHERE 全体は捨てない。
    ``allow_cross_filter_conversion`` が False のときは、 判定だけ残して
    副問い合わせにはしない （ FROM を子に切り替えたとき ） 。

    Returns:
        ``(top-level lines, missing, 追加 checks, 副問い合わせを使ったか,
        判定のリスト)`` 。
        lines は 1 行 1 条件を表す文字列のリスト ( 組立式が 1 行にまとまる
        場合は 1 要素、 通常は複数要素 )。 WHERE が無いときは空リスト。
    """
    missing: list[str] = []
    checks: list[str] = []
    filter_conditions = _collect_report_filter_conditions(metadata, column_lookup, missing, checks)
    date_lines = _collect_standard_date_lines(metadata, column_lookup, missing, checks)
    _collect_standard_filter_checks(metadata, checks)
    cross_filter_lines, links = _build_cross_filter_lines(
        judgements,
        missing=missing,
        checks=checks,
        allow_conversion=allow_cross_filter_conversion,
    )
    lines, ok = _combine_filter_conditions(
        metadata,
        filter_conditions,
        missing,
        bool(date_lines) or bool(cross_filter_lines),
    )
    if not ok:
        # WHERE 全体を出さないので、 副問い合わせも SOQL には入らない
        links = [_mark_not_converted(link, BROKEN_BOOLEAN_FILTER_REASON) for link in links]
        return [], missing, checks, False, links
    lines.extend(date_lines)
    lines.extend(cross_filter_lines)
    return lines, missing, checks, bool(cross_filter_lines), links


def _collect_report_filter_conditions(
    metadata: dict[str, Any],
    column_lookup: dict[str, dict[str, str]],
    missing: list[str],
    checks: list[str],
) -> dict[int, str]:
    """``reportFilters`` を ``{1 始まりの番号: 条件式}`` に組み立てる。"""
    filter_conditions: dict[int, str] = {}
    for index, entry in enumerate(metadata.get("reportFilters", []), start=1):
        column = entry.get("column", "")
        if not column:
            missing.append(f"条件 {index}: 列キーが無い")
            continue
        column_info = column_lookup.get(column)
        if not column_info or column_info["対応フィールドAPI名"] in ("", "(不明)"):
            missing.append(f"条件 {index}: {column} (省いたので件数が増える可能性)")
            continue
        values = _extract_filter_values(column_info, index, entry, missing)
        if values is None:
            continue
        operator = entry.get("operator", "")
        field_api = column_info["対応フィールドAPI名"]
        field_type = column_info["型"]
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
    return filter_conditions


def _extract_filter_values(
    column_info: dict[str, str],
    index: int,
    entry: dict[str, Any],
    missing: list[str],
) -> list[str] | None:
    """``reportFilters`` 1 件の ``value`` を、 型に合わせて ``values`` リストにする。

    数値系のときは ``1,000`` のような桁区切りを missing に送って ``None`` を返す
    (カンマで分割すると ``IN`` の要素が壊れるため) 。 それ以外はカンマで分割する。
    """
    field_type = column_info["型"]
    raw_value = _coerce_str(entry.get("value"))
    if field_type.strip().lower() in _NUMERIC_TYPES:
        if raw_value and "," in raw_value:
            missing.append(f"条件 {index}: 数値の値にカンマが含まれる可能性 {raw_value!r}: missing")
            return None
        return [raw_value] if raw_value else []
    return _split_values(raw_value)


def _collect_standard_date_lines(
    metadata: dict[str, Any],
    column_lookup: dict[str, dict[str, str]],
    missing: list[str],
    checks: list[str],
) -> list[str]:
    """``standardDateFilter`` を SOQL の日付条件式 (1 行) にして返す。

    ``CUSTOM`` なら start / end を ``>=`` / ``<=`` に展開、 相対日付リテラル
    ならそのまま ``= durationValue`` にする。 全期間は空リスト。
    """
    date_filter = metadata.get("standardDateFilter")
    if date_filter is None:
        return []
    column = date_filter.get("column", "")
    duration = date_filter.get("durationValue", "")
    start = date_filter.get("startDate", "")
    end = date_filter.get("endDate", "")
    if not column:
        missing.append("日付条件の列が指定されていない")
        return []
    column_info = column_lookup.get(column)
    if not column_info or column_info["対応フィールドAPI名"] in ("", "(不明)"):
        missing.append(f"日付条件の列 {column} に対応が無い (省いた)")
        return []
    field_api = column_info["対応フィールドAPI名"]
    field_type = column_info["型"].strip().lower()
    if duration == "CUSTOM":
        return _custom_date_range_lines(field_api, field_type, start, end, missing, checks)
    if duration:
        return _relative_date_line(field_api, duration, missing, checks)
    # 全期間: 何もしない
    return []


def _custom_date_range_lines(
    field_api: str,
    field_type: str,
    start: str,
    end: str,
    missing: list[str],
    checks: list[str],
) -> list[str]:
    """``CUSTOM`` の日付範囲を ``field >= start AND field <= end`` の 1 行にする。"""
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
    if not date_parts:
        return []
    return [" AND ".join(date_parts)]


def _relative_date_line(
    field_api: str,
    duration: str,
    missing: list[str],
    checks: list[str],
) -> list[str]:
    """相対日付リテラルを ``field = duration`` の 1 行にする。 解釈できなければ missing。"""
    literal = _normalize_relative_date(duration)
    if literal is not None:
        checks.extend(_relative_date_checks(duration, literal))
        condition, extra_checks = _build_relative_date_condition(field_api, literal, "equals")
        checks.extend(extra_checks)
        return [condition]
    missing.append(f"日付条件の durationValue {duration!r} を解釈できない")
    return []


def _collect_standard_filter_checks(
    metadata: dict[str, Any],
    checks: list[str],
) -> None:
    """``standardFilters`` を ``checks`` に並べる (汎用には訳せない) 。"""
    standard_filters = metadata.get("standardFilters")
    if not (isinstance(standard_filters, list) and standard_filters):
        return
    for index, sf in enumerate(standard_filters, start=1):
        checks.append(f"標準フィルタ {index}: {sf!r} (そのままでは SOQL に翻訳できない)")


@dataclass
class _CrossFilterJudgement:
    """クロスフィルタ 1 個の判定結果と、 副問い合わせの組み立てに使う材料。

    ``link`` は ``frozen`` なので、 ``converted`` を変えるときは
    ``dataclasses.replace`` を使う。
    """

    index: int
    cf: Any
    link: CrossFilterLink
    primary_name: str = ""
    join_name: str = ""
    child_name: str = ""
    child_describe: dict[str, Any] | None = None


def _judge_cross_filters(
    metadata: dict[str, Any],
    column_lookup: dict[str, dict[str, str]],
    *,
    main_object: str,
    main_describe: dict[str, Any] | None,
    cross_filter_objects: dict[str, dict[str, Any]] | None,
) -> list[_CrossFilterJudgement]:
    """``crossFilters`` を 1 件ずつ判定する。 判定は変換の前後を問わない。"""
    cross_filters = metadata.get("crossFilters")
    if not isinstance(cross_filters, list):
        return []
    return [
        _judge_cross_filter(
            index,
            cf,
            main_object=main_object,
            main_describe=main_describe,
            column_lookup=column_lookup,
            cross_filter_objects=cross_filter_objects,
        )
        for index, cf in enumerate(cross_filters, start=1)
    ]


def _judge_cross_filter(
    index: int,
    cf: Any,
    *,
    main_object: str,
    main_describe: dict[str, Any] | None,
    column_lookup: dict[str, dict[str, str]],
    cross_filter_objects: dict[str, dict[str, Any]] | None,
) -> _CrossFilterJudgement:
    """クロスフィルタ 1 個を判定する。

    判定は次の 3 種:

    - ``直接`` … 主側の項目が ``id`` 型で、 その子リレーションの参照項目が
      結合項目と一致し、 結合項目が子オブジェクトの項目に解決できる
    - ``直接でない`` … 情報はそろっているが直接つながっていない
    - ``確認できず`` … 情報が足りないので確認できない

    Returns:
        ``_CrossFilterJudgement`` 。 ``直接`` のときだけ副問い合わせの材料
        （ 解決済みの項目名・子の describe ） が入る。
    """

    def judged(
        status: str,
        reason: str,
        *,
        primary: str = "",
        child: str = "",
        join: str = "",
        relationship: str = "",
    ) -> _CrossFilterJudgement:
        return _CrossFilterJudgement(
            index=index,
            cf=cf,
            link=CrossFilterLink(
                index=index,
                status=status,
                primary_field=primary,
                child_object=child,
                join_field=join,
                relationship_name=relationship,
                converted=False,
                reason=reason,
            ),
        )

    if not isinstance(cf, dict):
        return judged(CROSS_FILTER_LINK_UNVERIFIED, "crossFilter が dict でない")

    # 主側の項目
    primary_raw = _coerce_str(cf.get("primaryEntityField")).strip()
    if not primary_raw:
        return judged(CROSS_FILTER_LINK_UNVERIFIED, "primaryEntityField が無い")
    primary = _resolve_primary_field(
        primary_raw,
        main_object=main_object,
        main_describe=main_describe,
        column_lookup=column_lookup,
    )
    if isinstance(primary, str):
        return judged(CROSS_FILTER_LINK_UNVERIFIED, primary)
    primary_name, primary_type = primary
    if primary_type != "id":
        return judged(
            CROSS_FILTER_LINK_NOT_DIRECT,
            f"主側の項目 {primary_name} の型が id でない（型: {primary_type}）",
            primary=primary_name,
        )

    # 子リレーション
    child_relationships = _describe_child_relationships(main_describe)
    if not child_relationships:
        return judged(
            CROSS_FILTER_LINK_UNVERIFIED,
            "主オブジェクトの childRelationships が空",
            primary=primary_name,
        )

    related_raw = _coerce_str(cf.get("relatedEntity")).strip()
    if not related_raw:
        return judged(
            CROSS_FILTER_LINK_UNVERIFIED,
            "relatedEntity が無い",
            primary=primary_name,
        )

    related_relationships = [
        relationship
        for relationship in child_relationships
        if _relationship_matches_entity(relationship, related_raw)
    ]
    if not related_relationships:
        return judged(
            CROSS_FILTER_LINK_NOT_DIRECT,
            f"relatedEntity {related_raw!r} が主オブジェクトの子リレーションに無い",
            primary=primary_name,
        )

    # 結合項目。 同じ子オブジェクトへ参照が 2 本あることがあるので、
    # 結合項目が一致するリレーションを 1 本選ぶ
    join_raw = _coerce_str(cf.get("relatedEntityJoinField")).strip()
    if not join_raw:
        return judged(
            CROSS_FILTER_LINK_UNVERIFIED,
            "relatedEntityJoinField が無い",
            primary=primary_name,
        )
    matched = [
        relationship
        for relationship in related_relationships
        if _coerce_str(relationship.get("field")).strip().lower() == join_raw.lower()
    ]
    if not matched:
        actual = "、".join(
            _coerce_str(relationship.get("field")) for relationship in related_relationships
        )
        return judged(
            CROSS_FILTER_LINK_NOT_DIRECT,
            f"結合項目 {join_raw!r} は {related_raw} の"
            f"リレーションの参照項目（{actual}）と一致しない",
            primary=primary_name,
        )
    relationship = matched[0]
    relationship_name = _coerce_str(relationship.get("relationshipName"))
    child_name = _coerce_str(relationship.get("childSObject"))
    # 手入力 （ ``input/子リレーション.csv`` ） の子リレーションかどうか
    is_manual = bool(relationship.get("手入力"))

    # 子オブジェクトの describe と、 結合項目の解決
    child_describe = _find_cross_filter_object(cross_filter_objects, child_name)
    if child_describe is None:
        return _judge_manual_child_relationship(
            judged,
            cf,
            is_manual=is_manual,
            related_raw=related_raw,
            primary_name=primary_name,
            child_name=child_name,
            join_raw=join_raw,
            relationship_name=relationship_name,
        )
    join = _resolve_child_field(
        join_raw,
        child_name=child_name,
        child_describe=child_describe,
    )
    if isinstance(join, str):
        return judged(
            CROSS_FILTER_LINK_UNVERIFIED,
            join,
            primary=primary_name,
            child=child_name,
            join=join_raw,
            relationship=relationship_name,
        )
    join_name, _join_type = join

    reason = ""
    if is_manual:
        reason = f"{CROSS_FILTER_MANUAL_REASON}（子の describe で確認した）"
    result = judged(
        CROSS_FILTER_LINK_DIRECT,
        reason,
        primary=primary_name,
        child=child_name,
        join=join_name,
        relationship=relationship_name,
    )
    result.primary_name = primary_name
    result.join_name = join_name
    result.child_name = child_name
    result.child_describe = child_describe
    return result


def _judge_manual_child_relationship(
    judged: Callable[..., _CrossFilterJudgement],
    cf: Any,
    *,
    is_manual: bool,
    related_raw: str,
    primary_name: str,
    child_name: str,
    join_raw: str,
    relationship_name: str,
) -> _CrossFilterJudgement:
    """子の describe が無いときの判定。

    手入力 （ ``input/子リレーション.csv`` ） の子リレーションは、 結合項目の
    describe 確認を省略してよい。 ただし criteria （ 絞り込みの列 ） が空の
    ときだけ副問い合わせにできる。 criteria があると列の型が分からないため
    確認できない。
    """
    if not is_manual:
        return judged(
            CROSS_FILTER_LINK_UNVERIFIED,
            f"relatedEntity {related_raw!r} の子オブジェクト {child_name!r} の describe が無い",
            primary=primary_name,
            child=child_name,
            join=join_raw,
            relationship=relationship_name,
        )
    if not _criteria_is_empty(cf):
        return judged(
            CROSS_FILTER_LINK_UNVERIFIED,
            f"{CROSS_FILTER_MANUAL_REASON}: 子の describe が無く、"
            "criteria の列の型が分からないため確認できない",
            primary=primary_name,
            child=child_name,
            join=join_raw,
            relationship=relationship_name,
        )
    result = judged(
        CROSS_FILTER_LINK_DIRECT,
        f"{CROSS_FILTER_MANUAL_REASON}（子の describe が無い。結合項目は手入力の値を使う）",
        primary=primary_name,
        child=child_name,
        join=join_raw,
        relationship=relationship_name,
    )
    result.primary_name = primary_name
    result.join_name = join_raw
    result.child_name = child_name
    result.child_describe = None
    return result


def _relationship_matches_entity(relationship: dict[str, Any], related_entity: str) -> bool:
    """子リレーションが ``relatedEntity`` に一致するか。

    ``childSObject`` と ``relationshipName`` のどちらでも一致する。
    比較は大文字小文字を無視する。
    """
    target = related_entity.strip().lower()
    child = _coerce_str(relationship.get("childSObject")).strip()
    relationship_name = _coerce_str(relationship.get("relationshipName")).strip()
    return child.lower() == target or relationship_name.lower() == target


def _mark_not_converted(link: CrossFilterLink, reason: str) -> CrossFilterLink:
    """``converted`` を False にする。 既に False なら理由はそのままでよい。"""
    if not link.converted:
        return link
    combined = f"{link.reason} / {reason}" if link.reason else reason
    return dc_replace(link, converted=False, reason=combined)


def _with_reason(link: CrossFilterLink, reason: str) -> CrossFilterLink:
    """``converted=False`` にして、 理由を重ねる。 既に False でも理由は足す。"""
    combined = f"{link.reason} / {reason}" if link.reason else reason
    return dc_replace(link, converted=False, reason=combined)


def _build_cross_filter_lines(
    judgements: list[_CrossFilterJudgement],
    *,
    missing: list[str],
    checks: list[str],
    allow_conversion: bool = True,
) -> tuple[list[str], list[CrossFilterLink]]:
    """判定済みのクロスフィルタを SOQL の副問い合わせ （ ``IN`` / ``NOT IN`` ） にする。

    ``直接`` のものだけを SOQL に入れる。 ``直接でない`` と ``確認できず`` は
    ``missing`` に元の値と理由を入れて、 従来どおり 【未完成】 にする。
    副問い合わせは 1 文に ``CROSS_FILTER_MAX_SUBQUERIES`` 個までで、 超えた分も
    判定だけ残して ``converted=False`` にする。
    ``allow_conversion`` が False のときは判定だけ残し、 副問い合わせにはしない。

    Returns:
        ``(副問い合わせの条件式のリスト, 判定のリスト)`` 。
        変換できたものが無ければ条件式は空リスト。
    """
    lines: list[str] = []
    links: list[CrossFilterLink] = []
    for judgement in judgements:
        link = judgement.link
        if link.status != CROSS_FILTER_LINK_DIRECT:
            missing.append(
                f"クロスフィルタ {judgement.index}: {judgement.cf!r} "
                f"(副問い合わせにできない: {link.reason})"
            )
            links.append(link)
            continue
        if not allow_conversion:
            reason = CROSS_FILTER_CHILD_BASE_REASON
            missing.append(
                f"クロスフィルタ {judgement.index}: {judgement.cf!r} "
                f"(副問い合わせにできない: {reason})"
            )
            links.append(_with_reason(link, reason))
            continue
        if len(lines) >= CROSS_FILTER_MAX_SUBQUERIES:
            reason = f"副問い合わせは 1 文に {CROSS_FILTER_MAX_SUBQUERIES} 個まで"
            missing.append(
                f"クロスフィルタ {judgement.index}: {judgement.cf!r} "
                f"(副問い合わせにできない: {reason})"
            )
            links.append(dc_replace(link, converted=False, reason=reason))
            continue
        condition, reason, extra_checks = _build_cross_filter_condition(judgement)
        if not condition:
            missing.append(
                f"クロスフィルタ {judgement.index}: {judgement.cf!r} "
                f"(副問い合わせにできない: {reason})"
            )
            links.append(dc_replace(link, converted=False, reason=reason))
            continue
        lines.append(condition)
        checks.extend(extra_checks)
        checks.append(f"{CROSS_FILTER_CHECK_PREFIX}: {condition}")
        links.append(dc_replace(link, converted=True))
    return lines, links


def _build_cross_filter_condition(
    judgement: _CrossFilterJudgement,
) -> tuple[str, str, list[str]]:
    """判定済みのクロスフィルタ 1 個を ``<主側項目> IN (SELECT ...)`` の形にする。

    1 つでも確かめられないものがあれば、 推測で埋めず ``missing`` にする
    （ レポート以上の件数を取る事故を防ぐため ） 。

    Returns:
        ``(条件式, 理由, 追加 checks)`` 。 条件式が空文字のときは理由が入る。
    """
    cf = judgement.cf
    includes = cf.get("includesObject")
    if not isinstance(includes, bool):
        return "", "includesObject が bool でない", []

    conditions, reason, extra_checks = _build_cross_filter_criteria(
        cf.get("criteria"),
        judgement.child_name,
        judgement.child_describe or {},
    )
    if reason:
        return "", reason, extra_checks

    keyword = "IN" if includes else "NOT IN"
    inner = f"SELECT {judgement.join_name} FROM {judgement.child_name}"
    if conditions:
        inner = f"{inner} WHERE {' AND '.join(conditions)}"
    return f"{judgement.primary_name} {keyword} ({inner})", "", extra_checks


def _resolve_primary_field(
    raw: object,
    *,
    main_object: str,
    main_describe: dict[str, Any] | None,
    column_lookup: dict[str, dict[str, str]],
) -> tuple[str, str] | str:
    """主側の項目を ``(API名, 型)`` で返す。 解決できなければ理由の文字列。

    解決順:

    1. 主オブジェクトの項目名と大文字小文字無視で一致
    2. ``<主オブジェクト>.`` を外して一致
    3. 列対応表の列キーと一致し、 対応フィールドが主オブジェクトの項目

    型は判定側で見る（ ``id`` でなければ 「直接でない」 のため）。
    """
    name = _coerce_str(raw).strip()
    if not name:
        return "primaryEntityField が無い"

    fields = _describe_fields(main_describe)
    field = _find_field(fields, name)
    if field is None:
        field = _find_field(fields, _strip_object_prefix(name, main_object))
    if field is None:
        field = _column_map_field_of_main(name, column_lookup, fields)
    if field is None:
        return f"primaryEntityField {name!r} が主オブジェクトの項目に解決できない"

    field_type = _coerce_str(field.get("type")).strip().lower()
    return _coerce_str(field.get("name")), field_type


def find_child_object_name(child_relationships: list[Any], related_entity: str) -> str:
    """主オブジェクトの子リレーションから、 ``relatedEntity`` に一致する子オブジェクト名。

    ``childSObject`` と ``relationshipName`` のどちらでも一致する。 一致する
    ものが無ければ空文字。 ``src.fetch.site_run`` も同じ照合を使うので、
    ここを公開関数にして両方から使う。

    Args:
        child_relationships: describe の ``childRelationships`` のリスト。
        related_entity: クロスフィルタの ``relatedEntity`` の値。

    Returns:
        子オブジェクト名。 一致するものが無ければ空文字。
    """
    if not related_entity:
        return ""
    target = related_entity.strip().lower()
    for relationship in child_relationships:
        if not isinstance(relationship, dict):
            continue
        child = _coerce_str(relationship.get("childSObject")).strip()
        relationship_name = _coerce_str(relationship.get("relationshipName")).strip()
        if child.lower() == target or relationship_name.lower() == target:
            return child
    return ""


def _resolve_child_field(
    raw: object,
    *,
    child_name: str,
    child_describe: dict[str, Any],
) -> tuple[str, str] | str:
    """子オブジェクトの項目を ``(API名, 型)`` で返す。 解決できなければ理由。"""
    name = _coerce_str(raw).strip()
    if not name:
        return "relatedEntityJoinField が無い"

    fields = _describe_fields(child_describe)
    field = _find_field(fields, name)
    if field is None:
        field = _find_field(fields, _strip_object_prefix(name, child_name))
    if field is None:
        return f"relatedEntityJoinField {name!r} が子オブジェクトの項目に解決できない"

    field_type = _coerce_str(field.get("type")).strip().lower()
    if field_type not in ("id", "reference"):
        return f"relatedEntityJoinField {name!r} の型が id / reference でない（型: {field_type}）"
    return _coerce_str(field.get("name")), field_type


def _build_cross_filter_criteria(
    raw: object,
    child_name: str,
    child_describe: dict[str, Any],
) -> tuple[list[str], str, list[str]]:
    """``criteria`` を子オブジェクトの条件式のリストにする。

    値は既存の ``_extract_filter_values`` / ``_build_filter_condition`` に
    任せた（ エスケープもそこに任せる ） 。 1 つでも解釈できなければ
    理由の文字列を返す。 内側の条件の ``checks`` （ ロングテキストの注意
    など ） も一緒に返す。

    ``criteria`` が無い（ キーが無い・ ``None`` ） ときは「条件なし」とは
    見なさない。 絞り込みが消えて件数が増える危険があるため、 ``missing``
    にする。 空リスト ``[]`` のときだけ「条件なし」として通す。

    Returns:
        ``(条件式のリスト, 理由, 追加 checks)`` 。 理由が空文字なら変換できた。
    """
    if raw is None:
        return [], "criteria が無い", []
    if not isinstance(raw, list):
        return [], "criteria が list でない", []

    fields = _describe_fields(child_describe)
    conditions: list[str] = []
    checks: list[str] = []
    for index, criterion in enumerate(raw, start=1):
        if not isinstance(criterion, dict):
            return [], f"条件 {index} が dict でない", checks
        column = _coerce_str(criterion.get("column")).strip()
        field = _find_field(fields, column)
        if field is None:
            field = _find_field(fields, _strip_object_prefix(column, child_name))
        if field is None:
            return [], f"条件 {index}: 列 {column!r} が子オブジェクトの項目に解決できない", checks
        field_name = _coerce_str(field.get("name"))
        field_type = _coerce_str(field.get("type"))

        value_missing: list[str] = []
        values = _extract_filter_values({"型": field_type}, index, criterion, value_missing)
        if values is None:
            reason = value_missing[0] if value_missing else "値を読めない"
            return [], f"条件 {index}: {reason}", checks

        condition, extra_missing, extra_checks = _build_filter_condition(
            _coerce_str(criterion.get("operator")),
            values,
            field_name,
            field_type,
        )
        if extra_missing:
            return [], f"条件 {index}: {extra_missing[0]}", checks
        if not condition:
            return [], f"条件 {index}: 条件式を作れない", checks
        conditions.append(condition)
        checks.extend(extra_checks)
    return conditions, "", checks


def _criteria_is_empty(cf: Any) -> bool:
    """クロスフィルタの ``criteria`` が空 （ ``[]`` ） かどうか。

    空リストだけが 「条件なし」 とみなす。 キーが無い ``None`` は
    「絞り込みが消えて件数が増える」 危険があるので条件なしとは扱わない。
    """
    if not isinstance(cf, dict):
        return False
    criteria = cf.get("criteria")
    return isinstance(criteria, list) and not criteria


def _describe_fields(describe: dict[str, Any] | None) -> list[dict[str, Any]]:
    """describe の ``fields`` を dict のリストで返す。 壊れていれば空リスト。

    ``make_slim_object`` を通った値は ``fields`` が必ずリストなので、 境界の
    内側では形の確認をしない。
    """
    if not describe:
        return []
    return list(describe.get("fields") or [])


def _describe_child_relationships(describe: dict[str, Any] | None) -> list[dict[str, Any]]:
    """describe の ``childRelationships`` を dict のリストで返す。

    ``make_slim_object`` を通った値は ``childRelationships`` が必ずリストなので、
    境界の内側では形の確認をしない。
    """
    if not describe:
        return []
    return list(describe.get("childRelationships") or [])


def _find_field(fields: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    """項目名が大文字小文字無視で一致する項目を返す。 無ければ ``None`` 。"""
    if not name:
        return None
    target = name.strip().lower()
    for item in fields:
        if _coerce_str(item.get("name")).strip().lower() == target:
            return item
    return None


def _strip_object_prefix(name: str, object_name: str) -> str:
    """``<オブジェクト>.`` が前に付いていれば外す。"""
    prefix = f"{object_name.strip().lower()}."
    if object_name and name.strip().lower().startswith(prefix):
        return name.strip()[len(prefix) :]
    return name.strip()


def _column_map_field_of_main(
    name: str,
    column_lookup: dict[str, dict[str, str]],
    fields: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """列対応表の列キーと一致し、 対応フィールドが主オブジェクトの項目なら
    その項目を返す。 無ければ ``None`` 。
    """
    row = column_lookup.get(name)
    if row is None:
        target = name.lower()
        for key, candidate in column_lookup.items():
            if key.lower() == target:
                row = candidate
                break
    if row is None:
        return None
    soql_name = _coerce_str(row.get("対応フィールドAPI名")).strip()
    if not soql_name or soql_name == "(不明)":
        return None
    return _find_field(fields, soql_name)


def _find_cross_filter_object(
    cross_filter_objects: dict[str, dict[str, Any]] | None,
    child_name: str,
) -> dict[str, Any] | None:
    """``cross_filter_objects`` から子オブジェクトの describe を探す。"""
    if not cross_filter_objects:
        return None
    target = child_name.strip().lower()
    for name, describe in cross_filter_objects.items():
        if name.strip().lower() == target:
            return describe
    return None


def _combine_filter_conditions(
    metadata: dict[str, Any],
    filter_conditions: dict[int, str],
    missing: list[str],
    has_date_lines: bool = False,
) -> tuple[list[str], bool]:
    """組立式 ``reportBooleanFilter`` があれば番号置換、 無ければ番号順で並べる。

    日付条件があるときは組立式全体を ``( ... )`` で囲む。 囲まないと
    ``( a ) OR ( b ) AND 日付`` になり、 OR と AND の優先順位で日付条件が
    1 つの条件にだけかかる形になってしまう。

    戻り値の ``ok`` は組立式の解釈に成功したかどうか。 ``False`` のときは呼び出し側で
    WHERE 全体を空にする (``missing`` には理由が既に入っている) 。
    """
    bool_filter = metadata.get("reportBooleanFilter", "")
    if not bool_filter:
        return [filter_conditions[index] for index in sorted(filter_conditions)], True
    try:
        combined = _replace_boolean_filter_numbers(bool_filter, filter_conditions)
    except ValueError as exc:
        missing.insert(0, f"組立式の解釈失敗: {exc}")
        missing.insert(
            0,
            "組立式の条件を作れない (条件を省くと式が壊れるため WHERE 全体を出さない)",
        )
        return [], False
    if has_date_lines:
        combined = f"( {combined} )"
    return [combined], True


def _replace_boolean_filter_numbers(
    bool_filter: str,
    conditions: dict[int, str],
) -> str:
    """``reportBooleanFilter`` の ``1`` / ``2`` / ``10`` を ``conditions`` で置換する。

    ``1`` と ``10`` がぶつからないように ``(?<!\\d)(\\d+)(?!\\d)`` で
    数字の塊を 1 個ずつ取り出して置換する。 同じ番号が 2 回出現した場合も
    同じ条件式で置き換える (2 回目を数字のまま残さない) 。

    置換の前に組立式を検査する。 使える語は番号と ``AND`` / ``OR`` / ``NOT``
    と括弧だけで、 それ以外は ``ValueError`` を上げる。 検査は **置換前の
    組立式** に対して行うので、 条件の中身 （ 値 ） に巻き込まれない。

    Raises:
        ValueError: 組立式に使えない語がある / 括弧が合っていない /
            同じ深さで ``AND`` と ``OR`` が混在しているとき。
    """
    tokens = _tokenize_bool_filter(bool_filter)
    _validate_bool_filter(tokens)
    normalized = _uppercase_bool_filter_keywords(bool_filter, tokens)

    def replace_one(match: re.Match[str]) -> str:
        number = int(match.group(1))
        if number not in conditions:
            raise ValueError(f"組立式に含まれる番号 {number} は条件一覧に無い")
        return f"( {conditions[number]} )"

    return _BOOLEAN_FILTER_NUMBER_PATTERN.sub(replace_one, normalized)


def _tokenize_bool_filter(bool_filter: str) -> list[tuple[str, str, int, int]]:
    """組立式を語 （ 番号 / キーワード / 括弧 ） に分解する。

    Returns:
        ``(種類, 語, 開始位置, 終了位置)`` のリスト。
    Raises:
        ValueError: 使える語以外が混ざっているとき。
    """
    tokens: list[tuple[str, str, int, int]] = []
    position = 0
    for match in _BOOL_FILTER_TOKEN_PATTERN.finditer(bool_filter):
        gap = bool_filter[position : match.start()]
        if gap.strip():
            raise ValueError(f"組立式に使えない文字があります: {gap.strip()!r}")
        tokens.append((match.lastgroup or "", match.group(), match.start(), match.end()))
        position = match.end()
    tail = bool_filter[position:]
    if tail.strip():
        raise ValueError(f"組立式に使えない文字があります: {tail.strip()!r}")
    return tokens


def _validate_bool_filter(tokens: list[tuple[str, str, int, int]]) -> None:
    """組立式の語と括弧を検査する。

    Raises:
        ValueError: 使える語以外が混ざっている / 括弧が合っていない /
            同じ深さで ``AND`` と ``OR`` が混在しているとき。
    """
    depth = 0
    operators_by_depth: dict[int, set[str]] = {}
    for kind, text, _start, _end in tokens:
        if kind == "open":
            depth += 1
            operators_by_depth.setdefault(depth, set())
            continue
        if kind == "close":
            if depth == 0:
                raise ValueError("組立式の括弧が合いません（ 閉じすぎ ）")
            depth -= 1
            continue
        if kind == "number":
            continue
        keyword = text.upper()
        if keyword not in _BOOL_FILTER_KEYWORDS:
            raise ValueError(f"組立式に使えない語があります: {text!r}")
        if keyword == "NOT":
            continue
        used = operators_by_depth.setdefault(depth, set())
        if used and keyword not in used:
            raise ValueError(f"組立式と同じ深さで AND と OR が混在しています: {keyword}")
        used.add(keyword)
    if depth != 0:
        raise ValueError("組立式の括弧が合いません（ 閉じられていない ）")


def _uppercase_bool_filter_keywords(
    bool_filter: str,
    tokens: list[tuple[str, str, int, int]],
) -> str:
    """組立式の語 （ ``AND`` / ``OR`` / ``NOT`` ） を大文字にそろえる。"""
    result = bool_filter
    for kind, text, start, end in reversed(tokens):
        if kind == "word":
            result = result[:start] + text.upper() + result[end:]
    return result


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
            missing.append(f"並び順 {index}: {sort_column} に対応が無い (省いた)")
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
    """``record`` (``ReportRecord``) から ``SoqlDraft`` を組み立てる (純粋関数) 。

    規則は依頼仕様 (ルール詳細) に従う。 ``record`` は ``ReportRecord``
    互換 (``key`` / ``summary`` / ``main_object`` / ``report`` / ``column_map`` /
    ``objects`` / ``cross_filter_objects`` ) であれば良い。 ``report`` は
    ``make_slim_report`` で絞った形式 (``reportMetadata`` /
    ``reportExtendedMetadata`` のみ) を想定。

    status の判定ルール:

    - ``作れず`` … 主オブジェクト不明 (``main_object`` が空 / ``None``) または
      SELECT に入れられる列が 1 つも無い ( 全列 ``(不明)`` または全列
      ``missing`` のため SELECT から落ちた ) 。
    - ``一部作れず`` … ``missing`` に 1 件以上ある (SOQL から省いた部分がある)
    - ``要確認`` … ``missing`` は無いが ``checks`` に 1 件以上ある。
    - ``作成済み`` … ``missing`` も ``checks`` も無い。

    FROM は ``record.soql_from_object`` があればそちらを、 無ければ主オブジェクトを
    使う （ 子から取る形にしたときだけ違う ） 。

    取り方 (``use_bulk``) は、 副問い合わせ （ クロスフィルタ ） を出したかどうかと
    件数で決める。 件数は管理表の「2000件超」列とサンドボックスの 2000 行チェック
    から 3 段で判定する（ ``src.row_size.decide_row_size`` ）。 文字列を
    後からパースしない。
    """
    main_object = record.main_object
    if not main_object:
        return SoqlDraft(
            soql="",
            status=STATUS_NONE,
            missing=("主オブジェクトが不明のため作れず",),
            checks=(),
        )

    # ``record.report`` は入口（``make_slim_report``）で形をそろえてある
    metadata = record.report["reportMetadata"]
    column_lookup = _build_column_lookup(record.column_map)
    main_describe = _record_main_describe(record, main_object)
    cross_filter_objects = record.cross_filter_objects
    from_object = record.soql_from_object or main_object

    missing: list[str] = []
    checks: list[str] = []
    select_lines = _build_select_lines(metadata, column_lookup, missing)
    _collect_unresolved_metadata_missing(metadata, missing)
    # クロスフィルタの判定は、 SELECT 列入りの早期 return でも残したいので
    # 先に済ませておく
    judgements = _judge_cross_filters(
        metadata,
        column_lookup,
        main_object=main_object,
        main_describe=main_describe,
        cross_filter_objects=cross_filter_objects,
    )
    if not select_lines:
        return SoqlDraft(
            soql="",
            status=STATUS_NONE,
            missing=(*tuple(missing), "SELECT に入れられる列が無い"),
            checks=tuple(checks),
            cross_filter_links=tuple(judgement.link for judgement in judgements),
        )

    where_lines, where_missing, where_checks, has_subquery, links = _build_where_clause(
        metadata,
        column_lookup,
        judgements=judgements,
        # FROM を子に切り替えたときは、 副問い合わせの左辺が変わるので変換しない
        allow_cross_filter_conversion=not record.soql_from_object,
    )
    missing.extend(where_missing)
    checks.extend(where_checks)
    _collect_scope_check(metadata, checks)
    _collect_owner_polymorphic_checks(metadata, column_lookup, checks)
    _collect_auto_path_checks(metadata, column_lookup, checks)
    _collect_child_base_check(record, checks)
    order_by_clause, order_missing, order_checks = _build_order_by_clause(metadata, column_lookup)
    missing.extend(order_missing)
    checks.extend(order_checks)

    # 取り方: 件数と副問い合わせの有無で決める
    row_size = decide_row_size(record.exceeds_row_limit, record.row_check)
    use_bulk, bulk_blockers = _decide_use_bulk(has_subquery, row_size)
    if has_subquery and row_size.over_2000 is not False:
        # 2000 件超 か 分からないときは、 本番の件数が 5000 件を超えると
        # SOQL を分ける必要がある
        checks.append(SUBQUERY_ROWS_CHECK)

    soql, count_soql = _assemble_soql_text(select_lines, where_lines, order_by_clause, from_object)
    status = _decide_status(missing, checks)
    soql, count_soql = _apply_incomplete_marker(soql, count_soql, status)

    return SoqlDraft(
        soql=soql,
        status=status,
        missing=tuple(missing),
        checks=tuple(checks),
        count_soql=count_soql,
        limit=_extract_top_rows_limit(metadata),
        # 「似た SOQL」 の判定に使う部品。 SOQL の文字列を後からパースしない
        select_items=tuple(select_lines),
        where_items=tuple(where_lines),
        order_by=order_by_clause,
        use_bulk=use_bulk,
        bulk_blockers=tuple(bulk_blockers),
        cross_filter_links=tuple(links),
    )


def _record_main_describe(record: ReportRecord, main_object: str) -> dict[str, Any] | None:
    """``record.objects`` から主オブジェクトの describe を取り出す。

    ``record.objects`` は ``make_slim_object`` を通った値なので、 境界の
    内側では形の確認をしない。
    """
    return record.objects.get(main_object)


def _decide_use_bulk(has_subquery: bool, row_size: RowSize) -> tuple[bool, list[str]]:
    """取り方 （ Bulk / 通常 SOQL ） と、 通常 SOQL にした理由を決める。

    - 副問い合わせ （ クロスフィルタ ） があるときは、 件数に関係なく通常 SOQL
    - 副問い合わせが無いときは、 件数が 1000 件以下なら通常 SOQL、 1000 件超
      （ 2000 件超 を含む） なら Bulk
    - 件数が分からないときは Bulk にする（ 今までどおり ）

    Returns:
        ``(use_bulk, bulk_blockers)`` 。 ``bulk_blockers`` は 通常 SOQL に
        した理由 （ 1 件 1 要素） 。
    """
    if has_subquery:
        blockers = [BULK_BLOCKER_SUBQUERY]
        if row_size.over_1000 is False:
            blockers.append(_small_rows_reason(row_size))
        return False, blockers
    if row_size.over_1000 is False:
        return False, [_small_rows_reason(row_size)]
    return True, []


def _small_rows_reason(row_size: RowSize) -> str:
    """件数が 1000 件以下なので通常 SOQL で取るときの理由。"""
    return f"{BULK_BLOCKER_SMALL_ROWS.format(BULK_MIN_ROWS)}（根拠: {row_size.basis}）"


def _build_select_lines(
    metadata: dict[str, Any],
    column_lookup: dict[str, dict[str, str]],
    missing: list[str],
) -> list[str]:
    """SELECT 句の ``field_api`` リストを作る。 取得できなかった列は missing に積む。"""
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
            # 重複は除く (同一フィールドが detail / groupings 両方に)
            continue
        seen_fields.add(field_api)
        select_lines.append(field_api)
    return select_lines


def _collect_unresolved_metadata_missing(
    metadata: dict[str, Any],
    missing: list[str],
) -> None:
    """SOQL に訳せない集計系を missing に積む。 バケット・独自計算式・集計・スナップショット。"""
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
                "(集計のため SOQL には入れない)"
            )

    custom_summary_formula = metadata.get("customSummaryFormula")
    if isinstance(custom_summary_formula, list) and custom_summary_formula:
        for index, csf in enumerate(custom_summary_formula, start=1):
            csf_dict = csf if isinstance(csf, dict) else {}
            label = _coerce_str(csf_dict.get("label"))
            formula = _coerce_str(csf_dict.get("formula"))
            missing.append(
                f"独自計算式 {index}: {label or '(ラベル不明)'} = {formula} (SOQL には入れない)"
            )

    # ``aggregates`` の ``"!"`` 形式は集計レポートなので SOQL にしない
    # (``RowCount`` は集計行が無いだけなので missing にも入れない)
    for agg in metadata.get("aggregates", []):
        if "!" not in agg:
            continue
        target = agg.split("!", 1)[-1]
        if target == "RowCount":
            continue
        missing.append(f"集計 {agg}: 集計レポートのため SOQL にしない (明細として取れない)")

    # ``historicalSnapshotDates`` は履歴スナップショットの日付指定。 SOQL に訳せず、
    # 黙って捨てるとレポート以上の範囲を取る危険があるため missing に理由を入れて
    # ``一部作れず`` に倒す
    snapshot_dates = metadata.get("historicalSnapshotDates")
    if isinstance(snapshot_dates, list) and snapshot_dates:
        missing.append(f"履歴スナップショット ({len(snapshot_dates)} 件) の条件は SOQL にできない")


def _collect_scope_check(metadata: dict[str, Any], checks: list[str]) -> None:
    """``scope`` が ``organization`` 以外なら実行ユーザー依存の注意を checks に積む。"""
    scope = metadata.get("scope", "")
    if scope and scope.lower() != "organization":
        checks.append(
            f"スコープ={scope}: 実行ユーザーで結果が変わる。 OwnerId 等の条件を足すか確認"
        )


def _collect_owner_polymorphic_checks(
    metadata: dict[str, Any],
    column_lookup: dict[str, dict[str, str]],
    checks: list[str],
) -> None:
    """Owner がポリモーフィックな列の注意書きを checks に 1 列 1 件積む。

     列の備考に ``OWNER_POLYMORPHIC_NOTE`` がある列が、 SELECT・絞り込み・
     並び順のどれかに使われているときだけ積む。 同じ列が複数の場所で使われて
    いても 1 件だけにする。
    """
    for column_key in _used_column_keys(metadata):
        column_info = column_lookup.get(column_key)
        if column_info is None:
            continue
        if OWNER_POLYMORPHIC_NOTE not in (column_info.get("備考") or ""):
            continue
        field_api = column_info.get("対応フィールドAPI名") or ""
        checks.append(f"{OWNER_POLYMORPHIC_NOTE}: {field_api}")


def _collect_auto_path_checks(
    metadata: dict[str, Any],
    column_lookup: dict[str, dict[str, str]],
    checks: list[str],
) -> None:
    """複数候補から自動で確定した列の注意書きを checks に 1 列 1 件積む。

    SELECT・絞り込み・並び順のどれかに使われている列だけを対象にする。
    件数比較では値の違いを検出できないため、 人が確かめる必要がある。
    """
    for column_key in _used_column_keys(metadata):
        column_info = column_lookup.get(column_key)
        if column_info is None:
            continue
        parts = auto_path_parts(column_info.get("備考") or "")
        if parts is None:
            continue
        adopted, others = parts
        label = column_info.get("表示名") or column_key
        checks.append(
            f"列 {label} は候補から自動で確定した: {adopted}"
            f"（他の候補: {others}）。値がレポートと同じか確かめる"
        )


def _collect_child_base_check(record: ReportRecord, checks: list[str]) -> None:
    """FROM を子オブジェクトに切り替えたときの注意書きを checks に積む。"""
    if not record.soql_from_object:
        return
    parent_object = record.main_object or ""
    relationship_name = record.soql_from_relationship or ""
    checks.append(
        f"子から取る形にした（親 {parent_object}、子 {record.soql_from_object}、"
        f"経路 {relationship_name}）。子を持たない親の行は出ない。レポートと件数を確かめる"
    )


def _used_column_keys(metadata: dict[str, Any]) -> list[str]:
    """SELECT・絞り込み・並び順で使う列キーを重複なしで集める。"""
    keys: list[str] = []

    def add(column_key: str) -> None:
        if column_key and column_key not in keys:
            keys.append(column_key)

    for column_key in mapping.select_column_keys(metadata):
        add(column_key)
    for report_filter in metadata.get("reportFilters", []):
        add(report_filter.get("column") or "")
    date_filter = metadata.get("standardDateFilter") or {}
    add(date_filter.get("column") or "")
    for sort_entry in metadata.get("sortBy", []):
        add(sort_entry.get("sortColumn") or "")
    return keys


def _assemble_soql_text(
    select_lines: list[str],
    where_lines: list[str],
    order_by_clause: str,
    main_object: str,
) -> tuple[str, str]:
    """組み立て済みの SELECT / WHERE / ORDER BY から、 本文用と件数用の SOQL を返す。"""
    # 条件のリストを直接整形する (文字列リテラル内の ' AND ' で改行を入れて値を変えない)
    where_block = "\n  AND\n  ".join(where_lines) if where_lines else ""
    select_block = ",\n  ".join(select_lines)
    soql_lines: list[str] = [f"SELECT {select_block}", f"FROM {main_object}"]
    if where_block:
        soql_lines.append(f"WHERE {where_block}")
    if order_by_clause:
        soql_lines.append(order_by_clause)
    soql = "\n".join(soql_lines)

    # 件数用は同じ ``select_lines`` / ``where_lines`` から組み立てる (文字列の切り貼りをしない)
    count_lines: list[str] = ["SELECT COUNT()", f"FROM {main_object}"]
    if where_block:
        count_lines.append(f"WHERE {where_block}")
    count_soql = "\n".join(count_lines)
    return soql, count_soql


def _decide_status(missing: list[str], checks: list[str]) -> str:
    """``missing`` / ``checks`` の有無から ``status`` を決める。"""
    if missing:
        return STATUS_PARTIAL
    if checks:
        return STATUS_NEEDS_CHECK
    return STATUS_CREATED


def _apply_incomplete_marker(
    soql: str,
    count_soql: str,
    status: str,
) -> tuple[str, str]:
    """``一部作れず`` の SOQL には 1 行目に印を付ける (ダウンローダーが実行前に止める) 。"""
    if status != STATUS_PARTIAL:
        return soql, count_soql
    return f"{INCOMPLETE_MARKER}\n{soql}", f"{INCOMPLETE_MARKER}\n{count_soql}"


def _extract_top_rows_limit(metadata: dict[str, Any]) -> int | None:
    """``topRows.rowLimit``（入口で正の整数か None にそろえてある）を返す。"""
    return (metadata.get("topRows") or {}).get("rowLimit")
