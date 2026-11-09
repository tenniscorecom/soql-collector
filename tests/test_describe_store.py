"""``src.describe_store``: オブジェクトの describe の原本の保存。"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from src.describe_store import (
    DESCRIBE_CSV_COLUMNS,
    TRUE_MARK,
    DescribeStore,
    describe_to_csv_rows,
    org_from_url,
)

ORG = "example.my.salesforce.com"
ORIGINAL: dict[str, Any] = {
    "name": "Account",
    "label": "取引先",
    "custom": False,
    "recordTypeInfos": [{"name": "残る"}],
    "childRelationships": [
        {"childSObject": "Contact", "field": "AccountId", "relationshipName": "Contacts"}
    ],
    "fields": [
        {
            "name": "Name",
            "label": "名前",
            "type": "string",
            "custom": False,
            "filterable": True,
            "sortable": False,
            "byteLength": 765,
        },
    ],
}


def _csv_path(tmp_path: Path) -> Path:
    return tmp_path / "data" / "項目定義.csv"


def test_org_from_url_lowercases_host() -> None:
    assert org_from_url("https://EXAMPLE.my.salesforce.com/lightning") == ORG


def test_org_from_url_distinguishes_orgs() -> None:
    assert org_from_url("https://a.my.salesforce.com") != org_from_url(
        "https://b.my.salesforce.com"
    )


def test_api_success_saves_original_as_is_and_next_store_reads_it(tmp_path: Path) -> None:
    """絞らずに保存する。 ファイル名はオブジェクト名の小文字。"""
    DescribeStore.load(_csv_path(tmp_path)).record_api_success(ORG, "Account", ORIGINAL)

    path = tmp_path / "data" / "元データ" / "オブジェクト" / f"{ORG}__account.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["形式"] == 1
    assert document["原本"] == ORIGINAL

    store = DescribeStore.load(_csv_path(tmp_path))
    cached = store.lookup(ORG, "ACCOUNT")
    assert cached is not None
    assert cached["describe"] == ORIGINAL
    assert cached["source"] == "saved"
    assert cached["fetched_at"] == document["取得日時"]
    assert store.saved_hit_count == 1
    assert store.fetched_at(ORG, "account") == document["取得日時"]


def test_force_does_not_read_saved_but_returns_what_this_run_fetched(tmp_path: Path) -> None:
    DescribeStore.load(_csv_path(tmp_path)).record_api_success(ORG, "Account", ORIGINAL)

    store = DescribeStore.load(_csv_path(tmp_path), force=True)
    assert store.lookup(ORG, "Account") is None

    newer = {**ORIGINAL, "label": "新しい"}
    store.record_api_success(ORG, "Account", newer)
    cached = store.lookup(ORG, "Account")
    assert cached is not None
    assert cached["describe"] == newer
    saved = DescribeStore.load(_csv_path(tmp_path)).lookup(ORG, "Account")
    assert saved is not None
    assert saved["describe"] == newer


def test_different_orgs_are_separate_files(tmp_path: Path) -> None:
    DescribeStore.load(_csv_path(tmp_path)).record_api_success(ORG, "Account", ORIGINAL)

    assert (
        DescribeStore.load(_csv_path(tmp_path)).lookup("other.my.salesforce.com", "Account") is None
    )


def test_failure_stays_in_memory_only(tmp_path: Path) -> None:
    """失敗は同じ実行の中でだけ覚える。 次の実行ではまた試す。"""
    store = DescribeStore.load(_csv_path(tmp_path))
    store.record_api_failure(ORG, "Account", "HTTP 404")

    cached = store.lookup(ORG, "Account")
    assert cached is not None
    assert cached["describe"] is None
    assert cached["warning"] == "HTTP 404"
    assert store.failure_count == 1
    assert not (tmp_path / "data").exists()
    assert DescribeStore.load(_csv_path(tmp_path)).lookup(ORG, "Account") is None


def test_empty_store_saves_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    store = DescribeStore.empty()
    store.record_api_success(ORG, "Account", ORIGINAL)
    assert store.lookup(ORG, "Account") is not None
    assert list(tmp_path.iterdir()) == []


def test_broken_file_is_treated_as_not_saved(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    folder = tmp_path / "data" / "元データ" / "オブジェクト"
    folder.mkdir(parents=True)
    (folder / f"{ORG}__account.json").write_text("{壊れた", encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        assert DescribeStore.load(_csv_path(tmp_path)).lookup(ORG, "Account") is None
    assert "無いものとして扱います" in caplog.text


def test_describe_to_csv_rows_uses_all_values_of_the_original() -> None:
    """AI チャット用の行。 絞ると落ちる値（絞り込み可・31 件目以降の選択肢）も出る。"""
    values = [{"active": True, "value": f"値{i}"} for i in range(31)]
    describe = {
        "name": "Account",
        "label": "取引先",
        "custom": True,
        "fields": [
            {
                "name": "Status__c",
                "label": "状態",
                "type": "picklist",
                "custom": True,
                "filterable": True,
                "sortable": False,
                "referenceTo": ["User", "Group"],
                "relationshipName": "Status__r",
                "picklistValues": [*values, {"active": False, "value": "無効"}],
            }
        ],
    }

    rows = describe_to_csv_rows(ORG, "Account", describe, "2026-11-09T00:00:00+00:00")

    assert list(rows[0]) == list(DESCRIBE_CSV_COLUMNS)
    row = rows[0]
    assert row["絞り込み可"] == TRUE_MARK
    assert row["並べ替え可"] == ""
    assert row["カスタムオブジェクト"] == TRUE_MARK
    assert row["参照先"] == "User|Group"
    assert row["選択肢"].split("|")[-1] == "値30"
    assert "無効" not in row["選択肢"]
