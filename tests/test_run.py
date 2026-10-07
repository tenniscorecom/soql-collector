"""src/run.py と main.py の薄い配線テスト（終了コードを捨てないことの確認）。
``run`` 本体のテストもここで行う。
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from comken.exceptions import SalesforceRequestError
from openpyxl import Workbook
from openpyxl.worksheet.table import Table as XlsxTable
from openpyxl.worksheet.table import TableStyleInfo

DOMAIN = "https://example--sandbox.sandbox.my.salesforce.com/lightning/r/Report"

# ── ヘルパー ─────────────────────────────────────────────────────────────


def _build_master(path: Path, rows: list[dict]) -> None:
    """テスト用の管理表 xlsx を作る。"""
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
    table = XlsxTable(displayName="PY_T_ReportEntry", ref=f"A1:I{len(rows) + 1}")
    table.tableStyleInfo = TableStyleInfo(
        name="TableStyleMedium2",
        showFirstColumn=False,
        showLastColumn=False,
        showRowStripes=True,
        showColumnStripes=False,
    )
    sheet.add_table(table)
    workbook.save(path)


def _write_ini(tmp_path: Path, master_xlsx: Path, *, objects_section: str = "") -> None:
    """テスト用 ``config.ini`` を書く。 ``[OBJECTS]`` セクションは追加で。"""
    ini = tmp_path / "config.ini"
    ini.write_text(
        f"[FILES]\nMASTER_XLSX_PATH = {master_xlsx}\nOUTPUT_DIR = ./output\n",
        encoding="utf-8",
    )
    if objects_section:
        with ini.open("a", encoding="utf-8") as handle:
            handle.write(objects_section)


def _make_settings(
    tmp_path: Path, master: Path, names: tuple[str, ...] = (), org_id: str | None = None
):
    """``Settings`` 互換（ ``run`` の中の ``load_settings`` を差し替え）。"""
    from src.settings import Settings

    return Settings(
        master_xlsx_path=master,
        output_dir=tmp_path / "output",
        related_max=40,
        credential_prefix="",
        objects_names=names,
        objects_org_id=org_id,
    )


def _patch_load_settings(monkeypatch: pytest.MonkeyPatch, settings: Any) -> None:
    """``src.run.load_settings`` を差し替え。"""
    import src.run as run_module

    monkeypatch.setattr(run_module, "load_settings", lambda: settings)


# ── 偽物のクライアント ─────────────────────────────────────────────────


class _ReportStub:
    def __init__(self, describe: dict) -> None:
        self._describe = describe

    def describe(self, report_id: str) -> dict:
        return self._describe


class _FakeClient:
    """Fake Salesforce クライアント（ ``with site() as client`` の中身 ）。"""

    def __init__(
        self,
        describe: dict,
        object_describes: dict[str, dict] | None = None,
        errors: dict[str, BaseException] | None = None,
    ) -> None:
        self.report = _ReportStub(describe)
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
    """Fake の ``SalesforceBase`` サブクラス。

    ``site_for(url)`` でインスタンス（ 実質 ``class`` のようなもの ） を返し、
    ``site_class()`` で ``__call__`` が ``self`` を返し、 ``__enter__`` で
    ``_FakeClient`` を返す形にする。 ``__name__`` は本物のクラスと同じく
    文字列なので、 組織クラス名の一覧表示にも使える。

    複数組織テストでは ``_SiteA`` / ``_SiteB`` など ``__name__`` だけ違う
    サブクラスを ``site_for(url)`` から返す。
    """

    __name__ = "_FakeSite"  # type: ignore[assignment]
    open_count = 0

    def __init__(self, client: _FakeClient) -> None:
        self._client = client

    def __call__(self) -> _FakeSite:
        return self

    def __enter__(self) -> _FakeClient:
        type(self).open_count += 1
        return self._client

    def __exit__(self, *args: object) -> None:
        return None


class _SiteA(_FakeSite):
    __name__ = "_SiteA"  # type: ignore[assignment]

    def __enter__(self) -> _FakeClient:  # type: ignore[override]
        raise AssertionError("should not open")


class _SiteB(_FakeSite):
    __name__ = "_SiteB"  # type: ignore[assignment]

    def __enter__(self) -> _FakeClient:  # type: ignore[override]
        raise AssertionError("should not open")


def _make_metadata(
    report_type: str = "Opportunity",
    *,
    detail_columns: list[str] | None = None,
    detail_column_labels: dict[str, str] | None = None,
) -> dict:
    """テスト用の ``client.report.describe()`` 戻り値相当を作る。"""
    detail_columns = detail_columns or ["Opp.Name"]
    labels = detail_column_labels or {}
    return {
        "reportMetadata": {
            "reportType": {"type": report_type},
            "reportFormat": "TABULAR",
            "detailColumns": detail_columns,
        },
        "reportExtendedMetadata": {
            "detailColumnInfo": {
                column_key: {"label": labels.get(column_key, column_key)}
                for column_key in detail_columns
            }
        },
    }


# ── 引数チェック ────────────────────────────────────────────────────────


def test_run_with_argv_returns_2_and_skips_site_for(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """引数が 1 つでもあれば ERROR（ 終了コード 2 ） を出して ``site_for`` を呼ばない。"""
    from src import run as run_module

    called = {"site_for": 0}
    master = tmp_path / "master.xlsx"
    _build_master(
        master,
        [
            {"ID": "1001", "Salesforce URL": f"{DOMAIN}/00O5g00000ABCDE/view", "有効": "○"},
        ],
    )
    _write_ini(tmp_path, master)
    _patch_load_settings(monkeypatch, _make_settings(tmp_path, master))

    def site_for(url: str):
        called["site_for"] += 1
        raise AssertionError("site_for should not be called")

    code = run_module.run(["fetch"], site_for=site_for)

    assert code == 2
    assert called["site_for"] == 0


def test_run_with_none_argv_uses_sys_argv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``argv=None`` のときは ``sys.argv[1:]`` を見る。"""
    from src import run as run_module

    master = tmp_path / "master.xlsx"
    _build_master(master, [])
    _write_ini(tmp_path, master)
    _patch_load_settings(monkeypatch, _make_settings(tmp_path, master))

    monkeypatch.setattr(sys, "argv", ["main.py", "--help"])

    assert run_module.run(None, site_for=lambda _u: (_ for _ in ()).throw(AssertionError())) == 2


# ── 「有効」の絞り込み ─────────────────────────────────────────────────


def test_run_filters_disabled_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """「有効」が ○ の行だけが取られ、 × の行は取られない。"""
    from src import run as run_module
    from src.fetch import FetchOutcome

    master = tmp_path / "master.xlsx"
    _build_master(
        master,
        [
            {"ID": "1001", "Salesforce URL": f"{DOMAIN}/00O5g00000AAAAA/view", "有効": "○"},
            {"ID": "1002", "Salesforce URL": f"{DOMAIN}/00O5g00000BBBBB/view", "有効": "×"},
            {"ID": "1003", "Salesforce URL": f"{DOMAIN}/00O5g00000CCCCC/view", "有効": "○"},
        ],
    )
    _write_ini(tmp_path, master)
    _patch_load_settings(monkeypatch, _make_settings(tmp_path, master))

    captured_keys: list[str] = []

    def fake_run_fetch(settings, entries, *, dry_run=False, site_for=None, object_cache=None):
        captured_keys.extend(entry.key for entry in entries)
        return [
            FetchOutcome(
                entry=entry,
                status="ok",
                output_path=settings.output_dir / f"{entry.key}.json",
                warnings=(),
                error=None,
            )
            for entry in entries
        ]

    monkeypatch.setattr(run_module, "run_fetch", fake_run_fetch)
    monkeypatch.setattr(
        run_module,
        "run_tables",
        lambda *args, **kwargs: None,
    )

    code = run_module.run([], site_for=lambda _u: (_ for _ in ()).throw(AssertionError()))

    assert code == 0
    assert captured_keys == ["1001", "1003"]


def test_run_no_enabled_rows_returns_2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """全部 × で ``NAMES`` も空なら終了コード 2、 ``site_for`` も呼ばない。"""
    from src import run as run_module

    master = tmp_path / "master.xlsx"
    _build_master(
        master,
        [
            {"ID": "1001", "Salesforce URL": f"{DOMAIN}/00O5g00000AAAAA/view", "有効": "×"},
        ],
    )
    _write_ini(tmp_path, master)
    _patch_load_settings(monkeypatch, _make_settings(tmp_path, master))

    called = {"site_for": 0}

    def site_for(url: str):
        called["site_for"] += 1
        raise AssertionError("site_for should not be called")

    code = run_module.run([], site_for=site_for)

    assert code == 2
    assert called["site_for"] == 0


# ── 取得成功／失敗 ──────────────────────────────────────────────────────


def test_run_writes_jsons_and_csvs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """有効な行が複数あれば全部取られ、 JSON と CSV ができる。"""
    from src import run as run_module
    from src.fetch import FetchOutcome

    master = tmp_path / "master.xlsx"
    _build_master(
        master,
        [
            {"ID": "1001", "Salesforce URL": f"{DOMAIN}/00O5g00000AAAAA/view", "有効": "○"},
            {"ID": "1002", "Salesforce URL": f"{DOMAIN}/00O5g00000BBBBB/view", "有効": "○"},
        ],
    )
    _write_ini(tmp_path, master)
    _patch_load_settings(monkeypatch, _make_settings(tmp_path, master))

    def fake_run_fetch(settings, entries, *, dry_run=False, site_for=None, object_cache=None):
        outcomes = []
        for entry in entries:
            output_path = settings.output_dir / f"{entry.key}.json"
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text('{"ok": true}', encoding="utf-8")
            outcomes.append(
                FetchOutcome(
                    entry=entry,
                    status="ok",
                    output_path=output_path,
                    warnings=(),
                    error=None,
                )
            )
        return outcomes

    tables_called: list[dict] = []

    def fake_run_tables(output_dir, *, only_keys=None):
        tables_called.append({"only_keys": list(only_keys) if only_keys is not None else None})
        return None

    monkeypatch.setattr(run_module, "run_fetch", fake_run_fetch)
    monkeypatch.setattr(run_module, "run_tables", fake_run_tables)

    code = run_module.run([], site_for=lambda _u: (_ for _ in ()).throw(AssertionError()))

    assert code == 0
    assert (tmp_path / "output" / "1001.json").exists()
    assert (tmp_path / "output" / "1002.json").exists()
    assert tables_called == [{"only_keys": ["1001", "1002"]}]


def test_run_partial_failure_continues_and_returns_1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """1 件失敗しても残りは続き、 終了コード 1。 失敗した ID の既存 CSV は消えない。"""
    from src import run as run_module
    from src.fetch import FetchOutcome

    master = tmp_path / "master.xlsx"
    _build_master(
        master,
        [
            {"ID": "1001", "Salesforce URL": f"{DOMAIN}/00O5g00000AAAAA/view", "有効": "○"},
            {"ID": "1002", "Salesforce URL": f"{DOMAIN}/00O5g00000BBBBB/view", "有効": "○"},
        ],
    )
    _write_ini(tmp_path, master)
    settings = _make_settings(tmp_path, master)
    _patch_load_settings(monkeypatch, settings)

    # 失敗 ID の既存 CSV を残しておく
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    existing_csv = settings.output_dir / "対応表_1002.csv"
    existing_csv.write_text("既存\nそのまま\n", encoding="utf-8")

    def fake_run_fetch(settings, entries, *, dry_run=False, site_for=None, object_cache=None):
        return [
            FetchOutcome(
                entry=entries[0],
                status="ok",
                output_path=settings.output_dir / "1001.json",
                warnings=(),
                error=None,
            ),
            FetchOutcome(
                entry=entries[1],
                status="failed",
                output_path=None,
                warnings=(),
                error="URL からレポート ID を取り出せません",
            ),
        ]

    captured: dict[str, Any] = {}

    def fake_run_tables(output_dir, *, only_keys=None):
        captured["only_keys"] = list(only_keys) if only_keys is not None else None
        return None

    monkeypatch.setattr(run_module, "run_fetch", fake_run_fetch)
    monkeypatch.setattr(run_module, "run_tables", fake_run_tables)

    code = run_module.run([], site_for=lambda _u: (_ for _ in ()).throw(AssertionError()))

    assert code == 1
    assert captured["only_keys"] == ["1001"]
    assert existing_csv.read_text(encoding="utf-8") == "既存\nそのまま\n"


# ── dry-run ────────────────────────────────────────────────────────────


def test_run_dry_run_skips_site_for_and_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``comken.runtime.is_dry_run`` が真のとき、 ``site_for`` は呼ばれず、
    何も書かれず、 終了コード 0。
    """
    from comken import runtime
    from comken.runtime import dry_run

    from src import run as run_module

    master = tmp_path / "master.xlsx"
    _build_master(
        master,
        [
            {"ID": "1001", "Salesforce URL": f"{DOMAIN}/00O5g00000AAAAA/view", "有効": "○"},
        ],
    )
    _write_ini(tmp_path, master)
    settings = _make_settings(tmp_path, master, names=("Account",))
    _patch_load_settings(monkeypatch, settings)

    called = {"fetch": 0, "tables": 0}

    def fake_run_fetch(*args, **kwargs):
        called["fetch"] += 1
        return []

    def fake_run_tables(*args, **kwargs):
        called["tables"] += 1
        return None

    monkeypatch.setattr(run_module, "run_fetch", fake_run_fetch)
    monkeypatch.setattr(run_module, "run_tables", fake_run_tables)

    with dry_run():
        code = run_module.run(
            [], site_for=lambda _u: (_ for _ in ()).throw(AssertionError("site_for"))
        )

    assert code == 0
    assert called["fetch"] == 0
    assert called["tables"] == 0
    # 何も書かれていない
    assert not (tmp_path / "output").exists() or not list((tmp_path / "output").iterdir())
    # 後始末（ dry-run を明示解除 ）
    assert not runtime.is_dry_run()


# ── オブジェクト ────────────────────────────────────────────────────────


def test_objects_single_org_resolves_automatically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """管理表の URL が 1 種類だけなら自動で決まる。"""
    from src import run as run_module

    master = tmp_path / "master.xlsx"
    _build_master(
        master,
        [
            {"ID": "1001", "Salesforce URL": f"{DOMAIN}/00O5g00000AAAAA/view", "有効": "○"},
            {"ID": "1002", "Salesforce URL": f"{DOMAIN}/00O5g00000BBBBB/view", "有効": "×"},
        ],
    )
    _write_ini(tmp_path, master, objects_section="[OBJECTS]\nNAMES = Account\n")
    settings = _make_settings(tmp_path, master, names=("Account",))
    _patch_load_settings(monkeypatch, settings)

    client = _FakeClient(
        describe=_make_metadata(),
        object_describes={"Account": {"name": "Account", "fields": []}},
    )

    def site_for(url: str):
        return _FakeSite(client)

    monkeypatch.setattr(run_module, "run_fetch", lambda *a, **kw: [])
    monkeypatch.setattr(run_module, "run_tables", lambda *a, **kw: None)

    code = run_module.run([], site_for=site_for)

    assert code == 0
    # Account.json が書かれている
    account_json = tmp_path / "output" / "objects" / "Account.json"
    assert account_json.exists()
    payload = json.loads(account_json.read_text(encoding="utf-8"))
    assert payload["オブジェクト"] == "Account"
    assert "object" in payload
    assert payload["object"]["name"] == "Account"


def test_objects_multiple_orgs_returns_1_without_org_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """組織が 2 種類で ``ORG_ID`` が無いと、 どのオブジェクトも取らず 終了コード 1
    （ エラー文に組織クラス名の一覧と ``ORG_ID`` の案内 ）。
    """
    from src import run as run_module

    master = tmp_path / "master.xlsx"
    _build_master(
        master,
        [
            {"ID": "1001", "Salesforce URL": f"{DOMAIN}/00O5g00000AAAAA/view", "有効": "○"},
            {
                "ID": "1002",
                "Salesforce URL": "https://other.example.com/lightning/r/Report/00O5g00000BBBBB/view",
                "有効": "×",
            },
        ],
    )
    _write_ini(tmp_path, master, objects_section="[OBJECTS]\nNAMES = Account\n")
    settings = _make_settings(tmp_path, master, names=("Account",))
    _patch_load_settings(monkeypatch, settings)

    by_org = {
        "example--sandbox.sandbox.my.salesforce.com": _SiteA,
        "other.example.com": _SiteB,
    }

    def site_for(url: str):
        from urllib.parse import urlsplit

        host = urlsplit(url).netloc.lower()
        for known, cls in by_org.items():
            if known in host:
                return cls(None)
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(run_module, "run_tables", lambda *a, **kw: None)
    monkeypatch.setattr(run_module, "run_fetch", lambda *a, **kw: [])

    code = run_module.run([], site_for=site_for)

    assert code == 1
    # オブジェクト JSON が無い
    assert not (tmp_path / "output" / "objects").exists() or not list(
        (tmp_path / "output" / "objects").iterdir()
    )


def test_objects_with_org_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ORG_ID`` で組織の URL を選ぶ。"""
    from src import run as run_module

    master = tmp_path / "master.xlsx"
    _build_master(
        master,
        [
            {"ID": "1001", "Salesforce URL": f"{DOMAIN}/00O5g00000AAAAA/view", "有効": "○"},
        ],
    )
    _write_ini(tmp_path, master, objects_section="[OBJECTS]\nNAMES = Account\nORG_ID = 1001\n")
    settings = _make_settings(tmp_path, master, names=("Account",), org_id="1001")
    _patch_load_settings(monkeypatch, settings)

    client = _FakeClient(
        describe=_make_metadata(),
        object_describes={"Account": {"name": "Account", "fields": []}},
    )
    monkeypatch.setattr(run_module, "run_tables", lambda *a, **kw: None)
    monkeypatch.setattr(run_module, "run_fetch", lambda *a, **kw: [])

    code = run_module.run([], site_for=lambda _u: _FakeSite(client))

    assert code == 0
    assert (tmp_path / "output" / "objects" / "Account.json").exists()


def test_objects_unknown_org_id_returns_1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """管理表に無い ``ORG_ID`` はエラー。"""
    from src import run as run_module

    master = tmp_path / "master.xlsx"
    _build_master(
        master,
        [
            {"ID": "1001", "Salesforce URL": f"{DOMAIN}/00O5g00000AAAAA/view", "有効": "○"},
        ],
    )
    _write_ini(tmp_path, master, objects_section="[OBJECTS]\nNAMES = Account\nORG_ID = 9999\n")
    settings = _make_settings(tmp_path, master, names=("Account",), org_id="9999")
    _patch_load_settings(monkeypatch, settings)
    monkeypatch.setattr(run_module, "run_tables", lambda *a, **kw: None)
    monkeypatch.setattr(run_module, "run_fetch", lambda *a, **kw: [])

    called = {"site_for": 0}

    def site_for(url: str):
        called["site_for"] += 1
        raise AssertionError("site_for should not be called")

    code = run_module.run([], site_for=site_for)

    assert code == 1
    assert called["site_for"] == 0


def test_object_http_error_isolated_to_one_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ある名前の HTTP エラー（ 401/403 以外 ） はその名前だけ失敗で残りは取れる。"""
    from src import run as run_module

    master = tmp_path / "master.xlsx"
    _build_master(
        master,
        [
            {"ID": "1001", "Salesforce URL": f"{DOMAIN}/00O5g00000AAAAA/view", "有効": "×"},
        ],
    )
    _write_ini(tmp_path, master, objects_section="[OBJECTS]\nNAMES = BadOne, GoodOne\n")
    settings = _make_settings(tmp_path, master, names=("BadOne", "GoodOne"))
    _patch_load_settings(monkeypatch, settings)

    client = _FakeClient(
        describe=_make_metadata(),
        object_describes={"GoodOne": {"name": "GoodOne", "fields": []}},
        errors={
            "BadOne": SalesforceRequestError("GET", "/sobjects/BadOne/describe", 404, "Not Found"),
        },
    )

    monkeypatch.setattr(run_module, "run_tables", lambda *a, **kw: None)
    monkeypatch.setattr(run_module, "run_fetch", lambda *a, **kw: [])

    code = run_module.run([], site_for=lambda _u: _FakeSite(client))

    assert code == 1
    # GoodOne は取れている、 BadOne は無い
    assert (tmp_path / "output" / "objects" / "GoodOne.json").exists()
    assert not (tmp_path / "output" / "objects" / "BadOne.json").exists()


def test_object_401_stops_remaining_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ある名前の 401 は残りの名前を取らない。"""
    from src import run as run_module

    master = tmp_path / "master.xlsx"
    _build_master(
        master,
        [
            {"ID": "1001", "Salesforce URL": f"{DOMAIN}/00O5g00000AAAAA/view", "有効": "×"},
        ],
    )
    _write_ini(tmp_path, master, objects_section="[OBJECTS]\nNAMES = BadOne, GoodOne\n")
    settings = _make_settings(tmp_path, master, names=("BadOne", "GoodOne"))
    _patch_load_settings(monkeypatch, settings)

    client = _FakeClient(
        describe=_make_metadata(),
        object_describes={"GoodOne": {"name": "GoodOne", "fields": []}},
        errors={
            "BadOne": SalesforceRequestError("GET", "/sobjects/BadOne/describe", 401, "Auth"),
        },
    )

    monkeypatch.setattr(run_module, "run_tables", lambda *a, **kw: None)
    monkeypatch.setattr(run_module, "run_fetch", lambda *a, **kw: [])

    code = run_module.run([], site_for=lambda _u: _FakeSite(client))

    assert code == 1
    # BadOne は JSON を書かないうちに上位で握りつぶされ、 GoodOne も取られない
    assert not (tmp_path / "output" / "objects" / "BadOne.json").exists()
    assert not (tmp_path / "output" / "objects" / "GoodOne.json").exists()


def test_object_invalid_name_isolated_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """不正な名前（ ``$`` など ） は ``ValueError`` でその名前だけ失敗。"""

    class _StrictClient(_FakeClient):
        def describe_object(self, name: str) -> dict:
            self.describe_object_calls.append(name)
            if "$" in name:
                raise ValueError(f"無効なオブジェクト名です: {name!r}")
            return {"name": name, "fields": []}

    from src import run as run_module

    master = tmp_path / "master.xlsx"
    _build_master(
        master,
        [
            {"ID": "1001", "Salesforce URL": f"{DOMAIN}/00O5g00000AAAAA/view", "有効": "×"},
        ],
    )
    _write_ini(
        tmp_path,
        master,
        objects_section="[OBJECTS]\nNAMES = Bad$One, GoodOne\n",
    )
    settings = _make_settings(tmp_path, master, names=("Bad$One", "GoodOne"))
    _patch_load_settings(monkeypatch, settings)

    monkeypatch.setattr(run_module, "run_tables", lambda *a, **kw: None)
    monkeypatch.setattr(run_module, "run_fetch", lambda *a, **kw: [])

    code = run_module.run([], site_for=lambda _u: _FakeSite(_StrictClient(None, None)))

    assert code == 1
    # Bad$One は失敗、 GoodOne は取れている
    assert (tmp_path / "output" / "objects" / "GoodOne.json").exists()
    assert not (tmp_path / "output" / "objects" / "Bad$One.json").exists()


def test_object_cache_shared_with_report_fetch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同じオブジェクトがレポートの取得で取られていたら ``describe_object`` は 1 回だけ。"""
    from src import run as run_module

    master = tmp_path / "master.xlsx"
    _build_master(
        master,
        [
            {"ID": "1001", "Salesforce URL": f"{DOMAIN}/00O5g00000AAAAA/view", "有効": "○"},
        ],
    )
    _write_ini(
        tmp_path,
        master,
        objects_section="[OBJECTS]\nNAMES = Account\n",
    )
    settings = _make_settings(tmp_path, master, names=("Account",))
    _patch_load_settings(monkeypatch, settings)

    # Account がレポートの主オブジェクトとして取られる設定
    main_describe = {"name": "Opportunity", "fields": []}
    account_describe = {"name": "Account", "fields": []}
    client = _FakeClient(
        describe=_make_metadata("Opportunity"),
        object_describes={"Opportunity": main_describe, "Account": account_describe},
    )

    # run_fetch に ``Account`` をすでにキャッシュ済みにする偽実装
    def fake_run_fetch(settings, entries, *, dry_run=False, site_for=None, object_cache=None):
        # Account はキャッシュ済みにして fetch 経路では describe_object を呼ばない
        object_cache["Account"] = {"describe": account_describe, "warning": None}
        return []

    monkeypatch.setattr(run_module, "run_fetch", fake_run_fetch)
    monkeypatch.setattr(run_module, "run_tables", lambda *a, **kw: None)

    code = run_module.run([], site_for=lambda _u: _FakeSite(client))

    assert code == 0
    # レポート経路では Account の describe_object は呼ばれていない （ 1 回 ）
    assert client.describe_object_calls == []
    # Account.json は作成されている
    assert (tmp_path / "output" / "objects" / "Account.json").exists()


# ── main.py ─────────────────────────────────────────────────────────────


def _reload_main(monkeypatch: pytest.MonkeyPatch) -> Any:
    """``main.py`` を再ロードする。"""
    project_root = Path(__file__).resolve().parent.parent
    sys.modules.pop("main", None)
    if not getattr(sys.modules.get("comken"), "__file__", None):
        for key in list(sys.modules):
            if key == "comken" or key.startswith("comken."):
                sys.modules.pop(key, None)
        comken_root = project_root.parent / "comken"
        monkeypatch.syspath_prepend(str(comken_root))
        import comken  # noqa: F401
    monkeypatch.syspath_prepend(str(project_root))
    return importlib.import_module("main")


def test_main_raises_systemexit_when_run_returns_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``main()`` は ``run()`` が 1 を返したとき ``SystemExit(1)`` を送出する。"""
    main = _reload_main(monkeypatch)

    monkeypatch.setattr(main, "run", lambda: 1)

    with pytest.raises(SystemExit) as exc:
        main.main()
    assert exc.value.code == 1


def test_main_does_not_raise_when_run_returns_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``main()`` は ``run()`` が 0 を返したとき何も起きない。"""
    main = _reload_main(monkeypatch)

    monkeypatch.setattr(main, "run", lambda: 0)

    main.main()


def test_main_propagates_exit_code_two(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``main()`` は ``run()`` が 2 を返したとき ``SystemExit(2)`` を送出する。"""
    main = _reload_main(monkeypatch)

    monkeypatch.setattr(main, "run", lambda: 2)

    with pytest.raises(SystemExit) as exc:
        main.main()
    assert exc.value.code == 2
