"""``src.report_py`` のテスト。

ダウンローダー用の SOQL レポート Python ファイルが、 そのまま
``src/soql_reports/reports/`` に置ける形で書かれることを確かめる。
生成されたコードは ``compile()`` → ``ast`` → ``exec()`` の順で読み、 値が
コードとして実行されないことも確かめる。
"""

from __future__ import annotations

import ast
import sys
import types
from pathlib import Path
from typing import ClassVar

from src.fetch import ReportRecord
from src.report_py import (
    REPORT_PY_DIR,
    build_report_py_source,
    write_report_py_files,
)
from src.soql_builder import INCOMPLETE_MARKER, build_soql

# ── ヘルパー ──────────────────────────────────────────────────────


def _column_map(rows: list[tuple[str, str, str]]) -> list[dict[str, str]]:
    """``(列キー, SOQL列名, 表示名)`` のリストから ``column_map`` を作る。"""
    return [
        {
            "列キー": key,
            "表示名": display_name,
            "所属オブジェクト": "Case",
            "対応フィールドAPI名": soql_name,
            "型": "string",
            "参照先オブジェクト": "",
            "リレーション名": "",
            "備考": "",
        }
        for key, soql_name, display_name in rows
    ]


def _make_record(
    key: str = "1001",
    summary: str = "テストのレポート",
    detail_columns: list[str] | None = None,
    column_map: list[dict[str, str]] | None = None,
    report_filters: list[dict] | None = None,
    unresolved_columns: list[str] | None = None,
    report_name: object = None,
) -> ReportRecord:
    """SOQL の下書きまで済んだ ``ReportRecord`` を作る。

    ``unresolved_columns`` に入れた列キーは解決できない列として扱う
    （ ``missing`` に入り、 SELECT から落ちる ）。
    ``report_name`` を渡すと、 ``reportMetadata.name`` に入れる。 渡さない
    （ ``None`` ） ときは ``name`` キー自体が無い状態にする。
    """
    detail_columns = list(detail_columns or ["Name"])
    metadata: dict = {
        "reportType": {"type": "Case"},
        "reportFormat": "TABULAR",
        "detailColumns": detail_columns,
    }
    if report_name is not None:
        metadata["name"] = report_name
    if report_filters is not None:
        metadata["reportFilters"] = report_filters
    if column_map is None:
        column_map = _column_map(
            [(column_key, column_key, column_key) for column_key in detail_columns]
        )
    for unresolved in unresolved_columns or []:
        detail_columns.append(unresolved)
        column_map.append(
            {
                "列キー": unresolved,
                "表示名": unresolved,
                "所属オブジェクト": "Case",
                "対応フィールドAPI名": "(不明)",
                "型": "",
                "参照先オブジェクト": "",
                "リレーション名": "",
                "備考": "対応フィールドなし",
            }
        )
    metadata["detailColumns"] = detail_columns
    record = ReportRecord(
        key=key,
        summary=summary,
        report_id="00O000000000001",
        url="https://example.my.salesforce.com/lightning/r/Report/00O000000000001/view",
        main_object="Case",
        report={"reportMetadata": metadata, "reportExtendedMetadata": {}},
        objects={},
        relations=(),
        column_map=column_map,
        warnings=(),
    )
    return ReportRecord(
        key=record.key,
        summary=record.summary,
        report_id=record.report_id,
        url=record.url,
        main_object=record.main_object,
        report=record.report,
        objects=record.objects,
        relations=record.relations,
        column_map=record.column_map,
        warnings=record.warnings,
        soql_draft=build_soql(record),
    )


def _load_generated(source: str) -> dict[str, object]:
    """生成した Python を実行して、 モジュールの名前空間返す。

    ダウンローダーの ``src.soql_reports.base`` は Fake を ``sys.modules`` に
    入れて代用する。
    """
    base_module = types.ModuleType("src.soql_reports.base")

    class SoqlReport:
        KEY: ClassVar[str] = ""
        COLUMN_NAMES: ClassVar[dict[str, str]] = {}

    base_module.SoqlReport = SoqlReport  # type: ignore[attr-defined]
    package_module = types.ModuleType("src.soql_reports")
    saved = {
        name: sys.modules.get(name) for name in ("src", "src.soql_reports", "src.soql_reports.base")
    }
    sys.modules["src.soql_reports"] = package_module
    sys.modules["src.soql_reports.base"] = base_module
    namespace: dict[str, object] = {"__name__": "generated_report"}
    try:
        exec(compile(source, "<generated>", "exec"), namespace)
    finally:
        for name, previous in saved.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous
    return namespace


def _class_attributes(source: str, class_name: str) -> list[str]:
    """生成したソースのクラスに代入されている属性名（ クラス変数 ） を返す。"""
    tree = ast.parse(source)
    class_def = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    return [
        target.id
        for node in class_def.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    ]


# ── 生成される文件的形 ────────────────────────────────────────────


def test_generated_source_is_valid_python() -> None:
    """生成したソースは、 Python としてコンパイルできる。"""
    record = _make_record()
    source = build_report_py_source(record, class_name="Report1001")
    assert source is not None
    compile(source, "<generated>", "exec")


def test_generated_source_has_expected_structure() -> None:
    """生成したソースは、 docstring と import と 1 つのクラスだけの形。"""
    record = _make_record()
    source = build_report_py_source(record, class_name="Report1001")
    assert source is not None
    tree = ast.parse(source)
    assert isinstance(tree.body[0], ast.Expr)
    assert isinstance(tree.body[0].value, ast.Constant)
    class_defs = [node for node in tree.body if isinstance(node, ast.ClassDef)]
    assert len(class_defs) == 1
    assert class_defs[0].name == "Report1001"
    # クラスのメソッドは soql だけ
    methods = [node.name for node in class_defs[0].body if isinstance(node, ast.FunctionDef)]
    assert methods == ["soql"]


def test_generated_source_does_not_write_check_count() -> None:
    """``CHECK_COUNT`` は書かない（ 既定の True のまま ） 。"""
    record = _make_record()
    source = build_report_py_source(record, class_name="Report1001")
    assert source is not None
    assert "CHECK_COUNT" not in source


def test_generated_soql_matches_build_soql_body() -> None:
    """``soql()`` の返り値は、 ``build_soql`` の本文と完全に一致する。"""
    record = _make_record(
        detail_columns=["Name", "Owner.Name"],
        column_map=_column_map([("Name", "Name", "件名"), ("Owner.Name", "Owner.Name", "所有者")]),
    )
    source = build_report_py_source(record, class_name="Report1001")
    assert source is not None
    namespace = _load_generated(source)
    report_class = namespace["Report1001"]
    assert report_class.KEY == "1001"  # type: ignore[attr-defined]
    assert report_class().soql() == record.soql_draft.soql  # type: ignore[attr-defined]


def test_generated_soql_keeps_incomplete_marker() -> None:
    """``一部作れず`` の SOQL は、 ``【未完成】`` の行をそのまま残す。"""
    record = _make_record(unresolved_columns=["Unknown"])
    assert record.soql_draft.soql.startswith(INCOMPLETE_MARKER)
    source = build_report_py_source(record, class_name="Report1001")
    assert source is not None
    namespace = _load_generated(source)
    report_class = namespace["Report1001"]
    assert report_class().soql() == record.soql_draft.soql  # type: ignore[attr-defined]
    assert INCOMPLETE_MARKER in report_class().soql()  # type: ignore[attr-defined]


def test_generated_source_has_no_extra_statements_for_injected_value() -> None:
    """注入された値でも、 余計な文が増えない。"""
    record = _make_record(summary='概要\n__import__("os").system("x")')
    source = build_report_py_source(record, class_name="Report1001")
    assert source is not None
    tree = ast.parse(source)
    # モジュール直下の文は docstring と import とクラス定義だけ
    for node in tree.body:
        assert isinstance(node, (ast.Expr, ast.Import, ast.ImportFrom, ast.ClassDef))
    assert len([node for node in tree.body if isinstance(node, ast.ClassDef)]) == 1
    # 注入された値がコードとして実行されていないこと（ 実行しても副作用が無い ）
    namespace = _load_generated(source)
    assert namespace["Report1001"]().soql() == record.soql_draft.soql  # type: ignore[attr-defined]


def test_generated_source_has_no_extra_statements_for_injected_display_name() -> None:
    """表示名に注入された値でも、 余計な文が増えない。"""
    record = _make_record(
        detail_columns=["Name"],
        column_map=_column_map([("Name", "Name", '名\n__import__("os").system("x")')]),
    )
    source = build_report_py_source(record, class_name="Report1001")
    assert source is not None
    tree = ast.parse(source)
    for node in tree.body:
        assert isinstance(node, (ast.Expr, ast.Import, ast.ImportFrom, ast.ClassDef))


# ── NAME ──────────────────────────────────────────────────────────


def test_generated_class_has_report_name() -> None:
    """``reportMetadata.name`` が、 クラス属性 ``NAME`` になる。"""
    record = _make_record(report_name="売上レポート")
    source = build_report_py_source(record, class_name="Report1001")
    assert source is not None
    compile(source, "<generated>", "exec")
    namespace = _load_generated(source)
    report_class = namespace["Report1001"]
    assert report_class.NAME == "売上レポート"  # type: ignore[attr-defined]


def test_generated_name_line_comes_right_after_key() -> None:
    """``NAME`` の行は、 ``KEY`` の次の行に書く。"""
    record = _make_record(report_name="売上レポート")
    source = build_report_py_source(record, class_name="Report1001")
    assert source is not None
    lines = source.splitlines()
    key_index = lines.index("    KEY = '1001'")
    assert lines[key_index + 1] == "    NAME = '売上レポート'"


def test_generated_name_falls_back_to_summary() -> None:
    """レポート名が空のときは、 概要を使う。"""
    record = _make_record(summary="概要から取る名前", report_name="")
    source = build_report_py_source(record, class_name="Report1001")
    assert source is not None
    namespace = _load_generated(source)
    report_class = namespace["Report1001"]
    assert report_class.NAME == "概要から取る名前"  # type: ignore[attr-defined]


def test_generated_name_falls_back_to_summary_when_name_is_not_text() -> None:
    """レポート名が文字列でないときは、 概要を使う。"""
    record = _make_record(summary="概要から取る名前", report_name=123)
    source = build_report_py_source(record, class_name="Report1001")
    assert source is not None
    namespace = _load_generated(source)
    report_class = namespace["Report1001"]
    assert report_class.NAME == "概要から取る名前"  # type: ignore[attr-defined]


def test_generated_name_falls_back_to_summary_when_name_is_missing() -> None:
    """レポート名のキーが無いときは、 概要を使う。"""
    record = _make_record(summary="概要から取る名前")
    source = build_report_py_source(record, class_name="Report1001")
    assert source is not None
    namespace = _load_generated(source)
    report_class = namespace["Report1001"]
    assert report_class.NAME == "概要から取る名前"  # type: ignore[attr-defined]


def test_generated_source_has_no_name_line_when_both_are_empty() -> None:
    """レポート名も概要も空のときは、 ``NAME`` の行を出さない。"""
    record = _make_record(summary="", report_name="")
    source = build_report_py_source(record, class_name="Report1001")
    assert source is not None
    assert "NAME" not in _class_attributes(source, "Report1001")
    assert not [line for line in source.splitlines() if line.startswith("    NAME = ")]


def test_generated_name_keeps_quotes_and_newlines_in_one_line() -> None:
    """レポート名に引用符・改行・ ``\"\"\"`` が入っても、 壊れず 1 行に収まる。"""
    record = _make_record(report_name='a\'b"c\nd"""e')
    source = build_report_py_source(record, class_name="Report1001")
    assert source is not None
    compile(source, "<generated>", "exec")
    # 改行は空白に置き換えて、 1 行に収まる
    assert [line for line in source.splitlines() if line.startswith("    NAME = ")] == [
        '    NAME = \'a\\\'b"c d"""e\''
    ]
    namespace = _load_generated(source)
    report_class = namespace["Report1001"]
    assert report_class.NAME == 'a\'b"c d"""e'  # type: ignore[attr-defined]


# ── COLUMN_NAMES ─────────────────────────────────────────────────


def test_column_names_holds_select_columns() -> None:
    """``COLUMN_NAMES`` は SELECT に入った列の 「SOQL列名: 表示名」 。"""
    record = _make_record(
        detail_columns=["Name", "Owner.Name"],
        column_map=_column_map([("Name", "Name", "件名"), ("Owner.Name", "Owner.Name", "所有者")]),
    )
    source = build_report_py_source(record, class_name="Report1001")
    assert source is not None
    namespace = _load_generated(source)
    report_class = namespace["Report1001"]
    assert report_class.COLUMN_NAMES == {"Name": "件名", "Owner.Name": "所有者"}  # type: ignore[attr-defined]


def test_column_names_excludes_duplicated_display_names() -> None:
    """同じ表示名 が 2 列あるときは、 その列を ``COLUMN_NAMES`` に入れない。"""
    record = _make_record(
        detail_columns=["Owner.Name", "CreatedBy.Name"],
        column_map=_column_map(
            [
                ("Owner.Name", "Owner.Name", "所有者"),
                ("CreatedBy.Name", "CreatedBy.Name", "所有者"),
            ]
        ),
    )
    source = build_report_py_source(record, class_name="Report1001")
    assert source is not None
    assert "# 見出しが重なるので自動の対応に任せた:" in source
    namespace = _load_generated(source)
    report_class = namespace["Report1001"]
    assert report_class.COLUMN_NAMES == {}  # type: ignore[attr-defined]


def test_duplicated_comment_is_flattened() -> None:
    """見出しが重なるコメントの SOQL 列名にも、 生の改行を出さない。"""
    record = _make_record(
        detail_columns=["Owner.Name", "CreatedBy.Name"],
        column_map=_column_map(
            [
                ("Owner.Name", "Owner.Name\n壊れた", "所有者"),
                ("CreatedBy.Name", "CreatedBy.Name", "所有者"),
            ]
        ),
    )
    source = build_report_py_source(record, class_name="Report1001")
    assert source is not None
    assert "# 見出しが重なるので自動の対応に任せた: Owner.Name 壊れた, CreatedBy.Name" in source
    # 改行が混入したコメントでも、 生成した Python は壊れない
    compile(source, "<generated>", "exec")


# ── ファイルを書く ────────────────────────────────────────────────


def test_write_report_py_files_writes_one_file_per_report(tmp_path: Path) -> None:
    """1 レポート 1 ファイルが、 ``SOQLレポート/`` に書かれる。"""
    record = _make_record(key="1001")
    written = write_report_py_files(tmp_path, [record])
    assert len(written) == 1
    assert written[0] == tmp_path / REPORT_PY_DIR / "report_1001.py"
    assert written[0].exists()


def test_write_report_py_files_skips_report_without_soql(tmp_path: Path) -> None:
    """SOQL が作れなかったレポートは、 ファイルを作らない。"""
    record = _make_record(
        detail_columns=["Unknown"],
        column_map=[
            {
                "列キー": "Unknown",
                "表示名": "不明",
                "所属オブジェクト": "Case",
                "対応フィールドAPI名": "(不明)",
                "型": "",
                "参照先オブジェクト": "",
                "リレーション名": "",
                "備考": "対応フィールドなし",
            }
        ],
    )
    assert record.soql_draft.soql == ""
    written = write_report_py_files(tmp_path, [record])
    assert written == ()
    assert not (tmp_path / REPORT_PY_DIR).exists()


def test_write_report_py_files_numbers_duplicated_keys(tmp_path: Path) -> None:
    """置換で同名になる管理番号は、 後の方に連番を付ける。"""
    first = _make_record(key="A-1")
    second = _make_record(key="A_1")
    written = write_report_py_files(tmp_path, [first, second])
    assert [path.name for path in written] == ["report_A_1.py", "report_A_1_2.py"]


def test_write_report_py_files_skips_names_used_by_other_keys(tmp_path: Path) -> None:
    """連番の結果が別の管理番号とぶつかるときは、 未使用の名前まで連番を進める。"""
    records = [_make_record(key=key) for key in ("A_1_2", "A-1", "A_1")]
    written = write_report_py_files(tmp_path, records)
    assert [path.name for path in written] == [
        "report_A_1_2.py",
        "report_A_1.py",
        "report_A_1_3.py",
    ]


def test_write_report_py_files_replaces_invalid_key_characters(tmp_path: Path) -> None:
    """管理番号の英数字と _ 以外は、 ``_`` に置き換える。"""
    record = _make_record(key="1001/2")
    written = write_report_py_files(tmp_path, [record])
    assert [path.name for path in written] == ["report_1001_2.py"]
