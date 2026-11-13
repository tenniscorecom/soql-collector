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
from src.fetch.models import RelatedNode, RelationRecord, RowCheck, SourceTimes
from src.fetch.object_fetch import fetch_object_cached, make_overrides_fetcher
from src.fetch.related import collect_related_objects, log_related_objects
from src.fetch.report_slim import SlimReport, collect_dropped_keys, make_slim_report
from src.fetch.row_check import can_save_row_check, check_row_count, log_row_check
from src.mapping.child_base import apply_child_base
from src.mapping.child_subquery import MODE_SUBQUERY, ChildSubquery, apply_child_shape
from src.master import MasterEntry
from src.report_store import ReportStore
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
    #: SOQL の SELECT に入れる、 子リレーションの副問い合わせ
    child_subquery: ChildSubquery | None = None
    #: 子の副問い合わせにしたときの要確認。 SOQL の下書きの checks に入る
    child_subquery_checks: tuple[str, ...] = ()
    #: 子の副問い合わせで取る列の列キー。 「列の直し」 の上書きから除外する
    child_subquery_column_keys: tuple[str, ...] = ()
    #: 接続先の組織
    org: str = ""
    #: 元データの取得日時
    source_times: SourceTimes | None = None


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
    related_depth: int,
    related_max: int,
    column_fix_overrides: dict[tuple[str, str], Any],
    state: FetchState,
    manual_child_relationships: tuple[child_relationship_csv.ManualChildRelationship, ...] = (),
) -> None:
    """``with site_class() as client:`` の中で行う一連の取得・組み立てを行う。

    取得の段でも解析の段でも同じ経路を通る。 取得の段は保存に無いものを API で取って保存し、
    解析の段は ``client`` がオフラインなので保存データだけで進む。

    ``manual_child_relationships`` は人が ``input/子リレーション.csv`` に
    手入力した子リレーション。 主オブジェクトの子リレーションに足してから
    クロスフィルタの判定と FROM の切り替えに使う。
    """
    state.org = org
    # 境界: 原本を手に入れたら、 ここで 1 回だけ整え、 以後は整えた値だけを使う
    metadata = report_store.get(org, report_id)
    if metadata is None:
        try:
            metadata = client.report.describe(report_id)
        except SalesforceRequestError as exc:
            report_store.record_failure()
            state.warnings.append(
                f"レポート describe に失敗（HTTP {exc.status_code}）: {report_id}\n{exc.detail}"
                + _NOT_FOUND_HINT * (exc.status_code == 404)
            )
            return
        report_store.put(org, report_id, metadata)
    state.slim_report = make_slim_report(metadata)
    state.dropped_keys = collect_dropped_keys(metadata)

    state.row_check = _get_row_check(client, org, report_id, report_store, row_limit)
    log_row_check(state.row_check)

    # 主オブジェクトの解決。 ``describe_object`` が 401 / 403 を返したら
    # ``fetch_object_cached`` がそのまま上位へ ``SalesforceRequestError``
    # を送出し、 ID 全体を失敗扱いにする。
    main_object_name, main_describe, object_reason = choose_main(
        client, org, state.slim_report, store, state.warnings
    )
    state.main_object_name = main_object_name
    state.main_describe = main_describe
    state.object_reason = object_reason
    state.source_times = SourceTimes(
        report=report_store.fetched_at(org, report_id),
        main_object=store.fetched_at(org, main_object_name) if main_object_name else "",
        row_count=_row_count_fetched_at(org, report_id, report_store),
    )
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
        )

    # 列対応表を組み立てる (主が取れるときは関連オブジェクト収集もここで行う)
    column_map = _build_initial_column_map(
        client=client,
        org=org,
        state=state,
        store=store,
        fetcher=fetcher,
        related_depth=related_depth,
        related_max=related_max,
    )
    state.column_map = column_map

    # 手入力 （ ``input/子リレーション.csv`` ） の子リレーションを主オブジェクトの
    # describe へ合わせる。 保存した原本には手入力の値を書き込まない
    _apply_manual_child_relationships(state, manual_child_relationships)

    # クロスフィルタの副問い合わせに使う子オブジェクトの describe を取りに行く。
    # ``objects`` には入れない（関係図・関連表・対応表の出力を変えないため）。
    state.cross_filter_objects = _collect_cross_filter_objects(
        client=client,
        org=org,
        state=state,
        store=store,
    )

    # レポートの列が子オブジェクトの項目のときは、 副問い合わせか 子から取る
    # 形かを決める。 列対応表ができたあと、 ``列の直し`` の上書きの前に行う
    _apply_child_shape(
        state=state,
        fetcher=fetcher,
        column_fix_overrides=column_fix_overrides,
    )

    # 人が ``列の直し.xlsx`` に入れた値で ``column_map`` を上書きする。
    # FROM を子に切り替えたときは、 主オブジェクトとして子を渡す
    state.column_map = column_fix_xlsx.apply_overrides(
        column_map_rows=state.column_map,
        existing=column_fix_overrides,
        main_object=state.soql_from_object or main_object_name or "",
        objects=state.objects,
        warnings=state.warnings,
        fetch_object=fetcher,
        # 子の副問い合わせで取る列は、 そちらで上書き済み
        skip_column_keys=state.child_subquery_column_keys,
    )


def _get_row_check(
    client: Any,
    org: str,
    report_id: str,
    report_store: ReportStore,
    row_limit: bool,
) -> RowCheck | None:
    """件数の判定を保存から読む。 無ければ、 ``row_limit`` のときだけ API で取って保存する。"""
    saved = report_store.get_row_count(org, report_id)
    if saved is not None:
        return RowCheck(status=saved.status, rows=saved.rows, reason=saved.reason)
    if not row_limit:
        return None
    report_store.row_count_api_count += 1
    row_check = check_row_count(client, report_id)
    if can_save_row_check(row_check):
        report_store.put_row_count(
            org,
            report_id,
            status=row_check.status,
            rows=row_check.rows,
            reason=row_check.reason,
        )
    return row_check


def _row_count_fetched_at(org: str, report_id: str, report_store: ReportStore) -> str:
    """件数を取った日時。 保存が無ければ空文字。"""
    saved = report_store.get_row_count(org, report_id)
    return saved.fetched_at if saved is not None else ""


def _apply_manual_child_relationships(
    state: FetchState,
    manual_child_relationships: tuple[child_relationship_csv.ManualChildRelationship, ...],
) -> None:
    """手入力の子リレーションを主オブジェクトの describe へ足す。

    主オブジェクトの describe が無いときは何もしない。 保存した原本には
    手入力の値を書き込まない。
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


def _apply_child_shape(
    *,
    state: FetchState,
    fetcher: Callable[[str], dict[str, Any] | None] | None,
    column_fix_overrides: dict[tuple[str, str], Any],
) -> None:
    """レポートの列が子の項目のとき、 副問い合わせか 子から取る形かを決める。

    - **A** … 人が「直したSOQL列名」に子リレーションの列 （ 例 ``Contacts.LastName`` ）
      を書いたとき。 SELECT の子リレーション副問い合わせにする
    - **B** … 人の指定が無くても、 子の列があり かつ クロスフィルタがあるとき。
      同じく副問い合わせにする
    - **C** … それ以外。 従来どおり FROM を子に切り替える

    副問い合わせにしたときは、 FROM は親のままにして、 子の列を
    ``Contacts.LastName`` の形へ書き換える。 「列の直し」 の上書きは
    その列を除いたところで行う。
    """
    main_object_name = state.main_object_name
    if not main_object_name or not state.column_map:
        return
    result = apply_child_shape(
        column_map=state.column_map,
        main_object=main_object_name,
        main_describe=state.main_describe,
        fetch_object=fetcher,
        human_fixes=_human_fixed_names(main_object_name, state.column_map, column_fix_overrides),
        has_cross_filter=_has_cross_filter(state.slim_report),
    )
    for note in result.notes:
        state.warnings.append(note)
    state.child_subquery_checks = result.checks
    if result.mode != MODE_SUBQUERY:
        # C: 従来どおり FROM を子に切り替える
        _apply_child_base_switch(state=state, fetcher=fetcher)
        return
    state.column_map = result.column_map
    state.child_subquery = result.subquery
    state.child_subquery_column_keys = result.child_column_keys


def _human_fixed_names(
    main_object: str,
    column_map: list[dict[str, Any]],
    column_fix_overrides: dict[tuple[str, str], Any],
) -> dict[str, str]:
    """列キー → 人が「直したSOQL列名」に書いた値。 無い列は含まない。"""
    fixed: dict[str, str] = {}
    for row in column_map:
        column_key = row.get("列キー", "")
        if not column_key:
            continue
        override = column_fix_overrides.get((main_object.strip().lower(), column_key))
        if override is None:
            continue
        name = str(getattr(override, "fixed_soql_name", "") or "")
        if name:
            fixed[column_key] = name
    return fixed


def _has_cross_filter(slim_report: SlimReport) -> bool:
    """レポートにクロスフィルタがあるか。"""
    cross_filters = slim_report.get("reportMetadata", {}).get("crossFilters")
    return bool(isinstance(cross_filters, list) and cross_filters)


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


def _build_initial_column_map(
    *,
    client: Any,
    org: str,
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
        fetcher=lambda name: fetch_object_cached(client, org, name, store, state.warnings),
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
