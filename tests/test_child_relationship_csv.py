"""手入力の子リレーション CSV （ ``input/子リレーション.csv`` ） のテスト。

読み込み・作成・不正な値の拒否・主子リレーションへの追加・置き換え・
クロスフィルタの判定への反映 を確かめる。
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

import pytest
from comken.exceptions import CSVError

from src.child_relationship_csv import (
    CHILD_RELATIONSHIP_COLUMNS,
    ManualChildRelationship,
    child_relationship_csv_path,
    ensure_child_relationship_file,
    load_manual_child_relationships,
    merge_manual_child_relationships,
)
from src.fetch import ReportRecord
from src.soql_builder import (
    CROSS_FILTER_LINK_DIRECT,
    CROSS_FILTER_LINK_UNVERIFIED,
    build_soql,
)

# ── ヘルパー ──────────────────────────────────────────────────────────────


def _write_csv(path: Path, rows: list[list[str]], *, encoding: str = "utf-8-sig") -> None:
    """指定の文字コードで CSV を書く。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding=encoding, newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(list(CHILD_RELATIONSHIP_COLUMNS))
        writer.writerows(rows)


def _manual_row(**overrides: str) -> ManualChildRelationship:
    values = {
        "parent_object": "Account",
        "relationship_name": "Contacts",
        "child_object": "Contact",
        "reference_field": "AccountId",
        "note": "",
    }
    values.update(overrides)
    return ManualChildRelationship(**values)  # type: ignore[arg-type]


# ── ファイルの作成と読み込み ──────────────────────────────────────────────


def test_file_is_created_with_header_only(tmp_path: Path) -> None:
    """ファイルが無ければ見出しだけのファイルを作る。"""
    path = child_relationship_csv_path(tmp_path / "input" / "列の直し.xlsx")
    ensure_child_relationship_file(path)
    assert path.exists()
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))
    assert rows == [list(CHILD_RELATIONSHIP_COLUMNS)]


def test_existing_file_is_not_overwritten(tmp_path: Path) -> None:
    """已有のファイルは上書きしない。"""
    path = child_relationship_csv_path(tmp_path / "input" / "列の直し.xlsx")
    _write_csv(path, [["Account", "Contacts", "Contact", "AccountId", "memo"]])
    ensure_child_relationship_file(path)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))
    assert len(rows) == 2
    assert rows[1][1] == "Contacts"


def test_reads_utf8_bom_file(tmp_path: Path) -> None:
    """UTF-8 BOM つきで保存されたファイルを読める。"""
    path = tmp_path / "子リレーション.csv"
    _write_csv(path, [["Account", "Contacts", "Contact", "AccountId", "手入力メモ"]])
    rows = load_manual_child_relationships(path)
    assert len(rows) == 1
    assert rows[0].parent_object == "Account"
    assert rows[0].relationship_name == "Contacts"
    assert rows[0].child_object == "Contact"
    assert rows[0].reference_field == "AccountId"
    assert rows[0].note == "手入力メモ"


def test_reads_cp932_file(tmp_path: Path) -> None:
    """cp932 で保存されたファイル （ Excel で保存した直後 ） を読める。"""
    path = tmp_path / "子リレーション.csv"
    _write_csv(
        path, [["Account", "Contacts", "Contact", "AccountId", "手入力メモ"]], encoding="cp932"
    )
    rows = load_manual_child_relationships(path)
    assert len(rows) == 1
    assert rows[0].child_object == "Contact"
    assert rows[0].note == "手入力メモ"


def test_missing_file_returns_empty(tmp_path: Path) -> None:
    """ファイルが無ければ空。"""
    assert load_manual_child_relationships(tmp_path / "無い.csv") == ()


def test_wrong_header_raises(tmp_path: Path) -> None:
    """見出しが違うときは例外で止まる。"""
    path = tmp_path / "子リレーション.csv"
    path.write_text("親,リレーション,子,参照,メモ\nA,B,C,D,E\n", encoding="utf-8-sig")
    with pytest.raises(CSVError):
        load_manual_child_relationships(path)


# ── 不正な値の拒否 ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "row",
    [
        ["", "Contacts", "Contact", "AccountId", ""],
        ["Account", "", "Contact", "AccountId", ""],
        ["Account", "Contacts", "", "AccountId", ""],
        ["Account", "Contacts", "Contact", "", ""],
        ["Account", "Contacts", "Con tact", "AccountId", ""],
        ["Account", "Contacts", "Contact", "Account'Id", ""],
        ["Account", "Contacts;DROP", "Contact", "AccountId", ""],
        ["Account", "Contacts", "1Contact", "AccountId", ""],
        ["Account Name", "Contacts", "Contact", "AccountId", ""],
    ],
)
def test_invalid_rows_are_not_used(tmp_path: Path, row: list[str]) -> None:
    """空や識別子でない値は使わず、 警告を出す。"""
    path = tmp_path / "子リレーション.csv"
    _write_csv(path, [row])
    rows = load_manual_child_relationships(path)
    assert rows == ()


def test_invalid_value_never_reaches_soql(tmp_path: Path) -> None:
    """不正な値は SOQL にも .py にも入らない。"""
    path = tmp_path / "子リレーション.csv"
    _write_csv(path, [["Account", "Contacts", "Contact'; DROP", "AccountId", ""]])
    rows = load_manual_child_relationships(path)
    assert rows == ()


def test_analyze_does_not_create_the_file_but_reads_it(tmp_path: Path) -> None:
    """解析の段は作らないが、 既にあれば読む。"""
    from src.run import FilePaths, _load_manual_child_relationships

    file_paths = FilePaths(
        master_xlsx_path=tmp_path / "master.xlsx",
        output_dir=tmp_path / "output",
        describe_csv_path=tmp_path / "data" / "項目定義.csv",
        column_fix_xlsx_path=tmp_path / "input" / "列の直し.xlsx",
        xlsx_name="SOQL調査.xlsx",
        markdown_dir="SOQL資料",
        diagram_dir="関係図",
    )
    path = child_relationship_csv_path(file_paths.column_fix_xlsx_path)

    # 解析の段: ファイルは作らない
    assert _load_manual_child_relationships(file_paths, creates_file=False) == ()
    assert not path.exists()

    # 取得の段: 見出しだけのファイルを作る
    assert _load_manual_child_relationships(file_paths, creates_file=True) == ()
    assert path.exists()

    # 既にあれば、 解析の段でも読む
    _write_csv(path, [["Account", "Contacts", "Contact", "AccountId", ""]])
    rows = _load_manual_child_relationships(file_paths, creates_file=False)
    assert len(rows) == 1
    assert rows[0].child_object == "Contact"


# ── 主オブジェクトの describe への追加 ────────────────────────────────────


def test_manual_row_is_added_to_child_relationships() -> None:
    """手入力の行は子リレーションに足される。"""
    merged = merge_manual_child_relationships(
        child_relationships=[],
        manual_rows=[_manual_row()],
        parent_object="Account",
    )
    assert len(merged) == 1
    assert merged[0]["childSObject"] == "Contact"
    assert merged[0]["relationshipName"] == "Contacts"
    assert merged[0]["field"] == "AccountId"
    assert merged[0]["手入力"] is True


def test_manual_row_replaces_same_child_and_field() -> None:
    """同じ (子オブジェクト, 参照項目) が有れば置き換える。"""
    existing = [
        {
            "childSObject": "Contact",
            "field": "AccountId",
            "relationshipName": "OldName",
        }
    ]
    merged = merge_manual_child_relationships(
        child_relationships=existing,
        manual_rows=[_manual_row()],
        parent_object="Account",
    )
    assert len(merged) == 1
    assert merged[0]["relationshipName"] == "Contacts"
    assert merged[0]["手入力"] is True


def test_manual_row_of_other_parent_is_ignored() -> None:
    """親が違う行は使わない。"""
    merged = merge_manual_child_relationships(
        child_relationships=[],
        manual_rows=[_manual_row(parent_object="Opportunity")],
        parent_object="Account",
    )
    assert merged == []


def test_parent_match_ignores_case() -> None:
    """親の照合は大文字小文字を無視する。"""
    merged = merge_manual_child_relationships(
        child_relationships=[],
        manual_rows=[_manual_row(parent_object="ACCOUNT")],
        parent_object="account",
    )
    assert len(merged) == 1


# ── クロスフィルタの判定 ──────────────────────────────────────────────────


def _account_describe(with_children: bool = True) -> dict[str, Any]:
    """Account の describe。"""
    child_relationships: list[dict[str, Any]] = []
    if with_children:
        child_relationships.append(
            {
                "childSObject": "Contact",
                "field": "AccountId",
                "relationshipName": "Contacts",
            }
        )
    return {
        "name": "Account",
        "label": "取引先",
        "custom": False,
        "fields": [
            {"name": "Id", "label": "ID", "type": "id"},
            {"name": "Name", "label": "取引先名", "type": "string"},
        ],
        "childRelationships": child_relationships,
    }


def _contact_describe() -> dict[str, Any]:
    """Contact の describe。"""
    return {
        "name": "Contact",
        "label": "取引先担当者",
        "custom": False,
        "fields": [
            {
                "name": "AccountId",
                "label": "取引先",
                "type": "reference",
                "relationshipName": "Account",
                "referenceTo": ["Account"],
            },
            {"name": "LastName", "label": "姓", "type": "string"},
        ],
        "childRelationships": [],
    }


def _cross_filter(criteria: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Contact への参照を使うクロスフィルタ。"""
    return {
        "criteria": criteria if criteria is not None else [],
        "includesObject": True,
        "primaryEntityField": "Id",
        "relatedEntity": "Contact",
        "relatedEntityJoinField": "AccountId",
    }


def _record(
    *,
    main_describe: dict[str, Any],
    cross_filter_objects: dict[str, dict[str, Any]] | None = None,
    criteria: list[dict[str, Any]] | None = None,
) -> ReportRecord:
    """手入力の子リレーションを使った Account の record を作る。"""
    column_map = [
        {
            "列キー": "Name",
            "表示名": "Name",
            "所属オブジェクト": "Account",
            "対応フィールドAPI名": "Name",
            "型": "string",
            "参照先オブジェクト": "",
            "リレーション名": "",
            "備考": "",
        }
    ]
    metadata = {
        "reportType": {"type": "Account"},
        "reportFormat": "TABULAR",
        "detailColumns": ["Name"],
        "crossFilters": [_cross_filter(criteria)],
    }
    return ReportRecord(
        key="1001",
        summary="テスト",
        report_id="00O000000000001",
        url="https://example.my.salesforce.com/lightning/r/Report/00O000000000001/view",
        main_object="Account",
        report={"reportMetadata": metadata, "reportExtendedMetadata": {}},
        objects={"Account": main_describe},
        relations=(),
        column_map=column_map,
        warnings=(),
        cross_filter_objects=cross_filter_objects or {},
    )


def _merged_describe(with_children: bool = True) -> dict[str, Any]:
    """手入力の子リレーションを足した Account の describe。"""
    describe = _account_describe(with_children=with_children)
    merged = merge_manual_child_relationships(
        child_relationships=list(describe["childRelationships"]),
        manual_rows=[_manual_row()],
        parent_object="Account",
    )
    describe["childRelationships"] = merged
    return describe


def test_manual_child_relationship_makes_cross_filter_direct() -> None:
    """手入力の子リレーションで、 クロスフィルタが 「直接」 になる。"""
    record = _record(
        main_describe=_merged_describe(with_children=False),
        cross_filter_objects={"Contact": _contact_describe()},
    )
    draft = build_soql(record)
    link = draft.cross_filter_links[0]
    assert link.status == CROSS_FILTER_LINK_DIRECT
    assert link.converted is True
    assert "手入力" in link.reason


def test_manual_child_relationship_without_child_describe_and_empty_criteria() -> None:
    """子の describe が無くても criteria が空なら変換できる。"""
    record = _record(main_describe=_merged_describe(with_children=False))
    draft = build_soql(record)
    link = draft.cross_filter_links[0]
    assert link.status == CROSS_FILTER_LINK_DIRECT
    assert link.converted is True
    assert "手入力" in link.reason
    assert "SELECT AccountId FROM Contact" in draft.soql


def test_manual_child_relationship_without_child_describe_and_criteria() -> None:
    """criteria が有ると、 列の型が分からないので 「確認できず」 になる。"""
    criteria = [
        {
            "column": "LastName",
            "operator": "equals",
            "value": "山田",
        }
    ]
    record = _record(main_describe=_merged_describe(with_children=False), criteria=criteria)
    draft = build_soql(record)
    link = draft.cross_filter_links[0]
    assert link.status == CROSS_FILTER_LINK_UNVERIFIED
    assert link.converted is False
    assert "手入力" in link.reason


def test_manual_child_relationship_is_reported_in_markdown() -> None:
    """Markdown の子リレーションの表に 「出どころ」 が出る。"""
    from src import ai_markdown

    record = _record(main_describe=_merged_describe(with_children=False))
    draft = build_soql(record)
    record = ReportRecord(
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
        cross_filter_objects=record.cross_filter_objects,
        soql_draft=draft,
    )
    markdown = ai_markdown.build_markdown(record)
    assert "| 出どころ |" in markdown
    assert "手入力" in markdown


# ── 保存データには書かない ────────────────────────────────────────────────


def test_manual_values_are_not_written_to_saved_data(tmp_path: Path) -> None:
    """保存する原本に手入力の値は書かない。"""
    from src.describe_store import DescribeStore
    from src.fetch.site_run import FetchState, run_within_site
    from src.report_store import ReportStore

    class _ReportAPI:
        def describe(self, report_id: str) -> dict[str, Any]:
            return metadata

    class _Client:
        def __init__(self) -> None:
            self.report = _ReportAPI()

        def describe_object(self, name: str) -> dict[str, Any]:
            if name == "Account":
                return _account_describe(with_children=False)
            return {"name": name, "fields": []}

    metadata = {
        "reportMetadata": {
            "reportType": {"type": "Account"},
            "reportFormat": "TABULAR",
            "detailColumns": ["Name"],
        },
        "reportExtendedMetadata": {},
    }
    describe_csv = tmp_path / "data" / "項目定義.csv"
    store = DescribeStore.load(describe_csv)
    report_store = ReportStore.empty()
    state = FetchState()
    run_within_site(
        client=_Client(),
        entry=_entry(),
        org="org",
        report_id="00O1",
        store=store,
        report_store=report_store,
        row_limit=False,
        related_depth=1,
        related_max=40,
        column_fix_overrides={},
        state=state,
        manual_child_relationships=(_manual_row(),),
    )
    assert state.main_object_name == "Account"
    assert state.main_describe is not None
    relationships = state.main_describe["childRelationships"]
    assert any(item.get("手入力") for item in relationships)
    # 保存した原本には手入力の値が入らない
    saved_path = tmp_path / "data" / "元データ" / "オブジェクト" / "org__account.json"
    assert saved_path.exists()
    assert "手入力" not in saved_path.read_text(encoding="utf-8")


def test_manual_child_describe_is_fetched_in_site_run(tmp_path: Path) -> None:
    """site_run は手入力で分かる子の describe も API で取りに行く。"""
    from src.describe_store import DescribeStore
    from src.fetch.site_run import FetchState, run_within_site
    from src.report_store import ReportStore

    metadata = {
        "reportMetadata": {
            "reportType": {"type": "Account"},
            "reportFormat": "TABULAR",
            "detailColumns": ["Name"],
            "crossFilters": [_cross_filter()],
        },
        "reportExtendedMetadata": {},
    }

    class _ReportAPI:
        def describe(self, report_id: str) -> dict[str, Any]:
            return metadata

    class _Client:
        def __init__(self) -> None:
            self.report = _ReportAPI()
            self.described: list[str] = []

        def describe_object(self, name: str) -> dict[str, Any]:
            self.described.append(name)
            if name == "Account":
                return _account_describe(with_children=False)
            if name == "Contact":
                return _contact_describe()
            return {"name": name, "fields": []}

    client = _Client()
    state = FetchState()
    run_within_site(
        client=client,
        entry=_entry(),
        org="org",
        report_id="00O1",
        store=DescribeStore.empty(),
        report_store=ReportStore.empty(),
        row_limit=False,
        related_depth=1,
        related_max=40,
        column_fix_overrides={},
        state=state,
        manual_child_relationships=(_manual_row(),),
    )
    assert "Contact" in client.described
    assert "Contact" in state.cross_filter_objects


def test_report_py_comment_mentions_manual() -> None:
    """生成する .py のコメントに 「手入力」 が出る。"""
    from src import report_py

    record = _record(
        main_describe=_merged_describe(with_children=False),
        cross_filter_objects={"Contact": _contact_describe()},
    )
    draft = build_soql(record)
    record = ReportRecord(
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
        cross_filter_objects=record.cross_filter_objects,
        soql_draft=draft,
    )
    source = report_py.build_report_py_source(record, class_name="Report1001")
    assert source is not None
    assert "手入力" in source


def _entry() -> Any:
    from src.master import MasterEntry

    return MasterEntry(
        key="1001",
        summary="テスト",
        url="https://example.my.salesforce.com/lightning/r/Report/00O000000000001/view",
        enabled=True,
        survey=True,
    )
