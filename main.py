"""
main.py — エントリポイント

このプロジェクトの入口。`実行.bat` か `python main.py` で実行する。
取るレポートは管理表の「有効」列が ○ の行で決まる。引数は使わない。

処理の本体は src/ 以下に書き、ここでは「実行 → エラーの受け止め」だけを行う。
社内 RPA 基盤から動かす場合は、下の「社内 RPA 基盤から実行する場合」を参照。
"""

from comken import comken_logger
from comken.runtime import run_main

from src.run import run


def main() -> None:
    """プロジェクト処理の入口。

    RPA 基盤から呼ばれる関数を薄く保ち、業務処理を ``src/run.py`` に分離する。
    こうするとテンプレートの起動方法と業務コードの責務が混ざらない。
    """
    # 設定の読み取りも処理も src/run.py に書く。ここは呼ぶだけにしておく
    code = run()
    # ``run()`` の終了コードを捨てない（ ``実行.bat`` / RPA が受け取れるようにする）
    if code:
        raise SystemExit(code)


if __name__ == "__main__":
    # 単体で動かすので、ログの出力先をここで用意する（コンソールと logs/local-YYYY-MM-DD.log）。
    # 動作確認だけしたいときは保存・送信をスキップできる:
    #   from comken import dry_run
    #   with dry_run():
    #       main()
    #
    # 画面・ファイルとも DEBUG まで出したいときは、setup_local_logging() を debug(): の中で呼ぶ
    # （console_level / file_level を明示しなければ自動で DEBUG になる）:
    #   from comken import debug
    #   with debug():
    #       comken_logger.setup_local_logging()
    #       main()
    #
    # いつもの定型は comken.runtime.run_main に集めた:
    #   comken_logger.setup_local_logging() は呼び出し側で 1 回呼ぶ
    #   run_main は ComkenError を error ログして終了コード 1、 想定外は logger.exception して再送出
    comken_logger.setup_local_logging()
    raise SystemExit(run_main(main))

# ── 社内 RPA 基盤から実行する場合 ─────────────────────────────────────────────
# 上の `comken_logger.setup_local_logging()` と `run_main(main)` を、 次の形に差し替える。
# 基盤が設定の初期化・時間計測・ログ設定をしてから main を呼ぶので、
# comken_logger.setup_local_logging() は呼ばない（呼んでも二重設定にはならないが、
# 基盤の設定が正になる）。
#
#     from comken.toolbox.rpa import backoffice   # イントラネットのツールなら intranet に変える
#     import logging
#
#     PROJECT_NAME = "soql-collector"   # 基盤へ渡す名前。ログの識別に使われる
#     logger = logging.getLogger(__name__)
#
#     if __name__ == "__main__":
#         try:
#             backoffice(main, PROJECT_NAME)
#         except ComkenError as e:
#             logger.error("処理を中断しました: %s", e)
#             raise
