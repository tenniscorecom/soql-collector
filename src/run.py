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

ログ方針: 実行の開始と終了、 dry-run の一覧はここで 1 回ずつ出す。 1
レポート分の経過ログ（ 取得開始・節目・取得完了/失敗 ） は ``run_fetch`` 側
（ ``src/fetch.py`` ） が持つ。 同じ事実を重ねて出さない。
"""

from __future__ import annotations

import logging
import sys
import time
from collections.abc import Callable
from typing import Any

from comken.runtime import is_dry_run
from comken.toolbox.salesforce.report import report_id_from_url

from src import pii
from src.fetch import run_fetch
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
    all_entries = read_master(settings.master_xlsx_path)
    entries = filter_enabled(all_entries)
    enabled_count = len(entries)
    total_count = len(all_entries)

    logger.info(
        "取得開始: 管理表=%s 全行数=%d 有効=%d 出力先=%s"
        " RELATED_DEPTH=%d RELATED_MAX=%d ROW_LIMIT=%s",
        settings.master_xlsx_path,
        total_count,
        enabled_count,
        settings.output_dir,
        settings.related_depth,
        settings.related_max,
        "○" if settings.row_limit else "×",
    )

    if is_dry_run():
        _log_dry_run(settings, entries)
        return 0

    if not entries:
        logger.error("管理表に『有効』が ○ の行がありません")
        return 2

    # レポートの取得 （ 同じ実行内で ``describe_object`` を 1 回にまとめる ）
    started_at = time.perf_counter()
    object_cache: dict[str, dict[str, Any]] = {}
    outcomes = list(
        run_fetch(settings, entries, site_for=site_for, object_cache=object_cache)
    )
    total_elapsed = time.perf_counter() - started_at
    ok_count = sum(1 for o in outcomes if o.status == "ok")
    failed_count = sum(1 for o in outcomes if o.status == "failed")
    logger.info(
        "取得終了: 成功=%d 失敗=%d 所要秒=%.1f",
        ok_count,
        failed_count,
        total_elapsed,
    )

    ok_keys = [o.entry.key for o in outcomes if o.status == "ok"]

    # 取れた record だけから CSV を組み立てる。 失敗した ID の CSV は作らず
    # 既存も消さない。
    records = [o.record for o in outcomes if o.record is not None]
    pii_config = pii.PIIConfig(
        keywords=settings.pii_keywords,
        person_objects=settings.pii_person_objects,
    )
    try:
        run_tables(
            settings.output_dir,
            records,
            only_keys=ok_keys,
            pii_config=pii_config,
            similar_threshold=settings.similar_column_similarity,
        )
    except Exception as exc:
        # CSV の失敗は全体の失敗にはしない （ record は取れているので ）
        logger.error("CSV の生成に失敗しました: %s", exc)

    return 1 if failed_count else 0


# ── ログ ────────────────────────────────────────────────────────────────


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
