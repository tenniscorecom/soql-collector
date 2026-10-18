"""1 レポート分の取得・列対応表の組み立て（接続先に入ったあとの一連の処理）。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any

from comken.exceptions import SalesforceRequestError

from src import column_fix_xlsx, mapping
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
from src.master import MasterEntry
from src.report_store import ReportStore

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
) -> None:
    """``with site_class() as client:`` の中で行う一連の取得・組み立てを行う。"""
    # 境界: レポート定義はここで 1 回だけ整え、 以後は整えた値だけを使う。
    # 貯めてあればそれを使い（期限は無い。 作り直すときは ``レポート定義/`` を消す）、
    # 無ければ API で取って貯める
    cached = report_store.get(org, report_id)
    if cached is not None:
        state.slim_report = cached.slim_report
        state.dropped_keys = cached.dropped_keys
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

    # 2000 行チェック: ``row_limit`` が True のときだけ実行
    state.row_check = check_row_count(client, report_id) if row_limit else None
    log_row_check(state.row_check)

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
    # このレポートで使われているなら API で取り直す
    _refresh_unresolved_columns(
        client=client,
        org=org,
        entry=entry,
        state=state,
        store=store,
        refresh_set=refresh_set,
        fetcher=fetcher,
    )

    # 人が ``列の直し.xlsx`` に入れた値で ``column_map`` を上書きする
    state.column_map = column_fix_xlsx.apply_overrides(
        column_map_rows=state.column_map,
        existing=column_fix_overrides,
        main_object=main_object_name or "",
        objects=state.objects,
        warnings=state.warnings,
        fetch_object=fetcher,
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
