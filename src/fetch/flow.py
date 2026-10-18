"""ID ごとの取得の全体の流れ（``run_fetch`` と、 1 ID 分の ``fetch_one``）。"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from comken.exceptions import (
    ComkenError,
    SalesforceError,
    SalesforceReportIDNotFoundError,
    SalesforceRequestError,
)
from comken.toolbox.salesforce.report import report_id_from_url

from src import pii
from src.describe_store import DescribeStore, org_from_url
from src.fetch.models import FetchOutcome, SiteFor
from src.fetch.outcome import build_fetch_outcome
from src.fetch.site_run import FetchState, run_within_site
from src.master import MasterEntry
from src.report_store import ReportStore

logger = logging.getLogger(__name__)


# 関連オブジェクトの合計件数の上限。 全段の合計が ``RELATED_MAX`` に達したら
# 打ち切り、 超えた名前は ``warnings`` に残す。 1 = 関連オブジェクトを取らない
RELATED_MAX: int = 40


# 関連オブジェクトを掘る最大の段数。 1 = 主オブジェクトの参照先 1 段だけ。
# 2 以上にすると ``Task.What.Account.Owner`` のように参照先をたどって掘る
RELATED_DEPTH: int = 3


# 1 レポートごとに 2000 行チェックを実行するか。 True = 実行する
# （一覧シートの 「2000件超」 列が埋まる）。 False = 実行しない
# （同列が 「未実施」 になる）
ROW_LIMIT_ENABLED: bool = True


# 調査（列の対応・SOQL の下書き）はサンドボックスで行う。 管理表は本番の URL のまま
# 1 レポート 1 行で、 接続先だけをサンドボックスの組織に差し替える（レポート ID は URL から
# そのまま使うので、 サンドボックスに同じ ID のレポートがあることが前提）。
# 本番へつなぎたいときだけ ``False`` にする。
SURVEY_ON_SANDBOX: bool = True


def default_site_for(url: str) -> Any:
    """既定の接続先判定（comken の ``site_for``）。 ``SURVEY_ON_SANDBOX`` ならサンドボックス側。"""
    from comken.toolbox.salesforce.sites import site_for

    return site_for(url, sandbox=SURVEY_ON_SANDBOX)


def run_fetch(
    entries: list[MasterEntry],
    *,
    related_depth: int = RELATED_DEPTH,
    related_max: int = RELATED_MAX,
    row_limit: bool = ROW_LIMIT_ENABLED,
    pii_keywords: tuple[str, ...] = pii.DEFAULT_KEYWORDS,
    pii_person_objects: tuple[str, ...] = pii.DEFAULT_PERSON_OBJECTS,
    describe_csv_path: Path,
    dry_run: bool = False,
    site_for: SiteFor | None = None,
    describe_store: DescribeStore | None = None,
    report_store: ReportStore | None = None,
    refresh_set: set[tuple[str, str]] | None = None,
    column_fix_overrides: dict[tuple[str, str], Any] | None = None,
) -> list[FetchOutcome]:
    """``entries`` の各エントリについて、 describe を取り ``ReportRecord`` を返す。

    **JSON を書かない**。 結果は ``outcomes[i].record`` で取り出して、
    そのまま ``run_tables`` に渡す。

    Args:
        entries: 処理対象の管理表エントリ（既に ``--all`` 絞り込み済み）。
        related_depth: 関連オブジェクトを掘る段数。 既定は ``RELATED_DEPTH``。
        related_max: 関連オブジェクトの合計件数上限。 既定は ``RELATED_MAX``。
        row_limit: True のとき 1 レポートごとに 2000 行チェックを実行する。
            既定は ``ROW_LIMIT_ENABLED``。
        pii_keywords: PII 判定に使うキーワード。 既定は ``pii.DEFAULT_KEYWORDS``。
        pii_person_objects: 個人系オブジェクト。 既定は
            ``pii.DEFAULT_PERSON_OBJECTS``。
        describe_csv_path: ``describe_store`` が ``None`` のときに空ストアを
            作るパス。 ``run.py`` が ``[FILES] DESCRIBE_CSV_PATH`` から渡す。
        dry_run: True なら接続せず、 対象だけ表示用 ``FetchOutcome`` を返す。
        site_for: URL → Salesforce サイトクラスの関数。テスト用差し替え。
        describe_store: メモリ＋CSV の describe キャッシュ。 ``None`` のときは
            ``describe_csv_path`` から空ストアを作る。 同じ実行内で
            ``describe_object`` を 1 回にまとめるための入口。
        refresh_set: 取り直し （一度 API で取り直して再試行） を実行済みの
            ``(org, object_lower)`` の集合。 ``None`` のときは内部で作る。
        column_fix_overrides: 人が ``列の直し.xlsx`` に入れた上書き。
            ``(主オブジェクト小文字, 列キー)`` → ``ColumnMapRow`` の dict。
            ``_fetch_one`` に渡して ``column_map`` の ``対応フィールドAPI名``
            等を差し替える。 空 dict なら何もしない

    Returns:
        入力と同じ順の ``FetchOutcome`` リスト。失敗 ID を含む個別では continue する。
    """
    if site_for is None:
        site_for = default_site_for

    if describe_store is None:
        describe_store = DescribeStore.empty(describe_csv_path)
    if report_store is None:
        report_store = ReportStore.empty()
    if refresh_set is None:
        refresh_set = set()
    if column_fix_overrides is None:
        column_fix_overrides = {}

    outcomes: list[FetchOutcome] = []
    total = len(entries)
    for index, entry in enumerate(entries, start=1):
        if dry_run:
            # dry-run のログは呼び出し側（``run.py`` / ``trial/``） が
            # ``[dry-run]`` の形でまとめて出す。 ここでは何もしない
            outcomes.append(
                FetchOutcome(
                    entry=entry,
                    status="dry-run",
                    record=None,
                    warnings=(),
                    error=None,
                )
            )
            continue
        # ログ用にレポート ID を取り出しておく。 URL が壊れているときは
        # ``_fetch_one`` 側で個別失敗に縮退するので、 ここでは "(URL不正)"
        # と書いて続行する
        report_id_for_log = _report_id_for_log(entry)
        logger.info(
            "[%d/%d] 取得開始: 管理番号=%s 概要=%s レポートID=%s",
            index,
            total,
            entry.key,
            entry.summary,
            report_id_for_log,
        )
        started_at = time.perf_counter()
        outcome = _fetch_one(
            entry,
            related_depth=related_depth,
            related_max=related_max,
            row_limit=row_limit,
            pii_keywords=pii_keywords,
            pii_person_objects=pii_person_objects,
            site_for=site_for,
            store=describe_store,
            report_store=report_store,
            refresh_set=refresh_set,
            column_fix_overrides=column_fix_overrides,
        )
        elapsed = time.perf_counter() - started_at
        if outcome.status == "ok":
            logger.info(
                "[%d/%d] 取得完了: 管理番号=%s 警告=%d 件 所要秒=%.1f",
                index,
                total,
                outcome.entry.key,
                len(outcome.warnings),
                elapsed,
            )
        elif outcome.status == "failed":
            logger.error(
                "[%d/%d] 取得失敗: 管理番号=%s 理由=%s 所要秒=%.1f",
                index,
                total,
                outcome.entry.key,
                outcome.error,
                elapsed,
            )
        for warning in outcome.warnings:
            logger.warning("  - %s", warning)
        outcomes.append(outcome)
    return outcomes


def _fetch_one(
    entry: MasterEntry,
    *,
    related_depth: int,
    related_max: int,
    row_limit: bool,
    pii_keywords: tuple[str, ...],
    pii_person_objects: tuple[str, ...],
    site_for: SiteFor,
    store: DescribeStore,
    refresh_set: set[tuple[str, str]],
    report_store: ReportStore | None = None,
    column_fix_overrides: dict[tuple[str, str], Any] | None = None,
) -> FetchOutcome:
    """1 ID の describe を取り、 ``ReportRecord`` を組み立てて返す。

    JSON は書かない。 失敗したら ``status="failed"`` を返し ``record`` は ``None`` 。
    """
    if column_fix_overrides is None:
        column_fix_overrides = {}
    report_id, site_error = _resolve_report_id_and_site(entry, site_for)
    if site_error is not None:
        return site_error
    assert report_id is not None  # 上の分岐で必ず代入される
    org = _org_of(entry, site_for)

    state = FetchState()
    try:
        with site_for(entry.url)() as client:
            run_within_site(
                client=client,
                entry=entry,
                org=org,
                report_id=report_id,
                store=store,
                report_store=report_store or ReportStore.empty(),
                row_limit=row_limit,
                refresh_set=refresh_set,
                related_depth=related_depth,
                related_max=related_max,
                column_fix_overrides=column_fix_overrides,
                state=state,
            )
    except SalesforceRequestError as exc:
        # 接続失敗・認証失敗・致命 HTTP
        return FetchOutcome(
            entry=entry,
            status="failed",
            record=None,
            warnings=(),
            error=(f"Salesforce への接続でエラー（HTTP {exc.status_code}）: {exc.detail}"),
        )
    except SalesforceError as exc:
        return FetchOutcome(
            entry=entry,
            status="failed",
            record=None,
            warnings=(),
            error=f"Salesforce エラー: {exc}",
        )

    return build_fetch_outcome(
        entry=entry,
        report_id=report_id,
        state=state,
        pii_keywords=pii_keywords,
        pii_person_objects=pii_person_objects,
    )


def _org_of(entry: MasterEntry, site_for: SiteFor) -> str:
    """実際につなぐ組織の識別（describe の保存のキー）を返す。

    サンドボックスに差し替えているときは、 管理表の URL（本番）ではなく、 つなぐ組織クラスの
    ``DOMAIN_URL`` で決める。 本番とサンドボックスの describe が、 同じ組織のものとして
    混ざらないようにするため。 ``DOMAIN_URL`` を持たない接続先（テストの代用品）は URL で決める。
    """
    domain_url = getattr(site_for(entry.url), "DOMAIN_URL", "")
    return org_from_url(domain_url or entry.url)


def _resolve_report_id_and_site(
    entry: MasterEntry,
    site_for: SiteFor,
) -> tuple[str | None, FetchOutcome | None]:
    """``entry.url`` からレポート ID を取る。 ``site_class`` は呼び出し側で別途取得。

    失敗時は ``FetchOutcome(status="failed")`` を返して呼び出し側で停止。
    """
    try:
        report_id = report_id_from_url(entry.url)
    except SalesforceReportIDNotFoundError:
        return None, FetchOutcome(
            entry=entry,
            status="failed",
            record=None,
            warnings=(),
            error=(
                f"管理表の URL からレポート ID を取り出せません: {entry.url}"
                f"（管理番号 {entry.key}）"
            ),
        )
    try:
        # ``site_for`` が ``ComkenError`` を出すと ``_fetch_one`` 側で個別失敗にする
        # ( 接続先を確定させるだけ。 実際の ``with site_class()`` は呼び出し側で行う )
        site_for(entry.url)
    except ComkenError as exc:
        return None, FetchOutcome(
            entry=entry,
            status="failed",
            record=None,
            warnings=(),
            error=f"Salesforce の接続先を決定できません: {exc}",
        )
    return report_id, None


def _report_id_for_log(entry: MasterEntry) -> str:
    """``entry.url`` からログ用のレポート ID を取り出す。 URL が壊れていたら
    ``"(URL不正)"`` を返す（``_fetch_one`` 側で個別失敗に縮退する前提）。"""
    try:
        return report_id_from_url(entry.url)
    except SalesforceReportIDNotFoundError:
        return "(URL不正)"
