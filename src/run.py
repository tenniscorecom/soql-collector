"""src/run.py — main.py から呼ばれる入口。

``実行.bat`` または ``python main.py`` から呼ばれると、 ``argv`` を
``sys.argv[1:]`` として受け取り、 管理表の「有効」列が ``○`` の行すべての
describe を取って ``ReportRecord`` に詰める。 その record の一覧を
``run_tables`` に渡して CSV を組み立てる。 JSON は書かない。

終了コード:

- 0 … 全件成功
- 1 … 1 件以上のレポートが失敗した / 出力の生成に失敗した
- 2 … 引数が付いた / 管理表に「有効」が ○ の行が無い / config.ini に
  必須キーが無い （comken の ``ConfigError`` がそのまま上がる）

`main.py` はこの ``run()`` の戻り値を ``SystemExit`` に流す。

ログ方針: 実行の開始と終了、 dry-run の一覧はここで 1 回ずつ出す。 1
レポート分の経過ログ（取得開始・節目・取得完了/失敗） は ``run_fetch`` 側
（``src/fetch.py``） が持つ。 同じ事実を重ねて出さない。

設定は comken の ``Config`` から直接読む（ ``from comken import config``
で ``config.FILES.MASTER_XLSX_PATH`` のように ）。 comken が自動で
やってくれない変換は 2 行だけ:

- ``[SURVEY] ROW_LIMIT_CHECK`` が ○/× で書かれているので ``== "○"`` で bool にする
- ``[PII] KEYWORDS`` / ``PERSON_OBJECTS`` が ``[ ]`` なしの素のカンマ区切りで
  書かれているので ``split(",")`` で tuple にする

範囲チェック・既定値の補完はここには持たせない（設定の規約にも反するため）。
キーが無いときは comken の ``ConfigError`` をそのまま使う側へ流す。
"""

from __future__ import annotations

import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from comken import config
from comken.exceptions import ConfigError, ExcelError, SalesforceReportIDNotFoundError
from comken.runtime import is_dry_run
from comken.toolbox.salesforce.report import report_id_from_url

from src import column_fix_xlsx
from src.column_fix_xlsx import ColumnFixRow
from src.describe_store import DescribeStore
from src.fetch import FetchOutcome, ReportRecord, SiteFor, run_fetch
from src.master import MasterEntry, filter_survey_targets, read_master
from src.report_store import ReportStore
from src.tables import run_tables

logger = logging.getLogger(__name__)

# 今回調べないレポート（管理表から消えた・「有効」が ×・「調査」が ○ でない）の、
# 貯めたレポート定義を実行の最後に消す。 消すのは ``data/レポート定義/`` の中だけ
# （``SOQL調査.xlsx`` / ``SOQL資料/`` / ``列名の対応.csv`` / ``項目定義.csv`` には触らない）。
# 消したものは、 次に調べるときに取り直しになる。
PRUNE_REPORT_STORE: bool = True


@dataclass(frozen=True)
class FilePaths:
    """``config.ini [FILES]`` から読むパス。 旧 ``COLUMN_MAP_CSV_PATH`` は
    互換のため受け取るが、 中身は ``column_fix_xlsx_path`` として保持する。
    """

    master_xlsx_path: Path
    output_dir: Path
    describe_csv_path: Path
    column_fix_xlsx_path: Path
    xlsx_name: str
    markdown_dir: str
    diagram_dir: str


def read_file_paths() -> FilePaths:
    """``config.ini [FILES]`` から ``FilePaths`` を読む。"""
    return FilePaths(
        master_xlsx_path=Path(config.FILES.MASTER_XLSX_PATH),
        output_dir=Path(config.FILES.OUTPUT_DIR),
        describe_csv_path=Path(config.FILES.DESCRIBE_CSV_PATH),
        column_fix_xlsx_path=Path(config.FILES.COLUMN_FIX_XLSX_PATH),
        xlsx_name=config.FILES.XLSX_NAME,
        markdown_dir=config.FILES.MARKDOWN_DIR,
        diagram_dir=config.FILES.DIAGRAM_DIR,
    )


def _load_column_fix_overrides(
    file_paths: FilePaths,
) -> dict[tuple[str, str], ColumnFixRow]:
    """``列の直し.xlsx`` を読み、 旧 ``data/列の対応.csv`` からの移行もまとめて
    ``column_fix_overrides`` を返す。 見出し違いは ``ExcelError`` をそのまま上げる。
    """
    column_fix_path = file_paths.column_fix_xlsx_path
    overrides = column_fix_xlsx.load_column_fix(column_fix_path)
    legacy_csv_path = file_paths.describe_csv_path.parent / "列の対応.csv"
    if not column_fix_path.exists() and legacy_csv_path.exists():
        legacy_rows = column_fix_xlsx.migrate_legacy_csv(legacy_csv_path)
        if legacy_rows:
            overrides = {**overrides, **legacy_rows}
            logger.info(
                "旧 data/列の対応.csv から %d 件の人の入力を引き継ぎました",
                len(legacy_rows),
            )
    return overrides


def _fetch(
    file_paths: FilePaths,
    entries: list[MasterEntry],
    column_fix_overrides: dict[tuple[str, str], ColumnFixRow],
    site_for: SiteFor | None,
) -> list[FetchOutcome]:
    """``run_fetch`` を呼び ``outcomes`` を返す。 所要時間と describe の集計ログもここで出す。"""
    started_at = time.perf_counter()
    describe_store = DescribeStore.load(file_paths.describe_csv_path)
    report_store = ReportStore.load(file_paths.describe_csv_path)
    refresh_set: set[tuple[str, str]] = set()
    outcomes = list(
        run_fetch(
            entries,
            related_depth=config.SURVEY.RELATED_DEPTH,
            related_max=config.SURVEY.RELATED_MAX,
            row_limit=config.SURVEY.ROW_LIMIT_CHECK == "○",
            pii_keywords=tuple(s.strip() for s in config.PII.KEYWORDS.split(",")),
            pii_person_objects=tuple(s.strip() for s in config.PII.PERSON_OBJECTS.split(",")),
            describe_csv_path=file_paths.describe_csv_path,
            site_for=site_for,
            describe_store=describe_store,
            report_store=report_store,
            refresh_set=refresh_set,
            column_fix_overrides=column_fix_overrides,
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
    logger.info(
        "レポート定義の describe: 保存データから %d 件 / Salesforce から %d 件",
        report_store.hit_count,
        report_store.api_count,
    )
    if PRUNE_REPORT_STORE:
        _prune_report_store(report_store, entries)
    return outcomes


def _prune_report_store(report_store: ReportStore, entries: list[MasterEntry]) -> None:
    """今回調べないレポートの、 貯めたレポート定義を消す（出力ファイルや項目定義には触らない）。"""
    keep_ids: set[str] = set()
    for entry in entries:
        try:
            keep_ids.add(report_id_from_url(entry.url))
        except SalesforceReportIDNotFoundError:
            continue  # URL が壊れた行は、 取得のときに個別の失敗として出ている
    removed = report_store.prune(keep_ids)
    if removed:
        logger.info(
            "今回調べないレポートの、 貯めたレポート定義を消しました: %d 件（%s）",
            len(removed),
            ", ".join(removed),
        )


def _write_output(
    file_paths: FilePaths,
    records: list[ReportRecord],
    ok_keys: list[str],
    column_fix_overrides: dict[tuple[str, str], ColumnFixRow],
) -> bool:
    """``run_tables`` と ``column_fix_xlsx.merge_and_write_after_run`` を順に呼ぶ。

    ``run_tables`` が例外を出したら ``True`` を返す （ ``run()`` の終了コードが
    1 になる判定材料 ）。 ``column_fix_xlsx.merge_and_write_after_run`` 内の
    ``PermissionError`` は WARNING に縮退する。
    """
    output_failed = False
    try:
        run_tables(
            file_paths.output_dir,
            records,
            only_keys=ok_keys,
            similar_threshold=config.SURVEY.SIMILAR_COLUMN_THRESHOLD,
            xlsx_name=file_paths.xlsx_name,
        )
    except Exception as exc:
        # xlsx / CSV のどちらも作れていないので、 終了コードは 1 にする
        output_failed = True
        logger.exception("CSV の生成に失敗しました: %s", exc)

    # ``列の直し.xlsx`` を最後に 1 回書く （人が入れた値を壊さない）。
    # PermissionError 等は ``merge_and_write_after_run`` 内で WARNING だけになり、 処理は続く
    column_fix_xlsx.merge_and_write_after_run(
        path=file_paths.column_fix_xlsx_path,
        existing=column_fix_overrides,
        records=records,
        today=column_fix_xlsx.today_local(),
    )

    return output_failed


def run(
    argv: list[str] | None = None,
    *,
    site_for: SiteFor | None = None,
    file_paths: FilePaths | None = None,
) -> int:
    """``実行.bat`` / RPA から呼ばれる入口。

    Args:
        argv: コマンドライン引数。 ``None`` なら ``sys.argv[1:]`` を使う。
            1 件でも指定されたら何もしないでエラー終了（終了コード 2）。
        site_for: URL → Salesforce サイトクラスの関数。 テスト用差し替え。
        file_paths: ``config.ini [FILES]`` の値。 省略時は ``read_file_paths()`` で
            読む。 テストで差し替えるために引数で受ける。

    Returns:
        0 … 全件成功
        1 … 失敗した ID が 1 件以上ある / 出力の生成に失敗した
        2 … 引数が付いた / 管理表に「有効」が ○ の行が無い / config.ini が不正
            （comken の ``ConfigError`` を 2 にして返す）
    """
    if argv is None:
        argv = sys.argv[1:]
    if argv:
        logger.error("引数は使いません。取るレポートは管理表の『有効』列が ○ の行で決まります")
        return 2

    # config.ini のキー欠落 （``ConfigError``） は全て return 2 にまとめる
    try:
        if file_paths is None:
            file_paths = read_file_paths()

        all_entries = read_master(file_paths.master_xlsx_path)
        entries = filter_survey_targets(all_entries)
        enabled_count = sum(1 for entry in all_entries if entry.enabled)
        target_count = len(entries)
        total_count = len(all_entries)

        # 設定は ``config.SURVEY.*`` / ``config.PII.*`` を直接読む （
        # キー欠落ならこの下で ``ConfigError`` ）
        logger.info(
            "取得開始: 管理表=%s 全行数=%d 有効=%d 調査対象=%d 出力先=%s "
            "RELATED_DEPTH=%d RELATED_MAX=%d ROW_LIMIT=%s SIMILAR=%.2f",
            file_paths.master_xlsx_path,
            total_count,
            enabled_count,
            target_count,
            file_paths.output_dir,
            config.SURVEY.RELATED_DEPTH,
            config.SURVEY.RELATED_MAX,
            "○" if config.SURVEY.ROW_LIMIT_CHECK == "○" else "×",
            config.SURVEY.SIMILAR_COLUMN_THRESHOLD,
        )

        if is_dry_run():
            _log_dry_run(file_paths, entries)
            return 0

        if not entries:
            logger.error(
                "管理表に調べる行がありません（『有効』が ○ の行。 『調査』列があれば『調査』も ○）"
            )
            return 2

        # 列の直しを読む（見出し違いなら ExcelError で止める）
        try:
            column_fix_overrides = _load_column_fix_overrides(file_paths)
        except ExcelError as exc:
            logger.error("%s", exc)
            return 2

        # 取得する
        outcomes = _fetch(
            file_paths=file_paths,
            entries=entries,
            column_fix_overrides=column_fix_overrides,
            site_for=site_for,
        )
        failed_count = sum(1 for o in outcomes if o.status == "failed")

        ok_keys = [o.entry.key for o in outcomes if o.status == "ok"]

        # 取れた record だけから CSV を組み立てる。 失敗した ID の CSV は作らず
        # 既存も消さない。
        records = [o.record for o in outcomes if o.record is not None]

        # 出力する
        output_failed = _write_output(
            file_paths=file_paths,
            records=records,
            ok_keys=ok_keys,
            column_fix_overrides=column_fix_overrides,
        )

        return 1 if (failed_count or output_failed) else 0
    except ConfigError as exc:
        logger.error("%s", exc)
        return 2


# ── ログ ────────────────────────────────────────────────────────────────


def _log_dry_run(file_paths: FilePaths, entries: list[MasterEntry]) -> None:
    """dry-run のログを出す（接続も書き込みもしない）。"""
    for entry in entries:
        try:
            report_id = report_id_from_url(entry.url)
        except SalesforceReportIDNotFoundError:
            report_id = "(URL 不正)"
        logger.info(
            "[dry-run] 管理番号=%s 概要=%s レポートID=%s 出力先=%s",
            entry.key,
            entry.summary,
            report_id,
            file_paths.output_dir,
        )
