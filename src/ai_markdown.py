r"""1 レポート 1 つの Markdown 資料を組み立て、 ``OUTPUT_DIR/SOQL資料/`` に書く。

SOQL は人が AI に組ませる方針で、 AI に渡すのは Markdown の方が読みやすい。
そこで既存の ``SOQL調査.xlsx`` （ 人が一覧で見る ） と ``列名の対応.csv``
（ ダウンローダー用 ） に加え、 レポート 1 本につき 1 つの Markdown を
``OUTPUT_DIR/SOQL資料/{管理番号}.md`` に書く。

- 中身は ``build_markdown()`` （ 純粋関数: ``ReportRecord`` → 文字列 ）。
  ``ReportRecord`` の中身だけを使い、 Salesforce には繋がない
- 書き出しは ``write_ai_markdowns()`` （ record 群 → ファイル ） と
  ``write_ai_markdown()`` （ 1 record ） 。 エンコードは UTF-8、 改行は LF、
  BOM なし
- 毎回「 今回取れたレポートの分だけ 」 書き直す。 失敗したレポートの古い
  ファイルや、 今回取れなかった管理番号のファイルは消さない
- 個人情報・レポート行データは、 列の定義・条件の値だけしか載せない
  （ 絞り込みの値もレポート定義に書かれた文字列をそのまま出す ）
- Markdown 表のセルは ``|`` と改行をエスケープ （ ``\|`` / 空白 ） する
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from typing import Any

logger = logging.getLogger(__name__)

# Markdown を配置するサブディレクトリ名
MARKDOWN_SUBDIR = "SOQL資料"

# Markdown 出力のエンコードと改行
_ENCODING = "utf-8"
_NEWLINE = "\n"

# 絞り込み「 集計レポート 」 判定に使うレポート形式
_AGGREGATE_FORMATS: frozenset[str] = frozenset({"SUMMARY", "MATRIX"})

# ``scope`` の 「 自分のレコード 」 判定に使う値
_SCOPE_MY = "my"


def write_ai_markdowns(output_dir: Any, records: Sequence[Any]) -> tuple[Any, ...]:
    """``records`` の各 ``ReportRecord`` について
    ``output_dir/SOQL資料/{key}.md`` を書く。

    ``record`` が ``None`` のもの （ 失敗したもの ） は書かない。 既存の
    ファイル （ 今回取れなかった管理番号のファイルや、 古い失敗ファイル ） は
    そのまま残す。 書き出し先ディレクトリが無ければ作る
    """
    target_dir = output_dir / MARKDOWN_SUBDIR
    target_dir.mkdir(parents=True, exist_ok=True)
    wrote: list[Any] = []
    for record in records:
        if record is None:
            continue
        path = write_ai_markdown(target_dir, record)
        if path is not None:
            wrote.append(path)
    if wrote:
        logger.info("Markdown を書き出し: %s（ %d 件 ）", target_dir, len(wrote))
    return tuple(wrote)


def write_ai_markdown(target_dir: Any, record: Any) -> Any | None:
    """1 record ぶんの Markdown を ``target_dir/{key}.md`` に書く。"""
    key = getattr(record, "key", "")
    if not isinstance(key, str) or not key:
        return None
    path = target_dir / f"{key}.md"
    content = build_markdown(record)
    path.write_text(content, encoding=_ENCODING, newline=_NEWLINE)
    return path


def build_markdown(record: Any) -> str:
    """1 record ぶんの Markdown 文字列を組み立てる （ 純粋関数 ） 。

    ``record`` は ``ReportRecord`` 互換 （ ``key`` / ``summary`` /
    ``report_id`` / ``report`` / ``main_object`` / ``column_map`` /
    ``relations`` / ``warnings`` / ``row_check`` / ``pii`` を持つ ） 。
    属性が無いものは空として扱うので、 壊れた record でも ``AttributeError``
    で落ちない。 個人情報やレポート行データは **列の定義・条件の値だけ** で、
    レポートの行データは載せない
    """
    metadata = _report_metadata(record)
    fmt = _format_of(metadata)
    column_map = _list_of(getattr(record, "column_map", None))
    column_map_by_key = _column_map_by_key(column_map)
    detail_columns = _list_of(metadata.get("detailColumns"))

    parts: list[str] = []

    # タイトル
    parts.append(_title(record))
    parts.append("")

    # ヘッダー （ 5 行のメタ ）
    parts.append(f"- レポートID: {_text(getattr(record, 'report_id', ''))}")
    parts.append(f"- 主オブジェクト: {_format_main_object(record)}")
    parts.append(f"- レポート形式: {_text(fmt)}")
    parts.append(f"- 2000件超: {_format_row_check(record)}")
    parts.append(f"- 個人情報: {_format_pii(record)}")
    parts.append("")

    # 出力する列
    parts.append("## 出力する列")
    output_rows = _build_output_rows(detail_columns, column_map_by_key)
    if output_rows:
        parts.append("| 表示名 | SOQL列名 | 型 | 備考 |")
        parts.append("| --- | --- | --- | --- |")
        for row in output_rows:
            parts.append(_md_table_row(row))
    else:
        parts.append("なし")
    parts.append("")

    # 絞り込み
    parts.append("## 絞り込み")
    parts.extend(_build_filter_section(metadata, column_map_by_key))
    parts.append("")

    # 並び順
    parts.append("## 並び順")
    sort_entries = _build_sort_entries(metadata.get("sortBy"))
    if sort_entries:
        parts.extend(sort_entries)
    else:
        parts.append("なし")
    parts.append("")

    # グループ・集計
    parts.append("## グループ・集計")
    if fmt == "TABULAR":
        parts.append("なし")
    else:
        parts.append("SOQL では GROUP BY と集計関数で表すか、 明細を取って集計する")
        parts.append("")
        for line in _build_grouping_aggregate_entries(metadata):
            parts.append(line)
    parts.append("")

    # 関連オブジェクトの経路
    parts.append("## 関連オブジェクトの経路")
    chains = _build_used_chain_lines(record)
    if chains:
        parts.extend(chains)
    else:
        parts.append("なし")
    parts.append("")

    # 注意
    notes = _build_notes(record, metadata)
    if notes:
        parts.append("## SOQL を組むときの注意")
        parts.append("")
        parts.extend(notes)

    # 末尾の "\n" を入れない（ 1 ファイル LF 1 個で終端 ）
    return "\n".join(parts)


# ── ヘッダー行の整形 ───────────────────────────────────────────────────────


def _title(record: Any) -> str:
    return f"# {_text(getattr(record, 'key', ''))} {_text(getattr(record, 'summary', ''))}"


def _format_main_object(record: Any) -> str:
    """主オブジェクトが取れなかったときは、 警告の先頭から理由を拾う。

    ``fetch._choose_main`` が ``candidate_failure_reason()`` で作った
    「 レポートタイプ '...' から主オブジェクト候補 ... 」 の文が
    ``warnings`` に乗っているので、 その 1 行を丸ごと理由として添える。
    該当が無ければ 「 不明 」 だけにする
    """
    main_object = getattr(record, "main_object", None)
    if isinstance(main_object, str) and main_object:
        return main_object
    warnings = getattr(record, "warnings", ()) or ()
    for warning in warnings:
        if isinstance(warning, str) and warning.startswith("レポートタイプ"):
            return f"不明（{warning}）"
    return "不明"


def _format_row_check(record: Any) -> str:
    """``row_check`` の判定を 「 超えていない（N 件 ） 」 の形に整える。

    ``status`` だけだと行数が分からないので、 AI が WHERE を組み立てる
    手掛かりになるよう件数も添える
    """
    row_check = getattr(record, "row_check", None)
    if row_check is None:
        return "未実施"
    status = getattr(row_check, "status", "") or ""
    rows = getattr(row_check, "rows", None)
    if status == "超えていない" and isinstance(rows, int):
        return f"超えていない（{rows}件）"
    return status or "未実施"


def _format_pii(record: Any) -> str:
    pii_result = getattr(record, "pii", None)
    if pii_result is None:
        return "なし"
    status = getattr(pii_result, "status", "") or "なし"
    return status


# ── 出力する列 ─────────────────────────────────────────────────────────────


def _build_output_rows(
    detail_columns: list[Any],
    column_map_by_key: dict[str, dict[str, Any]],
) -> list[list[str]]:
    """``detailColumns`` の順に ``(表示名, SOQL列名, 型, 備考)`` の行を作る。

    ``column_map`` に対応が無い列 （ 通常起こらないが壊れた record 用に防御 ）
    は表示名を列キーそのまま、 SOQL列名は ``"(不明)"`` にする
    """
    rows: list[list[str]] = []
    for col_key in detail_columns:
        if not isinstance(col_key, str) or not col_key:
            continue
        col = column_map_by_key.get(col_key)
        if not isinstance(col, dict):
            col = {}
        label = _coerce_str(col.get("表示名")) or col_key
        soql = _coerce_str(col.get("対応フィールドAPI名")) or "(不明)"
        field_type = _coerce_str(col.get("型"))
        note = _coerce_str(col.get("備考"))
        rows.append([label, soql, field_type, note])
    return rows


# ── 絞り込み ───────────────────────────────────────────────────────────────


def _build_filter_section(
    metadata: dict[str, Any],
    column_map_by_key: dict[str, dict[str, Any]],
) -> list[str]:
    """絞り込みセクションの本文を組み立てる。

    組立式 → 絞り込みテーブル → 日付 / 標準 / クロス / 上位N の箇条書き、 の順。
    何も無いときは 「 なし 」 1 行だけにする
    """
    lines: list[str] = []
    bool_filter = metadata.get("reportBooleanFilter")
    if isinstance(bool_filter, str) and bool_filter.strip():
        lines.append(f"組立式: {_inline_code(bool_filter)}")

    filter_rows = _build_filter_rows(metadata.get("reportFilters"), column_map_by_key)
    if filter_rows:
        lines.append("| 番号 | 表示名 | SOQL列名 | 演算子 | 値 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for row in filter_rows:
            lines.append(_md_table_row(row))

    extras = _build_filter_extras(metadata)
    if extras:
        lines.extend(extras)

    if not lines:
        lines.append("なし")
    return lines


def _build_filter_rows(
    report_filters: Any,
    column_map_by_key: dict[str, dict[str, Any]],
) -> list[list[str]]:
    """``reportFilters`` の各行を ``(番号, 表示名, SOQL列名, 演算子, 値)`` に展開。"""
    rows: list[list[str]] = []
    if not isinstance(report_filters, list):
        return rows
    for index, filt in enumerate(report_filters, start=1):
        if not isinstance(filt, dict):
            continue
        col_key = filt.get("column")
        col_key_text = col_key if isinstance(col_key, str) else ""
        col = column_map_by_key.get(col_key_text) if col_key_text else None
        if not isinstance(col, dict):
            col = {}
        label = _coerce_str(col.get("表示名")) or col_key_text
        soql = _coerce_str(col.get("対応フィールドAPI名"))
        operator = _coerce_str(filt.get("operator"))
        value = filt.get("value")
        value_text = "" if value is None else str(value)
        rows.append([str(index), label, soql, operator, value_text])
    return rows


def _build_filter_extras(metadata: dict[str, Any]) -> list[str]:
    """絞り込みのうちテーブル外の日付・標準フィルタ・クロス・上位N を箇条書きで出す。

    該当が無い項目は出さない （ 1 行も書かない ）。 呼び出し側で
    「 何も無いときは 『 なし 』 」 を決める
    """
    lines: list[str] = []

    date_filter = metadata.get("standardDateFilter")
    if isinstance(date_filter, dict) and date_filter:
        column = _coerce_str(date_filter.get("column"))
        start = _coerce_str(date_filter.get("startDate"))
        end = _coerce_str(date_filter.get("endDate"))
        duration = _coerce_str(date_filter.get("durationValue"))
        range_text = f"{start}〜{end}"
        if duration:
            range_text += f" ({duration})"
        lines.append(f"- 日付 {column}: {range_text}")

    standard_filters = metadata.get("standardFilters")
    if isinstance(standard_filters, list):
        for sf in standard_filters:
            if not isinstance(sf, dict):
                continue
            lines.append(f"- 標準フィルタ: {json.dumps(sf, ensure_ascii=False)}")

    cross_filters = metadata.get("crossFilters")
    if isinstance(cross_filters, list):
        for cf in cross_filters:
            if not isinstance(cf, dict):
                continue
            primary = _coerce_str(cf.get("primaryEntityField"))
            operator = _coerce_str(cf.get("operator"))
            related = _coerce_str(cf.get("relatedEntity"))
            join_field = _coerce_str(cf.get("relatedEntityJoinField"))
            criteria = cf.get("criteria")
            if isinstance(criteria, str):
                criteria_text = criteria
            elif criteria:
                criteria_text = json.dumps(criteria, ensure_ascii=False)
            else:
                criteria_text = ""
            value_text = f"{primary} {operator} {related}.{join_field}"
            if criteria_text:
                value_text += f" ({criteria_text})"
            lines.append(f"- クロスフィルタ: {value_text}")

    top_rows = metadata.get("topRows")
    if top_rows:
        lines.append(f"- 上位N: {_format_top_rows(top_rows)}")

    return lines


# ── 並び順 ─────────────────────────────────────────────────────────────────


def _build_sort_entries(sort_by: Any) -> list[str]:
    """``sortBy`` を ``- {sortColumn} {sortOrder}`` の箇条書きにする。"""
    lines: list[str] = []
    if not isinstance(sort_by, list):
        return lines
    for entry in sort_by:
        if not isinstance(entry, dict):
            continue
        column = _coerce_str(entry.get("sortColumn"))
        order = _coerce_str(entry.get("sortOrder"))
        text = column
        if order:
            text += f" {order}"
        lines.append(f"- {text}")
    return lines


# ── グループ・集計 ─────────────────────────────────────────────────────────


def _build_grouping_aggregate_entries(metadata: dict[str, Any]) -> list[str]:
    """SUMMARY / MATRIX の行グループ・列グループ・集計・バケット・独自計算式を箇条書きにする。

    0 件のときは空リストを返す （ 呼び出し側で 「 SOQL では ... 」 だけにする ）
    """
    lines: list[str] = []

    for key, label in (("groupingsDown", "行グループ"), ("groupingsAcross", "列グループ")):
        groupings = metadata.get(key)
        if isinstance(groupings, list):
            for grouping in groupings:
                if not isinstance(grouping, dict):
                    continue
                lines.append(f"- {label}: {_format_grouping(grouping)}")

    aggregates = metadata.get("aggregates")
    if isinstance(aggregates, list):
        for agg in aggregates:
            if not isinstance(agg, str):
                continue
            lines.append(f"- 集計: {agg}")

    buckets = metadata.get("buckets")
    if isinstance(buckets, list):
        for bucket in buckets:
            if not isinstance(bucket, dict):
                continue
            name = _coerce_str(bucket.get("developerName")) or _coerce_str(bucket.get("name"))
            label_value = _coerce_str(bucket.get("label"))
            bucket_type = _coerce_str(bucket.get("bucketType"))
            detail_parts: list[str] = []
            if label_value:
                detail_parts.append(label_value)
            if bucket_type:
                detail_parts.append(f"({bucket_type})")
            if detail_parts:
                lines.append(f"- バケット: {name} - {' '.join(detail_parts)}")
            else:
                lines.append(f"- バケット: {name}")

    custom_summary_formula = metadata.get("customSummaryFormula")
    if isinstance(custom_summary_formula, list):
        for csf in custom_summary_formula:
            if not isinstance(csf, dict):
                continue
            formula = _coerce_str(csf.get("formula"))
            label_value = _coerce_str(csf.get("label"))
            if label_value:
                lines.append(f"- 独自計算式: {formula} ({label_value})")
            else:
                lines.append(f"- 独自計算式: {formula}")

    return lines


def _format_grouping(grouping: dict[str, Any]) -> str:
    name = _coerce_str(grouping.get("name"))
    order = _coerce_str(grouping.get("sortOrder"))
    granularity = _coerce_str(grouping.get("dateGranularity"))
    detail: list[str] = []
    if order:
        detail.append(order)
    if granularity:
        detail.append(granularity)
    if detail:
        return f"{name} ({', '.join(detail)})"
    return name


# ── 関連オブジェクトの経路 ─────────────────────────────────────────────────


def _build_used_chain_lines(record: Any) -> list[str]:
    """主オブジェクトから、 出力列・絞り込みで使っている経路だけを箇条書きにする。

    ``detailColumns`` と ``reportFilters`` のうち ``column_map`` で **解決
    できた multi-hop の 対応フィールドAPI名** だけを材料にする。 経路が無い
    ときは空リスト
    """
    main_object = getattr(record, "main_object", None)
    if not isinstance(main_object, str) or not main_object:
        return []

    metadata = _report_metadata(record)
    column_map_by_key = _column_map_by_key(_list_of(getattr(record, "column_map", None)))

    used_keys: list[str] = []
    for col_key in metadata.get("detailColumns") or []:
        if isinstance(col_key, str) and col_key and col_key not in used_keys:
            used_keys.append(col_key)
    report_filters = metadata.get("reportFilters")
    if isinstance(report_filters, list):
        for filt in report_filters:
            if not isinstance(filt, dict):
                continue
            col_key = filt.get("column")
            if isinstance(col_key, str) and col_key and col_key not in used_keys:
                used_keys.append(col_key)

    rel_edges = _build_relation_edges(getattr(record, "relations", ()) or ())

    chains_by_object: dict[str, list[tuple[str, str]]] = {}
    for col_key in used_keys:
        col = column_map_by_key.get(col_key)
        if not isinstance(col, dict):
            continue
        soql = _coerce_str(col.get("対応フィールドAPI名"))
        if not soql or soql == "(不明)":
            continue
        chain_parts = soql.split(".")[:-1]
        if not chain_parts:
            continue
        chain = _walk_chain(main_object, chain_parts, rel_edges)
        if chain is None or not chain:
            continue
        endpoint = chain[-1][1]
        if endpoint == main_object:
            continue
        existing = chains_by_object.get(endpoint)
        if existing is None or len(chain) < len(existing):
            chains_by_object[endpoint] = chain

    if not chains_by_object:
        return []

    lines: list[str] = []
    for _endpoint, chain in chains_by_object.items():
        pieces = [main_object]
        for rel_name, child in chain:
            pieces.append(f"{rel_name}（{child}）")
        lines.append(f"- {' → '.join(pieces)}")
    return lines


def _build_relation_edges(relations: Any) -> dict[str, list[tuple[str, str]]]:
    """``RelationRecord`` のリストを ``parent -> [(rel_name, child), ...]`` に整える。

    同じ ``(parent, rel_name)`` の重複は入力順で先頭だけ残す
    """
    edges: dict[str, list[tuple[str, str]]] = {}
    if not isinstance(relations, (list, tuple)):
        return edges
    seen: set[tuple[str, str]] = set()
    for rel in relations:
        parent = getattr(rel, "parent", None)
        child = getattr(rel, "child", None)
        rel_name = getattr(rel, "relationship_name", None)
        if not (isinstance(parent, str) and isinstance(child, str) and parent and child):
            continue
        name = rel_name if isinstance(rel_name, str) else ""
        key = (parent, name)
        if key in seen:
            continue
        seen.add(key)
        edges.setdefault(parent, []).append((name, child))
    return edges


def _walk_chain(
    main_object: str,
    rel_names: list[str],
    edges: dict[str, list[tuple[str, str]]],
) -> list[tuple[str, str]] | None:
    """``main_object`` から ``rel_names`` の順に hop した ``[(rel_name, child), ...]`` を返す。

    1 段でもリレーションが見つからなければ ``None`` （ 経路は採用しない ）
    """
    chain: list[tuple[str, str]] = []
    current = main_object
    for rel_name in rel_names:
        next_child: str | None = None
        for candidate_rel, candidate_child in edges.get(current, []):
            if candidate_rel == rel_name:
                next_child = candidate_child
                break
        if next_child is None:
            return None
        chain.append((rel_name, next_child))
        current = next_child
    return chain


# ── 注意 ───────────────────────────────────────────────────────────────────


def _build_notes(record: Any, metadata: dict[str, Any]) -> list[str]:
    """``SOQL を組むときの注意`` セクションの箇条書きを組み立てる。

    当てはまるものだけを出す。 1 つもなければ空リスト
    """
    notes: list[str] = []

    column_map = _list_of(getattr(record, "column_map", None))
    has_unresolved = False
    has_multiple = False
    for col in column_map:
        if not isinstance(col, dict):
            continue
        soql = _coerce_str(col.get("対応フィールドAPI名"))
        if soql == "(不明)":
            has_unresolved = True
        note_text = _coerce_str(col.get("備考"))
        if "複数候補あり" in note_text:
            has_multiple = True

    if has_unresolved:
        notes.append("- 取れなかった列がある")
    if has_multiple:
        notes.append("- 複数候補の列がある")

    if metadata.get("scope") == _SCOPE_MY:
        notes.append("- スコープが自分のレコード（scope）")

    if isinstance(metadata.get("standardDateFilter"), dict) and metadata.get("standardDateFilter"):
        notes.append("- 日時の条件はタイムゾーンに注意")

    row_check = getattr(record, "row_check", None)
    if row_check is not None and getattr(row_check, "status", "") == "超えている":
        notes.append("- 2000件超なので Bulk で取る")

    cross_filters = metadata.get("crossFilters")
    if isinstance(cross_filters, list) and cross_filters:
        notes.append("- クロスフィルタはサブクエリ")

    if _format_of(metadata) in _AGGREGATE_FORMATS:
        notes.append("- 集計レポート")

    return notes


# ── 共通ヘルパー ───────────────────────────────────────────────────────────


def _report_metadata(record: Any) -> dict[str, Any]:
    """``record.report.reportMetadata`` を取り出す。 壊れていれば空 dict。"""
    report = getattr(record, "report", None)
    if not isinstance(report, dict):
        return {}
    metadata = report.get("reportMetadata")
    return metadata if isinstance(metadata, dict) else {}


def _format_of(metadata: dict[str, Any]) -> str:
    value = metadata.get("reportFormat")
    return value if isinstance(value, str) else ""


def _list_of(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _column_map_by_key(column_map: list[Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for col in column_map:
        if not isinstance(col, dict):
            continue
        key = col.get("列キー")
        if isinstance(key, str) and key:
            result[key] = col
    return result


def _md_table_row(cells: Sequence[str]) -> str:
    """``["a", "b|c", "c"]`` を ``"| a | b\\|c | c |"`` に整形する。"""
    escaped = [_escape_cell(str(cell) if cell is not None else "") for cell in cells]
    return "| " + " | ".join(escaped) + " |"


def _escape_cell(value: str) -> str:
    """Markdown 表のセルで ``|`` と改行をエスケープする。

    ``|`` は ``\\|`` 、 改行は空白 1 個に置き換える。 ``\\`` の二重エスケープ
    はしない （ 仕様の 「 ``|`` を ``\\|`` 」 をそのまま ） 。
    """
    escaped = value.replace("|", "\\|")
    escaped = escaped.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    return escaped


def _inline_code(text: str) -> str:
    """インラインコードのバッククオート表記を返す。

    値そのものにバッククオートが含まれるとき （ 壊れたデータを防御 ） は
    ダブルバッククオートで囲む
    """
    if "`" in text:
        return f"`` {text} ``"
    return f"`{text}`"


def _format_top_rows(top_rows: Any) -> str:
    """``reportMetadata.topRows`` を ``"10件 Asc"`` のように整形する。

    ``tables._format_top_rows`` と同じ形だが、 Markdown 側でも独立に使うので
    同じ実装を小さい関数として置く
    """
    if not isinstance(top_rows, dict):
        return _coerce_str(top_rows)
    parts: list[str] = []
    row_limit = top_rows.get("rowLimit")
    if not isinstance(row_limit, bool) and (
        isinstance(row_limit, int) or (isinstance(row_limit, str) and row_limit)
    ):
        parts.append(f"{row_limit}件")
    direction = top_rows.get("direction")
    if isinstance(direction, str) and direction:
        parts.append(direction)
    return " ".join(parts)


def _coerce_str(value: Any) -> str:
    """``None`` を空文字に、 他の値はそのまま ``str`` にする。"""
    if value is None:
        return ""
    return str(value)


def _text(value: Any) -> str:
    """タイトルや箇条書きの値用。 ``None`` を空文字にする。"""
    if value is None:
        return ""
    return str(value)
