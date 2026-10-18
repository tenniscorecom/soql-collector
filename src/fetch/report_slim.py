"""レポート・オブジェクトの describe を、 SOQL 組立に必要な形だけに絞る（境界の整形）。

許可リストで落としたキー名は ``collect_dropped_keys()`` で取り出し、 警告に 1 行入れる。"""

from __future__ import annotations

from typing import Any

from src.describe_store import active_picklist_values

# ``make_slim_report`` で残す ``reportMetadata`` のキー（SOQL 組立に必要なものだけ）。
# SELECT・ WHERE・ 日付条件・ サブクエリ・ GROUP BY・ 集計・ ORDER BY・ 件数・
# 独自グループ・ 独自計算式に必要な情報を網羅する。
REPORT_METADATA_KEYS: tuple[str, ...] = (
    "id",
    "name",
    "reportType",
    "reportFormat",
    "scope",
    "detailColumns",
    "reportFilters",
    "reportBooleanFilter",
    "standardDateFilter",
    "standardFilters",
    "crossFilters",
    "groupingsDown",
    "groupingsAcross",
    "aggregates",
    "sortBy",
    "topRows",
    "historicalSnapshotDates",
    "buckets",
    "customSummaryFormula",
    "division",
    "hasDetailRows",
)


# ``make_slim_report`` で残す ``reportExtendedMetadata`` のキー。
# 3 つだけ（``detailColumnInfo`` / ``groupingColumnInfo`` /
# ``aggregateColumnInfo``）。
REPORT_EXTENDED_METADATA_KEYS: tuple[str, ...] = (
    "detailColumnInfo",
    "groupingColumnInfo",
    "aggregateColumnInfo",
)


# ── slim 後のデータ型 ─────────────────────────────────────────────
# SOQL 組立と ``tables`` / ``ai_markdown`` が読む範囲だけを型で示す。
# SlimReport は ``reportMetadata`` / ``reportExtendedMetadata`` を持つ dict。
# SlimObject は ``make_slim_object`` の戻り値で、 ``name`` / ``label`` / ``custom`` /
# ``fields`` の 4 キーを必ず持つ。 ``mapping`` やテストも describe を素の dict として
# 扱うので、 TypedDict にはせず dict の別名にする。
SlimReport = dict[str, Any]


SlimObject = dict[str, Any]


def make_slim_report(metadata: object) -> SlimReport:
    """``client.report.describe()`` の戻り値を、 内側で検証せずに読める形に整える（境界）。

    - 戻り値は **必ず** ``reportMetadata`` と ``reportExtendedMetadata`` の 2 つの
      dict を持つ（入力が壊れていれば空の dict）
    - ``reportMetadata``: ``REPORT_METADATA_KEYS`` にあるキーだけ残す。 リストとして
      読むキーは中身の型をそろえる（``detailColumns`` / ``aggregates`` は空でない
      str だけ、 ``reportFilters`` / ``groupingsDown`` / ``groupingsAcross`` /
      ``sortBy`` は dict だけ、 ``reportType`` は dict）。 型がそろっている値は元の
      参照のまま
    - ``reportExtendedMetadata``: ``REPORT_EXTENDED_METADATA_KEYS`` の 3 キーだけを、
      ``{列キー: dict}`` の形で残す
    - 落としたキー名は含めない（``collect_dropped_keys()`` で取り出す）
    - 入力の dict は書き換えない
    """
    source = metadata if isinstance(metadata, dict) else {}
    report_metadata_raw = source.get("reportMetadata")
    report_extended_raw = source.get("reportExtendedMetadata")
    report_metadata = report_metadata_raw if isinstance(report_metadata_raw, dict) else {}
    report_extended = report_extended_raw if isinstance(report_extended_raw, dict) else {}

    slimmed_metadata = {
        key: value for key, value in report_metadata.items() if key in REPORT_METADATA_KEYS
    }
    for key in ("detailColumns", "aggregates"):
        if key in slimmed_metadata:
            slimmed_metadata[key] = _str_list(slimmed_metadata[key])
    for key in _TEXT_KEYS:
        if key in slimmed_metadata:
            slimmed_metadata[key] = _text(slimmed_metadata[key])
    # 要素の中で文字列として読む値の型もそろえる。 絞り込みは dict でない要素も
    # 空の dict にして位置を保つ（並び順が組立式の番号と対応しているため）
    for key, text_keys in _TEXT_KEYS_IN_LIST_ITEMS.items():
        if key not in slimmed_metadata:
            continue
        value = slimmed_metadata[key]
        items = (
            [item if isinstance(item, dict) else {} for item in value]
            if key == "reportFilters" and isinstance(value, list)
            else _dict_list(value)
        )
        slimmed_metadata[key] = [_with_text_values(item, text_keys) for item in items]
    for key, text_keys in _TEXT_KEYS_IN_DICT_VALUES.items():
        if key not in slimmed_metadata:
            continue
        value = slimmed_metadata[key]
        slimmed_metadata[key] = (
            _with_text_values(value, text_keys) if isinstance(value, dict) else None
        )
    top_rows = slimmed_metadata.get("topRows")
    if top_rows is not None:
        slimmed_metadata["topRows"] = {
            **top_rows,
            "rowLimit": _positive_int(top_rows.get("rowLimit")),
        }

    # ``standardDateFilter`` が 「実質指定なし」 （start / end が空で
    # durationValue が空か ``CUSTOM``） なら、 下流に空の日付フィルターが
    # 流れないようにここでキーごと落とす。 内側では「無い」 ものとして
    # 扱う前提で組まれているので、 個別ガードは足さない
    if _is_undated_filter(slimmed_metadata.get("standardDateFilter")):
        slimmed_metadata.pop("standardDateFilter", None)

    slimmed_extended = {
        key: {
            column_key: info
            for column_key, info in value.items()
            if isinstance(column_key, str) and isinstance(info, dict)
        }
        for key, value in report_extended.items()
        if key in REPORT_EXTENDED_METADATA_KEYS and isinstance(value, dict)
    }
    return {"reportMetadata": slimmed_metadata, "reportExtendedMetadata": slimmed_extended}


def _is_undated_filter(date_filter: object) -> bool:
    """``standardDateFilter`` が 「実質指定なし」 なら True。

    定義: ``startDate`` と ``endDate`` がどちらも空 （``None`` / 空文字 /
    空白のみ） で、 かつ ``durationValue`` が空か ``CUSTOM`` （大文字小文字
    無視） 。 相対期間 （``THIS_YEAR`` / ``LAST_N_DAYS:30`` 等） は
    「指定あり」 なので False。 ``date_filter`` が dict で無いときも
    True （「無かった」 ものとして扱う）
    """
    if not isinstance(date_filter, dict):
        return True

    def _is_blank(value: object) -> bool:
        if value is None:
            return True
        if not isinstance(value, str):
            return True
        return not value.strip()

    if not _is_blank(date_filter.get("startDate")):
        return False
    if not _is_blank(date_filter.get("endDate")):
        return False

    duration = date_filter.get("durationValue")
    if isinstance(duration, str):
        normalized = duration.strip()
        if not normalized:
            return True
        return normalized.upper() == "CUSTOM"
    return True


# 文字列としてそろえる単純な値のキー
_TEXT_KEYS: tuple[str, ...] = ("id", "name", "reportFormat", "scope", "reportBooleanFilter")


# リストの要素（dict）の中で、 文字列として読む値のキー
_TEXT_KEYS_IN_LIST_ITEMS: dict[str, tuple[str, ...]] = {
    "reportFilters": ("column", "operator"),
    "groupingsDown": ("name", "sortOrder", "dateGranularity"),
    "groupingsAcross": ("name", "sortOrder", "dateGranularity"),
    "sortBy": ("sortColumn", "sortOrder"),
}


# dict の値の中で、 文字列として読む値のキー（dict でなければ None。 Salesforce は
# 使わない項目を null で返す）
_TEXT_KEYS_IN_DICT_VALUES: dict[str, tuple[str, ...]] = {
    "reportType": ("type",),
    "standardDateFilter": ("column", "durationValue", "startDate", "endDate"),
    "topRows": ("direction",),
}


def _with_text_values(item: dict[str, Any], text_keys: tuple[str, ...]) -> dict[str, Any]:
    """``item`` の ``text_keys`` のうち、 ある値を str にそろえる。 そろっていれば元の dict。"""
    if all(isinstance(item.get(key), str) for key in text_keys if key in item):
        return item
    return {key: _text(value) if key in text_keys else value for key, value in item.items()}


def _str_list(value: object) -> list[str]:
    """空でない str だけのリストにする。 すでにそろっていれば元のリストを返す。"""
    if not isinstance(value, list):
        return []
    if all(isinstance(item, str) and item for item in value):
        return value
    return [item for item in value if isinstance(item, str) and item]


def _dict_list(value: object) -> list[dict[str, Any]]:
    """dict だけのリストにする。 すでにそろっていれば元のリストを返す。"""
    if not isinstance(value, list):
        return []
    if all(isinstance(item, dict) for item in value):
        return value
    return [item for item in value if isinstance(item, dict)]


def _positive_int(value: object) -> int | None:
    """正の整数（数字だけの文字列も可）なら int、 それ以外（bool を含む）は None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    return value if isinstance(value, int) and value > 0 else None


def _text(value: object) -> str:
    """str ならそのまま、 それ以外は空文字（境界の正規化用）。"""
    return value if isinstance(value, str) else ""


def collect_dropped_keys(metadata: object) -> tuple[str, ...]:
    """``make_slim_report`` と同じルールで、 落としたキー名を出現順で返す。

    warnings に 「レポート定義の中で使わなかったキー: ...」 の 1 行を
    出すために使う。 JSON 出力は一切しない （CSV 化も不要、 ログは入力区）
    """
    buckets: list[str] = []

    if not isinstance(metadata, dict):
        return ()

    report_metadata_raw = metadata.get("reportMetadata")
    if isinstance(report_metadata_raw, dict):
        for key in report_metadata_raw:
            if key not in REPORT_METADATA_KEYS:
                buckets.append(key)

    report_extended_raw = metadata.get("reportExtendedMetadata")
    if isinstance(report_extended_raw, dict):
        for key in report_extended_raw:
            if key not in REPORT_EXTENDED_METADATA_KEYS:
                buckets.append(key)

    for key in metadata:
        if key in ("reportMetadata", "reportExtendedMetadata"):
            continue
        buckets.append(key)

    return tuple(buckets)


def make_slim_object(describe: object) -> SlimObject:
    """``describe_object()`` の戻り値を、 内側で検証せずに読める形に整える（境界）。

    - 残す: ``name`` / ``label`` / ``custom`` / ``fields``（``childRelationships`` /
      ``recordTypeInfos`` などは落とす）
    - ``fields`` の各項目は次の 9 キーに**必ず**この型でそろえる: ``name`` /
      ``label`` / ``type`` / ``relationshipName``（str。 無ければ空文字）、
      ``custom`` / ``nameField``（bool）、 ``referenceTo``（空でない str の
      リスト）、 ``picklist``（active な ``value`` を最大 30 件）、
      ``picklistTotal``（active な選択肢の総数）
    - 入力が壊れていれば空の値で返す。 入力の dict は書き換えない
    """
    source = describe if isinstance(describe, dict) else {}
    fields_raw = source.get("fields")
    fields_list: list[Any] = fields_raw if isinstance(fields_raw, list) else []
    return {
        "name": _text(source.get("name")),
        "label": _text(source.get("label")),
        "custom": source.get("custom") is True,
        "fields": [_slim_field(field) for field in fields_list if isinstance(field, dict)],
    }


def _slim_field(field: dict[str, Any]) -> dict[str, Any]:
    """``fields`` の各項目を 9 キーにそろえる。キーの**順番**もこの順で返す。"""
    picklist, picklist_total = _extract_picklist(field.get("picklistValues"))
    return {
        "name": _text(field.get("name")),
        "label": _text(field.get("label")),
        "type": _text(field.get("type")),
        "custom": field.get("custom") is True,
        "referenceTo": _str_list(field.get("referenceTo")),
        "relationshipName": _text(field.get("relationshipName")),
        "nameField": field.get("nameField") is True,
        "picklist": picklist,
        "picklistTotal": picklist_total,
    }


def _extract_picklist(picklist_values: object) -> tuple[list[str], int]:
    """``picklistValues`` の active な ``value`` を、 最大 30 件と総数で返す。"""
    active = active_picklist_values(picklist_values)
    return active[:30], len(active)


def dropped_keys_warning_text(dropped: tuple[str, ...]) -> str | None:
    """``collect_dropped_keys()`` の戻り値から警告表用の 1 行を組み立てる。

    何も落ちていないときは ``None`` を返す。 出力形式:

        レポート定義の中で使わなかったキー: k1, k2, k3
    """
    if not dropped:
        return None
    return "レポート定義の中で使わなかったキー: " + ", ".join(dropped)
