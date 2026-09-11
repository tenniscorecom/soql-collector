"""
src/run.py — 処理の本体

main.py から呼ばれる。ここに「実際にやりたいこと」を書く。
下は「CSV を読んで Excel レポートを作る」最小例。不要なら丸ごと書き換えてよい。
"""

import logging

from comken import config

logger = logging.getLogger(__name__)

SHEET = "Sheet1"


def run() -> None:
    """処理の入口。main.py から呼ばれる。"""
    # config.SECTION.KEY で config.ini（プロジェクト直下）の値を読む。
    # config.FILES. まで打つと Pylance で補完が出る（typings スタブは自動生成）。
    output_folder = config.FILES.OUTPUT_FOLDER

    # ── ここに処理を書く ──────────────────────────────────────────────
    # 例:
    #   from comken.core import DateNameBuilder
    #   from comken.toolbox.csv import CSV
    #   from comken.toolbox.excel import Excel
    #
    #   with CSV(config.FILES.INPUT_CSV, read_only=True) as csv_file:
    #       table = csv_file.read()
    #   out = output_folder / DateNameBuilder("レポート").prefix()
    #   with Excel(out) as excel:
    #       excel.create_data_sheet(SHEET).create_table(SHEET, table)
    #   logger.info("出力しました: %s", out)
    # ──────────────────────────────────────────────────────────────────
    logger.info("run() を実装してください（出力先: %s）", output_folder)
