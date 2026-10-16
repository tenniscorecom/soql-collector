"""``build_column_map()`` 本体と 1 列を解決する手順群。

ビルドの手順:

- 0. 列キー （``主オブジェクト.`` 始まり） からたどる
- 0b. ``FK_`` からたどる
- 1/2. 表示名 （主 → 関連、 浅い順） で照合

各手順を独立したプライベート関数として持ち、 ``_resolve_one_column()`` が
順に試して結果を ``ResolverResult`` に詰めて返す。 dict への書き出しは
``_row_dict()`` だけで行う。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from src.mapping.chains import _ChainCandidate, build_relation_chains
from src.mapping.column_key import (
    COLUMN_KEY_PATH_MAX_DEPTH,
    resolve_column_key_to_soql,
    starts_with_main_object,
)
from src.mapping.columns import (
    collect_column_info,
    collect_describable_columns,
    field_name_from_soql,
)
from src.mapping.normalizers import (
    normalize_identifier,
    normalize_label,
    split_object_label,
)

# ═════════════════════════════════════════════════════════════════════
# 公開 API
# ═════════════════════════════════════════════════════════════════════


def build_column_map(
    metadata: dict[str, Any],
    main_describe: dict[str, Any] | None,
    reason: str | None,
    *,
    related_describes: dict[str, dict[str, Any]] | None = None,
    relations: list[Any] | None = None,
    main_object: str | None = None,
    fetch_object: Callable[[str], dict[str, Any] | None] | None = None,
) -> list[dict[str, str]]:
    """渡された主オブジェクトの describe から列対応表を作る （純粋関数） 。

    関連オブジェクトの describe （名前 → describe dict） と relations を
    渡すと、 関連を跨ぐ表示名も解決する。 ``main_describe`` が ``None`` の
    とき、 もしくは ``reason`` が ``None`` でないときは、 全列を ``"(不明)"``
    にして備考に ``reason`` を入れる。 一致するフィールドが無い列は
    ``"対応フィールドなし"``、 候補が 2 件以上の列は誤った候補を押し付けず
    「複数候補あり」 にする。

    解決アルゴリズム （列キーごとに） は以下を順に試す:

    1. 列キーからたどる （``_try_resolve_by_column_key``）
    2. ``FK_`` からたどる （``_try_resolve_by_fk``）
    3. 表示名で照合 （``_try_resolve_by_label``）

    列キーの実際の形は本物の組織で未確認。 この実装は推測の範囲で作る。
    結果は **必ず備考を確認** すること。

    Args:
        metadata: ``client.report.describe(report_id)`` の戻り値。
        main_describe: 主オブジェクトの Object Describe。 取れなかった /
            取らなかったときは ``None``。
        reason: 主オブジェクトを特定できなかった / Object Describe が失敗した
            理由。 ``None`` のときは特定・取得が成功している。
        related_describes: 関連オブジェクト名 → Object Describe の dict。
            渡された関連だけが解決対象になる。
        relations: ``RelationRecord`` のリスト。 鎖の計算に使う。
        main_object: 主オブジェクト名。 ``relations`` から鎖を引くために使う。
        fetch_object: 経路の補完 （列キーからのたどり） で経路上の
            オブジェクトを取りに行く関数。 ``None`` のときは ``main_describe``
            だけから既知の経路で解決を試み、 無いものはたどれなくなる。

    Returns:
        列対応表の行のリスト。 各行は ``列キー`` / ``表示名`` /
        ``所属オブジェクト`` / ``対応フィールドAPI名`` / ``型`` /
        ``参照先オブジェクト`` / ``リレーション名`` / ``備考`` のキーを
        持つ dict。
    """
    related_describes = related_describes or {}
    relations = relations or []

    report_metadata = metadata.get("reportMetadata", {})
    columns = collect_describable_columns(report_metadata)
    column_info = collect_column_info(metadata)

    main_object = _resolve_main_object(main_object, main_describe)
    object_label_to_api = _build_object_label_to_api(main_object, main_describe, related_describes)
    field_index = _build_main_field_index(main_describe)
    ordered_chains = _build_ordered_chains(relations, main_object)

    # ``main_describe`` が無い / ``reason`` が有るときは 「不明」 化で早期 return
    if main_describe is None or field_index is None or reason is not None:
        return [
            _unknown_row(column_key, column_info, main_object, reason) for column_key in columns
        ]

    ctx = ResolveContext(
        main_object=main_object,
        main_describe=main_describe,
        field_index=field_index,
        ordered_chains=ordered_chains,
        related_describes=related_describes,
        object_label_to_api=object_label_to_api,
        fetch_object=fetch_object,
    )

    rows: list[dict[str, str]] = []
    for column_key in columns:
        rows.append(_build_one_row(ctx, column_key, column_info))
    return rows


# ═════════════════════════════════════════════════════════════════════
# 解決コンテキスト
# ═════════════════════════════════════════════════════════════════════


@dataclass
class ResolveContext:
    """1 列の解決で使い回す値のスナップショット。

    ``related_indices`` は関連オブジェクトのフィールドインデックスを
    メモ化するための辞書。 列ごとに再構築しないために ``_build_one_row()``
    内 （実際にはその内部関数） で追加される。
    """

    main_object: str
    main_describe: dict[str, Any]
    field_index: dict[str, list[dict[str, Any]]]
    ordered_chains: list[_ChainCandidate]
    related_describes: dict[str, dict[str, Any]]
    object_label_to_api: dict[str, str]
    fetch_object: Callable[[str], dict[str, Any] | None] | None
    related_indices: dict[str, dict[str, list[dict[str, Any]]]] = field(default_factory=dict)


def _resolve_main_object(
    main_object: str | None,
    main_describe: dict[str, Any] | None,
) -> str:
    """``main_object`` を確定する。 ``None`` のときは ``main_describe["name"]`` から取る。"""
    if main_object is None and main_describe is not None:
        main_object = main_describe.get("name")
    return main_object or ""


def _build_object_label_to_api(
    main_object: str,
    main_describe: dict[str, Any] | None,
    related_describes: dict[str, dict[str, Any]],
) -> dict[str, str]:
    """オブジェクトの表示名 → API 名のマップを作る。

    「タスク: 作業会社名」 形式の照合に使う。 主オブジェクト自身と
    ``related_describes`` の各オブジェクトの表示名を、 ともに小文字化して登録。
    """
    if main_describe is None:
        return {}
    object_label_to_api: dict[str, str] = {}
    labeled = [(main_object, main_describe), *related_describes.items()]
    for object_name, describe in labeled:
        object_label = describe.get("label")
        if object_label:
            object_label_to_api[normalize_label(object_label)] = object_name
    return object_label_to_api


def _build_main_field_index(
    main_describe: dict[str, Any] | None,
) -> dict[str, list[dict[str, Any]]] | None:
    """主オブジェクトの ``fields`` を ``{正規化表示名: [field, ...]}`` に組み立てる。

    同じ表示名を持つフィールドが複数ある場合は最初に見つかった 1 件だけを
    選ばず **リストのまま** 残す。 呼び出し側で件数を判定し、 1 件なら
    採用、 2 件以上なら 「複数候補あり」 として注記する。 名前か表示名が
    空の項目は照合に使えないので入れない。
    """
    if main_describe is None:
        return None
    return _build_field_index(main_describe)


def _build_field_index(
    describe: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    """Object Describe の ``fields`` を ``{正規化表示名: [field, ...]}`` に組み立てる。"""
    index: dict[str, list[dict[str, Any]]] = {}
    for f in describe.get("fields", []):
        name = f.get("name")
        label = f.get("label")
        if not name or not label:
            continue
        index.setdefault(normalize_label(label), []).append(
            {
                "name": name,
                "label": label,
                "type": f.get("type") or "",
                "referenceTo": f.get("referenceTo") or [],
                "relationshipName": f.get("relationshipName") or "",
            }
        )
    return index


def _build_ordered_chains(
    relations: list[Any],
    main_object: str,
) -> list[_ChainCandidate]:
    """主オブジェクトからの関連オブジェクトの鎖を、 浅い順に並べたリストで返す。

    ``build_relation_chains()`` の戻り値を全候補に展開して ``(depth, object_name)``
    で安定ソートする。 主オブジェクトが空のときは空リスト。
    """
    if not main_object:
        return []
    chains_by_object = build_relation_chains(relations, main_object)
    ordered: list[_ChainCandidate] = []
    for obj_name in chains_by_object:
        for chain in chains_by_object[obj_name]:
            ordered.append(chain)
    ordered.sort(key=lambda c: (c.depth, c.object_name))
    return ordered


def _get_or_build_related_index(
    ctx: ResolveContext,
    object_name: str,
    describe: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    """関連オブジェクトのフィールドインデックスをメモ化して返す。"""
    if object_name not in ctx.related_indices:
        ctx.related_indices[object_name] = _build_field_index(describe)
    return ctx.related_indices[object_name]


# ═════════════════════════════════════════════════════════════════════
# 解決結果の中間型
# ═════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class ResolvedMatch:
    """1 列の解決に成功したときの内容。

    Attributes:
        note: 備考欄の文言 （ 例: ``"列キーから"`` 、 ``"FK_ から"`` 、
            ``"関連オブジェクト経由"`` 、 ``"列キーの接頭辞で絞り込み"`` ） 。
        owner: 所属オブジェクト名。
        soql_chain: 関連経由のときの鎖。 主オブジェクトのときは空文字。
        soql_name: 組み立て済みの SOQL 列名。 ``{"": "Name"}`` のように
            1 段だけの SOQL 名も含む。 備考に置くためフル形で持つ。
        field: 末端項目の dict。 ``name`` / ``label`` / ``type`` /
            ``referenceTo`` / ``relationshipName`` を持つ。
        terminal_describe: 末端の参照先 / リレーション名を ``_fill_reference()``
            で埋めるために探す describe 。
        id_column: 末端の参照項目を参照先の名前項目に置き換えたときの
            元の ID 列 SOQL 名。 ``None`` のときは置換していない。
        note_suffix_id_column: 備考に置く ID 列 SOQL 名で、 ``id_column``
            とは別 （FK_ のときは鎖付きにする） 。 ``None`` のときは
            ``id_column`` をそのまま使う。
    """

    note: str
    owner: str
    soql_chain: str
    soql_name: str
    field: dict[str, Any]
    terminal_describe: dict[str, Any]
    id_column: str | None = None
    note_suffix_id_column: str | None = None


@dataclass(frozen=True)
class ResolverResult:
    """1 列を解決した結果を 1 か所に集約する中間型。

    ``match`` が非 None のとき成功、 ``multi_note`` が非空のとき
    「複数候補あり」 の特殊成功、 どちらも無いときは ``failure_note``
    に失敗理由を入れた通常失敗。
    """

    match: ResolvedMatch | None = None
    multi_note: str = ""
    failure_note: str = ""


# ═════════════════════════════════════════════════════════════════════
# 行 dict 生成 （出力 dict の組み立てはここに集約）
# ═════════════════════════════════════════════════════════════════════


def _unknown_row(
    column_key: str,
    column_info: dict[str, dict[str, Any]],
    main_object: str,
    reason: str | None,
) -> dict[str, str]:
    """主 describe が無い / 失敗理由で全列を 「不明」 にする 1 行を作る。"""
    label = column_info.get(column_key, {}).get("label", column_key)
    return _row_dict_with(column_key, label, main_object, note=reason or "")


def _row_dict(
    column_key: str,
    label: str,
    main_object: str,
    result: ResolverResult,
) -> dict[str, str]:
    """解決結果 ``ResolverResult`` を行 dict に変換する。

    出力 dict の形は変えない。 dict 構築ロジックはこの関数と
    ``_apply_match()`` 、 ``_row_dict_with()`` だけで行う。
    """
    if result.match is not None:
        return _apply_match(column_key, label, main_object, result.match)
    if result.multi_note:
        return _row_dict_with(column_key, label, main_object, note=result.multi_note)
    return _row_dict_with(column_key, label, main_object, note=result.failure_note)


def _row_dict_with(
    column_key: str,
    label: str,
    main_object: str,
    *,
    note: str,
) -> dict[str, str]:
    """備考だけを埋めた行 dict を作る。 失敗行と複数候補行の共通ベース。"""
    return {
        "列キー": column_key,
        "表示名": label,
        "所属オブジェクト": main_object,
        "対応フィールドAPI名": "(不明)",
        "型": "",
        "参照先オブジェクト": "",
        "リレーション名": "",
        "備考": note,
    }


def _apply_match(
    column_key: str,
    label: str,
    main_object: str,
    match: ResolvedMatch,
) -> dict[str, str]:
    """解決済みの 1 件を dict に書き出す。

    ``id_column`` が非 None のときは参照先の名前項目に置き換えた経路で、
    末端は名前項目なので ``参照先オブジェクト`` と ``リレーション名`` は
    空のままにする。 そうでないときは ``_find_last_field()`` で
    末端項目の参照先 / リレーション名を ``_fill_reference()`` で埋める。
    """
    row = _row_dict_with(column_key, label, main_object, note="")
    # 「主オブジェクト由来」 は表示名照合で主の同名項目にヒットしたときだけ。
    # 列キー経路・ FK_ 経路では ``match.note`` が必ず具体的な文言を持つので、
    # ``is_from_main`` で空欄にするのは表示名経路の主由来ケースに限られる。
    is_from_main = match.note == ""
    row["所属オブジェクト"] = match.owner if not is_from_main else main_object
    row["対応フィールドAPI名"] = match.soql_name
    row["型"] = match.field.get("type") or ""

    if match.id_column is not None:
        # 末端の参照項目を参照先の名前項目に置き換えた。
        # 末端は名前項目なので 参照先 / リレーション名は埋めない。
        id_text = match.note_suffix_id_column or match.id_column
        row["備考"] = f"{match.note}（参照先の名前。ID なら {id_text}）"
        return row

    row["備考"] = "" if is_from_main else match.note

    last_field_name = field_name_from_soql(match.soql_name)
    last_field = _find_last_field(match.terminal_describe, last_field_name)
    if last_field is not None:
        _fill_reference(row, last_field)
    return row


def _fill_reference(row: dict[str, str], field_obj: dict[str, Any]) -> None:
    """``row`` に項目の参照先 （``|`` 区切り） とリレーション名を書く。"""
    row["参照先オブジェクト"] = "|".join(field_obj.get("referenceTo") or [])
    row["リレーション名"] = field_obj.get("relationshipName") or ""


def _find_last_field(
    main_describe: dict[str, Any],
    last_field_name: str,
) -> dict[str, Any] | None:
    """``main_describe`` の ``fields`` から ``name == last_field_name`` の項目を返す。

    列キー経路の最後の項目は多くの場合 **主オブジェクトの** フィールド
    （``Contact.Name`` / ``Contact.Account.Name`` など） 。 主オブジェクト
    に同名の項目があればそれを、 無ければ ``None`` を返す （ 参照先 /
    リレーション名は主オブジェクト上の項目に対してだけ埋める ） 。
    """
    if not last_field_name:
        return None
    return next(
        (
            field
            for field in main_describe.get("fields", [])
            if field.get("name") == last_field_name
        ),
        None,
    )


def _find_field_by_name(
    describe: dict[str, Any],
    name: str,
) -> dict[str, Any] | None:
    """``describe.fields`` から ``name == name`` の項目 dict を返す。"""
    if not name:
        return None
    return next(
        (f for f in describe.get("fields", []) if f.get("name") == name),
        None,
    )


# ═════════════════════════════════════════════════════════════════════
# 1 行の解決オーケストレーター
# ═════════════════════════════════════════════════════════════════════


def _build_one_row(
    ctx: ResolveContext,
    column_key: str,
    column_info: dict[str, dict[str, Any]],
) -> dict[str, str]:
    """1 列について解決手順を順に試し、 dict に変換して返す。"""
    label = column_info.get(column_key, {}).get("label", column_key)
    prefix_object, body_label = split_object_label(label)
    resolved_label = body_label if prefix_object else label
    normalized_label = normalize_label(resolved_label)
    target_object_hint = (
        ctx.object_label_to_api.get(normalize_label(prefix_object))
        if prefix_object is not None
        else None
    )
    result = _resolve_one_column(ctx, column_key, normalized_label, target_object_hint)
    return _row_dict(column_key, label, ctx.main_object, result)


# ═════════════════════════════════════════════════════════════════════
# 手順 0: 列キーからたどる
# ═════════════════════════════════════════════════════════════════════


def _resolve_one_column(
    ctx: ResolveContext,
    column_key: str,
    normalized_label: str,
    target_object_hint: str | None,
) -> ResolverResult:
    """1 列を解決して ``ResolverResult`` を返す。

    手順はコード順:

    1. 列キーからたどる
    2. ``FK_`` からたどる
    3. 表示名で照合

    前段で解けたら早期 return し、 解けなかった場合は失敗理由 （ 経路の補完
    に失敗したときのもの ） を保持して次の手順にフォールバックする。
    """
    # 手順 0
    if (
        ctx.main_object
        and "." in column_key
        and starts_with_main_object(column_key, ctx.main_object)
    ):
        soql_name, field_type, failure_note, id_column = resolve_column_key_to_soql(
            main_object=ctx.main_object,
            column_key=column_key,
            main_describe=ctx.main_describe,
            fetch_object=ctx.fetch_object,
            max_depth=COLUMN_KEY_PATH_MAX_DEPTH,
        )
        if soql_name:
            return _match_from_column_key(ctx, soql_name, field_type, id_column)
        path_failure_note = failure_note
    else:
        path_failure_note = None

    # 手順 0b
    if ctx.main_object and column_key.startswith("FK_"):
        fk_result = _try_resolve_by_fk(ctx, column_key)
        if fk_result.match is not None or fk_result.multi_note:
            return fk_result
        if fk_result.failure_note:
            path_failure_note = fk_result.failure_note

    # 手順 1/2: 表示名で照合
    return _try_resolve_by_label(
        ctx,
        column_key=column_key,
        normalized_label=normalized_label,
        target_object_hint=target_object_hint,
        path_failure_note=path_failure_note,
    )


def _match_from_column_key(
    ctx: ResolveContext,
    soql_name: str,
    field_type: str | None,
    id_column: str | None,
) -> ResolverResult:
    """``resolve_column_key_to_soql()`` の戻り値から ``ResolvedMatch`` を作る。

    主オブジェクトのフィールドインデックスから末端のフィールド dict を取り出し、
    必要なら ``_apply_match()`` で末端の参照先 / リレーション名を
    埋めるために ``terminal_describe`` として ``ctx.main_describe`` を渡す。
    """
    last_field_name = field_name_from_soql(soql_name)
    field = _find_field_by_name(ctx.main_describe, last_field_name)
    if field is None:
        # フィールド定義が取れないときは合成 dict で代替する
        field = {
            "name": last_field_name,
            "label": last_field_name,
            "type": field_type or "",
            "referenceTo": [],
            "relationshipName": "",
        }
    return ResolverResult(
        match=ResolvedMatch(
            note="列キーから",
            owner=ctx.main_object,
            soql_chain="",
            soql_name=soql_name,
            field=field,
            terminal_describe=ctx.main_describe,
            id_column=id_column,
        )
    )


# ═════════════════════════════════════════════════════════════════════
# 手順 0b: FK_ からたどる
# ═════════════════════════════════════════════════════════════════════


def _try_resolve_by_fk(ctx: ResolveContext, column_key: str) -> ResolverResult:
    """``FK_<Target>.<remainder>`` を主オブジェクトの参照項目経由で解く。

    戻り値の解釈は ``_resolve_one_column()`` がする。 解ければ
    ``ResolvedMatch`` （1 件確定） 、 経路が解けた候補が複数あれば
    ``multi_note``、 どれも無ければ ``failure_note`` に失敗理由を返す。
    """
    fk_body = column_key[3:]
    if fk_body.startswith("$"):
        fk_body = fk_body[1:]
    if "." not in fk_body:
        return ResolverResult()
    fk_target, _, fk_remainder = fk_body.partition(".")
    if not fk_target or not fk_remainder:
        return ResolverResult()

    fk_matches = _fk_reference_fields(ctx.main_describe, fk_target)
    if not fk_matches:
        return ResolverResult(
            failure_note=f"FK_ で {fk_target} を参照する項目が {ctx.main_object} にありません"
        )

    target_describe = _fk_target_describe(ctx, fk_target)
    if target_describe is None:
        return ResolverResult(failure_note=f"FK_ で {fk_target} の項目定義を取得できません")

    fk_sub_soql, fk_sub_type, fk_sub_failure, fk_sub_id_column = resolve_column_key_to_soql(
        main_object=fk_target,
        column_key=f"{fk_target}.{fk_remainder}",
        main_describe=target_describe,
        fetch_object=ctx.fetch_object,
        max_depth=COLUMN_KEY_PATH_MAX_DEPTH,
    )
    if not fk_sub_soql:
        return ResolverResult(
            failure_note=fk_sub_failure or f"FK_ で {fk_target} の経路 {fk_remainder} が解けません"
        )

    if len(fk_matches) == 1:
        return _match_from_fk_single(
            ctx=ctx,
            fk_match=fk_matches[0],
            target_describe=target_describe,
            fk_sub_soql=fk_sub_soql,
            fk_sub_type=fk_sub_type,
            fk_sub_id_column=fk_sub_id_column,
        )
    return _multi_from_fk(fk_matches, fk_sub_soql)


def _fk_reference_fields(
    main_describe: dict[str, Any],
    fk_target: str,
) -> list[dict[str, Any]]:
    """``fk_target`` を ``referenceTo`` に含み ``relationshipName`` の空でない主項目のリスト。"""
    return [
        field
        for field in main_describe.get("fields", [])
        if field.get("type") == "reference"
        and field.get("relationshipName")
        and field.get("name")
        and any(
            reference.lower() == fk_target.lower() for reference in field.get("referenceTo") or []
        )
    ]


def _fk_target_describe(
    ctx: ResolveContext,
    fk_target: str,
) -> dict[str, Any] | None:
    """``FK_`` 対象の describe を ``fetch_object`` 優先、
    無ければ ``related_describes`` から返す。
    """
    target_describe = ctx.fetch_object(fk_target) if ctx.fetch_object else None
    if target_describe is None:
        target_describe = ctx.related_describes.get(fk_target)
    return target_describe


def _match_from_fk_single(
    ctx: ResolveContext,
    fk_match: dict[str, Any],
    target_describe: dict[str, Any],
    fk_sub_soql: str,
    fk_sub_type: str | None,
    fk_sub_id_column: str | None,
) -> ResolverResult:
    """FK_ 経路で 1 件に確定したときの ``ResolvedMatch`` を作る。"""
    fk_rel = fk_match.get("relationshipName") or ""
    last_field_name = field_name_from_soql(fk_sub_soql)
    last_field = _find_field_by_name(target_describe, last_field_name)
    if last_field is None:
        last_field = {
            "name": last_field_name,
            "label": last_field_name,
            "type": fk_sub_type or "",
            "referenceTo": [],
            "relationshipName": "",
        }
    note_suffix_id_column = f"{fk_rel}.{fk_sub_id_column}" if fk_sub_id_column else None
    return ResolverResult(
        match=ResolvedMatch(
            note="FK_ から",
            owner=ctx.main_object,
            soql_chain=fk_rel,
            soql_name=f"{fk_rel}.{fk_sub_soql}",
            field={
                "name": fk_sub_soql,
                "label": last_field.get("label", last_field_name),
                "type": fk_sub_type or last_field.get("type") or "",
                "referenceTo": last_field.get("referenceTo", []),
                "relationshipName": last_field.get("relationshipName", ""),
            },
            terminal_describe=ctx.main_describe,
            id_column=fk_sub_id_column,
            note_suffix_id_column=note_suffix_id_column,
        )
    )


def _multi_from_fk(
    fk_matches: list[dict[str, Any]],
    fk_sub_soql: str,
) -> ResolverResult:
    """FK_ 経路で経路が解けた候補が複数のときの 「複数候補あり」 を組み立てる。"""
    resolved = [
        (fk_field.get("relationshipName") or "", fk_field.get("name") or "")
        for fk_field in fk_matches
    ]
    rendered = "複数候補あり: " + " / ".join(
        f"{fk_rel}.{fk_sub_soql}（{field_name}）" for fk_rel, field_name in resolved[:5]
    )
    return ResolverResult(multi_note=rendered)


# ═════════════════════════════════════════════════════════════════════
# 手順 1/2: 表示名で照合
# ═════════════════════════════════════════════════════════════════════


def _try_resolve_by_label(
    ctx: ResolveContext,
    column_key: str,
    normalized_label: str,
    target_object_hint: str | None,
    path_failure_note: str | None,
) -> ResolverResult:
    """主 → 関連 （浅い順） の順で表示名突き合わせをして 1 列を解決する。

    候補が 1 件なら採用、 2 件以上なら接頭辞で 1 件に絞れるかを試し、
    それでも絞れなければ 「複数候補あり」 で確定させる。 候補が 0 件の
    ときは ``path_failure_note`` か 「対応フィールドなし」 を失敗理由に
    する。 関連オブジェクトのフィールドインデックスは ``ResolveContext.related_indices``
    でメモ化して使い回す。
    """
    candidates = _collect_label_candidates(ctx, normalized_label, target_object_hint)
    if not candidates:
        return ResolverResult(failure_note=path_failure_note or "対応フィールドなし")

    if len(candidates) == 1:
        owner, soql_chain, field, terminal_describe = candidates[0]
        soql_name = f"{soql_chain}.{field['name']}" if soql_chain else field["name"]
        return ResolverResult(
            match=ResolvedMatch(
                note="関連オブジェクト経由" if soql_chain else "",
                owner=owner,
                soql_chain=soql_chain,
                soql_name=soql_name,
                field=field,
                terminal_describe=terminal_describe,
            )
        )

    filtered = _filter_label_candidates_by_prefix(ctx, candidates, column_key)
    if len(filtered) == 1:
        owner, soql_chain, field, terminal_describe = filtered[0]
        soql_name = f"{soql_chain}.{field['name']}" if soql_chain else field["name"]
        return ResolverResult(
            match=ResolvedMatch(
                note="列キーの接頭辞で絞り込み",
                owner=owner,
                soql_chain=soql_chain,
                soql_name=soql_name,
                field=field,
                terminal_describe=terminal_describe,
            )
        )

    pool = filtered if filtered else candidates
    rendered = _render_multiple_candidates(pool)
    return ResolverResult(multi_note=rendered)


def _collect_label_candidates(
    ctx: ResolveContext,
    normalized_label: str,
    target_object_hint: str | None,
) -> list[tuple[str, str, dict[str, Any], dict[str, Any]]]:
    """表示名が一致する候補を主 → 関連 （浅い順） で集める。

    戻り値の組は ``(所属オブジェクト名, 鎖文字列, field_dict, terminal_describe)`` 。
    鎖文字列が空のときは主オブジェクト由来。 「オブジェクト: 項目名」 形式で
    コロン前が見つかったときは ``target_object_hint`` で対象オブジェクトに
    絞り込む。
    """
    candidates: list[tuple[str, str, dict[str, Any], dict[str, Any]]] = []

    for main_field in ctx.field_index.get(normalized_label, []):
        if target_object_hint is not None and ctx.main_object != target_object_hint:
            continue
        candidates.append((ctx.main_object, "", main_field, ctx.main_describe))

    for chain in ctx.ordered_chains:
        related_describe = ctx.related_describes.get(chain.object_name)
        if related_describe is None:
            continue
        if target_object_hint is not None and chain.object_name != target_object_hint:
            continue
        related_index = _get_or_build_related_index(ctx, chain.object_name, related_describe)
        for related_field in related_index.get(normalized_label, []):
            candidates.append((chain.object_name, chain.chain, related_field, related_describe))

    return candidates


def _filter_label_candidates_by_prefix(
    ctx: ResolveContext,
    candidates: list[tuple[str, str, dict[str, Any], dict[str, Any]]],
    column_key: str,
) -> list[tuple[str, str, dict[str, Any], dict[str, Any]]]:
    """列キーの最初の ``.`` の前で候補を絞り込む。

    候補 ``(owner, soql_chain, field, terminal_describe)`` のうち、 列キー
    接頭辞と主オブジェクト名または鎖の最初のリレーション名 / 末端
    オブジェクト名が一致するものだけ残す。 一致が無いときは空リスト。
    """
    prefix = column_key.split(".", 1)[0]
    prefix_norm = normalize_identifier(prefix)
    filtered: list[tuple[str, str, dict[str, Any], dict[str, Any]]] = []
    for owner, soql_chain, cand_field, terminal_describe in candidates:
        if soql_chain:
            chain_obj = _find_chain(ctx, owner, soql_chain)
            if chain_obj is not None and _prefix_matches(chain_obj, prefix, prefix_norm):
                filtered.append((owner, soql_chain, cand_field, terminal_describe))
        else:
            if ctx.main_object and normalize_identifier(ctx.main_object) == prefix_norm:
                filtered.append((owner, soql_chain, cand_field, terminal_describe))
    return filtered


def _find_chain(
    ctx: ResolveContext,
    owner: str,
    soql_chain: str,
) -> _ChainCandidate | None:
    """``ctx.ordered_chains`` から ``(object_name, chain)`` が一致する鎖を返す。"""
    return next(
        (
            chain
            for chain in ctx.ordered_chains
            if chain.object_name == owner and chain.chain == soql_chain
        ),
        None,
    )


def _prefix_matches(
    chain_candidate: _ChainCandidate,
    prefix: str,
    prefix_norm: str | None = None,
) -> bool:
    """列キーの最初の ``.`` の前の語が、 候補の鎖と一致するか。

    一致条件: 大文字小文字・``_`` を無視して、 次のいずれかと等しい

    - 鎖の最初のリレーション名 （``"A.Owner"`` → ``"A"``）
    - 鎖の末端オブジェクト名 （``"What.Owner"`` → ``"User"``）

    末端オブジェクト名は鎖の行き先そのものなので、 鎖が深くなっても
    ``prefix`` が末端オブジェクトを指している場合は一致する。
    """
    if not chain_candidate.chain:
        return False
    parts = chain_candidate.chain.split(".")
    first_rel = parts[0]
    normalized_prefix = prefix_norm if prefix_norm is not None else normalize_identifier(prefix)
    if normalize_identifier(first_rel) == normalized_prefix:
        return True
    if normalize_identifier(chain_candidate.object_name) == normalized_prefix:
        return True
    return False


def _render_multiple_candidates(
    pool: list[tuple[str, str, dict[str, Any], dict[str, Any]]],
) -> str:
    """候補プールを 「複数候補あり: ...」 の文字列に整形する。"""
    sorted_pool = sorted(pool, key=lambda t: (0 if t[1] == "" else 1, t[0], t[1]))[:5]
    rendered = " / ".join(
        _format_one_candidate(owner, soql_chain, field)
        for owner, soql_chain, field, _ in sorted_pool
    )
    return "複数候補あり: " + rendered


def _format_one_candidate(
    owner: str,
    soql_chain: str,
    field: dict[str, Any],
) -> str:
    """複数候補ありの 1 件分の文字列を作る。

    主オブジェクトの項目は ``Name（Opportunity 商談名）`` のように表示名と
    オブジェクト名だけで添え、 関連経由の項目は ``Account.Name（Account
    取引先名）`` のように鎖つきの SOQL セグメントに続けてオブジェクト名 +
    表示名を添える。 SOQL 列名として ``Account.Name`` のようにそのまま
    貼れる形にする。
    """
    name = field.get("name", "")
    soql_segment = f"{soql_chain}.{name}" if soql_chain else name
    return f"{soql_segment}（{owner} {field.get('label', '')}）"
