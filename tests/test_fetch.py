"""``fetch`` フローのテスト（偽物の Salesforce クライアントで）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from comken.core.table import Table
from comken.exceptions import SalesforceRequestError

from src.fetch import (
    _apply_related_limit,
    _collect_related_object_names,
    _fetch_object_cached,
    _main_object_name,
    run_fetch,
)
from src.master import MasterEntry
from src.settings import Settings

DOMAIN = "https://example--sandbox.sandbox.my.salesforce.com/lightning/r/Report"


# ── 偽物のクライアント ────────────────────────────────────────────────────


class _ReportStub:
    def __init__(self, describe: dict, fields: tuple[Table, str | None]) -> None:
        self._describe = describe
        self._fields = fields

    def describe(self, report_id: str) -> dict:
        self.last_report_id = report_id
        return self._describe

    def describe_fields_with_object_status(self, metadata: dict) -> tuple[Table, str | None]:
        return self._fields


class _FakeClient:
    def __init__(
        self,
        describe: dict,
        fields: tuple[Table, str | None],
        object_describes: dict[str, dict] | None = None,
        errors: dict[str, BaseException] | None = None,
    ) -> None:
        self.report = _ReportStub(describe, fields)
        self._object_describes = object_describes or {}
        self._errors = errors or {}
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
        self.open_count = 0

    def __call__(self) -> _FakeClient:
        return self

    def __enter__(self) -> _FakeClient:
        self.open_count += 1
        return self._client

    def __exit__(self, *args: object) -> None:
        return None


def _site_for(client: _FakeClient):
    """``site_for(url)`` 互換の関数。"""
    site = _FakeSite(client)

    def resolver(url: str) -> _FakeSite:
        return site

    return resolver, site


def _settings(tmp_path: Path, related_max: int = 40) -> Settings:
    return Settings(
        master_xlsx_path=tmp_path / "master.xlsx",
        output_dir=tmp_path / "output",
        related_max=related_max,
        credential_prefix="",
    )


def _output_dir_exists(tmp_path: Path) -> Path:
    """出力先ディレクトリが無ければ作って返す（``iterdir`` 用）。"""
    output = tmp_path / "output"
    output.mkdir(parents=True, exist_ok=True)
    return output


def _make_metadata(
    report_type: str = "Opportunity",
    *,
    extra: dict | None = None,
) -> dict:
    metadata = {
        "reportMetadata": {
            "reportType": {"type": report_type},
            "reportFormat": "TABULAR",
            "detailColumns": ["Opp.Name"],
        }
    }
    if extra:
        metadata["reportMetadata"].update(extra)
    return metadata


def _make_fields(*rows: dict) -> tuple[Table, str | None]:
    return (
        Table(["列キー", "表示名", "対応フィールドAPI名", "型", "備考"], list(rows)),
        None,
    )


def _main_describe(
    name: str,
    fields: list[dict] | None = None,
    *,
    fields_map: dict[str, list[str]] | None = None,
) -> dict:
    """主オブジェクト describe を作る。``fields_map`` で ``Name`` → ``[Account, Contact]`` のように
    relationship を作れる（``referenceTo`` / ``relationshipName`` を埋める）。
    """
    fields = fields if fields is not None else []
    built: list[dict] = []
    if fields_map:
        for field_name, refs in fields_map.items():
            for ref in refs:
                built.append(
                    {
                        "name": field_name,
                        "type": "reference",
                        "referenceTo": [ref],
                        "relationshipName": f"{field_name}_{ref}",
                    }
                )
    built.extend(fields)
    return {"name": name, "fields": built}


# ── JSON 出力 ───────────────────────────────────────────────────────────


def test_run_fetch_writes_json_with_all_keys(tmp_path: Path) -> None:
    entry = MasterEntry(
        key="1001",
        summary="顧客一覧",
        url=f"{DOMAIN}/00O5g00000ABCDE/view",
        enabled=True,
    )
    metadata = _make_metadata()
    fields, _ = _make_fields(
        {
            "列キー": "Opp.Name",
            "表示名": "名前",
            "対応フィールドAPI名": "Name",
            "型": "string",
            "備考": "",
        }
    )
    client = _FakeClient(
        describe=metadata,
        fields=(fields, None),
        object_describes={
            "Opportunity": _main_describe("Opportunity", fields_map={"AccountId": ["Account"]}),
            "Account": _main_describe("Account"),
        },
    )
    site_for, _site = _site_for(client)
    settings = _settings(tmp_path)

    outcomes = run_fetch(settings, [entry], site_for=site_for)

    assert outcomes[0].status == "ok"
    assert outcomes[0].output_path is not None
    payload = json.loads(outcomes[0].output_path.read_text(encoding="utf-8"))
    assert payload["管理番号"] == "1001"
    assert payload["概要"] == "顧客一覧"
    assert payload["レポートID"] == "00O5g00000ABCDE"
    assert payload["URL"] == f"{DOMAIN}/00O5g00000ABCDE/view"
    assert "取得日時" in payload
    assert payload["主オブジェクト"] == "Opportunity"
    assert payload["report"] == metadata
    assert set(payload["objects"]) == {"Opportunity", "Account"}
    assert payload["objects"]["Opportunity"] == _main_describe(
        "Opportunity", fields_map={"AccountId": ["Account"]}
    )
    assert payload["objects"]["Account"] == _main_describe("Account")
    assert payload["column_map"] == [
        {
            "列キー": "Opp.Name",
            "表示名": "名前",
            "対応フィールドAPI名": "Name",
            "型": "string",
            "備考": "",
        }
    ]
    assert payload["warnings"] == []


def test_objects_dict_preserves_describe_content(tmp_path: Path) -> None:
    """``objects`` の中身は comken が返した dict をそのまま（加工・間引きをしない）。

    JSON 経由のラウンドトリップで再構築されるので ``is`` 比較ではなく、
    内容一致（``==``）で確かめる。
    """
    original = _main_describe("Opportunity", fields_map={"AccountId": ["Account"]})
    entry = MasterEntry(key="1", summary="", url=f"{DOMAIN}/00O5g00000ABCDE/view", enabled=True)
    client = _FakeClient(
        describe=_make_metadata(),
        fields=_make_fields(),
        object_describes={"Opportunity": original, "Account": _main_describe("Account")},
    )
    site_for, _ = _site_for(client)
    payload_path = run_fetch(_settings(tmp_path), [entry], site_for=site_for)[0].output_path
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    assert payload["objects"]["Opportunity"] == original
    assert payload["objects"]["Account"] == _main_describe("Account")


def test_related_objects_dedup_excludes_main_polymorphic(tmp_path: Path) -> None:
    """関連オブジェクト: ``referenceTo`` がポリモーフィックなら展開、自身は除く、出現順、重複なし。"""
    main = _main_describe(
        "Opportunity",
        fields_map={
            "AccountId": ["Account"],
            "WhoId": ["Contact", "Lead"],  # ポリモーフィック
            "OwnerId": ["User"],
            "DuplicateAccountId": ["Account"],  # 重複（先勝ち）
        },
    )
    entry = MasterEntry(key="1", summary="", url=f"{DOMAIN}/00O5g00000ABCDE/view", enabled=True)
    client = _FakeClient(
        describe=_make_metadata(),
        fields=_make_fields(),
        object_describes={
            "Opportunity": main,
            "Account": _main_describe("Account"),
            "Contact": _main_describe("Contact"),
            "Lead": _main_describe("Lead"),
            "User": _main_describe("User"),
        },
    )
    site_for, _ = _site_for(client)
    payload_path = run_fetch(_settings(tmp_path), [entry], site_for=site_for)[0].output_path
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    # 順序: Account, Contact, Lead, User（出現順。Account 重複は出ない）
    assert list(payload["objects"]) == ["Opportunity", "Account", "Contact", "Lead", "User"]


def test_related_object_failure_becomes_warning(tmp_path: Path) -> None:
    """関連オブジェクト 1 つの HTTP エラー（401/403 以外）は警告で続き、JSON は書かれる。"""
    main = _main_describe("Opportunity", fields_map={"AccountId": ["Account"], "OwnerId": ["User"]})
    entry = MasterEntry(key="1", summary="", url=f"{DOMAIN}/00O5g00000ABCDE/view", enabled=True)
    client = _FakeClient(
        describe=_make_metadata(),
        fields=_make_fields(),
        object_describes={"Opportunity": main, "Account": _main_describe("Account")},
        errors={"User": SalesforceRequestError("GET", "/sobjects/User/describe", 404, "Not Found")},
    )
    site_for, _ = _site_for(client)
    payload_path = run_fetch(_settings(tmp_path), [entry], site_for=site_for)[0].output_path
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    assert payload["主オブジェクト"] == "Opportunity"
    assert "Account" in payload["objects"]
    assert "User" not in payload["objects"]
    assert any("User" in w and "404" in w for w in payload["warnings"])


def test_related_max_overflow_warns(tmp_path: Path) -> None:
    """``RELATED_MAX`` を超えた分は取らず、件数と名前が警告に残る。"""
    # 関連を 5 件作る（Account / Contact / Lead / User / Campaign / Opportunity__c ...）
    fields_map = {
        "F1": ["Account"],
        "F2": ["Contact"],
        "F3": ["Lead"],
        "F4": ["User"],
        "F5": ["Campaign"],
    }
    main = _main_describe("Opportunity", fields_map=fields_map)
    entry = MasterEntry(key="1", summary="", url=f"{DOMAIN}/00O5g00000ABCDE/view", enabled=True)
    client = _FakeClient(
        describe=_make_metadata(),
        fields=_make_fields(),
        object_describes={
            "Opportunity": main,
            "Account": _main_describe("Account"),
            "Contact": _main_describe("Contact"),
            "Lead": _main_describe("Lead"),
            "User": _main_describe("User"),
            "Campaign": _main_describe("Campaign"),
        },
    )
    site_for, _ = _site_for(client)
    payload_path = run_fetch(_settings(tmp_path, related_max=2), [entry], site_for=site_for)[
        0
    ].output_path
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    assert list(payload["objects"]) == ["Opportunity", "Account", "Contact"]
    warning = next(w for w in payload["warnings"] if "上限" in w)
    # 全体の件数（5 件）と上限（2）、スキップ件数（3 件）と残りの名前が入る
    assert "5 件" in warning
    assert "2" in warning
    assert "3 件" in warning
    assert "Lead" in warning and "User" in warning and "Campaign" in warning


def test_main_object_describe_failure_yields_null(tmp_path: Path) -> None:
    """主オブジェクトの describe_object が失敗（401/403 以外）→ ``主オブジェクト: null`` 、警告、
    関連は取らない。"""
    entry = MasterEntry(key="1", summary="", url=f"{DOMAIN}/00O5g00000ABCDE/view", enabled=True)
    client = _FakeClient(
        describe=_make_metadata("Opportunity"),
        fields=_make_fields(),
        errors={
            "Opportunity": SalesforceRequestError(
                "GET", "/sobjects/Opportunity/describe", 400, "Bad"
            )
        },
    )
    site_for, _ = _site_for(client)
    payload_path = run_fetch(_settings(tmp_path), [entry], site_for=site_for)[0].output_path
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    assert payload["主オブジェクト"] is None
    assert payload["objects"] == {}
    assert any("Opportunity" in w for w in payload["warnings"])


def test_auth_error_on_main_object_marks_id_failed(tmp_path: Path) -> None:
    """主オブジェクト describe の 401/403 → ID 失敗、JSON は書かれない。"""
    output = _output_dir_exists(tmp_path)
    entry = MasterEntry(key="1", summary="", url=f"{DOMAIN}/00O5g00000ABCDE/view", enabled=True)
    client = _FakeClient(
        describe=_make_metadata("Opportunity"),
        fields=_make_fields(),
        errors={
            "Opportunity": SalesforceRequestError(
                "GET", "/sobjects/Opportunity/describe", 401, "Unauthorized"
            )
        },
    )
    site_for, _ = _site_for(client)
    outcomes = run_fetch(_settings(tmp_path), [entry], site_for=site_for)
    assert outcomes[0].status == "failed"
    assert "401" in (outcomes[0].error or "")
    assert outcomes[0].output_path is None
    # JSON は書かれていない
    assert not list(output.iterdir())


def test_auth_error_on_related_object_marks_id_failed(tmp_path: Path) -> None:
    """関連オブジェクト describe の 401/403 → ID 失敗。"""
    output = _output_dir_exists(tmp_path)
    main = _main_describe("Opportunity", fields_map={"AccountId": ["Account"]})
    entry = MasterEntry(key="1", summary="", url=f"{DOMAIN}/00O5g00000ABCDE/view", enabled=True)
    client = _FakeClient(
        describe=_make_metadata(),
        fields=_make_fields(),
        object_describes={"Opportunity": main},
        errors={
            "Account": SalesforceRequestError("GET", "/sobjects/Account/describe", 403, "Forbidden")
        },
    )
    site_for, _ = _site_for(client)
    outcomes = run_fetch(_settings(tmp_path), [entry], site_for=site_for)
    assert outcomes[0].status == "failed"
    assert "403" in (outcomes[0].error or "")
    # 主オブジェクトは書かれる前なので JSON 無し
    assert not list(output.iterdir())


def test_bad_url_marks_id_failed(tmp_path: Path) -> None:
    """URL が壊れている行は ``fetch`` 段で失敗として報告。"""
    entry = MasterEntry(key="1", summary="", url="https://壊れたURL", enabled=True)
    site_for, _ = _site_for(_FakeClient(describe={}, fields=_make_fields()))
    outcomes = run_fetch(_settings(tmp_path), [entry], site_for=site_for)
    assert outcomes[0].status == "failed"
    assert "URL" in (outcomes[0].error or "")


def test_object_describe_cached_across_ids(tmp_path: Path) -> None:
    """同じ実行で複数 ID が同じオブジェクトを参照しても ``describe_object`` は 1 回だけ。"""
    main = _main_describe("Opportunity", fields_map={"AccountId": ["Account"]})
    metadata = _make_metadata()
    fields, _ = _make_fields()
    client = _FakeClient(
        describe=metadata,
        fields=(fields, None),
        object_describes={
            "Opportunity": main,
            "Account": _main_describe("Account"),
        },
    )
    site_for, _ = _site_for(client)
    settings = _settings(tmp_path)
    entries = [
        MasterEntry(key="1001", summary="", url=f"{DOMAIN}/00O5g00000AAAAA/view", enabled=True),
        MasterEntry(key="1002", summary="", url=f"{DOMAIN}/00O5g00000BBBBB/view", enabled=True),
    ]
    run_fetch(settings, entries, site_for=site_for)
    # Opportunity と Account は 2 ID で参照されるが、HTTP は 1 回ずつ
    assert client.describe_object_calls.count("Opportunity") == 1
    assert client.describe_object_calls.count("Account") == 1


def test_failed_object_cached(tmp_path: Path) -> None:
    """関連オブジェクトの失敗（401/403 以外）もキャッシュされ、2 度目は再試行しない。"""
    main = _main_describe("Opportunity", fields_map={"AccountId": ["Account"]})
    metadata = _make_metadata()
    fields, _ = _make_fields()
    client = _FakeClient(
        describe=metadata,
        fields=(fields, None),
        object_describes={"Opportunity": main},
        errors={
            "Account": SalesforceRequestError("GET", "/sobjects/Account/describe", 500, "Boom")
        },
    )
    site_for, _ = _site_for(client)
    settings = _settings(tmp_path)
    entries = [
        MasterEntry(key="1001", summary="", url=f"{DOMAIN}/00O5g00000AAAAA/view", enabled=True),
        MasterEntry(key="1002", summary="", url=f"{DOMAIN}/00O5g00000BBBBB/view", enabled=True),
    ]
    outcomes = run_fetch(settings, entries, site_for=site_for)
    # 両方 OK（警告付き）
    assert outcomes[0].status == "ok"
    assert outcomes[1].status == "ok"
    # Account の describe_object は 1 回しか呼ばれない
    assert client.describe_object_calls.count("Account") == 1


def test_partial_failure_exit_code_is_one(tmp_path: Path) -> None:
    """1 件失敗しても残りの ID は取られ、``cli`` 側で exit code を 1 にする判定。"""
    entries = [
        MasterEntry(key="ok", summary="", url=f"{DOMAIN}/00O5g00000AAAAA/view", enabled=True),
        MasterEntry(key="bad", summary="", url="https://壊れたURL", enabled=True),
        MasterEntry(key="ok2", summary="", url=f"{DOMAIN}/00O5g00000BBBBB/view", enabled=True),
    ]
    client = _FakeClient(
        describe=_make_metadata(),
        fields=_make_fields(),
        object_describes={"Opportunity": _main_describe("Opportunity")},
    )
    site_for, _ = _site_for(client)
    outcomes = run_fetch(_settings(tmp_path), entries, site_for=site_for)
    statuses = [o.status for o in outcomes]
    assert statuses == ["ok", "failed", "ok"]


def test_dry_run_does_not_open_site(tmp_path: Path) -> None:
    """``--dry-run`` は ``site`` を一度も開かず、JSON を書かない。"""
    output = _output_dir_exists(tmp_path)
    entry = MasterEntry(
        key="1", summary="顧客一覧", url=f"{DOMAIN}/00O5g00000ABCDE/view", enabled=True
    )
    client = _FakeClient(describe={}, fields=_make_fields())
    site_for, _site = _site_for(client)
    outcomes = run_fetch(_settings(tmp_path), [entry], dry_run=True, site_for=site_for)
    assert outcomes[0].status == "dry-run"
    assert _site.open_count == 0
    # 出力先にファイルが無い
    assert not list(output.iterdir())


def test_site_for_salesforce_error_marks_id_failed(tmp_path: Path) -> None:
    """``site_for`` が ``SalesforceError`` （未登録組織の URL など）を出したら、
    その ID だけ失敗扱いにする。 ``site`` は開かれず、 JSON も書かれない。

    なぜ: 組織を追加し忘れて 1 件だけ URL が違うと、 1 件で全実行が止まると
    他が困るので、 ID ごとに切り離す。
    """
    from comken.exceptions import SalesforceError

    output = _output_dir_exists(tmp_path)
    good = MasterEntry(key="good", summary="", url=f"{DOMAIN}/00O5g00000AAAAA/view", enabled=True)
    bad = MasterEntry(
        key="bad",
        summary="",
        url=f"{DOMAIN}/00O5g00000BBBBB/view",
        enabled=True,
    )

    good_client = _FakeClient(
        describe=_make_metadata(),
        fields=_make_fields(),
        object_describes={"Opportunity": _main_describe("Opportunity")},
    )

    def resolver(url: str) -> _FakeSite:
        # bad だけ未登録組織として ``SalesforceError`` を投げる
        if "BBBBB" in url:
            raise SalesforceError("未登録")
        site = _FakeSite(good_client)

        def call() -> _FakeSite:
            return site

        return call

    settings = _settings(tmp_path)
    outcomes = run_fetch(settings, [good, bad], site_for=resolver)

    # good は OK、bad は failed（個別）
    statuses = {o.entry.key: o.status for o in outcomes}
    assert statuses == {"good": "ok", "bad": "failed"}
    assert outcomes[1].error and "接続先を決定できません" in outcomes[1].error

    # bad の JSON は書かれていない（good だけ書かれる）
    files = {path.name for path in output.iterdir()}
    assert files == {"good.json"}


def test_site_for_comken_error_marks_id_failed(tmp_path: Path) -> None:
    """``site_for`` が ``ComkenError`` （ ``SalesforceError`` 以外の派生）を
    出しても、その ID だけ失敗扱いにする。
    """
    from comken.exceptions import ComkenError

    output = _output_dir_exists(tmp_path)
    bad = MasterEntry(
        key="bad",
        summary="",
        url=f"{DOMAIN}/00O5g00000BBBBB/view",
        enabled=True,
    )

    def resolver(url: str) -> _FakeSite:
        raise ComkenError("何か comken のエラー")

    outcomes = run_fetch(_settings(tmp_path), [bad], site_for=resolver)

    assert outcomes[0].status == "failed"
    assert outcomes[0].error and "接続先を決定できません" in outcomes[0].error
    # JSON は書かれていない
    assert not list(output.iterdir())


def test_json_write_oserror_marks_id_failed(tmp_path: Path) -> None:
    """JSON の書き出しで ``OSError`` が起きると、 その ID だけ失敗扱いにする。

    なぜ: 出力先フォルダが消えた・権限が落ちた・ディスクが一杯、 といった
    環境側の問題で他の ID まで巻き込みたくないため。
    他の ID はそのまま書き続けられることを確かめる。
    """
    output = _output_dir_exists(tmp_path)
    entries = [
        MasterEntry(key="ok", summary="", url=f"{DOMAIN}/00O5g00000AAAAA/view", enabled=True),
        MasterEntry(key="bad", summary="", url=f"{DOMAIN}/00O5g00000BBBBB/view", enabled=True),
        MasterEntry(key="ok2", summary="", url=f"{DOMAIN}/00O5g00000CCCCC/view", enabled=True),
    ]
    client = _FakeClient(
        describe=_make_metadata(),
        fields=_make_fields(),
        object_describes={"Opportunity": _main_describe("Opportunity")},
    )
    site_for, _ = _site_for(client)

    from src import fetch as fetch_module

    original_atomic = fetch_module.atomic_write
    real_atomic = original_atomic

    def broken_atomic_write(path):
        if path.name == "bad.json":
            # ``atomic_write`` は ``with`` 文のコンテキストマネージャとして
            # 使われるので、 ``__enter__`` を呼ぶ前に例外を上げると
            # ``_fetch_one`` の ``except OSError`` 経路に入る
            raise OSError("ディスク書き込み失敗")
        return real_atomic(path)

    fetch_module.atomic_write = broken_atomic_write  # type: ignore[assignment]
    try:
        outcomes = run_fetch(_settings(tmp_path), entries, site_for=site_for)
    finally:
        fetch_module.atomic_write = original_atomic  # type: ignore[assignment]

    statuses = [o.status for o in outcomes]
    assert statuses == ["ok", "failed", "ok"]
    assert outcomes[1].error and "書き出しに失敗" in outcomes[1].error

    # bad の JSON は存在せず、 ok / ok2 は書かれている
    files = {path.name for path in output.iterdir()}
    assert files == {"ok.json", "ok2.json"}


# ── fetch → tables の接続（ ``_cmd_fetch`` 経由） ─────────────────────────


def _build_settings(tmp_path: Path) -> Settings:
    """CLI テスト用の ``Settings`` を作る（ ``output_dir`` を含む）。"""
    return Settings(
        master_xlsx_path=tmp_path / "master.xlsx",
        output_dir=tmp_path / "output",
        related_max=40,
        credential_prefix="",
    )


def _write_empty_master(tmp_path: Path, rows: list[dict] | None = None) -> None:
    """管理表 xlsx を ``tmp_path/master.xlsx`` に置く（既定は 0 行）。
    ``rows`` を渡すとその行も書き込む（ ``fetch --dry-run`` の対象用）。
    """
    from openpyxl import Workbook
    from openpyxl.worksheet.table import Table, TableStyleInfo

    rows = rows or []
    path = tmp_path / "master.xlsx"
    workbook = Workbook()
    active = workbook.active
    if active is not None:
        workbook.remove(active)
    sheet = workbook.create_sheet("PY_管理表")
    headers = [
        "ID",
        "グループ名",
        "担当者",
        "概要",
        "Salesforce URL",
        "保存先",
        "有効",
        "0件あり",
        "備考",
    ]
    sheet.append(headers)
    for row in rows:
        sheet.append([row.get(col, "") for col in headers])
    table = Table(displayName="PY_T_ReportEntry", ref=f"A1:I{len(rows) + 1}")
    table.tableStyleInfo = TableStyleInfo(
        name="TableStyleMedium2",
        showFirstColumn=False,
        showLastColumn=False,
        showRowStripes=True,
        showColumnStripes=False,
    )
    sheet.add_table(table)
    workbook.save(path)


def test_cmd_fetch_calls_tables_only_for_ok_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``fetch`` のあと、 ``_cmd_fetch`` が ``run_tables`` を呼び、
    成功した ID の ``対応表_{管理番号}.csv`` だけ作る。失敗した ID の
    既存 CSV はそのまま残る。
    """
    from src import cli as cli_module
    from src.fetch import FetchOutcome

    _write_empty_master(
        tmp_path,
        [
            {
                "ID": "good",
                "Salesforce URL": f"{DOMAIN}/00O5g00000AAAAA/view",
                "有効": "○",
            },
            {
                "ID": "bad",
                "Salesforce URL": f"{DOMAIN}/00O5g00000BBBBB/view",
                "有効": "○",
            },
        ],
    )
    output = _output_dir_exists(tmp_path)
    # 失敗した ID の既存 CSV （fetch 前に手動で作ったもの）
    bad_csv = output / "対応表_bad.csv"
    bad_csv.write_text("既存のまま", encoding="utf-8")

    # 成功 1 件・失敗 1 件を返す偽 ``run_fetch`` に差し替える
    good = MasterEntry(key="good", summary="", url=f"{DOMAIN}/00O5g00000AAAAA/view", enabled=True)
    bad = MasterEntry(
        key="bad",
        summary="",
        url=f"{DOMAIN}/00O5g00000BBBBB/view",
        enabled=True,
    )

    good_payload_path = output / "good.json"
    import json as _json

    good_payload_path.write_text(
        _json.dumps(
            {
                "管理番号": "good",
                "概要": "",
                "レポートID": "00O000000000001",
                "URL": "",
                "取得日時": "",
                "主オブジェクト": "Opportunity",
                "report": {},
                "objects": {
                    "Opportunity": {
                        "name": "Opportunity",
                        "label": "商談",
                        "fields": [{"name": "Name", "label": "商談名", "type": "string"}],
                    }
                },
                "column_map": [
                    {
                        "列キー": "Opp.Name",
                        "表示名": "商談名",
                        "対応フィールドAPI名": "Name",
                        "型": "string",
                        "備考": "",
                    }
                ],
                "warnings": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    def fake_run_fetch(settings, entries, *, dry_run=False, site_for=None):
        return [
            FetchOutcome(
                entry=good,
                status="ok",
                output_path=good_payload_path,
                warnings=(),
                error=None,
            ),
            FetchOutcome(
                entry=bad,
                status="failed",
                output_path=None,
                warnings=(),
                error="何かで失敗",
            ),
        ]

    monkeypatch.setattr(cli_module, "run_fetch", fake_run_fetch)
    settings = _build_settings(tmp_path)
    monkeypatch.setattr(cli_module, "load_settings", lambda: settings)
    monkeypatch.chdir(tmp_path)

    args = cli_module.build_parser().parse_args(["fetch", "good", "bad"])
    exit_code = cli_module._cmd_fetch(settings, args)
    # 失敗 ID が 1 件あるので exit code は 1
    assert exit_code == 1

    # 成功 ID の CSV は作られ、 失敗 ID の既存 CSV は残る
    assert (output / "対応表_good.csv").exists()
    assert bad_csv.read_text(encoding="utf-8") == "既存のまま"
    # 全体対応表・項目表も作られる
    assert (output / "対応表.csv").exists()
    assert (output / "項目表.csv").exists()


def test_cmd_fetch_dry_run_does_not_call_tables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``fetch --dry-run`` は ``run_tables`` を呼ばず、 CSV も作らない。"""
    from src import cli as cli_module

    _write_empty_master(
        tmp_path,
        [
            {
                "ID": "1001",
                "Salesforce URL": f"{DOMAIN}/00O5g00000AAAAA/view",
                "有効": "○",
            }
        ],
    )
    output = _output_dir_exists(tmp_path)
    called = {"count": 0}

    def fake_run_tables(output_dir, *, only_keys=None):
        called["count"] += 1
        from src.tables import TableOutcome

        return TableOutcome(wrote=(), skipped=())

    monkeypatch.setattr(cli_module, "run_tables", fake_run_tables)
    settings = _build_settings(tmp_path)
    monkeypatch.setattr(cli_module, "load_settings", lambda: settings)
    monkeypatch.chdir(tmp_path)

    args = cli_module.build_parser().parse_args(["fetch", "1001", "--dry-run"])
    assert cli_module._cmd_fetch(settings, args) == 0

    # ``run_tables`` は呼ばれていない
    assert called["count"] == 0
    # CSV も作られていない
    assert list(output.iterdir()) == []


def test_existing_json_not_corrupted_on_failure(tmp_path: Path) -> None:
    """``atomic_write`` の中で例外が起きたとき、既存の JSON が壊れない。

    ``atomic_write`` をパッチして途中で例外を上げ、本体の書き込みロジックが
    一時ファイル経由になっていることを確かめる（直接 ``write_text`` で書き
    換えると、パッチの影響を受けず JSON が破損する）。
    """
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    existing = output_dir / "1.json"
    existing.write_text('{"既存": "そのまま"}', encoding="utf-8")

    from src import fetch as fetch_module

    main = _main_describe("Opportunity", fields_map={"AccountId": ["Account"]})
    entry = MasterEntry(key="1", summary="", url=f"{DOMAIN}/00O5g00000ABCDE/view", enabled=True)
    client = _FakeClient(
        describe=_make_metadata(),
        fields=_make_fields(),
        object_describes={"Opportunity": main},
        errors={
            "Account": SalesforceRequestError("GET", "/sobjects/Account/describe", 500, "Boom")
        },
    )
    site_for, _ = _site_for(client)

    called = {"count": 0}

    def broken_atomic_write(path):
        called["count"] += 1
        # ``atomic_write`` が ``with`` ブロックのコンテキストマネージャとして
        # 使われていることを保証するため、呼ばれた瞬間に例外を上げる
        raise RuntimeError("爆発")

    original = fetch_module.atomic_write
    fetch_module.atomic_write = broken_atomic_write  # type: ignore[assignment]
    try:
        with pytest.raises(RuntimeError):
            run_fetch(_settings(tmp_path), [entry], site_for=site_for)
    finally:
        fetch_module.atomic_write = original  # type: ignore[assignment]

    # ``atomic_write`` が必ず通っている（直接 write_text にすると count=0 になる）
    assert called["count"] == 1
    # 既存 JSON は残っている
    assert existing.read_text(encoding="utf-8") == '{"既存": "そのまま"}'


# ── ヘルパー関数 ─────────────────────────────────────────────────────────


def test_main_object_name_extracts_type() -> None:
    metadata = {"reportMetadata": {"reportType": {"type": "Opportunity"}}}
    assert _main_object_name(metadata) == "Opportunity"


def test_main_object_name_missing_returns_none() -> None:
    assert _main_object_name({}) is None
    assert _main_object_name({"reportMetadata": {}}) is None
    assert _main_object_name({"reportMetadata": {"reportType": {}}}) is None


def test_collect_related_object_names_excludes_main_and_dedups() -> None:
    describe = _main_describe(
        "Opportunity",
        fields_map={
            "AccountId": ["Account"],
            "OwnerId": ["User"],
            "AccountId2": ["Account"],
            "WhatId": ["Account", "Opportunity"],  # 主オブジェクト自身を含む
        },
    )
    result = _collect_related_object_names(describe, exclude="Opportunity")
    # 出現順: Account, User, Opportunity（ただし exclude で Opportunity は除く）
    assert result == ["Account", "User"]


def test_apply_related_limit() -> None:
    names = ["a", "b", "c", "d"]
    kept, skipped = _apply_related_limit(names, 2)
    assert kept == ["a", "b"]
    assert skipped == ["c", "d"]
    kept, skipped = _apply_related_limit(names, 10)
    assert kept == names
    assert skipped == []


def test_fetch_object_cached_calls_only_once() -> None:
    """キャッシュ: 同じ名前は 1 回しか HTTP を打たない。"""
    client = _FakeClient(
        describe={},
        fields=_make_fields(),
        object_describes={"Account": _main_describe("Account")},
    )
    cache: dict = {}
    warnings1: list[str] = []
    assert _fetch_object_cached(client, "Account", cache, warnings1) is not None
    warnings2: list[str] = []
    assert _fetch_object_cached(client, "Account", cache, warnings2) is not None
    # 2 回目は client.describe_object が呼ばれないので calls には 1 件のみ
    assert client.describe_object_calls.count("Account") == 1
    assert warnings2 == []
