"""comken から移した、レポートの列 ⇔ Salesforce フィールド対応表の組み立て。

``report_type_candidates()`` がカスタムレポートタイプの ``$`` / ``@`` を
含む値から主オブジェクト候補を抽出し、 ``build_column_map()`` が渡された
describe から列対応表を作る。

``build_column_map()`` は主オブジェクトに加えて、関連オブジェクトの
describe と relations を受け取り、 関連オブジェクトを跨ぐ列も解決する。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# カスタムレポートタイプの ``reportType.type`` に前置される ``CustomEntity``
# プレースホルダを候補抽出時に除くための正規表現。
_CUSTOM_ENTITY_TOKEN_PATTERN = re.compile(r"\A(?:CustomEntity)+\Z")


def report_type_candidates(report_type: str) -> list[str]:
    """``reportType.type`` から主オブジェクトの候補を取り出す（純粋関数）。

    カスタムレポートタイプでは ``CustomEntity$Project__c`` や ``Account@Contact``
    のように ``$`` や ``@`` を含む値が入ることがある。 そのまま
    ``describe_object()`` に渡すと ``ValueError`` で止まるため、 ``$`` / ``@``
    で区切って候補のリストにし、 先頭から ``describe_object()`` が通るものを
    採る。

    ルール:

    - ``@`` が含まれるときは、 最初の ``@`` より左だけを使う
    - 残りを ``$`` で区切る
    - 空文字と、 ``CustomEntity`` を 1 回以上繰り返しただけのトークン
      （ ``CustomEntity`` / ``CustomEntityCustomEntity`` など） を除く
    - 重複は除き、 出現順を保つ
    - ``$`` も ``@`` も無ければ ``[report_type]``

    Examples:
        >>> report_type_candidates("CustomEntity$Project__c")
        ['Project__c']
        >>> report_type_candidates("Account@Contact")
        ['Account']
        >>> report_type_candidates("Opportunity")
        ['Opportunity']
        >>> report_type_candidates("CustomEntity$")
        []
    """
    if "@" in report_type:
        report_type = report_type.split("@", 1)[0]
    tokens = report_type.split("$")
    cleaned = [
        token for token in tokens if token and not _CUSTOM_ENTITY_TOKEN_PATTERN.fullmatch(token)
    ]
    seen: set[str] = set()
    unique: list[str] = []
    for token in cleaned:
        if token not in seen:
            seen.add(token)
            unique.append(token)
    return unique


def candidate_failure_reason(original: str, candidates: list[str]) -> str:
    """``$`` / ``@`` を含む ``reportType.type`` で候補を試したが全滅した
    （または候補が空だった） ときの理由文言を組み立てる。

    元の ``reportType.type`` の値と試した候補の一覧を添え、 「手動で確認」
    の手引きを入れる。 ``build_column_map()`` の備考列にそのまま埋め込む
    1 行分のテキスト。
    """
    if candidates:
        candidate_text = ", ".join(repr(candidate) for candidate in candidates)
        return (
            f"レポートタイプ {original!r} から主オブジェクト候補"
            f" {candidate_text} を順に確認しましたが、どれも Object Describe"
            " できず自動判定できません。手動で確認してください"
        )
    return (
        f"レポートタイプ {original!r} からは主オブジェクト候補を抽出できなかったため"
        "自動判定できません。手動で確認してください"
    )


def _normalize_label(label: str) -> str:
    """表示名の前後空白を落として小文字化する。

    Salesforce の表示名は前後に空白が入ることがあり、 また大小文字の
    ゆらぎは実フィールド側と一致しないことがあるため、 突き合わせの前に
    もう一段正規化する。 完全一致しなかったときのフォールバックは敢えて
    行わない — 不一致は「不一致」のまま残し、 誤った候補を押し付けない
    ことを優先するため。
    """
    return label.strip().lower()


def _normalize_identifier(value: str) -> str:
    """識別子の比較用に正規化する。 大文字小文字と ``_`` を無視する。"""
    return value.strip().lower().replace("_", "")


@dataclass(frozen=True)
class _ChainCandidate:
    """主オブジェクトからある関連オブジェクトまでの候補 1 本。

    ``chain``: ``.`` 区切りのリレーション名シーケンス （ 浅い順 ）。
    ``object_name``: 末端のオブジェクト名。
    ``depth``: 主オブジェクトから数えた段数。
    """

    chain: str
    object_name: str
    depth: int


def build_relation_chains(
    relations: list[Any],
    main_object: str | None,
) -> dict[str, list[_ChainCandidate]]:
    """``relations`` から「 主→ そのオブジェクトまでのリレーション鎖 」 を作る。

    関連オブジェクト名ごとに、 浅い順に並んだ ``_ChainCandidate`` のリスト
    を返す。 各オブジェクトに **複数の経路** がある場合は、 最も浅い経路を
    先頭にして、 同じ深さなら出現順を保つ。 主オブジェクト自身は対象外
    （ 候補に入れない ）。

    鎖は **リレーション名のシーケンス** で、 例: Task → Account （ rel="What" ）
    → User （ rel="Owner" ） のとき User の鎖は ``"What.Owner"`` 。

    Args:
        relations: ``fetch._collect_related_objects`` が返した
            ``RelationRecord`` のリスト。 各要素は ``parent`` / ``field`` /
            ``relationship_name`` / ``child`` / ``depth`` を持つ。
        main_object: 主オブジェクト名。 ``None`` のときは空 dict を返す。

    Returns:
        ``{子オブジェクト名: [_ChainCandidate, ...]}`` のマッピング。
    """
    if not main_object:
        return {}

    # 主→子の直エッジをまず集める
    adjacency: dict[str, list[tuple[str, str | None, str]]] = {}
    for rel in relations:
        if not hasattr(rel, "parent") or not hasattr(rel, "child"):
            continue
        parent = getattr(rel, "parent", None)
        child = getattr(rel, "child", None)
        rel_name = getattr(rel, "relationship_name", None)
        if not isinstance(parent, str) or not isinstance(child, str):
            continue
        if parent == main_object:
            # 主からの直エッジは depth=1 の chain になる
            adjacency.setdefault(parent, []).append((child, rel_name, child))

    # より深い段も集める （ 内部の関連 ）
    for rel in relations:
        if not hasattr(rel, "parent") or not hasattr(rel, "child"):
            continue
        parent = getattr(rel, "parent", None)
        child = getattr(rel, "child", None)
        rel_name = getattr(rel, "relationship_name", None)
        if not isinstance(parent, str) or not isinstance(child, str):
            continue
        if parent != main_object:
            adjacency.setdefault(parent, []).append((child, rel_name, child))

    # BFS で主から各オブジェクトまでの鎖を計算
    visited: dict[str, str] = {}  # 子 -> chain (リレーション名鎖)
    visited_depth: dict[str, int] = {}  # 子 -> depth
    queue: list[tuple[str, str, int]] = [(main_object, "", 0)]
    visited[main_object] = ""
    visited_depth[main_object] = 0
    head = 0
    while head < len(queue):
        current, chain, depth = queue[head]
        head += 1
        for child, rel_name, _ in adjacency.get(current, []):
            if child in visited:
                continue
            if not isinstance(rel_name, str) or not rel_name:
                # リレーション名が空の辺は鎖に使わない
                continue
            new_chain = f"{chain}.{rel_name}" if chain else rel_name
            visited[child] = new_chain
            visited_depth[child] = depth + 1
            queue.append((child, new_chain, depth + 1))

    # 子→ candidate のリストに展開 （ 1 件だけ。 浅い経路のみ採用 ）
    result: dict[str, list[_ChainCandidate]] = {}
    for child, chain in visited.items():
        if child == main_object:
            continue
        depth = visited_depth[child]
        result[child] = [_ChainCandidate(chain=chain, object_name=child, depth=depth)]

    # 主オブジェクト自身は含めない
    result.pop(main_object, None)

    return result


def _build_field_index(describe: object) -> dict[str, list[dict[str, Any]]]:
    """Object Describe の ``fields`` を ``{正規化表示名: [field, ...]}`` に組み立てる。

    同じ表示名を持つフィールドが複数ある場合は最初に見つかった 1 件だけを
    選ばず **リストのまま** 残す。 呼び出し側で件数を判定し、 1 件なら
    採用、 2 件以上なら「複数候補あり」として注記する。

    Object Describe が ``fields`` を返さなかった場合（壊れたレスポンス等）
    は空の辞書を返し、 全列が「対応フィールドなし」になる。 例外にはしない
    （ ``build_column_map()`` のポリシーと揃えるため ）。
    """
    fields = describe.get("fields", []) if isinstance(describe, dict) else []
    index: dict[str, list[dict[str, Any]]] = {}
    for field in fields:
        if not isinstance(field, dict):
            continue
        name = field.get("name")
        label = field.get("label")
        field_type = field.get("type")
        if not isinstance(name, str) or not isinstance(label, str):
            continue
        index.setdefault(_normalize_label(label), []).append(
            {
                "name": name,
                "type": field_type if isinstance(field_type, str) else "",
                "referenceTo": field.get("referenceTo", []),
                "relationshipName": field.get("relationshipName"),
            }
        )
    return index


def _filter_only_columns(report_filters: object) -> list[str]:
    """``reportFilters`` にだけ現れる列キーを返す（SELECT には出ない列）。"""
    if not isinstance(report_filters, list):
        return []
    return [
        column_key
        for report_filter in report_filters
        if isinstance(report_filter, dict)
        and isinstance(column_key := report_filter.get("column"), str)
    ]


def _grouping_columns(report_metadata: dict[str, Any]) -> list[str]:
    """``groupingsDown`` / ``groupingsAcross`` （ ``SUMMARY`` / ``MATRIX`` ）の列キー。"""
    columns = []
    for grouping_key in ("groupingsDown", "groupingsAcross"):
        for grouping in report_metadata.get(grouping_key, []) or []:
            if isinstance(grouping, dict) and isinstance(grouping.get("name"), str):
                columns.append(grouping["name"])
    return columns


def _aggregate_field_columns(aggregates: object) -> list[str]:
    """``aggregates`` の集計対象列キーを返す。

    集計キーは ``"s!Amount"`` のように「関数!列キー」の形。 ``"!"`` が無い
    ものは列キーとして扱えないため無視する。
    """
    if not isinstance(aggregates, list):
        return []
    return [
        aggregate_key.split("!", 1)[-1]
        for aggregate_key in aggregates
        if isinstance(aggregate_key, str) and "!" in aggregate_key
    ]


def collect_describable_columns(report_metadata: dict[str, Any]) -> list[str]:
    """``build_column_map()`` が解決を試みる列キーの一覧を組み立てる（重複除去済み）。

    ``detailColumns`` （SELECT に出す列） に加え、 SELECT には出ないが
    ``reportFilters`` だけで使う列、 ``SUMMARY`` / ``MATRIX`` の
    ``groupingsDown`` / ``groupingsAcross`` のグルーピング列、 ``aggregates``
    の集計対象列も含める。
    """
    columns = list(report_metadata.get("detailColumns", []))
    for column_key in (
        _filter_only_columns(report_metadata.get("reportFilters"))
        + _grouping_columns(report_metadata)
        + _aggregate_field_columns(report_metadata.get("aggregates"))
    ):
        if column_key not in columns:
            columns.append(column_key)
    return columns


def _collect_column_info(metadata: dict[str, Any]) -> dict[str, Any]:
    """``build_column_map()`` の表示名引き当てに使う列情報を組み立てる。

    グルーピング列・集計列の表示名は ``detailColumnInfo`` ではなく
    ``groupingColumnInfo`` / ``aggregateColumnInfo`` 側に入っているため、
    同じ列キー空間としてマージする（キーの重複は無い前提）。
    """
    extended_metadata: dict[str, Any] = (
        metadata.get("reportExtendedMetadata", {}) if isinstance(metadata, dict) else {}
    )
    column_info: dict[str, Any] = dict(extended_metadata.get("detailColumnInfo", {}) or {})
    for info_key in ("groupingColumnInfo", "aggregateColumnInfo"):
        info = extended_metadata.get(info_key)
        if isinstance(info, dict):
            column_info.update(info)
    return column_info


def _prefix_matches(chain_candidate: _ChainCandidate, prefix: str) -> bool:
    """列キーの最初の ``.`` の前の語が、 候補の鎖と一致するか。

    一致条件: 大文字小文字・``_`` を無視して、 次のいずれかと等しい

    - 鎖の最初のリレーション名 （ ``"A.Owner"`` → ``"A"`` ）
    - 鎖の末端オブジェクト名 （ ``"What.Owner"`` → ``"User"`` ）

    末端オブジェクト名は鎖の行き先そのものなので、 鎖が深くなっても
    ``prefix`` が末端オブジェクトを指している場合は一致する。
    """
    if not chain_candidate.chain:
        return False
    parts = chain_candidate.chain.split(".")
    first_rel = parts[0]
    normalized_prefix = _normalize_identifier(prefix)
    if _normalize_identifier(first_rel) == normalized_prefix:
        return True
    if _normalize_identifier(chain_candidate.object_name) == normalized_prefix:
        return True
    return False


def build_column_map(
    metadata: dict[str, Any],
    main_describe: dict[str, Any] | None,
    reason: str | None,
    *,
    related_describes: dict[str, dict[str, Any]] | None = None,
    relations: list[Any] | None = None,
    main_object: str | None = None,
) -> list[dict[str, str]]:
    """渡された主オブジェクトの describe から列対応表を作る（純粋関数）。

    関連オブジェクトの describe （ 名前 → describe dict ） と relations を
    渡すと、 関連を跨ぐ表示名も解決する。

    ``main_describe`` が ``None`` のとき、 もしくは ``reason`` が ``None`` でない
    ときは、 全列を ``"(不明)"`` にして備考に ``reason`` を入れる。 一致する
    フィールドが無い列は ``"対応フィールドなし"`` 、 候補が 2 件以上の列は
    誤った候補を押し付けず「複数候補あり」にする。

    解決アルゴリズム （ 列キーごとに、 列表示名 ラベル一致 ）:

    1. 主オブジェクトで、 表示名（ 前後空白除去・小文字 ） が **一意に**
       一致すればそれを採用 （ 今までの挙動 ）
    2. 一致しない列は、 取れた関連オブジェクト（ 段の浅い順 ） の項目
       ラベルとも突き合わせる。 候補の SOQL 名は「 主オブジェクトから
       そのオブジェクトまでのリレーション名の鎖 + ``.`` + 項目 API 名 」
       （ 例: ``Account.Owner.Name`` ）。 鎖は ``relations`` から最も浅い
       1 本を選ぶ
    3. 候補が 1 つなら採用して備考に「 関連オブジェクト経由 」。 複数
       あれば、 列キーの最初の ``.`` の前の語が候補の鎖の最初のリレーション
       名またはオブジェクト名と一致する候補だけに絞り、 1 つになったら採用
       （ 備考「 列キーの接頭辞で絞り込み 」 ）。 それでも複数あれば 対応は
       ``"(不明)"`` のまま、 備考に「複数候補あり: 鎖+項目（ 最大 5 件、
       浅い順 ）」。 無ければ「対応フィールドなし」

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

    Returns:
        列対応表の行のリスト。 各行は ``列キー`` / ``表示名`` /
        ``所属オブジェクト`` / ``対応フィールドAPI名`` / ``型`` /
        ``参照先オブジェクト`` / ``リレーション名`` / ``備考`` のキーを
        持つ dict。
    """
    related_describes = related_describes or {}
    relations = relations or []

    report_metadata = metadata.get("reportMetadata", {}) if isinstance(metadata, dict) else {}
    columns = collect_describable_columns(report_metadata)
    column_info = _collect_column_info(metadata)
    field_index = _build_field_index(main_describe) if main_describe is not None else None

    # 鎖を事前計算
    chains_by_object = build_relation_chains(relations, main_object)

    # 関連オブジェクトの鎖を段の浅い順に並べたもの
    ordered_chains: list[_ChainCandidate] = []
    for obj_name in chains_by_object:
        for chain in chains_by_object[obj_name]:
            ordered_chains.append(chain)
    ordered_chains.sort(key=lambda c: (c.depth, c.object_name))

    rows: list[dict[str, str]] = []
    for column_key in columns:
        info = column_info.get(column_key, {}) if isinstance(column_info, dict) else {}
        label = info.get("label", column_key) if isinstance(info, dict) else column_key
        normalized_label = _normalize_label(label)
        row = {
            "列キー": column_key,
            "表示名": label,
            "所属オブジェクト": main_object or "",
            "対応フィールドAPI名": "(不明)",
            "型": "",
            "参照先オブジェクト": "",
            "リレーション名": "",
            "備考": "",
        }

        if field_index is None or reason is not None:
            row["備考"] = reason or ""
            rows.append(row)
            continue

        # 候補を全部集める （ 主 → 関連、 関連は浅い順 ）
        # 各候補は (所属オブジェクト名, 鎖文字列, field_dict) の組
        all_candidates: list[tuple[str, str, dict[str, Any], bool]] = []
        # bool は "main に由来するか"
        main_matches = field_index.get(normalized_label, [])
        for field in main_matches:
            all_candidates.append((main_object or "", "", field, True))

        # 関連から
        for chain in ordered_chains:
            related_describe = related_describes.get(chain.object_name)
            if not isinstance(related_describe, dict):
                continue
            related_index = _build_field_index(related_describe)
            related_matches = related_index.get(normalized_label, [])
            for field in related_matches:
                soql_chain = chain.chain
                all_candidates.append((chain.object_name, soql_chain, field, False))

        if not all_candidates:
            row["備考"] = "対応フィールドなし"
            rows.append(row)
            continue

        if len(all_candidates) == 1:
            owner, soql_chain, field, from_main = all_candidates[0]
            row["所属オブジェクト"] = owner
            if from_main:
                row["対応フィールドAPI名"] = field["name"]
                row["型"] = field["type"]
                # 参照先 / リレーション名は主オブジェクトの項目のもの
                ref_to = field.get("referenceTo")
                if isinstance(ref_to, list) and ref_to:
                    row["参照先オブジェクト"] = "|".join(
                        str(item) for item in ref_to if isinstance(item, str) and item
                    )
                rel_name = field.get("relationshipName")
                if isinstance(rel_name, str):
                    row["リレーション名"] = rel_name
                row["備考"] = ""
            else:
                row["対応フィールドAPI名"] = (
                    f"{soql_chain}.{field['name']}" if soql_chain else field["name"]
                )
                row["型"] = field["type"]
                # 参照先 / リレーション名は関連オブジェクト側の項目のもの
                ref_to = field.get("referenceTo")
                if isinstance(ref_to, list) and ref_to:
                    row["参照先オブジェクト"] = "|".join(
                        str(item) for item in ref_to if isinstance(item, str) and item
                    )
                rel_name = field.get("relationshipName")
                if isinstance(rel_name, str):
                    row["リレーション名"] = rel_name
                row["備考"] = "関連オブジェクト経由"
            rows.append(row)
            continue

        # 2 件以上: 列キーの最初の ``.`` の前でフィルタ
        prefix = column_key.split(".", 1)[0]
        filtered: list[tuple[str, str, dict[str, Any], bool, _ChainCandidate | None]] = []
        for owner, soql_chain, field, from_main in all_candidates:
            if from_main:
                # 主由来は鎖なし。 接頭辞フィルタは main_object と比較
                if main_object and _normalize_identifier(prefix) == _normalize_identifier(
                    main_object
                ):
                    filtered.append((owner, soql_chain, field, from_main, None))
                continue
            # 関連由来: 鎖から比較
            chain_obj = next(
                (c for c in ordered_chains if c.object_name == owner and c.chain == soql_chain),
                None,
            )
            if chain_obj is not None and _prefix_matches(chain_obj, prefix):
                filtered.append((owner, soql_chain, field, from_main, chain_obj))

        if len(filtered) == 1:
            owner, soql_chain, field, from_main, _ = filtered[0]
            row["所属オブジェクト"] = owner
            if from_main:
                row["対応フィールドAPI名"] = field["name"]
                row["型"] = field["type"]
                ref_to = field.get("referenceTo")
                if isinstance(ref_to, list) and ref_to:
                    row["参照先オブジェクト"] = "|".join(
                        str(item) for item in ref_to if isinstance(item, str) and item
                    )
                rel_name = field.get("relationshipName")
                if isinstance(rel_name, str):
                    row["リレーション名"] = rel_name
                row["備考"] = ""
            else:
                row["対応フィールドAPI名"] = (
                    f"{soql_chain}.{field['name']}" if soql_chain else field["name"]
                )
                row["型"] = field["type"]
                ref_to = field.get("referenceTo")
                if isinstance(ref_to, list) and ref_to:
                    row["参照先オブジェクト"] = "|".join(
                        str(item) for item in ref_to if isinstance(item, str) and item
                    )
                rel_name = field.get("relationshipName")
                if isinstance(rel_name, str):
                    row["リレーション名"] = rel_name
                row["備考"] = "列キーの接頭辞で絞り込み"
            rows.append(row)
            continue

        # それでも複数 （ またはフィルタで 0 件 ）
        if not filtered:
            # フィルタで 0 件なら、 全部 「(不明)」 扱いで複数の候補を並べる
            filtered = [
                (owner, soql_chain, field, from_main, chain_obj)
                for owner, soql_chain, field, from_main in all_candidates
                for chain_obj in (
                    [None]
                    if from_main
                    else [
                        c
                        for c in ordered_chains
                        if c.object_name == owner and c.chain == soql_chain
                    ]
                )
            ]
        # 候補を最大 5 件、 浅い順 （ 主→関連の順 ） で並べる
        sorted_filtered = sorted(
            filtered,
            key=lambda t: (0 if t[3] else 1, t[0], t[1]),
        )[:5]
        candidate_texts = []
        for owner, soql_chain, field, from_main, _ in sorted_filtered:
            if from_main:
                candidate_texts.append(f"{owner}.{field['name']}")
            else:
                candidate_texts.append(
                    f"{soql_chain}.{field['name']}" if soql_chain else field["name"]
                )
        row["備考"] = "複数候補あり: " + ", ".join(candidate_texts)
        rows.append(row)
    return rows
