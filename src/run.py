"""src/run.py — main.py から呼ばれる入口。

``実行.bat`` または ``python main.py`` から呼ばれると、 ``argv`` を
``sys.argv[1:]`` として受け取り、 管理表の「有効」列が ``○`` の行すべての
describe を取って ``ReportRecord`` に詰める。 その record の一覧を
``run_tables`` に渡して CSV を組み立てる。 JSON は書かない。

終了コード:

- 0 … 全件成功
- 1 … 1 件以上のレポートが失敗した
- 2 … 引数が付いた / 管理表に「有効」が ○ の行が無い

`main.py` はこの ``run()`` の戻り値を ``SystemExit`` に流す。
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from typing import Any

from comken.runtime import is_dry_run
from comken.toolbox.salesforce.report import report_id_from_url

from src.fetch import FetchOutcome, run_fetch
from src.master import MasterEntry, filter_enabled, read_master
from src.settings import Settings, load_settings
from src.tables import run_tables

logger = logging.getLogger(__name__)

# ``run_fetch`` の ``site_for`` と同じ型（ テストで差し替えるため ）
SiteFor = Callable[[str], type]


def run(argv: list[str] | None = None, *, site_for: SiteFor | None = None) -> int:
    """``実行.bat`` / RPA から呼ばれる入口。

    Args:
        argv: コマンドライン引数。 ``None`` なら ``sys.argv[1:]`` を使う。
            1 件でも指定されたら何もしないでエラー終了（ 終了コード 2 ）。
        site_for: URL → Salesforce サイトクラスの関数。 テスト用差し替え。

    Returns:
        0 … 全件成功
        1 … 失敗した ID が 1 件以上ある
        2 … 引数が付いた / 管理表に「有効」が ○ の行が無い
    """
    if argv is None:
        argv = sys.argv[1:]
    if argv:
        logger.error("引数は使いません。取るレポートは管理表の『有効』列が ○ の行で決まります")
        return 2

    settings = load_settings()
    entries = filter_enabled(read_master(settings.master_xlsx_path))

    if is_dry_run():
        _log_dry_run(settings, entries)
        return 0

    if not entries:
        logger.error("管理表に『有効』が ○ の行がありません")
        return 2

    # レポートの取得 （ 同じ実行内で ``describe_object`` を 1 回にまとめる ）
    object_cache: dict[str, dict[str, Any]] = {}
    outcomes: list[FetchOutcome] = []
    for outcome in run_fetch(settings, entries, site_for=site_for, object_cache=object_cache):
        _log_outcome(outcome, settings)
        outcomes.append(outcome)

    ok_keys = [o.entry.key for o in outcomes if o.status == "ok"]
    failed_keys = [o.entry.key for o in outcomes if o.status == "failed"]

    # 取れた record だけから CSV を組み立てる。 失敗した ID の CSV は作らず
    # 既存も消さない。
    records = [o.record for o in outcomes if o.record is not None]
    try:
        run_tables(settings.output_dir, records, only_keys=ok_keys)
    except Exception as exc:
        # CSV の失敗は全体の失敗にはしない （ record は取れているので ）
        logger.error("CSV の生成に失敗しました: %s", exc)

    return 1 if failed_keys else 0


# ── ログ ────────────────────────────────────────────────────────────────


def _log_outcome(outcome: FetchOutcome, settings: Settings) -> None:
    """``FetchOutcome`` を ``[ok]`` / ``[failed]`` でログに出す。"""
    if outcome.status == "ok":
        logger.info(
            "[ok] 管理番号=%s 概要=%s 警告=%d 件",
            outcome.entry.key,
            outcome.entry.summary,
            len(outcome.warnings),
        )
        for warning in outcome.warnings:
            logger.warning("  - %s", warning)
    elif outcome.status == "failed":
        logger.error(
            "[failed] 管理番号=%s: %s",
            outcome.entry.key,
            outcome.error,
        )
    elif outcome.status == "dry-run":
        # dry-run は ``_log_dry_run`` 側でまとめて出す
        return


def _log_dry_run(settings: Settings, entries: list[MasterEntry]) -> None:
    """dry-run のログを出す（ 接続も書き込みもしない ）。"""
    for entry in entries:
        try:
            report_id = report_id_from_url(entry.url)
        except Exception:
            report_id = "(URL 不正)"
        logger.info(
            "[dry-run] 管理番号=%s 概要=%s レポートID=%s 出力先=%s",
            entry.key,
            entry.summary,
            report_id,
            settings.output_dir,
        )
