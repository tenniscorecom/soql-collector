"""列の対応シートの行を作る。"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING

from src import mapping

if TYPE_CHECKING:
    from src.fetch import ReportRecord

logger = logging.getLogger(__name__)


def build_columns_sheet_rows_from_records(
    records: Sequence[ReportRecord],
    *,
    pii_lookups: dict[str, dict[str, tuple[str, str]]],
) -> list[dict[str, str]]:
    """列の対応シートの行を組み立てる。 1 行 = 1 レポートの 1 列。

    並び: 管理番号 → 列キーの順 （レポートに並んだ順）
    """
    rows: list[dict[str, str]] = []
    for record in records:
        key = record.key
        summary = record.summary
        column_map = record.column_map
        for col in column_map:
            display_name = col["表示名"]
            column_key = col["列キー"]
            soql_name = col["対応フィールドAPI名"]
            status = mapping.column_status(col)
            where = mapping.column_source(col)
            candidates = mapping.column_candidates(col)
            # 自動の値は、 人が直して自動と違う値になったときだけ見せる
            auto = col.get("自動の値", "")
            auto_value = auto if auto and auto != soql_name else ""
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
