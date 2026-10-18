r"""``ReportRecord`` の ``relations`` から **主オブジェクト単位の木** を作り、
Excel 用の字下げツリーと Mermaid の ``flowchart LR`` を出力する。

設計の前提:

- 同じ主オブジェクトのレコードが複数あれば、 ``relations`` を ``(親, 項目,
  リレーション名, 子)`` で **マージ** する。 同じ辺が複数回現れても 1 本に。
- 深さ優先、 同じ親の中では **参照項目の出現順** （ ``describe`` の ``fields``
  の順 ） を優先する。 マージした ``fields`` の順は、 出現順を保つ
- 同じオブジェクトに 2 度目に着いたときは、 その先を展開せず
  ``（上に出ています）`` と書く。 循環・重複で木が無限にならないように。
  自己参照 （``Account.Parent`` → ``Account``） も同じ扱い
- ポリモーフィック参照は ``referenceTo`` のエントリごとに 1 行 （ 既存
  の ``RelationRecord`` 表現と同じ ）
- 段は ``RELATED_DEPTH=2`` で掘る。 経路上の describe は ``record.objects``
  に既載のものしか使わない （API を取り直さない）。 これで木の組み立ては
  ``records`` を渡せば決定的になる
- Mermaid はノード ID とラベルを分ける。 ノード名に記号 （``/`` ``__`` 等）
  があってもエラーにならないように ``_sanitize_node_id`` で ID を整える

公開するのは次の 2 つ:

- ``build_main_object_diagrams(records) -> dict[str, Diagram]``
  主オブジェクト名ごとに ``Diagram`` を組み立てる。 ``records`` が空か、
  主オブジェクトが取れていない record が無いときは空 dict
- ``Diagram.render_tree_lines() -> list[str]``
  Excel のセルに貼る字下げツリー （ 例: ``Task`` / ``├ What → Account`` /
  ``│ └ Owner → User`` / ``└ Owner → User`` ）
- ``Diagram.render_mermaid_lines() -> list[str]``
  Mermaid ``flowchart LR`` の本文 （先頭に ``flowchart LR`` 、 末尾に空行）
- ``write_main_object_diagrams(output_dir, records) -> tuple[Path, ...]``
  ``OUTPUT_DIR/関係図/{主オブジェクト}.md`` を書く。 既存はそのまま残し、
  今回取れた主オブジェクト分だけ上書きする
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.fetch import ReportRecord

logger = logging.getLogger(__name__)

# ``OUTPUT_DIR`` 配下に書く Mermaid ファイル群のサブディレクトリ名
DIAGRAM_SUBDIR = "関係図"

# Mermaid / Markdown の出力エンコード・改行
_ENCODING = "utf-8"
_NEWLINE = "\n"

# 字下げに使う罫線文字 （ASCII ではなく罫線素片） 。 Excel で見ても揃うように
# 全角を使う。 ``│`` は縦線、 ``├`` は T 字、 ``└`` は L 字、 ``─`` は横線
_TREE_VERTICAL = "│"
_TREE_T_RIGHT = "├"
_TREE_L_RIGHT = "└"
_TREE_HORIZONTAL = "─"


@dataclass(frozen=True)
class DiagramNode:
    """木の 1 ノード。 親への参照は ``parent`` で持ち、 ``children`` に子。

    ``label`` は Excel の字下げで 1 行に出す文字列 （例: ``What → Account``） 。
    ``note`` は 「上に出ています」 の印など、 1 行に続いて表示する注記。
    """

    label: str
    soql_hint: str
    field_api: str
    note: str = ""
    children: tuple[DiagramNode, ...] = ()


@dataclass(frozen=True)
class Diagram:
    """主オブジェクト 1 つ分の木と派生データ。"""

    main_object: str
    root: DiagramNode  # 常に 1 ノード （主オブジェクト自身）

    def render_tree_lines(self) -> list[str]:
        """Excel に貼る字下げツリーを行のリストで返す。

        並びは深さ優先、 同じ親の中は ``children`` の順。 先頭は主オブジェクト
        1 行、 末尾の空行は含めない。 ``│`` ``├`` ``└`` ``─`` の罫線で
        階層を表す。 ``│`` ``├`` ``└`` の前はインデント （全角スペース 4 個）
        を入れて Excel でセル内改行しても揃う形にする
        """
        lines: list[str] = [self.root.label]
        _render_children(lines, self.root.children, prefix="")
        return lines

    def render_mermaid_lines(self) -> list[str]:
        """Mermaid ``flowchart LR`` の本文を返す。 先頭は ``flowchart LR`` 、 末尾
        に空行 1 個。 ノード ID は ``_sanitize_node_id`` で整え、 ラベルは
        ``["…"]`` で囲む。 矢印は ``A -->|"parent"| B`` のように、 リレーション
        名をラベルにする （ポリモーフィックや上に出ています もそのまま）"""
        lines: list[str] = ["flowchart LR"]
        visited_nodes: set[str] = set()
        visited_edges: set[tuple[str, str, str]] = set()
        # 根 （主） を 1 行に
        root_id = _sanitize_node_id(self.root.label)
        lines.append(f'    {root_id}["{_escape_mermaid_label(self.root.label)}"]')
        visited_nodes.add(self.root.label)
        _render_mermaid_children(
            self.root,
            lines,
            parent_id=root_id,
            visited_nodes=visited_nodes,
            visited_edges=visited_edges,
        )
        return lines


# ── 構築 ─────────────────────────────────────────────────────────────────


def build_main_object_diagrams(
    records: Sequence[ReportRecord],
    *,
    max_depth: int = 3,
) -> dict[str, Diagram]:
    """``records`` から **主オブジェクトごと** に ``Diagram`` を作る。

    同じ主オブジェクトの ``relations`` を ``(親, 項目, リレーション名, 子)`` で
    マージし、 同じ辺が複数回現れても 1 本にする。 ``max_depth`` で掘る深さを
    制限する （既定 3 ＝ ``fetch.RELATED_DEPTH`` と同じ） 。 ``main_object``
    が無い record は無視する

    Args:
        records: ``ReportRecord`` 互換の ``relations`` を持つ iterable。
        max_depth: 何段目まで掘るか。 ``1`` なら 1 段だけ （主の参照先） 。
            ``fetch.RELATED_DEPTH`` と同じ既定値だが、 木の組み立て側は API を
            取らないので、 値の検証は呼び出し側で行う

    Returns:
        ``{主オブジェクト名: Diagram}`` の dict。 同じ主オブジェクトが複数
        record にあれば、 **マージした** ``Diagram`` が 1 つ返る。
    """
    merged_edges: dict[str, set[tuple[str, str, str]]] = {}
    merged_field_order: dict[str, list[tuple[str, str]]] = {}
    seen_field_order: dict[str, set[tuple[str, str]]] = {}
    main_objects: set[str] = set()

    for record in records:
        main_object = record.main_object
        if not main_object:
            continue
        main_objects.add(main_object)
        relations = record.relations
        for rel in relations:
            parent = rel.parent
            field_api = rel.field
            rel_name = rel.relationship_name
            child = rel.child
            if not (parent and field_api and child):
                continue
            edge_key = (parent, field_api, child)
            merged_edges.setdefault(main_object, set()).add(edge_key)
            # 親ごとに項目の出現順を保持 （リレーション名は表示用） 。
            # 同じ辺が既に登録されていても順序は変えない
            item = (field_api, rel_name or "")
            seen = seen_field_order.setdefault(main_object, set())
            if item not in seen:
                seen.add(item)
                merged_field_order.setdefault(main_object, []).append(item)

    diagrams: dict[str, Diagram] = {}
    for main_object in main_objects:
        edges = merged_edges.get(main_object, set())
        order = merged_field_order.get(main_object, [])
        root = _build_root(main_object, edges, order, max_depth)
        diagrams[main_object] = Diagram(main_object=main_object, root=root)
    return diagrams


def _build_root(
    main_object: str,
    edges: set[tuple[str, str, str]],
    field_order: list[tuple[str, str]],
    max_depth: int,
) -> DiagramNode:
    """``main_object`` を根とする ``DiagramNode`` を作る。 再帰で子を掘る。"""
    return _build_node(
        parent_name=main_object,
        label=main_object,
        soql_hint="",
        field_api_hint="",
        edges=edges,
        field_order=field_order,
        visited=frozenset({main_object}),
        max_depth=max_depth,
    )


def _build_node(
    *,
    parent_name: str,
    label: str,
    soql_hint: str,
    field_api_hint: str,
    edges: set[tuple[str, str, str]],
    field_order: list[tuple[str, str]],
    visited: frozenset[str],
    max_depth: int,
) -> DiagramNode:
    """再帰で ``DiagramNode`` を組み立てる。 ``visited`` は循環を防ぐための
    既に辿ったオブジェクト名の集合。 ``max_depth`` を超えると子を展開しない
    （ ``label`` は 「（上に出ています）」 に置換 ） 。"""
    if max_depth <= 1:
        # 1 段だけ掘る指定なら、 ここでは子を出さない
        return DiagramNode(
            label=label,
            soql_hint=soql_hint,
            field_api=field_api_hint,
        )

    children: list[DiagramNode] = []
    # ``field_order`` の順に並べ直す
    ordered_pairs: list[tuple[str, str, str]] = []
    seen_edges: set[tuple[str, str, str]] = set()
    for field_api, rel_name in field_order:
        for parent, fa, child in edges:
            if parent != parent_name or fa != field_api:
                continue
            edge_key = (parent, fa, child)
            if edge_key in seen_edges:
                continue
            seen_edges.add(edge_key)
            ordered_pairs.append((field_api, rel_name or "", child))

    next_visited = visited | {parent_name}
    for field_api, rel_name, child in ordered_pairs:
        if child in visited:
            # 循環・自己参照: 既に辿ったオブジェクトは 「（上に出ています）」 で打ち切り
            children.append(
                DiagramNode(
                    label=f"{rel_name} → {child}",
                    soql_hint=child,
                    field_api=field_api,
                    note="（上に出ています）",
                )
            )
            continue
        # 末端かどうか （子から先が出る辺があるか） 。
        child_has_outgoing = any(parent == child for parent, _, _ in edges)
        if not child_has_outgoing:
            # 末端。 子は表示だけで、 先は展開しない
            children.append(
                DiagramNode(
                    label=f"{rel_name} → {child}",
                    soql_hint=child,
                    field_api=field_api,
                )
            )
            continue
        # さらに掘り下げる
        child_label = f"{rel_name} → {child}"
        child_soql = f"{rel_name}.(項目)" if rel_name else child
        child_node = _build_node(
            parent_name=child,
            label=child_label,
            soql_hint=child_soql,
            field_api_hint=field_api,
            edges=edges,
            field_order=field_order,
            visited=next_visited | {child},
            max_depth=max_depth - 1,
        )
        children.append(child_node)

    return DiagramNode(
        label=label,
        soql_hint=soql_hint,
        field_api=field_api_hint,
        children=tuple(children),
    )


# ── 字下げツリーの描画 ───────────────────────────────────────────────────


def _render_children(
    lines: list[str],
    children: tuple[DiagramNode, ...],
    *,
    prefix: str,
) -> None:
    """``children`` を ``prefix`` の字下げで ``lines`` に追加する。

    各行の接頭辞は ``prefix`` + ``├ `` / ``└ `` で、 同じ親の中で最後の子は
    ``└`` を使う。 「最後の子」かどうかは ``children`` の長さで判定する
    """
    n = len(children)
    for idx, child in enumerate(children):
        is_last = idx == n - 1
        branch = f"{_TREE_L_RIGHT} " if is_last else f"{_TREE_T_RIGHT} "
        note = f" {child.note}" if child.note else ""
        lines.append(f"{prefix}{branch}{child.label}{note}")
        # 子の中にさらに子があれば、 字下げを 1 段深くする。
        # 「最後」だった場合は縦線 （「」） ではなくスペース 4 個で揃える
        if child.children:
            next_prefix = f"{prefix}{'    ' if is_last else f'{_TREE_VERTICAL}   '}"
            _render_children(lines, child.children, prefix=next_prefix)


# ── Mermaid 描画 ──────────────────────────────────────────────────────────


def _sanitize_node_id(name: str) -> str:
    """Mermaid のノード ID に使える形 （英数字と ``_`` だけ） に整える。"""
    chars: list[str] = []
    for ch in name:
        if ch.isalnum() or ch == "_":
            chars.append(ch)
        else:
            chars.append("_")
    sanitized = "".join(chars)
    if not sanitized:
        return "node"
    if sanitized[0].isdigit():
        sanitized = f"_{sanitized}"
    return sanitized


def _escape_mermaid_label(label: str) -> str:
    """Mermaid のラベル （``["…" ]`` の中身） で ``"`` を ``#quot;`` に換える。
    それ以外の記号は Mermaid の構文上そのまま書ける。 改行は ``<br>`` に直す
    """
    escaped = label.replace('"', "#quot;").replace("\n", "<br>")
    return escaped


def _render_mermaid_children(
    node: DiagramNode,
    lines: list[str],
    *,
    parent_id: str,
    visited_nodes: set[str],
    visited_edges: set[tuple[str, str, str]],
) -> None:
    """``node`` から出る子を Mermaid の ``A -->|"label"| B`` 形式で ``lines``
    に追加する。 多重辺は ``visited_edges`` で 1 本にする。 子ノードが
    「（上に出ています）」 のときは仮想ノード ``X_dup`` を 1 個用意して
    ラベルに印を残す
    """
    for child in node.children:
        # 子ノードのラベルを ``（上に出ています）`` に置換するか、
        # 通常の ``rel → obj`` かを判別
        rel_label = child.label.split(" → ", 1)[0] if " → " in child.label else child.label
        # 子ノードのオブジェクト名を取り出す
        if " → " in child.label:
            child_obj = child.label.split(" → ", 1)[1]
        else:
            child_obj = child.label
        # 重複辺を弾く
        edge_key = (node.label, child_obj, rel_label)
        if edge_key in visited_edges:
            continue
        visited_edges.add(edge_key)
        child_id = _sanitize_node_id(child_obj)
        # 既に登録済みのノードでも、 ラベルは最初の登録に揃える
        if child_obj not in visited_nodes:
            visited_nodes.add(child_obj)
            if child.note:
                # 「（上に出ています）」 のときは仮想ノードに印を入れる
                escaped_note = _escape_mermaid_label(child.note)
                lines.append(f'    {child_id}["{child_obj}<br>{escaped_note}"]')
            else:
                lines.append(f'    {child_id}["{_escape_mermaid_label(child_obj)}"]')
        # 辺の追加
        if child.note:
            # 上に出ていますは注記付きの辺で表現
            edge_label = _escape_mermaid_label(rel_label + " " + child.note)
            lines.append(f'    {parent_id} -->|"{edge_label}"| {child_id}')
        else:
            lines.append(f'    {parent_id} -->|"{_escape_mermaid_label(rel_label)}"| {child_id}')
        # 子がさらに子を持つ場合は再帰
        if child.children and not child.note:
            _render_mermaid_children(
                child,
                lines,
                parent_id=child_id,
                visited_nodes=visited_nodes,
                visited_edges=visited_edges,
            )


# ── ファイル書き出し ─────────────────────────────────────────────────────


def write_main_object_diagrams(
    output_dir: Any,
    diagrams: dict[str, Diagram],
    *,
    subdir: str = DIAGRAM_SUBDIR,
) -> tuple[Any, ...]:
    """``diagrams`` の各 ``Diagram`` を ``output_dir/{subdir}/{主オブジェクト}.md``
    に書く。 既存はそのまま残し、 今回渡された主オブジェクト分だけ上書きする。
    ``diagrams`` が空のときは何も書かない （ディレクトリも作らない）

    ``subdir`` は ``config.ini [FILES] DIAGRAM_DIR``（既定 ``関係図``）。
    """
    if not diagrams:
        return ()
    target_dir = output_dir / subdir
    target_dir.mkdir(parents=True, exist_ok=True)
    wrote: list[Any] = []
    for main_object, diagram in diagrams.items():
        path = target_dir / f"{main_object}.md"
        path.write_text(_render_markdown(diagram), encoding=_ENCODING, newline=_NEWLINE)
        wrote.append(path)
    if wrote:
        logger.info(
            "関係図の Markdown を書き出し: %s（%d 件）",
            target_dir,
            len(wrote),
        )
    return tuple(wrote)


def _render_markdown(diagram: Diagram) -> str:
    """1 つの ``Diagram`` を Markdown 文字列にする。 先頭に注記、
    Mermaid の ``flowchart LR`` を ``` フェンスで囲む。 Obsidian / GitHub で
    そのまま図になる
    """
    lines: list[str] = []
    lines.append(f"# 関係図: {diagram.main_object}")
    lines.append("")
    lines.append(
        "この図に無いオブジェクトは、 設定した段数 （``RELATED_DEPTH``） ・件数"
        "（``RELATED_MAX``） の範囲では主オブジェクトから参照項目で辿れない。"
        "子のリレーション （サブクエリで取るもの） は含まない"
    )
    lines.append("")
    lines.append("```mermaid")
    for line in diagram.render_mermaid_lines():
        lines.append(line)
    lines.append("```")
    lines.append("")
    return "\n".join(lines)


def render_inline_tree_for_record(
    record: ReportRecord,
    *,
    max_depth: int = 3,
) -> tuple[str, ...]:
    """``record.main_object`` の ``Diagram`` から字下げツリー部分を
    ``SOQL資料/{管理番号}.md`` に埋め込むためのタプルで返す。

    ``main_object`` が無い record や、 ``diagram`` が空のときは空タプル
    （埋め込まない） 。 他の主オブジェクト（同じ record 内）には影響しない
    ので、 ここでは ``record`` 単独で ``build_main_object_diagrams`` 相当を
    動かす
    """
    diagrams = build_main_object_diagrams([record], max_depth=max_depth)
    diagram = diagrams.get(record.main_object or "")
    if diagram is None:
        return ()
    return tuple(diagram.render_tree_lines())
