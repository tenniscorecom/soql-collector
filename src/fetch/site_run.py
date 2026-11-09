"""1 レポート分の取得・列対応表の組み立て（接続先に入ったあとの一連の処理）。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any

from comken.exceptions import SalesforceRequestError

from src import child_relationship_csv, column_fix_xlsx, mapping
from src.describe_store import DescribeStore
from src.fetch.main_object import choose_main
from src.fetch.models import RelatedNode, RelationRecord, RowCheck
from src.fetch.object_fetch import (
    api_name_from_store,
    fetch_object_cached,
    make_overrides_fetcher,
    object_key,
)
from src.fetch.related import collect_related_objects, log_related_objects
from src.fetch.report_slim import (
    SlimReport,
    collect_dropped_keys,
    make_slim_object,
    make_slim_report,
)
from src.fetch.row_check import check_row_count, log_row_check
from src.mapping.child_base import apply_child_base
from src.master import MasterEntry
from src.report_store import ReportStore
from src.row_check_status import ROW_CHECK_UNDER
from src.soql_builder import find_child_object_name
from src.text_utils import coerce_str as _coerce_str

logger = logging.getLogger(__name__)


@dataclass
class FetchState:
    """``_fetch_one`` の ``with site_class()`` 内で共有する作業用状態。

    dataclass は「入力が複雑」 とは言えないが、 多くの変数を 1 つの関数に
    持ち回さずに済むようにするためだけに使っている。 dataclass のフィールドは
    ``_fetch_one`` 内 (関数引数として) でのみ触る。
    """

    objects: dict[str, dict[str, Any]] = dc_field(default_factory=dict)
    main_object_name: str | None = None
    main_describe: dict[str, Any] | None = None
    object_reason: str | None = None
    csv_used_keys: set[tuple[str, str]] = dc_field(default_factory=set)
    column_map: list[dict[str, Any]] = dc_field(default_factory=list)
    related_describes: dict[str, dict[str, Any]] = dc_field(default_factory=dict)
    related_nodes: list[RelatedNode] = dc_field(default_factory=list)
    relations: list[RelationRecord] = dc_field(default_factory=list)
    row_check: RowCheck | None = None
    slim_report: SlimReport = dc_field(default_factory=lambda: make_slim_report(None))
    dropped_keys: tuple[str, ...] = ()
    warnings: list[str] = dc_field(default_factory=list)
    #: クロスフィルタの副問い合わせに使う子オブジェクトの describe。
    #: ``objects`` には入れない（関係図・関連表・対応表の出力を変えないため）。
    cross_filter_objects: dict[str, dict[str, Any]] = dc_field(default_factory=dict)
    #: SOQL の FROM に使うオブジェクト名。 子から取る形にしたときだけ入る
    soql_from_object: str | None = None
    #: 子から取る形にしたときの、 子から親へ戻るリレーション名
    soql_from_relationship: str = ""


# レポートが見つからないとき（HTTP 404）に、 警告へ足す説明
_NOT_FOUND_HINT = (
    "\nこのレポート ID は、 つないだ組織に無いようです。 サンドボックスで調査している場合、"
    " サンドボックスの更新（リフレッシュ）より後に本番で作ったレポートは見つかりません"
)


def run_within_site(
    *,
    client: Any,
    entry: MasterEntry,
    org: str,
    report_id: str,
    store: DescribeStore,
    report_store: ReportStore,
    row_limit: bool,
    refresh_set: set[tuple[str, str]],
    related_depth: int,
    related_max: int,
    column_fix_overrides: dict[tuple[str, str], Any],
    state: FetchState,
    allow_refresh: bool = True,
    manual_child_relationships: tuple[child_relationship_csv.ManualChildRelationship, ...] = (),
) -> None:
    """``with site_class() as client:`` の中で行う一連の取得・組み立てを行う。

    ``allow_refresh`` が ``False`` のときは、 取り直し （API で describe を
    取り直す） をしない。 保存データだけで作り直す（``--rebuild``） ときは
    API を呼ばないので、 取り直しもしない。

    ``manual_child_relationships`` は人が ``input/子リレーション.csv`` に
    手入力した子リレーション。 主オブジェクトの子リレーションに足してから
    クロスフィルタの判定と FROM の切り替えに使う。
    """
    # 境界: レポート定義はここで 1 回だけ整え、 以後は整えた値だけを使う。
    # 貯めてあればそれを使い（期限は無い。 作り直すときは ``レポート定義/`` を消す）、
    # 無ければ API で取って貯める
    cached = report_store.get(org, report_id)
    if cached is not None:
        state.slim_report = cached.slim_report
        state.dropped_keys = cached.dropped_keys
        cached_rows = cached.rows
    else:
        try:
            metadata = client.report.describe(report_id)
        except SalesforceRequestError as exc:
            state.warnings.append(
                f"レポート describe に失敗（HTTP {exc.status_code}）: {report_id}\n{exc.detail}"
                + _NOT_FOUND_HINT * (exc.status_code == 404)
            )
            return
        state.slim_report = make_slim_report(metadata)
        state.dropped_keys = collect_dropped_keys(metadata)
        report_store.put(org, report_id, state.slim_report, state.dropped_keys)
        cached_rows = None

    # 2000 行チェック: ``row_limit`` が True のときだけ実行。
    # チェックしないとき（ ``--rebuild`` ）は、 保存した件数から同じ判定を作る
    if row_limit:
        state.row_check = check_row_count(client, report_id)
    elif cached_rows is not None:
        state.row_check = RowCheck(status=ROW_CHECK_UNDER, rows=cached_rows, reason=None)
    else:
        state.row_check = None
    log_row_check(state.row_check)

    # 件数を保存する（通常の実行だけ。 ``--rebuild`` は保存データを書き換えない）
    if allow_refresh and state.row_check is not None and state.row_check.rows is not None:
        report_store.save_rows(org, report_id, state.row_check.rows)

    # 主オブジェクトの解決。 ``describe_object`` が 401 / 403 を返したら
    # ``fetch_object_cached`` がそのまま上位へ ``SalesforceRequestError``
    # を送出し、 ID 全体を失敗扱いにする。
    main_object_name, main_describe, object_reason, csv_used_keys = choose_main(
        client, org, state.slim_report, store, state.warnings
    )
    state.main_object_name = main_object_name
    state.main_describe = main_describe
    state.object_reason = object_reason
    state.csv_used_keys = csv_used_keys
    if object_reason:
        state.warnings.append(object_reason)
    _log_report_contents(state.slim_report, main_object_name)

    # 主オブジェクトが確定したら ``objects`` の
    # 種を蒔いて、 列キーからのたどりに使う fetcher を作る
    fetcher: Callable[[str], dict[str, Any] | None] | None = None
    if main_object_name and main_describe is not None:
        state.objects[main_object_name] = main_describe
        fetcher = make_overrides_fetcher(
            client=client,
            org=org,
            store=store,
            objects=state.objects,
            csv_used_keys=state.csv_used_keys,
        )

    # 保存済みの項目定義には子リレーションが入らないので、 主オブジェクトの
    # 子リレーションが未取得なら API で取り直す。 列対応表を作る前にやる
    # （ 取り直しの結果で ``main_describe`` が変わるため ）。
    # 保存データだけで作り直すとき（ ``allow_refresh=False`` ）は API を
    # 呼ばないので取り直さない
    if allow_refresh:
        _refresh_child_relationships(
            client=client,
            org=org,
            entry=entry,
            state=state,
            store=store,
            refresh_set=refresh_set,
        )

    # 列対応表を組み立てる (主が取れるときは関連オブジェクト収集もここで行う)
    column_map = _build_initial_column_map(
        client=client,
        org=org,
        entry=entry,
        state=state,
        store=store,
        fetcher=fetcher,
        related_depth=related_depth,
        related_max=related_max,
    )
    state.column_map = column_map

    # 取り直し: 列対応に (不明) が残り、 CSV から読んだオブジェクトが
    # このレポートで使われているなら API で取り直す。
    # 保存データだけで作り直すとき（ ``allow_refresh=False`` ）は API を
    # 呼ばないので取り直さない
    if allow_refresh:
        _refresh_unresolved_columns(
            client=client,
            org=org,
            entry=entry,
            state=state,
            store=store,
            refresh_set=refresh_set,
            fetcher=fetcher,
        )

    # 手入力 （ ``input/子リレーション.csv`` ） の子リレーションを主オブジェクトの
    # describe へ合わせる。 取り直しのあと（ ``main_describe`` が変わるため ） に行う。
    # 保存データ （ ``data/子リレーション.csv`` ） には手入力の値を書き込まない
    _apply_manual_child_relationships(state, manual_child_relationships)

    # クロスフィルタの副問い合わせに使う子オブジェクトの describe を取りに行く。
    # 取り直しのあと（ ``main_describe`` が変わるため ） に行う。
    # ``objects`` には入れない（関係図・関連表・対応表の出力を変えないため）。
    state.cross_filter_objects = _collect_cross_filter_objects(
        client=client,
        org=org,
        state=state,
        store=store,
    )

    # レポートの列が子オブジェクトの項目のときは、 SOQL の FROM を子に切り替える。
    # 列対応表ができたあと、 ``列の直し`` の上書きの前に行う
    _apply_child_base_switch(state=state, fetcher=fetcher)

    # 人が ``列の直し.xlsx`` に入れた値で ``column_map`` を上書きする。
    # FROM を子に切り替えたときは、 主オブジェクトとして子を渡す
    state.column_map = column_fix_xlsx.apply_overrides(
        column_map_rows=state.column_map,
        existing=column_fix_overrides,
        main_object=state.soql_from_object or main_object_name or "",
        objects=state.objects,
        warnings=state.warnings,
        fetch_object=fetcher,
    )


def _apply_manual_child_relationships(
    state: FetchState,
    manual_child_relationships: tuple[child_relationship_csv.ManualChildRelationship, ...],
) -> None:
    """手入力の子リレーションを主オブジェクトの describe へ足す。

    主オブジェクトの describe が無いときは何もしない。 保存データ
    （ ``data/子リレーション.csv`` ） には手入力の値を書き込まない。
    """
    main_object_name = state.main_object_name
    if not main_object_name or not manual_child_relationships:
        return
    main_describe = state.main_describe
    if not isinstance(main_describe, dict):
        return
    merged = child_relationship_csv.merge_manual_child_relationships(
        child_relationships=list(main_describe.get("childRelationships") or []),
        manual_rows=manual_child_relationships,
        parent_object=main_object_name,
    )
    if merged == list(main_describe.get("childRelationships") or []):
        return
    main_describe["childRelationships"] = merged
    # ``state.objects`` の主オブジェクトは ``state.main_describe`` と同じ dict
    state.objects[main_object_name] = main_describe


def _apply_child_base_switch(
    *,
    state: FetchState,
    fetcher: Callable[[str], dict[str, Any] | None] | None,
) -> None:
    """レポートの列が子の項目のとき、 列対応表を 子から取る形 に書き換える。

    切り替えたときは、 子の describe を ``state.objects`` に入れ、 FROM に使う
    子オブジェクト名を ``state.soql_from_object`` に記録する。
    """
    main_object_name = state.main_object_name
    if not main_object_name or not state.column_map:
        return
    result = apply_child_base(
        column_map=state.column_map,
        main_object=main_object_name,
        main_describe=state.main_describe,
        fetch_object=fetcher,
    )
    for note in result.notes:
        state.warnings.append(note)
    if not result.switched:
        return
    state.column_map = result.column_map
    state.soql_from_object = result.child_object
    state.soql_from_relationship = result.relationship_name
    if result.child_describe is not None:
        state.objects[result.child_object] = result.child_describe


def _collect_cross_filter_objects(
    *,
    client: Any,
    org: str,
    state: FetchState,
    store: DescribeStore,
) -> dict[str, dict[str, Any]]:
    """クロスフィルタの ``relatedEntity`` を子オブジェクトとして取り、 describe を返す。

    主オブジェクトの ``childRelationships`` に ``childSObject`` または
    ``relationshipName`` で一致するものを探す。 取りに行くのは既存の取得部品
    （ ストアのキャッシュ → API ） で、 失敗時は警告だけ残す。

    戻り値のキーは子オブジェクト名。 ``state.objects`` には入れない。
    """
    cross_filters = state.slim_report.get("reportMetadata", {}).get("crossFilters")
    if not (isinstance(cross_filters, list) and cross_filters):
        return {}
    main_describe = state.main_describe
    if not isinstance(main_describe, dict):
        return {}

    child_relationships = main_describe.get("childRelationships")
    if not isinstance(child_relationships, list):
        return {}

    result: dict[str, dict[str, Any]] = {}
    for cross_filter in cross_filters:
        if not isinstance(cross_filter, dict):
            continue
        related_entity = _coerce_str(cross_filter.get("relatedEntity")).strip()
        child_name = find_child_object_name(child_relationships, related_entity)
        if not child_name or child_name in result:
            continue
        describe = fetch_object_cached(client, org, child_name, store, state.warnings)
        if describe is None:
            continue
        result[child_name] = describe
    return result


def _refresh_child_relationships(
    *,
    client: Any,
    org: str,
    entry: MasterEntry,
    state: FetchState,
    store: DescribeStore,
    refresh_set: set[tuple[str, str]],
) -> None:
    """主オブジェクトの子リレーションが未取得なら、 API で取り直す。

    項目定義.csv には子リレーションが入らないので、 保存されたままでは
    クロスフィルタを副問い合わせに変換できない。 子リレーションは 副問い合わせ
    の材料なので、 クロスフィルタの有無いにかかわらず 全レポートの主オブジェクト
    で取る。 取り直しは ``store.refresh_from_api`` で行い、 子リレーションの CSV
    にも保存する。 取り直した結果は ``state.main_describe`` と ``state.objects``
    に反映する。

    - 取り直しに失敗したら CSV の値のまま続ける
    - 1 回の実行で同じオブジェクトを 2 度取り直さない
    - ``--rebuild`` （ ``allow_refresh=False`` ） では呼ばない
    - 子リレーションが 0 件でも 「取得済み」 として扱い、 取り直さない
      （ 子リレーション.csv に 1 行残すため、 2 回目の実行で API を呼ばない ）
    """
    main_object_name = state.main_object_name
    if not main_object_name:
        return
    key = object_key(org, main_object_name)
    # CSV 由来でない（ API から取ったか、 すでに取り直した） なら子リレーションも
    # 入っているので取り直さない
    if key not in state.csv_used_keys:
        return
    # 1 回の実行で同じオブジェクトを取り直さない
    if key in refresh_set:
        return
    if store.has_child_relationships(org, main_object_name):
        # 子リレーションの CSV から読んだ （ すでに保存されている ）
        return

    api_name = api_name_from_store(store, key) or main_object_name
    logger.info(
        "取り直し: 管理番号=%s 組織=%s オブジェクト=%s （子リレーションが未取得のため）",
        entry.key,
        org,
        api_name,
    )
    refreshed_raw = store.refresh_from_api(client, org, api_name, state.warnings)
    # 成功でも失敗でも、 この実行ではもう取り直さない
    refresh_set.add(key)
    if refreshed_raw is None:
        # 取り直しに失敗したら CSV の値のまま続ける。 ストアは失敗記録に
        # 置き換えてしまったので、 CSV の値を戻す（ 次のレポートが主オブジェクトを
        # 見つけられなくなるのを防ぐ ）。 失敗の理由はストアの文言をそのまま出す
        failed = store.lookup(org, main_object_name)
        if failed is not None and failed.get("warning"):
            state.warnings.append(failed["warning"])
        store.restore_csv_entry(org, main_object_name)
        return

    refreshed = make_slim_object(refreshed_raw)
    state.main_describe = refreshed
    state.objects[main_object_name] = refreshed
    # 取り直したものは CSV 由来でなくなる（ 以後取り直しの対象外にする ）
    state.csv_used_keys.discard(key)
    cached = store.lookup(org, main_object_name)
    if cached is not None and cached.get("source") == "csv":
        logger.warning(
            "取り直し後も CSV 由来のままです（子リレーションが古い可能性があります）: %s",
            main_object_name,
        )


def _build_initial_column_map(
    *,
    client: Any,
    org: str,
    entry: MasterEntry,
    state: FetchState,
    store: DescribeStore,
    fetcher: Callable[[str], dict[str, Any] | None] | None,
    related_depth: int,
    related_max: int,
) -> list[dict[str, Any]]:
    """主オブジェクトが取れたときは関連オブジェクトも掘ってから列対応表を作る。"""
    main_object_name = state.main_object_name
    main_describe = state.main_describe
    if not (main_object_name and main_describe is not None):
        # 主オブジェクトが取れなかったときの列対応表（全列が理由つきの (不明)）
        return mapping.build_column_map(
            state.slim_report,
            main_describe,
            state.object_reason,
            related_describes=state.related_describes,
            relations=[],
            main_object=main_object_name,
            fetch_object=fetcher,
        )
    # 関連オブジェクトを参照先から幅優先で掘る（``RELATED_DEPTH`` 段まで）
    new_nodes, new_relations, skipped_names = collect_related_objects(
        main_describe,
        fetcher=lambda name: fetch_object_cached(
            client, org, name, store, state.warnings, state.csv_used_keys
        ),
        related_depth=related_depth,
        related_max=related_max,
    )
    state.related_nodes = new_nodes
    state.relations = new_relations
    if skipped_names:
        collected_count = len(new_nodes)
        total = collected_count + len(skipped_names)
        state.warnings.append(
            f"関連オブジェクトが {total} 件あり"
            f"上限 {related_max} を超えたため、"
            f"次の {len(skipped_names)} 件は取得しません: {', '.join(skipped_names)}"
        )
    log_related_objects(new_nodes, skipped_names)
    for node in new_nodes:
        if node.describe is not None:
            state.objects[node.name] = node.describe
            state.related_describes[node.name] = node.describe
    # 列対応表を取り直す（関連オブジェクトが取れてから解く）
    return mapping.build_column_map(
        state.slim_report,
        main_describe,
        state.object_reason,
        related_describes=state.related_describes,
        relations=state.relations,
        main_object=main_object_name,
        fetch_object=fetcher,
    )


def _refresh_unresolved_columns(
    *,
    client: Any,
    org: str,
    entry: MasterEntry,
    state: FetchState,
    store: DescribeStore,
    refresh_set: set[tuple[str, str]],
    fetcher: Callable[[str], dict[str, Any] | None] | None,
) -> None:
    """列対応に ``(不明)`` が残るなら、 CSV から読んだオブジェクトを API で取り直す。

    1 パス内では 1 度きり ( 取り直し後も (不明) が残るケースは API 側でも
    解決できないので、 無限ループを避ける ) 。
    """
    main_object_name = state.main_object_name
    if not (
        main_object_name
        and state.main_describe is not None
        and _has_unresolved_columns(state.column_map, state.object_reason)
    ):
        return
    refresh_keys = state.csv_used_keys - refresh_set
    if not refresh_keys:
        return
    refresh_set.update(refresh_keys)
    refreshed_main = _refresh_keys_in_store(
        client=client,
        org=org,
        entry=entry,
        main_object_name=main_object_name,
        store=store,
        warnings=state.warnings,
        refresh_keys=refresh_keys,
        objects=state.objects,
        related_describes=state.related_describes,
    )
    if refreshed_main is not None:
        state.main_describe = refreshed_main
    # 列対応表を取り直す（取り直し後）
    state.column_map = mapping.build_column_map(
        state.slim_report,
        state.main_describe,
        state.object_reason,
        related_describes=state.related_describes,
        relations=state.relations,
        main_object=main_object_name,
        fetch_object=fetcher,
    )


def _refresh_keys_in_store(
    *,
    client: Any,
    org: str,
    entry: MasterEntry,
    main_object_name: str,
    store: DescribeStore,
    warnings: list[str],
    refresh_keys: set[tuple[str, str]],
    objects: dict[str, dict[str, Any]],
    related_describes: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    """``refresh_keys`` のオブジェクトを API で取り直し、 メモリに反映する。

    主オブジェクトが該当すれば最新値を返し、 それ以外は ``objects`` /
    ``related_describes`` を直接更新する。
    """
    refreshed_main: dict[str, Any] | None = None
    for key in refresh_keys:
        object_name = api_name_from_store(store, key)
        if object_name is None:
            continue
        logger.info(
            "取り直し: 管理番号=%s 組織=%s オブジェクト=%s",
            entry.key,
            key[0],
            object_name,
        )
        refreshed_raw = store.refresh_from_api(client, key[0], object_name, warnings)
        if refreshed_raw is None:
            continue
        # 境界: 取り直した describe もここで整えてからメモリに反映
        refreshed = make_slim_object(refreshed_raw)
        if object_key(org, main_object_name) == key:
            objects[main_object_name] = refreshed
            refreshed_main = refreshed
            continue
        for obj_name in list(objects):
            if object_key(org, obj_name) == key:
                objects[obj_name] = refreshed
                related_describes[obj_name] = refreshed
                break
    return refreshed_main


def _has_unresolved_columns(
    column_map: list[dict[str, str]],
    object_reason: str | None,
) -> bool:
    """``column_map`` に ``対応フィールドAPI名 == "(不明)"`` の行が **1 つでも**
    あるかを返す。

    ``object_reason`` が ``None`` でないとき （主オブジェクト不明・ describe 失敗）
    は、そのレポートの主オブジェクトはそもそも API で取れていないので、
    取り直しの対象外とする。
    """
    if object_reason:
        return False
    return any(row.get("対応フィールドAPI名") == "(不明)" for row in column_map)


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
