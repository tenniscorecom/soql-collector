"""``src.report_store``: レポートの原本と件数の保存。"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from comken.exceptions import CSVError

from src.report_store import ROW_COUNT_CSV_COLUMNS, ReportStore

ORG = "example.my.salesforce.com"
ORIGINAL = {
    "reportMetadata": {"name": "a", "unknownKey": "残る"},
    "reportExtendedMetadata": {},
    "factMap": {"残る": 1},
}


def _csv_path(tmp_path: Path) -> Path:
    return tmp_path / "data" / "項目定義.csv"


def test_put_saves_original_as_is_and_next_store_reads_it(tmp_path: Path) -> None:
    """絞らずに保存し、 次の実行のストアがそのまま読める。"""
    ReportStore.load(_csv_path(tmp_path)).put(ORG, "00O1", ORIGINAL)

    path = tmp_path / "data" / "元データ" / "レポート" / f"{ORG}__00O1.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["形式"] == 1
    assert document["組織"] == ORG
    assert document["取得日時"]
    assert document["原本"] == ORIGINAL

    store = ReportStore.load(_csv_path(tmp_path))
    assert store.get(ORG, "00O1") == ORIGINAL
    assert store.fetched_at(ORG, "00O1") == document["取得日時"]
    assert store.hit_count == 1


def test_force_does_not_read_saved_but_returns_what_this_run_fetched(tmp_path: Path) -> None:
    ReportStore.load(_csv_path(tmp_path)).put(ORG, "00O1", ORIGINAL)

    store = ReportStore.load(_csv_path(tmp_path), force=True)
    assert store.get(ORG, "00O1") is None

    newer = {"reportMetadata": {"name": "新しい"}}
    store.put(ORG, "00O1", newer)
    assert store.get(ORG, "00O1") == newer
    assert ReportStore.load(_csv_path(tmp_path)).get(ORG, "00O1") == newer


def test_different_org_or_report_does_not_share_a_file(tmp_path: Path) -> None:
    store = ReportStore.load(_csv_path(tmp_path))
    store.put(ORG, "00O1", ORIGINAL)

    fresh = ReportStore.load(_csv_path(tmp_path))
    assert fresh.get("other.my.salesforce.com", "00O1") is None
    assert fresh.get(ORG, "00O2") is None


def test_empty_store_keeps_memory_only(tmp_path: Path) -> None:
    store = ReportStore.empty()
    store.put(ORG, "00O1", ORIGINAL)
    assert store.get(ORG, "00O1") == ORIGINAL
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("broken", [{}, {"reportMetadata": {}}])
def test_empty_response_is_not_saved(tmp_path: Path, broken: dict[str, object]) -> None:
    ReportStore.load(_csv_path(tmp_path)).put(ORG, "00O1", broken)
    assert ReportStore.load(_csv_path(tmp_path)).get(ORG, "00O1") is None


@pytest.mark.parametrize(
    "content",
    [
        "{壊れた",
        "[]",
        json.dumps({"形式": 999, "原本": {}}),
        json.dumps({"形式": 1, "原本": "文字列"}),
    ],
)
def test_broken_file_is_treated_as_not_saved(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, content: str
) -> None:
    folder = tmp_path / "data" / "元データ" / "レポート"
    folder.mkdir(parents=True)
    (folder / f"{ORG}__00O1.json").write_text(content, encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        assert ReportStore.load(_csv_path(tmp_path)).get(ORG, "00O1") is None
    assert "無いものとして扱います" in caplog.text


def test_unwritable_folder_only_warns(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "元データ").write_text("フォルダを作れなくする", encoding="utf-8")

    store = ReportStore.load(_csv_path(tmp_path))
    with caplog.at_level(logging.WARNING):
        store.put(ORG, "00O1", ORIGINAL)

    assert "保存できませんでした" in caplog.text
    # この実行の中では使える
    assert store.get(ORG, "00O1") == ORIGINAL


def test_row_counts_round_trip_all_statuses(tmp_path: Path) -> None:
    store = ReportStore.load(_csv_path(tmp_path))
    store.put_row_count(ORG, "00O1", status="超えていない", rows=12, reason=None)
    store.put_row_count(ORG, "00O2", status="超えている", rows=None, reason=None)
    store.put_row_count(ORG, "00O3", status="判定不可", rows=None, reason="形式が違う")

    fresh = ReportStore.load(_csv_path(tmp_path))
    first = fresh.get_row_count(ORG, "00O1")
    assert first is not None
    assert (first.status, first.rows, first.reason) == ("超えていない", 12, None)
    assert first.fetched_at
    second = fresh.get_row_count(ORG, "00O2")
    assert second is not None
    assert (second.status, second.rows, second.reason) == ("超えている", None, None)
    third = fresh.get_row_count(ORG, "00O3")
    assert third is not None
    assert (third.status, third.rows, third.reason) == ("判定不可", None, "形式が違う")
    assert fresh.get_row_count(ORG, "00O4") is None


def test_row_count_force_ignores_saved_but_keeps_other_reports(tmp_path: Path) -> None:
    """取り直しに失敗したレポートの件数を消さない。"""
    first = ReportStore.load(_csv_path(tmp_path))
    first.put_row_count(ORG, "00O1", status="超えていない", rows=12, reason=None)
    first.put_row_count(ORG, "00O2", status="超えていない", rows=34, reason=None)

    forced = ReportStore.load(_csv_path(tmp_path), force=True)
    assert forced.get_row_count(ORG, "00O1") is None
    forced.put_row_count(ORG, "00O1", status="超えていない", rows=99, reason=None)
    refreshed = forced.get_row_count(ORG, "00O1")
    assert refreshed is not None
    assert refreshed.rows == 99

    fresh = ReportStore.load(_csv_path(tmp_path))
    kept = fresh.get_row_count(ORG, "00O2")
    assert kept is not None
    assert kept.rows == 34


def test_row_count_csv_with_wrong_header_raises(tmp_path: Path) -> None:
    raw_dir = tmp_path / "data" / "元データ"
    raw_dir.mkdir(parents=True)
    (raw_dir / "件数.csv").write_text("違う,見出し\na,b\n", encoding="utf-8")

    with pytest.raises(CSVError, match="見出し"):
        ReportStore.load(_csv_path(tmp_path))


def test_row_count_csv_has_no_row_data_columns(tmp_path: Path) -> None:
    """件数.csv の列は決まった 6 列だけ（行データの置き場が無い）。"""
    store = ReportStore.load(_csv_path(tmp_path))
    store.put_row_count(ORG, "00O1", status="超えていない", rows=1, reason=None)
    text = (tmp_path / "data" / "元データ" / "件数.csv").read_text(encoding="utf-8-sig")
    assert text.splitlines()[0] == ",".join(ROW_COUNT_CSV_COLUMNS)


def test_prune_removes_reports_not_kept_and_their_row_counts(tmp_path: Path) -> None:
    store = ReportStore.load(_csv_path(tmp_path))
    store.put(ORG, "00O1", ORIGINAL)
    store.put(ORG, "00O2", ORIGINAL)
    store.put_row_count(ORG, "00O1", status="超えていない", rows=1, reason=None)
    store.put_row_count(ORG, "00O2", status="超えていない", rows=2, reason=None)

    removed = store.prune({"00O1"})

    assert removed == [f"{ORG}__00O2.json"]
    fresh = ReportStore.load(_csv_path(tmp_path))
    assert fresh.get(ORG, "00O1") == ORIGINAL
    assert fresh.get(ORG, "00O2") is None
    assert fresh.get_row_count(ORG, "00O1") is not None
    assert fresh.get_row_count(ORG, "00O2") is None


def test_prune_with_nothing_to_keep_removes_nothing(tmp_path: Path) -> None:
    store = ReportStore.load(_csv_path(tmp_path))
    store.put(ORG, "00O1", ORIGINAL)
    store.put_row_count(ORG, "00O1", status="超えていない", rows=1, reason=None)

    assert store.prune(set()) == []

    fresh = ReportStore.load(_csv_path(tmp_path))
    assert fresh.get(ORG, "00O1") == ORIGINAL
    assert fresh.get_row_count(ORG, "00O1") is not None


def test_prune_only_touches_files_made_by_the_store(tmp_path: Path) -> None:
    store = ReportStore.load(_csv_path(tmp_path))
    store.put(ORG, "00O1", ORIGINAL)
    folder = tmp_path / "data" / "元データ" / "レポート"
    other = folder / "メモ.json"
    other.write_text("{}", encoding="utf-8")

    store.prune({"00O1"})

    assert other.exists()
