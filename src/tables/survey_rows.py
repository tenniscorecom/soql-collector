"""一覧シートと警告の行を作る。"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from typing import TYPE_CHECKING

from src import ai_markdown

if TYPE_CHECKING:
    from src.fetch import ReportRecord

logger = logging.getLogger(__name__)


# 警告 「オブジェクト {name} の describe に失敗 …」 を (name, reason) に分ける
DESCRIBE_FAILURE_PATTERN = re.compile(r"^オブジェクト (.+?) の describe に失敗(.*)$")


def _row_check_status_text(record: ReportRecord) -> tuple[str, str, str]:
    """record の ``row_check`` を ``(status_text, rows_text, reason_text)`` に分解する。"""
    row_check = record.row_check
    if row_check is None:
        return ("未実施", "", "")
    rows = row_check.rows
    reason = row_check.reason or ""
    rows_text = "" if rows is None else str(rows)
    return (row_check.status, rows_text, reason)


def _survey_pii_display(record: ReportRecord) -> tuple[str, str]:
    """record の ``pii`` を ``(表示用ステータス, 該当列)`` に直す。"""
    pii_result = record.pii
    if pii_result is None:
        return ("", "")
    formatted = "; ".join(f"{label}({basis})" for label, basis in pii_result.matching_columns)
    return (pii_result.status, formatted)


def soql_status_for_record(record: ReportRecord) -> str:
    """``record.soql_draft.status`` をそのまま返す （無ければ空文字） 。"""
    return record.soql_draft.status if record.soql_draft is not None else ""


def build_survey_table_rows_from_records(
    records: Sequence[ReportRecord],
    *,
    similar_lookup: dict[str, tuple[int, str, str]],
    unresolved_counts: dict[str, int],
    warnings: list[dict[str, str]],
    markdown_dir: str = ai_markdown.MARKDOWN_SUBDIR,
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
        key = record.key
        summary = record.summary
        main_object = record.main_object or ""
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
                "SOQL資料": f"{markdown_dir}/{key}.md" if key else "",
            }
        )
    return rows


def build_warning_table_rows_from_records(
    records: Sequence[ReportRecord],
) -> list[dict[str, str]]:
    """警告の行を作る。 describe 失敗（列の対応の 「取れず」 で分かる）は除く。"""
    return [
        {"管理番号": record.key, "警告": warning}
        for record in records
        for warning in record.warnings
        if _parse_describe_failure_warning(warning) is None
    ]


def _parse_describe_failure_warning(warning: str) -> tuple[str, str] | None:
    """警告 「オブジェクト X の describe に失敗…」 から ``(name, reason)`` を取り出す。"""
    if not warning.startswith("オブジェクト "):
        return None
    match = DESCRIBE_FAILURE_PATTERN.match(warning)
    if match is None:
        return None
    object_name = match.group(1)
    reason = match.group(2).lstrip("：: ")
    return object_name, reason
