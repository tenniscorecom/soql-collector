"""ID ごとに describe を取って、 ID ごとの JSON に書く。

流れ（IDごと）:

1. 管理番号 → 管理表の行（無ければ呼び出し側の ``cli`` でエラー）
2. URL から ``site_for(url)`` で接続先を決め、 ``with site() as client:`` で開く
3. ``metadata = client.report.describe(report_id)`` （レポートの describe）
4. 主オブジェクトの決定: ``reportType.type`` が空なら特定できず。 ``$`` / ``@``
   を含まなければその値、 含めば ``mapping.report_type_candidates()`` の候補を
   ``_fetch_object_cached()`` で順に ``describe_object`` して、 最初に成功した
   ものを主オブジェクトにする。 HTTP エラー（ ``SalesforceRequestError`` ）や
   ``ValueError`` で主オブジェクトが特定できなかった／ describe が取れなかった
   ときは、 列対応表を全列 ``(不明)`` ＋理由の備考に縮退する。 401 / 403 は
   握りつぶさず、 その ID の失敗にする
5. 関連オブジェクト（参照先を ``RELATED_DEPTH`` 段まで）: 主オブジェクトの
   describe の ``fields`` を起点に幅優先で参照を辿り、 ``relationshipName``
   が空でなく ``referenceTo`` が空でない項目を **1 段ずつ** 掘る。 同じ名前は
   浅い段から先に登録される。 件数は ``[LIMITS] RELATED_MAX`` （ 既定 40 ）
   で全段の合計を打ち切り、 超えた名前は ``warnings`` に残す
6. ``mapping.build_column_map(metadata, main_describe, reason)`` で列対応表を
   組み立てる。 主オブジェクトの describe は **ID ごとに 1 回** だけ取得する
   （ ``_fetch_object_cached`` のキャッシュを経由するため、 複数 ID が同じ
   主オブジェクトを参照しても HTTP は 1 回 ）

出力（ ``[FILES] OUTPUT_DIR`` の ``{管理番号}.json`` ）は SOQL 組立に必要な
**構造だけ** を書く:

- レポートの ``reportMetadata`` は ``REPORT_METADATA_KEYS`` にあるキーだけを
  **そのまま** 残す（ SELECT / WHERE / 日付条件 / サブクエリ / GROUP BY /
  集計 / ORDER BY / 件数 / 独自グループ / 独自計算式に必要なもの ）
- レポートの ``reportExtendedMetadata`` は ``detailColumnInfo`` /
  ``groupingColumnInfo`` / ``aggregateColumnInfo`` の 3 キーだけ残す
- オブジェクトは ``name`` / ``label`` / ``custom`` / ``fields`` だけ
- 項目は ``name`` / ``label`` / ``type`` / ``custom`` / ``referenceTo`` /
  ``relationshipName`` / ``picklist`` / ``picklistTotal`` の 8 キーだけ
- ``関連オブジェクト`` は主＋関連の **取れた** オブジェクト同士の親→子の
  参照を **1 行 1 本** で書いたリスト（ ``{親, 参照項目, リレーション名, 子, 段}`` ）。
  段数の上限で打ち切られた子はここに出ない。 ポリモーフィック参照は
  ``referenceTo`` のエントリごとに 1 行ずつ

``describe`` の **原本** は JSON に残さない。 落としたキーの名前は
``report.droppedKeys`` に**出現順で**入れる（ 最初の実行で、 必要なものを
落としていないか確かめる用 ）。 一時ファイル経由で書き込む
（ ``comken.core.files.atomic_write`` ）。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from comken.core.dates import now as comken_now
from comken.core.files import atomic_write
from comken.exceptions import (
    ComkenError,
    SalesforceError,
    SalesforceReportIDNotFoundError,
    SalesforceRequestError,
)
from comken.toolbox.salesforce.report import report_id_from_url

from src import mapping
from src.master import MasterEntry
from src.settings import Settings

logger = logging.getLogger(__name__)

# ``describe_object`` が 401 / 403 を返したとき、 その ID 全体を失敗させる
# ステータスコード（ comken の ``ReportAPI.describe`` と同じく ）。
FATAL_STATUS_CODES: frozenset[int] = frozenset({401, 403})

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
class FetchOutcome:
    """1 ID の取得結果。"""

    entry: MasterEntry
    status: str  # "ok" / "failed" / "dry-run"
    output_path: Path | None
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
    """``entries`` の各エントリについて、describe を取り JSON に書く。

    Args:
        settings: config.ini から読んだ設定。
        entries: 処理対象の管理表エントリ（既に ``--all`` 絞り込み済み）。
        dry_run: True なら接続せず、対象だけ表示用 ``FetchOutcome`` を返す。
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
                    output_path=settings.output_dir / f"{entry.key}.json",
                    warnings=(),
                    error=None,
                )
            )
            continue
        outcomes.append(_fetch_one(settings, entry, site_for=site_for, cache=cache))
    return outcomes


# ── JSON 用の slim ──────────────────────────────────────────────────────────
# JSON には describe の **原本** ではなく、 SOQL を組むのに必要な **構造だけ**
# （ レポートの ``reportMetadata`` / ``reportExtendedMetadata`` と、 オブジェクト
# の ``name`` / ``label`` / ``custom`` / ``fields`` ）を書く。 原本は JSON に
# 残さない。 下の 2 つの slim 関数は、 ``describe_object`` が返した dict（ と
# ``client.report.describe`` が返した dict ）を**書き換えずに**、 別の dict に
# コピーして絞る。 関連オブジェクト収集は ``_collect_related_objects``
# （ slim 前の ``fields[].referenceTo`` / ``relationshipName`` を使う） に
# 任せて、 slim は JSON に書く直前にだけ行う。
# は slim 前の ``fields[].referenceTo`` / ``relationshipName`` を使うので、
# slim は JSON 書く直前にだけ行う。
#
# レポート describe の絞り込みは **allowlist 方式** （ 「残すキーを選ぶ」 ）。
# 落とされたキーの名前は ``droppedKeys`` に**出現順で**入れる。 目的: 最初の
# 実行で本物の返り値を見て 「必要なものを落としていないか」 を確かめられる。
# ``droppedKeys`` のサブキーは 3 つ（ ``reportMetadata`` /
# ``reportExtendedMetadata`` / ``top`` ）常に存在する（ 1 つも落ちていない
# ときも空のリストとして ）。 一方入力に存在しないキーは「落とされた」のでは
# なく「元から無い」ので ``droppedKeys`` には入らない。


def _slim_report(metadata: object) -> dict[str, Any]:
    """``client.report.describe()`` の戻り値から SOQL 組立に必要な構造だけを返す。

    - ``reportMetadata``: ``REPORT_METADATA_KEYS`` にあるキーだけを**そのまま**
      残す。 値は元の参照のまま（ ``slimmed[key] is metadata[key]`` ）
    - ``reportExtendedMetadata``: ``REPORT_EXTENDED_METADATA_KEYS`` の 3 キー
      だけを**そのまま**残す
    - それ以外のトップレベルキー（ ``reportTypeMetadata`` — そのレポートタイプで
      使える全列の一覧で巨大、 ``attributes``、 グラフ・表示・フォルダ・説明など
      の管理用キー）はすべて落とす
    - 落としたキーの名前は ``droppedKeys`` に**出現順で**入れる
    - 入力が dict でない／ ``reportMetadata`` と ``reportExtendedMetadata`` が
      無い／ dict でない場合は、 例外を上げず省略する（ そのキーは droppedKeys
      には入らない ）
    - 入力の dict は書き換えない（ 別 dict にコピー ）
    """
    dropped: dict[str, list[str]] = {
        "reportMetadata": [],
        "reportExtendedMetadata": [],
        "top": [],
    }
    if not isinstance(metadata, dict):
        return {"droppedKeys": dropped}

    result: dict[str, Any] = {}

    report_metadata_raw = metadata.get("reportMetadata")
    if isinstance(report_metadata_raw, dict):
        slimmed_metadata: dict[str, Any] = {}
        for key, value in report_metadata_raw.items():
            if key in REPORT_METADATA_KEYS:
                slimmed_metadata[key] = value
            else:
                dropped["reportMetadata"].append(key)
        result["reportMetadata"] = slimmed_metadata

    report_extended_raw = metadata.get("reportExtendedMetadata")
    if isinstance(report_extended_raw, dict):
        slimmed_extended: dict[str, Any] = {}
        for key, value in report_extended_raw.items():
            if key in REPORT_EXTENDED_METADATA_KEYS:
                slimmed_extended[key] = value
            else:
                dropped["reportExtendedMetadata"].append(key)
        result["reportExtendedMetadata"] = slimmed_extended

    # トップレベル: ``reportMetadata`` と ``reportExtendedMetadata`` 以外を落とす
    for key in metadata:
        if key in ("reportMetadata", "reportExtendedMetadata"):
            continue
        dropped["top"].append(key)

    result["droppedKeys"] = dropped
    return result


def _slim_object(describe: object) -> dict[str, Any]:
    """``client.describe_object()`` の戻り値から、SOQL 組立に必要な構造だけを返す。

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


def _fetch_one(
    settings: Settings,
    entry: MasterEntry,
    *,
    site_for: SiteFor,
    cache: dict[str, dict[str, Any]],
) -> FetchOutcome:
    """1 ID の describe を取り、JSON に書く。失敗したら ``status="failed"`` を返す。"""
    warnings: list[str] = []
    output_path = settings.output_dir / f"{entry.key}.json"

    # URL → レポート ID
    try:
        report_id = report_id_from_url(entry.url)
    except SalesforceReportIDNotFoundError:
        return FetchOutcome(
            entry=entry,
            status="failed",
            output_path=None,
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
            output_path=None,
            warnings=(),
            error=f"Salesforce の接続先を決定できません: {exc}",
        )

    # 接続〜取得
    objects: dict[str, dict[str, Any]] = {}
    main_object_name: str | None = None
    metadata: dict[str, Any] = {}
    column_map: list[dict[str, Any]] = []

    try:
        with site_class() as client:
            try:
                metadata = client.report.describe(report_id)
            except SalesforceRequestError as exc:
                return FetchOutcome(
                    entry=entry,
                    status="failed",
                    output_path=None,
                    warnings=(),
                    error=(
                        f"レポート describe に失敗（HTTP {exc.status_code}）: {report_id}"
                        f"\n{exc.detail}"
                    ),
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
            column_map = mapping.build_column_map(metadata, main_describe, object_reason)

            # 主オブジェクト
            related_nodes: list[RelatedNode] = []
            relations: list[RelationRecord] = []
            if main_object_name and main_describe is not None:
                objects[main_object_name] = main_describe
                # 関連オブジェクトを参照先から幅優先で掘る （ ``RELATED_DEPTH`` 段まで ）
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
    except SalesforceRequestError as exc:
        # 接続失敗・認証失敗・致命 HTTP
        return FetchOutcome(
            entry=entry,
            status="failed",
            output_path=None,
            warnings=(),
            error=(f"Salesforce への接続でエラー（HTTP {exc.status_code}）: {exc.detail}"),
        )
    except SalesforceError as exc:
        return FetchOutcome(
            entry=entry,
            status="failed",
            output_path=None,
            warnings=(),
            error=f"Salesforce エラー: {exc}",
        )

    main_object = main_object_name if main_object_name and main_object_name in objects else None
    if main_object_name and main_object_name not in objects:
        warnings.append(
            f"主オブジェクト {main_object_name} の describe に失敗したため"
            f"関連オブジェクトは取得していません"
        )

    # JSON に書く直前に slim する。 ``metadata`` 自体と ``objects`` は
    # ``_collect_related_objects`` （関連オブジェクトの収集）で
    # 原本の ``fields[].referenceTo`` / ``relationshipName`` を使うので、
    # ここでは別 dict にコピーして絞る（原本の dict は壊さない）。
    payload = {
        "管理番号": entry.key,
        "概要": entry.summary,
        "レポートID": report_id,
        "URL": entry.url,
        "取得日時": comken_now().isoformat(timespec="seconds"),
        "主オブジェクト": main_object,
        "report": _slim_report(metadata),
        "objects": {name: _slim_object(describe) for name, describe in objects.items()},
        "関連オブジェクト": [
            {
                "親": relation.parent,
                "参照項目": relation.field,
                "リレーション名": relation.relationship_name or "",
                "子": relation.child,
                "段": relation.depth,
            }
            for relation in relations
        ],
        "column_map": column_map,
        "warnings": warnings,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with atomic_write(output_path) as tmp:
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        # フォルダ・権限・ディスクの失敗。その ID だけの失敗にして残りは続ける
        return FetchOutcome(
            entry=entry,
            status="failed",
            output_path=None,
            warnings=tuple(warnings),
            error=f"JSON の書き出しに失敗しました: {output_path}\n{exc}",
        )

    return FetchOutcome(
        entry=entry,
        status="ok",
        output_path=output_path,
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
            オブジェクト名のリスト。 警告用。
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
