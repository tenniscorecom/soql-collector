"""主オブジェクトから関連オブジェクトまでのリレーション鎖を組み立てる。

``relations`` （``RelationRecord`` のリスト） から浅さ順に 1 本ずつ鎖を選び、
主→関連の経路でレポート列を辿れるようにする。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.mapping.owner import OWNER_RELATIONSHIP_NAME, OWNER_USER_OBJECT

if TYPE_CHECKING:
    from src.fetch import RelationRecord


@dataclass(frozen=True)
class ChainCandidate:
    """主オブジェクトからある関連オブジェクトまでの候補 1 本。

    ``chain``: ``.`` 区切りのリレーション名シーケンス （浅い順） 。
    ``object_name``: 末端のオブジェクト名。
    ``depth``: 主オブジェクトから数えた段数。
    """

    chain: str
    object_name: str
    depth: int


def build_relation_chains(
    relations: list[RelationRecord],
    main_object: str | None,
) -> dict[str, list[ChainCandidate]]:
    """``relations`` から「主→そのオブジェクトまでのリレーション鎖」 を作る。

    関連オブジェクト名ごとに、 浅い順に並んだ ``ChainCandidate`` のリスト
    を返す。 各オブジェクトに **複数の経路** がある場合は、 最も浅い経路を
    先頭にして、 同じ深さなら出現順を保つ。 主オブジェクト自身は対象外
    （候補に入れない） 。

    鎖は **リレーション名のシーケンス** で、 例: Task → Account （rel="What"）
    → User （rel="Owner"） のとき User の鎖は ``"What.Owner"`` 。

    Args:
        relations: ``fetch._collect_related_objects`` が返した
            ``RelationRecord`` のリスト。 各要素は ``parent`` / ``field`` /
            ``relationship_name`` / ``child`` / ``depth`` を持つ。
        main_object: 主オブジェクト名。 ``None`` のときは空 dict を返す。

    Returns:
        ``{子オブジェクト名: [ChainCandidate, ...]}`` のマッピング。
    """
    if not main_object:
        return {}

    # 主→子の直エッジをまず集める
    adjacency: dict[str, list[tuple[str, str | None, str]]] = {}
    for rel in relations:
        parent = rel.parent
        child = rel.child
        rel_name = rel.relationship_name
        if not (parent and child):
            continue
        if parent == main_object:
            # 主からの直エッジは depth=1 の chain になる
            adjacency.setdefault(parent, []).append((child, rel_name, child))

    # より深い段も集める （内部の関連）
    for rel in relations:
        parent = rel.parent
        child = rel.child
        rel_name = rel.relationship_name
        if not (parent and child):
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
        for child, rel_name, _ in _prefer_owner_edge(adjacency.get(current, [])):
            if child in visited:
                continue
            if not rel_name:
                # リレーション名が空の辺は鎖に使わない
                continue
            new_chain = f"{chain}.{rel_name}" if chain else rel_name
            visited[child] = new_chain
            visited_depth[child] = depth + 1
            queue.append((child, new_chain, depth + 1))

    # 子→ candidate のリストに展開 （1 件だけ。 浅い経路のみ採用）
    result: dict[str, list[ChainCandidate]] = {}
    for child, chain in visited.items():
        if child == main_object:
            continue
        depth = visited_depth[child]
        result[child] = [ChainCandidate(chain=chain, object_name=child, depth=depth)]

    # 主オブジェクト自身は含めない
    result.pop(main_object, None)

    return result


def _prefer_owner_edge(
    edges: list[tuple[str, str | None, str]],
) -> list[tuple[str, str | None, str]]:
    """同じ子オブジェクトへの辺が複数あるとき、 ``Owner`` の辺を先にする。

    主オブジェクトから User への鎖が ``CreatedBy`` / ``LastModifiedBy`` /
    ``Owner`` のように複数あるときは ``Owner`` を採る。 describe の項目順
    （ ``fields`` の並び） で結果が変わらないよう、 ここで並べ替える。

    Args:
        edges: ``(子オブジェクト名, リレーション名, 子オブジェクト名)`` の辺のリスト。

    Returns:
        並べ替えた辺のリスト。 子オブジェクトの並びは元のまま。
    """
    if not edges:
        return edges
    grouped: dict[str, list[tuple[str, str | None, str]]] = {}
    child_order: list[str] = []
    for edge in edges:
        child = edge[0]
        if child not in grouped:
            grouped[child] = []
            child_order.append(child)
        grouped[child].append(edge)
    ordered: list[tuple[str, str | None, str]] = []
    for child in child_order:
        group = grouped[child]
        if child != OWNER_USER_OBJECT:
            ordered.extend(group)
            continue
        owner_edges = [edge for edge in group if edge[1] == OWNER_RELATIONSHIP_NAME]
        other_edges = [edge for edge in group if edge[1] != OWNER_RELATIONSHIP_NAME]
        ordered.extend([*owner_edges, *other_edges])
    return ordered
