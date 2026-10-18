"""``src.relation_diagram`` の単体テスト。

``ReportRecord`` を直接作って、 主オブジェクト単位の木が要件どおり組み立て
られるかを確かめる。 偽 ``relations`` だけでネットワークには出ない
"""

from __future__ import annotations

from pathlib import Path

from src.fetch import RelationRecord, ReportRecord
from src.relation_diagram import (
    Diagram,
    build_main_object_diagrams,
    render_inline_tree_for_record,
    write_main_object_diagrams,
)


def _make_record(
    *,
    key: str,
    main_object: str | None,
    relations: tuple[RelationRecord, ...],
) -> ReportRecord:
    """テスト用の最小 ``ReportRecord`` を作る。"""
    return ReportRecord(
        key=key,
        summary="テスト",
        report_id="00O000000000001",
        url="https://example.my.salesforce.com/lightning/r/Report/00O000000000001/view",
        main_object=main_object,
        report={"reportMetadata": {}, "reportExtendedMetadata": {}},
        objects={},
        relations=relations,
        column_map=[],
        warnings=(),
    )


# ── 構築 ──────────────────────────────────────────────────────────────────


def test_empty_records_returns_empty_dict() -> None:
    """``records`` が空なら空 dict。"""
    assert build_main_object_diagrams([]) == {}


def test_record_without_main_object_is_ignored() -> None:
    """``main_object`` が無い record は無視する。"""
    record = _make_record(
        key="1",
        main_object=None,
        relations=(
            RelationRecord(
                parent="Task", field="WhatId", relationship_name="What", child="Account", depth=1
            ),
        ),
    )
    assert build_main_object_diagrams([record]) == {}


def test_self_reference_becomes_already_above() -> None:
    """自己参照 （``Account.ParentId`` → ``Account``） は 「（上に出ています）」
    で打ち切り、 木の先は展開しない。
    """
    record = _make_record(
        key="1",
        main_object="Account",
        relations=(
            RelationRecord(
                parent="Account",
                field="ParentId",
                relationship_name="Parent",
                child="Account",
                depth=1,
            ),
        ),
    )
    diagrams = build_main_object_diagrams([record], max_depth=2)
    diagram = diagrams["Account"]
    tree = diagram.render_tree_lines()
    assert tree[0] == "Account"
    # 1 つだけ子が出て、 注記付き
    joined = "\n".join(tree)
    assert "Parent → Account" in joined
    assert "（上に出ています）" in joined
    # 自己参照の辺は 1 本だけ （主の行 + 「上に出ています」 注記の辺で 1 回）
    assert joined.count("Parent → Account") == 1


def test_cycle_via_indirection_is_collapsed() -> None:
    """間接的な循環 （Task → Account → Task） は 「（上に出ています）」 で止まる。"""
    record = _make_record(
        key="1",
        main_object="Task",
        relations=(
            RelationRecord(
                parent="Task", field="WhatId", relationship_name="What", child="Account", depth=1
            ),
            # Account → Task は通常無いが、 循環デモ用に逆辺を入れる
            RelationRecord(
                parent="Account",
                field="AccountTask__c",
                relationship_name="AccountTask",
                child="Task",
                depth=2,
            ),
        ),
    )
    diagrams = build_main_object_diagrams([record], max_depth=3)
    diagram = diagrams["Task"]
    joined = "\n".join(diagram.render_tree_lines())
    assert "What → Account" in joined
    # Account の子に Task が再登場するときは 「（上に出ています）」 で打ち切る
    assert "（上に出ています）" in joined


def test_polymorphic_each_reference_to_gets_one_line() -> None:
    """ポリモーフィック参照 （``What → Account`` と ``What → Opportunity``） は
    ``referenceTo`` のエントリごとに 1 行ずつ出る（既存 ``RelationRecord`` と
    同じ） 。"""
    record = _make_record(
        key="1",
        main_object="Task",
        relations=(
            RelationRecord(
                parent="Task", field="WhatId", relationship_name="What", child="Account", depth=1
            ),
            RelationRecord(
                parent="Task",
                field="WhatId",
                relationship_name="What",
                child="Opportunity",
                depth=1,
            ),
        ),
    )
    diagrams = build_main_object_diagrams([record], max_depth=2)
    tree = "\n".join(diagrams["Task"].render_tree_lines())
    assert "What → Account" in tree
    assert "What → Opportunity" in tree


def test_max_depth_limits_recursion() -> None:
    """``max_depth`` を超える辺は展開しない。"""
    record = _make_record(
        key="1",
        main_object="A",
        relations=(
            RelationRecord(parent="A", field="B__c", relationship_name="B", child="C", depth=1),
            RelationRecord(parent="C", field="D__c", relationship_name="D", child="E", depth=2),
        ),
    )
    diagrams = build_main_object_diagrams([record], max_depth=2)
    tree = "\n".join(diagrams["A"].render_tree_lines())
    # C までは出るが、 E は出ない
    assert "B → C" in tree
    assert "D → E" not in tree


def test_multiple_records_same_main_object_merge_edges() -> None:
    """同じ主オブジェクトの record が複数あれば ``relations`` がマージされ、
    同じ辺は重複しない。
    """

    def rel(child: str, name: str, depth: int = 1) -> RelationRecord:
        return RelationRecord(
            parent="Task",
            field=f"{name}Id",
            relationship_name=name,
            child=child,
            depth=depth,
        )

    records = [
        _make_record(
            key="1", main_object="Task", relations=(rel("Account", "What"), rel("Contact", "Who"))
        ),
        _make_record(
            key="2", main_object="Task", relations=(rel("Account", "What"), rel("Lead", "Who"))
        ),
    ]
    diagrams = build_main_object_diagrams(records, max_depth=2)
    tree = "\n".join(diagrams["Task"].render_tree_lines())
    # ``What → Account`` は 2 record に現れても 1 本
    assert tree.count("What → Account") == 1
    # ``Who → Contact`` / ``Who → Lead`` の両方が別々に出る
    assert "Who → Contact" in tree
    assert "Who → Lead" in tree


# ── 字下げツリー ─────────────────────────────────────────────────────────


def test_tree_lines_use_box_drawing_and_order() -> None:
    """字下げは罫線で、 同じ親の中は ``field_order`` の順。"""
    record = _make_record(
        key="1",
        main_object="Task",
        relations=(
            RelationRecord(
                parent="Task", field="WhoId", relationship_name="Who", child="Contact", depth=1
            ),
            RelationRecord(
                parent="Task", field="WhatId", relationship_name="What", child="Account", depth=1
            ),
            RelationRecord(
                parent="Account", field="OwnerId", relationship_name="Owner", child="User", depth=2
            ),
        ),
    )
    diagrams = build_main_object_diagrams([record], max_depth=3)
    lines = diagrams["Task"].render_tree_lines()
    assert lines[0] == "Task"
    # ``├ `` と ``└ `` の罫線で深さを出す
    assert any("├ " in line for line in lines)
    assert any("└ " in line for line in lines)
    # 直下の子は字下げ （全角スペース 4 個相当） される
    for line in lines[1:]:
        # 先頭は罫線の幅 （``│`` ``├`` ``└`` の前） を持つ
        assert line.startswith(("├ ", "└ ", "    ├ ", "    └ ", "│   ├ ", "│   └ "))


# ── Mermaid 出力 ─────────────────────────────────────────────────────────


def test_mermaid_lines_start_with_flowchart_lr() -> None:
    diagrams = build_main_object_diagrams(
        [
            _make_record(key="1", main_object="Task", relations=()),
        ]
    )
    lines = diagrams["Task"].render_mermaid_lines()
    assert lines[0] == "flowchart LR"


def test_mermaid_includes_arrow_with_relationship_name() -> None:
    """``A -->|"What"| B`` のように、 リレーション名が矢印のラベルに入る。"""
    record = _make_record(
        key="1",
        main_object="Task",
        relations=(
            RelationRecord(
                parent="Task", field="WhatId", relationship_name="What", child="Account", depth=1
            ),
        ),
    )
    diagrams = build_main_object_diagrams([record], max_depth=2)
    lines = diagrams["Task"].render_mermaid_lines()
    joined = "\n".join(lines)
    # ノード ID とラベルが出る
    assert 'Task["Task"]' in joined
    assert 'Account["Account"]' in joined
    # リレーション名の矢印
    assert '--"|"What"|"' in joined or '-->|"What"|' in joined


def test_mermaid_self_reference_label_has_note() -> None:
    """自己参照の矢印ラベルに 「（上に出ています）」 が添えられる。"""
    record = _make_record(
        key="1",
        main_object="Account",
        relations=(
            RelationRecord(
                parent="Account",
                field="ParentId",
                relationship_name="Parent",
                child="Account",
                depth=1,
            ),
        ),
    )
    diagrams = build_main_object_diagrams([record], max_depth=2)
    joined = "\n".join(diagrams["Account"].render_mermaid_lines())
    assert "（上に出ています）" in joined


def test_mermaid_sanitizes_node_ids() -> None:
    """ノード ID に ``__c`` や ``/`` があっても、 整えてエラー回避。"""
    record = _make_record(
        key="1",
        main_object="Task",
        relations=(
            RelationRecord(
                parent="Task",
                field="Custom__c",
                relationship_name="Custom__r",
                child="Custom__c",
                depth=1,
            ),
        ),
    )
    diagrams = build_main_object_diagrams([record], max_depth=2)
    joined = "\n".join(diagrams["Task"].render_mermaid_lines())
    # ``Custom__c`` はそのままラベルに
    assert 'Custom__c["Custom__c"]' in joined
    # ノード ID は整えてあるので ``Custom__c`` がそのまま ID に
    assert "Custom__c" in joined


# ── ファイル書き出し ─────────────────────────────────────────────────────


def test_write_main_object_diagrams_writes_files(tmp_path: Path) -> None:
    """``OUTPUT_DIR/関係図/{主オブジェクト}.md`` を 1 ファイルずつ書く。"""
    diagrams = build_main_object_diagrams(
        [
            _make_record(key="1", main_object="Task", relations=()),
            _make_record(key="2", main_object="Account", relations=()),
        ]
    )
    wrote = write_main_object_diagrams(tmp_path, diagrams)
    names = sorted(path.name for path in wrote)
    assert names == ["Account.md", "Task.md"]
    # 中身は Mermaid ``` フェンスで囲まれている
    text = (tmp_path / "関係図" / "Task.md").read_text(encoding="utf-8")
    assert "```mermaid" in text
    assert "flowchart LR" in text


def test_write_main_object_diagrams_empty_dict_writes_nothing(tmp_path: Path) -> None:
    """空 ``diagrams`` なら何も書かずディレクトリも作らない。"""
    assert write_main_object_diagrams(tmp_path, {}) == ()
    assert not (tmp_path / "関係図").exists()


def test_write_main_object_diagrams_keeps_existing_files(tmp_path: Path) -> None:
    """既存ファイルはそのまま残る （今回渡されなかった主オブジェクト分）。"""
    diagrams_dir = tmp_path / "関係図"
    diagrams_dir.mkdir()
    existing = diagrams_dir / "Other.md"
    existing.write_text("# 再利用されない古いファイル", encoding="utf-8")

    diagrams = build_main_object_diagrams(
        [
            _make_record(key="1", main_object="Task", relations=()),
        ]
    )
    write_main_object_diagrams(tmp_path, diagrams)
    # 既存はそのまま
    assert existing.read_text(encoding="utf-8") == "# 再利用されない古いファイル"
    # Task.md は新規
    assert (diagrams_dir / "Task.md").exists()


# ── Markdown への埋め込み ─────────────────────────────────────────────────


def test_render_inline_tree_for_record_returns_lines() -> None:
    """``render_inline_tree_for_record`` は ``record`` から字下げツリーを返す。"""
    record = _make_record(
        key="1",
        main_object="Task",
        relations=(
            RelationRecord(
                parent="Task", field="WhatId", relationship_name="What", child="Account", depth=1
            ),
        ),
    )
    lines = render_inline_tree_for_record(record, max_depth=2)
    assert lines[0] == "Task"
    assert any("What → Account" in line for line in lines)


def test_render_inline_tree_for_record_returns_empty_without_main_object() -> None:
    record = _make_record(key="1", main_object=None, relations=())
    assert render_inline_tree_for_record(record) == ()


def test_diagram_is_frozen() -> None:
    """``Diagram`` は ``frozen`` で書き換え不可。"""
    diagram = Diagram(
        main_object="Task",
        root=__import__("src.relation_diagram", fromlist=["DiagramNode"]).DiagramNode(
            label="Task",
            soql_hint="",
            field_api="",
        ),
    )
    try:
        diagram.main_object = "変えた"  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("Diagram は frozen で書き換え不可のはず")
