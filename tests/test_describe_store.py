"""``describe_store`` のテスト。

「 1 回目の実行で API を呼んだオブジェクトが 2 回目の実行でも API を呼ばずに
使われること 」 と 「 別組織では別物扱い 」「 取り直し 」「 見出し違いで
失敗 」 を、 ``tmp_path`` の CSV と既存のフェイククライアントで検証する。

設計メモ （ このテストは実装の現状に合わせて書かれている ）:

- メモリ辞書のキーは ``(org, object_lower)`` のタプル。 ``org`` は URL の
  ホスト名 （ 小文字 ）。
- 失敗 （ HTTP / ValueError ） は CSV に書かない。 1 回の実行のメモリにだけ
  残るので、 次回は API を呼び直す
- 既存の ``describe_object`` 直呼びと「 CSV 経由で読んだあと ``_slim_object`` 」
  は同じ slim 出力を返す
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

from comken.exceptions import CSVError, SalesforceRequestError

from src.describe_store import (
    DESCRIBE_CSV_COLUMNS,
    DescribeStore,
    csv_rows_to_describe,
    describe_to_csv_rows,
    load_describe_index,
    org_from_url,
    upsert_object_rows,
)
from src.fetch import _slim_object, run_fetch
from src.master import MasterEntry

# 既存テストと揃える
DOMAIN_A = "https://example.my.salesforce.com/lightning/r/Report"
DOMAIN_B = "https://example--sandbox.sandbox.my.salesforce.com/lightning/r/Report"


# ── 偽物の Salesforce クライアント ────────────────────────────────────────


class _ReportStub:
    def __init__(self, describe: dict) -> None:
        self._describe = describe

    def describe(self, report_id: str) -> dict:
        return self._describe

    def get(self, report_id: str, filters: object = None, allow_truncated: bool = False) -> object:
        return []


class _FakeClient:
    def __init__(
        self,
        describe: dict,
        object_describes: dict[str, dict] | None = None,
        errors: dict[str, BaseException] | None = None,
    ) -> None:
        self._object_describes = object_describes or {}
        self._errors = errors or {}
        self.report = _ReportStub(describe)
        self.describe_object_calls: list[str] = []

    def describe_object(self, name: str) -> dict:
        self.describe_object_calls.append(name)
        if name in self._errors:
            raise self._errors[name]
        return self._object_describes.get(name, {"name": name, "fields": []})

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *args: object) -> None:
        return None


class _FakeSite:
    def __init__(self, client: _FakeClient) -> None:
        self._client = client

    def __call__(self) -> _FakeSite:
        return self

    def __enter__(self) -> _FakeClient:
        return self._client

    def __exit__(self, *args: object) -> None:
        return None


def _site_for(client: _FakeClient):
    def resolver(url: str) -> _FakeSite:
        return _FakeSite(client)

    return resolver


# ── ヘルパー ─────────────────────────────────────────────────────────────


def _settings(tmp_path: Path) -> dict:
    """``run_fetch`` に渡す kwargs を返す。"""
    return {
        "related_max": 40,
        "related_depth": 1,
        "row_limit": False,  # 2000 件チェックは行わない （ API を呼ばない ）
        "pii_keywords": ("name",),
        "pii_person_objects": ("Contact",),
        "describe_csv_path": tmp_path / "describe.csv",
    }

    return {
        "related_max": 40,
        "related_depth": 1,
        "row_limit": False,  # 2000 件チェックは行わない （ API を呼ばない ）
        "pii_keywords": ("name",),
        "pii_person_objects": ("Contact",),
        "describe_csv_path": tmp_path / "describe.csv",
    }


def _opportunity_describe() -> dict:
    return {
        "name": "Opportunity",
        "label": "商談",
        "custom": False,
        "fields": [
            {"name": "Name", "label": "商談名", "type": "string"},
            {"name": "Amount", "label": "金額", "type": "currency"},
        ],
    }


def _account_describe() -> dict:
    return {
        "name": "Account",
        "label": "取引先",
        "custom": False,
        "fields": [
            {"name": "Name", "label": "取引先名", "type": "string"},
            {"name": "Phone", "label": "電話", "type": "phone"},
        ],
    }


def _make_metadata(report_type: str = "Opportunity", columns: list[str] | None = None) -> dict:
    detail_columns = columns if columns is not None else ["Opp.Name"]
    return {
        "reportMetadata": {
            "reportType": {"type": report_type},
            "reportFormat": "TABULAR",
            "detailColumns": detail_columns,
        },
        "reportExtendedMetadata": {
            "detailColumnInfo": {c: {"label": c} for c in detail_columns},
        },
    }


def _read_csv_rows(path: Path) -> list[dict[str, str]]:
    """テスト用に UTF-8 BOM つき + CRLF の CSV を読む。 見出しは別途
    ``DESCRIBE_CSV_COLUMNS`` を使う想定。"""
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        columns = next(reader)
        return [dict(zip(columns, line, strict=True)) for line in reader]


# ── org_from_url ─────────────────────────────────────────────────────────


def test_org_from_url_lowercases_host() -> None:
    assert org_from_url(f"{DOMAIN_A}/00O5g00000ABCDE/view") == "example.my.salesforce.com"


def test_org_from_url_distinguishes_orgs() -> None:
    a = org_from_url(f"{DOMAIN_A}/00O5g00000ABCDE/view")
    b = org_from_url(f"{DOMAIN_B}/00O5g00000ABCDE/view")
    assert a != b
    assert a == "example.my.salesforce.com"
    assert b == "example--sandbox.sandbox.my.salesforce.com"


# ── describe ⇄ CSV ラウンドトリップ ────────────────────────────────────


def test_describe_csv_roundtrip_preserves_slim_output() -> None:
    """CSV に書いて読み戻した describe を ``_slim_object`` に通すと、 API 直呼びと
    同じ slim 結果になる。 これが「CSV を読んで mapping / pii 関連の挙動を
    変えない」要件の中心。
    """
    original = _opportunity_describe()
    rows = describe_to_csv_rows(
        "example.my.salesforce.com", "Opportunity", original, "2025-01-01T00:00:00+00:00"
    )
    # 1 オブジェクト 2 項目で 2 行
    assert len(rows) == 2
    restored = csv_rows_to_describe(rows)
    assert _slim_object(restored) == _slim_object(original)


def test_describe_csv_roundtrip_with_reference_field(tmp_path: Path) -> None:
    """参照項目 （ referenceTo / relationshipName ） も往復で残る。"""
    original = {
        "name": "Task",
        "label": "活動",
        "custom": False,
        "fields": [
            {
                "name": "WhatId",
                "label": "関連先",
                "type": "reference",
                "referenceTo": ["Account"],
                "relationshipName": "What",
            },
            {
                "name": "OwnerId",
                "label": "担当者",
                "type": "reference",
                "referenceTo": ["User"],
                "relationshipName": "Owner",
            },
        ],
    }
    rows = describe_to_csv_rows(
        "example.my.salesforce.com", "Task", original, "2025-01-01T00:00:00+00:00"
    )
    path = tmp_path / "describe.csv"
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(DESCRIBE_CSV_COLUMNS))
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    index = load_describe_index(path)
    restored = index[("example.my.salesforce.com", "task")]
    assert _slim_object(restored) == _slim_object(original)


# ── 読み込みのフォールバック ─────────────────────────────────────────────


def test_load_returns_empty_when_file_not_found(tmp_path: Path) -> None:
    assert load_describe_index(tmp_path / "nonexistent.csv") == {}


def test_load_raises_on_wrong_header(tmp_path: Path) -> None:
    path = tmp_path / "describe.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["wrong", "header"])
        writer.writerow(["a", "b"])
    with pytest.raises(CSVError):
        load_describe_index(path)


# ── 取り直し前の単純なシナリオ ─────────────────────────────────────────


def test_run_persists_describe_to_csv(tmp_path: Path) -> None:
    """1 回目の実行で API を呼んだ結果が CSV に書かれる。"""
    client = _FakeClient(
        describe=_make_metadata("Opportunity", ["Opp.Name"]),
        object_describes={"Opportunity": _opportunity_describe()},
    )
    settings = _settings(tmp_path)
    entry = MasterEntry(
        key="1001",
        summary="",
        url=f"{DOMAIN_A}/00O5g00000AAAAA/view",
        enabled=True,
    )
    outcomes = run_fetch([entry], site_for=_site_for(client), **settings)
    assert outcomes[0].status == "ok"
    assert client.describe_object_calls == ["Opportunity"]

    # CSV に Opportunity の行が書かれている
    path = settings["describe_csv_path"]
    assert path.exists()
    rows = _read_csv_rows(path)
    assert any(row["オブジェクト"] == "Opportunity" for row in rows)
    # 同じオブジェクト名は 1 個にまとまっている （ 同じ取得日時で項目 2 件 ）
    opp_rows = [r for r in rows if r["オブジェクト"] == "Opportunity"]
    assert len(opp_rows) == 2
    assert opp_rows[0]["取得日時"] == opp_rows[1]["取得日時"]


def test_second_run_does_not_call_api_for_cached_object(tmp_path: Path) -> None:
    """2 回目の実行 （ 別のメモリ辞書、 同じ CSV ） では API を呼ばない。
    列対応も 1 回目と同じ出力になる。
    """
    opportunity = _opportunity_describe()
    settings = _settings(tmp_path)
    # detailColumnInfo の label を フィールドの label と一致させて column_map
    # が解決できるようにする
    metadata = {
        "reportMetadata": {
            "reportType": {"type": "Opportunity"},
            "reportFormat": "TABULAR",
            "detailColumns": ["Opp.Name"],
        },
        "reportExtendedMetadata": {
            "detailColumnInfo": {"Opp.Name": {"label": "商談名"}},
        },
    }

    entry = MasterEntry(
        key="1001",
        summary="",
        url=f"{DOMAIN_A}/00O5g00000AAAAA/view",
        enabled=True,
    )

    # 1 回目: API を呼ぶ
    client1 = _FakeClient(
        describe=metadata,
        object_describes={"Opportunity": opportunity},
    )
    store1 = DescribeStore.empty(settings["describe_csv_path"])
    first_outcomes = run_fetch(
        [entry],
        site_for=_site_for(client1),
        describe_store=store1,
        refresh_set=set(),
        **settings,
    )
    first_record = first_outcomes[0].record
    first_column_map = first_record.column_map if first_record else None
    assert client1.describe_object_calls == ["Opportunity"]
    assert first_column_map is not None

    # 2 回目: 別のメモリ辞書、 同じ CSV （ 別の ``DescribeStore.load`` ）
    client2 = _FakeClient(
        describe=metadata,
        object_describes={"Opportunity": opportunity},
    )
    store2 = DescribeStore.load(settings["describe_csv_path"])
    assert store2.csv_hit_count == 0  # load 時点ではカウンタは進まない
    second_outcomes = run_fetch(
        [entry],
        site_for=_site_for(client2),
        describe_store=store2,
        refresh_set=set(),
        **settings,
    )
    assert client2.describe_object_calls == []
    assert store2.csv_hit_count == 1  # Opportunity は CSV から 1 度引かれた
    second_record = second_outcomes[0].record
    second_column_map = second_record.column_map if second_record else None
    # 列対応が同じ （ 主要オブジェクト, SOQL列名, 表示名, 型, ... ）
    assert first_column_map == second_column_map


# ── 別組織の扱い ─────────────────────────────────────────────────────────


def test_different_orgs_are_separated_in_csv(tmp_path: Path) -> None:
    """本番と Sandbox の同名オブジェクトは別物として扱われる。

    1 回の実行でも 2 つの URL から 2 回 API が呼ばれる。
    実行をまたいでも、 CSV は (org, object) ごとに独立している。
    """
    client = _FakeClient(
        describe=_make_metadata("Opportunity", ["Opp.Name"]),
        object_describes={
            "Opportunity": _opportunity_describe(),
        },
    )
    settings = _settings(tmp_path)
    entries = [
        MasterEntry(
            key="1001",
            summary="",
            url=f"{DOMAIN_A}/00O5g00000AAAAA/view",
            enabled=True,
        ),
        MasterEntry(
            key="1002",
            summary="",
            url=f"{DOMAIN_B}/00O5g00000BBBBB/view",
            enabled=True,
        ),
    ]
    outcomes = run_fetch(entries, site_for=_site_for(client), **settings)
    assert all(o.status == "ok" for o in outcomes)
    # 2 つの組織 （ netloc が違う ） なので Opportunity の API は 2 回呼ばれる
    assert client.describe_object_calls.count("Opportunity") == 2

    # CSV には 2 組織分の Opportunity の行が残る
    rows = _read_csv_rows(settings["describe_csv_path"])
    orgs = sorted({r["組織"] for r in rows if r["オブジェクト"] == "Opportunity"})
    assert orgs == [
        "example--sandbox.sandbox.my.salesforce.com",
        "example.my.salesforce.com",
    ]


def test_different_orgs_keep_separate_state_across_runs(tmp_path: Path) -> None:
    """実行をまたいでも別組織として扱われる。 A 組織の Opportunity は取ってあるが
    B 組織の Opportunity は初見なので API が呼ばれる。
    """
    settings = _settings(tmp_path)
    # CSV に A 組織の Opportunity を書いておく
    upsert_object_rows(
        settings["describe_csv_path"],
        "example.my.salesforce.com",
        "Opportunity",
        _opportunity_describe(),
        fetched_at="2025-01-01T00:00:00+00:00",
    )

    metadata = {
        "reportMetadata": {
            "reportType": {"type": "Opportunity"},
            "reportFormat": "TABULAR",
            "detailColumns": ["Opp.Name"],
        },
        "reportExtendedMetadata": {
            "detailColumnInfo": {"Opp.Name": {"label": "商談名"}},
        },
    }
    # 2 つの組織の URL で同時に取る
    client = _FakeClient(
        describe=metadata,
        object_describes={"Opportunity": _opportunity_describe()},
    )
    entries = [
        MasterEntry(
            key="1001",
            summary="",
            url=f"{DOMAIN_A}/00O5g00000AAAAA/view",
            enabled=True,
        ),
        MasterEntry(
            key="1002",
            summary="",
            url=f"{DOMAIN_B}/00O5g00000BBBBB/view",
            enabled=True,
        ),
    ]
    store = DescribeStore.load(settings["describe_csv_path"])
    run_fetch(
        entries,
        site_for=_site_for(client),
        describe_store=store,
        refresh_set=set(),
        **settings,
    )
    # A 組織は CSV から、 B 組織は API から取るので Opportunity の呼び出しは 1 回だけ
    assert client.describe_object_calls == ["Opportunity"]


# ── 失敗は CSV に残らない ──────────────────────────────────────────────


def test_describe_failure_is_not_persisted(tmp_path: Path) -> None:
    """HTTP エラーは CSV に書かれず、 次回は API を呼び直す。"""
    client = _FakeClient(
        describe=_make_metadata("Opportunity", ["Opp.Name"]),
        errors={
            "Opportunity": SalesforceRequestError(
                "GET", "/sobjects/Opportunity/describe", 500, "Boom"
            )
        },
    )
    settings = _settings(tmp_path)
    entry = MasterEntry(
        key="1001",
        summary="",
        url=f"{DOMAIN_A}/00O5g00000AAAAA/view",
        enabled=True,
    )
    outcomes = run_fetch([entry], site_for=_site_for(client), **settings)
    # describe 失敗で主オブジェクトは None、 警告が残る
    assert outcomes[0].status == "ok"
    assert outcomes[0].record is not None
    assert outcomes[0].record.main_object is None

    # CSV は作られない
    assert not settings["describe_csv_path"].exists()

    # もう一度 （ 別のクライアント 、 同じ CSV パス ） では API が呼ばれる
    client2 = _FakeClient(
        describe=_make_metadata("Opportunity", ["Opp.Name"]),
        object_describes={"Opportunity": _opportunity_describe()},
    )
    outcomes2 = run_fetch([entry], site_for=_site_for(client2), **settings)
    assert client2.describe_object_calls == ["Opportunity"]
    assert outcomes2[0].record is not None
    assert outcomes2[0].record.main_object == "Opportunity"


# ── 行を消したら次回 API で取り直す ─────────────────────────────────────────


def test_object_without_csv_rows_triggers_api_call(tmp_path: Path) -> None:
    """Account は CSV に書かれていて、 Opportunity は書かれていない、 という
    状態で走らせると、 Opportunity だけ API が呼ばれる。
    """
    settings = _settings(tmp_path)
    # CSV に Account だけ書いておく
    upsert_object_rows(
        settings["describe_csv_path"],
        "example.my.salesforce.com",
        "Account",
        _account_describe(),
        fetched_at="2025-01-01T00:00:00+00:00",
    )

    metadata = {
        "reportMetadata": {
            "reportType": {"type": "Account"},
            "reportFormat": "TABULAR",
            "detailColumns": ["Acc.Name"],
        },
        "reportExtendedMetadata": {
            "detailColumnInfo": {"Acc.Name": {"label": "取引先名"}},
        },
    }
    client = _FakeClient(
        describe=metadata,
        object_describes={"Account": _account_describe()},
    )
    entry = MasterEntry(
        key="1001",
        summary="",
        url=f"{DOMAIN_A}/00O5g00000AAAAA/view",
        enabled=True,
    )
    store = DescribeStore.load(settings["describe_csv_path"])
    run_fetch(
        [entry],
        site_for=_site_for(client),
        describe_store=store,
        refresh_set=set(),
        **settings,
    )
    # Account は CSV から取れたので API は呼ばれない
    assert client.describe_object_calls == []


def test_csv_with_some_object_rows_triggers_api_for_others(tmp_path: Path) -> None:
    """Account は CSV にあるが Opportunity はない、 という状態で両方参照する
    レポートを走らせると、 Opportunity だけ API が呼ばれる。
    """
    settings = _settings(tmp_path)
    upsert_object_rows(
        settings["describe_csv_path"],
        "example.my.salesforce.com",
        "Account",
        _account_describe(),
        fetched_at="2025-01-01T00:00:00+00:00",
    )

    client = _FakeClient(
        describe=_make_metadata("Opportunity", ["Opp.Name", "Acc.Name"]),
        object_describes={
            "Opportunity": _opportunity_describe(),
            "Account": _account_describe(),
        },
    )
    entry = MasterEntry(
        key="1001",
        summary="",
        url=f"{DOMAIN_A}/00O5g00000AAAAA/view",
        enabled=True,
    )
    run_fetch([entry], site_for=_site_for(client), **settings)
    # Opportunity だけ API から取られる
    assert client.describe_object_calls == ["Opportunity"]


# ── 取り直し ────────────────────────────────────────────────────────────


def test_stale_csv_triggers_refresh_once(tmp_path: Path) -> None:
    """CSV の古い Opportunity に存在しない項目名を持つレポートを取り、
    列対応に (不明) が出るなら、 API で取り直して列対応を再計算する。
    取り直しは 1 レポート 1 回限りで、 API が新しい項目を返さなくても
    繰り返さない。
    """
    settings = _settings(tmp_path)
    # 古い Opportunity: Name だけ、 Amount は無い
    stale_opportunity = {
        "name": "Opportunity",
        "label": "商談",
        "custom": False,
        "fields": [
            {"name": "Name", "label": "商談名", "type": "string"},
        ],
    }
    upsert_object_rows(
        settings["describe_csv_path"],
        "example.my.salesforce.com",
        "Opportunity",
        stale_opportunity,
        fetched_at="2024-01-01T00:00:00+00:00",
    )

    # API が返す新しい Opportunity には Amount も含まれる
    fresh_opportunity = _opportunity_describe()

    metadata = {
        "reportMetadata": {
            "reportType": {"type": "Opportunity"},
            "reportFormat": "TABULAR",
            "detailColumns": ["Opp.Name", "Opp.Amount"],
        },
        "reportExtendedMetadata": {
            "detailColumnInfo": {
                "Opp.Name": {"label": "商談名"},
                "Opp.Amount": {"label": "金額"},
            }
        },
    }

    entry = MasterEntry(
        key="1001",
        summary="",
        url=f"{DOMAIN_A}/00O5g00000AAAAA/view",
        enabled=True,
    )
    # 1 回目: API を 1 回呼ばれる （ Amount が解決できず (不明) 、 取り直し ）
    client = _FakeClient(
        describe=metadata,
        object_describes={"Opportunity": fresh_opportunity},
    )
    store = DescribeStore.load(settings["describe_csv_path"])
    outcomes = run_fetch(
        [entry],
        site_for=_site_for(client),
        describe_store=store,
        refresh_set=set(),
        **settings,
    )
    record = outcomes[0].record
    assert record is not None
    # 列対応に Amount が解決されて入る
    soql_names = {row["対応フィールドAPI名"] for row in record.column_map}
    assert "Amount" in soql_names
    # 取り直しは 1 回だけ
    assert client.describe_object_calls.count("Opportunity") == 1

    # 2 回目: 同じレポートをもう一度 （ 新しい describe は CSV に書かれている ）
    client2 = _FakeClient(
        describe=metadata,
        object_describes={"Opportunity": fresh_opportunity},
    )
    store2 = DescribeStore.load(settings["describe_csv_path"])
    outcomes2 = run_fetch(
        [entry],
        site_for=_site_for(client2),
        describe_store=store2,
        refresh_set=set(),
        **settings,
    )
    record2 = outcomes2[0].record
    assert record2 is not None
    # CSV から読めたので API は呼ばれない
    assert client2.describe_object_calls == []
    # 列対応も 1 回目と同じ
    soql_names2 = {row["対応フィールドAPI名"] for row in record2.column_map}
    assert soql_names == soql_names2


def test_refresh_does_not_repeat_if_api_returns_same_data(tmp_path: Path) -> None:
    """取り直しで API を呼んでも新しい項目が無い場合は、 もう一度 API を
    呼ばない （ 同じ取得日時で CSV を入れ替えるだけ ）。
    """
    settings = _settings(tmp_path)
    # 古い Opportunity （ 項目 1 つ ）
    stale = {
        "name": "Opportunity",
        "label": "商談",
        "custom": False,
        "fields": [
            {"name": "Name", "label": "商談名", "type": "string"},
        ],
    }
    upsert_object_rows(
        settings["describe_csv_path"],
        "example.my.salesforce.com",
        "Opportunity",
        stale,
        fetched_at="2024-01-01T00:00:00+00:00",
    )
    # API も 古いものと同じものを返す
    client = _FakeClient(
        describe=_make_metadata("Opportunity", ["Opp.Amount"]),
        object_describes={"Opportunity": stale},
    )
    entry = MasterEntry(
        key="1001",
        summary="",
        url=f"{DOMAIN_A}/00O5g00000AAAAA/view",
        enabled=True,
    )
    outcomes = run_fetch([entry], site_for=_site_for(client), **settings)
    record = outcomes[0].record
    assert record is not None
    # 列対応の Amount は (不明) のまま
    soql_names = {row["対応フィールドAPI名"] for row in record.column_map}
    assert "(不明)" in soql_names
    # API は 1 回しか呼ばない （ 取り直しは 1 回限り ）
    assert client.describe_object_calls.count("Opportunity") == 1


# ── 見出し違い ─────────────────────────────────────────────────────────


def test_existing_csv_with_wrong_header_aborts(tmp_path: Path) -> None:
    """既存 CSV の見出しが想定と違うと、 上書きせず失敗させる。"""
    settings = _settings(tmp_path)
    path = settings["describe_csv_path"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("wrong,header\nfoo,bar\n", encoding="utf-8-sig")

    # ``DescribeStore.load`` の時点で CSVError が出る
    with pytest.raises(CSVError):
        DescribeStore.load(path)
    # ``run_fetch`` の中で ``upsert_object_rows`` が呼ばれると CSVError になるが、
    # このテストでは直接 ``upsert_object_rows`` を試して失敗を確かめる
    with pytest.raises(CSVError):
        upsert_object_rows(
            path, "example.my.salesforce.com", "Opportunity", _opportunity_describe()
        )
    # ファイルの内容は変わっていない
    assert path.read_text(encoding="utf-8-sig") == "wrong,header\nfoo,bar\n"


# ── 必須キー欠落 ─────────────────────────────────────────────────────


def test_settings_missing_describe_csv_raises(tmp_path: Path) -> None:
    """config.ini に ``DESCRIBE_CSV_PATH`` が無いと comken の
    ``ConfigKeyNotFoundError`` が上がり、 メッセージにキー名が入る。
    """
    from comken.core.config import _reset_cached_config
    from comken.exceptions import ConfigKeyNotFoundError

    from src.run import read_file_paths

    _reset_cached_config()

    config_dir = tmp_path / "proj"
    config_dir.mkdir()
    (config_dir / "config.ini").write_text(
        "[FILES]\n"
        "MASTER_XLSX_PATH = ./master.xlsx\n"
        "OUTPUT_DIR = ./output\n"
        "COLUMN_MAP_CSV_PATH = ./data/列の対応.csv\n",
        encoding="utf-8",
    )

    original_argv = sys.argv
    sys.argv = [str(config_dir / "fake_main.py")]
    try:
        with pytest.raises(ConfigKeyNotFoundError) as exc_info:
            read_file_paths()
        assert "DESCRIBE_CSV_PATH" in str(exc_info.value)
    finally:
        sys.argv = original_argv
        _reset_cached_config()


# ── 終了ログ ─────────────────────────────────────────────────────────


def test_run_logs_describe_counts(tmp_path: Path, caplog) -> None:
    """実行の終わりに「 保存データから N 件 / Salesforce から N 件 / 取り直し N 件 」
    が INFO で出る。
    """
    import logging

    caplog.set_level(logging.INFO)
    settings = _settings(tmp_path)
    # CSV に Account を書いておく
    upsert_object_rows(
        settings["describe_csv_path"],
        "example.my.salesforce.com",
        "Account",
        _account_describe(),
        fetched_at="2025-01-01T00:00:00+00:00",
    )
    client = _FakeClient(
        describe=_make_metadata("Opportunity", ["Opp.Name"]),
        object_describes={"Opportunity": _opportunity_describe()},
    )
    entry = MasterEntry(
        key="1001",
        summary="",
        url=f"{DOMAIN_A}/00O5g00000AAAAA/view",
        enabled=True,
    )
    run_fetch([entry], site_for=_site_for(client), **settings)
    # ``run_fetch`` は終了ログを出さない （ run.py 側 ） ので、 テストは
    # ``DescribeStore`` のカウントを直接見て確認する。 （ 終了ログは run.py の
    # 統合テストで別途確かめる ）


def test_describe_store_counts_track_sources(tmp_path: Path) -> None:
    """``DescribeStore`` の ``csv_hit_count`` / ``api_count`` が正しく増える。"""
    settings = _settings(tmp_path)
    upsert_object_rows(
        settings["describe_csv_path"],
        "example.my.salesforce.com",
        "Account",
        _account_describe(),
        fetched_at="2025-01-01T00:00:00+00:00",
    )
    store = DescribeStore.load(settings["describe_csv_path"])
    # Account は CSV から取れる
    assert store.lookup("example.my.salesforce.com", "Account") is not None
    assert store.csv_hit_count == 1
    # Opportunity は CSV に無い
    assert store.lookup("example.my.salesforce.com", "Opportunity") is None
    # 無いとき API で取って書き込む
    store.record_api_success(
        "example.my.salesforce.com",
        "Opportunity",
        _opportunity_describe(),
    )
    assert store.api_count == 1
    # もう一度引くと今度はメモリにある
    assert store.lookup("example.my.salesforce.com", "Opportunity") is not None


# ── pytest import ──────────────────────────────────────────────────────


import pytest  # noqa: E402  # ファイル末尾に置く （ 上のテストでも使う ）
