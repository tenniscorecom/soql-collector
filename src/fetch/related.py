"""主オブジェクトから参照先を幅優先で掘り、 関連オブジェクトと参照の一覧を作る。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from src.fetch.models import RelatedNode, RelationRecord

logger = logging.getLogger(__name__)


def collect_related_objects(
    main_describe: dict[str, Any],
    fetcher: Callable[[str], dict[str, Any] | None],
    *,
    related_depth: int,
    related_max: int,
) -> tuple[list[RelatedNode], list[RelationRecord], list[str]]:
    """主オブジェクトから幅優先で関連オブジェクトを掘る。

    BFS は **「どのオブジェクトを取るか（nodes と段）」** だけを決め、
    ``visited`` に主オブジェクトを最初から入れる。 これで BFS 中に主
    オブジェクトが ``nodes`` に入らず、 describe の再取得も発生しない。
    ``RELATED_DEPTH`` 段まで掘り、 各段で frontier を全部消費して次の
    frontier を作る。 重複は **見つけた時点で** ``visited`` に入れ、
    ``RELATED_MAX`` の制限も frontier を作る段で「候補のまま」 打ち切る。
    打ち切られた候補は ``visited`` に既に入っているので同じ実行内では
    再試行しない（より浅い経路の 1 本だけが残る）。

    ``relations`` は BFS が **終わったあと** に別の処理で作る。 主オブジェクト
    と ``nodes`` の ``fields`` を主→ ``nodes`` の順（``nodes`` は段順） に
    たどり、 参照先が「取れたオブジェクト（主を含む）」 であれば全部
    1 行にする。 行の ``段`` は **子の段** （主 = 0）。 並びは「浅い親
    （主→段順） → 同じ親内では項目の出現順」。 ポリモーフィック参照は
    ``referenceTo`` のエントリごとに 1 行。 同じ ``(親, 項目, 子)`` は 1 行。
    取り損ねた子（上限で打ち切られた子・ describe に失敗した子） は
    親にも子にも行に出ない。 主オブジェクトへの参照（例: Account.TaskId
    → Task） や自己参照（例: Account.ParentId → Account） も **出る** 。

    戻り値の ``skipped`` は **重複を除いた** 出現順のリスト。 同じ名前が
    複数候補として出ても 1 回しか数えない。 警告文の「N 件」 もこの
    重複を除いた数。

    Args:
        main_describe: 主オブジェクトの describe（slim 前）。
        fetcher: オブジェクト名を取り、 ``describe`` を返す関数。 取れなかった
            ／ 失敗したときは ``None`` を返す （警告文は呼び出し側で積む）。
            同じ名前の 2 度目の呼び出しはキャッシュが効いて ``None`` か
            ``describe`` がそのまま返る （HTTP を 2 回打たない） 前提。
        related_depth: 何段目まで掘るか。 ``1`` なら今と同じ（1 段だけ）。
        related_max: 全体で取る関連オブジェクト数の上限。 全段の合計が
            ``related_max`` に達したら打ち切る。

    Returns:
        ``(nodes, relations, skipped)``
        ``nodes``: 深さごとに並んだ ``RelatedNode`` のリスト（**主オブジェクト
            自身は含まない**）。 出現順は「浅い親から先」 。 ``describe``
            は ``fetcher`` で埋めた状態 （``fetcher`` が ``None`` を返した
            ものは ``nodes`` に含めない — 上限内に収まったが describe 取得
            に失敗した分）。
        ``relations``: **取れたオブジェクト同士** で張られた親→子の参照
            リスト。 主＋関連オブジェクトが「お互いに参照している」 行を
            全部含む（主オブジェクトへの参照、 自己参照も出る）。 並びは
            「浅い親（主→段順） → 同じ親内では項目の出現順」 。 行の
            ``段`` は **子の段** （主 = 0）。 ポリモーフィック参照は
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
    # skipped の重複を除く （出現順を保つ）
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
    を超えた分は打ち切る。 戻り値の ``skipped`` は重複を除く前 (出現順) 。
    """
    # 主オブジェクトは「最初から取れた」 ものとして ``visited`` に入れて
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
    ``fields`` の出現順 (``_related_ref_pairs`` に従う) 。

    重複ルール: 過去段で ``visited`` 済みの子は再登録しない。 同じ
    (親, 項目, 子) は 1 度だけ。 別親から同じ子を指している場合は別行 (複数行可) 。
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
    candidates を空にする (この段は探索しない) 。
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

    ``visited`` は取得成功／失敗に関わらず更新する (同じ名前は再試行しない) 。
    同じ子が複数の親候補から重複して ``nodes`` と ``next_frontier`` に追加
    されるのを防ぐ。
    """
    next_frontier: list[tuple[str, dict[str, Any]]] = []
    seen_next: set[str] = set()
    for _, _, _, ref_name in candidates:
        describe = fetcher(ref_name)
        visited.add(ref_name)
        if describe is None:
            # 取れなかった。 ``visited`` は入れておく (再試行しない) 。
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

    主→ nodes の順 (nodes は段順) で ``fields`` をたどり、 参照先が「取れた
    オブジェクト (主を含む)」 なら 1 行ずつ出す。 同じ ``(親, 項目, 子)``
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
    ``referenceTo`` のエントリごとに展開（``relationshipName`` は同じ）。
    出現順を保ち、 重複は呼び出し側で ``visited`` により落とす。
    """
    return [
        (field["name"], field["relationshipName"], ref_name)
        for field in describe.get("fields", [])
        if field.get("relationshipName") and field.get("name")
        for ref_name in field.get("referenceTo") or []
    ]


def log_related_objects(
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
