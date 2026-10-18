"""関係図シートの行を作る（主オブジェクトごとの木を 1 行 1 経路に開く）。"""

from __future__ import annotations

import logging

from src import relation_diagram
from src.tables.constants import relation_diagram_columns

logger = logging.getLogger(__name__)


def build_relation_diagram_rows(
    diagrams: dict[str, relation_diagram.Diagram],
) -> list[dict[str, str]]:
    """``diagrams`` （主オブジェクト → ``Diagram``） から関係図シートの行を作る。

    1 行 = 木の 1 ノード（深さ優先）。 ノードの名前は、 その深さの「N 段目」の列に
    ``オブジェクト名（リレーション名）`` の形で入れる。 主オブジェクト自身の行は
    「N 段目」の列が全部空になる。 深い段は右の列へずれるので、 字下げした表に見える。
    """
    entries: list[tuple[str, int, str, str, str, str]] = []
    for main_object, diagram in diagrams.items():
        for depth, label, soql_hint, field_api, note, target in _iter_diagram_rows(
            diagram, depth=0
        ):
            entries.append(
                (main_object, depth, _node_cell_text(label, target), field_api, soql_hint, note)
            )

    max_depth = max((entry[1] for entry in entries), default=1)
    level_names = [f"{depth}段目" for depth in range(1, max(max_depth, 1) + 1)]
    rows: list[dict[str, str]] = []
    for main_object, depth, cell_text, field_api, soql_hint, note in entries:
        row = {"主オブジェクト": main_object}
        row.update(dict.fromkeys(level_names, ""))
        if depth >= 1:
            row[f"{depth}段目"] = cell_text
        row["参照項目API名"] = field_api
        row["SOQLでの書き方"] = soql_hint
        row["備考"] = note
        rows.append(row)
    return rows


def _node_cell_text(label: str, target: str) -> str:
    """``What → Account`` を ``Account（What）`` にする（矢印をやめ、 名前を先に出す）。"""
    if " → " not in label:
        return target
    relationship = label.split(" → ", 1)[0]
    return f"{target}（{relationship}）" if relationship else target


def relation_diagram_headers(rows: list[dict[str, str]]) -> tuple[str, ...]:
    """行に入っている「N 段目」の列数から、 関係図シートの見出しを決める。"""
    levels = sum(1 for key in (rows[0] if rows else {}) if key.endswith("段目"))
    return relation_diagram_columns(levels)


def _iter_diagram_rows(
    diagram: relation_diagram.Diagram,
    *,
    depth: int,
) -> list[tuple[int, str, str, str, str, str]]:
    """``Diagram`` の木を ``(段, 図, SOQLでの書き方, 参照項目API名, 注記, 行き先)``
    のリストに展開。"""
    out: list[tuple[int, str, str, str, str, str]] = []
    if depth == 0:
        out.append((0, diagram.root.label, "", "", "", diagram.main_object))
    for child in diagram.root.children:
        target = _extract_target(child.label)
        soql_hint = _build_soql_hint(diagram.main_object, child, diagram)
        note = child.note
        label_with_note = _format_label_with_note(child)
        out.append((depth + 1, label_with_note, soql_hint, child.field_api, note, target))
        if child.children and not child.note:
            for grandchild in child.children:
                _emit_descendants(out, child, grandchild, diagram, current_depth=depth + 2)
    return out


def _emit_descendants(
    out: list[tuple[int, str, str, str, str, str]],
    parent: relation_diagram.DiagramNode,
    child: relation_diagram.DiagramNode,
    diagram: relation_diagram.Diagram,
    *,
    current_depth: int,
) -> None:
    _ = parent
    target = _extract_target(child.label)
    note = child.note
    soql_hint = _build_soql_hint(diagram.main_object, child, diagram)
    label_with_note = _format_label_with_note(child)
    out.append((current_depth, label_with_note, soql_hint, child.field_api, note, target))
    if child.children and not child.note:
        for grandchild in child.children:
            _emit_descendants(out, child, grandchild, diagram, current_depth=current_depth + 1)


def _extract_target(label: str) -> str:
    if " → " in label:
        return label.split(" → ", 1)[1]
    return label


def _format_label_with_note(node: relation_diagram.DiagramNode) -> str:
    if node.note:
        return f"{node.label} {node.note}"
    return node.label


def _build_soql_hint(
    main_object: str,
    child: relation_diagram.DiagramNode,
    diagram: relation_diagram.Diagram,
) -> str:
    path: list[str] = []
    _collect_rel_path(diagram.root, child, path)
    if not path:
        return f"{main_object}.(項目)"
    return ".".join(path) + ".(項目)"


def _collect_rel_path(
    node: relation_diagram.DiagramNode,
    target: relation_diagram.DiagramNode,
    path: list[str],
) -> bool:
    for child in node.children:
        rel = child.label.split(" → ", 1)[0] if " → " in child.label else child.label
        if child is target:
            if rel:
                path.append(rel)
            return True
        if child.children:
            path.append(rel)
            if _collect_rel_path(child, target, path):
                return True
            path.pop()
    return False
