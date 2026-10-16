r"""1 レポート 1 つの Markdown 資料を組み立て、 ``OUTPUT_DIR/SOQL資料/`` に書く。

SOQL は人が AI に組ませる方針で、 AI に渡すのは Markdown の方が読みやすい。
そこで既存の ``SOQL調査.xlsx`` （人が一覧で見る） と ``列名の対応.csv``
（ダウンローダー用） に加え、 レポート 1 本につき 1 つの Markdown を
``OUTPUT_DIR/SOQL資料/{管理番号}.md`` に書く。

- 中身は ``build_markdown()`` （純粋関数: ``ReportRecord`` → 文字列）。
  ``ReportRecord`` の中身だけを使い、 Salesforce には繋がない
- 書き出しは ``write_ai_markdowns()`` （record 群 → ファイル） と
  ``write_ai_markdown()`` （1 record） 。 エンコードは UTF-8、 改行は LF、
  BOM なし
- 毎回「今回取れたレポートの分だけ」 書き直す。 失敗したレポートの古い
  ファイルや、 今回取れなかった管理番号のファイルは消さない
- 個人情報・レポート行データは、 列の定義・条件の値だけしか載せない
  （絞り込みの値もレポート定義に書かれた文字列をそのまま出す）
- Markdown 表のセルは ``|`` と改行をエスケープ （``\|`` / 空白） する
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from src import mapping, relation_diagram
from src.text_utils import coerce_str as _coerce_str

if TYPE_CHECKING:
    from src.fetch import RelationRecord, ReportRecord

logger = logging.getLogger(__name__)

# Markdown を配置するサブディレクトリ名
MARKDOWN_SUBDIR = "SOQL資料"

# Markdown 出力のエンコードと改行
_ENCODING = "utf-8"
_NEWLINE = "\n"

# 絞り込み「集計レポート」 判定に使うレポート形式
_AGGREGATE_FORMATS: frozenset[str] = frozenset({"SUMMARY", "MATRIX"})

# ``scope`` の 「自分のレコード」 判定に使う値
_SCOPE_MY = "my"


def write_ai_markdowns(
    output_dir: Any,
    records: Sequence[ReportRecord],
    *,
    similar_lookup: dict[str, tuple[int, str, str]] | None = None,
    diagrams: dict[str, relation_diagram.Diagram] | None = None,
) -> tuple[Any, ...]:
    """``records`` の各 ``ReportRecord`` について
    ``output_dir/SOQL資料/{key}.md`` を書く。

    ``diagrams`` は ``{主オブジェクト: Diagram}``（表を作るときに 1 回組んだもの）。
    渡せばレポートごとに関係図を組み直さない。

    ``record`` が ``None`` のもの （失敗したもの） は書かない。 既存の
    ファイル （今回取れなかった管理番号のファイルや、 古い失敗ファイル） は
    そのまま残す。 書き出し先ディレクトリが無ければ作る
    """
    target_dir = output_dir / MARKDOWN_SUBDIR
    target_dir.mkdir(parents=True, exist_ok=True)
    wrote: list[Any] = []
    for record in records:
        diagram = (diagrams or {}).get(record.main_object or "")
        path = write_ai_markdown(target_dir, record, similar_lookup=similar_lookup, diagram=diagram)
        if path is not None:
            wrote.append(path)
    if wrote:
        logger.info("Markdown を書き出し: %s（%d 件）", target_dir, len(wrote))
    return tuple(wrote)


def write_ai_markdown(
    target_dir: Any,
    record: ReportRecord,
    *,
    similar_lookup: dict[str, tuple[int, str, str]] | None = None,
    diagram: relation_diagram.Diagram | None = None,
) -> Any | None:
    """1 record ぶんの Markdown を ``target_dir/{key}.md`` に書く。"""
    key = record.key
    if not key:
        return None
    path = target_dir / f"{key}.md"
    content = build_markdown(record, similar_lookup=similar_lookup, diagram=diagram)
    path.write_text(content, encoding=_ENCODING, newline=_NEWLINE)
    return path


def build_markdown(
    record: ReportRecord,
    *,
    similar_lookup: dict[str, tuple[int, str, str]] | None = None,
    diagram: relation_diagram.Diagram | None = None,
) -> str:
    """1 record ぶんの Markdown 文字列を組み立てる （純粋関数） 。

    ``record`` は ``ReportRecord`` 互換 （ ``key`` / ``summary`` /
    ``report_id`` / ``report`` / ``main_object`` / ``column_map`` /
    ``relations`` / ``warnings`` / ``row_check`` / ``pii`` を持つ ） 。
    属性が無いものは空として扱うので、 壊れた record でも ``AttributeError``
    で落ちない。 個人情報やレポート行データは **列の定義・条件の値だけ** で、
    レポートの行データは載せない

    ``similar_lookup`` は ``{管理番号: (group_number, base_key, integration)}`` 。
    ``tables.run_tables`` が ``similar.find_similar_groups`` の結果から組み立てる。
    ``None`` のときは 「似たレポート」 セクションは 「なし」 だけ出す
    """
    metadata = _report_metadata(record)
    fmt = metadata.get("reportFormat", "")
    column_map = record.column_map
    column_map_by_key = _column_map_by_key(column_map)
    detail_columns = metadata.get("detailColumns", [])

    parts: list[str] = []

    # AI への案内: 先頭で必ず伝える
    parts.append(_ai_instruction_header())
    parts.append("")

    # タイトル
    parts.append(_title(record))
    parts.append("")

    # ヘッダー （5 行のメタ）
    parts.append(f"- レポートID: {record.report_id}")
    parts.append(f"- 主オブジェクト: {_format_main_object(record)}")
    parts.append(f"- レポート形式: {fmt}")
    parts.append(f"- 2000件超: {_format_row_check(record)}")
    parts.append(f"- 個人情報: {_format_pii(record)}")
    parts.append("")

    # 列の対応
    parts.append("## 列の対応")
    correspondence_rows = _build_correspondence_rows(detail_columns, column_map_by_key)
    if correspondence_rows:
        parts.append("| 表示名 | 使う SOQL 列名 | どこから | 候補 |")
        parts.append("| --- | --- | --- | --- |")
        for row in correspondence_rows:
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
    sort_entries = _build_sort_entries(metadata.get("sortBy", []))
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

    # 似たレポート
    parts.append("## 似たレポート")
    similar_text = _format_similar(similar_lookup, key=record.key)
    parts.append(similar_text)
    parts.append("")

    # 警告
    parts.append("## 警告")
    warning_lines = _build_warning_lines(record)
    if warning_lines:
        for line in warning_lines:
            parts.append(f"- {line}")
    else:
        parts.append("なし")
    parts.append("")

    # 関係図ファイルへの相対リンクと、 字下げ版のツリーを添える
    # 表を作るときに組んだ関係図があればそれを使い、 単独で書くときだけ組む
    diagram_tree = (
        tuple(diagram.render_tree_lines())
        if diagram is not None
        else relation_diagram.render_inline_tree_for_record(record)
    )
    if diagram_tree:
        parts.append("関係図 （Mermaid）: ")
        main_object = record.main_object or ""
        if main_object:
            parts.append(f"../関係図/{main_object}.md")
        parts.append("")
        parts.append("```")
        parts.extend(diagram_tree)
        parts.append("```")
        parts.append("")

    # SOQL の下書き
    soql_section = _build_soql_section(record)
    if soql_section:
        parts.extend(soql_section)

    # 注意
    notes = _build_notes(record, metadata)
    if notes:
        parts.append("## SOQL を組むときの注意")
        parts.append("")
        parts.extend(notes)

    # 末尾の "\n" を入れない（1 ファイル LF 1 個で終端）
    return "\n".join(parts)


# ── ヘッダー行の整形 ───────────────────────────────────────────────────────


def _ai_instruction_header() -> str:
    """ファイル先頭に置く、 AI への案内文。

    この Markdown を丸ごと AI に貼って、 SOQL の下書きを直してもらうための
    1 行メッセージ。 先頭から変える必要があるので、 専用関数にしておく
    """
    return (
        "> このファイルを丸ごと AI に貼ってください。"
        "SOQL の下書きを、下の資料と照らして直すよう頼みます。"
    )


def _title(record: ReportRecord) -> str:
    return f"# {record.key} {record.summary}"


def _format_main_object(record: ReportRecord) -> str:
    """主オブジェクトが取れなかったときは、 警告の先頭から理由を拾う。

    ``fetch._choose_main`` が ``candidate_failure_reason()`` で作った
    「レポートタイプ '...' から主オブジェクト候補 ...」 の文が
    ``warnings`` に乗っているので、 その 1 行を丸ごと理由として添える。
    該当が無ければ 「不明」 だけにする
    """
    main_object = record.main_object
    if main_object:
        return main_object
    for warning in record.warnings:
        if warning.startswith("レポートタイプ"):
            return f"不明（{warning}）"
    return "不明"


def _format_row_check(record: ReportRecord) -> str:
    """``row_check`` の判定を 「超えていない（N 件）」 の形に整える。

    ``status`` だけだと行数が分からないので、 AI が WHERE を組み立てる
    手掛かりになるよう件数も添える
    """
    row_check = record.row_check
    if row_check is None:
        return "未実施"
    status = row_check.status or ""
    rows = row_check.rows
    if status == "超えていない" and rows is not None:
        return f"超えていない（{rows}件）"
    return status or "未実施"


def _format_pii(record: ReportRecord) -> str:
    pii_result = record.pii
    if pii_result is None:
        return "なし"
    return pii_result.status or "なし"


# ── 列の対応 ───────────────────────────────────────────────────────────────


def _build_correspondence_rows(
    detail_columns: list[str],
    column_map_by_key: dict[str, dict[str, str]],
) -> list[list[str]]:
    """``detailColumns`` の順に ``(表示名, 使う SOQL 列名, どこから, 候補)`` の行を作る。

    使う SOQL 列名はそのまま SOQL に貼れる形 （``Owner.Name`` のようなドット記法）。
    どこから・候補は xlsx の列の対応シートと同じ判定（``mapping.column_source``）。
    """
    rows: list[list[str]] = []
    for col_key in detail_columns:
        col = column_map_by_key.get(col_key)
        if col is None:
            rows.append([col_key, "(不明)", "取れず", ""])
            continue
        rows.append(
            [
                col["表示名"] or col_key,
                col["対応フィールドAPI名"] or "(不明)",
                mapping.column_source(col),
                mapping.column_candidates(col),
            ]
        )
    return rows


# ── 似たレポート ───────────────────────────────────────────────────────────


def _format_similar(
    similar_lookup: dict[str, tuple[int, str, str]] | None,
    *,
    key: str,
) -> str:
    """``similar_lookup`` のこの record 分の情報を 1 行にまとめる。

    ``tables.run_tables`` が出す形式に合わせて、 「グループN: ...」 の
    文字列を返す。 ``key`` が無い/該当が無いときは 「なし」 にする
    """
    if not key or similar_lookup is None:
        return "なし"
    info = similar_lookup.get(key)
    if info is None:
        return "なし"
    group_number, base_key, _integration = info
    if base_key == key:
        return f"グループ{group_number}: {key} が基準"
    return f"グループ{group_number}: {base_key} が基準・{key} 違い"


# ── 警告 ────────────────────────────────────────────────────────────────────


def _build_warning_lines(record: ReportRecord) -> list[str]:
    """``record.warnings`` の各警告を 1 行ずつ取り出す。 空 / 無いときは空リスト"""
    return [w for w in record.warnings if w]


# ── 絞り込み ───────────────────────────────────────────────────────────────


def _build_filter_section(
    metadata: dict[str, Any],
    column_map_by_key: dict[str, dict[str, Any]],
) -> list[str]:
    """絞り込みセクションの本文を組み立てる。

    組立式 → 絞り込みテーブル → 日付 / 標準 / クロス / 上位N の箇条書き、 の順。
    何も無いときは 「なし」 1 行だけにする
    """
    lines: list[str] = []
    bool_filter = metadata.get("reportBooleanFilter", "")
    if bool_filter.strip():
        lines.append(f"組立式: {_inline_code(bool_filter)}")

    filter_rows = _build_filter_rows(metadata.get("reportFilters", []), column_map_by_key)
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
    report_filters: list[dict[str, Any]],
    column_map_by_key: dict[str, dict[str, str]],
) -> list[list[str]]:
    """``reportFilters`` の各行を ``(番号, 表示名, SOQL列名, 演算子, 値)`` に展開。"""
    rows: list[list[str]] = []
    for index, filt in enumerate(report_filters, start=1):
        col_key = filt.get("column", "")
        col = column_map_by_key.get(col_key, {})
        value = filt.get("value")
        rows.append(
            [
                str(index),
                col.get("表示名") or col_key,
                col.get("対応フィールドAPI名", ""),
                filt.get("operator", ""),
                "" if value is None else str(value),
            ]
        )
    return rows


def _build_filter_extras(metadata: dict[str, Any]) -> list[str]:
    """絞り込みのうちテーブル外の日付・標準フィルタ・クロス・上位N を箇条書きで出す。

    該当が無い項目は出さない （1 行も書かない）。 呼び出し側で
    「何も無いときは 『 なし 』」 を決める
    """
    lines: list[str] = []

    date_filter = metadata.get("standardDateFilter")
    if date_filter:
        column = date_filter.get("column", "")
        start = date_filter.get("startDate", "")
        end = date_filter.get("endDate", "")
        duration = date_filter.get("durationValue", "")
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


def _build_sort_entries(sort_by: list[dict[str, Any]]) -> list[str]:
    """``sortBy`` を ``- {sortColumn} {sortOrder}`` の箇条書きにする。"""
    return [
        f"- {entry.get('sortColumn', '')}"
        + (f" {entry['sortOrder']}" if entry.get("sortOrder") else "")
        for entry in sort_by
    ]


# ── グループ・集計 ─────────────────────────────────────────────────────────


def _build_grouping_aggregate_entries(metadata: dict[str, Any]) -> list[str]:
    """SUMMARY / MATRIX の行グループ・列グループ・集計・バケット・独自計算式を箇条書きにする。

    0 件のときは空リストを返す （呼び出し側で 「SOQL では ...」 だけにする）
    """
    lines: list[str] = []

    for key, label in (("groupingsDown", "行グループ"), ("groupingsAcross", "列グループ")):
        lines.extend(
            f"- {label}: {_format_grouping(grouping)}" for grouping in metadata.get(key, [])
        )
    lines.extend(f"- 集計: {agg}" for agg in metadata.get("aggregates", []))

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
    detail = [
        value for value in (grouping.get("sortOrder"), grouping.get("dateGranularity")) if value
    ]
    name = grouping.get("name", "")
    return f"{name} ({', '.join(detail)})" if detail else name


# ── 関連オブジェクトの経路 ─────────────────────────────────────────────────


def _build_used_chain_lines(record: ReportRecord) -> list[str]:
    """主オブジェクトから、 出力列・絞り込みで使っている経路だけを箇条書きにする。

    ``detailColumns`` と ``reportFilters`` のうち ``column_map`` で **解決
    できた multi-hop の 対応フィールドAPI名** だけを材料にする。 経路が無い
    ときは空リスト
    """
    main_object = record.main_object
    if not main_object:
        return []

    metadata = _report_metadata(record)
    column_map_by_key = _column_map_by_key(record.column_map)

    filter_keys = [filt.get("column", "") for filt in metadata.get("reportFilters", [])]
    used_keys = [
        key for key in dict.fromkeys(metadata.get("detailColumns", []) + filter_keys) if key
    ]

    rel_edges = _build_relation_edges(record.relations)

    chains_by_object: dict[str, list[tuple[str, str]]] = {}
    for col_key in used_keys:
        soql = column_map_by_key.get(col_key, {}).get("対応フィールドAPI名", "")
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


def _build_relation_edges(
    relations: tuple[RelationRecord, ...],
) -> dict[str, list[tuple[str, str]]]:
    """``RelationRecord`` のリストを ``parent -> [(rel_name, child), ...]`` に整える。

    同じ ``(parent, rel_name)`` の重複は入力順で先頭だけ残す
    """
    edges: dict[str, list[tuple[str, str]]] = {}
    seen: set[tuple[str, str]] = set()
    for rel in relations:
        parent = rel.parent
        child = rel.child
        rel_name = rel.relationship_name
        if not (parent and child):
            continue
        name = rel_name or ""
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

    1 段でもリレーションが見つからなければ ``None`` （経路は採用しない）
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


# ── SOQL の下書き ───────────────────────────────────────────────────────


def _build_soql_section(record: ReportRecord) -> list[str]:
    """``record.soql_draft`` から ``## SOQL の下書き`` セクションを組み立てる。

    AI が下書きを直しやすいように、 状態・SOQL本文（印付き） ・作れなかった
    部分・要確認をそのまま載せる。 ``soql_draft`` が無い record は空リスト
    （セクションを出さない） 。 ``作れず`` は本文が無いので状態・行だけ。
    """
    draft = record.soql_draft
    if draft is None:
        return []

    lines: list[str] = ["## SOQL の下書き", ""]
    lines.append(f"状態: {draft.status or '(不明)'}")
    lines.append("")
    if draft.soql:
        lines.append("```sql")
        lines.append(draft.soql)
        lines.append("```")
        lines.append("")
        lines.append(
            "この下書きを、下の資料と照らして直してください。"
            "作れなかった部分は資料から補ってください。"
        )
    else:
        # ``作れず`` は本文が無いので、 「なし」 の代わりに理由を残す
        lines.append("SOQL: なし")
    lines.append("")

    for heading, items in (("作れなかった部分", draft.missing), ("要確認", draft.checks)):
        if items:
            lines.append(f"{heading}:")
            lines.append("")
            lines.extend(f"- {item}" for item in items)
            lines.append("")
    return lines


# ── 注意 ───────────────────────────────────────────────────────────────────


def _build_notes(record: ReportRecord, metadata: dict[str, Any]) -> list[str]:
    """``SOQL を組むときの注意`` セクションの箇条書きを組み立てる。

    当てはまるものだけを出す。 1 つもなければ空リスト
    """
    notes: list[str] = []

    has_unresolved = any(col["対応フィールドAPI名"] == "(不明)" for col in record.column_map)
    has_multiple = any("複数候補あり" in col["備考"] for col in record.column_map)

    if has_unresolved:
        notes.append("- 取れなかった列がある")
    if has_multiple:
        notes.append("- 複数候補の列がある")

    if metadata.get("scope") == _SCOPE_MY:
        notes.append("- スコープが自分のレコード（scope）")

    if metadata.get("standardDateFilter"):
        notes.append("- 日時の条件はタイムゾーンに注意")

    row_check = record.row_check
    if row_check is not None and row_check.status == "超えている":
        notes.append("- 2000件超なので Bulk で取る")

    cross_filters = metadata.get("crossFilters")
    if isinstance(cross_filters, list) and cross_filters:
        notes.append("- クロスフィルタはサブクエリ")

    if metadata.get("reportFormat", "") in _AGGREGATE_FORMATS:
        notes.append("- 集計レポート")

    return notes


# ── 共通ヘルパー ───────────────────────────────────────────────────────────


def _report_metadata(record: ReportRecord) -> dict[str, Any]:
    """``record.report.reportMetadata``（入口で形をそろえてある）を取り出す。"""
    return record.report["reportMetadata"]


def _column_map_by_key(column_map: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    return {col["列キー"]: col for col in column_map}


def _md_table_row(cells: Sequence[str]) -> str:
    """``["a", "b|c", "c"]`` を ``"| a | b\\|c | c |"`` に整形する。"""
    escaped = [_escape_cell(str(cell) if cell is not None else "") for cell in cells]
    return "| " + " | ".join(escaped) + " |"


def _escape_cell(value: str) -> str:
    """Markdown 表のセルで ``|`` と改行をエスケープする。

    ``|`` は ``\\|`` 、 改行は空白 1 個に置き換える。 ``\\`` の二重エスケープ
    はしない （仕様の 「``|`` を ``\\|``」 をそのまま） 。
    """
    escaped = value.replace("|", "\\|")
    escaped = escaped.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    return escaped


def _inline_code(text: str) -> str:
    """インラインコードのバッククオート表記を返す。

    値そのものにバッククオートが含まれるとき （壊れたデータを防御） は
    ダブルバッククオートで囲む
    """
    if "`" in text:
        return f"`` {text} ``"
    return f"`{text}`"


def _format_top_rows(top_rows: dict[str, Any]) -> str:
    """``reportMetadata.topRows``（入口でそろえてある）を ``"10件 Asc"`` のように整形する。"""
    parts: list[str] = []
    if top_rows.get("rowLimit"):
        parts.append(f"{top_rows['rowLimit']}件")
    if top_rows.get("direction"):
        parts.append(top_rows["direction"])
    return " ".join(parts)
