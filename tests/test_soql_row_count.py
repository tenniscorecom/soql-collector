"""件数 （ 2000 行チェック ） に依らず SOQL を作ることを確かめるテスト。

``src.soql_builder.build_soql`` は件数を見ずに SOQL を作ること、 件数が
少ないレポートでも ``.py`` を作ること、 Markdown に SOQL が出ること、
``列の直し.xlsx`` に未解決の列が足されること、 保存した件数から同じ判定
になること を確かめる。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from src import column_fix_xlsx
from src.fetch import ReportRecord
from src.fetch.models import RowCheck
from src.fetch.row_check import ROW_CHECK_OVER, ROW_CHECK_UNDER, ROW_CHECK_UNKNOWN
from src.soql_builder import STATUS_CREATED, build_soql

# ── ヘルパー ──────────────────────────────────────────────────────────────


def _make_record(
    *,
    row_check: RowCheck | None = None,
    main_object: str = "Opportunity",
    detail_columns: list[str] | None = None,
    column_map: list[dict[str, str]] | None = None,
) -> ReportRecord:
    """テスト用の ``ReportRecord`` を作る。"""
    detail_columns = detail_columns or ["Opportunity.Name"]
    if column_map is None:
        column_map = [
            {
                "列キー": key,
                "表示名": key,
                "所属オブジェクト": main_object,
                "対応フィールドAPI名": key.split(".")[-1],
                "型": "string",
                "参照先オブジェクト": "",
                "リレーション名": "",
                "備考": "",
            }
            for key in detail_columns
        ]
    report_metadata = {
        "reportType": {"type": main_object},
        "reportFormat": "TABULAR",
        "detailColumns": detail_columns,
    }
    return ReportRecord(
        key="1001",
        summary="テスト",
        report_id="00O000000000001",
        url="https://example.my.salesforce.com/lightning/r/Report/00O000000000001/view",
        main_object=main_object,
        report={"reportMetadata": report_metadata, "reportExtendedMetadata": {}},
        objects={},
        relations=(),
        column_map=column_map,
        warnings=(),
        row_check=row_check,
    )


def _with_draft(record: ReportRecord) -> ReportRecord:
    """``record`` に ``build_soql`` の結果を入れた record を返す。"""
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
        row_check=record.row_check,
        soql_draft=build_soql(record),
    )


# ── 件数にかかわらず SOQL を作る ──────────────────────────────────────────


@pytest.mark.parametrize("rows", [5, 1000, 1001])
def test_soql_is_made_when_rows_are_not_over(rows: int) -> None:
    """「超えていない」 の何件でも SOQL を作る。"""
    record = _make_record(row_check=RowCheck(status=ROW_CHECK_UNDER, rows=rows, reason=None))
    draft = build_soql(record)
    assert draft.status == STATUS_CREATED
    assert "FROM Opportunity" in draft.soql


def test_soql_is_made_when_rows_are_over_limit() -> None:
    """「超えている」 は件数が分からないので SOQL を作る。"""
    record = _make_record(row_check=RowCheck(status=ROW_CHECK_OVER, rows=None, reason=None))
    draft = build_soql(record)
    assert draft.status == STATUS_CREATED
    assert "FROM Opportunity" in draft.soql


def test_soql_is_made_when_row_check_is_unknown() -> None:
    """「判定不可」 は SOQL を作る。"""
    record = _make_record(row_check=RowCheck(status=ROW_CHECK_UNKNOWN, rows=None, reason="失敗"))
    draft = build_soql(record)
    assert draft.status == STATUS_CREATED


def test_soql_is_made_when_row_check_is_none() -> None:
    """``row_check`` が無いとき（ 未実施 ） は SOQL を作る。"""
    record = _make_record(row_check=None)
    draft = build_soql(record)
    assert draft.status == STATUS_CREATED


# ── 出力への反映 ──────────────────────────────────────────────────────────


def test_report_py_is_written_for_small_report(tmp_path: Path) -> None:
    """件数が少ないレポートでも、 レポートの .py を作る。"""
    from src import report_py

    record = _with_draft(
        _make_record(row_check=RowCheck(status=ROW_CHECK_UNDER, rows=10, reason=None))
    )
    written = report_py.write_report_py_files(tmp_path, [record])
    assert len(written) == 1
    assert written[0].parent.name == report_py.REPORT_PY_DIR
    assert "FROM Opportunity" in written[0].read_text(encoding="utf-8")


def test_markdown_shows_soql_for_small_report() -> None:
    """件数が少ないレポートでも、 Markdown に SOQL 本文が出る。"""
    from src import ai_markdown

    record = _with_draft(
        _make_record(row_check=RowCheck(status=ROW_CHECK_UNDER, rows=10, reason=None))
    )
    markdown = ai_markdown.build_markdown(record)
    assert "```sql" in markdown
    assert "FROM Opportunity" in markdown


def test_column_fix_xlsx_adds_rows_of_small_report(tmp_path: Path) -> None:
    """件数が少ないレポートの未解決列も 列の直し.xlsx に足す。"""
    column_map = [
        {
            "列キー": "Opportunity.MYSTERY",
            "表示名": "不明な列",
            "所属オブジェクト": "Opportunity",
            "対応フィールドAPI名": "(不明)",
            "型": "",
            "参照先オブジェクト": "",
            "リレーション名": "",
            "備考": "対応フィールドなし",
        }
    ]
    record = _with_draft(
        _make_record(
            row_check=RowCheck(status=ROW_CHECK_UNDER, rows=10, reason=None),
            detail_columns=["Opportunity.MYSTERY"],
            column_map=column_map,
        )
    )
    path = tmp_path / "列の直し.xlsx"
    column_fix_xlsx.merge_and_write_after_run(
        path=path,
        existing={},
        records=[record],
        today=column_fix_xlsx.today_local(),
    )
    rows = column_fix_xlsx.load_column_fix(path)
    assert ("opportunity", "Opportunity.MYSTERY") in rows


def test_column_fix_xlsx_keeps_existing_rows(tmp_path: Path) -> None:
    """既にある行と人の入力は消さない。"""
    column_map = [
        {
            "列キー": "Opportunity.MYSTERY",
            "表示名": "不明な列",
            "所属オブジェクト": "Opportunity",
            "対応フィールドAPI名": "(不明)",
            "型": "",
            "参照先オブジェクト": "",
            "リレーション名": "",
            "備考": "対応フィールドなし",
        }
    ]
    record = _with_draft(
        _make_record(
            row_check=RowCheck(status=ROW_CHECK_UNDER, rows=10, reason=None),
            detail_columns=["Opportunity.MYSTERY"],
            column_map=column_map,
        )
    )
    path = tmp_path / "列の直し.xlsx"
    existing = {
        ("opportunity", "Opportunity.MYSTERY"): column_fix_xlsx.ColumnFixRow(
            status=column_fix_xlsx.STATUS_OK,
            main_object="Opportunity",
            display_name="不明な列",
            column_key="Opportunity.MYSTERY",
            auto_soql_name="",
            candidates="",
            fixed_soql_name="Name",
            note="手で直した",
            used_in_reports="1001",
            updated_at="2026-11-01",
        )
    }
    column_fix_xlsx.merge_and_write_after_run(
        path=path,
        existing=existing,
        records=[record],
        today=column_fix_xlsx.today_local(),
    )
    rows = column_fix_xlsx.load_column_fix(path)
    kept = rows[("opportunity", "Opportunity.MYSTERY")]
    assert kept.fixed_soql_name == "Name"
    assert kept.note == "手で直した"


def test_similar_soql_compares_small_reports() -> None:
    """「似たSOQL」 の比較に、 件数が少ないレポートも入る。"""
    from src import soql_similar

    def _record(key: str, rows: int) -> ReportRecord:
        return _with_draft(
            _make_record(row_check=RowCheck(status=ROW_CHECK_UNDER, rows=rows, reason=None))
        )

    small = _record("1001", 10)
    other = _record("1002", 5000)
    found = soql_similar.find_similar_soql([small, other])
    assert found != {}


def test_outcome_builds_draft_for_small_report() -> None:
    """outcome を通した実行でも、 件数が少ないレポートの SOQL を作る。

    ``build_fetch_outcome`` が ``build_soql`` に渡す仮の ``ReportRecord`` に
    ``row_check`` を入れないと、 件数が反映されない。
    """
    from src.fetch.outcome import build_fetch_outcome
    from src.fetch.site_run import FetchState

    state = FetchState()
    state.slim_report = {
        "reportMetadata": {
            "reportType": {"type": "Opportunity"},
            "reportFormat": "TABULAR",
            "detailColumns": ["Opportunity.Name"],
        },
        "reportExtendedMetadata": {},
    }
    state.main_object_name = "Opportunity"
    state.objects = {
        "Opportunity": {
            "name": "Opportunity",
            "label": "商談",
            "custom": False,
            "fields": [{"name": "Name", "label": "名前", "type": "string"}],
            "childRelationships": [],
        }
    }
    state.column_map = [
        {
            "列キー": "Opportunity.Name",
            "表示名": "名前",
            "所属オブジェクト": "Opportunity",
            "対応フィールドAPI名": "Name",
            "型": "string",
            "参照先オブジェクト": "",
            "リレーション名": "",
            "備考": "",
        }
    ]
    state.row_check = RowCheck(status=ROW_CHECK_UNDER, rows=10, reason=None)

    outcome = build_fetch_outcome(
        entry=_entry(),
        report_id="00O000000000001",
        state=state,
        pii_keywords=(),
        pii_person_objects=(),
    )
    assert outcome.record is not None
    assert outcome.record.soql_draft is not None
    assert outcome.record.soql_draft.status == STATUS_CREATED
    assert "FROM Opportunity" in outcome.record.soql_draft.soql


# ── 件数の保存 ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("rows", [0, 1, 999, 1000])
def test_analyze_uses_saved_rows_for_the_same_judgement(tmp_path: Path, rows: int) -> None:
    """保存した件数は、 実行時と同じ判定 （ 超えていない ） になる。"""
    from src.describe_store import DescribeStore
    from src.fetch.offline import OfflineClient
    from src.fetch.site_run import FetchState, run_within_site
    from src.report_store import ReportStore

    slim = {
        "reportMetadata": {
            "reportType": {"type": "Opportunity"},
            "reportFormat": "TABULAR",
            "detailColumns": ["Opportunity.Name"],
        },
        "reportExtendedMetadata": {},
    }
    store = ReportStore.load(tmp_path / "項目定義.csv")
    store.put("org", "00O1", slim)
    store.put_row_count("org", "00O1", status=ROW_CHECK_UNDER, rows=rows, reason=None)

    state = FetchState()
    run_within_site(
        client=OfflineClient(),
        entry=_entry(),
        org="org",
        report_id="00O1",
        store=DescribeStore.empty(),
        report_store=store,
        row_limit=False,
        related_depth=1,
        related_max=40,
        column_fix_overrides={},
        state=state,
    )
    assert state.row_check is not None
    assert state.row_check.status == ROW_CHECK_UNDER
    assert state.row_check.rows == rows


def test_analyze_without_saved_rows_leaves_row_check_none(tmp_path: Path) -> None:
    """保存が無いときは row_check なしのまま。"""
    from src.describe_store import DescribeStore
    from src.fetch.offline import OfflineClient
    from src.fetch.site_run import FetchState, run_within_site
    from src.report_store import ReportStore

    slim = {
        "reportMetadata": {
            "reportType": {"type": "Opportunity"},
            "reportFormat": "TABULAR",
            "detailColumns": ["Opportunity.Name"],
        },
        "reportExtendedMetadata": {},
    }
    store = ReportStore.load(tmp_path / "項目定義.csv")
    store.put("org", "00O1", slim)

    state = FetchState()
    run_within_site(
        client=OfflineClient(),
        entry=_entry(),
        org="org",
        report_id="00O1",
        store=DescribeStore.empty(),
        report_store=store,
        row_limit=False,
        related_depth=1,
        related_max=40,
        column_fix_overrides={},
        state=state,
    )
    assert state.row_check is None


def test_analyze_does_not_write_saved_data(tmp_path: Path) -> None:
    """``--analyze`` は保存データを書き換えない。"""
    from src.describe_store import DescribeStore
    from src.fetch.offline import OfflineClient
    from src.fetch.site_run import FetchState, run_within_site
    from src.report_store import ReportStore

    slim = {
        "reportMetadata": {
            "reportType": {"type": "Opportunity"},
            "reportFormat": "TABULAR",
            "detailColumns": ["Opportunity.Name"],
        },
        "reportExtendedMetadata": {},
    }
    store = ReportStore.load(tmp_path / "項目定義.csv")
    store.put("org", "00O1", slim)
    path = tmp_path / "元データ" / "レポート" / "org__00O1.json"
    before = path.read_text(encoding="utf-8")

    state = FetchState()
    run_within_site(
        client=OfflineClient(),
        entry=_entry(),
        org="org",
        report_id="00O1",
        store=DescribeStore.empty(),
        report_store=store,
        row_limit=False,
        related_depth=1,
        related_max=40,
        column_fix_overrides={},
        state=state,
    )
    assert path.read_text(encoding="utf-8") == before


def _entry() -> Any:
    from src.master import MasterEntry

    return MasterEntry(
        key="1001",
        summary="テスト",
        url="https://example.my.salesforce.com/lightning/r/Report/00O000000000001/view",
        enabled=True,
        survey=True,
    )
