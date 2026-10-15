"""ID ごとに describe を取って、 結果を ``ReportRecord`` （ メモリ上の記録 ）
として返す。

流れ （ IDごと ） :

1. 管理番号 → 管理表の行 （ 無ければ呼び出し側の ``cli`` でエラー ）
2. URL から ``site_for(url)`` で接続先を決め、 ``with site() as client:`` で開く
3. ``metadata = client.report.describe(report_id)`` （ レポートの describe ）
4. 主オブジェクトの決定: ``reportType.type`` が空なら特定できず。 ``$`` / ``@``
   を含まなければその値、 含めば ``mapping.report_type_candidates()`` の候補を
   ``_fetch_object_cached()`` で順に ``describe_object`` して、 最初に成功した
   ものを主オブジェクトにする。 HTTP エラー （ ``SalesforceRequestError`` ） や
   ``ValueError`` で主オブジェクトが特定できなかった／ describe が取れなかった
   ときは、 列対応表を全列 ``(不明)`` ＋理由の備考に縮退する。 401 / 403 は
   握りつぶさず、 その ID の失敗にする
5. 関連オブジェクト （ 参照先を ``RELATED_DEPTH`` 段まで ） : 主オブジェクトの
   describe の ``fields`` を起点に幅優先で参照を辿り、 ``relationshipName``
   が空でなく ``referenceTo`` が空でない項目を **1 段ずつ** 掘る。 同じ名前は
   浅い段から先に登録される。 件数は ``RELATED_MAX`` （ 既定 40 ） で全段の
   合計を打ち切り、 超えた名前は ``warnings`` に残す
6. ``mapping.build_column_map(metadata, main_describe, reason, ...)`` で列対応表
   を組み立てる。 主オブジェクトの describe は **ID ごとに 1 回** だけ取得する
   （ ``_fetch_object_cached`` のキャッシュを経由するため、 複数 ID が同じ
   主オブジェクトを参照しても HTTP は 1 回 ）

**出力は CSV だけ**。 ``ReportRecord`` （ frozen dataclass ） に詰めて
``FetchOutcome.record`` で返す。 ``run_tables`` がこの record を受け取って
CSV を組み立てる。 許可リストでレポート describe から落としたキー名は
``_fetch_one`` の ``warnings`` に
「 レポート定義の中で使わなかったキー: ... 」 の形でも 1 行入れる
（ SOQL の組み立てはしない 2026-10-14 の方針。 落としたキー名は引き続き
可視化のための参考情報として残す ）。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field as dc_field
from pathlib import Path
from typing import Any, TypedDict

from comken.exceptions import (
    ComkenError,
    SalesforceError,
    SalesforceReportAccessDeniedError,
    SalesforceReportFormatError,
    SalesforceReportIDNotFoundError,
    SalesforceReportTruncatedError,
    SalesforceRequestError,
)
from comken.toolbox.salesforce.report import report_id_from_url

from src import column_fix_xlsx, mapping, pii, soql_builder
from src.describe_store import DescribeStore, active_picklist_values, org_from_url
from src.master import MasterEntry

logger = logging.getLogger(__name__)

# 関連オブジェクトの合計件数の上限。 全段の合計が ``RELATED_MAX`` に達したら
# 打ち切り、 超えた名前は ``warnings`` に残す。 1 = 関連オブジェクトを取らない
RELATED_MAX: int = 40

# 関連オブジェクトを掘る最大の段数。 1 = 主オブジェクトの参照先 1 段だけ
# （ 2026-10-14 以前の挙動 ）。 2 以上にすると ``Task.What.Account.Owner`` の
# ように参照先をたどって掘る
RELATED_DEPTH: int = 3

# 1 レポートごとに 2000 行チェックを実行するか。 True = 実行する
# （ 一覧シートの 「 2000件超 」 列が埋まる ）。 False = 実行しない
# （ 同列が 「 未実施 」 になる ）
ROW_LIMIT_ENABLED: bool = True

# ``describe_object`` が 401 / 403 を返したとき、 その ID 全体を失敗させる
# ステータスコード（ comken の ``ReportAPI.describe`` と同じく ）。
FATAL_STATUS_CODES: frozenset[int] = frozenset({401, 403})

# ``client.report.get()`` の戻り値 ``Table`` の最小インターフェース （ ``len()`` だけ ）。
# テストで偽の ``Table`` を入れるために ``Any`` を扱う。
_TableLike = Any


# 理由欄に出す例外メッセージの最大長（ 行データや機微情報を漏らさない ）
_ERROR_REASON_MAX_LENGTH = 200

# レポート row check の状態定数
ROW_CHECK_OVER = "超えている"
ROW_CHECK_UNDER = "超えていない"
ROW_CHECK_UNKNOWN = "判定不可"
ROW_CHECK_DISABLED = "未実施"

# 2026-10-14: ``reportType.type`` から主オブジェクトが取れなかったときの
# 代替候補として、 列キーの先頭を最大何個試すか。 多すぎても 1 度目で
# 見つかることが多いので、 この値で十分
_MAIN_OBJECT_PREFIX_LIMIT: int = 5

# 接続先判定の既定実装（ comken の ``site_for`` ）。 テストでは差し替える。
SiteFor = Callable[[str], type]

# ``_slim_report`` で残す ``reportMetadata`` のキー（ SOQL 組立に必要なものだけ ）。
# SELECT・ WHERE・ 日付条件・ サブクエリ・ GROUP BY・ 集計・ ORDER BY・ 件数・
# 独自グループ・ 独自計算式に必要な情報を網羅する。
REPORT_METADATA_KEYS: tuple[str, ...] = (
    "id",
    "name",
    "reportType",
    "reportFormat",
    "scope",
    "detailColumns",
    "reportFilters",
    "reportBooleanFilter",
    "standardDateFilter",
    "standardFilters",
    "crossFilters",
    "groupingsDown",
    "groupingsAcross",
    "aggregates",
    "sortBy",
    "topRows",
    "historicalSnapshotDates",
    "buckets",
    "customSummaryFormula",
    "division",
    "hasDetailRows",
)

# ``_slim_report`` で残す ``reportExtendedMetadata`` のキー。
# 3 つだけ（ ``detailColumnInfo`` / ``groupingColumnInfo`` /
# ``aggregateColumnInfo`` ）。
REPORT_EXTENDED_METADATA_KEYS: tuple[str, ...] = (
    "detailColumnInfo",
    "groupingColumnInfo",
    "aggregateColumnInfo",
)


@dataclass(frozen=True)
class RelatedNode:
    """幅優先で掘った関連オブジェクト 1 件。

    ``describe`` は ``fetcher`` で埋めた状態（``describe_object`` が失敗した
    ときは ``None``）。 ``depth`` は主オブジェクトから数えた段数（ 主 = 0、
    1 段目から ``RELATED_DEPTH`` まで ）。
    """

    name: str
    depth: int
    describe: dict[str, Any] | None


@dataclass(frozen=True)
class RelationRecord:
    """主＋関連の **取れた** オブジェクト同士で張られた親→子の参照 1 本。

    ``tables: 関連表.csv`` の 1 行に対応する。
    ポリモーフィックな参照は ``referenceTo`` のエントリごとに 1 本ずつ記録する。
    """

    parent: str  # 親オブジェクト名
    field: str  # 参照項目 API 名
    relationship_name: str | None  # リレーション名（ ``fields[].relationshipName`` ）
    child: str  # 子オブジェクト名
    depth: int  # 子の深さ（ 主 = 0 ）


@dataclass(frozen=True)
class RowCheck:
    """2000 行チェックの結果。 1 レポート 1 件。

    ``status`` は次のいずれか:

    - ``超えている`` （ SalesforceReportTruncatedError ）
    - ``超えていない`` （ 取得でき、 ``rows`` に行数 ）
    - ``判定不可`` （ ``reason`` に理由 ）
    - ``未実施`` （ ``ROW_LIMIT_ENABLED`` が ``False`` のとき ）

    行データ（ 値そのもの ） は **保持しない** 。 メモリ上でも行数だけ数え、
    CSV / ログ / 警告 / 例外メッセージに絶対に出さない。
    """

    status: str
    rows: int | None
    reason: str | None


@dataclass(frozen=True)
class ReportRecord:
    """1 ID の取得結果をメモリに保持する記録。 ``frozen`` で再代入しない。

    ``fetch`` が ``run`` に渡して、 ``run`` が ``run_tables`` に渡す。
    ``tables`` はこの record から CSV を組み立てる。 JSON ファイルは作らない。
    """

    key: str  # 管理番号
    summary: str  # 概要
    report_id: str  # レポート ID
    url: str  # レポート URL
    main_object: str | None  # 主オブジェクト名（ 取得失敗なら ``None`` ）
    #: レポート describe を ``_slim_report`` で絞った dict。 CSV 生成や
    #: 警告表 で読む。 落としたキー名はここに含めず ``_collect_dropped_keys``
    #: で別途取り出す。
    report: dict[str, Any]
    #: 主＋関連のオブジェクト describe を ``_slim_object`` で絞った dict。
    #: キー = オブジェクト名、 値 = ``{name, label, custom, fields}``
    objects: dict[str, dict[str, Any]]
    relations: tuple[RelationRecord, ...]  # 主＋関連オブジェクト同士の参照
    column_map: list[dict[str, str]]  # 列対応表
    warnings: tuple[str, ...]  # 警告メッセージ
    #: 2000 行チェックの結果。 ``None`` なら 未実施。 失敗した ID は record ごと無い
    row_check: RowCheck | None = None
    #: レポート単位の PII 判定結果。 調査表と 対応表の 個人情報 列で使う
    pii: pii.ReportPIIResult = dc_field(
        default_factory=lambda: pii.ReportPIIResult(status="なし", matching_columns=())
    )
    #: SOQL の下書き。 ``tables`` が組み立てて持たせ、 次段 （ AI に渡す
    #: Markdown / xlsx ） で読む。 ``None`` のときは下書き未組み立て
    #: （ 主オブジェクト不明など、 組み立てそのものが走らなかったケース ）
    soql_draft: Any = dc_field(
        default=None,
    )


@dataclass(frozen=True)
class FetchOutcome:
    """1 ID の取得結果。"""

    entry: MasterEntry
    status: str  # "ok" / "failed" / "dry-run"
    record: ReportRecord | None  # status="ok" のときだけ入る
    warnings: tuple[str, ...]
    error: str | None


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
    refresh_set: set[tuple[str, str]] | None = None,
    column_fix_overrides: dict[tuple[str, str], Any] | None = None,
) -> list[FetchOutcome]:
    """``entries`` の各エントリについて、 describe を取り ``ReportRecord`` を返す。

    **JSON を書かない**。 結果は ``outcomes[i].record`` で取り出して、
    そのまま ``run_tables`` に渡す。

    Args:
        entries: 処理対象の管理表エントリ（ 既に ``--all`` 絞り込み済み ）。
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
        refresh_set: 取り直し （ 一度 API で取り直して再試行 ） を実行済みの
            ``(org, object_lower)`` の集合。 ``None`` のときは内部で作る。
        column_fix_overrides: 人が ``列の直し.xlsx`` に入れた上書き。
            ``(主オブジェクト小文字, 列キー)`` → ``ColumnMapRow`` の dict。
            ``_fetch_one`` に渡して ``column_map`` の ``対応フィールドAPI名``
            等を差し替える。 空 dict なら何もしない

    Returns:
        入力と同じ順の ``FetchOutcome`` リスト。失敗 ID を含む個別では continue する。
    """
    if site_for is None:
        from comken.toolbox.salesforce.sites import site_for as default_site_for

        site_for = default_site_for

    assert site_for is not None  # 上の分岐で必ず代入される

    if describe_store is None:
        describe_store = DescribeStore.empty(describe_csv_path)
    if refresh_set is None:
        refresh_set = set()
    if column_fix_overrides is None:
        column_fix_overrides = {}

    outcomes: list[FetchOutcome] = []
    total = len(entries)
    for index, entry in enumerate(entries, start=1):
        if dry_run:
            # dry-run のログは呼び出し側（ ``run.py`` / ``trial/`` ） が
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


# ── ログ ──────────────────────────────────────────────────────────────────
# 1 レポート分の処理の各節目で INFO を 1 行ずつ出す。 INFO は節目と分岐の
# 理由だけにとどめ、 値そのもの（ 個人情報の値など ） は絶対に出さない。
# 失敗時の ERROR は ``run_fetch`` 側で出す。 フォールバック（ except で
# 続行した箇所 ） の WARNING は ``_fetch_object_cached`` 側で出す。
# ログメッセージは既存スタイル（ ``管理番号=%s 概要=%s 警告=%d 件`` ） に
# そろえる。


def _report_id_for_log(entry: MasterEntry) -> str:
    """``entry.url`` からログ用のレポート ID を取り出す。 URL が壊れていたら
    ``"(URL不正)"`` を返す（ ``_fetch_one`` 側で個別失敗に縮退する前提）。"""
    try:
        return report_id_from_url(entry.url)
    except SalesforceReportIDNotFoundError:
        return "(URL不正)"


def _log_row_check(row_check: RowCheck | None) -> None:
    """2000 件チェックの結果を 1 行で出す。 行数だけ、 値は出さない。"""
    if row_check is None:
        logger.info("  2000件チェック: 未実施")
        return
    rows_text = str(row_check.rows) if row_check.rows is not None else "-"
    logger.info("  2000件チェック: 判定=%s 行数=%s", row_check.status, rows_text)


def _log_report_contents(slim_report: SlimReport, main_object_name: str | None) -> None:
    """レポートの形式・主オブジェクト・列数・フィルタ数を 1 行にまとめて出す。"""
    report_metadata = slim_report["reportMetadata"]
    logger.info(
        "  レポート: 形式=%s 主オブジェクト=%s 列数=%d フィルタ数=%d",
        report_metadata.get("reportFormat") or "(なし)",
        main_object_name or "(不明)",
        len(report_metadata.get("detailColumns", [])),
        len(report_metadata.get("reportFilters", [])),
    )


def _log_related_objects(
    related_nodes: list[RelatedNode],
    skipped_names: list[str],
) -> None:
    """関連オブジェクトの収集結果を 1 行で出す。 件数・段・打ち切りの有無。"""
    collected_count = len(related_nodes)
    if collected_count == 0:
        logger.info("  関連オブジェクト: 0 件 打ち切り=なし")
        return
    depth_reached = max(node.depth for node in related_nodes)
    truncated = bool(skipped_names)
    logger.info(
        "  関連オブジェクト: %d 件 段=%d 打ち切り=%s",
        collected_count,
        depth_reached,
        "あり" if truncated else "なし",
    )


def _log_pii(pii_result: pii.ReportPIIResult) -> None:
    """個人情報判定の結果を 1 行で出す。 列名だけ、 値は出さない。"""
    columns_text = ", ".join(label for label, _ in pii_result.matching_columns) or "-"
    logger.info("  個人情報: %s 根拠列=%s", pii_result.status, columns_text)


# ── record 用の slim ────────────────────────────────────────────────────────
# JSON はもう書かないが、 ``record`` に詰めるデータも SOQL 組立に必要な
# **構造だけ** にする。 レポート describe は ``_slim_report`` で
# ``REPORT_METADATA_KEYS`` の許可リストに絞り、 オブジェクト describe は
# ``_slim_object`` で ``name`` / ``label`` / ``custom`` / ``fields`` だけ、
# 項目は 8 キーだけに絞る。 許可リストで落としたキー名は別関数
# ``_collect_dropped_keys()`` で取り出し、 warnings に 1 行入れる。
#
# 関連オブジェクト収集は ``_collect_related_objects`` （ slim 前の
# ``fields[].referenceTo`` / ``relationshipName`` を使う ） に任せて、
# slim は ``_fetch_one`` の最後、 record に詰める直前だけ行う。


# ── slim 後のデータ型 ─────────────────────────────────────────────
# SOQL 組立と ``tables`` / ``ai_markdown`` が読む範囲だけを型で示す。
# SlimReport は ``reportMetadata`` / ``reportExtendedMetadata`` を持つ dict。
# SlimObject は ``_slim_object`` の戻り値の構造（ TypedDict ） 。 フィールドは
# 全部 optional にして、 境界検証に失敗したときの欠落キーを表現する。
SlimReport = dict[str, Any]


class SlimObject(TypedDict, total=False):
    """``_slim_object`` の戻り値の構造。"""

    name: str
    label: str
    custom: bool
    fields: list[dict[str, Any]]


def _slim_report(metadata: object) -> SlimReport:
    """``client.report.describe()`` の戻り値を、 内側で検証せずに読める形に整える（ 境界 ）。

    - 戻り値は **必ず** ``reportMetadata`` と ``reportExtendedMetadata`` の 2 つの
      dict を持つ（ 入力が壊れていれば空の dict ）
    - ``reportMetadata``: ``REPORT_METADATA_KEYS`` にあるキーだけ残す。 リストとして
      読むキーは中身の型をそろえる（ ``detailColumns`` / ``aggregates`` は空でない
      str だけ、 ``reportFilters`` / ``groupingsDown`` / ``groupingsAcross`` /
      ``sortBy`` は dict だけ、 ``reportType`` は dict ）。 型がそろっている値は元の
      参照のまま
    - ``reportExtendedMetadata``: ``REPORT_EXTENDED_METADATA_KEYS`` の 3 キーだけを、
      ``{列キー: dict}`` の形で残す
    - 落としたキー名は含めない（ ``_collect_dropped_keys()`` で取り出す ）
    - 入力の dict は書き換えない
    """
    source = metadata if isinstance(metadata, dict) else {}
    report_metadata_raw = source.get("reportMetadata")
    report_extended_raw = source.get("reportExtendedMetadata")
    report_metadata = report_metadata_raw if isinstance(report_metadata_raw, dict) else {}
    report_extended = report_extended_raw if isinstance(report_extended_raw, dict) else {}

    slimmed_metadata = {
        key: value for key, value in report_metadata.items() if key in REPORT_METADATA_KEYS
    }
    for key in ("detailColumns", "aggregates"):
        if key in slimmed_metadata:
            slimmed_metadata[key] = _str_list(slimmed_metadata[key])
    for key in _TEXT_KEYS:
        if key in slimmed_metadata:
            slimmed_metadata[key] = _text(slimmed_metadata[key])
    # 要素の中で文字列として読む値の型もそろえる。 絞り込みは dict でない要素も
    # 空の dict にして位置を保つ（ 並び順が組立式の番号と対応しているため ）
    for key, text_keys in _TEXT_KEYS_IN_LIST_ITEMS.items():
        if key not in slimmed_metadata:
            continue
        value = slimmed_metadata[key]
        items = (
            [item if isinstance(item, dict) else {} for item in value]
            if key == "reportFilters" and isinstance(value, list)
            else _dict_list(value)
        )
        slimmed_metadata[key] = [_with_text_values(item, text_keys) for item in items]
    for key, text_keys in _TEXT_KEYS_IN_DICT_VALUES.items():
        if key not in slimmed_metadata:
            continue
        value = slimmed_metadata[key]
        slimmed_metadata[key] = (
            _with_text_values(value, text_keys) if isinstance(value, dict) else None
        )
    top_rows = slimmed_metadata.get("topRows")
    if top_rows is not None:
        slimmed_metadata["topRows"] = {
            **top_rows,
            "rowLimit": _positive_int(top_rows.get("rowLimit")),
        }

    # ``standardDateFilter`` が 「 実質指定なし 」 （ start / end が空で
    # durationValue が空か ``CUSTOM`` ） なら、 下流に空の日付フィルターが
    # 流れないようにここでキーごと落とす。 内側では「 無い 」 ものとして
    # 扱う前提で組まれているので、 個別ガードは足さない
    if _is_undated_filter(slimmed_metadata.get("standardDateFilter")):
        slimmed_metadata.pop("standardDateFilter", None)

    slimmed_extended = {
        key: {
            column_key: info
            for column_key, info in value.items()
            if isinstance(column_key, str) and isinstance(info, dict)
        }
        for key, value in report_extended.items()
        if key in REPORT_EXTENDED_METADATA_KEYS and isinstance(value, dict)
    }
    return {"reportMetadata": slimmed_metadata, "reportExtendedMetadata": slimmed_extended}


def _is_undated_filter(date_filter: object) -> bool:
    """``standardDateFilter`` が 「 実質指定なし 」 なら True。

    定義: ``startDate`` と ``endDate`` がどちらも空 （ ``None`` / 空文字 /
    空白のみ ） で、 かつ ``durationValue`` が空か ``CUSTOM`` （ 大文字小文字
    無視 ） 。 相対期間 （ ``THIS_YEAR`` / ``LAST_N_DAYS:30`` 等 ） は
    「 指定あり 」 なので False。 ``date_filter`` が dict で無いときも
    True （ 「 無かった 」 ものとして扱う ）
    """
    if not isinstance(date_filter, dict):
        return True

    def _is_blank(value: object) -> bool:
        if value is None:
            return True
        if not isinstance(value, str):
            return True
        return not value.strip()

    if not _is_blank(date_filter.get("startDate")):
        return False
    if not _is_blank(date_filter.get("endDate")):
        return False

    duration = date_filter.get("durationValue")
    if isinstance(duration, str):
        normalized = duration.strip()
        if not normalized:
            return True
        return normalized.upper() == "CUSTOM"
    return True


# 文字列としてそろえる単純な値のキー
_TEXT_KEYS: tuple[str, ...] = ("id", "name", "reportFormat", "scope", "reportBooleanFilter")

# リストの要素（ dict ）の中で、 文字列として読む値のキー
_TEXT_KEYS_IN_LIST_ITEMS: dict[str, tuple[str, ...]] = {
    "reportFilters": ("column", "operator"),
    "groupingsDown": ("name", "sortOrder", "dateGranularity"),
    "groupingsAcross": ("name", "sortOrder", "dateGranularity"),
    "sortBy": ("sortColumn", "sortOrder"),
}

# dict の値の中で、 文字列として読む値のキー（ dict でなければ None。 Salesforce は
# 使わない項目を null で返す ）
_TEXT_KEYS_IN_DICT_VALUES: dict[str, tuple[str, ...]] = {
    "reportType": ("type",),
    "standardDateFilter": ("column", "durationValue", "startDate", "endDate"),
    "topRows": ("direction",),
}


def _with_text_values(item: dict[str, Any], text_keys: tuple[str, ...]) -> dict[str, Any]:
    """``item`` の ``text_keys`` のうち、 ある値を str にそろえる。 そろっていれば元の dict。"""
    if all(isinstance(item.get(key), str) for key in text_keys if key in item):
        return item
    return {key: _text(value) if key in text_keys else value for key, value in item.items()}


def _str_list(value: object) -> list[str]:
    """空でない str だけのリストにする。 すでにそろっていれば元のリストを返す。"""
    if not isinstance(value, list):
        return []
    if all(isinstance(item, str) and item for item in value):
        return value
    return [item for item in value if isinstance(item, str) and item]


def _dict_list(value: object) -> list[dict[str, Any]]:
    """dict だけのリストにする。 すでにそろっていれば元のリストを返す。"""
    if not isinstance(value, list):
        return []
    if all(isinstance(item, dict) for item in value):
        return value
    return [item for item in value if isinstance(item, dict)]


def _positive_int(value: object) -> int | None:
    """正の整数（ 数字だけの文字列も可 ）なら int、 それ以外（ bool を含む ）は None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    return value if isinstance(value, int) and value > 0 else None


def _text(value: object) -> str:
    """str ならそのまま、 それ以外は空文字（ 境界の正規化用 ）。"""
    return value if isinstance(value, str) else ""


def _collect_dropped_keys(metadata: object) -> tuple[str, ...]:
    """``_slim_report`` と同じルールで、 落としたキー名を出現順で返す。

    warnings に 「 レポート定義の中で使わなかったキー: ... 」 の 1 行を
    出すために使う。 JSON 出力は一切しない （ CSV 化も不要、 ログは入力区 ）
    """
    buckets: list[str] = []

    if not isinstance(metadata, dict):
        return ()

    report_metadata_raw = metadata.get("reportMetadata")
    if isinstance(report_metadata_raw, dict):
        for key in report_metadata_raw:
            if key not in REPORT_METADATA_KEYS:
                buckets.append(key)

    report_extended_raw = metadata.get("reportExtendedMetadata")
    if isinstance(report_extended_raw, dict):
        for key in report_extended_raw:
            if key not in REPORT_EXTENDED_METADATA_KEYS:
                buckets.append(key)

    for key in metadata:
        if key in ("reportMetadata", "reportExtendedMetadata"):
            continue
        buckets.append(key)

    return tuple(buckets)


def _slim_object(describe: object) -> SlimObject:
    """``describe_object()`` の戻り値を、 内側で検証せずに読める形に整える（ 境界 ）。

    - 残す: ``name`` / ``label`` / ``custom`` / ``fields``（ ``childRelationships`` /
      ``recordTypeInfos`` などは落とす ）
    - ``fields`` の各項目は次の 9 キーに**必ず**この型でそろえる: ``name`` /
      ``label`` / ``type`` / ``relationshipName``（ str。 無ければ空文字 ）、
      ``custom`` / ``nameField``（ bool ）、 ``referenceTo``（ 空でない str の
      リスト ）、 ``picklist``（ active な ``value`` を最大 30 件 ）、
      ``picklistTotal``（ active な選択肢の総数 ）
    - 入力が壊れていれば空の値で返す。 入力の dict は書き換えない
    """
    source = describe if isinstance(describe, dict) else {}
    fields_raw = source.get("fields")
    fields_list: list[Any] = fields_raw if isinstance(fields_raw, list) else []
    return {
        "name": _text(source.get("name")),
        "label": _text(source.get("label")),
        "custom": source.get("custom") is True,
        "fields": [_slim_field(field) for field in fields_list if isinstance(field, dict)],
    }


def _slim_field(field: dict[str, Any]) -> dict[str, Any]:
    """``fields`` の各項目を 9 キーにそろえる。キーの**順番**もこの順で返す。"""
    picklist, picklist_total = _extract_picklist(field.get("picklistValues"))
    return {
        "name": _text(field.get("name")),
        "label": _text(field.get("label")),
        "type": _text(field.get("type")),
        "custom": field.get("custom") is True,
        "referenceTo": _str_list(field.get("referenceTo")),
        "relationshipName": _text(field.get("relationshipName")),
        "nameField": field.get("nameField") is True,
        "picklist": picklist,
        "picklistTotal": picklist_total,
    }


def _extract_picklist(picklist_values: object) -> tuple[list[str], int]:
    """``picklistValues`` の active な ``value`` を、 最大 30 件と総数で返す。"""
    active = active_picklist_values(picklist_values)
    return active[:30], len(active)


def _dropped_keys_warning_text(dropped: tuple[str, ...]) -> str | None:
    """``_collect_dropped_keys()`` の戻り値から警告表用の 1 行を組み立てる。

    何も落ちていないときは ``None`` を返す。 出力形式:

        レポート定義の中で使わなかったキー: k1, k2, k3
    """
    if not dropped:
        return None
    return "レポート定義の中で使わなかったキー: " + ", ".join(dropped)


def _check_row_count(client: Any, report_id: str) -> RowCheck:
    """``client.report.get()`` を呼んで 2000 行チェックを行う。

    comken の ``ReportAPI.get()`` は TABULAR だけでなく SUMMARY / MATRIX の
    レポートも行を返すので、 describe の ``reportFormat`` に関わらず常に
    ``get`` を呼ぶ。

    - ``SalesforceReportTruncatedError`` → ``超えている`` （ ``rows=None`` ）
    - 正常終了 → ``超えていない`` （ ``rows=len(table)`` ）
    - 形式エラー （ 結合レポート・明細の列が無いレポートなど ） → ``判定不可`` で
      「この形式のレポートは取得できないため判定できません（結合レポート・明細の列が無いレポートなど）」
    - 401 / 403 由来の ``SalesforceError`` は呼び出し元に再送出（ レポート全体を
      失敗扱いにする ）
    - その他の例外 → ``判定不可`` で 例外名＋短い説明
    """
    try:
        table = client.report.get(report_id)
    except SalesforceReportTruncatedError:
        # 2000 行超。 切り捨て検知は正常パスの一部なので reason は None。
        return RowCheck(status=ROW_CHECK_OVER, rows=None, reason=None)
    except SalesforceError as exc:
        if isinstance(exc, SalesforceReportAccessDeniedError):
            # 401 / 403: レポート全体を失敗扱いにするために再送出
            raise
        if isinstance(exc, SalesforceReportFormatError):
            return RowCheck(
                status=ROW_CHECK_UNKNOWN,
                rows=None,
                reason=(
                    "この形式のレポートは取得できないため判定できません"
                    "（結合レポート・明細の列が無いレポートなど）"
                ),
            )
        return RowCheck(
            status=ROW_CHECK_UNKNOWN,
            rows=None,
            reason=_format_error_reason(exc),
        )
    except Exception as exc:
        return RowCheck(
            status=ROW_CHECK_UNKNOWN,
            rows=None,
            reason=_format_error_reason(exc),
        )
    return RowCheck(
        status=ROW_CHECK_UNDER,
        rows=len(table),
        reason=None,
    )


def _format_error_reason(exc: BaseException) -> str:
    """例外を 1 行の理由文字列に整形する。 メッセージは上限で切る。"""
    type_name = type(exc).__name__
    message = str(exc).strip()
    if not message:
        return type_name
    if len(message) > _ERROR_REASON_MAX_LENGTH:
        message = message[:_ERROR_REASON_MAX_LENGTH] + "…"
    return f"{type_name}: {message}"


def _evaluate_record_pii(
    slim_report: SlimReport,
    column_map: list[dict[str, str]],
    slim_objects: dict[str, dict[str, Any]],
    config: pii.PIIConfig,
) -> pii.ReportPIIResult:
    """``ReportRecord`` 用の PII 評価を行う。 出力列のみ対象。"""
    refs = _collect_output_column_refs(slim_report, column_map)
    return pii.evaluate_report_pii(refs, config, slim_objects)


def _collect_output_column_refs(
    slim_report: SlimReport,
    column_map: list[dict[str, str]],
) -> list[pii.ColumnRef]:
    """レポートの出力列（ detailColumns / groupingsDown / groupingsAcross /
    集計対象 ） について PII 評価の入力 ``ColumnRef`` を作る。

    絞り込みにだけ使う列は対象外。 ``column_map`` が見つからない列は未解決として
    扱う。
    """
    column_map_by_key = {col["列キー"]: col for col in column_map}
    refs: list[pii.ColumnRef] = []
    for column_key in dict.fromkeys(mapping.output_column_keys(slim_report["reportMetadata"])):
        col = column_map_by_key.get(column_key, {})
        field_api = col.get("対応フィールドAPI名", "")
        refs.append(
            pii.ColumnRef(
                column_key=column_key,
                label=col.get("表示名") or column_key,
                field_name=mapping.field_name_from_soql(field_api),
                field_type=col.get("型", ""),
                owner_object=col.get("所属オブジェクト", ""),
                resolved=bool(field_api) and field_api != "(不明)",
            )
        )
    return refs


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
    column_fix_overrides: dict[tuple[str, str], Any] | None = None,
) -> FetchOutcome:
    """1 ID の describe を取り、 ``ReportRecord`` を組み立てて返す。

    JSON は書かない。 失敗したら ``status="failed"`` を返し ``record`` は ``None`` 。
    """
    warnings: list[str] = []
    if column_fix_overrides is None:
        column_fix_overrides = {}
    org = org_from_url(entry.url)

    # URL → レポート ID
    try:
        report_id = report_id_from_url(entry.url)
    except SalesforceReportIDNotFoundError:
        return FetchOutcome(
            entry=entry,
            status="failed",
            record=None,
            warnings=(),
            error=(
                f"管理表の URL からレポート ID を取り出せません: {entry.url}"
                f"（管理番号 {entry.key}）"
            ),
        )

    # 接続先決定。``site_for`` が ``SalesforceError``（未登録組織）などの
    # ``ComkenError`` を出すと、その ID だけの失敗にする（他の ID は続ける）
    try:
        site_class = site_for(entry.url)
    except ComkenError as exc:
        return FetchOutcome(
            entry=entry,
            status="failed",
            record=None,
            warnings=(),
            error=f"Salesforce の接続先を決定できません: {exc}",
        )

    # 接続〜取得
    # ``objects`` はオブジェクト名 → ``_slim_object`` で整えた describe。 取ってきた
    # describe は入口（ ``_fetch_object_cached`` / fetcher / 取り直し ）で必ず整え、
    # 以後は整えた値だけを扱う（ 生の describe は項目定義のキャッシュにだけ書く ）
    objects: dict[str, dict[str, Any]] = {}
    main_object_name: str | None = None
    metadata: dict[str, Any] = {}
    slim_report: SlimReport = _slim_report(None)
    column_map: list[dict[str, Any]] = []
    related_describes: dict[str, dict[str, Any]] = {}
    related_nodes: list[RelatedNode] = []
    relations: list[RelationRecord] = []
    row_check: RowCheck | None = None
    object_reason: str | None = None

    # CSV から読んだ （ 今回 API で取ったものではない ） オブジェクトのキー。
    # 取り直し判定に使う。
    csv_used_keys: set[tuple[str, str]] = set()

    try:
        with site_class() as client:
            try:
                metadata = client.report.describe(report_id)
            except SalesforceRequestError as exc:
                return FetchOutcome(
                    entry=entry,
                    status="failed",
                    record=None,
                    warnings=(),
                    error=(
                        f"レポート describe に失敗（HTTP {exc.status_code}）: {report_id}"
                        f"\n{exc.detail}"
                    ),
                )

            # 境界: レポート定義はここで 1 回だけ整え、 以後は整えた値だけを使う
            slim_report = _slim_report(metadata)

            # 2000 行チェック: ``row_limit`` が True のときだけ実行。
            # 401 / 403 のような致命エラーは外側の except に伝播させて ID 全体を
            # 失敗扱いにする。 その他のエラーは ``RowCheck`` に縮退する。
            row_check = _check_row_count(client, report_id) if row_limit else None
            _log_row_check(row_check)

            # 主オブジェクトの解決。 ``describe_object`` が 401 / 403 を返したら
            # ``_fetch_object_cached`` がそのまま上位へ ``SalesforceRequestError``
            # を送出し、 ID 全体を失敗扱いにする。
            main_object_name, main_describe, object_reason, csv_used_keys = _choose_main(
                client, org, slim_report, store, warnings
            )
            if object_reason:
                warnings.append(object_reason)
            _log_report_contents(slim_report, main_object_name)

            # 主オブジェクトが確定したら ``objects`` の
            # 種を蒔いて、 列キーからのたどりに使う fetcher を作る。 同じ
            # fetcher を後の関連オブジェクト収集後にも ``build_column_map`` /
            # ``apply_overrides`` に渡すので、 1 度作った dict を使い回す。
            fetcher: Callable[[str], dict[str, Any] | None] | None = None
            if main_object_name and main_describe is not None:
                objects[main_object_name] = main_describe
                fetcher = _make_overrides_fetcher(
                    client=client,
                    org=org,
                    store=store,
                    objects=objects,
                    csv_used_keys=csv_used_keys,
                )

            # 主オブジェクトが取れなかったときの列対応表（ 全列が理由つきの (不明) ）。
            # 取れたときは、 関連オブジェクトを集めてから下で 1 回だけ組み立てる
            if not (main_object_name and main_describe is not None):
                column_map = mapping.build_column_map(
                    slim_report,
                    main_describe,
                    object_reason,
                    related_describes=related_describes,
                    relations=[],
                    main_object=main_object_name,
                    fetch_object=fetcher,
                )
            else:
                # 関連オブジェクトを参照先から幅優先で掘る（ ``RELATED_DEPTH`` 段まで ）
                related_nodes, relations, skipped_names = _collect_related_objects(
                    main_describe,
                    fetcher=lambda name: _fetch_object_cached(
                        client, org, name, store, warnings, csv_used_keys
                    ),
                    related_depth=related_depth,
                    related_max=related_max,
                )
                if skipped_names:
                    collected_count = len(related_nodes)
                    total = collected_count + len(skipped_names)
                    warnings.append(
                        f"関連オブジェクトが {total} 件あり"
                        f"上限 {related_max} を超えたため、"
                        f"次の {len(skipped_names)} 件は取得しません: {', '.join(skipped_names)}"
                    )
                _log_related_objects(related_nodes, skipped_names)
                for node in related_nodes:
                    if node.describe is not None:
                        objects[node.name] = node.describe
                        related_describes[node.name] = node.describe

                # 列対応表を取り直す（ 関連オブジェクトが取れてから解く ）
                column_map = mapping.build_column_map(
                    slim_report,
                    main_describe,
                    object_reason,
                    related_describes=related_describes,
                    relations=relations,
                    main_object=main_object_name,
                    fetch_object=fetcher,
                )

            # 取り直し: 列対応に (不明) が残り、 CSV から読んだオブジェクトが
            # このレポートで使われているなら API で取り直す。
            if (
                main_object_name
                and main_describe is not None
                and _has_unresolved_columns(column_map, object_reason)
            ):
                refresh_keys = csv_used_keys - refresh_set
                if refresh_keys:
                    refresh_set.update(refresh_keys)
                    for key in refresh_keys:
                        object_name = _api_name_from_store(store, key)
                        if object_name is None:
                            continue
                        logger.info(
                            "取り直し: 管理番号=%s 組織=%s オブジェクト=%s",
                            entry.key,
                            key[0],
                            object_name,
                        )
                        refreshed_raw = store.refresh_from_api(
                            client, key[0], object_name, warnings
                        )
                        if refreshed_raw is None:
                            continue
                        # 境界: 取り直した describe もここで整えてからメモリに反映
                        refreshed = _slim_object(refreshed_raw)
                        if main_object_name and _object_key(org, main_object_name) == key:
                            main_describe = refreshed
                            objects[main_object_name] = refreshed
                        else:
                            for obj_name in list(objects):
                                if _object_key(org, obj_name) == key:
                                    objects[obj_name] = refreshed
                                    related_describes[obj_name] = refreshed
                                    break

                    # 列対応表を取り直す（ 取り直し後 ）
                    column_map = mapping.build_column_map(
                        slim_report,
                        main_describe,
                        object_reason,
                        related_describes=related_describes,
                        relations=relations,
                        main_object=main_object_name,
                        fetch_object=fetcher,
                    )
                # 1 パス内では取り直しは 1 度きり （ 取り直し後も (不明) が
                # 残るケースは API 側でも解決できないので、 無限ループを避ける ）
                # ※ 同じ実行の中で複数レポートがある場合は refresh_set で
                #   同じ (org, object) を 1 度しか取り直さない

            # 人が ``列の直し.xlsx`` に入れた値で ``column_map`` を上書きする。
            # ``apply_overrides`` が ``Account.Owner.Name`` のような経路を辿る
            # ときに、 その経路上に無いオブジェクトは fetcher が取りに行く。
            # 取れたオブジェクトは ``objects`` に加わる
            column_map = column_fix_xlsx.apply_overrides(
                column_map_rows=column_map,
                existing=column_fix_overrides,
                main_object=main_object_name or "",
                objects=objects,
                warnings=warnings,
                fetch_object=fetcher,
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

    main_object = main_object_name if main_object_name and main_object_name in objects else None
    if main_object_name and main_object_name not in objects:
        warnings.append(
            f"主オブジェクト {main_object_name} の describe に失敗したため"
            f"関連オブジェクトは取得していません"
        )

    dropped_keys = _collect_dropped_keys(metadata)
    dropped_keys_warning = _dropped_keys_warning_text(dropped_keys)
    if dropped_keys_warning:
        warnings.append(dropped_keys_warning)

    # PII 評価: 出力列だけを対象にする。 絞り込みにだけ使う列は対象外。
    pii_config = pii.PIIConfig(
        keywords=pii_keywords,
        person_objects=pii_person_objects,
    )
    pii_result = _evaluate_record_pii(slim_report, column_map, objects, pii_config)
    _log_pii(pii_result)

    # 2000 件超判定が 判定不可 なら、 警告表にも 1 行入れる
    if row_check is not None and row_check.status == ROW_CHECK_UNKNOWN and row_check.reason:
        warnings.append(f"2000件超の判定ができませんでした: {row_check.reason}")

    # SOQL の下書きを組み立てる （ 2026-10-15 復活 ） 。 取れた record 1 件につき
    # 1 回 ``build_soql`` を呼び、 結果は record の ``soql_draft`` に詰める。
    # 一部作れずの SOQL は先頭に ``INCOMPLETE_MARKER`` が付き、 Salesforce に
    # 投げると必ず止まる形になる （ 詳しくは ``src/soql_builder.py`` ）
    soql_draft = soql_builder.build_soql(
        ReportRecord(
            key=entry.key,
            summary=entry.summary,
            report_id=report_id,
            url=entry.url,
            main_object=main_object,
            report=slim_report,
            objects=objects,
            relations=tuple(relations),
            column_map=column_map,
            warnings=tuple(warnings),
        )
    )

    record = ReportRecord(
        key=entry.key,
        summary=entry.summary,
        report_id=report_id,
        url=entry.url,
        main_object=main_object,
        report=slim_report,
        objects=objects,
        relations=tuple(relations),
        column_map=column_map,
        warnings=tuple(warnings),
        row_check=row_check,
        pii=pii_result,
        soql_draft=soql_draft,
    )

    return FetchOutcome(
        entry=entry,
        status="ok",
        record=record,
        warnings=tuple(warnings),
        error=None,
    )


def _collect_related_objects(
    main_describe: dict[str, Any],
    fetcher: Callable[[str], dict[str, Any] | None],
    *,
    related_depth: int,
    related_max: int,
) -> tuple[list[RelatedNode], list[RelationRecord], list[str]]:
    """主オブジェクトから幅優先で関連オブジェクトを掘る。

    BFS は **「 どのオブジェクトを取るか（ nodes と段 ） 」** だけを決め、
    ``visited`` に主オブジェクトを最初から入れる。 これで BFS 中に主
    オブジェクトが ``nodes`` に入らず、 describe の再取得も発生しない。
    ``RELATED_DEPTH`` 段まで掘り、 各段で frontier を全部消費して次の
    frontier を作る。 重複は **見つけた時点で** ``visited`` に入れ、
    ``RELATED_MAX`` の制限も frontier を作る段で「 候補のまま 」 打ち切る。
    打ち切られた候補は ``visited`` に既に入っているので同じ実行内では
    再試行しない（ より浅い経路の 1 本だけが残る ）。

    ``relations`` は BFS が **終わったあと** に別の処理で作る。 主オブジェクト
    と ``nodes`` の ``fields`` を主→ ``nodes`` の順（ ``nodes`` は段順 ） に
    たどり、 参照先が「 取れたオブジェクト（ 主を含む ）」 であれば全部
    1 行にする。 行の ``段`` は **子の段** （ 主 = 0 ）。 並びは「 浅い親
    （ 主→段順 ） → 同じ親内では項目の出現順 」。 ポリモーフィック参照は
    ``referenceTo`` のエントリごとに 1 行。 同じ ``(親, 項目, 子)`` は 1 行。
    取り損ねた子（ 上限で打ち切られた子・ describe に失敗した子 ） は
    親にも子にも行に出ない。 主オブジェクトへの参照（ 例: Account.TaskId
    → Task ） や自己参照（ 例: Account.ParentId → Account ） も **出る** 。

    戻り値の ``skipped`` は **重複を除いた** 出現順のリスト。 同じ名前が
    複数候補として出ても 1 回しか数えない。 警告文の「 N 件」 もこの
    重複を除いた数。

    Args:
        main_describe: 主オブジェクトの describe（ slim 前 ）。
        fetcher: オブジェクト名を取り、 ``describe`` を返す関数。 取れなかった
            ／ 失敗したときは ``None`` を返す （ 警告文は呼び出し側で積む ）。
            同じ名前の 2 度目の呼び出しはキャッシュが効いて ``None`` か
            ``describe`` がそのまま返る （ HTTP を 2 回打たない ） 前提。
        related_depth: 何段目まで掘るか。 ``1`` なら今と同じ（ 1 段だけ ）。
        related_max: 全体で取る関連オブジェクト数の上限。 全段の合計が
            ``related_max`` に達したら打ち切る。

    Returns:
        ``(nodes, relations, skipped)``
        ``nodes``: 深さごとに並んだ ``RelatedNode`` のリスト（ **主オブジェクト
            自身は含まない** ）。 出現順は「 浅い親から先 」 。 ``describe``
            は ``fetcher`` で埋めた状態 （ ``fetcher`` が ``None`` を返した
            ものは ``nodes`` に含めない — 上限内に収まったが describe 取得
            に失敗した分 ）。
        ``relations``: **取れたオブジェクト同士** で張られた親→子の参照
            リスト。 主＋関連オブジェクトが「 お互いに参照している 」 行を
            全部含む（ 主オブジェクトへの参照、 自己参照も出る ）。 並びは
            「 浅い親（ 主→段順 ） → 同じ親内では項目の出現順 」 。 行の
            ``段`` は **子の段** （ 主 = 0 ）。 ポリモーフィック参照は
            ``referenceTo`` のエントリごとに 1 行。 同じ
            ``(親, 項目, 子)`` は 1 行。 取り損ねた子は親にも子にも行に
            出ない。
        ``skipped``: ``related_max`` のせいで掘れなかった候補の
            オブジェクト名。 重複を除いた出現順のリスト。
    """
    main_name = main_describe.get("name") or None

    nodes: list[RelatedNode] = []
    relations: list[RelationRecord] = []
    skipped: list[str] = []

    if main_name is None or related_depth < 1:
        return nodes, relations, skipped

    nodes, skipped = _bfs_related_objects(
        main_name, main_describe, fetcher, related_depth, related_max
    )
    # skipped の重複を除く （ 出現順を保つ ）
    skipped = _dedup_preserve_order(skipped)

    relations = _collect_relations(main_name, main_describe, nodes)
    return nodes, relations, skipped


def _bfs_related_objects(
    main_name: str,
    main_describe: dict[str, Any],
    fetcher: Callable[[str], dict[str, Any] | None],
    related_depth: int,
    related_max: int,
) -> tuple[list[RelatedNode], list[str]]:
    """主オブジェクトを起点に幅優先で関連オブジェクトを掘る。

    段ごとに frontier を全部消費して次の frontier を作り、 ``related_max``
    を超えた分は打ち切る。 戻り値の ``skipped`` は重複を除く前 ( 出現順 ) 。
    """
    # 主オブジェクトは「 最初から取れた 」 ものとして ``visited`` に入れて
    # おく。 これで BFS 中に主オブジェクトが ``nodes`` に入らず、 describe
    # の再取得も発生しない。
    visited: set[str] = {main_name}
    nodes: list[RelatedNode] = []
    skipped: list[str] = []

    frontier: list[tuple[str, dict[str, Any]]] = [(main_name, main_describe)]
    depth = 0
    while depth < related_depth and frontier:
        depth += 1
        candidates = _collect_candidates(frontier, visited)
        if not candidates:
            break

        candidates, skipped_in_step = _apply_related_max(candidates, nodes, related_max)
        skipped.extend(skipped_in_step)
        if not candidates:
            break

        next_frontier, nodes = _fetch_candidates(candidates, fetcher, visited, depth, nodes)
        if not next_frontier:
            break
        frontier = next_frontier
    return nodes, skipped


def _collect_candidates(
    frontier: list[tuple[str, dict[str, Any]]],
    visited: set[str],
) -> list[tuple[str, str, str, str]]:
    """``frontier`` の各親から、 ``visited`` に無い参照先を集めて並べる。

    形式は ``(親, 項目, リレーション名, 子)`` 。 並び順は親順、 同一親内では
    ``fields`` の出現順 ( ``_related_ref_pairs`` に従う ) 。

    重複ルール: 過去段で ``visited`` 済みの子は再登録しない。 同じ
    (親, 項目, 子) は 1 度だけ。 別親から同じ子を指している場合は別行 ( 複数行可 ) 。
    """
    candidates: list[tuple[str, str, str, str]] = []
    seen_keys: set[tuple[str, str, str]] = set()
    for parent_name, parent_describe in frontier:
        for field_name, relationship_name, ref_name in _related_ref_pairs(parent_describe):
            if ref_name in visited:
                continue
            key = (parent_name, field_name, ref_name)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            candidates.append((parent_name, field_name, relationship_name, ref_name))
    return candidates


def _apply_related_max(
    candidates: list[tuple[str, str, str, str]],
    nodes: list[RelatedNode],
    related_max: int,
) -> tuple[list[tuple[str, str, str, str]], list[str]]:
    """``related_max`` を超える分を ``skipped`` に移し、 candidates を切り詰める。

    既に ``len(nodes) >= related_max`` のときは残り全部を skipped にして
    candidates を空にする ( この段は探索しない ) 。
    """
    remaining = related_max - len(nodes)
    if remaining <= 0:
        skipped = [child for _, _, _, child in candidates]
        return [], skipped
    if len(candidates) > remaining:
        skipped = [child for _, _, _, child in candidates[remaining:]]
        return candidates[:remaining], skipped
    return candidates, []


def _fetch_candidates(
    candidates: list[tuple[str, str, str, str]],
    fetcher: Callable[[str], dict[str, Any] | None],
    visited: set[str],
    depth: int,
    nodes: list[RelatedNode],
) -> tuple[list[tuple[str, dict[str, Any]]], list[RelatedNode]]:
    """``candidates`` の子オブジェクトを ``fetcher`` で取り、 ``nodes`` と次の frontier を作る。

    ``visited`` は取得成功／失敗に関わらず更新する ( 同じ名前は再試行しない ) 。
    同じ子が複数の親候補から重複して ``nodes`` と ``next_frontier`` に追加
    されるのを防ぐ。
    """
    next_frontier: list[tuple[str, dict[str, Any]]] = []
    seen_next: set[str] = set()
    for _, _, _, ref_name in candidates:
        describe = fetcher(ref_name)
        visited.add(ref_name)
        if describe is None:
            # 取れなかった。 ``visited`` は入れておく ( 再試行しない ) 。
            # ただし ``nodes`` にも追加しないし、 ``relations`` にも行を
            # 出さない。
            continue
        if ref_name not in seen_next:
            seen_next.add(ref_name)
            nodes.append(RelatedNode(name=ref_name, depth=depth, describe=describe))
            next_frontier.append((ref_name, describe))
    return next_frontier, nodes


def _collect_relations(
    main_name: str,
    main_describe: dict[str, Any],
    nodes: list[RelatedNode],
) -> list[RelationRecord]:
    """主＋関連オブジェクト同士の参照 ``RelationRecord`` 一覧を作る。

    主→ nodes の順 ( nodes は段順 ) で ``fields`` をたどり、 参照先が「 取れた
    オブジェクト ( 主を含む ) 」 なら 1 行ずつ出す。 同じ ``(親, 項目, 子)``
    は 1 行。 取り損ねた子は親にも子にも行に出ない。
    """
    obtained: set[str] = {main_name} | {n.name for n in nodes}
    depth_by_name: dict[str, int] = {main_name: 0}
    for node in nodes:
        depth_by_name[node.name] = node.depth
    describe_by_name: dict[str, dict[str, Any]] = {main_name: main_describe}
    for node in nodes:
        if node.describe is not None:
            describe_by_name[node.name] = node.describe

    relations: list[RelationRecord] = []
    parent_order: list[str] = [main_name] + [n.name for n in nodes]
    seen_rows: set[tuple[str, str, str]] = set()
    for parent_name in parent_order:
        parent_describe = describe_by_name.get(parent_name)
        if parent_describe is None:
            continue
        for field_name, relationship_name, ref_name in _related_ref_pairs(parent_describe):
            if ref_name not in obtained:
                continue
            key = (parent_name, field_name, ref_name)
            if key in seen_rows:
                continue
            seen_rows.add(key)
            relations.append(
                RelationRecord(
                    parent=parent_name,
                    field=field_name,
                    relationship_name=relationship_name,
                    child=ref_name,
                    depth=depth_by_name.get(ref_name, 0),
                )
            )
    return relations


def _dedup_preserve_order(values: list[str]) -> list[str]:
    """出現順を保ったまま重複を除く。"""
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _related_ref_pairs(describe: dict[str, Any]) -> list[tuple[str, str, str]]:
    """``describe`` の ``fields`` から ``(項目名, リレーション名, 子名)`` の組を集める。

    ``relationshipName`` と ``referenceTo`` がある項目だけ。 ポリモーフィックなら
    ``referenceTo`` のエントリごとに展開（ ``relationshipName`` は同じ ）。
    出現順を保ち、 重複は呼び出し側で ``visited`` により落とす。
    """
    return [
        (field["name"], field["relationshipName"], ref_name)
        for field in describe.get("fields", [])
        if field.get("relationshipName") and field.get("name")
        for ref_name in field.get("referenceTo") or []
    ]


def _make_overrides_fetcher(
    *,
    client: Any,
    org: str,
    store: DescribeStore,
    objects: dict[str, dict[str, Any]],
    csv_used_keys: set[tuple[str, str]],
) -> Callable[[str], dict[str, Any] | None]:
    """列キーのたどりと ``apply_overrides`` に渡す fetcher を作る。

    引数 ``name`` のオブジェクトを取りに行くが、 自前の WARNING は出さない
    （ ``apply_overrides`` が 「 たどれません 」 文に整形して出す ）。 401
    と 403 はそのまま上位へ送出 （ ``apply_overrides`` を経由して
    ``_fetch_one`` の ``except`` で拾われ、 レポート全体が失敗扱いになる ） 。

    取得順序は ``_fetch_object_cached`` と同じ:

    1. ``objects`` に有ればそれを返す （ API を叩かない ）
    2. ``DescribeStore`` （ メモリ → CSV ） を見る
    3. 無ければ API を呼ぶ。 成功時はメモリと CSV に保存

    取れた describe は ``_slim_object`` で整えて ``objects[name]`` に入れる。
    """

    def fetcher(name: str) -> dict[str, Any] | None:
        # 1) 既に取っていればそれを返す
        if name in objects:
            return objects[name]
        # 2) ストアのキャッシュを引く
        cached = store.lookup(org, name)
        if cached is not None:
            describe = cached.get("describe")
            if describe is None:
                # 失敗記録だけのエントリは取りに行かない
                return None
            # CSV から読んだものは取り直し候補に入れる
            if cached.get("source") == "csv":
                csv_used_keys.add((org.lower(), name.strip().lower()))
            objects[name] = _slim_object(describe)
            return objects[name]
        # 3) API 呼び出し
        try:
            describe = client.describe_object(name)
        except SalesforceRequestError as exc:
            if exc.status_code in FATAL_STATUS_CODES:
                # 401 / 403 は上位へ送出してレポート全体を失敗扱いにする
                raise
            # その他: 失敗をメモリにだけ覚えて None を返す。 警告は
            # ``apply_overrides`` 側で 「 たどれません 」 文に変換する
            store.record_api_failure(org, name, f"HTTP {exc.status_code}: {exc.detail}")
            return None
        except ValueError as exc:
            store.record_api_failure(org, name, str(exc))
            return None
        store.record_api_success(org, name, describe)
        objects[name] = _slim_object(describe)
        return objects[name]

    return fetcher


def _fetch_object_cached(
    client: Any,
    org: str,
    name: str,
    store: DescribeStore,
    warnings: list[str],
    csv_used_keys: set[tuple[str, str]] | None = None,
) -> dict[str, Any] | None:
    """オブジェクトの describe をストア（ メモリ → CSV ）か API から取り、 整えて返す。

    戻り値:
        成功時は ``_slim_object`` で整えた describe。 失敗時（ HTTP エラー /
        ``ValueError`` ）は ``None``。 401 / 403 は ``SalesforceRequestError`` を
        そのまま上位へ送出する（ 呼び出し側で ID 全体を失敗扱い ）。 ストアには
        生の describe を書く（ 項目定義.csv の列のため ）。

    ``csv_used_keys`` を渡すと、 CSV から読んだ （ 今回 API で取ったものではない ）
    ``(org, object_lower)`` のキーをそこに追加する。 取り直しの判定に使う。
    """
    cached = store.lookup(org, name)
    if cached is not None:
        describe = cached.get("describe")
        warning = cached.get("warning")
        if warning:
            warnings.append(warning)
        if csv_used_keys is not None and cached.get("source") == "csv":
            csv_used_keys.add((org.lower(), name.strip().lower()))
        return None if describe is None else _slim_object(describe)

    try:
        describe = client.describe_object(name)
    except SalesforceRequestError as exc:
        if exc.status_code in FATAL_STATUS_CODES:
            raise
        warning = f"オブジェクト {name} の describe に失敗（HTTP {exc.status_code}: {exc.detail}）"
        store.record_api_failure(org, name, warning)
        warnings.append(warning)
        return None
    except ValueError as exc:
        warning = f"オブジェクト {name} の describe に失敗: {exc}"
        store.record_api_failure(org, name, warning)
        warnings.append(warning)
        return None

    store.record_api_success(org, name, describe)
    return _slim_object(describe)


def _has_unresolved_columns(
    column_map: list[dict[str, str]],
    object_reason: str | None,
) -> bool:
    """``column_map`` に ``対応フィールドAPI名 == "(不明)"`` の行が **1 つでも**
    あるかを返す。

    ``object_reason`` が ``None`` でないとき （ 主オブジェクト不明・ describe 失敗 ）
    は、そのレポートの主オブジェクトはそもそも API で取れていないので、
    取り直しの対象外とする。
    """
    if object_reason:
        return False
    return any(row.get("対応フィールドAPI名") == "(不明)" for row in column_map)


def _object_key(org: str, object_name: str) -> tuple[str, str]:
    """``(org, object)`` をストレージ用の正規化キーにする （ ``DescribeStore._key``
    と同じ正規化 ）。"""
    return (org.strip().lower(), object_name.strip().lower())


def _api_name_from_store(store: DescribeStore, key: tuple[str, str]) -> str | None:
    """``(org, object_lower)`` に対応する describe がストアにあれば ``name`` を返す。
    取り直しの API 呼び出しで正しい API 名 （ 大文字小文字そのまま ） を渡すために必要。
    """
    entry = store.memory_cache.get(key) or {}
    describe = entry.get("describe") or store.csv_index.get(key) or {}
    return describe.get("name") or None


def _choose_main(
    client: Any,
    org: str,
    slim_report: SlimReport,
    store: DescribeStore,
    warnings: list[str],
) -> tuple[str | None, dict[str, Any] | None, str | None, set[tuple[str, str]]]:
    """``metadata`` から主オブジェクトを決める （ 2 段: レポートタイプ → 列キー先頭 ）。

    2026-10-14 の方針: ``reportType.type`` から候補が取れなかったとき （ カ
    スタムレポートタイプのレポートタイプ自体が主オブジェクトの API 名でな
    いケース ） に備え、 **列キーの先頭** を代替の候補として試す。 次の順
    で ``_fetch_object_cached()`` を呼んで、 最初に describe が取れた候補を
    採用する:

    (a) ``mapping.report_type_candidates(reportType.type)`` の候補
    (b) (a) が全滅したとき、 列キーの先頭 （ ``.`` を含む列キーの最初の
        ``.`` より前 ） を多い順、 同数は最初に出た順で最大
        ``_MAIN_OBJECT_PREFIX_LIMIT`` 個

    (a) で決まれば従来通り、 (b) で決まれば警告を出さずに正常な経路として
    扱う。 どちらもだめなら ``mapping.candidate_failure_reason()`` の形
    で両方のリストを並べた理由を返す。

    ``_fetch_object_cached()`` の 401 / 403 はそのまま上位へ
    ``SalesforceRequestError`` が送出され、 ID 全体が失敗扱いになる（呼び出し
    側で ``try`` している前提）。

    (a) の ``describe_object`` が 404 / ValueError などで失敗したときの
    WARNING は、 (b) が **成功する** なら出さない ( 正常な経路として扱う ) 。
    (a) (b) ともに全滅したときは (a) の WARNING も積む。

    Returns:
        ``(main_object_name | None, main_describe | None, reason | None,
        csv_used_keys)``
        - ``main_object_name``: 採用した主オブジェクト名。 describe が取れ
          なかったときは ``None``
        - ``main_describe``: 主オブジェクトの describe dict。 特定・取得に失敗
          したときは ``None``
        - ``reason``: ``main_describe`` が ``None`` のときの理由（ ``None``
          なら取得成功）
        - ``csv_used_keys``: このレポートで参照されたオブジェクトのうち、
          **CSV から読んだもの** （ 今回 API で取ったものではない ） の
          ``(org, object_lower)`` の集合。 取り直し判定用
    """
    object_name = (slim_report["reportMetadata"].get("reportType") or {}).get("type", "")

    csv_used_keys: set[tuple[str, str]] = set()

    # (a) ``reportType.type`` から候補。 失敗 WARNING は一時バッファに
    # 溜め、 (b) が成功したら捨てる ( 正常な経路扱い ) 。
    candidates_a = mapping.report_type_candidates(object_name)
    a_warnings: list[str] = []
    for candidate in candidates_a:
        describe = _fetch_object_cached(client, org, candidate, store, a_warnings, csv_used_keys)
        if describe is not None:
            return candidate, describe, None, csv_used_keys

    # (b) 列キーの先頭から候補。 こちらは「 正常な経路 」 なので WARNING を
    # 直接 ``warnings`` に積む。 ただし (b) で出た WARNING は (a) が全滅
    # してもそのまま残す。
    candidates_b = _extract_column_key_prefix_candidates(slim_report)
    b_warnings: list[str] = []
    for candidate in candidates_b:
        describe = _fetch_object_cached(client, org, candidate, store, b_warnings, csv_used_keys)
        if describe is not None:
            warnings.extend(b_warnings)
            return candidate, describe, None, csv_used_keys

    # 両方だめ: (a) の WARNING も積んでから理由を組み立てる
    warnings.extend(a_warnings)
    warnings.extend(b_warnings)
    reason = _main_object_failure_reason(object_name, candidates_a, candidates_b)
    return None, None, reason, csv_used_keys


def _extract_column_key_prefix_candidates(
    slim_report: SlimReport,
    *,
    limit: int = _MAIN_OBJECT_PREFIX_LIMIT,
) -> list[str]:
    """``metadata`` の列キーのうち ``.`` を含むものについて最初の ``.`` よ
    り前を集計し、 多い順（ 同数は最初に出た順 ） で ``limit`` 個返す。

    列キーは ``mapping.collect_describable_columns`` が集める全ソース
    （ ``detailColumns`` / ``reportFilters`` / ``groupingsDown`` /
    ``groupingsAcross`` / ``aggregates`` / ``standardDateFilter`` /
    ``sortBy`` ） を使う。 ``.`` を含まない列キーは対象外（ 主オブジェクトと
    同じ名前 1 つのキーや、 ``OPPORTUNITY_NAME`` のような値 ）。

    ``FK_`` で始まる列キーの先頭は主オブジェクトではなく主からの参照先を
    指しているので、 候補からは外す （ 2026-10-14 ） 。

    ``_choose_main`` の (b) 段階で主オブジェクトの候補を組み立てるのに使う。
    """
    columns = mapping.collect_describable_columns(slim_report["reportMetadata"])
    counts: dict[str, int] = {}
    first_seen: dict[str, int] = {}
    for index, column_key in enumerate(columns):
        if "." not in column_key:
            continue
        if column_key.startswith("FK_"):
            # ``FK_`` の先は主オブジェクトではなく主からの参照先
            continue
        prefix = column_key.split(".", 1)[0].strip()
        if not prefix:
            continue
        counts[prefix] = counts.get(prefix, 0) + 1
        if prefix not in first_seen:
            first_seen[prefix] = index
    # 多い順、 同数は最初に出た順
    sorted_prefixes = sorted(
        counts.keys(),
        key=lambda p: (-counts[p], first_seen[p]),
    )
    return sorted_prefixes[:limit]


def _main_object_failure_reason(
    original: str,
    candidates_a: list[str],
    candidates_b: list[str],
) -> str:
    """``_choose_main`` の (a) / (b) が両方全滅したときの理由を組み立てる。

    既存 ``mapping.candidate_failure_reason`` のスタイルを保ちつつ、 両方
    のリストを 「 ``(a)`` / ``(b)`` 」 と分かる形で添える。 ``(a)`` も
    ``(b)`` も無い（ ``reportType.type`` が空で列キーにも ``.`` が無い）
    ときは、 「 主オブジェクト候補を抽出できなかった 」 の旨を返す。
    """
    if candidates_a and candidates_b:
        a_text = ", ".join(repr(c) for c in candidates_a)
        b_text = ", ".join(repr(c) for c in candidates_b)
        return (
            f"レポートタイプ {original!r} の主オブジェクト候補 {a_text} と"
            f" 列キー先頭の候補 {b_text} を順に確認しましたが、"
            "どれも Object Describe できず自動判定できません。手動で確認してください"
        )
    if candidates_a:
        return mapping.candidate_failure_reason(original, candidates_a)
    if candidates_b:
        b_text = ", ".join(repr(c) for c in candidates_b)
        return (
            f"レポートタイプ {original!r} の主オブジェクトは特定できず、"
            f" 列キー先頭の候補 {b_text} を順に確認しましたが、"
            "どれも Object Describe できず自動判定できません。手動で確認してください"
        )
    return (
        f"レポートタイプ {original!r} からは主オブジェクト候補を抽出できず、"
        "列キーにも主オブジェクトの手がかりが無いため自動判定できません。"
        "手動で確認してください"
    )
