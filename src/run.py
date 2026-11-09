"""src/run.py — main.py から呼ばれる入口。

レポートの describe とオブジェクトの describe を加工せず原本のまま保存し、 出力は保存データ
だけから作る。 取得と解析を分けることで、 同じ情報を何度も Salesforce から取らずに済み、
解析で新しい項目が要るようになっても取り直さずに済む。

入口（引数）:

- なし（``実行.bat``）… 取得の段のあと解析の段を通す。 取得で失敗したレポートがあっても、
  解析は保存にある分で進む
- ``--fetch``（``取得.bat``）… 取得の段だけ。 保存に無いものだけ取る。 出力は作らない
- ``--fetch --force``（``全部取り直し.bat``）… 取得の段だけ。 全部取り直して上書きする
- ``--analyze``（``解析.bat``）… 解析の段だけ。 Salesforce に接続しない

終了コード:

- 0 … 全件成功
- 1 … 1 件以上のレポートが失敗した / 出力の生成に失敗した
- 2 … 引数が違う / 管理表に調べる行が無い / config.ini に必須キーが無い /
  入力ファイルの見出しが違う

取得の段:

- 解析と同じ経路（``run_fetch``）を通すことで、 必要なオブジェクトを漏れなく保存する
- API の失敗は保存しない（次の取得でやり直す）。 同じ実行の中では 1 回だけ試す
- 今回調べないレポートの保存を消すのは取得の段だけ

解析の段:

- 接続しない。 ``site_for`` は ``DOMAIN_URL`` を読むだけで、 サイトクラスを作らない
- レポートの原本が無いレポートは失敗にする。 オブジェクトの原本が無いのは警告にして進める
- 保存データは一切書き換えない

`main.py` はこの ``run()`` の戻り値を ``SystemExit`` に流す。

ログ方針: 実行の開始と終了、 dry-run の一覧はここで 1 回ずつ出す。 1
レポート分の経過ログ（取得開始・節目・取得完了/失敗） は ``run_fetch`` 側
（``src/fetch/``） が持つ。 同じ事実を重ねて出さない。

設定は comken の ``Config`` から直接読む（``from comken import config``
で ``config.FILES.MASTER_XLSX_PATH`` のように）。 comken が自動で
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
from comken.exceptions import (
    ConfigError,
    CSVError,
    ExcelError,
    SalesforceReportIDNotFoundError,
)
from comken.runtime import is_dry_run
from comken.toolbox.salesforce.report import report_id_from_url

from src import child_relationship_csv, column_fix_xlsx
from src.child_relationship_csv import ManualChildRelationship
from src.column_fix_xlsx import ColumnFixRow
from src.describe_store import DescribeStore
from src.fetch import (
    FetchOutcome,
    ReportRecord,
    SiteFor,
    default_site_for,
    offline_site_for,
    run_fetch,
)
from src.master import MasterEntry, filter_survey_targets, read_master
from src.report_store import ReportStore
from src.tables import run_tables

logger = logging.getLogger(__name__)

# 今回調べないレポート（管理表から消えた・「有効」が ×・「調査」が ○ でない）の保存を、
# 取得の段の最後に消す。 消すのは ``元データ/レポート/`` と ``元データ/件数.csv`` の中だけ
# （出力ファイルとオブジェクトの原本には触らない）。 消したものは、 次に調べるときに取り直しになる。
PRUNE_REPORT_STORE: bool = True


FETCH_OPTION = "--fetch"
FORCE_OPTION = "--force"
ANALYZE_OPTION = "--analyze"


@dataclass(frozen=True)
class RunMode:
    """どの段を通すか。"""

    does_fetch: bool
    does_analyze: bool
    is_force: bool = False


# 受け付ける引数の組み合わせ
_RUN_MODES: dict[tuple[str, ...], RunMode] = {
    (): RunMode(does_fetch=True, does_analyze=True),
    (FETCH_OPTION,): RunMode(does_fetch=True, does_analyze=False),
    (FETCH_OPTION, FORCE_OPTION): RunMode(does_fetch=True, does_analyze=False, is_force=True),
    (ANALYZE_OPTION,): RunMode(does_fetch=False, does_analyze=True),
}


@dataclass(frozen=True)
class FilePaths:
    """``config.ini [FILES]`` から読むパス。 ``describe_csv_path`` の親フォルダの下に
    ``元データ/`` を置く（キーの名前は既存の config.ini を壊さないためそのまま）。
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


def _load_manual_child_relationships(
    file_paths: FilePaths,
    *,
    creates_file: bool,
) -> tuple[ManualChildRelationship, ...]:
    """``input/子リレーション.csv`` を読んで、 手入力の子リレーションを返す。

    ``creates_file`` なら、 ファイルが無いときに見出しだけのファイルを作る
    （取得の段だけ。 解析の段はファイルを作らない）。 見出しが違うときは
    ``CSVError`` を上げる（停止させる）。
    """
    path = child_relationship_csv.child_relationship_csv_path(file_paths.column_fix_xlsx_path)
    if creates_file:
        child_relationship_csv.ensure_child_relationship_file(path)
    return child_relationship_csv.load_manual_child_relationships(path)


def _run_stage(
    entries: list[MasterEntry],
    *,
    column_fix_overrides: dict[tuple[str, str], ColumnFixRow],
    manual_child_relationships: tuple[ManualChildRelationship, ...],
    site_for: SiteFor | None,
    describe_store: DescribeStore,
    report_store: ReportStore,
    row_limit: bool,
) -> list[FetchOutcome]:
    """``run_fetch`` を呼び ``outcomes`` を返す。 取得の段と解析の段で同じ経路を通すための入口。"""
    started_at = time.perf_counter()
    outcomes = run_fetch(
        entries,
        related_depth=config.SURVEY.RELATED_DEPTH,
        related_max=config.SURVEY.RELATED_MAX,
        row_limit=row_limit,
        pii_keywords=tuple(s.strip() for s in config.PII.KEYWORDS.split(",")),
        pii_person_objects=tuple(s.strip() for s in config.PII.PERSON_OBJECTS.split(",")),
        site_for=site_for,
        describe_store=describe_store,
        report_store=report_store,
        column_fix_overrides=column_fix_overrides,
        manual_child_relationships=manual_child_relationships,
    )
    logger.info(
        "処理終了: 成功=%d 失敗=%d 所要秒=%.1f",
        sum(1 for o in outcomes if o.status == "ok"),
        sum(1 for o in outcomes if o.status == "failed"),
        time.perf_counter() - started_at,
    )
    return outcomes


def _run_fetch_stage(
    file_paths: FilePaths,
    entries: list[MasterEntry],
    *,
    column_fix_overrides: dict[tuple[str, str], ColumnFixRow],
    manual_child_relationships: tuple[ManualChildRelationship, ...],
    site_for: SiteFor | None,
    is_force: bool,
) -> bool:
    """取得の段を通し、 失敗があれば ``True`` を返す。 出力は作らない。"""
    logger.info("取得の段を始めます%s", "（全部取り直し）" if is_force else "")
    describe_store = DescribeStore.load(file_paths.describe_csv_path, force=is_force)
    report_store = ReportStore.load(file_paths.describe_csv_path, force=is_force)
    outcomes = _run_stage(
        entries,
        column_fix_overrides=column_fix_overrides,
        manual_child_relationships=manual_child_relationships,
        site_for=site_for,
        describe_store=describe_store,
        report_store=report_store,
        row_limit=config.SURVEY.ROW_LIMIT_CHECK == "○",
    )
    logger.info(
        "API 呼び出し: レポート describe=%d 回 / 件数=%d 回 / オブジェクト describe=%d 回"
        "（ほか失敗 %d 回）",
        report_store.api_count,
        report_store.row_count_api_count,
        describe_store.api_count,
        describe_store.failure_count,
    )
    logger.info(
        "保存データから: レポート=%d 件 / オブジェクト=%d 件",
        report_store.hit_count,
        describe_store.saved_hit_count,
    )
    if PRUNE_REPORT_STORE:
        _prune_report_store(report_store, entries)
    has_failed_report = any(o.status == "failed" for o in outcomes)
    return has_failed_report or report_store.failure_count > 0


def _prune_report_store(report_store: ReportStore, entries: list[MasterEntry]) -> None:
    """今回調べないレポートの保存を消す（出力ファイルとオブジェクトの原本には触らない）。"""
    keep_ids: set[str] = set()
    for entry in entries:
        try:
            keep_ids.add(report_id_from_url(entry.url))
        except SalesforceReportIDNotFoundError:
            continue  # URL が壊れた行は、 取得のときに個別の失敗として出ている
    removed = report_store.prune(keep_ids)
    if removed:
        logger.info(
            "今回調べないレポートの保存を消しました: %d 件（%s）",
            len(removed),
            ", ".join(removed),
        )


def _run_analyze_stage(
    file_paths: FilePaths,
    entries: list[MasterEntry],
    *,
    column_fix_overrides: dict[tuple[str, str], ColumnFixRow],
    manual_child_relationships: tuple[ManualChildRelationship, ...],
    site_for: SiteFor | None,
) -> bool:
    """解析の段を通して出力を作り、 失敗があれば ``True`` を返す。 接続しない。"""
    logger.info("解析の段を始めます（Salesforce に接続しません）")
    describe_store = DescribeStore.load(file_paths.describe_csv_path)
    report_store = ReportStore.load(file_paths.describe_csv_path)
    outcomes = _run_stage(
        entries,
        column_fix_overrides=column_fix_overrides,
        manual_child_relationships=manual_child_relationships,
        site_for=offline_site_for(site_for or default_site_for),
        describe_store=describe_store,
        report_store=report_store,
        row_limit=False,
    )
    # 取れた record だけから出力を組み立てる。 失敗した ID のファイルは作らず既存も消さない
    output_failed = _write_output(
        file_paths=file_paths,
        records=[o.record for o in outcomes if o.record is not None],
        ok_keys=[o.entry.key for o in outcomes if o.status == "ok"],
        column_fix_overrides=column_fix_overrides,
        describe_store=describe_store,
    )
    return output_failed or any(o.status == "failed" for o in outcomes)


def _write_output(
    file_paths: FilePaths,
    records: list[ReportRecord],
    ok_keys: list[str],
    column_fix_overrides: dict[tuple[str, str], ColumnFixRow],
    describe_store: DescribeStore,
) -> bool:
    """``run_tables`` と ``column_fix_xlsx.merge_and_write_after_run`` を順に呼ぶ。

    ``run_tables`` が例外を出したら ``True`` を返す （``run()`` の終了コードが
    1 になる判定材料）。 ``column_fix_xlsx.merge_and_write_after_run`` 内の
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
            markdown_dir=file_paths.markdown_dir,
            diagram_dir=file_paths.diagram_dir,
            describe_store=describe_store,
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
            受け付ける形はモジュールの説明のとおり。 ほかは何もしないで終了コード 2。
        site_for: URL → Salesforce サイトクラスの関数。 テスト用差し替え。
            解析の段では クラス属性 ``DOMAIN_URL`` しか読まない（ログインしない）。
        file_paths: ``config.ini [FILES]`` の値。 省略時は ``read_file_paths()`` で
            読む。 テストで差し替えるために引数で受ける。

    Returns:
        0 … 全件成功
        1 … 失敗した ID が 1 件以上ある / 出力の生成に失敗した
        2 … 引数が違う / 管理表に調べる行が無い / config.ini が不正 / 入力の見出しが違う
    """
    if argv is None:
        argv = sys.argv[1:]
    mode = _RUN_MODES.get(tuple(argv))
    if mode is None:
        logger.error(
            "引数が違います: %s。取るレポートは管理表の『有効』列が ○ の行で決まります"
            "（指定できるのは %s、%s %s、%s のどれかです）",
            " ".join(argv),
            FETCH_OPTION,
            FETCH_OPTION,
            FORCE_OPTION,
            ANALYZE_OPTION,
        )
        return 2

    # config.ini のキー欠落 （``ConfigError``） と入力の見出し違いは全て return 2 にまとめる
    try:
        if file_paths is None:
            file_paths = read_file_paths()

        all_entries = read_master(file_paths.master_xlsx_path)
        entries = filter_survey_targets(all_entries)

        logger.info(
            "開始: 管理表=%s 全行数=%d 有効=%d 調査対象=%d 出力先=%s "
            "RELATED_DEPTH=%d RELATED_MAX=%d ROW_LIMIT=%s SIMILAR=%.2f",
            file_paths.master_xlsx_path,
            len(all_entries),
            sum(1 for entry in all_entries if entry.enabled),
            len(entries),
            file_paths.output_dir,
            config.SURVEY.RELATED_DEPTH,
            config.SURVEY.RELATED_MAX,
            config.SURVEY.ROW_LIMIT_CHECK,
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

        column_fix_overrides = _load_column_fix_overrides(file_paths)
        manual_child_relationships = _load_manual_child_relationships(
            file_paths, creates_file=mode.does_fetch
        )

        has_failed = False
        if mode.does_fetch:
            has_failed = _run_fetch_stage(
                file_paths,
                entries,
                column_fix_overrides=column_fix_overrides,
                manual_child_relationships=manual_child_relationships,
                site_for=site_for,
                is_force=mode.is_force,
            )
        if mode.does_analyze:
            has_failed = (
                _run_analyze_stage(
                    file_paths,
                    entries,
                    column_fix_overrides=column_fix_overrides,
                    manual_child_relationships=manual_child_relationships,
                    site_for=site_for,
                )
                or has_failed
            )
        return 1 if has_failed else 0
    except (ConfigError, ExcelError, CSVError) as exc:
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
