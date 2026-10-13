"""``trial/`` の単品サンプルを fake client で検証する。
実 Salesforce に繋がないよう、 ``site_for`` をテストで差し替えられる形にし、
``tmp_path`` 配下の ``config.ini`` + 空の管理表で ``load_settings`` を動かす。

``trial/`` は **名前空間パッケージ** （ ``__init__.py`` を **持たない** ）。
``python -m trial.fetch_one_report`` でそのまま動くことを前提に、
``tests/`` 側での ``sys.path`` 書き換えは **行わない** 。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from openpyxl import Workbook, load_workbook
from openpyxl.worksheet.table import Table as XlsxTable
from openpyxl.worksheet.table import TableStyleInfo

DOMAIN = "https://example--sandbox.sandbox.my.salesforce.com/lightning/r/Report"
DOMAIN_PROD = "https://example.my.salesforce.com/lightning/r/Report"


# ── ヘルパー ─────────────────────────────────────────────────────────────


def _build_master(path: Path) -> None:
    """``load_settings`` が困らないよう ``MASTER_XLSX_PATH`` の指す xlsx を作る。

    中身は空の ``PY_管理表`` 1 枚でよい （ 例題は管理表を読まない ）。
    """
    workbook = Workbook()
    active = workbook.active
    if active is not None:
        workbook.remove(active)
    sheet = workbook.create_sheet("PY_管理表")
    sheet.append(["ID", "概要", "Salesforce URL", "有効"])
    table = XlsxTable(displayName="PY_T_ReportEntry", ref="A1:D2")
    table.tableStyleInfo = TableStyleInfo(
        name="TableStyleMedium2",
        showFirstColumn=False,
        showLastColumn=False,
        showRowStripes=True,
        showColumnStripes=False,
    )
    sheet.add_table(table)
    workbook.save(path)


def _build_master_with_rows(path: Path, rows: list[tuple[str, str, str, str]]) -> None:
    """行つきの ``PY_管理表`` を作る。 ``rows`` は ``(ID, 概要, URL, 有効)`` のリスト。"""
    workbook = Workbook()
    active = workbook.active
    if active is not None:
        workbook.remove(active)
    sheet = workbook.create_sheet("PY_管理表")
    sheet.append(["ID", "概要", "Salesforce URL", "有効"])
    for row in rows:
        sheet.append(list(row))
    last_row = len(rows) + 1
    table = XlsxTable(displayName="PY_T_ReportEntry", ref=f"A1:D{last_row}")
    table.tableStyleInfo = TableStyleInfo(
        name="TableStyleMedium2",
        showFirstColumn=False,
        showLastColumn=False,
        showRowStripes=True,
        showColumnStripes=False,
    )
    sheet.add_table(table)
    workbook.save(path)


def _write_ini(tmp_path: Path, master: Path) -> None:
    """テスト用 ``config.ini`` を ``tmp_path`` に書く。"""
    (tmp_path / "config.ini").write_text(
        f"[FILES]\nMASTER_XLSX_PATH = {master.name}\nOUTPUT_DIR = ./output\n",
        encoding="utf-8",
    )


def _patch_load_settings(
    monkeypatch: pytest.MonkeyPatch, module: object, project_root: Path
) -> None:
    """``module.load_settings`` を ``project_root`` からの読み込みに差し替える。"""
    from src import settings as settings_module

    real_load = settings_module.load_settings

    def patched() -> object:
        return real_load(project_root=project_root)

    monkeypatch.setattr(module, "load_settings", patched)


# ── 偽物のクライアント ─────────────────────────────────────────────────


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
    ) -> None:
        self.report = _ReportStub(describe)
        self._object_describes = object_describes or {}
        self.describe_object_calls: list[str] = []

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
        return None, {}

    def describe_object(self, name: str) -> dict:
        self.describe_object_calls.append(name)
        return self._object_describes.get(name, {"name": name, "fields": []})

    def __enter__(self) -> _FakeClient:
        return self._client

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


def _make_metadata(report_type: str = "Opportunity") -> dict:
    return {
        "reportMetadata": {
            "reportType": {"type": report_type},
            "reportFormat": "TABULAR",
            "detailColumns": ["Opp.Name"],
        },
        "reportExtendedMetadata": {
            "detailColumnInfo": {"Opp.Name": {"label": "名前"}},
        },
    }


def _site_for(client: _FakeClient) -> Callable[[str], _FakeSite]:
    """``site_for(url)`` 互換の関数。"""

    def resolver(url: str) -> _FakeSite:
        return _FakeSite(client)

    return resolver


# ── fetch_one_report ────────────────────────────────────────────────────


def test_fetch_one_report_no_argv_exits_2() -> None:
    """引数なしで終了コード 2 (argparse が使い方を出して ``SystemExit``)。"""
    from trial import fetch_one_report

    with pytest.raises(SystemExit) as exc:
        fetch_one_report.main([])
    assert exc.value.code == 2


def test_fetch_one_report_writes_xlsx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """URL と管理番号を渡すと ``output/single/SOQL調査.xlsx`` と
    ``output/single/列名の対応.csv`` が書かれる。 JSON は書かれない。"""
    from trial import fetch_one_report

    master = tmp_path / "master.xlsx"
    _build_master(master)
    _write_ini(tmp_path, master)
    _patch_load_settings(monkeypatch, fetch_one_report, tmp_path)

    client = _FakeClient(
        describe=_make_metadata(),
        object_describes={"Opportunity": {"name": "Opportunity", "fields": []}},
    )

    code = fetch_one_report.main(
        [f"{DOMAIN}/00O5g00000AAAAA/view", "1001"],
        site_for=_site_for(client),
    )

    assert code == 0
    assert (tmp_path / "output" / "single" / "SOQL調査.xlsx").exists()
    assert (tmp_path / "output" / "single" / "列名の対応.csv").exists()
    # per-id の csv / txt は作らない
    assert not (tmp_path / "output" / "single" / "対応表_1001.csv").exists()
    assert not (tmp_path / "output" / "single" / "列名の対応_1001.txt").exists()
    # JSON は書かれていない
    assert not (tmp_path / "output" / "single" / "1001.json").exists()
    # 本体 output には触らない
    assert not (tmp_path / "output" / "1001.csv").exists()

    # xlsx の中身もざっくり確認
    workbook = load_workbook(tmp_path / "output" / "single" / "SOQL調査.xlsx")
    assert "一覧" in workbook.sheetnames
    workbook.close()


def test_fetch_one_report_default_key_is_sample(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """管理番号を省略すると既定 ``sample`` で xlsx が書かれる。"""
    from trial import fetch_one_report

    master = tmp_path / "master.xlsx"
    _build_master(master)
    _write_ini(tmp_path, master)
    _patch_load_settings(monkeypatch, fetch_one_report, tmp_path)

    client = _FakeClient(
        describe=_make_metadata(),
        object_describes={"Opportunity": {"name": "Opportunity", "fields": []}},
    )

    code = fetch_one_report.main(
        [f"{DOMAIN}/00O5g00000AAAAA/view"],
        site_for=_site_for(client),
    )

    assert code == 0
    assert (tmp_path / "output" / "single" / "SOQL調査.xlsx").exists()


def test_fetch_one_report_failure_returns_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """接続側 (``site_for``) が ``ComkenError`` を投げたら終了コード 1、 ファイルは作らない。

    ``run_fetch`` が ``ComkenError`` を握って ``FetchOutcome(status="failed")``
    に縮退する仕様に合わせて、 例外型を ``ComkenError`` にする。
    """
    from comken.exceptions import ComkenError

    from trial import fetch_one_report

    master = tmp_path / "master.xlsx"
    _build_master(master)
    _write_ini(tmp_path, master)
    _patch_load_settings(monkeypatch, fetch_one_report, tmp_path)

    def failing_site_for(url: str) -> _FakeSite:
        raise ComkenError("未登録組織")

    code = fetch_one_report.main(
        [f"{DOMAIN}/00O5g00000AAAAA/view", "1001"],
        site_for=failing_site_for,
    )

    assert code == 1
    assert not (tmp_path / "output" / "single").exists()


# ── fetch_objects ────────────────────────────────────────────────────────


def _account_describe_full() -> dict:
    """``fetch_objects`` の出力 / キャッシュ検証に使う Account describe。"""
    return {
        "name": "Account",
        "label": "取引先",
        "custom": False,
        "fields": [
            {
                "name": "Name",
                "label": "取引先名",
                "type": "string",
                "custom": False,
                "referenceTo": [],
                "relationshipName": None,
                "picklistValues": [],
            },
            {
                "name": "Phone",
                "label": "電話",
                "type": "phone",
                "custom": False,
                "referenceTo": [],
                "relationshipName": None,
                "picklistValues": [],
            },
            {
                "name": "OwnerId",
                "label": "所有者Id",
                "type": "reference",
                "custom": False,
                "referenceTo": ["User"],
                "relationshipName": "Owner",
                "picklistValues": [],
            },
            {
                "name": "Type",
                "label": "取引先種別",
                "type": "picklist",
                "custom": False,
                "referenceTo": [],
                "relationshipName": None,
                "picklistValues": [
                    {"active": True, "value": "Customer - Direct"},
                    {"active": True, "value": "Customer - Channel"},
                    {"active": False, "value": "Obsolete"},
                ],
            },
        ],
    }


def _user_describe_full() -> dict:
    """``fetch_objects`` の出力 / キャッシュ検証に使う User describe。"""
    return {
        "name": "User",
        "label": "ユーザ",
        "custom": False,
        "fields": [
            {
                "name": "Name",
                "label": "名前",
                "type": "string",
                "custom": False,
                "referenceTo": [],
                "relationshipName": None,
                "picklistValues": [],
            },
        ],
    }


def test_fetch_objects_resolves_single_org_automatically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """管理表に 1 種類だけ組織がある URL なら自動判定で取得できる。"""
    from trial import fetch_objects

    master = tmp_path / "master.xlsx"
    _build_master_with_rows(
        master,
        [
            ("1001", "顧客一覧", f"{DOMAIN}/00O5g00000AAAAA/view", "○"),
            ("1002", "売上実績", f"{DOMAIN}/00O5g00000BBBBB/view", "×"),
        ],
    )
    _write_ini(tmp_path, master)
    _patch_load_settings(monkeypatch, fetch_objects, tmp_path)

    account_describe = _account_describe_full()
    client = _FakeClient(
        describe=_make_metadata(),
        object_describes={"Account": account_describe},
    )

    code = fetch_objects.main(
        object_names=["Account"],
        site_for=_site_for(client),
    )

    assert code == 0
    assert client.describe_object_calls == ["Account"]
    output_csv = tmp_path / "output" / "single" / "オブジェクトの項目.csv"
    assert output_csv.exists()
    captured = capsys.readouterr()
    assert "取得: Account" in captured.out


def test_fetch_objects_multiple_orgs_returns_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """管理表に複数の組織の URL があると、 終了コード 2 でエラーを出す。"""
    from trial import fetch_objects

    master = tmp_path / "master.xlsx"
    _build_master_with_rows(
        master,
        [
            ("1001", "本番", f"{DOMAIN_PROD}/00O5g00000AAAAA/view", "○"),
            ("1002", "Sandbox", f"{DOMAIN}/00O5g00000BBBBB/view", "○"),
        ],
    )
    _write_ini(tmp_path, master)
    _patch_load_settings(monkeypatch, fetch_objects, tmp_path)

    client = _FakeClient(
        describe=_make_metadata(),
        object_describes={"Account": _account_describe_full()},
    )

    code = fetch_objects.main(
        object_names=["Account"],
        site_for=_site_for(client),
    )

    assert code == 2
    assert client.describe_object_calls == []
    captured = capsys.readouterr()
    assert "ORG_URL" in captured.out
    assert not (tmp_path / "output" / "single").exists()


def test_fetch_objects_with_org_url_skips_master_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``org_url`` を指定すると、 管理表に複数組織があってもそちらを優先する。

    Sandbox を URL で明示し、 Account と User を取りに行く。
    """
    from trial import fetch_objects

    master = tmp_path / "master.xlsx"
    _build_master_with_rows(
        master,
        [
            ("1001", "本番", f"{DOMAIN_PROD}/00O5g00000AAAAA/view", "○"),
        ],
    )
    _write_ini(tmp_path, master)
    _patch_load_settings(monkeypatch, fetch_objects, tmp_path)

    client = _FakeClient(
        describe=_make_metadata(),
        object_describes={
            "Account": _account_describe_full(),
            "User": _user_describe_full(),
        },
    )

    code = fetch_objects.main(
        object_names=["Account", "User"],
        org_url=f"{DOMAIN}/00O5g00000CCCCC/view",
        site_for=_site_for(client),
    )

    assert code == 0
    assert client.describe_object_calls == ["Account", "User"]
    captured = capsys.readouterr()
    assert "取得: Account" in captured.out
    assert "取得: User" in captured.out


def test_fetch_objects_writes_to_describe_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """取得した describe は ``[FILES] DESCRIBE_CSV_PATH`` に書き込まれ、 次の
    ``DescribeStore.load().lookup`` で読める。"""
    from src.describe_store import DescribeStore
    from trial import fetch_objects

    master = tmp_path / "master.xlsx"
    _build_master_with_rows(
        master,
        [
            ("1001", "顧客一覧", f"{DOMAIN}/00O5g00000AAAAA/view", "○"),
        ],
    )
    _write_ini(tmp_path, master)
    _patch_load_settings(monkeypatch, fetch_objects, tmp_path)

    account_describe = _account_describe_full()
    client = _FakeClient(
        describe=_make_metadata(),
        object_describes={"Account": account_describe},
    )

    code = fetch_objects.main(
        object_names=["Account"],
        site_for=_site_for(client),
    )

    assert code == 0
    describe_csv = tmp_path / "data" / "項目定義.csv"
    assert describe_csv.exists()

    store = DescribeStore.load(describe_csv)
    cached = store.lookup("example--sandbox.sandbox.my.salesforce.com", "Account")
    assert cached is not None
    assert cached.get("source") == "csv"
    describe = cached.get("describe")
    assert isinstance(describe, dict)
    assert describe.get("name") == "Account"
    fields = describe.get("fields")
    assert isinstance(fields, list)
    assert len(fields) == 4


def test_fetch_objects_refreshes_existing_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """キャッシュにすでに値があっても、 API を 1 回は必ず呼び、 値を上書きする。

    壊す系 （ ``record_api_success`` を呼ばない ） にするとこのテストが落ちる
    ことを、 実装を書くときに一度確かめる。
    """
    from src.describe_store import DescribeStore, upsert_object_rows
    from trial import fetch_objects

    master = tmp_path / "master.xlsx"
    _build_master_with_rows(
        master,
        [
            ("1001", "顧客一覧", f"{DOMAIN}/00O5g00000AAAAA/view", "○"),
        ],
    )
    _write_ini(tmp_path, master)
    _patch_load_settings(monkeypatch, fetch_objects, tmp_path)

    # キャッシュに 「 古い Account 」 を入れておく （ 1 項目だけ ）
    old_describe = {
        "name": "Account",
        "label": "古い取引先",
        "custom": False,
        "fields": [{"name": "OldField", "label": "旧項目", "type": "string"}],
    }
    describe_csv = tmp_path / "data" / "項目定義.csv"
    describe_csv.parent.mkdir(parents=True, exist_ok=True)
    upsert_object_rows(
        describe_csv,
        "example--sandbox.sandbox.my.salesforce.com",
        "Account",
        old_describe,
        fetched_at="2020-01-01T00:00:00+00:00",
    )

    # 新しく fetch_objects を実行
    new_describe = _account_describe_full()
    client = _FakeClient(
        describe=_make_metadata(),
        object_describes={"Account": new_describe},
    )

    code = fetch_objects.main(
        object_names=["Account"],
        site_for=_site_for(client),
    )

    assert code == 0
    # API は呼ばれた （ キャッシュを読まず ）
    assert client.describe_object_calls == ["Account"]

    # キャッシュが新しい値で上書きされている
    store = DescribeStore.load(describe_csv)
    cached = store.lookup("example--sandbox.sandbox.my.salesforce.com", "Account")
    assert cached is not None
    describe = cached.get("describe")
    assert isinstance(describe, dict)
    fields = describe.get("fields")
    assert isinstance(fields, list)
    # 古い項目は消え、 新しい 4 項目になっている
    field_names = [field.get("name") for field in fields if isinstance(field, dict)]
    assert "OldField" not in field_names
    # ``DescribeStore.upsert_object_rows`` は API 名で安定ソートするので、
    # 新しい 4 項目は API 名の昇順で並ぶ
    assert field_names == ["Name", "OwnerId", "Phone", "Type"]


def test_fetch_objects_partial_failure_returns_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """1 件失敗（ ``ValueError`` ） しても他は取り、 終了コードは 1。"""
    from trial import fetch_objects

    master = tmp_path / "master.xlsx"
    _build_master_with_rows(
        master,
        [
            ("1001", "顧客一覧", f"{DOMAIN}/00O5g00000AAAAA/view", "○"),
        ],
    )
    _write_ini(tmp_path, master)
    _patch_load_settings(monkeypatch, fetch_objects, tmp_path)

    class _FakeClientWithError(_FakeClient):
        def __init__(
            self,
            describe: dict,
            object_describes: dict[str, dict] | None = None,
            errors: dict[str, BaseException] | None = None,
        ) -> None:
            super().__init__(describe, object_describes)
            self._errors = errors or {}

        def describe_object(self, name: str) -> dict:
            self.describe_object_calls.append(name)
            if name in self._errors:
                raise self._errors[name]
            if name in self._object_describes:
                return self._object_describes[name]
            return {"name": name, "fields": []}

    client = _FakeClientWithError(
        describe=_make_metadata(),
        object_describes={"User": _user_describe_full()},
        errors={"Account": ValueError("存在しない")},
    )

    code = fetch_objects.main(
        object_names=["Account", "User"],
        site_for=_site_for(client),
    )

    assert code == 1
    assert client.describe_object_calls == ["Account", "User"]
    captured = capsys.readouterr()
    assert "取得できませんでした: Account" in captured.out
    assert "取得: User" in captured.out

    # User は CSV に書かれている
    output_csv = tmp_path / "output" / "single" / "オブジェクトの項目.csv"
    assert output_csv.exists()


def test_fetch_objects_empty_object_names_returns_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``OBJECT_NAMES`` が空のときはエラー 2。"""
    from trial import fetch_objects

    master = tmp_path / "master.xlsx"
    _build_master(master)
    _write_ini(tmp_path, master)
    _patch_load_settings(monkeypatch, fetch_objects, tmp_path)

    client = _FakeClient(
        describe=_make_metadata(),
        object_describes={"Account": _account_describe_full()},
    )

    code = fetch_objects.main(
        object_names=[],
        site_for=_site_for(client),
    )

    assert code == 2
    assert client.describe_object_calls == []
    captured = capsys.readouterr()
    assert "OBJECT_NAMES" in captured.out
    assert not (tmp_path / "output" / "single").exists()


def test_fetch_objects_output_csv_columns_and_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """出力 CSV の列順・行の中身が仕様どおり （ 9 列、 参照 / picklist / カスタム ）。"""
    import csv

    from trial import fetch_objects

    master = tmp_path / "master.xlsx"
    _build_master_with_rows(
        master,
        [
            ("1001", "顧客一覧", f"{DOMAIN}/00O5g00000AAAAA/view", "○"),
        ],
    )
    _write_ini(tmp_path, master)
    _patch_load_settings(monkeypatch, fetch_objects, tmp_path)

    client = _FakeClient(
        describe=_make_metadata(),
        object_describes={"Account": _account_describe_full()},
    )

    code = fetch_objects.main(
        object_names=["Account"],
        site_for=_site_for(client),
    )

    assert code == 0
    output_csv = tmp_path / "output" / "single" / "オブジェクトの項目.csv"
    assert output_csv.exists()

    with output_csv.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        columns = next(reader)
        rows = [dict(zip(columns, line, strict=True)) for line in reader]

    expected_columns = [
        "オブジェクト",
        "オブジェクト表示名",
        "項目API名",
        "項目表示名",
        "型",
        "参照先オブジェクト",
        "リレーション名",
        "カスタム",
        "選択肢",
    ]
    assert columns == expected_columns
    assert len(rows) == 4  # Account の 4 項目

    by_api_name = {r["項目API名"]: r for r in rows}
    assert by_api_name["Name"]["オブジェクト"] == "Account"
    assert by_api_name["Name"]["オブジェクト表示名"] == "取引先"
    assert by_api_name["Name"]["型"] == "string"
    assert by_api_name["Name"]["参照先オブジェクト"] == ""
    assert by_api_name["Name"]["カスタム"] == ""

    owner_row = by_api_name["OwnerId"]
    assert owner_row["参照先オブジェクト"] == "User"
    assert owner_row["リレーション名"] == "Owner"

    type_row = by_api_name["Type"]
    # active な値だけが ``|`` 区切りで並ぶ
    assert type_row["選択肢"] == "Customer - Direct|Customer - Channel"

    phone_row = by_api_name["Phone"]
    assert phone_row["型"] == "phone"


def test_fetch_objects_broken_no_cache_write_fails_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``record_api_success`` を呼ばないように ``fetch_objects`` を壊すと、
    「 キャッシュに入ること 」 のテストが落ちることを確かめる （ 戻す前に確認用 ）。

    このテスト自体は壊した実装がどう振る舞うかを見るためのもので、 修正後は
    ``monkeypatch.setattr`` ではなく普通の ``fetch_objects.main`` を呼ぶ。
    落ちないことを確認したら、 壊した実装は元に戻し、 このテストは削除する。
    """
    from src.describe_store import DescribeStore
    from trial import fetch_objects

    master = tmp_path / "master.xlsx"
    _build_master_with_rows(
        master,
        [
            ("1001", "顧客一覧", f"{DOMAIN}/00O5g00000AAAAA/view", "○"),
        ],
    )
    _write_ini(tmp_path, master)
    _patch_load_settings(monkeypatch, fetch_objects, tmp_path)

    # ``record_api_success`` を何もしない no-op に差し替えて壊す
    def _noop(self: DescribeStore, org: str, object_name: str, describe: dict) -> None:
        return None

    monkeypatch.setattr(DescribeStore, "record_api_success", _noop)

    client = _FakeClient(
        describe=_make_metadata(),
        object_describes={"Account": _account_describe_full()},
    )

    code = fetch_objects.main(
        object_names=["Account"],
        site_for=_site_for(client),
    )

    # API 呼び出し自体は成功扱いになる （ exit code は 0 ）
    assert code == 0

    # ただしキャッシュには何も書かれていない
    describe_csv = tmp_path / "data" / "項目定義.csv"
    if describe_csv.exists():
        store = DescribeStore.load(describe_csv)
        cached = store.lookup("example--sandbox.sandbox.my.salesforce.com", "Account")
        assert cached is None, "no-op にしたらキャッシュは空のはず"
