"""``src.report_store``（レポート定義の保存場所）の単体テスト。"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from src.report_store import ReportStore

SLIM = {
    "reportMetadata": {"name": "売上", "detailColumns": ["Opportunity.Name"]},
    "reportExtendedMetadata": {"detailColumnInfo": {"Opportunity.Name": {"label": "商談名"}}},
}


def _store(tmp_path: Path) -> ReportStore:
    return ReportStore.load(tmp_path / "data" / "項目定義.csv")


def test_put_then_get_returns_same_definition_and_dropped_keys(tmp_path: Path) -> None:
    store = _store(tmp_path)

    store.put("example.my.salesforce.com", "00O5g00000ABCDE", SLIM, ("foo", "bar"))
    cached = _store(tmp_path).get("example.my.salesforce.com", "00O5g00000ABCDE")

    assert cached is not None
    assert cached.slim_report == SLIM
    assert cached.dropped_keys == ("foo", "bar")


def test_file_is_placed_next_to_describe_csv_and_readable(tmp_path: Path) -> None:
    _store(tmp_path).put("example.my.salesforce.com", "00O5g00000ABCDE", SLIM, ())

    path = tmp_path / "data" / "レポート定義" / "example.my.salesforce.com__00O5g00000ABCDE.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["レポートID"] == "00O5g00000ABCDE"
    assert document["レポート定義"] == SLIM
    assert "取得日時" in document
    # 人が開いて読める（日本語がエスケープされない）
    assert "商談名" in path.read_text(encoding="utf-8")
    # 一時ファイルは残らない
    assert [p.name for p in path.parent.iterdir()] == [path.name]


def test_counters_count_hits_and_api_fetches(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.get("org", "A") is None
    store.put("org", "A", SLIM, ())

    assert store.get("org", "A") is not None
    assert (store.hit_count, store.api_count) == (1, 1)


def test_different_org_or_report_does_not_share_a_file(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.put("prod.example.com", "A", SLIM, ())

    assert store.get("sandbox.example.com", "A") is None
    assert store.get("prod.example.com", "B") is None


def test_empty_store_never_reads_or_writes(tmp_path: Path) -> None:
    store = ReportStore.empty()

    store.put("org", "A", SLIM, ())

    assert store.get("org", "A") is None
    assert list(tmp_path.iterdir()) == []


def test_empty_definition_is_not_stored(tmp_path: Path) -> None:
    """壊れた応答（定義が空）を貯めると、 次回から壊れたまま使ってしまう。"""
    store = _store(tmp_path)

    store.put("org", "A", {"reportMetadata": {}, "reportExtendedMetadata": {}}, ())

    assert store.get("org", "A") is None
    assert not (tmp_path / "data" / "レポート定義").exists()


@pytest.mark.parametrize(
    "content",
    [
        "{ これはJSONではない",
        "[]",
        json.dumps({"形式": 99, "レポート定義": SLIM, "落としたキー": []}),
        json.dumps({"形式": 1, "レポート定義": {"reportMetadata": []}, "落としたキー": []}),
        json.dumps({"形式": 1, "レポート定義": SLIM, "落としたキー": "foo"}),
    ],
)
def test_broken_file_is_treated_as_not_stored(
    tmp_path: Path, content: str, caplog: pytest.LogCaptureFixture
) -> None:
    store = _store(tmp_path)
    store.put("org", "A", SLIM, ())
    path = next((tmp_path / "data" / "レポート定義").iterdir())
    path.write_text(content, encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        assert store.get("org", "A") is None

    assert "取り直します" in caplog.text
    assert store.hit_count == 0


def test_unwritable_folder_only_warns(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """保存先を作れなくても、 取得は止めない（キャッシュは速くするためだけのもの）。"""
    blocker = tmp_path / "data"
    blocker.write_text("ファイルがフォルダの場所を塞いでいる", encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        ReportStore.load(blocker / "項目定義.csv").put("org", "A", SLIM, ())

    assert "貯められませんでした" in caplog.text
