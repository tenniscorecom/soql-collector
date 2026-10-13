"""件数チェック（ レポート総件数 vs SOQL の ``totalSize`` ） のテスト。

偽物の Salesforce クライアントで 1 レポートずつ検証する。 実 Salesforce には
繋がない。 レポートの行データ（ 個人情報を含みうる ） は一切取らないこと
（ ``?includeDetails=false`` を付けて ``factMap`` の ``T!T.aggregates`` だけ
使う。 SOQL は ``SELECT COUNT() FROM ...`` で ``totalSize`` だけ ） 。
"""

from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import quote, unquote

import pytest
from comken.exceptions import SalesforceError, SalesforceRequestError

from src.describe_store import DescribeStore
from src.fetch import (
    COUNT_CHECK_MATCHED,
    COUNT_CHECK_MISMATCHED,
    COUNT_CHECK_UNKNOWN,
    ReportRecord,
    _check_count,
    _extract_row_count_from_report_payload,
    _fetch_one,
    _fetch_report_total_count,
    _fetch_soql_total_count,
)
from src.master import MasterEntry
from src.settings import Settings
from src.soql_builder import STATUS_NONE, SoqlDraft, build_soql

DOMAIN = "https://example--sandbox.sandbox.my.salesforce.com/lightning/r/Report"
REPORT_ID = "00O5g00000ABCDE"

# ── 偽物の Salesforce クライアント ─────────────────────────────


class _ReportStub:
    def __init__(
        self,
        describe: dict,
        *,
        get_table: object | None = None,
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
    """``_check_count`` のテスト用クライアント。

    ``request_responses`` に ``(path, payload)`` を渡すと、 ``client.request``
    がその path の順に対応する payload を返す。 ``request_errors`` は path →
    例外で、 引いた path に対して呼ぶと例外が上がる。

    ``/query?q=...`` へのリクエストは SOQL を URL から取り出して、 その
    形 (``SELECT COUNT()`` で始まり ``FROM`` の前に他の列が無い) を assert する。
    これで ``build_count_soql`` が文字列の切り貼りで壊れた SOQL を作って
    いてもテストで必ず落ちる。
    """

    def __init__(
        self,
        describe: dict,
        object_describes: dict[str, dict] | None = None,
        *,
        request_responses: list[tuple[str, tuple[object, dict[str, str]]]] | None = None,
        request_errors: dict[str, BaseException] | None = None,
        get_table: object | None = None,
        get_error: BaseException | None = None,
    ) -> None:
        self._object_describes = object_describes or {}
        self.report = _ReportStub(describe, get_table=get_table, get_error=get_error)
        self.describe_object_calls: list[str] = []
        self._request_responses = request_responses or []
        self._request_errors = request_errors or {}
        self._request_index = 0
        self.request_calls: list[tuple[str, str]] = []

    def data_path(self, path: str) -> str:
        return path

    def request(
        self,
        method: str,
        path: str,
        body: object = None,
        component: str = "other",
        headers: dict[str, str] | None = None,
        data: str | None = None,
    ) -> tuple[object, dict[str, str]]:
        self.request_calls.append((method, path))
        if path in self._request_errors:
            raise self._request_errors[path]
        # ``/query?q=...`` は SOQL が URL にエンコードされているので、
        # decode して ``SELECT COUNT()`` + ``FROM`` 形 (``FROM`` の前に
        # 他の列名が無い) を assert する。 これで ``build_count_soql`` が
        # カンマ付きの列を残したまま投げる壊れた SOQL を必ず弾く。
        if path.startswith("/query?q="):
            soql = unquote(path[len("/query?q="):])
            soql_upper = soql.upper()
            assert soql_upper.startswith("SELECT COUNT()"), (
                f"/query の SOQL は SELECT COUNT() で始まること: {soql!r}"
            )
            # SELECT 句 (``SELECT COUNT()`` から ``FROM`` まで) を取り出して
            # その中に ``SELECT COUNT()`` 以外の文字列 ( 列名や ``,`` ) が
            # 無いかを見る。 ``\nFROM`` と `` FROM `` の両方に対応。
            if "\nFROM" in soql_upper:
                select_clause = soql_upper.split("\nFROM", 1)[0]
            elif " FROM " in soql_upper:
                select_clause = soql_upper.split(" FROM ", 1)[0]
            else:
                # ``FROM`` が無い ( 異常ケース ) は今回の assert の対象外
                select_clause = soql_upper
            tail = select_clause[len("SELECT COUNT()"):].strip()
            assert tail == "", (
                f"SELECT COUNT() の直後に余計な文字列がある: "
                f"select_clause={select_clause!r}"
            )
        if self._request_index < len(self._request_responses):
            entry = self._request_responses[self._request_index]
            self._request_index += 1
            response_path, payload = entry
            assert response_path == path, (
                f"path mismatch: expected {response_path!r}, got {path!r}"
            )
            return payload
        return None, {}

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


def _settings(tmp_path: Path, *, soql_count: bool = True) -> Settings:
    return Settings(
        master_xlsx_path=tmp_path / "master.xlsx",
        output_dir=tmp_path / "output",
        related_max=40,
        related_depth=1,
        credential_prefix="",
        row_limit=False,
        soql_count=soql_count,
        pii_keywords=(),
        pii_person_objects=(),
        similar_column_similarity=0.8,
        describe_csv_path=tmp_path / "describe.csv",
    )


def _report_payload(total_count: int | None = None) -> dict:
    """``client.request("GET", "/analytics/reports/{id}")`` の戻り値ペイロード。

    ``?includeDetails=false`` なので ``factMap`` の ``T!T.aggregates`` しか
    返らない形。
    """
    if total_count is None:
        return {
            "reportMetadata": {"aggregates": ["RowCount"]},
            "factMap": {"T!T": {"aggregates": []}},
        }
    return {
        "reportMetadata": {"aggregates": ["RowCount"]},
        "factMap": {"T!T": {"aggregates": [total_count]}},
    }


def _query_payload(total_size: int) -> tuple[dict, dict[str, str]]:
    """``client.request("GET", "/query?q=...")`` の戻り値ペイロード。"""
    return ({"totalSize": total_size}, {})


def _report_request_path(report_id: str) -> str:
    """``_fetch_report_total_count`` が ``client.request`` に渡す path を組み立てる。"""
    return f"/analytics/reports/{report_id}?includeDetails=false"


def _soql_query_path(soql: str) -> str:
    return f"/query?q={quote(soql)}"


# ── RowCount 抽出ヘルパ ─────────────────────────────────────


def test_extract_row_count_basic() -> None:
    """``reportMetadata.aggregates`` の ``"RowCount"`` 位置の値を取り出す。"""
    payload = _report_payload(total_count=42)
    assert _extract_row_count_from_report_payload(payload) == 42


def test_extract_row_count_no_rowcount_in_aggregates() -> None:
    """``RowCount`` が ``aggregates`` に無い / 集計の位置に無いとき ``None``。"""
    # aggregates に RowCount が無い
    payload = _report_payload()
    assert _extract_row_count_from_report_payload(payload) is None
    # factMap の aggregates が無い
    payload_no_fact = {
        "reportMetadata": {"aggregates": ["RowCount"]},
        "factMap": {"T!T": {}},
    }
    assert _extract_row_count_from_report_payload(payload_no_fact) is None


def test_extract_row_count_handles_non_dict_payload() -> None:
    """``client.request`` が壊れた値を返しても ``None``。"""
    assert _extract_row_count_from_report_payload(None) is None  # type: ignore[arg-type]
    assert _extract_row_count_from_report_payload("not a dict") is None
    assert _extract_row_count_from_report_payload(42) is None


# ── レポート総件数 / SOQL 件数 単体 ────────────────────────


def test_fetch_report_total_count_success() -> None:
    """正常時: レポートの総件数が返る。"""
    client = _FakeClient(
        describe={},
        request_responses=[(_report_request_path(REPORT_ID), (_report_payload(99), {}))],
    )
    assert _fetch_report_total_count(client, REPORT_ID) == 99


def test_fetch_report_total_count_non_fatal_returns_none() -> None:
    """``SalesforceRequestError`` の 401 / 403 以外は ``None`` に縮退。"""
    err = SalesforceRequestError("GET", "/analytics/reports/00O", 500, "Server Error")
    client = _FakeClient(
        describe={},
        request_errors={_report_request_path(REPORT_ID): err},
    )
    assert _fetch_report_total_count(client, REPORT_ID) is None


def test_fetch_report_total_count_auth_propagates() -> None:
    """401 / 403 は ``SalesforceRequestError`` のまま上位へ送出。"""
    err = SalesforceRequestError("GET", "/analytics/reports/00O", 401, "Unauthorized")
    client = _FakeClient(
        describe={},
        request_errors={_report_request_path(REPORT_ID): err},
    )
    with pytest.raises(SalesforceRequestError):
        _fetch_report_total_count(client, REPORT_ID)


def test_fetch_soql_total_count_returns_total_size() -> None:
    """SOQL ``COUNT()`` の ``totalSize`` を返す。"""
    soql = "SELECT COUNT() FROM Opportunity"
    client = _FakeClient(
        describe={},
        request_responses=[(_soql_query_path(soql), _query_payload(7))],
    )
    total, error = _fetch_soql_total_count(client, soql, limit=None)
    assert total == 7
    assert error is None


def test_fetch_soql_total_count_applies_limit_when_smaller() -> None:
    """LIMIT があるとき、 ``totalSize`` が LIMIT を超えていれば ``min`` を返す。"""
    soql = "SELECT COUNT() FROM Opportunity"
    client = _FakeClient(
        describe={},
        request_responses=[(_soql_query_path(soql), _query_payload(100))],
    )
    # totalSize=100 が LIMIT=10 を超えるので 10
    total, error = _fetch_soql_total_count(client, soql, limit=10)
    assert total == 10
    assert error is None


def test_fetch_soql_total_count_returns_total_size_when_under_limit() -> None:
    """LIMIT より小さい ``totalSize`` はそのまま返す。"""
    soql = "SELECT COUNT() FROM Opportunity"
    client = _FakeClient(
        describe={},
        request_responses=[(_soql_query_path(soql), _query_payload(5))],
    )
    total, _ = _fetch_soql_total_count(client, soql, limit=100)
    assert total == 5


def test_fetch_soql_total_count_error_returns_none_with_message() -> None:
    """SOQL 実行エラー（ 401 / 403 以外 ） は ``None`` ＋ 短いメッセージを返す。"""
    soql = "SELECT COUNT() FROM BadObject"
    err = SalesforceError("INVALID_FIELD: bad field")
    client = _FakeClient(
        describe={},
        request_errors={_soql_query_path(soql): err},
    )
    total, error = _fetch_soql_total_count(client, soql, limit=None)
    assert total is None
    # メッセージの抜粋が返る
    assert error is not None
    assert "INVALID_FIELD" in error


def test_fetch_soql_total_count_auth_propagates() -> None:
    """401 / 403 はそのまま上位へ送出。"""
    soql = "SELECT COUNT() FROM Opportunity"
    err = SalesforceRequestError("GET", "/query?q=...", 403, "Forbidden")
    client = _FakeClient(
        describe={},
        request_errors={_soql_query_path(soql): err},
    )
    with pytest.raises(SalesforceRequestError):
        _fetch_soql_total_count(client, soql, limit=None)


# ── _check_count 統合判定 ─────────────────────────────────────


def test_check_count_matched_when_counts_equal() -> None:
    """レポート件数 == SOQL 件数 → ``一致``"""
    soql = "SELECT COUNT() FROM Opportunity"
    client = _FakeClient(
        describe={},
        request_responses=[
            (_report_request_path(REPORT_ID), (_report_payload(123), {})),
            (_soql_query_path(soql), _query_payload(123)),
        ],
    )
    result = _check_count(client, REPORT_ID, soql, limit=None)
    assert result.status == COUNT_CHECK_MATCHED
    assert result.report_count == 123
    assert result.soql_count == 123
    assert result.reason is None


def test_check_count_mismatched_reports_both_numbers() -> None:
    """レポート件数 != SOQL 件数 → ``不一致`` ＋ 両方の件数。"""
    soql = "SELECT COUNT() FROM Opportunity"
    client = _FakeClient(
        describe={},
        request_responses=[
            (_report_request_path(REPORT_ID), (_report_payload(200), {})),
            (_soql_query_path(soql), _query_payload(150)),
        ],
    )
    result = _check_count(client, REPORT_ID, soql, limit=None)
    assert result.status == COUNT_CHECK_MISMATCHED
    assert result.report_count == 200
    assert result.soql_count == 150
    assert result.reason is None


def test_check_count_mismatched_with_limit_uses_min() -> None:
    """LIMIT 付きのときは ``min(totalSize, limit)`` と比べる。"""
    soql = "SELECT COUNT() FROM Opportunity"
    client = _FakeClient(
        describe={},
        request_responses=[
            (_report_request_path(REPORT_ID), (_report_payload(50), {})),
            (_soql_query_path(soql), _query_payload(999)),
        ],
    )
    result = _check_count(client, REPORT_ID, soql, limit=100)
    # soql_count = min(999, 100) = 100、 report_count = 50 → 不一致
    assert result.status == COUNT_CHECK_MISMATCHED
    assert result.report_count == 50
    assert result.soql_count == 100
    # limit も保持する
    assert result.limit == 100


def test_check_count_soql_error_returns_unknown_with_message() -> None:
    """SOQL 実行エラー → ``確認できず`` ＋ Salesforce のエラーメッセージ。"""
    soql = "SELECT COUNT() FROM Opportunity"
    client = _FakeClient(
        describe={},
        request_responses=[
            (_report_request_path(REPORT_ID), (_report_payload(123), {})),
            (
                _soql_query_path(soql),
                _query_payload(123),
            ),  # こちらは使わないのでダミー
        ],
        request_errors={
            _soql_query_path(soql): SalesforceError(
                "INVALID_FIELD: No such column 'Stage__c' on entity 'Opportunity'"
            ),
        },
    )
    result = _check_count(client, REPORT_ID, soql, limit=None)
    assert result.status == COUNT_CHECK_UNKNOWN
    assert result.report_count == 123
    assert result.soql_count is None
    assert "INVALID_FIELD" in (result.reason or "")


def test_check_count_report_count_missing_returns_unknown() -> None:
    """レポートの総件数が取れない（ ``RowCount`` 無し ） → ``確認できず``"""
    soql = "SELECT COUNT() FROM Opportunity"
    client = _FakeClient(
        describe={},
        request_responses=[
            # factMap["T!T"] が空 → レポート件数取れない
            (_report_request_path(REPORT_ID), (_report_payload(), {})),
            (_soql_query_path(soql), _query_payload(123)),
        ],
    )
    result = _check_count(client, REPORT_ID, soql, limit=None)
    assert result.status == COUNT_CHECK_UNKNOWN
    assert result.report_count is None
    assert result.soql_count == 123
    # レポート件数が取れない理由
    assert "レポートの総件数が取れない" in (result.reason or "")


def test_check_count_soql_none_returns_unknown_make_soql_soql_reason() -> None:
    """SOQL が ``作れず`` で ``count_soql=None`` のとき、 レポート件数が取れていれば
    ``確認できず``、 理由は ``SOQL を作れず``"""
    client = _FakeClient(
        describe={},
        request_responses=[
            (_report_request_path(REPORT_ID), (_report_payload(50), {})),
        ],
    )
    result = _check_count(client, REPORT_ID, count_soql=None, limit=None)
    assert result.status == COUNT_CHECK_UNKNOWN
    assert result.report_count == 50
    assert result.soql_count is None
    assert result.reason == "SOQL を作れず"


def test_check_count_soql_none_and_report_count_unknown() -> None:
    """SOQL が無い ＋ レポート件数も取れないときは理由が 「 レポートの総件数が取れない 」。"""
    client = _FakeClient(
        describe={},
        # request_responses を空にしておく → レポート件数取れない
    )
    result = _check_count(client, REPORT_ID, count_soql=None, limit=None)
    assert result.status == COUNT_CHECK_UNKNOWN
    assert result.report_count is None
    assert result.soql_count is None
    assert result.reason == "レポートの総件数が取れない"


# ── _fetch_one に統合 ────────────────────────────────────────


def _metadata(report_type: str = "Opportunity") -> dict:
    return {
        "reportMetadata": {
            "reportType": {"type": report_type},
            "reportFormat": "TABULAR",
            "detailColumns": ["Opp.Name"],
            "topRows": {"rowLimit": 100},
        },
        "reportExtendedMetadata": {
            "detailColumnInfo": {"Opp.Name": {"label": "商談名"}},
        },
    }


def _opportunity_with_name() -> dict:
    return {
        "name": "Opportunity",
        "fields": [{"name": "Name", "label": "商談名", "type": "string"}],
    }


def test_fetch_one_count_disabled_skips_request(tmp_path: Path) -> None:
    """``[CHECKS] SOQL_COUNT = ×`` のときは ``request`` を呼ばない。"""
    soql = "SELECT COUNT()\nFROM Opportunity"
    client = _FakeClient(
        describe=_metadata(),
        object_describes={"Opportunity": _opportunity_with_name()},
        request_responses=[
            # 呼ばれるとしたら備えておくが、 ここでは呼ばれないはず
            (_report_request_path(REPORT_ID), (_report_payload(1), {})),
            (_soql_query_path(soql), _query_payload(1)),
        ],
    )
    entry = MasterEntry(key="1", summary="", url=f"{DOMAIN}/{REPORT_ID}/view", enabled=True)
    outcome = _fetch_one(
        _settings(tmp_path, soql_count=False),
        entry,
        site_for=_site_for(client),
        store=DescribeStore.empty(tmp_path / "describe.csv"),
        refresh_set=set(),
    )
    assert outcome.status == "ok"
    assert outcome.record is not None
    assert outcome.record.count_check is None
    # request が 1 回も呼ばれていない （ describe / describe_object は別経路 ）
    assert client.request_calls == []


def test_fetch_one_soql_made_it_none_skips_query_request(tmp_path: Path) -> None:
    """SOQL が ``作れず`` で ``count_soql=None`` のときは query が呼ばれない
    （ レポート件数は取る ） 。"""
    client = _FakeClient(
        describe=_metadata(report_type="$Unknown$"),  # 主オブジェクト不明
        request_responses=[
            (_report_request_path(REPORT_ID), (_report_payload(50), {})),
        ],
    )
    entry = MasterEntry(key="1", summary="", url=f"{DOMAIN}/{REPORT_ID}/view", enabled=True)
    outcome = _fetch_one(
        _settings(tmp_path, soql_count=True),
        entry,
        site_for=_site_for(client),
        store=DescribeStore.empty(tmp_path / "describe.csv"),
        refresh_set=set(),
    )
    assert outcome.status == "ok"
    assert outcome.record is not None
    # count_check は 「 確認できず 」 SOQL 作れず
    assert outcome.record.count_check is not None
    assert outcome.record.count_check.status == COUNT_CHECK_UNKNOWN
    assert outcome.record.count_check.report_count == 50
    assert outcome.record.count_check.soql_count is None
    assert outcome.record.count_check.reason == "SOQL を作れず"
    # request_calls には /query が無い（ レポート件数取得の 1 件だけ ）
    assert all(not call[1].startswith("/query") for call in client.request_calls)
    assert any(call[1].startswith("/analytics/reports") for call in client.request_calls)


def test_fetch_one_count_check_uses_record_soql_draft(tmp_path: Path) -> None:
    """``record.soql_draft`` が record に持たせられている。 tables 側で
    ``build_soql`` を 2 回呼ばない （ ここでは fetch 段階で持つことに集中 ）。
    """
    client = _FakeClient(
        describe=_metadata(),
        object_describes={"Opportunity": _opportunity_with_name()},
        request_responses=[
            (_report_request_path(REPORT_ID), (_report_payload(10), {})),
            (
                _soql_query_path(
                    "SELECT COUNT()\nFROM Opportunity"
                ),
                _query_payload(10),
            ),
        ],
    )
    entry = MasterEntry(key="1", summary="", url=f"{DOMAIN}/{REPORT_ID}/view", enabled=True)
    outcome = _fetch_one(
        _settings(tmp_path, soql_count=True),
        entry,
        site_for=_site_for(client),
        store=DescribeStore.empty(tmp_path / "describe.csv"),
        refresh_set=set(),
    )
    assert outcome.status == "ok"
    record = outcome.record
    assert record is not None
    # soql_draft が record に紐づいている
    assert hasattr(record, "soql_draft")
    assert isinstance(record.soql_draft, SoqlDraft)
    assert record.soql_draft.status != STATUS_NONE
    assert "FROM Opportunity" in record.soql_draft.soql
    # 一致 （ 10 == 10 ）
    assert record.count_check is not None
    assert record.count_check.status == COUNT_CHECK_MATCHED
    assert record.count_check.report_count == 10
    assert record.count_check.soql_count == 10


def test_fetch_one_does_not_use_report_get_for_count(tmp_path: Path, caplog) -> None:
    """件数チェックが行データを取らない: ``report.get`` も ``?includeDetails=true``
    のレポート取得も呼ばない。"""
    caplog.set_level(logging.INFO)

    class _Boom:
        def __len__(self):  # pragma: no cover - 呼ばれないことを確認
            raise AssertionError("len() 呼ばれた = 行データに触れた")

        def __iter__(self):  # pragma: no cover
            raise AssertionError("iter() 呼ばれた = 行データに触れた")

    client = _FakeClient(
        describe=_metadata(),
        object_describes={"Opportunity": _opportunity_with_name()},
        get_table=_Boom(),
        request_responses=[
            (_report_request_path(REPORT_ID), (_report_payload(7), {})),
            # SOQL COUNT() の totalSize は 7
            (
                _soql_query_path(
                    "SELECT COUNT()\nFROM Opportunity"
                ),
                _query_payload(7),
            ),
        ],
    )
    entry = MasterEntry(key="1", summary="", url=f"{DOMAIN}/{REPORT_ID}/view", enabled=True)
    outcome = _fetch_one(
        _settings(tmp_path, soql_count=True, ),  # row_limit=False 既定
        entry,
        site_for=_site_for(client),
        store=DescribeStore.empty(tmp_path / "describe.csv"),
        refresh_set=set(),
    )
    assert outcome.status == "ok"
    assert outcome.record is not None
    assert outcome.record.count_check is not None
    assert outcome.record.count_check.status == COUNT_CHECK_MATCHED
    # report.get は呼ばれていない
    assert client.report.get_calls == []


def test_fetch_one_soql_body_not_in_info_log(tmp_path: Path, caplog) -> None:
    """INFO ログに SOQL 本文が出ないこと。"""
    caplog.set_level(logging.INFO)
    client = _FakeClient(
        describe=_metadata(),
        object_describes={"Opportunity": _opportunity_with_name()},
        request_responses=[
            (_report_request_path(REPORT_ID), (_report_payload(1), {})),
            (
                _soql_query_path(
                    "SELECT COUNT()\nFROM Opportunity"
                ),
                _query_payload(1),
            ),
        ],
    )
    entry = MasterEntry(key="1", summary="", url=f"{DOMAIN}/{REPORT_ID}/view", enabled=True)
    _fetch_one(
        _settings(tmp_path, soql_count=True),
        entry,
        site_for=_site_for(client),
        store=DescribeStore.empty(tmp_path / "describe.csv"),
        refresh_set=set(),
    )
    for record in caplog.records:
        if record.levelno >= logging.INFO:
            assert "FROM Opportunity" not in record.getMessage(), (
                "SOQL 本文が INFO 以上のログに出ている"
            )


def test_fetch_one_partial_failure_warning_for_count_check_unknown(tmp_path: Path) -> None:
    """件数チェックが ``確認できず`` なら警告表に出る（ 既存と同じ形 ）。"""
    client = _FakeClient(
        describe=_metadata(),
        object_describes={"Opportunity": _opportunity_with_name()},
        # RowCount の fact_aggregates が無い設定
        request_responses=[
            (_report_request_path(REPORT_ID), (_report_payload(), {})),
            (
                _soql_query_path("SELECT COUNT()\nFROM Opportunity"),
                _query_payload(2),
            ),
        ],
    )
    entry = MasterEntry(key="1", summary="", url=f"{DOMAIN}/{REPORT_ID}/view", enabled=True)
    outcome = _fetch_one(
        _settings(tmp_path, soql_count=True),
        entry,
        site_for=_site_for(client),
        store=DescribeStore.empty(tmp_path / "describe.csv"),
        refresh_set=set(),
    )
    assert outcome.status == "ok"
    record = outcome.record
    assert record is not None
    assert record.count_check is not None
    assert record.count_check.status == COUNT_CHECK_UNKNOWN


# ── SOQL の SELECT 句書き換えルール ──────────────────────────


def test_build_count_soql_replaces_select_with_count(tmp_path: Path) -> None:
    """``build_count_soql`` は SOQL の SELECT 句を ``COUNT()`` に置き換え、
    ORDER BY / LIMIT を外す（ ``COUNT()`` の直後が ``FROM`` になる ）。
    ``topRows.rowLimit`` が ``limit`` の戻り値になる。"""
    # topRows.rowLimit=5 の metadata を直接作る
    metadata = {
        "reportMetadata": {
            "reportType": {"type": "Opportunity"},
            "reportFormat": "TABULAR",
            "detailColumns": ["Opp.Name"],
            # sortBy を入れて ORDER BY が必ず生成されるようにする
            # （ LIMIT だけでなく ORDER BY も除去されることを確かめる ）
            "sortBy": [
                {"sortColumn": "Opp.Name", "sortOrder": "ASC"},
            ],
            "topRows": {"rowLimit": 5},
        },
        "reportExtendedMetadata": {
            "detailColumnInfo": {"Opp.Name": {"label": "商談名"}},
        },
    }
    client = _FakeClient(
        describe=metadata,
        object_describes={"Opportunity": _opportunity_with_name()},
    )
    entry = MasterEntry(key="1", summary="", url=f"{DOMAIN}/{REPORT_ID}/view", enabled=True)
    outcome = _fetch_one(
        _settings(tmp_path, soql_count=False),  # request を呼ばない
        entry,
        site_for=_site_for(client),
        store=DescribeStore.empty(tmp_path / "describe.csv"),
        refresh_set=set(),
    )
    record = outcome.record
    assert record is not None
    draft = record.soql_draft

    # ``build_count_soql`` を 改めて record 経由で呼ぶ
    from src.soql_builder import build_count_soql

    count_soql, limit = build_count_soql(record)
    assert count_soql is not None
    # SELECT COUNT() で始まり、 ORDER BY / LIMIT を含まない
    assert count_soql.startswith("SELECT COUNT()")
    assert "ORDER BY" not in count_soql.upper()
    assert "LIMIT" not in count_soql.upper()
    # topRows.rowLimit=5 が取れる
    assert limit == 5
    # 元の draft に FROM が残っていれば、 COUNT 用 SOQL にも残る
    if "FROM" in draft.soql.upper():
        assert "FROM" in count_soql.upper()


def test_fetch_one_count_check_passes_through_query_with_correct_soql_shape() -> None:
    """3 列 + ``WHERE`` の値に `` LIMIT `` / `` ORDER BY `` を含む条件で
    件数用 SOQL を ``_FakeClient`` の ``/query?q=...`` まで投げたとき、
    ``_FakeClient.request`` の SOQL 形 assert (``SELECT COUNT()`` +
    ``FROM`` の前に他の列名が無い) が通ることを確認する。 文字列の
    切り貼りで 1 行目だけ ``COUNT()`` に置換する壊れた実装だと assertion
    で必ず落ちる。
    """
    from src.soql_builder import build_count_soql

    column_map = [
        {
            "列キー": "Subject",
            "表示名": "件名",
            "所属オブジェクト": "Task",
            "対応フィールドAPI名": "Subject",
            "型": "string",
            "参照先オブジェクト": "",
            "リレーション名": "",
            "備考": "",
        },
        {
            "列キー": "What.Name",
            "表示名": "What名",
            "所属オブジェクト": "Task",
            "対応フィールドAPI名": "What.Name",
            "型": "string",
            "参照先オブジェクト": "",
            "リレーション名": "",
            "備考": "",
        },
        {
            "列キー": "Owner.Name",
            "表示名": "Owner名",
            "所属オブジェクト": "Task",
            "対応フィールドAPI名": "Owner.Name",
            "型": "string",
            "参照先オブジェクト": "",
            "リレーション名": "",
            "備考": "",
        },
    ]
    metadata = {
        "reportType": {"type": "Task"},
        "reportFormat": "TABULAR",
        "detailColumns": ["Subject", "What.Name", "Owner.Name"],
        "reportFilters": [
            {"column": "Subject", "operator": "contains", "value": "A LIMIT B"},
            {"column": "What.Name", "operator": "equals", "value": "B"},
        ],
        "reportBooleanFilter": "1 OR 2",
        "topRows": {"rowLimit": 10},
    }
    record = ReportRecord(
        key="1",
        summary="",
        report_id=REPORT_ID,
        url=f"{DOMAIN}/{REPORT_ID}/view",
        main_object="Task",
        report={"reportMetadata": metadata, "reportExtendedMetadata": {}},
        objects={},
        relations=(),
        column_map=column_map,
        warnings=(),
    )
    # ``build_soql`` を回して ``soql_draft`` を持つ record を作る
    draft = build_soql(record)
    assert draft.status != STATUS_NONE  # 件数用 SOQL が組み立てられる前提
    record_with_draft = ReportRecord(
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
    )
    count_soql, limit = build_count_soql(record_with_draft)
    assert count_soql is not None
    assert limit == 10

    client = _FakeClient(
        describe={},
        request_responses=[
            (
                _soql_query_path(count_soql),
                _query_payload(7),
            )
        ],
    )
    # ``_fetch_soql_total_count`` に件数用 SOQL を投げる。 ``_FakeClient.request``
    # の SOQL 形 assert が先走り、``SELECT COUNT()`` 形のときに限って通る。
    # 壊れた実装では 1 行目だけ ``COUNT()`` に置換して 2 列目以降が残るため、
    # wire に ``SELECT COUNT()\\n  What.Name,\\n  Owner.Name\\nFROM …`` が
    # 流れた瞬間に assertion が落ちる。
    total, error = _fetch_soql_total_count(client, count_soql, limit)
    assert error is None
    assert total == 7
    # /query が 1 回呼ばれている ( 件数チェックが走った )
    assert any(call[1].startswith("/query") for call in client.request_calls)
