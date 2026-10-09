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
   浅い段から先に登録される。 件数は ``[LIMITS] RELATED_MAX`` （ 既定 40 ）
   で全段の合計を打ち切り、 超えた名前は ``warnings`` に残す
6. ``mapping.build_column_map(metadata, main_describe, reason, ...)`` で列対応表
   を組み立てる。 主オブジェクトの describe は **ID ごとに 1 回** だけ取得する
   （ ``_fetch_object_cached`` のキャッシュを経由するため、 複数 ID が同じ
   主オブジェクトを参照しても HTTP は 1 回 ）

**出力は CSV だけ**。 ``ReportRecord`` （ frozen dataclass ） に詰めて
``FetchOutcome.record`` で返す。 ``run_tables`` がこの record を受け取って
CSV を組み立てる。 許可リストでレポート describe から落としたキー名は
``_fetch_one`` の ``warnings`` に
「 レポート定義の中で使わなかったキー: ... 」 の形でも 1 行入れる
（ SOQL 組立に必要なものを落としていないか、 最初の本番実行で確かめるため ） 。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any

from comken.exceptions import (
    ComkenError,
    SalesforceError,
    SalesforceReportIDNotFoundError,
    SalesforceReportTruncatedError,
    SalesforceRequestError,
)
from comken.toolbox.salesforce.report import report_id_from_url

from src import mapping, pii
from src.master import MasterEntry
from src.settings import Settings

logger = logging.getLogger(__name__)

# ``describe_object`` が 401 / 403 を返したとき、 その ID 全体を失敗させる
# ステータスコード（ comken の ``ReportAPI.describe`` と同じく ）。
FATAL_STATUS_CODES: frozenset[int] = frozenset({401, 403})

# ``client.report.get()`` の戻り値 ``Table`` の最小インターフェース （ ``len()`` だけ ）。
# テストで偽の ``Table`` を入れるために ``Any`` を扱う。
_TableLike = Any

# レポート access denied エラー（ 401 / 403 由来 ） のメッセージ接頭辞
_REPORT_ACCESS_DENIED_PREFIX = "Salesforce のレポート API（Analytics API）へのアクセスが"

# レポート形式エラー（ 集計 / マトリックス ） の文言マーカー
_REPORT_FORMAT_MARKERS = ("このレポートは", "形式です")

# 理由欄に出す例外メッセージの最大長（ 行データや機微情報を漏らさない ）
_ERROR_REASON_MAX_LENGTH = 200

# レポート row check の状態定数
ROW_CHECK_OVER = "超えている"
ROW_CHECK_UNDER = "超えていない"
ROW_CHECK_UNKNOWN = "判定不可"
ROW_CHECK_DISABLED = "未実施"

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

    ``JSON: 関連オブジェクト`` と ``tables: 関連表.csv`` の 1 行に対応する。
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
    - ``未実施`` （ ``[CHECKS] ROW_LIMIT = ×`` で実行しなかった ）

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


@dataclass(frozen=True)
class FetchOutcome:
    """1 ID の取得結果。"""

    entry: MasterEntry
    status: str  # "ok" / "failed" / "dry-run"
    record: ReportRecord | None  # status="ok" のときだけ入る
    warnings: tuple[str, ...]
    error: str | None


def run_fetch(
    settings: Settings,
    entries: list[MasterEntry],
    *,
    dry_run: bool = False,
    site_for: SiteFor | None = None,
    object_cache: dict[str, dict[str, Any]] | None = None,
) -> list[FetchOutcome]:
    """``entries`` の各エントリについて、 describe を取り ``ReportRecord`` を返す。

    **JSON を書かない**。 結果は ``outcomes[i].record`` で取り出して、
    そのまま ``run_tables`` に渡す。

    Args:
        settings: config.ini から読んだ設定。
        entries: 処理対象の管理表エントリ（ 既に ``--all`` 絞り込み済み ）。
        dry_run: True なら接続せず、 対象だけ表示用 ``FetchOutcome`` を返す。
        site_for: URL → Salesforce サイトクラスの関数。テスト用差し替え。
        object_cache: ``describe_object`` の結果共有辞書。``None`` なら内部で作る。
            同じ実行内で個別オブジェクトの取得 (``run_objects``) と共有するための
            入口。テストでも同じ名前に 2 回 HTTP を打たないことを検証する。

    Returns:
        入力と同じ順の ``FetchOutcome`` リスト。失敗 ID を含む個別では continue する。
    """
    if site_for is None:
        from comken.toolbox.salesforce.sites import site_for as default_site_for

        site_for = default_site_for

    assert site_for is not None  # 上の分岐で必ず代入される

    # 同じ実行内で重複する describe_object を 1 回にまとめる（User / Account 等）
    cache = object_cache if object_cache is not None else {}

    outcomes: list[FetchOutcome] = []
    for entry in entries:
        if dry_run:
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
        outcomes.append(_fetch_one(settings, entry, site_for=site_for, cache=cache))
    return outcomes


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


def _slim_report(metadata: object) -> dict[str, Any]:
    """``client.report.describe()`` の戻り値から SOQL 組立に必要な構造だけを返す。

    - ``reportMetadata``: ``REPORT_METADATA_KEYS`` にあるキーだけを**そのまま**
      残す。 値は元の参照のまま（ ``slimmed[key] is metadata[key]`` ）
    - ``reportExtendedMetadata``: ``REPORT_EXTENDED_METADATA_KEYS`` の 3 キー
      だけを**そのまま**残す
    - それ以外のトップレベルキー（ ``reportTypeMetadata`` — そのレポートタイプで
      使える全列の一覧で巨大、 ``attributes``、 グラフ・表示・フォルダ・説明など
      の管理用キー）はすべて落とす
    - 入力が dict でない／ ``reportMetadata`` と ``reportExtendedMetadata`` が
      無い／ dict でない場合は、 例外を上げず省略する
    - 入力の dict は書き換えない（ 別 dict にコピー ）
    - 戻り値には **落としたキー名は含めない** （ 別関数
      ``_collect_dropped_keys()`` で取り出し、 warnings に変換する ）
    """
    if not isinstance(metadata, dict):
        return {}

    result: dict[str, Any] = {}

    report_metadata_raw = metadata.get("reportMetadata")
    if isinstance(report_metadata_raw, dict):
        slimmed_metadata: dict[str, Any] = {}
        for key, value in report_metadata_raw.items():
            if key in REPORT_METADATA_KEYS:
                slimmed_metadata[key] = value
        result["reportMetadata"] = slimmed_metadata

    report_extended_raw = metadata.get("reportExtendedMetadata")
    if isinstance(report_extended_raw, dict):
        slimmed_extended: dict[str, Any] = {}
        for key, value in report_extended_raw.items():
            if key in REPORT_EXTENDED_METADATA_KEYS:
                slimmed_extended[key] = value
        result["reportExtendedMetadata"] = slimmed_extended

    return result


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


def _slim_object(describe: object) -> dict[str, Any]:
    """``client.describe_object()`` の戻り値から、 SOQL 組立に必要な構造だけを返す。

    - 残す: ``name`` / ``label`` / ``custom`` / ``fields``
    - 落とす: ``childRelationships`` / ``recordTypeInfos`` / ``urls`` /
      ``supportedScopes`` など
    - ``fields`` の各項目は次の8キーだけ（**この順**）:
      ``name`` / ``label`` / ``type`` / ``custom`` / ``referenceTo`` /
      ``relationshipName`` / ``picklist`` / ``picklistTotal``
    - ``picklist``: 元の ``picklistValues`` のうち ``active`` が真の ``value``
      を最大 30 件。 ``picklistTotal``: active な選択肢の総数（30 を超えたかが
      分かる）。 選択肢が無ければ ``[]`` と ``0``
    - 入力の dict は書き換えない（別 dict にコピー）
    """
    if not isinstance(describe, dict):
        return {}
    name_raw = describe.get("name")
    label_raw = describe.get("label")
    custom_raw = describe.get("custom")
    fields_raw = describe.get("fields")

    fields_list: list[Any] = fields_raw if isinstance(fields_raw, list) else []
    slimmed_fields: list[dict[str, Any]] = [
        _slim_field(field) for field in fields_list if isinstance(field, dict)
    ]

    return {
        "name": name_raw if isinstance(name_raw, str) else "",
        "label": label_raw if isinstance(label_raw, str) else "",
        "custom": custom_raw if isinstance(custom_raw, bool) else False,
        "fields": slimmed_fields,
    }


def _slim_field(field: dict[str, Any]) -> dict[str, Any]:
    """``fields`` の各項目を8キーに絞る。キーの**順番**もそのまま返す。"""
    picklist, picklist_total = _extract_picklist(field.get("picklistValues"))

    reference_to_raw = field.get("referenceTo")
    reference_to: list[Any] = reference_to_raw if isinstance(reference_to_raw, list) else []

    relationship_name_raw = field.get("relationshipName")
    relationship_name: str | None = (
        relationship_name_raw if isinstance(relationship_name_raw, str) else None
    )

    custom_raw = field.get("custom")
    custom = custom_raw if isinstance(custom_raw, bool) else False

    return {
        "name": field.get("name", ""),
        "label": field.get("label", ""),
        "type": field.get("type", ""),
        "custom": custom,
        "referenceTo": reference_to,
        "relationshipName": relationship_name,
        "picklist": picklist,
        "picklistTotal": picklist_total,
    }


def _extract_picklist(picklist_values: object) -> tuple[list[str], int]:
    """``picklistValues`` から ``active`` が真の ``value`` を取り出す。

    Returns:
        (最大 30 件の値リスト, active な選択肢の総数)
    """
    if not isinstance(picklist_values, list):
        return [], 0
    active: list[str] = []
    for entry in picklist_values:
        if not isinstance(entry, dict):
            continue
        if not entry.get("active"):
            continue
        value = entry.get("value")
        if isinstance(value, str) and value:
            active.append(value)
    return active[:30], len(active)


def _dropped_keys_warning_text(dropped: tuple[str, ...]) -> str | None:
    """``_collect_dropped_keys()`` の戻り値から警告表用の 1 行を組み立てる。

    何も落ちていないときは ``None`` を返す。 出力形式:

        レポート定義の中で使わなかったキー: k1, k2, k3
    """
    if not dropped:
        return None
    return "レポート定義の中で使わなかったキー: " + ", ".join(dropped)


def _check_row_count(client: Any, report_id: str, metadata: dict[str, Any]) -> RowCheck:
    """``client.report.get()`` を呼んで 2000 行チェックを行う。

    - ``SalesforceReportTruncatedError`` → ``超えている`` （ ``rows=None`` ）
    - 正常終了 → ``超えていない`` （ ``rows=len(table)`` ）
    - 集計 / マトリックス形式（ describe で TABULAR 以外） → ``判定不可`` で
      「集計/マトリックス形式のため判定できません」
    - 401 / 403 由来の ``SalesforceError`` は呼び出し元に再送出（ レポート全体を
      失敗扱いにする ）
    - その他の例外 → ``判定不可`` で 例外名＋短い説明
    """
    format_value = _report_format(metadata)
    if format_value and format_value != "TABULAR":
        return RowCheck(
            status=ROW_CHECK_UNKNOWN,
            rows=None,
            reason="集計/マトリックス形式のため判定できません",
        )

    try:
        table = client.report.get(report_id)
    except SalesforceReportTruncatedError:
        # 2000 行超。 切り捨て検知は正常パスの一部なので reason は None。
        return RowCheck(status=ROW_CHECK_OVER, rows=None, reason=None)
    except SalesforceError as exc:
        if _is_report_access_denied(exc):
            # 401 / 403: レポート全体を失敗扱いにするために再送出
            raise
        if _is_report_format_error(exc):
            return RowCheck(
                status=ROW_CHECK_UNKNOWN,
                rows=None,
                reason="集計/マトリックス形式のため判定できません",
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
        rows=_safe_row_count(table),
        reason=None,
    )


def _safe_row_count(table: Any) -> int:
    """``Table`` 互換オブジェクトの行数を取り出す。 ``len()`` を持たない偽物に備える。"""
    try:
        return len(table)
    except TypeError:
        return 0


def _report_format(metadata: dict[str, Any]) -> str:
    """``metadata`` から ``reportFormat`` を取り出す。 取得できなければ空文字。"""
    if not isinstance(metadata, dict):
        return ""
    report_metadata = metadata.get("reportMetadata")
    if not isinstance(report_metadata, dict):
        return ""
    value = report_metadata.get("reportFormat")
    return value if isinstance(value, str) else ""


def _is_report_access_denied(exc: SalesforceError) -> bool:
    """comken の ``_report_access_denied_error`` か（ 401 / 403 ） を判定する。"""
    message = str(exc)
    return _REPORT_ACCESS_DENIED_PREFIX in message and ("401" in message or "403" in message)


def _is_report_format_error(exc: SalesforceError) -> bool:
    """comken の ``_report_format_error`` （ 集計 / マトリックス ） を判定する。"""
    message = str(exc)
    return all(marker in message for marker in _REPORT_FORMAT_MARKERS)


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
    slim_report: dict[str, Any],
    column_map: list[dict[str, str]],
    slim_objects: dict[str, dict[str, Any]],
    config: pii.PIIConfig,
) -> pii.ReportPIIResult:
    """``ReportRecord`` 用の PII 評価を行う。 出力列のみ対象。"""
    refs = _collect_output_column_refs(slim_report, column_map)
    return pii.evaluate_report_pii(refs, config, slim_objects)


def _collect_output_column_refs(
    slim_report: dict[str, Any],
    column_map: list[dict[str, str]],
) -> list[pii.ColumnRef]:
    """レポートの出力列（ detailColumns / groupingsDown / groupingsAcross /
    集計対象 ） について PII 評価の入力 ``ColumnRef`` を作る。

    絞り込みにだけ使う列は対象外。 ``column_map`` が見つからない列は未解決として
    扱う。
    """
    output_keys = _output_column_keys(slim_report)
    refs: list[pii.ColumnRef] = []
    column_map_by_key: dict[str, dict[str, str]] = {}
    for col in column_map:
        if not isinstance(col, dict):
            continue
        key = col.get("列キー")
        if isinstance(key, str) and key:
            column_map_by_key[key] = col

    seen: set[str] = set()
    for column_key in output_keys:
        if column_key in seen:
            continue
        seen.add(column_key)
        col = column_map_by_key.get(column_key, {})
        if not isinstance(col, dict):
            col = {}
        label = _coerce_str(col.get("表示名")) or column_key
        field_api = _coerce_str(col.get("対応フィールドAPI名"))
        field_type = _coerce_str(col.get("型"))
        owner_object = _coerce_str(col.get("所属オブジェクト"))
        field_name = _field_name_from_soql(field_api)
        resolved = bool(field_api) and field_api != "(不明)"
        refs.append(
            pii.ColumnRef(
                column_key=column_key,
                label=label,
                field_name=field_name,
                field_type=field_type,
                owner_object=owner_object,
                resolved=resolved,
            )
        )
    return refs


def _output_column_keys(slim_report: dict[str, Any]) -> list[str]:
    """``slim_report`` の出力列キーを順序付きで集める。

    並び順: ``detailColumns`` → ``groupingsDown`` の ``name`` →
    ``groupingsAcross`` の ``name`` → ``aggregates`` の対象列
    （ ``"!"`` の後ろ ）。
    """
    metadata = slim_report.get("reportMetadata") if isinstance(slim_report, dict) else None
    if not isinstance(metadata, dict):
        return []
    keys: list[str] = []
    for column in metadata.get("detailColumns") or []:
        if isinstance(column, str) and column:
            keys.append(column)
    for grouping_key in ("groupingsDown", "groupingsAcross"):
        for grouping in metadata.get(grouping_key) or []:
            if isinstance(grouping, dict):
                name = grouping.get("name")
                if isinstance(name, str) and name:
                    keys.append(name)
    for aggregate in metadata.get("aggregates") or []:
        if not isinstance(aggregate, str) or "!" not in aggregate:
            continue
        column = aggregate.split("!", 1)[-1]
        if column:
            keys.append(column)
    return keys


def _field_name_from_soql(soql_name: str) -> str:
    """``"Account.Owner.Name"`` から末端の ``"Name"`` を取り出す。"""
    if not soql_name or soql_name == "(不明)":
        return ""
    if "." in soql_name:
        return soql_name.rsplit(".", 1)[-1]
    return soql_name


def _coerce_str(value: object) -> str:
    if value is None:
        return ""
    return str(value)


def _fetch_one(
    settings: Settings,
    entry: MasterEntry,
    *,
    site_for: SiteFor,
    cache: dict[str, dict[str, Any]],
) -> FetchOutcome:
    """1 ID の describe を取り、 ``ReportRecord`` を組み立てて返す。

    JSON は書かない。 失敗したら ``status="failed"`` を返し ``record`` は ``None`` 。
    """
    warnings: list[str] = []

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
    objects: dict[str, dict[str, Any]] = {}
    main_object_name: str | None = None
    metadata: dict[str, Any] = {}
    column_map: list[dict[str, Any]] = []

    related_describes: dict[str, dict[str, Any]] = {}

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

            # 2000 行チェック: ``[CHECKS] ROW_LIMIT`` が ○ のときだけ実行。
            # 401 / 403 のような致命エラーは外側の except に伝播させて ID 全体を
            # 失敗扱いにする。 その他のエラーは ``RowCheck`` に縮退する。
            row_check: RowCheck | None = (
                _check_row_count(client, report_id, metadata) if settings.row_limit else None
            )

            # 主オブジェクトの解決（ ``$`` / ``@`` を含むときは候補を
            # ``_fetch_object_cached()`` で順に試す）。 ``describe_object`` が
            # 401 / 403 を返したら ``_fetch_object_cached`` がそのまま上位へ
            # ``SalesforceRequestError`` を送出し、 ID 全体を失敗扱いにする。
            main_object_name, main_describe, object_reason = _choose_main(
                client, metadata, cache, warnings
            )
            if object_reason:
                warnings.append(object_reason)

            # 列対応表: 主オブジェクトで 1 つも決まらないとき用に、 関連
            # オブジェクトの describe と relations を渡して解決範囲を広げる。
            column_map = mapping.build_column_map(
                metadata,
                main_describe,
                object_reason,
                related_describes=related_describes,
                relations=[],
                main_object=main_object_name,
            )

            # 主オブジェクト
            related_nodes: list[RelatedNode] = []
            relations: list[RelationRecord] = []
            if main_object_name and main_describe is not None:
                objects[main_object_name] = main_describe
                # 関連オブジェクトを参照先から幅優先で掘る（ ``RELATED_DEPTH`` 段まで ）
                related_nodes, relations, skipped_names = _collect_related_objects(
                    main_describe,
                    fetcher=lambda name: _fetch_object_cached(client, name, cache, warnings),
                    related_depth=settings.related_depth,
                    related_max=settings.related_max,
                )
                if skipped_names:
                    collected_count = len(related_nodes)
                    total = collected_count + len(skipped_names)
                    warnings.append(
                        f"関連オブジェクトが {total} 件あり"
                        f"上限 {settings.related_max} を超えたため、"
                        f"次の {len(skipped_names)} 件は取得しません: {', '.join(skipped_names)}"
                    )
                for node in related_nodes:
                    if node.describe is not None:
                        objects[node.name] = node.describe
                        related_describes[node.name] = node.describe

                # 列対応表を取り直す（ 関連オブジェクトが取れてから解く ）
                column_map = mapping.build_column_map(
                    metadata,
                    main_describe,
                    object_reason,
                    related_describes=related_describes,
                    relations=relations,
                    main_object=main_object_name,
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

    # record に詰める直前に slim する。 ``metadata`` 自体と ``objects`` は
    # ``_collect_related_objects`` （ 関連オブジェクトの収集 ） で
    # 原本の ``fields[].referenceTo`` / ``relationshipName`` を使うので、
    # ここでは別 dict にコピーして絞る（ 原本の dict は壊さない ）。
    slim_report = _slim_report(metadata)
    slim_objects: dict[str, dict[str, Any]] = {
        name: _slim_object(describe) for name, describe in objects.items()
    }

    dropped_keys = _collect_dropped_keys(metadata)
    dropped_keys_warning = _dropped_keys_warning_text(dropped_keys)
    if dropped_keys_warning:
        warnings.append(dropped_keys_warning)

    # PII 評価: 出力列だけを対象にする。 絞り込みにだけ使う列は対象外。
    pii_config = pii.PIIConfig(
        keywords=settings.pii_keywords,
        person_objects=settings.pii_person_objects,
    )
    pii_result = _evaluate_record_pii(slim_report, column_map, slim_objects, pii_config)

    # 2000 件超判定が 判定不可 なら、 警告表にも 1 行入れる
    if row_check is not None and row_check.status == ROW_CHECK_UNKNOWN and row_check.reason:
        warnings.append(f"2000件超の判定ができませんでした: {row_check.reason}")

    record = ReportRecord(
        key=entry.key,
        summary=entry.summary,
        report_id=report_id,
        url=entry.url,
        main_object=main_object,
        report=slim_report,
        objects=slim_objects,
        relations=tuple(relations),
        column_map=column_map,
        warnings=tuple(warnings),
        row_check=row_check,
        pii=pii_result,
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
    main_name_raw = main_describe.get("name") if isinstance(main_describe, dict) else None
    main_name = main_name_raw if isinstance(main_name_raw, str) and main_name_raw else None

    nodes: list[RelatedNode] = []
    relations: list[RelationRecord] = []
    skipped: list[str] = []

    if main_name is None or related_depth < 1:
        return nodes, relations, skipped

    # 主オブジェクトは「 最初から取れた 」 ものとして ``visited`` に入れて
    # おく。 これで BFS 中に主オブジェクトが ``nodes`` に入らず、 describe
    # の再取得も発生しない。 さらに「 主への参照 」 （ 例: Account.TaskId
    # → Task ） や自己参照 （ 例: Account.ParentId → Account ） も、
    # 候補としては作らない代わりに、 BFS 後の relations 生成で主を含めて
    # 拾う。
    visited: set[str] = {main_name}

    # frontier は (親オブジェクト名, describe) のリスト。 1 段目では主
    # オブジェクト自身を根とする。 BFS の各段で frontier を全部消費して
    # 次の frontier を作る 1 本のループ。
    frontier: list[tuple[str, dict[str, Any]]] = [(main_name, main_describe)]
    depth = 0
    while depth < related_depth and frontier:
        depth += 1
        # 1) 候補を frontier の親順で集める（同段内で親の出現順を保つ）。
        #    重複ルール:
        #      - 過去段で ``visited`` 済みの子は再登録しない （ 浅い親優先 ）
        #      - 同じ (親, 項目, 子) は 1 度だけ
        #      - 同じ親から別項目で同じ子を指している ⇒ 別行
        #      - 別親から同じ子を指している ⇒ 別行 （ 同じ段で複数行可 ）
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
        if not candidates:
            break

        # 2) 上限で打ち切り: candidates の順序で related_max まで採用、 残りは skipped
        remaining = related_max - len(nodes)
        if remaining <= 0:
            # 既に上限に達している。 残りは全部 skipped にする
            skipped.extend(child for _, _, _, child in candidates)
            break
        if len(candidates) > remaining:
            skipped.extend(child for _, _, _, child in candidates[remaining:])
            candidates = candidates[:remaining]

        # 3) describe を取って ``nodes`` を作る。 ``visited`` は取得成功／失敗
        #    に関わらず更新する（ 同じ名前は再試行しない ）。
        next_frontier: list[tuple[str, dict[str, Any]]] = []
        seen_next: set[str] = set()
        for _, _, _, ref_name in candidates:
            describe = fetcher(ref_name)
            visited.add(ref_name)
            if describe is None:
                # 取れなかった。 ``visited`` は入れておく （ 再試行しない ） 。
                # ただし ``nodes`` にも追加しないし、 ``relations`` にも行を
                # 出さない。
                continue
            # 同じ子が複数の親候補から重複して ``nodes`` と ``next_frontier`` に
            # 追加されるのを防ぐ （ 1 段目で複数の (親, 項目) から同じ子を
            # 指しているとき ） 。
            if ref_name not in seen_next:
                seen_next.add(ref_name)
                nodes.append(RelatedNode(name=ref_name, depth=depth, describe=describe))
                next_frontier.append((ref_name, describe))

        if not next_frontier:
            break
        frontier = next_frontier

    # skipped の重複を除く （ 出現順を保つ ）
    skipped = _dedup_preserve_order(skipped)

    # relations: BFS が終わったあとに、 「 取れたオブジェクト全部 （ 主 +
    # nodes ） の fields 」 を主→ nodes の順 （ nodes は段順 ） でたどって
    # 作る。 参照先が「 取れたオブジェクト （ 主を含む ）」 なら 1 行ずつ
    # 出す。 行の ``段`` は **子の段** （ 主 = 0 ）。 並びは「 浅い親
    # （ 主→段順 ） → 同じ親内では項目の出現順 」 。 ポリモーフィック参照は
    # ``referenceTo`` のエントリごとに 1 行。 同じ ``(親, 項目, 子)`` は
    # 1 行。 取り損ねた子 （ 上限で打ち切られた子・ describe に失敗した子 ）
    # は親にも子にも行に出ない。
    obtained: set[str] = {main_name} | {n.name for n in nodes}
    depth_by_name: dict[str, int] = {main_name: 0}
    for node in nodes:
        depth_by_name[node.name] = node.depth
    describe_by_name: dict[str, dict[str, Any]] = {main_name: main_describe}
    for node in nodes:
        if node.describe is not None:
            describe_by_name[node.name] = node.describe

    parent_order: list[str] = [main_name] + [n.name for n in nodes]
    seen_rows: set[tuple[str, str, str]] = set()
    for parent_name in parent_order:
        parent_describe = describe_by_name.get(parent_name)
        if not isinstance(parent_describe, dict):
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

    return nodes, relations, skipped


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

    ``relationshipName`` が空でなく ``referenceTo`` が空でない項目だけ。
    ポリモーフィックなら ``referenceTo`` のエントリごとに展開
    （ ``relationshipName`` は同じ ）。 出現順を保ち、 重複は呼び出し側で
    ``visited`` により落とす。 戻り値の要素はすべて ``str`` （ 長さが 0 の
    ものは呼び出し側で既に弾いている ）。
    """
    fields = describe.get("fields") if isinstance(describe, dict) else None
    if not isinstance(fields, list):
        return []
    triples: list[tuple[str, str, str]] = []
    for field in fields:
        if not isinstance(field, dict):
            continue
        relationship_name = field.get("relationshipName")
        field_name = field.get("name")
        if (
            not isinstance(relationship_name, str)
            or not relationship_name
            or not isinstance(field_name, str)
            or not field_name
        ):
            continue
        reference_to = field.get("referenceTo")
        if not isinstance(reference_to, list) or not reference_to:
            continue
        for ref_name in reference_to:
            if not isinstance(ref_name, str) or not ref_name:
                continue
            triples.append((field_name, relationship_name, ref_name))
    return triples


def _fetch_object_cached(
    client: Any,
    name: str,
    cache: dict[str, dict[str, Any]],
    warnings: list[str],
) -> dict[str, Any] | None:
    """``client.describe_object(name)`` を呼び、結果をキャッシュする。

    戻り値:
        成功時は describe の dict。失敗時（HTTP エラー / ``ValueError``）は
        ``None``。401 / 403 は ``SalesforceRequestError`` をそのまま上位へ
        送出する（呼び出し側で ID 全体を失敗扱い）。
    """
    if name in cache:
        cached = cache[name]
        describe = cached.get("describe")
        warning = cached.get("warning")
        if warning:
            warnings.append(warning)
        return describe if isinstance(describe, dict) else None

    try:
        describe = client.describe_object(name)
    except SalesforceRequestError as exc:
        if exc.status_code in FATAL_STATUS_CODES:
            raise
        warning = f"オブジェクト {name} の describe に失敗（HTTP {exc.status_code}: {exc.detail}）"
        cache[name] = {"describe": None, "warning": warning}
        warnings.append(warning)
        return None
    except ValueError as exc:
        warning = f"オブジェクト {name} の describe に失敗: {exc}"
        cache[name] = {"describe": None, "warning": warning}
        warnings.append(warning)
        return None

    if not isinstance(describe, dict):
        # 念のため空 dict と同じ扱いにする
        describe = {}

    cache[name] = {"describe": describe, "warning": None}
    return describe


def _choose_main(
    client: Any,
    metadata: dict[str, Any],
    cache: dict[str, dict[str, Any]],
    warnings: list[str],
) -> tuple[str | None, dict[str, Any] | None, str | None]:
    """``metadata`` の ``reportType.type`` から主オブジェクトを決める。

    ``report_type_candidates()`` で抽出した候補を先頭から
    ``_fetch_object_cached()`` で試し、 最初に describe が取れた候補を
    採用する。 全滅したとき・候補が無いときは ``candidate_failure_reason()``
    の理由を返す（ ``reportType.type`` が空のときは「候補を抽出できなかった」
    になる）。

    ``_fetch_object_cached()`` の 401 / 403 はそのまま上位へ
    ``SalesforceRequestError`` が送出され、 ID 全体が失敗扱いになる（呼び出し
    側で ``try`` している前提）。

    Returns:
        ``(main_object_name | None, main_describe | None, reason | None)``
        - ``main_object_name``: 採用した主オブジェクト名。 describe が取れ
          なかったときは ``None``
        - ``main_describe``: 主オブジェクトの describe dict。 特定・取得に失敗
          したときは ``None``
        - ``reason``: ``main_describe`` が ``None`` のときの理由（ ``None``
          なら取得成功）
    """
    report_metadata = metadata.get("reportMetadata", {}) if isinstance(metadata, dict) else {}
    object_name = report_metadata.get("reportType", {}).get("type", "")
    if not isinstance(object_name, str):
        object_name = ""

    candidates = mapping.report_type_candidates(object_name)
    for candidate in candidates:
        describe = _fetch_object_cached(client, candidate, cache, warnings)
        if describe is not None:
            return candidate, describe, None

    return None, None, mapping.candidate_failure_reason(object_name, candidates)
