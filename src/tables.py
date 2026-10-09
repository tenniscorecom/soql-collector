"""``ReportRecord`` のリストから、 AI / 人が Excel で読む CSV を組み立てる。

入力は ``ReportRecord`` のイテラブル。 出力は同じ ``OUTPUT_DIR`` に次の
8 種類の CSV を書く:

- ``対応表_{管理番号}.csv`` … 1 ID 分のレポート 1 列（ 1 ID ごと ）
- ``対応表.csv`` … 全体 （ 管理番号昇順 ）
- ``項目表.csv`` … オブジェクト × 項目のフラット表
- ``関連表.csv`` … 親→子の参照 1 行ずつ
- ``条件表.csv`` … レポート定義の絞り込み・組立式・標準フィルタ・クロス・
  上位 N を 1 行ずつ展開
- ``集計表.csv`` … 形式・グルーピング・集計・並び順・バケット・独自計算式を
  1 行ずつ展開
- ``警告表.csv`` … 主オブジェクト取得失敗・関連オブジェクト打ち切り・describe
  失敗・許可リスト外キー などの警告を集約
- ``列名の対応.csv`` … ``COLUMN_NAMES`` 貼付用 CSV
- ``列名の対応_{管理番号}.txt`` … 同じく貼付用 Python dict テキスト

per-ID CSV は ``only_keys`` で絞った record だけ作る。 失敗した ID の
CSV は作らず既存も消さない。 全体 CSV は ``records`` 全体から組み立てる。

すべて UTF-8 BOM つき + CRLF。 comken の ``CSV`` クラス
（ ``comken.toolbox.csv`` ） で書き出す。 新規ファイルは BOM 付き + CRLF
になることは確認済み。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from comken.toolbox.csv import CSV

from src import pii, similar

logger = logging.getLogger(__name__)

# 1 つの CSV ファイルあたりの選択肢の最大値数。超えたら末尾に ``…`` を付ける
PICKLIST_LIMIT = 30

# 「複数候補あり」 備考に並べる鎖+項目の最大件数
MULTIPLE_CANDIDATES_LIMIT = 5

# 1 つの ``COLUMN_NAMES`` dict に同じ表示名が複数回現れたときに「 表示名が重複 」
# と注記するチェックで使う


# 対応表の列名 （ 所属オブジェクト を追加 ）
CORRESPONDENCE_COLUMNS: tuple[str, ...] = (
    "管理番号",
    "概要",
    "レポートID",
    "レポートタイプ",
    "主オブジェクト",
    "列キー",
    "表示名",
    "所属オブジェクト",
    "対応フィールドAPI名",
    "型",
    "参照先オブジェクト",
    "リレーション名",
    "備考",
    "個人情報",
    "個人情報の根拠",
)

# 調査表の列名
SURVEY_TABLE_COLUMNS: tuple[str, ...] = (
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
    "類似グループ",
    "統合の見込み",
    "警告数",
)

# 類似レポートの列名
SIMILAR_TABLE_COLUMNS: tuple[str, ...] = (
    "類似グループ",
    "管理番号",
    "概要",
    "レポートID",
    "基準の管理番号",
    "区分",
    "基準との違い",
    "統合の見込み",
)

# 項目表の列名
FIELD_TABLE_COLUMNS: tuple[str, ...] = (
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

# 関連表の列名
RELATED_TABLE_COLUMNS: tuple[str, ...] = (
    "管理番号",
    "親オブジェクト",
    "参照項目API名",
    "リレーション名",
    "子オブジェクト",
    "段",
)

# 条件表の列名
CONDITION_TABLE_COLUMNS: tuple[str, ...] = (
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

# 集計表の列名
AGGREGATE_TABLE_COLUMNS: tuple[str, ...] = (
    "管理番号",
    "概要",
    "レポートID",
    "種別",
    "順",
    "名前",
    "内容",
)

# 警告表の列名
WARNING_TABLE_COLUMNS: tuple[str, ...] = (
    "管理番号",
    "概要",
    "レポートID",
    "警告",
)

# 列名の対応の列名
COLUMN_NAMES_COLUMNS: tuple[str, ...] = (
    "管理番号",
    "概要",
    "SOQL列名",
    "表示名",
    "備考",
)


@dataclass(frozen=True)
class TableOutcome:
    """tables 実行の結果。"""

    wrote: tuple[Path, ...]
    skipped: tuple[Path, ...]


def run_tables(
    output_dir: Path,
    records: Iterable[object],
    *,
    only_keys: Iterable[str] | None = None,
    pii_config: pii.PIIConfig | None = None,
    similar_threshold: float = 0.8,
) -> TableOutcome:
    """``records`` から CSV を組み立てる。

    Args:
        output_dir: CSV を置くフォルダ。 無ければ作る。 失敗した ID の
        per-id CSV は作らない。
        records: ``fetch.ReportRecord`` のイテラブル。 メモリ上の記録。
        only_keys: per-ID CSV ``対応表_{管理番号}.csv`` を作る対象の管理番号。
            ``None`` なら ``records`` の全部を対象にする。
        pii_config: 対応表の ``個人情報`` 列を埋めるための設定。 ``None`` なら
            デフォルト設定を使う。 詳細を見ない場合は ``None`` のままでも動く。
        similar_threshold: 似たレポート判定の Jaccard 下限。

    Returns:
        ``TableOutcome``: 書いたファイル一覧。 skipped は常に空 （ 互換用 ）。

    Raises:
        FileNotFoundError: 投げない （ ``output_dir`` が無くても作る ）。
    """
    record_list = list(records)
    only_set = set(only_keys) if only_keys is not None else None
    pii_config = pii_config or pii.PIIConfig(
        keywords=pii.DEFAULT_KEYWORDS,
        person_objects=pii.DEFAULT_PERSON_OBJECTS,
    )

    wrote: list[Path] = []

    # per-id CSV
    if only_set is not None:
        per_id_records = [r for r in record_list if getattr(r, "key", "") in only_set]
    else:
        per_id_records = record_list

    for record in per_id_records:
        key = getattr(record, "key", "")
        summary = getattr(record, "summary", "")
        report_id = getattr(record, "report_id", "")
        report = getattr(record, "report", {}) or {}
        objects = getattr(record, "objects", {}) or {}
        column_map = getattr(record, "column_map", []) or []
        main_object = getattr(record, "main_object", "") or ""

        # per-id 対応表
        per_path = output_dir / f"対応表_{key}.csv"
        per_pii_lookup = _build_pii_lookup_for_record(
            report=report,
            column_map=column_map,
            objects=objects,
            config=pii_config,
        )
        per_rows = _build_correspondence_rows_from_record(
            key,
            summary,
            report_id,
            report,
            objects,
            column_map,
            pii_lookup=per_pii_lookup,
            main_object=main_object or None,
        )
        _write_csv(per_path, CORRESPONDENCE_COLUMNS, per_rows)
        wrote.append(per_path)

        # per-id 列名の対応 (txt)
        per_txt_path = output_dir / f"列名の対応_{key}.txt"
        per_txt_path.write_text(
            _build_column_names_text(
                key=key,
                summary=summary,
                column_map=column_map,
                report_id=report_id,
            ),
            encoding="utf-8",
        )
        wrote.append(per_txt_path)

    # 全体 CSV は record 全体から作る （ 管理番号昇順 ）
    sorted_records = sorted(record_list, key=lambda r: getattr(r, "key", ""))

    # 全体 対応表
    all_correspondence_rows: list[dict[str, str]] = []
    for record in sorted_records:
        report = getattr(record, "report", {}) or {}
        objects = getattr(record, "objects", {}) or {}
        column_map = getattr(record, "column_map", []) or []
        pii_lookup = _build_pii_lookup_for_record(
            report=report, column_map=column_map, objects=objects, config=pii_config
        )
        all_correspondence_rows.extend(
            _build_correspondence_rows_from_record(
                getattr(record, "key", ""),
                getattr(record, "summary", ""),
                getattr(record, "report_id", ""),
                report,
                objects,
                column_map,
                pii_lookup=pii_lookup,
                main_object=getattr(record, "main_object", "") or None,
            )
        )
    correspondence_path = output_dir / "対応表.csv"
    _write_csv(correspondence_path, CORRESPONDENCE_COLUMNS, all_correspondence_rows)
    wrote.append(correspondence_path)

    # 項目表
    field_rows = _build_field_table_rows_from_records(sorted_records)
    field_path = output_dir / "項目表.csv"
    _write_csv(field_path, FIELD_TABLE_COLUMNS, field_rows)
    wrote.append(field_path)

    # 関連表
    related_rows = _build_related_table_rows_from_records(sorted_records)
    related_path = output_dir / "関連表.csv"
    _write_csv(related_path, RELATED_TABLE_COLUMNS, related_rows)
    wrote.append(related_path)

    # 条件表
    condition_rows = _build_condition_table_rows_from_records(sorted_records)
    condition_path = output_dir / "条件表.csv"
    _write_csv(condition_path, CONDITION_TABLE_COLUMNS, condition_rows)
    wrote.append(condition_path)

    # 集計表
    aggregate_rows = _build_aggregate_table_rows_from_records(sorted_records)
    aggregate_path = output_dir / "集計表.csv"
    _write_csv(aggregate_path, AGGREGATE_TABLE_COLUMNS, aggregate_rows)
    wrote.append(aggregate_path)

    # 警告表
    warning_rows = _build_warning_table_rows_from_records(sorted_records)
    warning_path = output_dir / "警告表.csv"
    _write_csv(warning_path, WARNING_TABLE_COLUMNS, warning_rows)
    wrote.append(warning_path)

    # 列名の対応
    column_names_rows = _build_column_names_rows_from_records(sorted_records)
    column_names_path = output_dir / "列名の対応.csv"
    _write_csv(column_names_path, COLUMN_NAMES_COLUMNS, column_names_rows)
    wrote.append(column_names_path)

    # 似たレポートのグループ化（ 全体 ） 。 1 件や該当なしならファイルを作らない
    similar_groups = similar.find_similar_groups(
        sorted_records, column_similarity_threshold=similar_threshold
    )
    key_to_group: dict[str, tuple[int, str, str]] = {}  # 管理番号 → (group, base, integration)
    if similar_groups:
        similar_rows = _build_similar_table_rows(sorted_records, similar_groups)
        similar_path = output_dir / "類似レポート.csv"
        _write_csv(similar_path, SIMILAR_TABLE_COLUMNS, similar_rows)
        wrote.append(similar_path)
        # 調査表用に 管理番号 → グループ / 基準 / 見込み のマッピングを作る
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

    # 調査表（ 全体 ）
    survey_rows = _build_survey_table_rows_from_records(
        sorted_records, pii_config=pii_config, similar_lookup=key_to_group
    )
    survey_path = output_dir / "調査表.csv"
    _write_csv(survey_path, SURVEY_TABLE_COLUMNS, survey_rows)
    wrote.append(survey_path)

    for path in wrote:
        logger.info("CSV を書き出し: %s（%d 行）", path, _count_data_rows(path))

    return TableOutcome(wrote=tuple(wrote), skipped=())


# ── 対応表 ──────────────────────────────────────────────────────────────


def _extract_report_type(report: object) -> str:
    """``record.report.reportMetadata.reportType.type`` を取り出す。無ければ空文字。"""
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
    refs = _column_refs_for_record(report=report, column_map=column_map, objects=objects)
    lookup: dict[str, tuple[str, str]] = {}
    for ref in refs:
        if ref.column_key not in output_keys:
            continue
        result = pii.evaluate_column_pii(ref, config, objects if isinstance(objects, dict) else {})
        if result.status in ("あり", "要確認"):
            lookup[ref.column_key] = (result.status, result.basis)
    return lookup


def _output_column_keys_from_slim(slim_report: object) -> list[str]:
    """``slim_report`` の出力列キー（ 順序付き ） を集める。"""
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


def _column_refs_for_record(
    *,
    report: dict,
    column_map: list[dict[str, str]],
    objects: dict,
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


def _row_check_display(record: object) -> tuple[str, str]:
    """record の ``row_check`` を ``(表示用ステータス, 表示用理由)`` に直す。"""
    status, _rows, reason = _row_check_status_text(record)
    return status, reason


def _survey_pii_display(record: object) -> tuple[str, str]:
    """record の ``pii`` を ``(表示用ステータス, 該当列)`` に直す。"""
    pii_result = getattr(record, "pii", None)
    if pii_result is None:
        return ("", "")
    status = getattr(pii_result, "status", "")
    matches: list[tuple[str, str]] = getattr(pii_result, "matching_columns", []) or []
    formatted = "; ".join(f"{label}({basis})" for label, basis in matches)
    return (status, formatted)


def _build_survey_table_rows_from_records(
    records: Sequence[object],
    *,
    pii_config: pii.PIIConfig,
    similar_lookup: dict[str, tuple[int, str, str]],
) -> list[dict[str, str]]:
    """全 record から 調査表 の行を作る。"""
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
                "類似グループ": group_text,
                "統合の見込み": integration,
                "警告数": str(len(warnings)),
            }
        )
    return rows


def _extract_report_format(report: object) -> str:
    """``record.report.reportMetadata.reportFormat`` を取り出す。 無ければ空文字。"""
    if not isinstance(report, dict):
        return ""
    metadata = report.get("reportMetadata")
    if not isinstance(metadata, dict):
        return ""
    value = metadata.get("reportFormat")
    return value if isinstance(value, str) else ""


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


def _build_field_lookup(describe: object) -> dict[str, dict[str, str]]:
    """主オブジェクト describe の ``fields`` から、 ``name → (referenceTo, relationshipName)``
    の辞書を作る。

    ``referenceTo`` がリストなら ``|`` 区切り、 ``relationshipName`` が無ければ空。
    名前は ``describe`` 内に複数出てきても先勝ち（上書きしない）。
    """
    if not isinstance(describe, dict):
        return {}
    fields = describe.get("fields")
    if not isinstance(fields, list):
        return {}
    result: dict[str, dict[str, str]] = {}
    for field in fields:
        if not isinstance(field, dict):
            continue
        name = field.get("name")
        if not isinstance(name, str) or not name:
            continue
        reference_to = field.get("referenceTo")
        reference_text = ""
        if isinstance(reference_to, list) and reference_to:
            reference_text = "|".join(
                str(item) for item in reference_to if isinstance(item, str) and item
            )
        relationship_name = field.get("relationshipName")
        relationship_text = relationship_name if isinstance(relationship_name, str) else ""
        result[name] = {
            "referenceTo": reference_text,
            "relationshipName": relationship_text,
        }
    return result


def _build_correspondence_rows_from_record(
    key: str,
    summary: str,
    report_id: str,
    report: dict,
    objects: dict,
    column_map: list[dict[str, str]],
    *,
    pii_lookup: dict[str, tuple[str, str]] | None = None,
    main_object: str | None = None,
) -> list[dict[str, str]]:
    """1 record から対応表の行を作る（ 1 レポート列 = 1 行 ）。

    ``pii_lookup`` には ``列キー → (個人情報, 個人情報の根拠)`` のマッピングを
    渡す。 未指定なら PII 列は空になる。 ``main_object`` が ``None`` / 空文字
    のときは ``主オブジェクト`` 列を ``(不明)`` にする。
    """
    report_type = _extract_report_type(report)

    # column_map に 所属オブジェクト が入っていればそれを採用。 無ければ
    # objects のキーをもとに補完する。
    main_object_text = main_object or "(不明)"
    rows: list[dict[str, str]] = []
    for col in column_map:
        if not isinstance(col, dict):
            continue
        field_api = _coerce_str(col.get("対応フィールドAPI名"))
        owner_object = _coerce_str(col.get("所属オブジェクト"))
        column_key = _coerce_str(col.get("列キー"))
        # 所属オブジェクトが空のときのフォールバック: 解決できたフィールドが
        # 主オブジェクト側にあれば主オブジェクト名を入れる。
        lookup: dict[str, str] = {}
        if owner_object and isinstance(objects, dict):
            describe = objects.get(owner_object)
            if isinstance(describe, dict):
                lookup = _build_field_lookup(describe).get(field_api, {})
        pii_status, pii_basis = (pii_lookup or {}).get(column_key, ("", ""))
        row = {
            "管理番号": key,
            "概要": summary,
            "レポートID": report_id,
            "レポートタイプ": report_type,
            "主オブジェクト": main_object_text,
            "列キー": column_key,
            "表示名": _coerce_str(col.get("表示名")),
            "所属オブジェクト": owner_object,
            "対応フィールドAPI名": field_api,
            "型": _coerce_str(col.get("型")),
            "参照先オブジェクト": lookup.get("referenceTo", ""),
            "リレーション名": lookup.get("relationshipName", ""),
            "備考": _coerce_str(col.get("備考")),
            "個人情報": pii_status,
            "個人情報の根拠": pii_basis,
        }
        rows.append(row)
    return rows


# ── 項目表 ──────────────────────────────────────────────────────────────


def _build_field_table_rows_from_records(
    records: Sequence[object],
) -> list[dict[str, str]]:
    """全 record から項目表の行を作る（ ``オブジェクト × 項目`` のフラット表 ）。

    同じオブジェクトが複数の record にあれば、 1 回だけにする。
    並びは「 ``objects`` のキー名の昇順 → describe の ``fields`` の並び順」 。
    """
    merged: dict[str, dict] = {}
    depth_by_name: dict[str, int] = {}
    for record in records:
        main_object = getattr(record, "main_object", None)
        if isinstance(main_object, str) and main_object:
            # 主オブジェクトは段 0。 同じ名前が複数 record に出ても
            # 最小の 0 を採用（ 既出が 1 / 2 でも 0 で上書き ）。
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
    """全 record の ``relations`` から関連表（ 親→子 ） の行を作る。"""
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


# ── 条件表・集計表・警告表 ──────────────────────────────────────────────


def _build_condition_table_rows_from_records(
    records: Sequence[object],
) -> list[dict[str, str]]:
    """全 record から条件表の行を作る。 record.report を 1 行ずつ展開する。

    並びは 「 管理番号 → 種別の順 （ 絞り込み → 組立式 → 日付 → 標準フィルタ
     → クロス → 上位 N ） 」 。 展開した順は record.report のキー順。
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
    """全 record から集計表の行を作る。 record.report の集計系キーを展開する。"""
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

        # グルーピング（ 行 ）
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
        # グルーピング（ 列 ）
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
            # Analytics API のバケットは ``developerName`` / ``label`` /
            # ``bucketType`` などのキーを持つ。 ``developerName`` が無い
            # 実装向けに ``name`` もフォールバックで見る。 内容の文字列は
            # ``label`` を主、 ``bucketType`` を添え書きで出す。
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
    """全 record から警告表の行を作る。 record.warnings を 1 件ずつ展開。"""
    rows: list[dict[str, str]] = []
    for record in records:
        key = getattr(record, "key", "")
        summary = getattr(record, "summary", "")
        report_id = getattr(record, "report_id", "")
        warnings = getattr(record, "warnings", ()) or ()
        for warning in warnings:
            rows.append(
                {
                    "管理番号": key,
                    "概要": summary,
                    "レポートID": report_id,
                    "警告": _coerce_str(warning),
                }
            )
    return rows


# ── 列名の対応 ──────────────────────────────────────────────────────────


def _build_column_names_rows_from_records(
    records: Sequence[object],
) -> list[dict[str, str]]:
    """全 record から ``列名の対応.csv`` の行を作る。"""
    rows: list[dict[str, str]] = []
    for record in records:
        key = getattr(record, "key", "")
        summary = getattr(record, "summary", "")
        column_map = getattr(record, "column_map", []) or []
        # 表示名の重複検知
        seen_labels: dict[str, int] = {}
        for col in column_map:
            if not isinstance(col, dict):
                continue
            field_api = _coerce_str(col.get("対応フィールドAPI名"))
            if not field_api or field_api == "(不明)":
                continue
            label = _coerce_str(col.get("表示名"))
            seen_labels[label] = seen_labels.get(label, 0) + 1

        for col in column_map:
            if not isinstance(col, dict):
                continue
            field_api = _coerce_str(col.get("対応フィールドAPI名"))
            if not field_api or field_api == "(不明)":
                continue
            label = _coerce_str(col.get("表示名"))
            note = ""
            if seen_labels.get(label, 0) > 1:
                note = "表示名が重複"
            rows.append(
                {
                    "管理番号": key,
                    "概要": summary,
                    "SOQL列名": field_api,
                    "表示名": label,
                    "備考": note,
                }
            )
    return rows


def _build_column_names_text(
    *,
    key: str,
    summary: str,
    column_map: list[dict[str, str]],
    report_id: str,
) -> str:
    """``列名の対応_{管理番号}.txt`` の中身を組み立てる。

    Python の ``dict`` リテラル貼り付け用 （ 4 スペースインデント ）。 解決
    できなかった列は ``# 未対応: 列キー / 表示名 / 理由`` のコメント行として
    末尾に並ぶ。 表示名が重複する列は ``# 表示名が重複: 要修正`` を末尾に。
    """
    lines: list[str] = []
    lines.append(f"# 管理番号: {key}")
    if summary:
        lines.append(f"# 概要: {summary}")
    if report_id:
        lines.append(f"# レポートID: {report_id}")
    lines.append("COLUMN_NAMES = {")

    seen_labels: dict[str, int] = {}
    for col in column_map:
        if not isinstance(col, dict):
            continue
        label = _coerce_str(col.get("表示名"))
        if label:
            seen_labels[label] = seen_labels.get(label, 0) + 1

    unresolved: list[str] = []
    duplicate_labels: list[str] = []
    for col in column_map:
        if not isinstance(col, dict):
            continue
        column_key = _coerce_str(col.get("列キー"))
        label = _coerce_str(col.get("表示名"))
        field_api = _coerce_str(col.get("対応フィールドAPI名"))
        if not field_api or field_api == "(不明)":
            reason = _coerce_str(col.get("備考")) or "対応フィールドなし"
            unresolved.append(f"# 未対応: {column_key} / {label} / {reason}")
            continue
        line = f'    "{field_api}": "{label}",'
        if seen_labels.get(label, 0) > 1:
            line += "  # 表示名が重複: 要修正"
            duplicate_labels.append(label)
        lines.append(line)

    lines.extend(sorted(set(unresolved)))
    # 重複した表示名は値ではなくラベルとして末尾に列挙
    if duplicate_labels:
        lines.append("# 表示名が重複している列: " + ", ".join(sorted(set(duplicate_labels))))
    lines.append("}")
    return "\n".join(lines) + "\n"


# ── 共通ヘルパー ────────────────────────────────────────────────────────


def _resolve_column_field_api(column_map: object, column_key: str) -> str:
    """``column_map`` から ``列キー`` が一致する行の ``対応フィールドAPI名`` を返す。

    解決できた列（ ``対応フィールドAPI名`` が空でなく ``"(不明)"`` でない） だけを
    採用する。 同じ ``列キー`` が複数行に出るときは先頭を採用。 該当が無い
    ときは空文字を返す（ 列キーそのものが空のときも空文字）。
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


def _format_top_rows(top_rows: object) -> str:
    """``reportMetadata.topRows`` を人が読める 1 行にする。

    Salesforce の ``topRows`` は dict (``{"rowLimit": N, "direction": "Asc"}``)
    で来ることが多い。 そのまま ``str()`` すると Python の repr になって
    しまうので、 ``"N件 direction"`` の形に整形する。  ``rowLimit`` と
    ``direction`` は **独立に** 省略でき、 片方しか無いときはある方だけ
    出力する。 dict でないとき（ 文字列・数値 ） は従来どおり ``str()`` する
    （ 既存テスト ``topRows: "10"`` を壊さないため ）。
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
    """``picklist`` （ active な ``value`` だけの配列）を ``|`` 区切りにする。

    ``picklistTotal`` が ``PICKLIST_LIMIT``（ 既定 30 ）を超えるとき末尾に
    ``…`` を 1 個足して切る（ 業務で実際に必要になるのは数件までなので、
    30 で十分。 多すぎると CSV が読みづらくなる ） 。 ``picklist`` 自体が 30 件に
    絞られていても、 ``picklistTotal`` で「 さらに後ろがある 」 ことを検知する。
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
    を渡すと ``rows`` が空でも列が決まる（ 「 全行なし 」 の状態でも
    見出しだけの CSV ファイルができる）。 comken の
    ``CSV._write`` は途中で失敗したら ``.part`` 一時
    ファイルが片付けられ、 既存の同名ファイルは無傷で残る。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with CSV(path, columns=list(columns)) as csv:
        csv.replace(rows)


def _count_data_rows(path: Path) -> int:
    """CSV のデータ行数を返す（ 見出しを含まない ）。 ログ用に軽量に数える。

    comken の ``CSV`` クラスは読み込みでも使えるが、 ログのためだけに読み出し
    クラスをインスタンス化するのは重いため、 ここでは ``utf-8-sig`` コーデック
    ＋ ``csv.reader`` で軽量に数える。 文字コードはこのプロジェクトの CSV が
    必ず UTF-8 BOM 付きで書かれることを前提にする。
    """
    import csv as _csv

    count = 0
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = _csv.reader(file)
        for _ in reader:
            count += 1
    return max(count - 1, 0)
