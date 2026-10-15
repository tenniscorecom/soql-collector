"""``ReportRecord`` のリストから、 人が Excel で見るブックとダウンローダー用
CSV を組み立てる。

入力は ``ReportRecord`` のイテラブル。 出力は同じ ``OUTPUT_DIR`` に次の
2 種類のファイルを書く:

- ``SOQL調査.xlsx`` … 人が見る 1 冊のブック。 毎回その回の結果で丸ごと
  作り直す。 シートは **3 枚** （ 一覧 / 列の対応 / 関係図 ） 、 すべて
  Excel のテーブル （ 構造化 ） にする。 comken の
  ``Sheet.create_table(name, table, start_cell)`` を使う
- ``列名の対応.csv`` … ダウンローダーが SOQL レポートの列名の置き換えに使う
  CSV。 全レポート分を積み上げる。 その回に取れた ``(主オブジェクト,
  SOQL列名)`` の行は今回書いた内容で入れ替わり、 他の行はそのまま残る

``run_fetch`` が ``Record`` を 1 件も作れなかったときは ``SOQL調査.xlsx``
は作らず、 ``列名の対応.csv`` も何もしない （ 既存もそのまま ） 。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from comken.core.table import Table
from comken.exceptions import CSVError
from comken.toolbox.csv import CSV

from src import ai_markdown, pii, relation_diagram, similar, xlsx_style

logger = logging.getLogger(__name__)

# 1 つの CSV ファイルあたりの選択肢の最大値数。 超えたら末尾に ``…`` を付ける
PICKLIST_LIMIT = 30

# 「 複数候補あり 」 備考に並べる鎖+項目の最大件数
MULTIPLE_CANDIDATES_LIMIT = 5

# 一覧 / 列の対応 シートの 1 行目に出す案内文
_ANNOUNCEMENT_TEXT = (
    "直すときは input/列の直し.xlsx の『直したSOQL列名』に書いて、もう一度実行してください。"
)

# 警告 「 オブジェクト {name} の describe に失敗 … 」 を (name, reason) に分ける
DESCRIBE_FAILURE_PATTERN = re.compile(r"^オブジェクト (.+?) の describe に失敗(.*)$")

# 1 レポートの中で同じ表示名が複数回現れたときの 取得できなかった項目 シートの理由
DUPLICATE_LABEL_REASON = "同じレポート内で表示名が重複"

# xlsx / csv 出力ファイル名 （ 既定値 ）
XLSX_NAME = "SOQL調査.xlsx"
COLUMN_NAMES_CSV_NAME = "列名の対応.csv"


# ── シート名・列名定数 ────────────────────────────────────────────────────

# シート名 （ 表示順 ）
SHEET_OVERVIEW = "一覧"
SHEET_COLUMNS = "列の対応"
SHEET_RELATION_DIAGRAM = "関係図"

SHEET_NAMES_IN_ORDER: tuple[str, ...] = (
    SHEET_OVERVIEW,
    SHEET_COLUMNS,
    SHEET_RELATION_DIAGRAM,
)

# テーブル名 （ Excel テーブル、 英数字のみ ）
TABLE_OVERVIEW = "T_Overview"
TABLE_COLUMNS = "T_ColumnMap"
TABLE_DIAGRAM = "T_RelationDiagram"

# 状態列の色は ``xlsx_style.FILL_NEEDS_FIX`` / ``xlsx_style.FILL_NEEDS_CHECK`` を参照

# 一覧シートの列名
SURVEY_COLUMNS: tuple[str, ...] = (
    "管理番号",
    "概要",
    "主オブジェクト",
    "SOQLの状態",
    "要対応・要確認の列数",
    "2000件超",
    "行数",
    "個人情報",
    "個人情報の該当列",
    "似たレポート",
    "警告",
    "SOQL資料",
)

# 列の対応シートの列名
COLUMNS_SHEET_COLUMNS: tuple[str, ...] = (
    "状態",
    "管理番号",
    "概要",
    "表示名",
    "列キー",
    "使うSOQL列名",
    "どこから",
    "候補",
    "自動の値",
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
    """``(主オブジェクト, SOQL列名)`` で集約した 1 つの解決済みフィールド。"""

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
    xlsx_name: str = XLSX_NAME,
) -> TableOutcome:
    """``records`` から ``SOQL調査.xlsx`` と ``列名の対応.csv`` を組み立てる。

    Args:
        output_dir: 出力フォルダ。 無ければ作る。
        records: ``fetch.ReportRecord`` のイテラブル。
        only_keys: 互換性のために受け取るが今回は使われない。
        pii_config: PII 列を埋めるための設定。 ``None`` ならデフォルト。
        similar_threshold: 似たレポート判定の Jaccard 下限。
        xlsx_name: 出力する xlsx のファイル名。 既定 ``SOQL調査.xlsx``。

    Returns:
        ``TableOutcome``: 書いたファイル一覧。 ``skipped`` は常に空。
    """
    del only_keys  # per-ID CSV は作らないので無視
    record_list = list(records)
    pii_config = pii_config or pii.PIIConfig(
        keywords=pii.DEFAULT_KEYWORDS,
        person_objects=pii.DEFAULT_PERSON_OBJECTS,
    )

    sorted_records = sorted(record_list, key=lambda r: getattr(r, "key", ""))
    ok_keys = [getattr(r, "key", "") for r in sorted_records]

    # PII lookup を record 単位で組み立てる
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

    # 似たレポート判定
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

    # 解決済みフィールドの集約
    resolved_fields = _aggregate_resolved_fields(sorted_records, pii_lookups)

    # 警告・要対応件数
    warning_rows = _build_warning_table_rows_from_records(sorted_records)
    unresolved_rows = _build_unresolved_rows_from_records(sorted_records)
    unresolved_counts: dict[str, int] = {}
    for row in unresolved_rows:
        for key in row["管理番号"].split("|"):
            if key:
                unresolved_counts[key] = unresolved_counts.get(key, 0) + 1

    # 関係図
    diagrams = relation_diagram.build_main_object_diagrams(sorted_records)
    relation_diagram_rows = _build_relation_diagram_rows(diagrams)

    # 各シートの行を作る
    survey_rows = _build_survey_table_rows_from_records(
        sorted_records,
        similar_lookup=key_to_group,
        unresolved_counts=unresolved_counts,
        warnings=warning_rows,
    )
    columns_rows = _build_columns_sheet_rows_from_records(
        sorted_records,
        pii_lookups=pii_lookups,
    )

    wrote: list[Path] = []

    if sorted_records:
        xlsx_path = output_dir / xlsx_name
        _write_xlsx(
            xlsx_path,
            survey=survey_rows,
            columns=columns_rows,
            relation_diagram=relation_diagram_rows,
        )
        wrote.append(xlsx_path)
        logger.info(
            "Excel を書き出し: %s（ 一覧=%d / 列の対応=%d / 関係図=%d ）",
            xlsx_path,
            len(survey_rows),
            len(columns_rows),
            len(relation_diagram_rows),
        )

    # 列名の対応.csv （ 積み上げ ）
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
        logger.info(
            "CSV は既存を維持: %s（ record なしのため新規/置換なし ）",
            column_names_path,
        )
    if column_names_path.exists():
        wrote.append(column_names_path)

    # 1 レポート 1 つの Markdown 資料を ``SOQL資料/{管理番号}.md`` に書く。
    # AI に SOQL を組ませるときにそのまま渡す用途。 既存のファイルはそのまま
    # 残し、 今回取れた record の分だけ上書きする。 似たレポートの情報を
    # ``similar_lookup`` で渡すと、 Markdown の 「 似たレポート 」 セクションに
    # 反映される
    wrote.extend(
        ai_markdown.write_ai_markdowns(output_dir, sorted_records, similar_lookup=key_to_group)
    )

    return TableOutcome(wrote=tuple(wrote), skipped=())


# ── 一覧 ──────────────────────────────────────────────────────────────


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
    """``record.soql_draft.status`` をそのまま返す （ 無ければ空文字 ） 。"""
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
    warnings: list[dict[str, str]],
) -> list[dict[str, str]]:
    """全 record から 一覧 の行を作る。

    警告は最初に見つかった 1 件を 1 行のテキストにまとめる
    """
    # 警告を管理番号ごとに集約
    warnings_by_key: dict[str, list[str]] = {}
    for w in warnings:
        key = w.get("管理番号", "")
        if not key:
            continue
        warnings_by_key.setdefault(key, []).append(w.get("警告", ""))

    rows: list[dict[str, str]] = []
    for record in records:
        key = getattr(record, "key", "")
        summary = getattr(record, "summary", "")
        main_object = getattr(record, "main_object", "") or ""
        status, _rows, _reason = _row_check_status_text(record)
        pii_status, pii_columns = _survey_pii_display(record)
        record_warnings = warnings_by_key.get(key, [])
        # 似たレポート
        group_info = similar_lookup.get(key)
        if group_info is not None:
            group_number, base_key, _integration = group_info
            if base_key == key:
                group_text = f"グループ{group_number}: {key} が基準"
            else:
                group_text = f"グループ{group_number}: {base_key} が基準・{key} 違い"
        else:
            group_text = ""
        # 警告を 1 行のサマリに
        if record_warnings:
            warning_summary = f"{len(record_warnings)}件: " + record_warnings[0]
            if len(warning_summary) > 200:
                warning_summary = warning_summary[:197] + "…"
        else:
            warning_summary = ""
        rows.append(
            {
                "管理番号": key,
                "概要": summary,
                "主オブジェクト": main_object or "(不明)",
                "SOQLの状態": soql_status_for_record(record),
                "要対応・要確認の列数": str(unresolved_counts.get(key, 0)),
                "2000件超": status,
                "行数": _rows,
                "個人情報": pii_status,
                "個人情報の該当列": pii_columns,
                "似たレポート": group_text,
                "警告": warning_summary,
                "SOQL資料": f"SOQL資料/{key}.md" if key else "",
            }
        )
    return rows


# ── 列の対応 ──────────────────────────────────────────────────────


def _build_columns_sheet_rows_from_records(
    records: Sequence[object],
    *,
    pii_lookups: dict[str, dict[str, tuple[str, str]]],
) -> list[dict[str, str]]:
    """列の対応シートの行を組み立てる。 1 行 = 1 レポートの 1 列。

    並び: 管理番号 → 列キーの順 （ レポートに並んだ順 ）
    """
    rows: list[dict[str, str]] = []
    for record in records:
        key = getattr(record, "key", "")
        summary = getattr(record, "summary", "")
        column_map = getattr(record, "column_map", []) or []
        for col in column_map:
            if not isinstance(col, dict):
                continue
            display_name = _coerce_str(col.get("表示名"))
            column_key = _coerce_str(col.get("列キー"))
            soql_name = _coerce_str(col.get("対応フィールドAPI名"))
            note = _coerce_str(col.get("備考"))
            # 人が直した値の判定。 備考に 「 直したSOQL列名 」 または
            # 「 人が直した（要確認: たどれない） 」 が入っていれば、 人の値が
            # column_map に上書きされている
            user_fix_failed = "人が直した（要確認: たどれない）" in note
            # 状態
            if not soql_name or soql_name == "(不明)":
                # 候補が複数あるか
                if "複数候補あり" in note:
                    status = "要確認"
                else:
                    status = "要対応"
            elif user_fix_failed:
                # 人の値は column_map に乗っているが、 項目定義でたどれなかった
                status = "要確認"
            else:
                status = "OK"
            # どこから
            if user_fix_failed:
                where = "人が直した（要確認: たどれない）"
            elif "直したSOQL列名" in note:
                where = "人が直した"
            elif soql_name and soql_name != "(不明)":
                # 自動が取れた。 備考は 「 列キーから 」 / 「 FK_ から 」 など
                where = "自動"
            else:
                where = "取れず"
            # 候補
            candidates = ""
            if "複数候補あり" in note:
                candidates = note.replace("複数候補あり: ", "")
            # 自動の値（ 人が直した値と、 人が直す前に自動が取っていた値が違うとき ） は
            # column_map の段階で 「 直したSOQL列名 」 で上書きされて元の自動の値を
            # 取れないので、 人が直した事実は 「 どこから 」 列で見せる。 ここでは空のまま
            auto_value = ""
            rows.append(
                {
                    "状態": status,
                    "管理番号": key,
                    "概要": summary,
                    "表示名": display_name,
                    "列キー": column_key,
                    "使うSOQL列名": soql_name if soql_name else "(不明)",
                    "どこから": where,
                    "候補": candidates,
                    "自動の値": auto_value,
                }
            )
    return rows


# ── 列名の対応.csv ────────────────────────────────────────────


def _aggregate_resolved_fields(
    records: Sequence[object],
    pii_lookups: dict[str, dict[str, tuple[str, str]]],
) -> list[_ResolvedField]:
    """全 record の解決済み column_map エントリを ``(主オブジェクト, SOQL列名)`` で集約する。"""
    groups: dict[tuple[str, str], list[tuple[str, dict, str]]] = {}
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
            # 人が直した値で上書きされている場合、 備考に 「 直したSOQL列名 」 を含むが
            # CSV に出すのは実 SOQL 列名で良い
            column_key = _coerce_str(col.get("列キー"))
            pii_status, pii_basis = pii_lookup.get(column_key, ("", ""))
            dedup_key = (main_object_text, field_api)
            groups.setdefault(dedup_key, []).append((key, col, pii_status + "\t" + pii_basis))

    fields: list[_ResolvedField] = []
    for (main_object, soql_name), contributions in groups.items():
        keys = sorted({row[0] for row in contributions})
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

        other_parts: list[str] = []
        for label, min_key in label_min_key.items():
            if label == best_label:
                continue
            other_parts.append(f"{label}（{min_key}）")
        other_note = ""
        if other_parts:
            other_note = "別の表示名あり: " + ", ".join(other_parts)

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

    fields.sort(key=lambda f: (f.main_object, f.soql_name))
    return fields


def _resolved_fields_to_csv_rows(
    fields: Sequence[_ResolvedField], today: date
) -> list[dict[str, str]]:
    """``列名の対応.csv`` 用の行 dict を作る。"""
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


# ── 警告・要対応 ─────────────────────────────────────────────


def _build_warning_table_rows_from_records(
    records: Sequence[object],
) -> list[dict[str, str]]:
    """警告シートの行を作る。 describe 失敗 ( 取得できなかった項目シートが拾う ) は除く。"""
    rows: list[dict[str, str]] = []
    for record in records:
        key = getattr(record, "key", "")
        warnings = getattr(record, "warnings", ()) or ()
        for warning in warnings:
            warning_text = _coerce_str(warning)
            if _parse_describe_failure_warning(warning_text) is not None:
                continue
            rows.append({"管理番号": key, "警告": warning_text})
    return rows


def _build_unresolved_rows_from_records(
    records: Sequence[object],
) -> list[dict[str, str]]:
    """要対応/要確認の件数を数えるため、 既存の集計ロジック （ 旧 「 取得できなかった
    項目 」 シート相当 ） を動かす。 1 レポート 1 件として 件数 を出している
    """
    field_entries: dict[tuple[str, str, str], dict[str, str]] = {}
    field_keys: dict[tuple[str, str, str], set[str]] = {}

    for record in records:
        key = getattr(record, "key", "")
        main_object_text = getattr(record, "main_object", "") or "(不明)"
        column_map = getattr(record, "column_map", []) or []

        if isinstance(column_map, list):
            for col in column_map:
                if not isinstance(col, dict):
                    continue
                field_api = _coerce_str(col.get("対応フィールドAPI名"))
                note_text = _coerce_str(col.get("備考"))
                # 人が直した値がたどれなかった行も 「 要確認 」 として数える
                user_fix_unresolved = "人が直した（要確認: たどれない）" in note_text
                if field_api and field_api != "(不明)" and not user_fix_unresolved:
                    continue
                column_key = _coerce_str(col.get("列キー"))
                reason = note_text or "対応フィールドなし"
                dedup_key = (main_object_text, column_key, reason)
                if dedup_key not in field_entries:
                    field_entries[dedup_key] = {
                        "種別": "項目",
                        "主オブジェクト": main_object_text,
                        "列キー": column_key,
                        "表示名": _coerce_str(col.get("表示名")),
                        "理由": reason,
                    }
                    field_keys[dedup_key] = set()
                field_keys[dedup_key].add(key)

    field_rows: list[dict[str, str]] = []
    for dedup_key, base in field_entries.items():
        keys = sorted(field_keys[dedup_key])
        row = dict(base)
        row["件数"] = str(len(keys))
        row["管理番号"] = "|".join(keys)
        field_rows.append(row)

    return field_rows


def _parse_describe_failure_warning(warning: str) -> tuple[str, str] | None:
    """警告 「 オブジェクト X の describe に失敗… 」 から ``(name, reason)`` を取り出す。"""
    if not warning.startswith("オブジェクト "):
        return None
    match = DESCRIBE_FAILURE_PATTERN.match(warning)
    if match is None:
        return None
    object_name = match.group(1)
    reason = match.group(2).lstrip("：: ")
    return object_name, reason


# ── PII 関連 ──────────────────────────────────────────────────


def _build_pii_lookup_for_record(
    *,
    report: dict,
    column_map: list[dict[str, str]],
    objects: dict,
    config: pii.PIIConfig,
) -> dict[str, tuple[str, str]]:
    """1 record ぶんの ``列キー → (個人情報, 個人情報の根拠)`` を作る。"""
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


# ── 関係図 ──────────────────────────────────────────────────


def _build_relation_diagram_rows(
    diagrams: dict[str, relation_diagram.Diagram],
) -> list[dict[str, str]]:
    """``diagrams`` （ 主オブジェクト → ``Diagram`` ） から 関係図シートの行を作る。"""
    rows: list[dict[str, str]] = []
    for main_object, diagram in diagrams.items():
        for depth, label, soql_hint, field_api, _note, target in _iter_diagram_rows(
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
    return rows


def _iter_diagram_rows(
    diagram: relation_diagram.Diagram,
    *,
    depth: int,
) -> list[tuple[int, str, str, str, str, str]]:
    """``Diagram`` の木を ``(段, 図, SOQLでの書き方, 参照項目API名, 注記, 行き先)``
    のリストに展開。"""
    out: list[tuple[int, str, str, str, str, str]] = []
    if depth == 0:
        out.append((0, diagram.root.label, "", "", "", diagram.main_object))
    for child in diagram.root.children:
        target = _extract_target(child.label)
        soql_hint = _build_soql_hint(diagram.main_object, child, diagram)
        note = child.note
        label_with_note = _format_label_with_note(child)
        out.append((depth + 1, label_with_note, soql_hint, child.field_api, note, target))
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
    _ = parent
    target = _extract_target(child.label)
    note = child.note
    soql_hint = _build_soql_hint(diagram.main_object, child, diagram)
    label_with_note = _format_label_with_note(child)
    out.append((current_depth, label_with_note, soql_hint, child.field_api, note, target))
    if child.children and not child.note:
        for grandchild in child.children:
            _emit_descendants(out, child, grandchild, diagram, current_depth=current_depth + 1)


def _extract_target(label: str) -> str:
    if " → " in label:
        return label.split(" → ", 1)[1]
    return label


def _format_label_with_note(node: relation_diagram.DiagramNode) -> str:
    if node.note:
        return f"{node.label} {node.note}"
    return node.label


def _build_soql_hint(
    main_object: str,
    child: relation_diagram.DiagramNode,
    diagram: relation_diagram.Diagram,
) -> str:
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


# ── 列名の対応.csv 書き出し ────────────────────────────────────


def _merge_column_names_csv(
    path: Path,
    new_rows: Sequence[dict[str, str]],
    replaced_keys: Iterable[str],
) -> None:
    """``列名の対応.csv`` を既存ファイルと ``new_rows`` でマージして書き直す。

    並びは **主オブジェクト → SOQL列名** 。
    既存ファイルの見出しが ``COLUMN_NAMES_COLUMNS`` と違うときは ``CSVError`` を上げる。
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    existing_rows: list[dict[str, str]] = []
    if path.exists():
        try:
            with CSV(path) as csv:
                table = csv.read()
        except CSVError as exc:
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

    merged.sort(key=lambda row: (row.get("主オブジェクト", ""), row.get("SOQL列名", "")))
    _write_csv(path, COLUMN_NAMES_COLUMNS, merged)


# ── Excel 書き出し ────────────────────────────────────────────


def _write_xlsx(
    path: Path,
    *,
    survey: list[dict[str, str]],
    columns: list[dict[str, str]],
    relation_diagram: list[dict[str, str]],
) -> None:
    """``SOQL調査.xlsx`` を書く。 シート 3 枚、 すべて Excel のテーブル。"""
    if path.exists():
        path.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)

    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font

    workbook = Workbook()
    # デフォルトの Sheet を消す
    default_sheet = workbook.active
    if default_sheet is not None:
        workbook.remove(default_sheet)

    # 一覧シート
    overview = workbook.create_sheet(SHEET_OVERVIEW)
    overview["A1"] = _ANNOUNCEMENT_TEXT
    overview["A1"].font = Font(bold=True)
    overview["A1"].alignment = Alignment(wrap_text=True)
    _write_table(
        overview,
        headers=SURVEY_COLUMNS,
        rows=survey,
        start_row=3,
        start_col=1,
        table_name=TABLE_OVERVIEW,
    )
    _adjust_column_widths(overview, SURVEY_COLUMNS, survey, start_row=3)
    overview.freeze_panes = "A3"

    # 列の対応シート
    cols_sheet = workbook.create_sheet(SHEET_COLUMNS)
    cols_sheet["A1"] = _ANNOUNCEMENT_TEXT
    cols_sheet["A1"].font = Font(bold=True)
    cols_sheet["A1"].alignment = Alignment(wrap_text=True)
    _write_table(
        cols_sheet,
        headers=COLUMNS_SHEET_COLUMNS,
        rows=columns,
        start_row=3,
        start_col=1,
        table_name=TABLE_COLUMNS,
    )
    _apply_status_fills(cols_sheet, COLUMNS_SHEET_COLUMNS, columns, start_row=3)
    _adjust_column_widths(cols_sheet, COLUMNS_SHEET_COLUMNS, columns, start_row=3)
    cols_sheet.freeze_panes = "A3"

    # 関係図シート
    diagram_sheet = workbook.create_sheet(SHEET_RELATION_DIAGRAM)
    _write_table(
        diagram_sheet,
        headers=RELATION_DIAGRAM_COLUMNS,
        rows=relation_diagram,
        start_row=1,
        start_col=1,
        table_name=TABLE_DIAGRAM,
    )
    _adjust_column_widths(diagram_sheet, RELATION_DIAGRAM_COLUMNS, relation_diagram, start_row=1)
    diagram_sheet.freeze_panes = "A2"

    workbook.save(path)


def _write_table(
    worksheet: Any,
    *,
    headers: Sequence[str],
    rows: list[dict[str, str]],
    start_row: int,
    start_col: int,
    table_name: str,
) -> None:
    """openpyxl で 「 見出し行 + データ行 + Excel テーブル 」 を一度に書く。"""
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter as _gcl
    from openpyxl.worksheet.table import Table as XlsxTable
    from openpyxl.worksheet.table import TableStyleInfo

    # 見出し
    for col_offset, header in enumerate(headers):
        cell = worksheet.cell(row=start_row, column=start_col + col_offset, value=header)
        cell.font = Font(bold=True)
    # データ
    for row_offset, row in enumerate(rows):
        for col_offset, header in enumerate(headers):
            worksheet.cell(
                row=start_row + 1 + row_offset,
                column=start_col + col_offset,
                value=row.get(header, ""),
            )
    end_row = start_row + len(rows)
    end_col = start_col + len(headers) - 1
    if end_row <= start_row:
        # データが無いときは Excel テーブルが作れないのでヘッダーだけにする
        return
    ref = f"{_gcl(start_col)}{start_row}:{_gcl(end_col)}{end_row}"
    table = XlsxTable(displayName=table_name, ref=ref)
    table.tableStyleInfo = TableStyleInfo(
        name="TableStyleMedium2",
        showFirstColumn=False,
        showLastColumn=False,
        showRowStripes=True,
        showColumnStripes=False,
    )
    worksheet.add_table(table)


def _adjust_column_widths(
    worksheet: Any,
    headers: Sequence[str],
    rows: list[dict[str, str]],
    *,
    start_row: int,
) -> None:
    """``xlsx_style.adjust_column_widths`` への薄いラッパー。"""
    xlsx_style.adjust_column_widths(
        worksheet,
        headers,
        rows,
        start_row=start_row,
    )


def _apply_status_fills(
    worksheet: Any,
    headers: Sequence[str],
    rows: list[dict[str, str]],
    *,
    start_row: int,
) -> None:
    """状態列の値でセルを塗り分ける。 要対応=薄い赤、 要確認=薄い黄。"""
    from openpyxl.styles import PatternFill

    if "状態" not in headers:
        return
    # start_row は見出し行、 データは start_row+1 から
    fill_fix = PatternFill(
        start_color=xlsx_style.FILL_NEEDS_FIX,
        end_color=xlsx_style.FILL_NEEDS_FIX,
        fill_type="solid",
    )
    fill_check = PatternFill(
        start_color=xlsx_style.FILL_NEEDS_CHECK,
        end_color=xlsx_style.FILL_NEEDS_CHECK,
        fill_type="solid",
    )
    for row_offset, row in enumerate(rows):
        status = row.get("状態", "")
        if status == "要対応":
            fill = fill_fix
        elif status == "要確認":
            fill = fill_check
        else:
            continue
        for col_index in range(1, len(headers) + 1):
            cell = worksheet.cell(row=start_row + 1 + row_offset, column=col_index)
            cell.fill = fill


# ── 共通ヘルパー ────────────────────────────────────────────


def _today_local() -> date:
    """「 今日の日付 」 をこの PC のローカル時間で返す。"""
    return datetime.now(tz=UTC).astimezone().date()


def _coerce_str(value: object) -> str:
    """値を ``str`` にする。 ``None`` は空文字。"""
    if value is None:
        return ""
    return str(value)


def _write_csv(path: Path, columns: Sequence[str], rows: list[dict[str, str]]) -> None:
    """comken の ``CSV`` クラスで UTF-8 BOM + CRLF の CSV を書く。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with CSV(path, columns=list(columns)) as csv:
        csv.replace(Table(list(columns), rows))
