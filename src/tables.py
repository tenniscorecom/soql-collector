"""``ReportRecord`` のリストから、 人が Excel で見るブックとダウンローダー用
CSV を組み立てる。

入力は ``ReportRecord`` のイテラブル。 出力は同じ ``OUTPUT_DIR`` に次の
2 種類のファイルを書く:

- ``SOQL調査.xlsx`` … 人が見る 1 冊のブック。 毎回その回の結果で丸ごと作り
  直す。 シートは次の 9 枚 （ この順 ） :

  1. 一覧
  2. 取得できなかった項目
  3. 項目の対応
  4. 条件
  5. 集計
  6. 似たレポート
  7. 警告
  8. 項目一覧
  9. 関連

- ``列名の対応.csv`` … ダウンローダーが SOQL レポートの列名の置き換えに使う
  CSV。 全レポート分を積み上げる。 その回に取れた ``(主オブジェクト,
  SOQL列名)`` の行は今回書いた内容で入れ替わり、 他の行はそのまま残る。

``run_fetch`` が ``Record`` を 1 件も作れなかったときは ``SOQL調査.xlsx``
は作らず、 ``列名の対応.csv`` も何もしない （ 既存もそのまま ） 。

**SOQL はこのツールでは組み立てない** （ 2026-10-14 撤去 ）。 人は
``SOQL調査.xlsx`` の ``一覧`` ``条件`` ``集計`` ``項目の対応`` ``項目一覧``
``関連`` シートを AI に渡すなどして組み立てる。
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from comken.core.table import Table
from comken.exceptions import CSVError
from comken.toolbox.csv import CSV
from comken.toolbox.excel import Excel
from openpyxl.utils import get_column_letter

from src import ai_markdown, pii, relation_diagram, similar

logger = logging.getLogger(__name__)

# 1 つの CSV ファイルあたりの選択肢の最大値数。 超えたら末尾に ``…`` を付ける
PICKLIST_LIMIT = 30

# 「 複数候補あり 」 備考に並べる鎖+項目の最大件数
MULTIPLE_CANDIDATES_LIMIT = 5

# 警告 「 オブジェクト {name} の describe に失敗 … 」 を (name, reason) に分ける
DESCRIBE_FAILURE_PATTERN = re.compile(r"^オブジェクト (.+?) の describe に失敗(.*)$")

# 1 レポートの中で同じ表示名が複数回現れたときの 取得できなかった項目 シートの理由
DUPLICATE_LABEL_REASON = "同じレポート内で表示名が重複"

# xlsx / csv 出力ファイル名
XLSX_NAME = "SOQL調査.xlsx"
COLUMN_NAMES_CSV_NAME = "列名の対応.csv"


# ── シート名・列名定数 ────────────────────────────────────────────────────

# シート名 （ 表示順 ）
SHEET_OVERVIEW = "一覧"
SHEET_UNRESOLVED = "取得できなかった項目"
SHEET_RESOLVED = "項目の対応"
SHEET_CONDITION = "条件"
SHEET_AGGREGATE = "集計"
SHEET_SIMILAR = "似たレポート"
SHEET_WARNING = "警告"
SHEET_FIELDS = "項目一覧"
SHEET_RELATED = "関連"
SHEET_RELATION_DIAGRAM = "関係図"
SHEET_SOQL = "SOQL"

SHEET_NAMES_IN_ORDER: tuple[str, ...] = (
    SHEET_OVERVIEW,
    SHEET_UNRESOLVED,
    SHEET_RESOLVED,
    SHEET_CONDITION,
    SHEET_AGGREGATE,
    SHEET_SIMILAR,
    SHEET_WARNING,
    SHEET_FIELDS,
    SHEET_RELATED,
    SHEET_RELATION_DIAGRAM,
    SHEET_SOQL,
)

# 一覧シートの列名
SURVEY_COLUMNS: tuple[str, ...] = (
    "管理番号",
    "概要",
    "レポートID",
    "レポートタイプ",
    "主オブジェクト",
    "形式",
    "出力列数",
    "2000件超",
    "行数",
    "2000件超の補足",
    "個人情報",
    "個人情報の該当列",
    "SOQLの状態",
    "類似グループ",
    "統合の見込み",
    "取得できなかった項目数",
    "警告数",
)

# 取得できなかった項目シートの列名
UNRESOLVED_COLUMNS: tuple[str, ...] = (
    "種別",
    "主オブジェクト",
    "列キー",
    "表示名",
    "理由",
    "件数",
    "管理番号",
)

# 項目の対応シートの列名
RESOLVED_COLUMNS: tuple[str, ...] = (
    "主オブジェクト",
    "SOQL列名",
    "表示名",
    "型",
    "参照先オブジェクト",
    "個人情報",
    "個人情報の根拠",
    "使っているレポート数",
    "管理番号",
    "備考",
)

# 条件シートの列名
CONDITION_COLUMNS: tuple[str, ...] = (
    "管理番号",
    "概要",
    "レポートID",
    "種別",
    "番号",
    "列キー",
    "項目",
    "演算子",
    "値",
    "備考",
)

# 集計シートの列名
AGGREGATE_COLUMNS: tuple[str, ...] = (
    "管理番号",
    "概要",
    "レポートID",
    "種別",
    "順",
    "名前",
    "内容",
)

# 似たレポートシートの列名
SIMILAR_COLUMNS: tuple[str, ...] = (
    "類似グループ",
    "管理番号",
    "概要",
    "レポートID",
    "基準の管理番号",
    "区分",
    "基準との違い",
    "統合の見込み",
)

# 警告シートの列名 （ describe 失敗を除いた残り ）
WARNING_COLUMNS: tuple[str, ...] = (
    "管理番号",
    "概要",
    "レポートID",
    "警告",
)

# 項目一覧シートの列名
FIELD_COLUMNS: tuple[str, ...] = (
    "オブジェクト",
    "段",
    "オブジェクト表示名",
    "項目API名",
    "項目表示名",
    "型",
    "参照先オブジェクト",
    "リレーション名",
    "カスタム",
    "選択肢",
)

# 関連シートの列名
RELATED_COLUMNS: tuple[str, ...] = (
    "管理番号",
    "親オブジェクト",
    "参照項目API名",
    "リレーション名",
    "子オブジェクト",
    "段",
)

# 関係図シートの列名 （ 主オブジェクト単位の木を Excel で見やすくしたもの ）
RELATION_DIAGRAM_COLUMNS: tuple[str, ...] = (
    "主オブジェクト",
    "段",
    "図",
    "行き先オブジェクト",
    "SOQLでの書き方",
    "参照項目API名",
)

# SOQL シートの列名 （ 人が AI に渡す前にも、 そのまま実行前にも使える下書き ）
SOQL_COLUMNS: tuple[str, ...] = (
    "管理番号",
    "概要",
    "主オブジェクト",
    "状態",
    "SOQL",
    "作れなかった部分",
    "要確認",
)

# 列名の対応.csv の列名 （ ダウンローダー固定 ）
COLUMN_NAMES_COLUMNS: tuple[str, ...] = (
    "主オブジェクト",
    "SOQL列名",
    "表示名",
    "備考",
    "更新日",
)


@dataclass(frozen=True)
class TableOutcome:
    """tables 実行の結果。"""

    wrote: tuple[Path, ...]
    skipped: tuple[Path, ...]


@dataclass(frozen=True)
class _ResolvedField:
    """``(主オブジェクト, SOQL列名)`` で集約した 1 つの解決済みフィールド。

    同じ ``(主オブジェクト, SOQL列名)`` でもレポートごとに表示名が違うことが
    あるので、 表示名は最多 ( 同数なら管理番号の小さいレポートのもの ) を
    ``display_name`` に、 残りは ``other_label_note`` に 「 別の表示名あり: 」
    形式でまとめて持つ。
    """

    main_object: str
    soql_name: str
    display_name: str
    field_type: str
    reference_to: str
    pii_status: str
    pii_basis: str
    report_count: int
    keys: tuple[str, ...]
    other_label_note: str


def run_tables(
    output_dir: Path,
    records: Iterable[object],
    *,
    only_keys: Iterable[str] | None = None,
    pii_config: pii.PIIConfig | None = None,
    similar_threshold: float = 0.8,
) -> TableOutcome:
    """``records`` から ``SOQL調査.xlsx`` と ``列名の対応.csv`` を組み立てる。

    Args:
        output_dir: 出力フォルダ。 無ければ作る。 per-ID の CSV は作らない
            （ 1 ブックに集約するため ）。
        records: ``fetch.ReportRecord`` のイテラブル。
        only_keys: 互換性のために受け取るが今回は使われない （ per-ID CSV を
            作らないので、 全 record を 1 冊に集約する ） 。
        pii_config: PII 列を埋めるための設定。 ``None`` ならデフォルト。
        similar_threshold: 似たレポート判定の Jaccard 下限。

    Returns:
        ``TableOutcome``: 書いたファイル一覧。 ``skipped`` は常に空。

    Raises:
        FileNotFoundError: 投げない （ ``output_dir`` が無くても作る ）。
        CSVError: 既存の ``列名の対応.csv`` が見出し違い・壊れているなどで
            読み込めないとき。 既存ファイルは変更しない。
    """
    del only_keys  # per-ID CSV は作らないので無視
    record_list = list(records)
    pii_config = pii_config or pii.PIIConfig(
        keywords=pii.DEFAULT_KEYWORDS,
        person_objects=pii.DEFAULT_PERSON_OBJECTS,
    )

    sorted_records = sorted(record_list, key=lambda r: getattr(r, "key", ""))
    ok_keys = [getattr(r, "key", "") for r in sorted_records]

    # PII lookup を record 単位で組み立てる （ 解決済みフィールド用 ）
    pii_lookups: dict[str, dict[str, tuple[str, str]]] = {}
    for record in sorted_records:
        report = getattr(record, "report", {}) or {}
        objects = getattr(record, "objects", {}) or {}
        column_map = getattr(record, "column_map", []) or []
        pii_lookups[getattr(record, "key", "")] = _build_pii_lookup_for_record(
            report=report,
            column_map=column_map,
            objects=objects,
            config=pii_config,
        )

    # 似たレポート判定 （ 全体 ）
    similar_groups = similar.find_similar_groups(
        sorted_records, column_similarity_threshold=similar_threshold
    )
    key_to_group: dict[str, tuple[int, str, str]] = {}
    for group in similar_groups:
        base_integration = next(
            (p.integration for p in group.pairs if p.key_b == group.base_key),
            "1本にまとめられる",
        )
        key_to_group[group.base_key] = (group.group_number, group.base_key, base_integration)
        for pair in group.pairs:
            if pair.key_b != group.base_key:
                key_to_group[pair.key_b] = (
                    group.group_number,
                    group.base_key,
                    pair.integration,
                )

    # 解決済みフィールドの集約 （ 項目の対応 / 列名の対応.csv で再利用 ）
    resolved_fields = _aggregate_resolved_fields(sorted_records, pii_lookups)
    resolved_rows = _resolved_fields_to_sheet_rows(resolved_fields)

    # 取得できなかった項目の組み立て （ 項目 + オブジェクト ）
    unresolved_rows = _build_unresolved_rows_from_records(sorted_records)
    unresolved_counts: dict[str, int] = {}
    for row in unresolved_rows:
        for key in row["管理番号"].split("|"):
            if key:
                unresolved_counts[key] = unresolved_counts.get(key, 0) + 1

    # 各シートの元になる行を作る
    survey_rows = _build_survey_table_rows_from_records(
        sorted_records,
        similar_lookup=key_to_group,
        unresolved_counts=unresolved_counts,
    )
    condition_rows = _build_condition_table_rows_from_records(sorted_records)
    aggregate_rows = _build_aggregate_table_rows_from_records(sorted_records)
    similar_rows = (
        _build_similar_table_rows(sorted_records, similar_groups) if similar_groups else []
    )
    warning_rows = _build_warning_table_rows_from_records(sorted_records)
    field_rows = _build_field_table_rows_from_records(sorted_records)
    related_rows = _build_related_table_rows_from_records(sorted_records)
    # 関係図: 同じ主オブジェクトの ``relations`` が合わさった木
    diagrams = relation_diagram.build_main_object_diagrams(sorted_records)
    relation_diagram_rows = _build_relation_diagram_rows(diagrams)
    soql_rows = _build_soql_table_rows_from_records(sorted_records)

    wrote: list[Path] = []

    if sorted_records:
        xlsx_path = output_dir / XLSX_NAME
        _write_xlsx(
            xlsx_path,
            survey=survey_rows,
            unresolved=unresolved_rows,
            resolved=resolved_rows,
            condition=condition_rows,
            aggregate=aggregate_rows,
            similar=similar_rows,
            warning=warning_rows,
            field=field_rows,
            related=related_rows,
            relation_diagram=relation_diagram_rows,
            soql=soql_rows,
        )
        wrote.append(xlsx_path)
        logger.info(
            "Excel を書き出し: %s（ 一覧=%d / 取得できなかった項目=%d / "
            "項目の対応=%d / 条件=%d / 集計=%d / 似たレポート=%d / "
            "警告=%d / 項目一覧=%d / 関連=%d / 関係図=%d / SOQL=%d ）",
            xlsx_path,
            len(survey_rows),
            len(unresolved_rows),
            len(resolved_rows),
            len(condition_rows),
            len(aggregate_rows),
            len(similar_rows),
            len(warning_rows),
            len(field_rows),
            len(related_rows),
            len(relation_diagram_rows),
            len(soql_rows),
        )

    # 列名の対応.csv （ 積み上げ ）
    # record が無いときは 「 今回 record が来た管理番号 」 が無いので、
    # 既存ファイルに何もしない ( 追加も上書きもしない ) 。
    column_names_path = output_dir / COLUMN_NAMES_CSV_NAME
    if ok_keys:
        column_names_rows = _resolved_fields_to_csv_rows(resolved_fields, _today_local())
        _merge_column_names_csv(column_names_path, column_names_rows, ok_keys)
        logger.info(
            "CSV を書き出し: %s（ %d 行 ）",
            column_names_path,
            len(column_names_rows),
        )
    elif column_names_path.exists():
        # 既存ファイルがあるが無更新 ( 既存の (主, SOQL列名) はそのまま )
        logger.info(
            "CSV は既存を維持: %s（ record なしのため新規/置換なし ）",
            column_names_path,
        )
    if column_names_path.exists():
        wrote.append(column_names_path)

    # 1 レポート 1 つの Markdown 資料を ``SOQL資料/{管理番号}.md`` に書く。
    # AI に SOQL を組ませるときにそのまま渡す用途。 既存のファイルはそのまま
    # 残し、 今回取れた record の分だけ上書きする。 xlsx / csv の挙動は
    # 変えない
    wrote.extend(ai_markdown.write_ai_markdowns(output_dir, sorted_records))

    # 関係図の Markdown を ``OUTPUT_DIR/関係図/{主オブジェクト}.md`` に書く。
    # 既存のファイルはそのまま残し、 今回取れた主オブジェクト分だけ上書きする
    wrote.extend(relation_diagram.write_main_object_diagrams(output_dir, diagrams))

    return TableOutcome(wrote=tuple(wrote), skipped=())


# ── 一覧 ──────────────────────────────────────────────────────────────


def _extract_report_type(report: object) -> str:
    """``record.report.reportMetadata.reportType.type`` を取り出す。 無ければ空文字。"""
    if not isinstance(report, dict):
        return ""
    report_metadata = report.get("reportMetadata")
    if not isinstance(report_metadata, dict):
        return ""
    report_type = report_metadata.get("reportType")
    if not isinstance(report_type, dict):
        return ""
    name = report_type.get("type")
    return name if isinstance(name, str) else ""


def _extract_report_format(report: object) -> str:
    """``record.report.reportMetadata.reportFormat`` を取り出す。 無ければ空文字。"""
    if not isinstance(report, dict):
        return ""
    metadata = report.get("reportMetadata")
    if not isinstance(metadata, dict):
        return ""
    value = metadata.get("reportFormat")
    return value if isinstance(value, str) else ""


def _output_column_keys_from_slim(slim_report: object) -> list[str]:
    """``slim_report`` の出力列キー （ 順序付き ） を集める。"""
    metadata = slim_report.get("reportMetadata") if isinstance(slim_report, dict) else None
    if not isinstance(metadata, dict):
        return []
    keys: list[str] = []
    for column in metadata.get("detailColumns") or []:
        if isinstance(column, str) and column:
            keys.append(column)
    for grouping_key in ("groupingsDown", "groupingsAcross"):
        for grouping in metadata.get(grouping_key) or []:
            if isinstance(grouping, dict):
                name = grouping.get("name")
                if isinstance(name, str) and name:
                    keys.append(name)
    for aggregate in metadata.get("aggregates") or []:
        if isinstance(aggregate, str) and "!" in aggregate:
            column = aggregate.split("!", 1)[-1]
            if column:
                keys.append(column)
    return keys


def _field_name_from_soql(soql_name: str) -> str:
    """``"Account.Owner.Name"`` から末端の ``"Name"`` を取り出す。"""
    if not soql_name or soql_name == "(不明)":
        return ""
    if "." in soql_name:
        return soql_name.rsplit(".", 1)[-1]
    return soql_name


def _row_check_status_text(record: object) -> tuple[str, str, str]:
    """record の ``row_check`` を ``(status_text, rows_text, reason_text)`` に分解する。"""
    row_check = getattr(record, "row_check", None)
    if row_check is None:
        return ("未実施", "", "")
    status = getattr(row_check, "status", "")
    rows = getattr(row_check, "rows", None)
    reason = getattr(row_check, "reason", "") or ""
    rows_text = "" if rows is None else str(rows)
    return (status, rows_text, reason)


def _survey_pii_display(record: object) -> tuple[str, str]:
    """record の ``pii`` を ``(表示用ステータス, 該当列)`` に直す。"""
    pii_result = getattr(record, "pii", None)
    if pii_result is None:
        return ("", "")
    status = getattr(pii_result, "status", "")
    matches: list[tuple[str, str]] = getattr(pii_result, "matching_columns", []) or []
    formatted = "; ".join(f"{label}({basis})" for label, basis in matches)
    return (status, formatted)


def soql_status_for_record(record: object) -> str:
    """``record.soql_draft.status`` をそのまま返す （ 無ければ空文字 ） 。

    一覧シートの ``SOQLの状態`` 列で使う。 ``STATUS_NONE`` /
    ``STATUS_PARTIAL`` / ``STATUS_NEEDS_CHECK`` / ``STATUS_CREATED`` の
    いずれかが返る。 ``soql_draft`` が無い record は空文字。
    """
    draft = getattr(record, "soql_draft", None)
    if draft is None:
        return ""
    status = getattr(draft, "status", "")
    return status if isinstance(status, str) else ""


def _build_survey_table_rows_from_records(
    records: Sequence[object],
    *,
    similar_lookup: dict[str, tuple[int, str, str]],
    unresolved_counts: dict[str, int],
) -> list[dict[str, str]]:
    """全 record から 一覧 の行を作る。"""
    rows: list[dict[str, str]] = []
    for record in records:
        key = getattr(record, "key", "")
        summary = getattr(record, "summary", "")
        report_id = getattr(record, "report_id", "")
        report = getattr(record, "report", {}) or {}
        main_object = getattr(record, "main_object", "") or ""
        report_type = _extract_report_type(report)
        format_value = _extract_report_format(report)
        output_columns = _output_column_keys_from_slim(report)
        status, _rows, reason = _row_check_status_text(record)
        pii_status, pii_columns = _survey_pii_display(record)
        warnings = getattr(record, "warnings", ()) or ()
        group_info = similar_lookup.get(key)
        if group_info is not None:
            group_number, base_key, integration = group_info
            group_text = str(group_number)
            if base_key != key:
                group_text = f"{group_number} ({base_key} 基準)"
        else:
            group_text = ""
            integration = ""
        # 取得できなかった項目数: 該当 record に紐付く件数の合計
        unresolved_count = unresolved_counts.get(key, 0)
        rows.append(
            {
                "管理番号": key,
                "概要": summary,
                "レポートID": report_id,
                "レポートタイプ": report_type,
                "主オブジェクト": main_object or "(不明)",
                "形式": format_value,
                "出力列数": str(len(output_columns)),
                "2000件超": status,
                "行数": _rows,
                "2000件超の補足": reason,
                "個人情報": pii_status,
                "個人情報の該当列": pii_columns,
                "SOQLの状態": soql_status_for_record(record),
                "類似グループ": group_text,
                "統合の見込み": integration,
                "取得できなかった項目数": str(unresolved_count),
                "警告数": str(len(warnings)),
            }
        )
    return rows


# ── 取得できなかった項目 ───────────────────────────────────────────────


def _build_unresolved_rows_from_records(
    records: Sequence[object],
) -> list[dict[str, str]]:
    """取得できなかった項目シートの行を作る。

    2 つの系統を同じ形式で並べる:

    - ``種別=項目``: 1 record の ``column_map`` のうち ``対応フィールドAPI名``
      が ``"(不明)"`` の行、 および同じ record の中で表示名が重複している列
      （ 理由 「 同じレポート内で表示名が重複 」 ） 。 重複の単位は
      ``(主オブジェクト, 列キー, 理由)`` 。 表示名が違うときは最初を使い、
      他は捨てる。
    - ``種別=オブジェクト``: ``record.warnings`` のうち describe に失敗した
      オブジェクト （ 警告文の先頭が 「 オブジェクト X の describe に失敗 」 ）
      。 重複の単位は ``(主オブジェクト, 理由)`` 。
    """
    # 1) 項目 ( 不明 / 重複 )
    field_entries: dict[tuple[str, str, str], dict[str, str]] = {}
    field_keys: dict[tuple[str, str, str], set[str]] = {}

    for record in records:
        key = getattr(record, "key", "")
        main_object_text = getattr(record, "main_object", "") or "(不明)"
        column_map = getattr(record, "column_map", []) or []

        # (a) (不明) エントリ
        if isinstance(column_map, list):
            for col in column_map:
                if not isinstance(col, dict):
                    continue
                field_api = _coerce_str(col.get("対応フィールドAPI名"))
                if field_api != "(不明)":
                    continue
                column_key = _coerce_str(col.get("列キー"))
                reason = _coerce_str(col.get("備考")) or "対応フィールドなし"
                label = _coerce_str(col.get("表示名"))
                dedup_key = (main_object_text, column_key, reason)
                if dedup_key not in field_entries:
                    field_entries[dedup_key] = {
                        "種別": "項目",
                        "主オブジェクト": main_object_text,
                        "列キー": column_key,
                        "表示名": label,
                        "理由": reason,
                    }
                    field_keys[dedup_key] = set()
                field_keys[dedup_key].add(key)

            # (b) 同じ record 内で表示名が重複している列
            label_to_keys: dict[str, list[str]] = {}
            for col in column_map:
                if not isinstance(col, dict):
                    continue
                label = _coerce_str(col.get("表示名"))
                if not label:
                    continue
                label_to_keys.setdefault(label, []).append(_coerce_str(col.get("列キー")))
            duplicate_labels = {label for label, keys in label_to_keys.items() if len(keys) > 1}
            if duplicate_labels:
                for col in column_map:
                    if not isinstance(col, dict):
                        continue
                    label = _coerce_str(col.get("表示名"))
                    if label not in duplicate_labels:
                        continue
                    column_key = _coerce_str(col.get("列キー"))
                    dedup_key = (main_object_text, column_key, DUPLICATE_LABEL_REASON)
                    if dedup_key not in field_entries:
                        field_entries[dedup_key] = {
                            "種別": "項目",
                            "主オブジェクト": main_object_text,
                            "列キー": column_key,
                            "表示名": label,
                            "理由": DUPLICATE_LABEL_REASON,
                        }
                        field_keys[dedup_key] = set()
                    field_keys[dedup_key].add(key)

    field_rows = _finalize_unresolved_rows(field_entries, field_keys)

    # 2) オブジェクト ( describe 失敗 )
    object_entries: dict[tuple[str, str], dict[str, str]] = {}
    object_keys: dict[tuple[str, str], set[str]] = {}
    for record in records:
        key = getattr(record, "key", "")
        warnings = getattr(record, "warnings", ()) or ()
        for warning in warnings:
            parsed = _parse_describe_failure_warning(_coerce_str(warning))
            if parsed is None:
                continue
            object_name, reason = parsed
            dedup_key = (object_name, reason)
            if dedup_key not in object_entries:
                object_entries[dedup_key] = {
                    "種別": "オブジェクト",
                    "主オブジェクト": object_name,
                    "列キー": "",
                    "表示名": "",
                    "理由": reason or "describe に失敗",
                }
                object_keys[dedup_key] = set()
            object_keys[dedup_key].add(key)

    object_rows = _finalize_unresolved_rows(object_entries, object_keys)

    # 項目 と オブジェクト を結合し、 件数 → 種別 → 主オブジェクト → 列キー で再ソート
    combined = field_rows + object_rows
    combined.sort(
        key=lambda r: (
            -int(r["件数"]),
            r["種別"],
            r["主オブジェクト"],
            r["列キー"],
        )
    )
    return combined


def _finalize_unresolved_rows(
    entries: dict,
    keys_by_entry: dict,
) -> list[dict[str, str]]:
    """重複マップから最終行を作る。 並びは 件数 ( 降順 ) → 種別 → 主オブジェクト → 列キー。"""
    rows: list[dict[str, str]] = []
    for dedup_key, base in entries.items():
        keys = sorted(keys_by_entry[dedup_key])
        row = dict(base)
        row["件数"] = str(len(keys))
        row["管理番号"] = "|".join(keys)
        rows.append(row)
    rows.sort(
        key=lambda r: (
            -int(r["件数"]),
            r["種別"],
            r["主オブジェクト"],
            r["列キー"],
        )
    )
    return rows


def _parse_describe_failure_warning(warning: str) -> tuple[str, str] | None:
    """警告 「 オブジェクト X の describe に失敗… 」 から ``(name, reason)`` を取り出す。

    該当しない警告は ``None`` を返す。
    """
    if not warning.startswith("オブジェクト "):
        return None
    match = DESCRIBE_FAILURE_PATTERN.match(warning)
    if match is None:
        return None
    object_name = match.group(1)
    reason = match.group(2).lstrip("：: ")
    return object_name, reason


# ── 項目の対応 / 列名の対応.csv ──────────────────────────────────────


def _aggregate_resolved_fields(
    records: Sequence[object],
    pii_lookups: dict[str, dict[str, tuple[str, str]]],
) -> list[_ResolvedField]:
    """全 record の解決済み column_map エントリを ``(主オブジェクト, SOQL列名)`` で集約する。

    解決済み = ``対応フィールドAPI名`` が ``"(不明)"`` でないもの。
    主オブジェクトは ``record.main_object`` （ 無ければ ``"(不明)"`` ） を採用。
    """
    groups: dict[tuple[str, str], list[tuple[str, dict, str]]] = {}
    # 値 (record.key, column_map 行 dict, pii_status)
    for record in records:
        key = getattr(record, "key", "")
        main_object_text = getattr(record, "main_object", "") or "(不明)"
        column_map = getattr(record, "column_map", []) or []
        pii_lookup = pii_lookups.get(key, {})
        if not isinstance(column_map, list):
            continue
        for col in column_map:
            if not isinstance(col, dict):
                continue
            field_api = _coerce_str(col.get("対応フィールドAPI名"))
            if not field_api or field_api == "(不明)":
                continue
            column_key = _coerce_str(col.get("列キー"))
            pii_status, pii_basis = pii_lookup.get(column_key, ("", ""))
            dedup_key = (main_object_text, field_api)
            groups.setdefault(dedup_key, []).append((key, col, pii_status + "\t" + pii_basis))

    fields: list[_ResolvedField] = []
    for (main_object, soql_name), contributions in groups.items():
        keys = sorted({row[0] for row in contributions})
        # 表示名の最多 ( 同数なら最小 管理番号 )
        label_count: dict[str, int] = {}
        label_min_key: dict[str, str] = {}
        for key, col, _ in contributions:
            label = _coerce_str(col.get("表示名"))
            label_count[label] = label_count.get(label, 0) + 1
            if label not in label_min_key or key < label_min_key[label]:
                label_min_key[label] = key
        best_label = ""
        best_count = -1
        best_min_key = ""
        for label, count in label_count.items():
            min_key = label_min_key[label]
            if count > best_count or (count == best_count and min_key < best_min_key):
                best_label = label
                best_count = count
                best_min_key = min_key

        # 別の表示名あり
        other_parts: list[str] = []
        for label, min_key in label_min_key.items():
            if label == best_label:
                continue
            other_parts.append(f"{label}（{min_key}）")
        other_note = ""
        if other_parts:
            other_note = "別の表示名あり: " + ", ".join(other_parts)

        # 型・参照先・PII は最小 管理番号 のものを採用
        first_key = keys[0]
        first_col = next(col for key, col, _ in contributions if key == first_key)
        first_pii = next(pii for key, _, pii in contributions if key == first_key)
        pii_status, pii_basis = first_pii.split("\t", 1)

        fields.append(
            _ResolvedField(
                main_object=main_object,
                soql_name=soql_name,
                display_name=best_label,
                field_type=_coerce_str(first_col.get("型")),
                reference_to=_coerce_str(first_col.get("参照先オブジェクト")),
                pii_status=pii_status,
                pii_basis=pii_basis,
                report_count=len(keys),
                keys=tuple(keys),
                other_label_note=other_note,
            )
        )

    # 並びは 主オブジェクト → SOQL列名
    fields.sort(key=lambda f: (f.main_object, f.soql_name))
    return fields


def _resolved_fields_to_sheet_rows(fields: Sequence[_ResolvedField]) -> list[dict[str, str]]:
    """``項目の対応`` シート用の行 dict を作る。"""
    rows: list[dict[str, str]] = []
    for field in fields:
        rows.append(
            {
                "主オブジェクト": field.main_object,
                "SOQL列名": field.soql_name,
                "表示名": field.display_name,
                "型": field.field_type,
                "参照先オブジェクト": field.reference_to,
                "個人情報": field.pii_status,
                "個人情報の根拠": field.pii_basis,
                "使っているレポート数": str(field.report_count),
                "管理番号": "|".join(field.keys),
                "備考": field.other_label_note,
            }
        )
    return rows


def _resolved_fields_to_csv_rows(
    fields: Sequence[_ResolvedField], today: date
) -> list[dict[str, str]]:
    """``列名の対応.csv`` 用の行 dict を作る。 更新日は YYYY-MM-DD。"""
    rows: list[dict[str, str]] = []
    iso = today.isoformat()
    for field in fields:
        rows.append(
            {
                "主オブジェクト": field.main_object,
                "SOQL列名": field.soql_name,
                "表示名": field.display_name,
                "備考": field.other_label_note,
                "更新日": iso,
            }
        )
    return rows


# ── 条件・集計・警告・項目一覧・関連・似たレポート ────────────────


def _build_condition_table_rows_from_records(
    records: Sequence[object],
) -> list[dict[str, str]]:
    """全 record から条件シートの行を作る。 record.report を 1 行ずつ展開する。

    並びは 「 管理番号 → 種別の順 ( 絞り込み → 組立式 → 日付 → 標準フィルタ
    → クロス → 上位 N ) 」 。 展開した順は record.report のキー順。
    """
    rows: list[dict[str, str]] = []
    for record in records:
        key = getattr(record, "key", "")
        summary = getattr(record, "summary", "")
        report_id = getattr(record, "report_id", "")
        report = getattr(record, "report", {}) or {}
        if not isinstance(report, dict):
            continue
        metadata = report.get("reportMetadata")
        if not isinstance(metadata, dict):
            metadata = {}
        column_map = getattr(record, "column_map", []) or []

        # 絞り込み
        filters = metadata.get("reportFilters")
        if isinstance(filters, list):
            for index, filt in enumerate(filters, start=1):
                if not isinstance(filt, dict):
                    continue
                column = filt.get("column", "")
                operator = filt.get("operator", "")
                value = filt.get("value", "")
                column_text = _coerce_str(column)
                rows.append(
                    {
                        "管理番号": key,
                        "概要": summary,
                        "レポートID": report_id,
                        "種別": "絞り込み",
                        "番号": str(index),
                        "列キー": column_text,
                        "項目": _resolve_column_field_api(column_map, column_text),
                        "演算子": _coerce_str(operator),
                        "値": _coerce_str(value),
                        "備考": "",
                    }
                )

        # 組立式
        bool_filter = metadata.get("reportBooleanFilter")
        if bool_filter:
            rows.append(
                {
                    "管理番号": key,
                    "概要": summary,
                    "レポートID": report_id,
                    "種別": "組立式",
                    "番号": "",
                    "列キー": "",
                    "項目": "",
                    "演算子": "",
                    "値": _coerce_str(bool_filter),
                    "備考": "",
                }
            )

        # 日付
        date_filter = metadata.get("standardDateFilter")
        if isinstance(date_filter, dict):
            column = date_filter.get("column", "")
            start = date_filter.get("startDate", "")
            end = date_filter.get("endDate", "")
            rows.append(
                {
                    "管理番号": key,
                    "概要": summary,
                    "レポートID": report_id,
                    "種別": "日付",
                    "番号": "",
                    "列キー": _coerce_str(column),
                    "項目": "",
                    "演算子": "",
                    "値": f"{start}〜{end}",
                    "備考": _coerce_str(date_filter.get("durationValue", "")),
                }
            )

        # 標準フィルタ
        std_filters = metadata.get("standardFilters")
        if isinstance(std_filters, list) and std_filters:
            for index, sf in enumerate(std_filters, start=1):
                if not isinstance(sf, dict):
                    continue
                rows.append(
                    {
                        "管理番号": key,
                        "概要": summary,
                        "レポートID": report_id,
                        "種別": "標準フィルタ",
                        "番号": str(index),
                        "列キー": "",
                        "項目": "",
                        "演算子": "",
                        "値": json.dumps(sf, ensure_ascii=False),
                        "備考": "",
                    }
                )

        # クロス
        cross_filters = metadata.get("crossFilters")
        if isinstance(cross_filters, list) and cross_filters:
            for index, cf in enumerate(cross_filters, start=1):
                if not isinstance(cf, dict):
                    continue
                primary = cf.get("primaryEntityField", "")
                operator = cf.get("operator", "")
                related = cf.get("relatedEntity", "")
                join_field = cf.get("relatedEntityJoinField", "")
                criteria = cf.get("criteria")
                if isinstance(criteria, str):
                    criteria_text = criteria
                elif criteria:
                    criteria_text = json.dumps(criteria, ensure_ascii=False)
                else:
                    criteria_text = ""
                value = f"{primary} {operator} {related}.{join_field}" + (
                    f" ({criteria_text})" if criteria_text else ""
                )
                rows.append(
                    {
                        "管理番号": key,
                        "概要": summary,
                        "レポートID": report_id,
                        "種別": "クロス",
                        "番号": str(index),
                        "列キー": "",
                        "項目": "",
                        "演算子": "",
                        "値": value,
                        "備考": "",
                    }
                )

        # 上位N
        top_rows = metadata.get("topRows")
        if top_rows:
            rows.append(
                {
                    "管理番号": key,
                    "概要": summary,
                    "レポートID": report_id,
                    "種別": "上位N",
                    "番号": "",
                    "列キー": "",
                    "項目": "",
                    "演算子": "",
                    "値": _format_top_rows(top_rows),
                    "備考": "",
                }
            )
    return rows


def _build_aggregate_table_rows_from_records(
    records: Sequence[object],
) -> list[dict[str, str]]:
    """全 record から集計シートの行を作る。 record.report の集計系キーを展開する。"""
    rows: list[dict[str, str]] = []
    for record in records:
        key = getattr(record, "key", "")
        summary = getattr(record, "summary", "")
        report_id = getattr(record, "report_id", "")
        report = getattr(record, "report", {}) or {}
        if not isinstance(report, dict):
            continue
        metadata = report.get("reportMetadata")
        if not isinstance(metadata, dict):
            metadata = {}

        # 形式
        report_format = metadata.get("reportFormat")
        if report_format:
            rows.append(
                {
                    "管理番号": key,
                    "概要": summary,
                    "レポートID": report_id,
                    "種別": "形式",
                    "順": "",
                    "名前": "",
                    "内容": _coerce_str(report_format),
                }
            )

        # グルーピング ( 行 )
        for index, g in enumerate(metadata.get("groupingsDown") or [], start=1):
            if not isinstance(g, dict):
                continue
            rows.append(
                {
                    "管理番号": key,
                    "概要": summary,
                    "レポートID": report_id,
                    "種別": "グルーピング(行)",
                    "順": str(index),
                    "名前": _coerce_str(g.get("name")),
                    "内容": _coerce_str(g.get("sortOrder")),
                }
            )
        # グルーピング ( 列 )
        for index, g in enumerate(metadata.get("groupingsAcross") or [], start=1):
            if not isinstance(g, dict):
                continue
            rows.append(
                {
                    "管理番号": key,
                    "概要": summary,
                    "レポートID": report_id,
                    "種別": "グルーピング(列)",
                    "順": str(index),
                    "名前": _coerce_str(g.get("name")),
                    "内容": _coerce_str(g.get("sortOrder")),
                }
            )
        # 集計
        for index, agg in enumerate(metadata.get("aggregates") or [], start=1):
            if not isinstance(agg, str):
                continue
            rows.append(
                {
                    "管理番号": key,
                    "概要": summary,
                    "レポートID": report_id,
                    "種別": "集計",
                    "順": str(index),
                    "名前": _coerce_str(agg),
                    "内容": "",
                }
            )
        # 並び順
        for index, sb in enumerate(metadata.get("sortBy") or [], start=1):
            if not isinstance(sb, dict):
                continue
            rows.append(
                {
                    "管理番号": key,
                    "概要": summary,
                    "レポートID": report_id,
                    "種別": "並び順",
                    "順": str(index),
                    "名前": _coerce_str(sb.get("sortColumn")),
                    "内容": _coerce_str(sb.get("sortOrder")),
                }
            )
        # バケット
        for index, bucket in enumerate(metadata.get("buckets") or [], start=1):
            if not isinstance(bucket, dict):
                continue
            name = bucket.get("developerName")
            if not (isinstance(name, str) and name):
                name = bucket.get("name")
            label = bucket.get("label")
            bucket_type = bucket.get("bucketType")
            content_parts: list[str] = []
            if isinstance(label, str) and label:
                content_parts.append(label)
            if isinstance(bucket_type, str) and bucket_type:
                content_parts.append(f"({bucket_type})")
            rows.append(
                {
                    "管理番号": key,
                    "概要": summary,
                    "レポートID": report_id,
                    "種別": "バケット",
                    "順": str(index),
                    "名前": _coerce_str(name),
                    "内容": " ".join(content_parts),
                }
            )
        # 独自計算式
        for index, csf in enumerate(metadata.get("customSummaryFormula") or [], start=1):
            if not isinstance(csf, dict):
                continue
            rows.append(
                {
                    "管理番号": key,
                    "概要": summary,
                    "レポートID": report_id,
                    "種別": "独自計算式",
                    "順": str(index),
                    "名前": _coerce_str(csf.get("formula")),
                    "内容": _coerce_str(csf.get("label")),
                }
            )
    return rows


def _build_warning_table_rows_from_records(
    records: Sequence[object],
) -> list[dict[str, str]]:
    """警告シートの行を作る。 describe 失敗 ( 取得できなかった項目シートが拾う )
    は除く。
    """
    rows: list[dict[str, str]] = []
    for record in records:
        key = getattr(record, "key", "")
        summary = getattr(record, "summary", "")
        report_id = getattr(record, "report_id", "")
        warnings = getattr(record, "warnings", ()) or ()
        for warning in warnings:
            warning_text = _coerce_str(warning)
            if _parse_describe_failure_warning(warning_text) is not None:
                continue
            rows.append(
                {
                    "管理番号": key,
                    "概要": summary,
                    "レポートID": report_id,
                    "警告": warning_text,
                }
            )
    return rows


def _build_pii_lookup_for_record(
    *,
    report: dict,
    column_map: list[dict[str, str]],
    objects: dict,
    config: pii.PIIConfig,
) -> dict[str, tuple[str, str]]:
    """1 record ぶんの ``列キー → (個人情報, 個人情報の根拠)`` を作る。

    対応表の PII 列を埋めるため、 ``column_map`` の各行について出力列の
    ときだけ評価する。 絞り込みにだけ使う列は対象外。
    """
    output_keys = set(_output_column_keys_from_slim(report))
    refs = _column_refs_for_record(report=report, column_map=column_map)
    lookup: dict[str, tuple[str, str]] = {}
    for ref in refs:
        if ref.column_key not in output_keys:
            continue
        result = pii.evaluate_column_pii(ref, config, objects if isinstance(objects, dict) else {})
        if result.status in ("あり", "要確認"):
            lookup[ref.column_key] = (result.status, result.basis)
    return lookup


def _column_refs_for_record(
    *,
    report: dict,
    column_map: list[dict[str, str]],
) -> list[pii.ColumnRef]:
    """レポートの出力列から ``pii.ColumnRef`` を作る。"""
    output_keys = _output_column_keys_from_slim(report)
    column_map_by_key: dict[str, dict[str, str]] = {}
    for col in column_map:
        if not isinstance(col, dict):
            continue
        key = col.get("列キー")
        if isinstance(key, str) and key:
            column_map_by_key[key] = col
    refs: list[pii.ColumnRef] = []
    seen: set[str] = set()
    for column_key in output_keys:
        if column_key in seen:
            continue
        seen.add(column_key)
        col = column_map_by_key.get(column_key, {})
        field_api = _coerce_str(col.get("対応フィールドAPI名")) if isinstance(col, dict) else ""
        label = _coerce_str(col.get("表示名")) if isinstance(col, dict) else ""
        field_type = _coerce_str(col.get("型")) if isinstance(col, dict) else ""
        owner_object = _coerce_str(col.get("所属オブジェクト")) if isinstance(col, dict) else ""
        field_name = _field_name_from_soql(field_api)
        resolved = bool(field_api) and field_api != "(不明)"
        refs.append(
            pii.ColumnRef(
                column_key=column_key,
                label=label or column_key,
                field_name=field_name,
                field_type=field_type,
                owner_object=owner_object,
                resolved=resolved,
            )
        )
    return refs


def _build_field_table_rows_from_records(
    records: Sequence[object],
) -> list[dict[str, str]]:
    """全 record から 項目一覧シートの行を作る ( ``オブジェクト × 項目`` のフラット表 ) 。

    同じオブジェクトが複数の record にあれば、 1 回だけにする。
    並びは「 ``objects`` のキー名の昇順 → describe の ``fields`` の並び順 」 。
    """
    merged: dict[str, dict] = {}
    depth_by_name: dict[str, int] = {}
    for record in records:
        main_object = getattr(record, "main_object", None)
        if isinstance(main_object, str) and main_object:
            # 主オブジェクトは段 0。 同じ名前が複数 record に出ても
            # 最小の 0 を採用 ( 既出が 1 / 2 でも 0 で上書き ) 。
            existing = depth_by_name.get(main_object, 0)
            depth_by_name[main_object] = min(existing, 0)
        objects = getattr(record, "objects", {}) or {}
        if isinstance(objects, dict):
            for obj_name, describe in objects.items():
                if not isinstance(obj_name, str) or not obj_name:
                    continue
                if not isinstance(describe, dict):
                    continue
                merged[obj_name] = describe
        relations = getattr(record, "relations", ()) or ()
        for rel in relations:
            child = getattr(rel, "child", "")
            depth = getattr(rel, "depth", None)
            if not isinstance(child, str) or not child:
                continue
            if not isinstance(depth, int):
                continue
            if child not in depth_by_name or depth < depth_by_name[child]:
                depth_by_name[child] = depth

    rows: list[dict[str, str]] = []
    for obj_name in sorted(merged.keys()):
        describe = merged[obj_name]
        obj_label = describe.get("label") if isinstance(describe, dict) else None
        obj_label_str = obj_label if isinstance(obj_label, str) else ""
        fields = describe.get("fields") if isinstance(describe, dict) else None
        if not isinstance(fields, list):
            continue
        for field in fields:
            if not isinstance(field, dict):
                continue
            reference_to = field.get("referenceTo")
            reference_text = ""
            if isinstance(reference_to, list) and reference_to:
                reference_text = "|".join(
                    str(item) for item in reference_to if isinstance(item, str) and item
                )
            relationship_name = field.get("relationshipName")
            relationship_text = relationship_name if isinstance(relationship_name, str) else ""
            custom = field.get("custom")
            custom_mark = "○" if isinstance(custom, bool) and custom else ""
            rows.append(
                {
                    "オブジェクト": obj_name,
                    "段": str(depth_by_name.get(obj_name, "")),
                    "オブジェクト表示名": obj_label_str,
                    "項目API名": _coerce_str(field.get("name")),
                    "項目表示名": _coerce_str(field.get("label")),
                    "型": _coerce_str(field.get("type")),
                    "参照先オブジェクト": reference_text,
                    "リレーション名": relationship_text,
                    "カスタム": custom_mark,
                    "選択肢": _format_picklist(field.get("picklist"), field.get("picklistTotal")),
                }
            )
    return rows


def _build_related_table_rows_from_records(
    records: Sequence[object],
) -> list[dict[str, str]]:
    """全 record の ``relations`` から 関連シート ( 親→子 ) の行を作る。"""
    rows: list[dict[str, str]] = []
    for record in records:
        key = getattr(record, "key", "")
        relations = getattr(record, "relations", ()) or ()
        for rel in relations:
            rows.append(
                {
                    "管理番号": key,
                    "親オブジェクト": getattr(rel, "parent", ""),
                    "参照項目API名": getattr(rel, "field", ""),
                    "リレーション名": getattr(rel, "relationship_name", "") or "",
                    "子オブジェクト": getattr(rel, "child", ""),
                    "段": str(getattr(rel, "depth", "")),
                }
            )
    return rows


def _build_relation_diagram_rows(
    diagrams: dict[str, relation_diagram.Diagram],
) -> list[dict[str, str]]:
    """``diagrams`` （ 主オブジェクト → ``Diagram`` ） から 関係図シートの行を作る。

    各 ``Diagram`` の木を深さごとに展開し、 1 ノード 1 行の形式にする。
    主オブジェクト自身は段 0、 子は段階 1 と数える。 「 （上に出ています）」
    の注記は ``行き先オブジェクト`` にそのまま添え、 ``SOQLでの書き方`` は
    主オブジェクトからのリレーション鎖を ``.`` 区切りで書く （ 末端は ``.(項目)``
    ） 。
    """
    rows: list[dict[str, str]] = []
    for main_object, diagram in diagrams.items():
        for depth, label, soql_hint, field_api, note, target in _iter_diagram_rows(
            diagram, depth=0
        ):
            rows.append(
                {
                    "主オブジェクト": main_object,
                    "段": str(depth),
                    "図": label,
                    "行き先オブジェクト": target,
                    "SOQLでの書き方": soql_hint,
                    "参照項目API名": field_api,
                }
            )
            # 注記は 行き先 側に添える （ ``"図"`` 列の label は注釈付き）
            if note:
                # 既に ``図`` 側に注釈を含めたので、 行き先 列には追加しない
                pass
    return rows


def _iter_diagram_rows(
    diagram: relation_diagram.Diagram,
    *,
    depth: int,
) -> list[tuple[int, str, str, str, str, str]]:
    """``Diagram`` の木を ``(段, 図, SOQLでの書き方, 参照項目API名, 注記, 行き先)``
    のリストに展開する。 注記 （ 「（上に出ています）」 ） は 図の文字列に含めて、
    行き先 列はそのまま オブジェクト名だけにする
    """
    out: list[tuple[int, str, str, str, str, str]] = []
    if depth == 0:
        # 主オブジェクト自身
        out.append((0, diagram.root.label, "", "", "", diagram.main_object))
    for child in diagram.root.children:
        target = _extract_target(child.label)
        soql_hint = _build_soql_hint(diagram.main_object, child, diagram)
        note = child.note
        label_with_note = _format_label_with_note(child)
        out.append((depth + 1, label_with_note, soql_hint, child.field_api, note, target))
        # 孫以下
        if child.children and not child.note:
            for grandchild in child.children:
                _emit_descendants(out, child, grandchild, diagram, current_depth=depth + 2)
    return out


def _emit_descendants(
    out: list[tuple[int, str, str, str, str, str]],
    parent: relation_diagram.DiagramNode,
    child: relation_diagram.DiagramNode,
    diagram: relation_diagram.Diagram,
    *,
    current_depth: int,
) -> None:
    """``child`` の ``DiagramNode`` を行のリストに追加し、 孫があれば再帰する。

    ``parent`` は子に辿るまでの文脈用。 現状は再帰時の ``diagram`` 取得にしか
    使わないので 「 参照 」 のみで処理パスは無い （ 将来の拡張用 ） 。
    """
    _ = parent  # 現状は参照のみ。 将来の拡張で使うかもしれない
    target = _extract_target(child.label)
    note = child.note
    soql_hint = _build_soql_hint(diagram.main_object, child, diagram)
    label_with_note = _format_label_with_note(child)
    out.append((current_depth, label_with_note, soql_hint, child.field_api, note, target))
    if child.children and not child.note:
        for grandchild in child.children:
            _emit_descendants(out, child, grandchild, diagram, current_depth=current_depth + 1)


def _extract_target(label: str) -> str:
    """``"What → Account"`` から ``"Account"`` を取り出す。 ``" → "`` が
    無いときはラベル全体を返す。
    """
    if " → " in label:
        return label.split(" → ", 1)[1]
    return label


def _format_label_with_note(node: relation_diagram.DiagramNode) -> str:
    """``DiagramNode.label`` に 注記 （ ``（上に出ています）`` ） が有れば添える。
    Excel のセルで字下げ罫線が揃うように、 注記は半角スペース 1 個で離す
    """
    if node.note:
        return f"{node.label} {node.note}"
    return node.label


def _build_soql_hint(
    main_object: str,
    child: relation_diagram.DiagramNode,
    diagram: relation_diagram.Diagram,
) -> str:
    """``主オブジェクト → … → child`` のリレーション鎖を ``.`` 区切りで書く。

    末端は ``.(項目)`` を添えて、 そこに項目名を書く式にする。
    """
    path: list[str] = []
    _collect_rel_path(diagram.root, child, path)
    if not path:
        return f"{main_object}.(項目)"
    return ".".join(path) + ".(項目)"


def _collect_rel_path(
    node: relation_diagram.DiagramNode,
    target: relation_diagram.DiagramNode,
    path: list[str],
) -> bool:
    """``node`` から ``target`` までのリレーション名鎖を ``path`` に集める。"""
    for child in node.children:
        rel = child.label.split(" → ", 1)[0] if " → " in child.label else child.label
        if child is target:
            if rel:
                path.append(rel)
            return True
        if child.children:
            path.append(rel)
            if _collect_rel_path(child, target, path):
                return True
            path.pop()
    return False


def _build_soql_table_rows_from_records(
    records: Sequence[object],
) -> list[dict[str, str]]:
    """全 record の ``soql_draft`` から SOQL シートの行を作る。

    各 record について ``管理番号 / 概要 / 主オブジェクト / 状態 / SOQL /
    作れなかった部分 / 要確認`` の 1 行を組み立てる。 ``soql_draft`` が無い
    record は行を出さない （ 失敗した record など ） 。
    ``soql`` の 1 行目にある ``【未完成】…`` の印はそのまま残し、 Excel で
    見た人が 「 この SOQL は未完成で実行できない 」 と一目で分かるようにする。
    ``missing`` と ``checks`` は ``\\n`` 区切りで 1 セルにまとめる。
    """
    rows: list[dict[str, str]] = []
    for record in records:
        key = getattr(record, "key", "")
        summary = getattr(record, "summary", "")
        main_object = getattr(record, "main_object", "") or ""
        draft = getattr(record, "soql_draft", None)
        if draft is None:
            continue
        status = getattr(draft, "status", "") or ""
        soql = getattr(draft, "soql", "") or ""
        missing = getattr(draft, "missing", ()) or ()
        checks = getattr(draft, "checks", ()) or ()
        rows.append(
            {
                "管理番号": key,
                "概要": summary,
                "主オブジェクト": main_object or "(不明)",
                "状態": status,
                "SOQL": soql,
                "作れなかった部分": "\n".join(str(item) for item in missing),
                "要確認": "\n".join(str(item) for item in checks),
            }
        )
    return rows


def _build_similar_table_rows(
    records: Sequence[object],
    groups: list[similar.SimilarGroup],
) -> list[dict[str, str]]:
    """似たレポートの行を 1 グループずつ組み立てる。 基準自身の行も出力する。"""
    record_by_key: dict[str, object] = {getattr(r, "key", ""): r for r in records}
    rows: list[dict[str, str]] = []
    for group in groups:
        base_record = record_by_key.get(group.base_key)
        base_summary = getattr(base_record, "summary", "") if base_record is not None else ""
        base_report_id = getattr(base_record, "report_id", "") if base_record is not None else ""
        # 基準行
        rows.append(
            {
                "類似グループ": str(group.group_number),
                "管理番号": group.base_key,
                "概要": base_summary,
                "レポートID": base_report_id,
                "基準の管理番号": group.base_key,
                "区分": "基準",
                "基準との違い": "",
                "統合の見込み": "",
            }
        )
        for pair in group.pairs:
            other_key = pair.key_b
            other_record = record_by_key.get(other_key)
            other_summary = getattr(other_record, "summary", "") if other_record is not None else ""
            other_report_id = (
                getattr(other_record, "report_id", "") if other_record is not None else ""
            )
            rows.append(
                {
                    "類似グループ": str(group.group_number),
                    "管理番号": other_key,
                    "概要": other_summary,
                    "レポートID": other_report_id,
                    "基準の管理番号": group.base_key,
                    "区分": pair.category,
                    "基準との違い": pair.diff_text,
                    "統合の見込み": pair.integration,
                }
            )
    return rows


# ── 列名の対応.csv ────────────────────────────────────────────


def _merge_column_names_csv(
    path: Path,
    new_rows: Sequence[dict[str, str]],
    replaced_keys: Iterable[str],
) -> None:
    """``列名の対応.csv`` を既存ファイルと ``new_rows`` でマージして書き直す。

    置換キー ( ``replaced_keys`` ) は 「 今回 record が来た ( 主オブジェクト,
    SOQL列名 ) 」 の単位ではなく、 「 今回 record が来た管理番号 」 を渡される。
    ``new_rows`` 側で行が無い ( 該当 record に解決済み列が無い ) ときも古い
    行を消すための判断材料として使う。

    並びは **主オブジェクト → SOQL列名** 。 ``new_rows`` の順は渡された順を
    保つ ( 安定ソート ) 。

    既存ファイルの見出しが ``COLUMN_NAMES_COLUMNS`` と違うとき、 もしくは
    既存ファイルを CSV として読めないときは ``CSVError`` を上げる。 既存
    ファイルは変更しない。

    なぜ: ダウンローダーがこの CSV を ``(主オブジェクト, SOQL列名)`` で引いて
    使う。 実行のたびに今回取れた主オブジェクトの全行を丸ごと上書きすると、
    別の管理表から取れて残っていた ``(主オブジェクト, SOQL列名)`` の対応が
    消えてしまう。 そこで 「 今回 record が来た管理番号が解決した ( 主,
    SOQL列名 ) 」 の行だけ入れ替え、 それ以外はそのまま残す。
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    existing_rows: list[dict[str, str]] = []
    if path.exists():
        try:
            with CSV(path) as csv:
                table = csv.read()
        except CSVError as exc:
            # 読み込めない ( BOM だけ、 列数不一致、 文字化け 等 ) ときも
            # 上書きせず失敗させる。
            raise CSVError(
                f"既存の {path.name} を読み込めません: {path}\n{exc}"
                "\n対処: ファイルを別名に退避してから再実行してください。"
            ) from exc
        if list(table.columns) != list(COLUMN_NAMES_COLUMNS):
            raise CSVError(
                f"既存の {path.name} の見出しが想定と違います: {path}\n"
                f"期待: {list(COLUMN_NAMES_COLUMNS)}\n"
                f"実際: {list(table.columns)}"
                "\n対処: ファイルを別名に退避してから再実行してください。"
            )
        existing_rows = [dict(row) for row in table.to_rows()]

    # 今回 record が来た管理番号から、 新しい行の (主オブジェクト, SOQL列名)
    # 集合を作る。 この集合に属する既存行は捨てて ``new_rows`` で置き換える。
    # (「 その回に取れた (主, SOQL列名) の全件 」 ) は、旧形式と新形式を
    # 跨いで 「 同じ主オブジェクトを取った ( 主オブジェクト, SOQL列名 ) 」 の
    # 集合で扱う。 ただし ``replaced_keys`` が空 ( 全件失敗 ) のときは何も
    # 消さない ( 既存を全件残す ) 。
    new_keys = set(replaced_keys)
    if not new_keys:
        merged = list(existing_rows)
    else:
        new_pairs = {(row.get("主オブジェクト", ""), row.get("SOQL列名", "")) for row in new_rows}
        kept_old_rows = [
            row
            for row in existing_rows
            if (
                row.get("主オブジェクト", ""),
                row.get("SOQL列名", ""),
            )
            not in new_pairs
        ]
        merged = kept_old_rows + list(new_rows)

    # 主オブジェクト → SOQL列名 の安定ソート
    merged.sort(key=lambda row: (row.get("主オブジェクト", ""), row.get("SOQL列名", "")))

    _write_csv(path, COLUMN_NAMES_COLUMNS, merged)


# ── Excel 書き出し ──────────────────────────────────────────────


def _write_xlsx(
    path: Path,
    *,
    survey: list[dict[str, str]],
    unresolved: list[dict[str, str]],
    resolved: list[dict[str, str]],
    condition: list[dict[str, str]],
    aggregate: list[dict[str, str]],
    similar: list[dict[str, str]],
    warning: list[dict[str, str]],
    field: list[dict[str, str]],
    related: list[dict[str, str]],
    relation_diagram: list[dict[str, str]],
    soql: list[dict[str, str]],
) -> None:
    """``SOQL調査.xlsx`` を書く。

    毎回丸ごと作り直すため、 既存ファイルがあれば消す。 comken の ``Excel``
    は新規ファイルを開くときに既定の ``Sheet`` を作る。 一覧シートは新規
    ブックの既定シートを改名し、 残り 10 枚は ``create_sheet`` で追加する
    ( openpyxl のシート順 = 表示順 ) 。
    """
    if path.exists():
        path.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)

    payload: dict[str, tuple[tuple[str, ...], list[dict[str, str]]]] = {
        SHEET_OVERVIEW: (SURVEY_COLUMNS, survey),
        SHEET_UNRESOLVED: (UNRESOLVED_COLUMNS, unresolved),
        SHEET_RESOLVED: (RESOLVED_COLUMNS, resolved),
        SHEET_CONDITION: (CONDITION_COLUMNS, condition),
        SHEET_AGGREGATE: (AGGREGATE_COLUMNS, aggregate),
        SHEET_SIMILAR: (SIMILAR_COLUMNS, similar),
        SHEET_WARNING: (WARNING_COLUMNS, warning),
        SHEET_FIELDS: (FIELD_COLUMNS, field),
        SHEET_RELATED: (RELATED_COLUMNS, related),
        SHEET_RELATION_DIAGRAM: (RELATION_DIAGRAM_COLUMNS, relation_diagram),
        SHEET_SOQL: (SOQL_COLUMNS, soql),
    }

    with Excel(path) as excel:
        # 一覧シート: 新規ブックの既定シート ( "Sheet" ) を 一覧 に。
        overview_sheet = excel.sheet(SHEET_OVERVIEW)
        _write_sheet(overview_sheet, *payload[SHEET_OVERVIEW])
        del payload[SHEET_OVERVIEW]

        for name in SHEET_NAMES_IN_ORDER:
            if name == SHEET_OVERVIEW:
                continue
            columns, rows = payload[name]
            sheet_obj = excel.create_sheet(name)
            _write_sheet(sheet_obj, columns, rows)


def _write_sheet(
    sheet_obj: object,
    columns: tuple[str, ...],
    rows: list[dict[str, str]],
) -> None:
    """1 枚のシートに見出し行 ( 太字 ) とデータ行を書き、 先頭行を固定する。

    comken の ``Sheet.write_range`` は 2 行以上ないと使えないため、 ヘッダーは
    ``write_value`` で 1 セルずつ書き、 データ行は ``write_range`` で書く。
    列幅・交互の縞模様などは comken の API に無いため、 ここではやらない
    ( 別途報告 ) 。
    """
    headers = list(columns)
    n_cols = len(headers)
    last_col_letter = get_column_letter(n_cols)

    # ヘッダー行
    for i, header in enumerate(headers, start=1):
        col_letter = get_column_letter(i)
        sheet_obj.write_value(f"{col_letter}1", header)  # type: ignore[attr-defined]

    # データ行
    if rows:
        n_data = len(rows)
        range_str = f"A2:{last_col_letter}{n_data + 1}"
        values = [[row.get(col, "") for col in headers] for row in rows]
        sheet_obj.write_range(range_str, values)  # type: ignore[attr-defined]

    # ヘッダー行を太字に
    for i in range(1, n_cols + 1):
        col_letter = get_column_letter(i)
        sheet_obj.format(f"{col_letter}1", bold=True)  # type: ignore[attr-defined]

    # 先頭行を固定
    sheet_obj.freeze_panes("A2")  # type: ignore[attr-defined]


# ── 共通ヘルパー ────────────────────────────────────────────


def _resolve_column_field_api(column_map: object, column_key: str) -> str:
    """``column_map`` から ``列キー`` が一致する行の ``対応フィールドAPI名`` を返す。

    解決できた列 ( ``対応フィールドAPI名`` が空でなく ``"(不明)"`` でない )
    だけを採用する。 同じ ``列キー`` が複数行に出るときは先頭を採用。 該当が
    無いときは空文字を返す ( 列キーそのものが空のときも空文字 ) 。
    """
    if not isinstance(column_map, list) or not column_key:
        return ""
    for col in column_map:
        if not isinstance(col, dict):
            continue
        if _coerce_str(col.get("列キー")) != column_key:
            continue
        api_name = _coerce_str(col.get("対応フィールドAPI名"))
        if api_name and api_name != "(不明)":
            return api_name
    return ""


def _today_local() -> date:
    """「 今日の日付 」 をこの PC のローカル時間で返す。

    ``datetime.date.today()`` はそのままでも地域の 現在日付を返すが、 業務
    日付として明示するため ``datetime.now(UTC).astimezone()`` から ``date``
    を作る。 ``ZoneInfo`` を使うと Windows オフラインで tzdata が要るので
    避ける ( comken と同じ方針 ) 。
    """
    return datetime.now(tz=UTC).astimezone().date()


def _format_top_rows(top_rows: object) -> str:
    """``reportMetadata.topRows`` を人が読める 1 行にする。

    Salesforce の ``topRows`` は dict (``{"rowLimit": N, "direction": "Asc"}``)
    で来ることが多い。 そのまま ``str()`` すると Python の repr になって
    しまうので、 ``"N件 direction"`` の形に整形する。  ``rowLimit`` と
    ``direction`` は **独立に** 省略でき、 片方しか無いときはある方だけ
    出力する。 dict でないとき ( 文字列・数値 ) は従来どおり ``str()`` する
    ( 既存テスト ``topRows: "10"`` を壊さないため ) 。
    """
    if not isinstance(top_rows, dict):
        return _coerce_str(top_rows)
    row_limit = top_rows.get("rowLimit")
    direction = top_rows.get("direction")
    parts: list[str] = []
    if isinstance(row_limit, bool):
        # bool は int の subclass なのでここで除外
        pass
    elif isinstance(row_limit, int) or (isinstance(row_limit, str) and row_limit):
        parts.append(f"{row_limit}件")
    if isinstance(direction, str) and direction:
        parts.append(direction)
    return " ".join(parts)


def _format_picklist(picklist: object, picklist_total: object = 0) -> str:
    """``picklist`` ( active な ``value`` だけの配列 ) を ``|`` 区切りにする。

    ``picklistTotal`` が ``PICKLIST_LIMIT`` ( 既定 30 ) を超えるとき末尾に
    ``…`` を 1 個足して切る ( 業務で実際に必要になるのは数件までなので、
    30 で十分。 多すぎると CSV が読みづらくなる ) 。 ``picklist`` 自体が
    30 件に絞られていても、 ``picklistTotal`` で 「 さらに後ろがある 」 こと
    を検知する。
    """
    if not isinstance(picklist, list) or not picklist:
        return ""
    values: list[str] = [v for v in picklist if isinstance(v, str) and v]
    if not values:
        return ""
    total = picklist_total if isinstance(picklist_total, int) else len(values)
    if total > PICKLIST_LIMIT:
        if len(values) > PICKLIST_LIMIT:
            values = values[:PICKLIST_LIMIT]
        values.append("…")
    return "|".join(values)


def _coerce_str(value: object) -> str:
    """値を ``str`` にする。 ``None`` は空文字。"""
    if value is None:
        return ""
    return str(value)


def _write_csv(path: Path, columns: Sequence[str], rows: list[dict[str, str]]) -> None:
    """comken の ``CSV`` クラスで UTF-8 BOM + CRLF の CSV を書く。

    ``CSV`` クラスの既定では新規ファイルは **UTF-8 BOM 付き** で書かれ、
    ``csv.DictWriter`` のデフォルト動作で **CRLF** 改行になる。 ``columns=``
    を渡すと ``rows`` が空でも列が決まる ( 「 全行なし 」 の状態でも
    見出しだけの CSV ファイルができる ) 。 comken の ``CSV._write`` は途中で
    失敗したら ``.part`` 一時ファイルが片付けられ、 既存の同名ファイルは
    無傷で残る。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with CSV(path, columns=list(columns)) as csv:
        csv.replace(Table(list(columns), rows))
