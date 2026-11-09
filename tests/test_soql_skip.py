"""1000 件以下のレポートを SOQL にしない功能的テスト。

``src.soql_builder.build_soql`` が ``row_check`` を見て SOQL を作らないこと、
``.py`` を作らないこと、 ``列の直し.xlsx`` に足さないこと、
``--rebuild`` で同じ判定になること を確かめる。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src import column_fix_xlsx
from src.fetch import ReportRecord
from src.fetch.models import RowCheck
from src.fetch.row_check import ROW_CHECK_OVER, ROW_CHECK_UNDER, ROW_CHECK_UNKNOWN
from src.soql_builder import (
    SOQL_SKIP_MAX_ROWS,
    STATUS_CREATED,
    STATUS_SKIPPED,
    build_soql,
)

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


# ── 境界 ──────────────────────────────────────────────────────────────────


def test_skip_when_rows_is_1000() -> None:
    """ちょうど 1000 件なら SOQL を作らない。"""
    record = _make_record(row_check=RowCheck(status=ROW_CHECK_UNDER, rows=1000, reason=None))
    draft = build_soql(record)
    assert draft.status == STATUS_SKIPPED
    assert draft.soql == ""
    assert draft.count_soql == ""


def test_skip_when_rows_is_less_than_1000() -> None:
    """1000 件未満も SOQL を作らない。"""
    record = _make_record(row_check=RowCheck(status=ROW_CHECK_UNDER, rows=0, reason=None))
    draft = build_soql(record)
    assert draft.status == STATUS_SKIPPED
    assert draft.soql == ""


def test_not_skip_when_rows_is_1001() -> None:
    """1001 件なら SOQL を作る。"""
    record = _make_record(row_check=RowCheck(status=ROW_CHECK_UNDER, rows=1001, reason=None))
    draft = build_soql(record)
    assert draft.status == STATUS_CREATED
    assert "FROM Opportunity" in draft.soql


def test_not_skip_when_over_limit() -> None:
    """「超えている」 は作らないが SOQL は作る（ 件数が分からない ） 。"""
    record = _make_record(row_check=RowCheck(status=ROW_CHECK_OVER, rows=None, reason=None))
    draft = build_soql(record)
    assert draft.status == STATUS_CREATED
    assert "FROM Opportunity" in draft.soql


def test_not_skip_when_unknown() -> None:
    """「判定不可」 は SOQL を作る。"""
    record = _make_record(row_check=RowCheck(status=ROW_CHECK_UNKNOWN, rows=None, reason="失敗"))
    draft = build_soql(record)
    assert draft.status == STATUS_CREATED


def test_not_skip_when_row_check_is_none() -> None:
    """``row_check`` が無いとき（ 未実施 ） は SOQL を作る。"""
    record = _make_record(row_check=None)
    draft = build_soql(record)
    assert draft.status == STATUS_CREATED


def test_skip_max_rows_constant() -> None:
    """境界の定数が 1000 のまま。"""
    assert SOQL_SKIP_MAX_ROWS == 1000
    assert STATUS_SKIPPED == "対象外（1000件以下）"


# ── 出力への反映 ──────────────────────────────────────────────────────────


def test_report_py_is_not_written_for_skipped_report(tmp_path: Path) -> None:
    """SOQL が無いので、 レポートの .py を作らない。"""
    from src import report_py

    record = _make_record(row_check=RowCheck(status=ROW_CHECK_UNDER, rows=10, reason=None))
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
        row_check=record.row_check,
        soql_draft=build_soql(record),
    )
    written = report_py.write_report_py_files(tmp_path, [record])
    assert written == ()
    assert not (tmp_path / report_py.REPORT_PY_DIR).exists()


def test_markdown_says_not_made_because_of_rows(tmp_path: Path) -> None:
    """Markdown は作るが、 SOQL の節は 「1000件以下なので SOQL にしない」 。"""
    from src import ai_markdown

    record = _make_record(row_check=RowCheck(status=ROW_CHECK_UNDER, rows=10, reason=None))
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
        row_check=record.row_check,
        soql_draft=build_soql(record),
    )
    markdown = ai_markdown.build_markdown(record)
    assert "1000件以下なので SOQL にしない" in markdown
    assert "```sql" not in markdown


def test_column_fix_xlsx_does_not_add_rows_of_skipped_report(tmp_path: Path) -> None:
    """飛ばしたレポートの未解決列は 列の直し.xlsx に足さない。"""
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
    record = _make_record(
        row_check=RowCheck(status=ROW_CHECK_UNDER, rows=10, reason=None),
        detail_columns=["Opportunity.MYSTERY"],
        column_map=column_map,
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
        row_check=record.row_check,
        soql_draft=build_soql(record),
    )
    path = tmp_path / "列の直し.xlsx"
    column_fix_xlsx.merge_and_write_after_run(
        path=path,
        existing={},
        records=[record],
        today=column_fix_xlsx.today_local(),
    )
    # ファイルは作られるが、 未解決の列の行は足されない
    assert column_fix_xlsx.load_column_fix(path) == {}


def test_column_fix_xlsx_keeps_existing_rows_of_skipped_report(tmp_path: Path) -> None:
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
    record = _make_record(
        row_check=RowCheck(status=ROW_CHECK_UNDER, rows=10, reason=None),
        detail_columns=["Opportunity.MYSTERY"],
        column_map=column_map,
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
        row_check=record.row_check,
        soql_draft=build_soql(record),
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


def test_similar_soql_skips_skipped_reports() -> None:
    """「似たSOQL」 の比較から対象外（1000件以下） を外す。"""
    from src import soql_similar

    def _record(key: str, rows: int) -> ReportRecord:
        record = _make_record(row_check=RowCheck(status=ROW_CHECK_UNDER, rows=rows, reason=None))
        return ReportRecord(
            key=key,
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

    skipped = _record("1001", 10)
    created = _record("1002", 5000)
    found = soql_similar.find_similar_soql([skipped, created])
    assert found == {}


def test_outcome_builds_skipped_draft() -> None:
    """outcome を通した実行でも、 1000 件以下なら SOQL を作らない。

    ``build_fetch_outcome`` が ``build_soql`` に渡す仮の ``ReportRecord`` に
    ``row_check`` を入れないと、 この判定が効かない。
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
    assert outcome.record.soql_draft.status == STATUS_SKIPPED
    assert outcome.record.soql_draft.soql == ""


# ── 件数の保存 ────────────────────────────────────────────────────────────


def test_report_store_saves_and_reads_rows(tmp_path: Path) -> None:
    """件数を保存し、 読み出せる。"""
    from src.report_store import ReportStore

    store = ReportStore.load(tmp_path / "項目定義.csv")
    slim = {"reportMetadata": {"detailColumns": []}, "reportExtendedMetadata": {}}
    store.put("org", "00O1", slim, ())
    store.save_rows("org", "00O1", 42)
    cached = store.get("org", "00O1")
    assert cached is not None
    assert cached.rows == 42


def test_report_store_reads_old_file_without_rows(tmp_path: Path) -> None:
    """「件数」 が無い古いファイルでも読める（ 件数は None ） 。"""
    from src.report_store import ReportStore

    store = ReportStore.load(tmp_path / "項目定義.csv")
    slim = {"reportMetadata": {"detailColumns": []}, "reportExtendedMetadata": {}}
    store.put("org", "00O1", slim, ())
    path = tmp_path / "レポート定義" / "org__00O1.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document.pop("件数", None)
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    cached = store.get("org", "00O1")
    assert cached is not None
    assert cached.rows is None


@pytest.mark.parametrize("rows", [0, 1, 999, 1000])
def test_rebuild_uses_saved_rows_for_the_same_judgement(tmp_path: Path, rows: int) -> None:
    """保存した件数から、 実行時と同じ判定 （ 飛ばす ） になる。"""
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
    store.put("org", "00O1", slim, ())
    store.save_rows("org", "00O1", rows)

    state = FetchState()
    run_within_site(
        client=OfflineClient(),
        entry=_entry(),
        org="org",
        report_id="00O1",
        store=DescribeStore.empty(tmp_path / "項目定義.csv"),
        report_store=store,
        row_limit=False,
        refresh_set=set(),
        related_depth=1,
        related_max=40,
        column_fix_overrides={},
        state=state,
        allow_refresh=False,
    )
    assert state.row_check is not None
    assert state.row_check.status == ROW_CHECK_UNDER
    assert state.row_check.rows == rows


def test_rebuild_without_saved_rows_leaves_row_check_none(tmp_path: Path) -> None:
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
    store.put("org", "00O1", slim, ())

    state = FetchState()
    run_within_site(
        client=OfflineClient(),
        entry=_entry(),
        org="org",
        report_id="00O1",
        store=DescribeStore.empty(tmp_path / "項目定義.csv"),
        report_store=store,
        row_limit=False,
        refresh_set=set(),
        related_depth=1,
        related_max=40,
        column_fix_overrides={},
        state=state,
        allow_refresh=False,
    )
    assert state.row_check is None


def test_rebuild_does_not_write_saved_data(tmp_path: Path) -> None:
    """``--rebuild`` は保存データを書き換えない。"""
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
    store.put("org", "00O1", slim, ())
    path = tmp_path / "レポート定義" / "org__00O1.json"
    before = path.read_text(encoding="utf-8")

    state = FetchState()
    run_within_site(
        client=OfflineClient(),
        entry=_entry(),
        org="org",
        report_id="00O1",
        store=DescribeStore.empty(tmp_path / "項目定義.csv"),
        report_store=store,
        row_limit=False,
        refresh_set=set(),
        related_depth=1,
        related_max=40,
        column_fix_overrides={},
        state=state,
        allow_refresh=False,
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
