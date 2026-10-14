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
from dataclasses import dataclass
from pathlib import Path

from comken import config
from comken.exceptions import CSVError
from comken.runtime import is_dry_run
from comken.toolbox.salesforce.report import report_id_from_url

from src import column_map_csv, pii
from src.fetch import (
    RELATED_DEPTH,
    RELATED_MAX,
    ROW_LIMIT_ENABLED,
    run_fetch,
)
from src.master import MasterEntry, filter_enabled, read_master
from src.tables import run_tables

logger = logging.getLogger(__name__)

# 似たレポート判定で 「 近い 」 とみなす出力列の Jaccard 類似度の下限。
# 0.0 〜 1.0 の範囲。 値を下げると より多くのレポートが 「 近い 」 になる
SIMILAR_COLUMN_SIMILARITY = 0.8

# ``run_fetch`` の ``site_for`` と同じ型（ テストで差し替えるため ）
SiteFor = Callable[[str], type]


@dataclass(frozen=True)
class FilePaths:
    """``config.ini [FILES]`` から読む 4 つのパス。"""

    master_xlsx_path: Path
    output_dir: Path
    describe_csv_path: Path
    column_map_csv_path: Path


def read_file_paths() -> FilePaths:
    """``config.ini [FILES]`` から 4 つのパスを ``FilePaths`` として読む。

    4 つのキー ``MASTER_XLSX_PATH`` / ``OUTPUT_DIR`` / ``DESCRIBE_CSV_PATH`` /
    ``COLUMN_MAP_CSV_PATH`` は **すべて必須** 。 どれか 1 つでも config.ini に
    無ければ comken が ``ConfigKeyNotFoundError`` を上げる （ メッセージに
    キー名が入る ） 。

    値の変換や既定値の補完は一切しない。 ``FilePaths`` はテストでパスを
    差し替えるための入れ物として用意しているだけで、 ここでの加工はしない。

    旧 config.ini に ``[LIMITS]`` / ``[SF]`` / ``[CHECKS]`` / ``[PII]`` /
    ``[SIMILAR]`` などのセクションが残っていても、 ここでは読まないので
    エラーにならない。
    """
    return FilePaths(
        master_xlsx_path=config.FILES.MASTER_XLSX_PATH,
        output_dir=config.FILES.OUTPUT_DIR,
        describe_csv_path=config.FILES.DESCRIBE_CSV_PATH,
        column_map_csv_path=config.FILES.COLUMN_MAP_CSV_PATH,
    )


def run(
    argv: list[str] | None = None,
    *,
    site_for: SiteFor | None = None,
    file_paths: FilePaths | None = None,
) -> int:
    """``実行.bat`` / RPA から呼ばれる入口。

    Args:
        argv: コマンドライン引数。 ``None`` なら ``sys.argv[1:]`` を使う。
            1 件でも指定されたら何もしないでエラー終了（ 終了コード 2 ）。
        site_for: URL → Salesforce サイトクラスの関数。 テスト用差し替え。
        file_paths: ``config.ini [FILES]`` の値。 省略時は ``read_file_paths()`` で
            読む。 テストで差し替えるために引数で受ける。

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

    if file_paths is None:
        file_paths = read_file_paths()

    all_entries = read_master(file_paths.master_xlsx_path)
    entries = filter_enabled(all_entries)
    enabled_count = len(entries)
    total_count = len(all_entries)

    logger.info(
        "取得開始: 管理表=%s 全行数=%d 有効=%d 出力先=%s"
        " RELATED_DEPTH=%d RELATED_MAX=%d ROW_LIMIT=%s",
        file_paths.master_xlsx_path,
        total_count,
        enabled_count,
        file_paths.output_dir,
        RELATED_DEPTH,
        RELATED_MAX,
        "○" if ROW_LIMIT_ENABLED else "×",
    )

    if is_dry_run():
        _log_dry_run(file_paths, entries)
        return 0

    if not entries:
        logger.error("管理表に『有効』が ○ の行がありません")
        return 2

    # 人が手で見比べる ``列の対応.csv`` を 1 回の実行の最初に読む。
    # 見出し違いなら ``CSVError`` で止める （ 出力物に壊せない位置 ） 。
    # ファイルが無いときは空 dict （ 新規扱い ） 。
    try:
        column_map_overrides = column_map_csv.load_column_map(file_paths.column_map_csv_path)
    except CSVError as exc:
        logger.error("%s", exc)
        return 2

    # レポートの取得 （ 同じ実行内で ``describe_object`` を 1 回にまとめる ）
    started_at = time.perf_counter()
    from src.describe_store import DescribeStore

    describe_store = DescribeStore.load(file_paths.describe_csv_path)
    refresh_set: set[tuple[str, str]] = set()
    outcomes = list(
        run_fetch(
            entries,
            related_depth=RELATED_DEPTH,
            related_max=RELATED_MAX,
            row_limit=ROW_LIMIT_ENABLED,
            pii_keywords=pii.DEFAULT_KEYWORDS,
            pii_person_objects=pii.DEFAULT_PERSON_OBJECTS,
            describe_csv_path=file_paths.describe_csv_path,
            site_for=site_for,
            describe_store=describe_store,
            refresh_set=refresh_set,
            column_map_overrides=column_map_overrides,
        )
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
    logger.info(
        "オブジェクトの describe: 保存データから %d 件 / Salesforce から %d 件 / 取り直し %d 件",
        describe_store.csv_hit_count,
        describe_store.api_count,
        describe_store.refresh_count,
    )

    ok_keys = [o.entry.key for o in outcomes if o.status == "ok"]

    # 取れた record だけから CSV を組み立てる。 失敗した ID の CSV は作らず
    # 既存も消さない。
    records = [o.record for o in outcomes if o.record is not None]
    pii_config = pii.PIIConfig(
        keywords=pii.DEFAULT_KEYWORDS,
        person_objects=pii.DEFAULT_PERSON_OBJECTS,
    )
    try:
        run_tables(
            file_paths.output_dir,
            records,
            only_keys=ok_keys,
            pii_config=pii_config,
            similar_threshold=SIMILAR_COLUMN_SIMILARITY,
        )
    except Exception as exc:
        # CSV の失敗は全体の失敗にはしない （ record は取れているので ）
        logger.error("CSV の生成に失敗しました: %s", exc)

    # ``列の対応.csv`` を最後に 1 回書く （ 人が入れた値を壊さない ）。
    # PermissionError 等は ``merge_and_write`` 内で WARNING だけになり、 処理は続く
    column_map_csv._merge_and_write_after_run(
        path=file_paths.column_map_csv_path,
        existing=column_map_overrides,
        records=records,
        today=column_map_csv.today_local(),
    )

    return 1 if failed_count else 0


# ── ログ ────────────────────────────────────────────────────────────────


def _log_dry_run(file_paths: FilePaths, entries: list[MasterEntry]) -> None:
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
            file_paths.output_dir,
        )
