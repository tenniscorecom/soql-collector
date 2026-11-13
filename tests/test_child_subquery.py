"""子の列を SELECT の子リレーション副問い合わせで取るのテスト。

``src.mapping.child_subquery.apply_child_shape`` が
「副問い合わせ」か「子から取る」かを決めて列対応表を書き換えることと、
その結果の SOQL・出力が正しいことを確かめる。
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any, ClassVar, Self

from src import report_py
from src.fetch import ReportRecord
from src.mapping.child_base import apply_child_base
from src.mapping.child_subquery import MODE_CHILD_BASE, MODE_SUBQUERY, apply_child_shape
from src.soql_builder import STATUS_NEEDS_CHECK, STATUS_PARTIAL, build_soql

# ── ヘルパー ──────────────────────────────────────────────────────────────


class _FakeClient:
    """偽物の Salesforce クライアント。"""

    def __init__(
        self,
        describe: dict,
        object_describes: dict[str, dict] | None = None,
    ) -> None:
        self._describe = describe
        self._object_describes = object_describes or {}
        self.report = self
        self.describe_object_calls: list[str] = []

    def describe(self, report_id: str) -> dict:
        return self._describe

    def describe_object(self, name: str) -> dict:
        self.describe_object_calls.append(name)
        return self._object_describes.get(name, {"name": name, "fields": []})

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *args: object) -> None:
        return None


class _FakeSite:
    def __init__(self, client: _FakeClient) -> None:
        self._client = client

    def __call__(self) -> Self:
        return self

    def __enter__(self) -> _FakeClient:
        return self._client

    def __exit__(self, *args: object) -> None:
        return None


def _site_for(client: _FakeClient):
    """``site_for(url)`` 互換の関数。"""
    site = _FakeSite(client)

    def resolver(url: str) -> _FakeSite:
        return site

    return resolver, site


def _settings(tmp_path: Path, related_max: int = 40, related_depth: int = 1) -> dict:
    """``run_fetch`` に渡す kwargs を返す。"""
    return {
        "related_max": related_max,
        "related_depth": related_depth,
        "row_limit": True,
        "pii_keywords": ("name", "電話"),
        "pii_person_objects": ("Contact",),
    }


def _account_describe(
    child_relationships: list[dict[str, Any]] | None = None,
    fields: list[dict[str, Any]] | None = None,
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
    if fields is None:
        fields = [
            {"name": "Id", "label": "ID", "type": "id"},
            {"name": "Name", "label": "取引先名", "type": "string"},
        ]
    return {
        "name": "Account",
        "label": "取引先",
        "custom": False,
        "fields": fields,
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
            {"name": "Email", "label": "メール", "type": "email"},
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
    child_subquery: Any = None,
    child_subquery_checks: tuple[str, ...] = (),
    soql_from_object: str | None = None,
    soql_from_relationship: str = "",
    report_filters: list[dict[str, Any]] | None = None,
    sort_by: list[dict[str, Any]] | None = None,
    cross_filters: list[dict[str, Any]] | None = None,
    cross_filter_objects: dict[str, dict[str, Any]] | None = None,
) -> ReportRecord:
    """副問い合わせの形にした後の record を作る。"""
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
        child_subquery=child_subquery,
        child_subquery_checks=child_subquery_checks,
    )


def _apply(
    column_map: list[dict[str, str]],
    *,
    human_fixes: dict[str, str] | None = None,
    has_cross_filter: bool = False,
    main_describe: dict[str, Any] | None = None,
) -> Any:
    """``apply_child_shape`` を呼ぶ。"""
    return apply_child_shape(
        column_map=column_map,
        main_object="Account",
        main_describe=main_describe if main_describe is not None else _account_describe(),
        fetch_object=_fetcher({"Contact": _contact_describe()}),
        human_fixes=human_fixes or {},
        has_cross_filter=has_cross_filter,
    )


def _load_generated(source: str) -> dict[str, object]:
    """生成した Python を実行して、 モジュールの名前空間返す。"""
    base_module = types.ModuleType("src.reports.base")

    class SoqlReport:
        KEY: ClassVar[str] = ""
        COLUMN_NAMES: ClassVar[dict[str, str]] = {}

    base_module.SoqlReport = SoqlReport  # type: ignore[attr-defined]
    package_module = types.ModuleType("src.reports")
    saved = {name: sys.modules.get(name) for name in ("src", "src.reports", "src.reports.base")}
    sys.modules["src.reports"] = package_module
    sys.modules["src.reports.base"] = base_module
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


# ── A: 人が「直したSOQL列名」に書いたとき ────────────────────────────────


def test_human_written_child_column_becomes_a_subquery() -> None:
    """A: 人が ``Contacts.LastName`` と書いた列は、 子の副問い合わせで取る。"""
    result = _apply(_column_map(), human_fixes={"Contact.LastName": "Contacts.LastName"})
    assert result.mode == MODE_SUBQUERY
    assert result.subquery is not None
    assert result.subquery.relation_name == "Contacts"
    assert result.subquery.child_object == "Contact"
    assert result.subquery.fields == ("LastName",)
    rows = {row["列キー"]: row for row in result.column_map}
    # 子の列は 「Contacts.LastName」 の形になる
    assert rows["Contact.LastName"]["対応フィールドAPI名"] == "Contacts.LastName"
    assert rows["Contact.LastName"]["所属オブジェクト"] == "Contact"
    # 親の列はそのまま
    assert rows["Account.Name"]["対応フィールドAPI名"] == "Name"


def test_subquery_soql_puts_the_child_in_one_parenthesis() -> None:
    """副問い合わせにした SOQL は、 親の列のうしろに 1 つだけ入る。"""
    result = _apply(_column_map(), human_fixes={"Contact.LastName": "Contacts.LastName"})
    draft = build_soql(_record(result.column_map, child_subquery=result.subquery))
    assert draft.soql == "SELECT Name,\n  (SELECT LastName FROM Contacts)\nFROM Account"
    assert draft.status == STATUS_NEEDS_CHECK
    assert draft.use_bulk is False
    assert any("子の副問い合わせ" in blocker for blocker in draft.bulk_blockers)
    assert any("子の副問い合わせにした" in check for check in draft.checks)
    assert "親 Account" in draft.checks[0]
    assert "子リレーション Contacts" in draft.checks[0]


def test_subquery_count_soql_has_no_child_subquery() -> None:
    """件数用の COUNT() は、 子の副問い合わせを含まない。"""
    result = _apply(_column_map(), human_fixes={"Contact.LastName": "Contacts.LastName"})
    draft = build_soql(_record(result.column_map, child_subquery=result.subquery))
    assert draft.count_soql == "SELECT COUNT()\nFROM Account"
    assert any("親の件数" in check for check in draft.checks)


def test_subquery_select_items_use_the_relation_prefix() -> None:
    """select_items は ``Contacts.LastName`` の形。"""
    result = _apply(_column_map(), human_fixes={"Contact.LastName": "Contacts.LastName"})
    draft = build_soql(_record(result.column_map, child_subquery=result.subquery))
    assert draft.select_items == ("Name", "Contacts.LastName")


def test_relation_name_uses_the_describe_spelling() -> None:
    """人が小文字で書いても、 出力は describe の綴り。"""
    result = _apply(_column_map(), human_fixes={"Contact.LastName": "contacts.LastName"})
    assert result.subquery is not None
    assert result.subquery.relation_name == "Contacts"
    rows = {row["列キー"]: row for row in result.column_map}
    assert rows["Contact.LastName"]["対応フィールドAPI名"] == "Contacts.LastName"


def test_ambiguous_prefix_is_a_reference_field_and_needs_check() -> None:
    """A: 接頭辞が子リレーションにも参照項目にも一致したら要確認。"""
    describe = _account_describe(
        fields=[
            {"name": "Id", "label": "ID", "type": "id"},
            {"name": "Name", "label": "取引先名", "type": "string"},
            {
                "name": "Contacts",
                "label": "contacts",
                "type": "reference",
                "relationshipName": "Contacts",
                "referenceTo": ["Contact"],
            },
        ],
    )
    result = _apply(
        _column_map(),
        human_fixes={"Contact.LastName": "Contacts.LastName"},
        main_describe=describe,
    )
    # 曖昧なので副問い合わせにしない（ 参照項目として扱う ）
    assert result.mode == MODE_CHILD_BASE
    assert any("参照項目として扱った" in check for check in result.checks)
    rows = {row["列キー"]: row for row in result.column_map}
    assert rows["Contact.LastName"]["対応フィールドAPI名"] == "(不明)"


def test_unknown_child_field_needs_check_but_is_used() -> None:
    """A: 子の項目定義に無い項目でも、 列はそのまま使う。"""
    result = _apply(_column_map(), human_fixes={"Contact.LastName": "Contacts.NoSuchField"})
    assert result.mode == MODE_SUBQUERY
    assert any("に項目 NoSuchField が無い" in check for check in result.checks)
    rows = {row["列キー"]: row for row in result.column_map}
    assert rows["Contact.LastName"]["対応フィールドAPI名"] == "Contacts.NoSuchField"


def test_two_child_relations_make_it_missing() -> None:
    """A: 子リレーションが 2 種類なら作れないので missing。"""
    describe = _account_describe(
        child_relationships=[
            {"childSObject": "Contact", "field": "AccountId", "relationshipName": "Contacts"},
            {
                "childSObject": "Opportunity",
                "field": "AccountId",
                "relationshipName": "Opportunities",
            },
        ],
    )
    column_map = [
        _row("Account.Name", "Name"),
        _row("Contact.LastName", "(不明)", field_type="", note="対応フィールドなし"),
        _row("Opportunity.Amount", "(不明)", field_type="", note="対応フィールドなし"),
    ]
    result = _apply(
        column_map,
        human_fixes={
            "Contact.LastName": "Contacts.LastName",
            "Opportunity.Amount": "Opportunities.Amount",
        },
        main_describe=describe,
    )
    assert result.mode == MODE_SUBQUERY
    assert result.subquery is None
    assert any("子リレーションが複数" in note for note in result.notes)
    rows = {row["列キー"]: row for row in result.column_map}
    assert rows["Contact.LastName"]["対応フィールドAPI名"] == "(不明)"
    # missing に入れて、 状態は 「一部作れず」 になる
    draft = build_soql(_record(result.column_map, child_subquery=result.subquery))
    assert any("子リレーションが複数" in m for m in draft.missing)
    assert draft.status == STATUS_PARTIAL
    assert rows["Opportunity.Amount"]["対応フィールドAPI名"] == "(不明)"


def test_filter_on_a_child_column_is_missing() -> None:
    """子の列の絞り込みは副問い合わせの外に書けないので missing。"""
    result = _apply(_column_map(), human_fixes={"Contact.LastName": "Contacts.LastName"})
    record = _record(
        result.column_map,
        child_subquery=result.subquery,
        report_filters=[{"column": "Contact.LastName", "operator": "equals", "value": "山田"}],
    )
    draft = build_soql(record)
    assert any("子の列の絞り込み" in m for m in draft.missing)
    assert draft.status == STATUS_PARTIAL


def test_sort_on_a_child_column_is_a_check() -> None:
    """子の列の並び順は省くが、 行は変わらないので checks。"""
    result = _apply(_column_map(), human_fixes={"Contact.LastName": "Contacts.LastName"})
    record = _record(
        result.column_map,
        child_subquery=result.subquery,
        sort_by=[{"sortColumn": "Contact.LastName", "sortOrder": "Asc"}],
    )
    draft = build_soql(record)
    assert any("子の列の並び順" in c for c in draft.checks)
    assert draft.missing == ()


# ── B: 子の列 + クロスフィルタ ───────────────────────────────────────────


def test_cross_filter_report_uses_the_subquery() -> None:
    """B: 子の列とクロスフィルタがあるときは、 副問い合わせにする。"""
    result = _apply(_column_map(), has_cross_filter=True)
    assert result.mode == MODE_SUBQUERY
    assert result.subquery is not None
    cross_filter = {
        "criteria": [{"column": "LastName", "operator": "equals", "value": "山田"}],
        "includesObject": True,
        "primaryEntityField": "Id",
        "relatedEntity": "Contact",
        "relatedEntityJoinField": "AccountId",
    }
    record = _record(
        result.column_map,
        child_subquery=result.subquery,
        cross_filters=[cross_filter],
        cross_filter_objects={"Contact": _contact_describe()},
    )
    draft = build_soql(record)
    # 子の副問い合わせと、 クロスフィルタの副問い合わせの両方が入る
    assert "(SELECT LastName FROM Contacts)" in draft.soql
    assert "WHERE Id IN (SELECT AccountId FROM Contact WHERE LastName = '山田')" in draft.soql
    # 従来の 「子から取るレポートのクロスフィルタは未対応」 の missing は消える
    assert not any("子から取るレポートのクロスフィルタは未対応" in m for m in draft.missing)
    assert draft.missing == ()
    assert draft.use_bulk is False


def test_cross_filter_report_without_a_child_column_keeps_the_from() -> None:
    """子の列が無いレポートは、 副問い合わせにしない。"""
    result = _apply([_row("Account.Name", "Name")], has_cross_filter=True)
    assert result.mode == MODE_CHILD_BASE
    assert result.subquery is None


def test_human_value_without_a_relation_prefix_is_used_as_the_child_field() -> None:
    """B: 人が「LastName」とだけ書いた列も、 子の項目として使う。"""
    result = _apply(
        _column_map(),
        human_fixes={"Contact.LastName": "LastName"},
        has_cross_filter=True,
    )
    assert result.mode == MODE_SUBQUERY
    assert result.subquery is not None
    assert result.subquery.fields == ("LastName",)
    rows = {row["列キー"]: row for row in result.column_map}
    assert rows["Contact.LastName"]["対応フィールドAPI名"] == "Contacts.LastName"


# ── C: それ以外 ──────────────────────────────────────────────────────────


def test_child_column_without_a_cross_filter_keeps_the_child_base() -> None:
    """C: 子の列だけのレポートは、 従来どおり 子から取る。"""
    result = _apply(_column_map())
    assert result.mode == MODE_CHILD_BASE
    assert result.subquery is None


def test_child_base_report_still_switches_the_from() -> None:
    """C: 子から取る形になったときの SOQL は 今までどおり。"""
    column_map = _column_map()
    base = apply_child_base(
        column_map=column_map,
        main_object="Account",
        main_describe=_account_describe(),
        fetch_object=_fetcher({"Contact": _contact_describe()}),
    )
    assert base.switched is True
    record = _record(
        base.column_map,
        soql_from_object=base.child_object,
        soql_from_relationship=base.relationship_name,
    )
    draft = build_soql(record)
    assert draft.soql == "SELECT Account.Name,\n  LastName\nFROM Contact"


# ── 出力への反映 ──────────────────────────────────────────────────────────


def test_column_names_csv_uses_the_parent_and_the_child_column_name() -> None:
    """``列名の対応.csv`` は、 主オブジェクトが親、 子の列が ``Contacts.LastName`` 。"""
    from datetime import date

    from src.tables.column_names_csv import (
        aggregate_resolved_fields,
        resolved_fields_to_csv_rows,
    )

    result = _apply(_column_map(), human_fixes={"Contact.LastName": "Contacts.LastName"})
    record = _record(result.column_map, child_subquery=result.subquery)
    fields = aggregate_resolved_fields([record], {})
    rows = resolved_fields_to_csv_rows(fields, date(2026, 11, 13))
    assert {row["主オブジェクト"] for row in rows} == {"Account"}
    soql_names = {row["SOQL列名"] for row in rows}
    assert "Name" in soql_names
    assert "Contacts.LastName" in soql_names


def test_generated_py_soql_returns_the_same_string() -> None:
    """生成した .py の ``soql()`` が、 下書きと同じ文字列を返す。"""
    result = _apply(_column_map(), human_fixes={"Contact.LastName": "Contacts.LastName"})
    record = _record(result.column_map, child_subquery=result.subquery)
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
        child_subquery=record.child_subquery,
        child_subquery_checks=record.child_subquery_checks,
    )
    source = report_py.build_report_py_source(record, class_name="Report1001")
    assert source is not None
    namespace = _load_generated(source)
    report_class = namespace["Report1001"]
    assert report_class.soql(report_class) == draft.soql  # type: ignore[attr-defined]
    assert "'Contacts.LastName'" in source
    assert "USE_BULK = False" in source


def test_markdown_says_the_child_subquery() -> None:
    """Markdown の 「取り方」 に 子の副問い合わせ写出る。"""
    from src import ai_markdown

    result = _apply(_column_map(), human_fixes={"Contact.LastName": "Contacts.LastName"})
    record = _record(result.column_map, child_subquery=result.subquery)
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
        child_subquery=record.child_subquery,
    )
    markdown = ai_markdown.build_markdown(record)
    assert "子の列は SELECT の副問い合わせで取る" in markdown


# ── 一連の取得を通した確認 ────────────────────────────────────────────────


def test_run_fetch_with_a_human_written_child_column(tmp_path: Path) -> None:
    """「列の直し.xlsx」 の値を入れたとき、 一連の取得で副問い合わせの SOQL になる。"""
    from src.column_fix_xlsx import ColumnFixRow
    from src.describe_store import DescribeStore
    from src.fetch import run_fetch
    from src.master import MasterEntry

    metadata = {
        "reportMetadata": {
            "reportType": {"type": "Account"},
            "reportFormat": "TABULAR",
            "detailColumns": ["Account.Name", "Contact.LastName"],
        },
        "reportExtendedMetadata": {
            "detailColumnInfo": {
                "Account.Name": {"label": "取引先名"},
                "Contact.LastName": {"label": "姓"},
            }
        },
    }
    client = _FakeClient(
        describe=metadata,
        object_describes={
            "Account": _account_describe(),
            "Contact": _contact_describe(),
        },
    )
    site_for, _site = _site_for(client)
    overrides = {
        ("account", "Contact.LastName"): ColumnFixRow(
            status="OK",
            main_object="Account",
            display_name="姓",
            column_key="Contact.LastName",
            auto_soql_name="",
            candidates="",
            fixed_soql_name="Contacts.LastName",
            note="",
            used_in_reports="",
            updated_at="",
        ),
    }
    entry = MasterEntry(
        key="1",
        summary="",
        url="https://example.my.salesforce.com/lightning/r/Report/00O000000000001/view",
        enabled=True,
    )
    outcomes = run_fetch(
        [entry],
        site_for=site_for,
        describe_store=DescribeStore.empty(),
        column_fix_overrides=overrides,
        **_settings(tmp_path),
    )
    assert outcomes[0].status == "ok"
    record = outcomes[0].record
    assert record is not None
    draft = record.soql_draft
    assert draft is not None
    # FROM は親のまま、 子の列は副問い合わせ
    assert draft.soql == "SELECT Name,\n  (SELECT LastName FROM Contacts)\nFROM Account"
    assert draft.use_bulk is False
    row = next(r for r in record.column_map if r["列キー"] == "Contact.LastName")
    assert row["対応フィールドAPI名"] == "Contacts.LastName"
    assert row["所属オブジェクト"] == "Contact"
