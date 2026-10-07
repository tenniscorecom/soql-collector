"""src/run.py — main.py から呼ばれる入口。

``main.py`` から ``run()`` を呼ぶと ``argv`` を ``src.cli.main()`` に渡し、
その戻り値をそのまま返す。 ``argv`` が ``None`` なら ``sys.argv[1:]`` を使い、
空のときは対話メニューを起動する。 CLI 側の終了コードを捨てる箇所が無いので、
``main.py`` はその値を ``SystemExit`` に流せる。
"""

from __future__ import annotations

import logging
import sys

from src.cli import main as cli_main

logger = logging.getLogger(__name__)


def run(argv: list[str] | None = None) -> int:
    """コマンドライン引数（または対話メニュー）で cli を動かし、終了コードを返す。

    Args:
        argv: コマンドライン引数。 ``None`` なら ``sys.argv[1:]`` を使う。
            空リストなら対話メニューを起動する。

    Returns:
        ``src.cli.main()`` の戻り値。0 以外なら ``main.py`` が ``SystemExit``
        でその値を返す。
    """
    if argv is None:
        argv = sys.argv[1:]
    return cli_main(argv)
