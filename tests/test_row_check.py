"""行数チェック（ ``_check_row_count`` 経由 ） のテスト。

偽物の Salesforce クライアントで 1 レポートずつ検証する。 実 Salesforce には
繋がない。 レポートの行データ（ 個人情報を含みうる ） はメモリ上でも
``len(table)`` のカウントだけにして、 CSV / ログ / 例外メッセージに絶対出さない
ことを確認する。
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from comken.exceptions import (
    SalesforceError,
    SalesforceReportTruncatedError,
    SalesforceRequestError,
)

from src.fetch import _check_row_count, _fetch_one
from src.master import MasterEntry
from src.settings import Settings

DOMAIN = "https://example--sandbox.sandbox.my.salesforce.com/lightning/r/Report"

# 目印の文字列。 行データが CSV やログに漏れていないことを検知するために使う
SECRET_ROW_VALUE = "SECRET-ROW-VALUE"


# ── 偽物のクライアント ─────────────────────────────────────────────


class _ReportStub:
    def __init__(
        self,
        describe: dict,
        *,
        get_table=None,
        get_error: BaseException | None = None,
    ) -> None:
        self._describe = describe
        self._get_table = get_table if get_table is not None else []
        self._get_error = get_error
        self.get_calls: list[str] = []

    def describe(self, report_id: str) -> dict:
        return self._describe

    def get(self, report_id: str, filters: object = None, allow_truncated: bool = False) -> object:
        self.get_calls.append(report_id)
        if self._get_error is not None:
            raise self._get_error
        return self._get_table


class _FakeClient:
    def __init__(
        self,
        describe: dict,
        object_describes: dict[str, dict] | None = None,
        *,
        get_table=None,
        get_error: BaseException | None = None,
    ) -> None:
        self._object_describes = object_describes or {}
        self.report = _ReportStub(describe, get_table=get_table, get_error=get_error)
        self.describe_object_calls: list[str] = []

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


def _settings(tmp_path: Path, *, row_limit: bool = True) -> Settings:
    return Settings(
        master_xlsx_path=tmp_path / "master.xlsx",
        output_dir=tmp_path / "output",
        related_max=40,
        related_depth=1,
        credential_prefix="",
        row_limit=row_limit,
        pii_keywords=(),
        pii_person_objects=(),
        similar_column_similarity=0.8,
    )


def _metadata(report_format: str = "TABULAR") -> dict:
    return {
        "reportMetadata": {
            "reportType": {"type": "Opportunity"},
            "reportFormat": report_format,
            "detailColumns": ["Opp.Name"],
        },
        "reportExtendedMetadata": {
            "detailColumnInfo": {"Opp.Name": {"label": "商談名"}},
        },
    }


class _Row:
    """``len()`` だけ持つ ``Table`` 互換。"""

    def __init__(self, count: int) -> None:
        self._count = count

    def __len__(self) -> int:
        return self._count


# ── 状態判定 ─────────────────────────────────────────────────────


def test_truncated_marks_over() -> None:
    """``SalesforceReportTruncatedError`` → 超えている。"""
    client = _FakeClient(
        describe=_metadata(),
        get_error=SalesforceReportTruncatedError("00O", 2000),
    )
    result = _check_row_count(client, "00O", _metadata())
    assert result.status == "超えている"
    assert result.rows is None


def test_normal_get_marks_under_with_count() -> None:
    """正常終了 → 超えていない ＋ 行数。"""
    client = _FakeClient(
        describe=_metadata(),
        get_table=_Row(1234),
    )
    result = _check_row_count(client, "00O", _metadata())
    assert result.status == "超えていない"
    assert result.rows == 1234


def test_format_other_than_tabular_marks_unknown() -> None:
    """describe で TABULAR 以外なら get を呼ばずに 判定不可。"""
    client = _FakeClient(describe=_metadata("SUMMARY"))
    result = _check_row_count(client, "00O", _metadata("SUMMARY"))
    assert result.status == "判定不可"
    assert "集計" in (result.reason or "")
    assert client.report.get_calls == []


def test_format_error_from_get_marks_unknown() -> None:
    """``get()`` が集計形式エラーを上げても 判定不可。"""
    err = SalesforceError(
        "このレポートは SUMMARY 形式です: 00O\n取得できるのは明細（TABULAR）形式のレポートだけです。"
    )
    client = _FakeClient(describe=_metadata("TABULAR"), get_error=err)
    result = _check_row_count(client, "00O", _metadata("TABULAR"))
    assert result.status == "判定不可"
    assert "集計" in (result.reason or "")


def test_other_salesforce_error_marks_unknown_with_type_and_message() -> None:
    """その他の ``SalesforceError`` → 判定不可。 例外名と短い説明が出る。"""
    err = SalesforceError("何か問題がありました")
    client = _FakeClient(describe=_metadata(), get_error=err)
    result = _check_row_count(client, "00O", _metadata())
    assert result.status == "判定不可"
    assert "SalesforceError" in (result.reason or "")
    assert "何か問題" in (result.reason or "")


def test_auth_error_propagates_so_report_fails() -> None:
    """401 / 403 の access denied エラーは再送出されて ID 全体が失敗扱い。"""
    access_denied = SalesforceError(
        "Salesforce のレポート API（Analytics API）へのアクセスが"
        "拒否されました（HTTP 401）: 00O\nUnauthorized"
    )
    client = _FakeClient(describe=_metadata(), get_error=access_denied)
    with pytest.raises(SalesforceError):
        _check_row_count(client, "00O", _metadata())


def test_generic_exception_marks_unknown() -> None:
    """``TimeoutError`` のような通常例外も 判定不可 に倒れる。"""
    client = _FakeClient(
        describe=_metadata(),
        get_error=TimeoutError("connect timeout"),
    )
    result = _check_row_count(client, "00O", _metadata())
    assert result.status == "判定不可"
    assert "TimeoutError" in (result.reason or "")


# ── row_limit=× で get が呼ばれない ─────────────────────────────


def test_row_limit_disabled_skips_get(tmp_path: Path) -> None:
    """``[CHECKS] ROW_LIMIT = ×`` のときは ``get`` が呼ばれず ``row_check`` は None。"""
    client = _FakeClient(
        describe=_metadata(),
        get_table=_Row(10),
    )
    settings = _settings(tmp_path, row_limit=False)
    entry = MasterEntry(key="1", summary="", url=f"{DOMAIN}/00O5g00000ABCDE/view", enabled=True)
    outcome = _fetch_one(settings, entry, site_for=_site_for(client), cache={})
    assert outcome.status == "ok"
    assert outcome.record is not None
    assert outcome.record.row_check is None
    assert client.report.get_calls == []


# ── 行データの機密性 ─────────────────────────────────────────


def test_row_data_never_leaks_to_output(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """目印を含む行データが出力ファイル / ログ / 警告 / 例外メッセージに
    一切現れないこと。
    """
    caplog.set_level(logging.DEBUG)

    class _TableWithSecret:
        def __init__(self) -> None:
            self._rows = [
                {"col": f"value {SECRET_ROW_VALUE} 1"},
                {"col": f"value {SECRET_ROW_VALUE} 2"},
            ]

        def __len__(self) -> int:
            # 機微な値（ 値そのもの ） を返さない
            return 2

    # _check_row_count 単体でログ / 例外に漏れない
    client = _FakeClient(describe=_metadata(), get_table=_TableWithSecret())
    result = _check_row_count(client, "00O", _metadata())
    assert result.status == "超えていない"
    assert result.rows == 2
    assert SECRET_ROW_VALUE not in str(result.reason or "")

    # _fetch_one 経由で 1 件取って warnings を出させる
    err = SalesforceError("タイムアウト的な何か: 内部メッセージ")
    client2 = _FakeClient(describe=_metadata(), get_error=err)
    settings = _settings(tmp_path)
    entry = MasterEntry(key="1", summary="", url=f"{DOMAIN}/00O5g00000ABCDE/view", enabled=True)
    outcome = _fetch_one(settings, entry, site_for=_site_for(client2), cache={})
    # 警告 / エラーメッセージに目印が入らない
    assert outcome.status == "ok"
    assert outcome.record is not None
    for warning in outcome.record.warnings:
        assert SECRET_ROW_VALUE not in warning
    # CSV を書いて確認
    from src.tables import run_tables

    output = tmp_path / "output"
    output.mkdir(parents=True, exist_ok=True)
    run_tables(output, [outcome.record], only_keys=["1"])
    for path in output.iterdir():
        if path.is_file():
            text = path.read_text(encoding="utf-8-sig", errors="replace")
            assert SECRET_ROW_VALUE not in text, f"行データが {path} に漏れた"

    # ログにも出ない
    for record in caplog.records:
        assert SECRET_ROW_VALUE not in record.getMessage()


def test_salesforce_request_error_marks_unknown() -> None:
    """``SalesforceRequestError`` （ 401 / 403 以外 ） は 判定不可。"""
    err = SalesforceRequestError("GET", "/analytics/reports/00O", 500, "Server Error")
    client = _FakeClient(describe=_metadata(), get_error=err)
    result = _check_row_count(client, "00O", _metadata())
    assert result.status == "判定不可"
    assert "500" in (result.reason or "")
