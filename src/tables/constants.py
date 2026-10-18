"""``SOQL調査.xlsx`` / ``列名の対応.csv`` のファイル名・シート名・列名の定数。"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


# xlsx / csv 出力ファイル名 （既定値）
XLSX_NAME = "SOQL調査.xlsx"


COLUMN_NAMES_CSV_NAME = "列名の対応.csv"


# シート名 （表示順）
SHEET_OVERVIEW = "一覧"


SHEET_COLUMNS = "列の対応"


SHEET_RELATION_DIAGRAM = "関係図"


# テーブル名 （Excel テーブル、 英数字のみ）
TABLE_OVERVIEW = "T_Overview"


TABLE_COLUMNS = "T_ColumnMap"


TABLE_DIAGRAM = "T_RelationDiagram"


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


# 関係図シートの列名。 階層は「段目」の列を右へずらして表す
# （主オブジェクトの下に 1 段目、 その下に 2 段目 …）。 段目の列数は、 掘った段数に合わせて増える
RELATION_DIAGRAM_FIXED_HEAD: tuple[str, ...] = ("主オブジェクト",)


RELATION_DIAGRAM_FIXED_TAIL: tuple[str, ...] = ("参照項目API名", "SOQLでの書き方", "備考")


def relation_diagram_columns(max_depth: int) -> tuple[str, ...]:
    """関係図シートの見出し。 ``max_depth`` 段目までの列を間に挟む（最低 1 段）。"""
    levels = tuple(f"{depth}段目" for depth in range(1, max(max_depth, 1) + 1))
    return RELATION_DIAGRAM_FIXED_HEAD + levels + RELATION_DIAGRAM_FIXED_TAIL


# 列名の対応.csv の列名 （ダウンローダー固定）
COLUMN_NAMES_COLUMNS: tuple[str, ...] = (
    "主オブジェクト",
    "SOQL列名",
    "表示名",
    "備考",
    "更新日",
)
