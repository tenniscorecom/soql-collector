"""``ReportRecord`` のリストから、 人が Excel で見るブックとダウンローダー用
CSV を組み立てる。

入力は ``ReportRecord`` のイテラブル。 出力は同じ ``OUTPUT_DIR`` に次の
2 種類のファイルを書く:

- ``SOQL調査.xlsx`` … 人が見る 1 冊のブック。 毎回その回の結果で丸ごと
  作り直す。 シートは **3 枚** （一覧 / 列の対応 / 関係図） 、 すべて
  Excel のテーブル （構造化） にする。 comken の
  ``Sheet.create_table(name, table, start_cell)`` を使う
- ``列名の対応.csv`` … ダウンローダーが SOQL レポートの列名の置き換えに使う
  CSV。 全レポート分を積み上げる。 その回に取れた ``(主オブジェクト,
  SOQL列名)`` の行は今回書いた内容で入れ替わり、 他の行はそのまま残る

``run_fetch`` が ``Record`` を 1 件も作れなかったときは ``SOQL調査.xlsx``
は作らず、 ``列名の対応.csv`` も何もしない （既存もそのまま） 。

ファイルの構成:

- ``build.py``: ``run_tables``（全体の流れ）
- ``survey_rows.py``: 一覧シート・警告の行
- ``columns_rows.py``: 列の対応シートの行
- ``column_names_csv.py``: ``列名の対応.csv`` の行と積み上げ
- ``diagram_rows.py``: 関係図シートの行
- ``xlsx_out.py``: xlsx の書き出し
- ``constants.py``: ファイル名・シート名・列名の定数
"""

from src.tables.build import TableOutcome, run_tables
from src.tables.constants import (
    COLUMN_NAMES_COLUMNS,
    COLUMN_NAMES_CSV_NAME,
    COLUMNS_SHEET_COLUMNS,
    SHEET_COLUMNS,
    SHEET_OVERVIEW,
    SHEET_RELATION_DIAGRAM,
    SURVEY_COLUMNS,
    TABLE_COLUMNS,
    TABLE_DIAGRAM,
    TABLE_OVERVIEW,
    XLSX_NAME,
    relation_diagram_columns,
)

__all__ = [
    "COLUMNS_SHEET_COLUMNS",
    "COLUMN_NAMES_COLUMNS",
    "COLUMN_NAMES_CSV_NAME",
    "SHEET_COLUMNS",
    "SHEET_OVERVIEW",
    "SHEET_RELATION_DIAGRAM",
    "SURVEY_COLUMNS",
    "TABLE_COLUMNS",
    "TABLE_DIAGRAM",
    "TABLE_OVERVIEW",
    "XLSX_NAME",
    "TableOutcome",
    "relation_diagram_columns",
    "run_tables",
]
