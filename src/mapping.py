"""comken から移した、レポートの列 ⇔ Salesforce フィールド対応表の組み立て。

``report_type_candidates()`` がカスタムレポートタイプの ``$`` / ``@`` を
含む値から主オブジェクト候補を抽出し、 ``build_column_map()`` が渡された
describe から列対応表を作る。
"""

from __future__ import annotations

import re
from typing import Any

# カスタムレポートタイプの ``reportType.type`` に前置される ``CustomEntity``
# プレースホルダを候補抽出時に除くための正規表現。
_CUSTOM_ENTITY_TOKEN_PATTERN = re.compile(r"\A(?:CustomEntity)+\Z")


def report_type_candidates(report_type: str) -> list[str]:
    """``reportType.type`` から主オブジェクトの候補を取り出す（純粋関数）。

    カスタムレポートタイプでは ``CustomEntity$Project__c`` や ``Account@Contact``
    のように ``$`` や ``@`` を含む値が入ることがある。 そのまま
    ``describe_object()`` に渡すと ``ValueError`` で止まるため、 ``$`` / ``@``
    で区切って候補のリストにし、 先頭から ``describe_object()`` が通るものを
    採る。

    ルール:

    - ``@`` が含まれるときは、 最初の ``@`` より左だけを使う
    - 残りを ``$`` で区切る
    - 空文字と、 ``CustomEntity`` を 1 回以上繰り返しただけのトークン
      （ ``CustomEntity`` / ``CustomEntityCustomEntity`` など） を除く
    - 重複は除き、 出現順を保つ
    - ``$`` も ``@`` も無ければ ``[report_type]``

    Examples:
        >>> report_type_candidates("CustomEntity$Project__c")
        ['Project__c']
        >>> report_type_candidates("Account@Contact")
        ['Account']
        >>> report_type_candidates("Opportunity")
        ['Opportunity']
        >>> report_type_candidates("CustomEntity$")
        []
    """
    if "@" in report_type:
        report_type = report_type.split("@", 1)[0]
    tokens = report_type.split("$")
    cleaned = [
        token for token in tokens if token and not _CUSTOM_ENTITY_TOKEN_PATTERN.fullmatch(token)
    ]
    seen: set[str] = set()
    unique: list[str] = []
    for token in cleaned:
        if token not in seen:
            seen.add(token)
            unique.append(token)
    return unique


def candidate_failure_reason(original: str, candidates: list[str]) -> str:
    """``$`` / ``@`` を含む ``reportType.type`` で候補を試したが全滅した
    （または候補が空だった） ときの理由文言を組み立てる。

    元の ``reportType.type`` の値と試した候補の一覧を添え、 「手動で確認」
    の手引きを入れる。 ``build_column_map()`` の備考列にそのまま埋め込む
    1 行分のテキスト。
    """
    if candidates:
        candidate_text = ", ".join(repr(candidate) for candidate in candidates)
        return (
            f"レポートタイプ {original!r} から主オブジェクト候補"
            f" {candidate_text} を順に確認しましたが、どれも Object Describe"
            " できず自動判定できません。手動で確認してください"
        )
    return (
        f"レポートタイプ {original!r} からは主オブジェクト候補を抽出できなかったため"
        "自動判定できません。手動で確認してください"
    )


def _normalize_label(label: str) -> str:
    """表示名の前後空白を落として小文字化する。

    Salesforce の表示名は前後に空白が入ることがあり、 また大小文字の
    ゆらぎは実フィールド側と一致しないことがあるため、 突き合わせの前に
    もう一段正規化する。 完全一致しなかったときのフォールバックは敢えて
    行わない — 不一致は「不一致」のまま残し、 誤った候補を押し付けない
    ことを優先するため。
    """
    return label.strip().lower()


def _build_field_index(describe: object) -> dict[str, list[dict[str, Any]]]:
    """Object Describe の ``fields`` を ``{正規化表示名: [field, ...]}`` に組み立てる。

    同じ表示名を持つフィールドが複数ある場合は最初に見つかった 1 件だけを
    選ばず **リストのまま** 残す。 呼び出し側で件数を判定し、 1 件なら
    採用、 2 件以上なら「複数候補あり」として注記する。

    Object Describe が ``fields`` を返さなかった場合（壊れたレスポンス等）
    は空の辞書を返し、 全列が「対応フィールドなし」になる。 例外にはしない
    （ ``build_column_map()`` のポリシーと揃えるため）。
    """
    fields = describe.get("fields", []) if isinstance(describe, dict) else []
    index: dict[str, list[dict[str, Any]]] = {}
    for field in fields:
        if not isinstance(field, dict):
            continue
        name = field.get("name")
        label = field.get("label")
        field_type = field.get("type")
        if not isinstance(name, str) or not isinstance(label, str):
            continue
        index.setdefault(_normalize_label(label), []).append(
            {"name": name, "type": field_type if isinstance(field_type, str) else ""}
        )
    return index


def _filter_only_columns(report_filters: object) -> list[str]:
    """``reportFilters`` にだけ現れる列キーを返す（SELECT には出ない列）。"""
    if not isinstance(report_filters, list):
        return []
    return [
        column_key
        for report_filter in report_filters
        if isinstance(report_filter, dict)
        and isinstance(column_key := report_filter.get("column"), str)
    ]


def _grouping_columns(report_metadata: dict[str, Any]) -> list[str]:
    """``groupingsDown`` / ``groupingsAcross`` （ ``SUMMARY`` / ``MATRIX`` ）の列キー。"""
    columns = []
    for grouping_key in ("groupingsDown", "groupingsAcross"):
        for grouping in report_metadata.get(grouping_key, []) or []:
            if isinstance(grouping, dict) and isinstance(grouping.get("name"), str):
                columns.append(grouping["name"])
    return columns


def _aggregate_field_columns(aggregates: object) -> list[str]:
    """``aggregates`` の集計対象列キーを返す。

    集計キーは ``"s!Amount"`` のように「関数!列キー」の形。 ``"!"`` が無い
    ものは列キーとして扱えないため無視する。
    """
    if not isinstance(aggregates, list):
        return []
    return [
        aggregate_key.split("!", 1)[-1]
        for aggregate_key in aggregates
        if isinstance(aggregate_key, str) and "!" in aggregate_key
    ]


def collect_describable_columns(report_metadata: dict[str, Any]) -> list[str]:
    """``build_column_map()`` が解決を試みる列キーの一覧を組み立てる（重複除去済み）。

    ``detailColumns`` （SELECT に出す列） に加え、 SELECT には出ないが
    ``reportFilters`` だけで使う列、 ``SUMMARY`` / ``MATRIX`` の
    ``groupingsDown`` / ``groupingsAcross`` のグルーピング列、 ``aggregates``
    の集計対象列も含める。
    """
    columns = list(report_metadata.get("detailColumns", []))
    for column_key in (
        _filter_only_columns(report_metadata.get("reportFilters"))
        + _grouping_columns(report_metadata)
        + _aggregate_field_columns(report_metadata.get("aggregates"))
    ):
        if column_key not in columns:
            columns.append(column_key)
    return columns


def _collect_column_info(metadata: dict[str, Any]) -> dict[str, Any]:
    """``build_column_map()`` の表示名引き当てに使う列情報を組み立てる。

    グルーピング列・集計列の表示名は ``detailColumnInfo`` ではなく
    ``groupingColumnInfo`` / ``aggregateColumnInfo`` 側に入っているため、
    同じ列キー空間としてマージする（キーの重複は無い前提）。
    """
    extended_metadata: dict[str, Any] = (
        metadata.get("reportExtendedMetadata", {}) if isinstance(metadata, dict) else {}
    )
    column_info: dict[str, Any] = dict(extended_metadata.get("detailColumnInfo", {}) or {})
    for info_key in ("groupingColumnInfo", "aggregateColumnInfo"):
        info = extended_metadata.get(info_key)
        if isinstance(info, dict):
            column_info.update(info)
    return column_info


def build_column_map(
    metadata: dict[str, Any],
    main_describe: dict[str, Any] | None,
    reason: str | None,
) -> list[dict[str, str]]:
    """渡された主オブジェクトの describe から列対応表を作る（純粋関数）。

    呼び出し側で主オブジェクトの describe を既に取ってある前提で、 その dict
    と、 取れなかった理由（ ``None`` なら取れている） を渡す。

    ``main_describe`` が ``None`` のとき、 もしくは ``reason`` が ``None`` でない
    ときは、 全列を ``"(不明)"`` にして備考に ``reason`` を入れる。 一致する
    フィールドが無い列は ``"対応フィールドなし"`` 、 候補が 2 件以上の列は
    誤った候補を押し付けず「複数候補あり」にする。

    Args:
        metadata: ``client.report.describe(report_id)`` の戻り値。
        main_describe: 主オブジェクトの Object Describe。 取れなかった /
            取らなかったときは ``None``。
        reason: 主オブジェクトを特定できなかった / Object Describe が失敗した
            理由。 ``None`` のときは特定・取得が成功している。

    Returns:
        列対応表の行のリスト。 各行は ``列キー`` / ``表示名`` /
        ``対応フィールドAPI名`` / ``型`` / ``備考`` のキーを持つ dict。
    """
    report_metadata = metadata.get("reportMetadata", {}) if isinstance(metadata, dict) else {}
    columns = collect_describable_columns(report_metadata)
    column_info = _collect_column_info(metadata)
    field_index = _build_field_index(main_describe) if main_describe is not None else None

    rows: list[dict[str, str]] = []
    for column_key in columns:
        info = column_info.get(column_key, {}) if isinstance(column_info, dict) else {}
        label = info.get("label", column_key) if isinstance(info, dict) else column_key
        row = {
            "列キー": column_key,
            "表示名": label,
            "対応フィールドAPI名": "(不明)",
            "型": "",
            "備考": "",
        }
        if field_index is None or reason is not None:
            row["備考"] = reason or ""
        else:
            matches = field_index.get(_normalize_label(label), [])
            if len(matches) == 1:
                field = matches[0]
                row["対応フィールドAPI名"] = field["name"]
                row["型"] = field["type"]
            elif len(matches) > 1:
                candidates = ", ".join(f"{field['name']}({field['type']})" for field in matches)
                row["備考"] = f"複数候補あり: {candidates}"
            else:
                row["備考"] = "対応フィールドなし"
        rows.append(row)
    return rows
