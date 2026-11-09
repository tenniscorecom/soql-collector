"""子の列を子から取る（ SOQL の FROM の切り替え ） のテスト。

``src.mapping.child_base.apply_child_base`` が列対応表を 「子から取る形」
に書き換えることと、 その結果の SOQL が正しいことを確かめる。
"""

from __future__ import annotations

from typing import Any

import pytest

from src import report_py
from src.fetch import ReportRecord
from src.mapping.child_base import apply_child_base
from src.soql_builder import (
    INCOMPLETE_MARKER,
    STATUS_NEEDS_CHECK,
    build_soql,
)

# ── ヘルパー ──────────────────────────────────────────────────────────────


def _account_describe(
    child_relationships: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Account の describe。"""
    if child_relationships is None:
        child_relationships = [
            {
                "childSObject": "Contact",
                "field": "AccountId",
                "relationshipName": "Contacts",
            }
        ]
    return {
        "name": "Account",
        "label": "取引先",
        "custom": False,
        "fields": [
            {"name": "Id", "label": "ID", "type": "id"},
            {"name": "Name", "label": "取引先名", "type": "string"},
            {"name": "OwnerId", "label": "所有者", "type": "reference"},
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
            {"name": "FirstName", "label": "名", "type": "string"},
        ],
        "childRelationships": [],
    }


def _fetcher(describes: dict[str, dict[str, Any]]):
    """指定の describe を返す fetcher。"""

    def fetcher(name: str) -> dict[str, Any] | None:
        return describes.get(name)

    return fetcher


def _row(
    column_key: str,
    soql_name: str,
    *,
    field_type: str = "string",
    note: str = "",
) -> dict[str, str]:
    """列対応表の 1 行を作る。"""
    return {
        "列キー": column_key,
        "表示名": column_key,
        "所属オブジェクト": "Account",
        "対応フィールドAPI名": soql_name,
        "型": field_type,
        "参照先オブジェクト": "",
        "リレーション名": "",
        "備考": note,
    }


def _column_map() -> list[dict[str, str]]:
    """親の列と、 子の列（未解決） が混ざった列対応表。"""
    return [
        _row("Account.Name", "Name"),
        _row("Contact.LastName", "(不明)", field_type="", note="対応フィールドなし"),
    ]


def _record(
    column_map: list[dict[str, str]],
    *,
    soql_from_object: str | None = None,
    soql_from_relationship: str = "",
    report_filters: list[dict[str, Any]] | None = None,
    sort_by: list[dict[str, Any]] | None = None,
    cross_filters: list[dict[str, Any]] | None = None,
    cross_filter_objects: dict[str, dict[str, Any]] | None = None,
) -> ReportRecord:
    """FROM を切り替えた後の record を作る。"""
    metadata: dict[str, Any] = {
        "reportType": {"type": "Account"},
        "reportFormat": "TABULAR",
        "detailColumns": [row["列キー"] for row in column_map],
    }
    if report_filters is not None:
        metadata["reportFilters"] = report_filters
    if sort_by is not None:
        metadata["sortBy"] = sort_by
    if cross_filters is not None:
        metadata["crossFilters"] = cross_filters
    return ReportRecord(
        key="1001",
        summary="テスト",
        report_id="00O000000000001",
        url="https://example.my.salesforce.com/lightning/r/Report/00O000000000001/view",
        main_object="Account",
        report={"reportMetadata": metadata, "reportExtendedMetadata": {}},
        objects={"Account": _account_describe()},
        relations=(),
        column_map=column_map,
        warnings=(),
        cross_filter_objects=cross_filter_objects or {},
        soql_from_object=soql_from_object,
        soql_from_relationship=soql_from_relationship,
    )


# ── 切り替え basics ───────────────────────────────────────────────────────


def test_switches_from_to_the_child() -> None:
    """レポートの列が子の項目なら、 FROM を子に切り替える。"""
    result = apply_child_base(
        column_map=_column_map(),
        main_object="Account",
        main_describe=_account_describe(),
        fetch_object=_fetcher({"Contact": _contact_describe()}),
    )
    assert result.switched is True
    assert result.child_object == "Contact"
    assert result.relationship_name == "Account"
    rows = {row["列キー"]: row for row in result.column_map}
    # 子の列は、 子の項目として解決される
    assert rows["Contact.LastName"]["対応フィールドAPI名"] == "LastName"
    assert rows["Contact.LastName"]["所属オブジェクト"] == "Contact"
    # 親の列は、 子から親へ戻るリレーション名が付く
    assert rows["Account.Name"]["対応フィールドAPI名"] == "Account.Name"


def test_soql_selects_parent_and_child_columns() -> None:
    """親の列と子の列が混ざった SOQL が 「完全一致」 でできる。"""
    column_map = _column_map()
    result = apply_child_base(
        column_map=column_map,
        main_object="Account",
        main_describe=_account_describe(),
        fetch_object=_fetcher({"Contact": _contact_describe()}),
    )
    record = _record(
        result.column_map,
        soql_from_object=result.child_object,
        soql_from_relationship=result.relationship_name,
    )
    draft = build_soql(record)
    assert draft.soql == "SELECT Account.Name,\n  LastName\nFROM Contact"
    assert draft.status == STATUS_NEEDS_CHECK
    assert draft.use_bulk is True


def test_child_base_check_is_added() -> None:
    """切り替えの注意書きが checks に入る。"""
    column_map = _column_map()
    result = apply_child_base(
        column_map=column_map,
        main_object="Account",
        main_describe=_account_describe(),
        fetch_object=_fetcher({"Contact": _contact_describe()}),
    )
    record = _record(
        result.column_map,
        soql_from_object=result.child_object,
        soql_from_relationship=result.relationship_name,
    )
    draft = build_soql(record)
    assert any("子から取る形にした" in check for check in draft.checks)
    assert any("親 Account" in check for check in draft.checks)
    assert any("子 Contact" in check for check in draft.checks)
    assert any("経路 Account" in check for check in draft.checks)


def test_where_and_order_by_use_the_parent_path() -> None:
    """絞り込みと並び順の親の列にも経路が付く。"""
    column_map = _column_map()
    result = apply_child_base(
        column_map=column_map,
        main_object="Account",
        main_describe=_account_describe(),
        fetch_object=_fetcher({"Contact": _contact_describe()}),
    )
    record = _record(
        result.column_map,
        soql_from_object=result.child_object,
        soql_from_relationship=result.relationship_name,
        report_filters=[
            {"column": "Account.Name", "operator": "equals", "value": "テスト株式会社"}
        ],
        sort_by=[{"sortColumn": "Account.Name", "sortOrder": "Asc"}],
    )
    draft = build_soql(record)
    assert "WHERE Account.Name = 'テスト株式会社'" in draft.soql
    assert "ORDER BY Account.Name ASC" in draft.soql


def test_report_without_child_column_keeps_from() -> None:
    """子の列が無いレポートは FROM が変わらない。"""
    column_map = [_row("Account.Name", "Name")]
    result = apply_child_base(
        column_map=column_map,
        main_object="Account",
        main_describe=_account_describe(),
        fetch_object=_fetcher({"Contact": _contact_describe()}),
    )
    assert result.switched is False
    record = _record(column_map)
    draft = build_soql(record)
    assert "FROM Account" in draft.soql


def test_manual_child_relationship_also_switches() -> None:
    """手入力の子リレーションでも切り替えられる。"""
    from src.child_relationship_csv import ManualChildRelationship, merge_manual_child_relationships

    describe = _account_describe(child_relationships=[])
    describe["childRelationships"] = merge_manual_child_relationships(
        child_relationships=[],
        manual_rows=[
            ManualChildRelationship(
                parent_object="Account",
                relationship_name="Contacts",
                child_object="Contact",
                reference_field="AccountId",
                note="",
            )
        ],
        parent_object="Account",
    )
    result = apply_child_base(
        column_map=_column_map(),
        main_object="Account",
        main_describe=describe,
        fetch_object=_fetcher({"Contact": _contact_describe()}),
    )
    assert result.switched is True
    assert result.child_object == "Contact"


# ── 切り替えられないとき ──────────────────────────────────────────────────


def test_two_children_are_not_switched() -> None:
    """子が 2 つなら切り替えず、 【未完成】 になる。"""
    column_map = [
        _row("Account.Name", "Name"),
        _row("Contact.LastName", "(不明)", field_type="", note="対応フィールドなし"),
        _row("Task.Subject", "(不明)", field_type="", note="対応フィールドなし"),
    ]
    describe = _account_describe(
        child_relationships=[
            {"childSObject": "Contact", "field": "AccountId", "relationshipName": "Contacts"},
            {"childSObject": "Task", "field": "WhatId", "relationshipName": "Tasks"},
        ]
    )
    result = apply_child_base(
        column_map=column_map,
        main_object="Account",
        main_describe=describe,
        fetch_object=_fetcher({"Contact": _contact_describe()}),
    )
    assert result.switched is False
    assert any("子オブジェクトが複数あるので 1 つの SOQL にできない" in n for n in result.notes)
    rows = {row["列キー"]: row for row in result.column_map}
    assert rows["Contact.LastName"]["対応フィールドAPI名"] == "(不明)"
    assert "子オブジェクトが複数" in rows["Contact.LastName"]["備考"]

    # 【未完成】 になる （ missing に出る ）
    record = _record(result.column_map)
    draft = build_soql(record)
    assert draft.soql.startswith(INCOMPLETE_MARKER)
    assert any("子オブジェクトが複数" in m for m in draft.missing)


def test_two_references_to_the_same_child_are_not_switched() -> None:
    """同じ子への参照が 2 本あるときは判別できないので切り替えない。"""
    column_map = [
        _row("Account.Name", "Name"),
        _row("Contact.LastName", "(不明)", field_type="", note="対応フィールドなし"),
    ]
    describe = _account_describe(
        child_relationships=[
            {"childSObject": "Contact", "field": "AccountId", "relationshipName": "Contacts"},
            {"childSObject": "Contact", "field": "OtherId", "relationshipName": "OtherContacts"},
        ]
    )
    result = apply_child_base(
        column_map=column_map,
        main_object="Account",
        main_describe=describe,
        fetch_object=_fetcher({"Contact": _contact_describe()}),
    )
    assert result.switched is False
    assert any("同じ子オブジェクトへの参照が複数" in n for n in result.notes)


def test_too_deep_path_becomes_unknown() -> None:
    """経路が深すぎると (不明) になり、 理由が残る。"""
    column_map = [
        _row("Account.Name", "A.B.C.D.E"),
        _row("Contact.LastName", "(不明)", field_type="", note="対応フィールドなし"),
    ]
    result = apply_child_base(
        column_map=column_map,
        main_object="Account",
        main_describe=_account_describe(),
        fetch_object=_fetcher({"Contact": _contact_describe()}),
    )
    assert result.switched is True
    rows = {row["列キー"]: row for row in result.column_map}
    assert rows["Account.Name"]["対応フィールドAPI名"] == "(不明)"
    assert "長すぎます" in rows["Account.Name"]["備考"]


def test_missing_child_describe_does_not_switch() -> None:
    """子の describe が取れなければ切り替えない。"""
    result = apply_child_base(
        column_map=_column_map(),
        main_object="Account",
        main_describe=_account_describe(),
        fetch_object=_fetcher({}),
    )
    assert result.switched is False
    assert any("項目定義が取れない" in n for n in result.notes)


def test_child_without_reference_field_does_not_switch() -> None:
    """子の項目に親への参照項目が無ければ切り替えない。"""
    describe = _account_describe(
        child_relationships=[
            {"childSObject": "Contact", "field": "NoSuchId", "relationshipName": "Contacts"}
        ]
    )
    result = apply_child_base(
        column_map=_column_map(),
        main_object="Account",
        main_describe=describe,
        fetch_object=_fetcher({"Contact": _contact_describe()}),
    )
    assert result.switched is False
    assert any("参照項目" in n for n in result.notes)


# ── クロスフィルタ併用 ────────────────────────────────────────────────────


def test_cross_filter_is_not_converted_when_switched() -> None:
    """FROM を切り替えたときは、 クロスフィルタを副問い合わせにしない。"""
    column_map = _column_map()
    result = apply_child_base(
        column_map=column_map,
        main_object="Account",
        main_describe=_account_describe(),
        fetch_object=_fetcher({"Contact": _contact_describe()}),
    )
    cross_filter = {
        "criteria": [],
        "includesObject": True,
        "primaryEntityField": "Id",
        "relatedEntity": "Contact",
        "relatedEntityJoinField": "AccountId",
    }
    record = _record(
        result.column_map,
        soql_from_object=result.child_object,
        soql_from_relationship=result.relationship_name,
        cross_filters=[cross_filter],
        cross_filter_objects={"Contact": _contact_describe()},
    )
    draft = build_soql(record)
    link = draft.cross_filter_links[0]
    assert link.status == "直接"
    assert link.converted is False
    assert "子から取るレポートのクロスフィルタは未対応" in link.reason
    assert any("子から取るレポートのクロスフィルタは未対応" in m for m in draft.missing)
    assert draft.use_bulk is True


# ── 出力への反映 ──────────────────────────────────────────────────────────


def test_column_names_csv_uses_the_from_object() -> None:
    """``列名の対応.csv`` の主オブジェクトは FROM の値。"""
    from src.tables.column_names_csv import (
        aggregate_resolved_fields,
        resolved_fields_to_csv_rows,
    )

    column_map = _column_map()
    result = apply_child_base(
        column_map=column_map,
        main_object="Account",
        main_describe=_account_describe(),
        fetch_object=_fetcher({"Contact": _contact_describe()}),
    )
    record = _record(
        result.column_map,
        soql_from_object=result.child_object,
        soql_from_relationship=result.relationship_name,
    )
    fields = aggregate_resolved_fields([record], {})
    rows = resolved_fields_to_csv_rows(fields, __import__("datetime").date(2026, 11, 9))
    assert rows
    assert {row["主オブジェクト"] for row in rows} == {"Contact"}


def test_generated_py_has_column_names() -> None:
    """生成する .py の COLUMN_NAMES に、 切り替え後の列名が入る。"""
    column_map = _column_map()
    result = apply_child_base(
        column_map=column_map,
        main_object="Account",
        main_describe=_account_describe(),
        fetch_object=_fetcher({"Contact": _contact_describe()}),
    )
    record = _record(
        result.column_map,
        soql_from_object=result.child_object,
        soql_from_relationship=result.relationship_name,
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
        soql_draft=draft,
        soql_from_object=record.soql_from_object,
        soql_from_relationship=record.soql_from_relationship,
    )
    source = report_py.build_report_py_source(record, class_name="Report1001")
    assert source is not None
    assert "'Account.Name'" in source
    assert "'LastName'" in source
    assert "FROM Contact" in source


def test_markdown_says_the_from_switched() -> None:
    """Markdown の 「取り方」 に FROM の切り替え写出る。"""
    from src import ai_markdown

    column_map = _column_map()
    result = apply_child_base(
        column_map=column_map,
        main_object="Account",
        main_describe=_account_describe(),
        fetch_object=_fetcher({"Contact": _contact_describe()}),
    )
    record = _record(
        result.column_map,
        soql_from_object=result.child_object,
        soql_from_relationship=result.relationship_name,
    )
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
        soql_draft=build_soql(record),
        soql_from_object=record.soql_from_object,
        soql_from_relationship=record.soql_from_relationship,
    )
    markdown = ai_markdown.build_markdown(record)
    assert "FROM は子オブジェクトの Contact にした" in markdown
    # 主オブジェクトは親のまま
    assert "- 主オブジェクト: Account" in markdown


@pytest.mark.parametrize("column_key", ["Contacts.LastName", "Contact.LastName"])
def test_child_column_key_prefix_is_accepted(column_key: str) -> None:
    """列キーの接頭辞がリレーション名でも子オブジェクト名でも解決できる。"""
    column_map = [
        _row("Account.Name", "Name"),
        _row(column_key, "(不明)", field_type="", note="対応フィールドなし"),
    ]
    result = apply_child_base(
        column_map=column_map,
        main_object="Account",
        main_describe=_account_describe(),
        fetch_object=_fetcher({"Contact": _contact_describe()}),
    )
    assert result.switched is True
    rows = {row["列キー"]: row for row in result.column_map}
    assert rows[column_key]["対応フィールドAPI名"] == "LastName"


# ── 解決済みの列も子の列として扱う ─────────────────────────────────────────


def _name_label_describes() -> tuple[dict[str, Any], dict[str, Any]]:
    """表示名が 「名前」 で同じ、 親（ Account ） と子（ Contact ） の describe。

    子の ``Contact.Name`` が表示名の照合で親の ``Account.Name`` に解決される
    状況を再現する。
    """
    parent_describe = _account_describe()
    for field in parent_describe["fields"]:
        if field["name"] == "Name":
            field["label"] = "名前"
    child_describe = _contact_describe()
    child_describe["fields"].append({"name": "Name", "label": "名前", "type": "string"})
    return parent_describe, child_describe


def test_resolved_child_column_switches_to_the_child() -> None:
    """表示名で親に解決済みの列も、 子の列として扱って切り替える。"""
    parent_describe, child_describe = _name_label_describes()
    column_map = [
        _row("Account.Name", "Name"),
        # 子の列が、 表示名の照合で親の Name に解決された状態
        _row("Contact.Name", "Name"),
    ]
    result = apply_child_base(
        column_map=column_map,
        main_object="Account",
        main_describe=parent_describe,
        fetch_object=_fetcher({"Contact": child_describe}),
    )
    assert result.switched is True
    rows = {row["列キー"]: row for row in result.column_map}
    # 子の列は、 子の Name になる （ 親の列ではない ）
    assert rows["Contact.Name"]["対応フィールドAPI名"] == "Name"
    assert rows["Contact.Name"]["所属オブジェクト"] == "Contact"
    assert "子から取る" in rows["Contact.Name"]["備考"]
    # 親の列は、 子から親へ戻るリレーション名が付く
    assert rows["Account.Name"]["対応フィールドAPI名"] == "Account.Name"


def test_resolved_child_column_selects_the_child_field() -> None:
    """解決済みの子の列でも、 SOQL は親の Account.Name ではなく子の Name になる。"""
    parent_describe, child_describe = _name_label_describes()
    column_map = [
        _row("Account.Name", "Name"),
        _row("Contact.Name", "Name"),
    ]
    result = apply_child_base(
        column_map=column_map,
        main_object="Account",
        main_describe=parent_describe,
        fetch_object=_fetcher({"Contact": child_describe}),
    )
    record = _record(
        result.column_map,
        soql_from_object=result.child_object,
        soql_from_relationship=result.relationship_name,
    )
    draft = build_soql(record)
    assert draft.soql == "SELECT Account.Name,\n  Name\nFROM Contact"


def test_child_column_that_cannot_be_resolved_becomes_unknown() -> None:
    """子の describe で解決できない列は、 親の値を残さず (不明) にする。"""
    column_map = [
        _row("Account.Name", "Name"),
        # 子の describe に無い項目が、 親の項目に解決されていた状態
        _row("Contact.NoSuchField", "Name"),
    ]
    result = apply_child_base(
        column_map=column_map,
        main_object="Account",
        main_describe=_account_describe(),
        fetch_object=_fetcher({"Contact": _contact_describe()}),
    )
    assert result.switched is True
    rows = {row["列キー"]: row for row in result.column_map}
    assert rows["Contact.NoSuchField"]["対応フィールドAPI名"] == "(不明)"
    assert "見つかりません" in rows["Contact.NoSuchField"]["備考"]


def test_two_columns_of_the_same_child_are_not_ambiguous() -> None:
    """同じ子への列が複数あっても、 切り替えを妨げない。"""
    column_map = [
        _row("Account.Name", "Name"),
        _row("Contact.LastName", "(不明)", field_type="", note="対応フィールドなし"),
        _row("Contact.FirstName", "(不明)", field_type="", note="対応フィールドなし"),
    ]
    result = apply_child_base(
        column_map=column_map,
        main_object="Account",
        main_describe=_account_describe(),
        fetch_object=_fetcher({"Contact": _contact_describe()}),
    )
    assert result.switched is True
    rows = {row["列キー"]: row for row in result.column_map}
    assert rows["Contact.LastName"]["対応フィールドAPI名"] == "LastName"
    assert rows["Contact.FirstName"]["対応フィールドAPI名"] == "FirstName"


def test_reference_field_name_is_case_insensitive() -> None:
    """手入力の子リレーションの項目名は、 大文字小文字を無視して照合する。"""
    describe = _account_describe(
        child_relationships=[
            {"childSObject": "Contact", "field": "accountid", "relationshipName": "Contacts"}
        ]
    )
    result = apply_child_base(
        column_map=_column_map(),
        main_object="Account",
        main_describe=describe,
        fetch_object=_fetcher({"Contact": _contact_describe()}),
    )
    assert result.switched is True
    assert result.relationship_name == "Account"
