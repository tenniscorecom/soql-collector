"""src/run.py と main.py の薄い配線テスト（終了コードを捨てないことの確認）。
``run`` 本体のテストもここで行う。
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from openpyxl import Workbook
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table as XlsxTable
from openpyxl.worksheet.table import TableStyleInfo

if TYPE_CHECKING:
    from src.fetch import ReportRecord

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
    # 「調査」列は任意。 行のどれかが「調査」を持つときだけ、 列を足す
    if any("調査" in row for row in rows):
        headers.append("調査")
    sheet.append(headers)
    for row in rows:
        sheet.append([row.get(col, "") for col in headers])
    table = XlsxTable(
        displayName="PY_T_ReportEntry", ref=f"A1:{get_column_letter(len(headers))}{len(rows) + 1}"
    )
    table.tableStyleInfo = TableStyleInfo(
        name="TableStyleMedium2",
        showFirstColumn=False,
        showLastColumn=False,
        showRowStripes=True,
        showColumnStripes=False,
    )
    sheet.add_table(table)
    workbook.save(path)


def _write_ini(tmp_path: Path, master_xlsx: Path, *, extra: str = "") -> None:
    """テスト用 ``config.ini`` を書く。 ``[FILES]`` の 7 つのキーはすべて必須
    なので、 このヘルパでは全部書く。 追加でセクションを足したいときは
    ``extra`` に ``"[OBJECTS]\\nNAMES = ..."`` のように渡す。 ``[OBJECTS]``
    が残っていても無視されることを確認するための経路として残しておく。
    """
    ini = tmp_path / "config.ini"
    ini.write_text(
        "[FILES]\n"
        f"MASTER_XLSX_PATH = {master_xlsx}\n"
        "OUTPUT_DIR = ./output\n"
        "DESCRIBE_CSV_PATH = ./data/項目定義.csv\n"
        "COLUMN_FIX_XLSX_PATH = ./input/列の直し.xlsx\n"
        "XLSX_NAME = SOQL調査.xlsx\n"
        "MARKDOWN_DIR = SOQL資料\n"
        "DIAGRAM_DIR = 関係図\n"
        "[SURVEY]\n"
        "RELATED_DEPTH = 3\n"
        "RELATED_MAX = 40\n"
        "ROW_LIMIT_CHECK = ○\n"
        "SIMILAR_COLUMN_THRESHOLD = 0.8\n"
        "[PII]\n"
        "KEYWORDS = 氏名, 名前\n"
        "PERSON_OBJECTS = Contact, Lead\n",
        encoding="utf-8",
    )
    if extra:
        with ini.open("a", encoding="utf-8") as handle:
            handle.write(extra)


def _write_ini_without_survey(tmp_path: Path, master_xlsx: Path) -> None:
    """``[SURVEY]`` / ``[PII]`` セクションを書かない config.ini を書く。

    設定欠落時の ``ConfigError → return 2`` の経路を検証するために使う。
    """
    ini = tmp_path / "config.ini"
    ini.write_text(
        "[FILES]\n"
        f"MASTER_XLSX_PATH = {master_xlsx}\n"
        "OUTPUT_DIR = ./output\n"
        "DESCRIBE_CSV_PATH = ./data/項目定義.csv\n"
        "COLUMN_FIX_XLSX_PATH = ./input/列の直し.xlsx\n"
        "XLSX_NAME = SOQL調査.xlsx\n"
        "MARKDOWN_DIR = SOQL資料\n"
        "DIAGRAM_DIR = 関係図\n",
        encoding="utf-8",
    )


def _make_file_paths(tmp_path: Path, master: Path) -> Any:
    """``run()`` 用の ``FilePaths`` を作る（``read_file_paths`` の差し替えに使う）。"""
    from src.run import FilePaths

    return FilePaths(
        master_xlsx_path=master,
        output_dir=tmp_path / "output",
        describe_csv_path=tmp_path / "describe.csv",
        column_fix_xlsx_path=tmp_path / "column_fix.xlsx",
        xlsx_name="SOQL調査.xlsx",
        markdown_dir="SOQL資料",
        diagram_dir="関係図",
    )


def _patch_read_file_paths(
    monkeypatch: pytest.MonkeyPatch, file_paths: Any, tmp_path: Path
) -> None:
    """``src.run.read_file_paths`` を差し替え、 ``run()`` が comken の
    デフォルト config から ``[SURVEY]`` / ``[PII]`` を読む先を ``tmp_path``
    の ``config.ini`` に向ける （``reset_cached_config`` でキャッシュも
    破棄）。
    """
    import sys

    from comken_state import reset_cached_config

    import src.run as run_module

    reset_cached_config()
    monkeypatch.setattr(sys, "argv", [str(tmp_path / "fake_main.py")])

    monkeypatch.setattr(run_module, "read_file_paths", lambda: file_paths)


# ── 偽物のクライアント ─────────────────────────────────────────────────


class _ReportStub:
    def __init__(self, describe: dict) -> None:
        self._describe = describe

    def describe(self, report_id: str) -> dict:
        return self._describe

    def get(self, report_id: str, filters: object = None, allow_truncated: bool = False) -> object:
        return []


class _FakeClient:
    """Fake Salesforce クライアント（``with site() as client`` の中身）。"""

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

    ``site_for(url)`` でインスタンス（実質 ``class`` のようなもの） を返し、
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
    """引数が 1 つでもあれば ERROR（終了コード 2） を出して ``site_for`` を呼ばない。"""
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
    _patch_read_file_paths(monkeypatch, _make_file_paths(tmp_path, master), tmp_path)

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
    _patch_read_file_paths(monkeypatch, _make_file_paths(tmp_path, master), tmp_path)

    monkeypatch.setattr(sys, "argv", ["main.py", "--help"])

    assert run_module.run(None, site_for=lambda _u: (_ for _ in ()).throw(AssertionError())) == 2


# ── 「有効」の絞り込み ─────────────────────────────────────────────────


def _run_with_fake_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, master: Path
) -> list[str]:
    """``run_fetch`` を差し替えて ``run()`` を回し、 取りに行った管理番号を返す。"""
    from src import run as run_module
    from src.fetch import FetchOutcome

    _write_ini(tmp_path, master)
    _patch_read_file_paths(monkeypatch, _make_file_paths(tmp_path, master), tmp_path)
    fetched: list[str] = []

    def fake_run_fetch(entries, **_kwargs):
        fetched.extend(entry.key for entry in entries)
        return [
            FetchOutcome(
                entry=entry,
                status="ok",
                record=_record_stub(
                    key=entry.key, summary=entry.summary, report_id="00O", url=entry.url
                ),
                warnings=(),
                error=None,
            )
            for entry in entries
        ]

    monkeypatch.setattr(run_module, "run_fetch", fake_run_fetch)
    monkeypatch.setattr(run_module, "run_tables", lambda *args, **kwargs: None)

    assert run_module.run([], site_for=lambda _u: (_ for _ in ()).throw(AssertionError())) == 0
    return fetched


def test_run_surveys_only_rows_marked_in_survey_column(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """「調査」列があれば、 「有効」かつ「調査」が ○ の行だけを取りに行く。"""
    master = tmp_path / "master.xlsx"
    _build_master(
        master,
        [
            {
                "ID": "1001",
                "Salesforce URL": f"{DOMAIN}/00O5g00000AAAAA/view",
                "有効": "○",
                "調査": "○",
            },
            {
                "ID": "1002",
                "Salesforce URL": f"{DOMAIN}/00O5g00000BBBBB/view",
                "有効": "○",
                "調査": "",
            },
            {
                "ID": "1003",
                "Salesforce URL": f"{DOMAIN}/00O5g00000CCCCC/view",
                "有効": "×",
                "調査": "○",
            },
        ],
    )

    assert _run_with_fake_fetch(tmp_path, monkeypatch, master) == ["1001"]


def test_run_prunes_stored_report_definitions_of_reports_not_surveyed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """今回調べないレポートの貯めたレポート定義は消し、 調べるレポートのものは残す。

    ``SOQL調査.xlsx`` / ``列名の対応.csv`` / ``項目定義.csv`` などの出力には触らない。
    """
    from src.describe_store import DESCRIBE_CSV_COLUMNS
    from src.report_store import ReportStore

    master = tmp_path / "master.xlsx"
    _build_master(
        master,
        [
            {
                "ID": "1001",
                "Salesforce URL": f"{DOMAIN}/00O5g00000AAAAA/view",
                "有効": "○",
                "調査": "○",
            },
            {
                "ID": "1002",
                "Salesforce URL": f"{DOMAIN}/00O5g00000BBBBB/view",
                "有効": "○",
                "調査": "",
            },
        ],
    )
    slim = {"reportMetadata": {"name": "x"}, "reportExtendedMetadata": {}}
    store = ReportStore.load(tmp_path / "describe.csv")
    store.put("example.my.salesforce.com", "00O5g00000AAAAA", slim, ())
    store.put("example.my.salesforce.com", "00O5g00000BBBBB", slim, ())
    store.put("example.my.salesforce.com", "00O5g00000ZZZZZ", slim, ())  # 管理表に無いレポート
    output_keep = tmp_path / "output" / "列名の対応.csv"
    output_keep.parent.mkdir(parents=True)
    output_keep.write_text("残す", encoding="utf-8")
    describe_csv = tmp_path / "describe.csv"
    describe_content = ",".join(DESCRIBE_CSV_COLUMNS) + "\n"  # 見出しだけの正しい項目定義
    describe_csv.write_text(describe_content, encoding="utf-8")

    _run_with_fake_fetch(tmp_path, monkeypatch, master)

    remaining = sorted(p.name for p in (tmp_path / "レポート定義").iterdir())
    assert remaining == ["example.my.salesforce.com__00O5g00000AAAAA.json"]
    assert output_keep.read_text(encoding="utf-8") == "残す"
    assert describe_csv.read_text(encoding="utf-8") == describe_content


def test_run_does_not_prune_when_switched_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src import run as run_module
    from src.report_store import ReportStore

    monkeypatch.setattr(run_module, "PRUNE_REPORT_STORE", False)
    master = tmp_path / "master.xlsx"
    _build_master(
        master,
        [{"ID": "1001", "Salesforce URL": f"{DOMAIN}/00O5g00000AAAAA/view", "有効": "○"}],
    )
    slim = {"reportMetadata": {"name": "x"}, "reportExtendedMetadata": {}}
    ReportStore.load(tmp_path / "describe.csv").put("org", "00O5g00000ZZZZZ", slim, ())

    _run_with_fake_fetch(tmp_path, monkeypatch, master)

    assert [p.name for p in (tmp_path / "レポート定義").iterdir()] == ["org__00O5g00000ZZZZZ.json"]


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
    _patch_read_file_paths(monkeypatch, _make_file_paths(tmp_path, master), tmp_path)

    captured_keys: list[str] = []

    def fake_run_fetch(
        entries,
        *,
        related_depth=1,
        related_max=40,
        row_limit=True,
        pii_keywords=(),
        pii_person_objects=(),
        describe_csv_path=None,
        dry_run=False,
        site_for=None,
        describe_store=None,
        report_store=None,
        refresh_set=None,
        column_fix_overrides=None,
    ):
        captured_keys.extend(entry.key for entry in entries)
        outcomes = []
        for entry in entries:
            rec = _record_stub(key=entry.key, summary=entry.summary, report_id="00O", url=entry.url)
            outcomes.append(
                FetchOutcome(
                    entry=entry,
                    status="ok",
                    record=rec,
                    warnings=(),
                    error=None,
                )
            )
        return outcomes

    monkeypatch.setattr(run_module, "run_fetch", fake_run_fetch)
    monkeypatch.setattr(
        run_module,
        "run_tables",
        lambda *args, **kwargs: None,
    )

    code = run_module.run([], site_for=lambda _u: (_ for _ in ()).throw(AssertionError()))

    assert code == 0
    assert captured_keys == ["1001", "1003"]


def _record_stub(key: str, summary: str, report_id: str, url: str) -> ReportRecord:
    """中身が空の ``ReportRecord`` を作る （テスト用）。"""
    from src.fetch import ReportRecord

    return ReportRecord(
        key=key,
        summary=summary,
        report_id=report_id,
        url=url,
        main_object=None,
        report={},
        objects={},
        relations=(),
        column_map=[],
        warnings=(),
    )


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
    _patch_read_file_paths(monkeypatch, _make_file_paths(tmp_path, master), tmp_path)

    called = {"site_for": 0}

    def site_for(url: str):
        called["site_for"] += 1
        raise AssertionError("site_for should not be called")

    code = run_module.run([], site_for=site_for)

    assert code == 2
    assert called["site_for"] == 0


def test_run_returns_2_when_survey_config_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``[SURVEY]`` セクションが欠けている config.ini では ``ConfigError``
    を 2 にして返す。 ``run_fetch`` / ``site_for`` は呼ばれない。
    """
    from src import run as run_module

    master = tmp_path / "master.xlsx"
    _build_master(
        master,
        [
            {"ID": "1001", "Salesforce URL": f"{DOMAIN}/00O5g00000AAAAA/view", "有効": "○"},
        ],
    )
    _write_ini_without_survey(tmp_path, master)
    _patch_read_file_paths(monkeypatch, _make_file_paths(tmp_path, master), tmp_path)

    called = {"fetch": 0, "site_for": 0}

    def fake_run_fetch(*args, **kwargs):
        called["fetch"] += 1
        raise AssertionError("run_fetch should not be called")

    def site_for(url: str):
        called["site_for"] += 1
        raise AssertionError("site_for should not be called")

    monkeypatch.setattr(run_module, "run_fetch", fake_run_fetch)
    monkeypatch.setattr(
        run_module,
        "run_tables",
        lambda *args, **kwargs: None,
    )

    code = run_module.run([], site_for=site_for)

    assert code == 2
    assert called["fetch"] == 0
    assert called["site_for"] == 0


# ── 取得成功／失敗 ──────────────────────────────────────────────────────


def test_run_writes_jsons_and_csvs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """有効な行が複数あれば全部取られ、 CSV ができる。 JSON は書かれない。"""
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
    _patch_read_file_paths(monkeypatch, _make_file_paths(tmp_path, master), tmp_path)

    def fake_run_fetch(
        entries,
        *,
        related_depth=1,
        related_max=40,
        row_limit=True,
        pii_keywords=(),
        pii_person_objects=(),
        describe_csv_path=None,
        dry_run=False,
        site_for=None,
        describe_store=None,
        report_store=None,
        refresh_set=None,
        column_fix_overrides=None,
    ):
        outcomes = []
        for entry in entries:
            rec = _record_stub(key=entry.key, summary=entry.summary, report_id="00O", url=entry.url)
            outcomes.append(
                FetchOutcome(
                    entry=entry,
                    status="ok",
                    record=rec,
                    warnings=(),
                    error=None,
                )
            )
        return outcomes

    tables_called: list[dict] = []

    def fake_run_tables(output_dir, records, *, only_keys=None, **kwargs):
        tables_called.append(
            {
                "records": list(records),
                "only_keys": list(only_keys) if only_keys is not None else None,
            }
        )
        return None

    monkeypatch.setattr(run_module, "run_fetch", fake_run_fetch)
    monkeypatch.setattr(run_module, "run_tables", fake_run_tables)

    code = run_module.run([], site_for=lambda _u: (_ for _ in ()).throw(AssertionError()))

    assert code == 0
    # JSON は書かれていない
    assert not (tmp_path / "output" / "1001.json").exists()
    # CSV へ渡す record と only_keys が一致
    assert len(tables_called) == 1
    assert [r.key for r in tables_called[0]["records"]] == ["1001", "1002"]
    assert tables_called[0]["only_keys"] == ["1001", "1002"]


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
    file_paths = _make_file_paths(tmp_path, master)
    _patch_read_file_paths(monkeypatch, file_paths, tmp_path)

    # 失敗 ID の既存 CSV を残しておく
    file_paths.output_dir.mkdir(parents=True, exist_ok=True)
    existing_csv = file_paths.output_dir / "対応表_1002.csv"
    existing_csv.write_text("既存\nそのまま\n", encoding="utf-8")

    def fake_run_fetch(
        entries,
        *,
        related_depth=1,
        related_max=40,
        row_limit=True,
        pii_keywords=(),
        pii_person_objects=(),
        describe_csv_path=None,
        dry_run=False,
        site_for=None,
        describe_store=None,
        report_store=None,
        refresh_set=None,
        column_fix_overrides=None,
    ):
        rec = _record_stub(key=entries[0].key, summary="", report_id="00O", url=entries[0].url)
        return [
            FetchOutcome(
                entry=entries[0],
                status="ok",
                record=rec,
                warnings=(),
                error=None,
            ),
            FetchOutcome(
                entry=entries[1],
                status="failed",
                record=None,
                warnings=(),
                error="URL からレポート ID を取り出せません",
            ),
        ]

    captured: dict[str, Any] = {}

    def fake_run_tables(output_dir, records, *, only_keys=None, **kwargs):
        captured["only_keys"] = list(only_keys) if only_keys is not None else None
        captured["records"] = list(records)
        return None

    monkeypatch.setattr(run_module, "run_fetch", fake_run_fetch)
    monkeypatch.setattr(run_module, "run_tables", fake_run_tables)

    code = run_module.run([], site_for=lambda _u: (_ for _ in ()).throw(AssertionError()))

    assert code == 1
    assert captured["only_keys"] == ["1001"]
    assert [r.key for r in captured["records"]] == ["1001"]
    assert existing_csv.read_text(encoding="utf-8") == "既存\nそのまま\n"


def test_run_returns_1_when_run_tables_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``run_tables`` が例外を出しても、 全件成功に見えず 1 を返す。"""
    from src import run as run_module
    from src.fetch import FetchOutcome

    master = tmp_path / "master.xlsx"
    _build_master(
        master,
        [
            {"ID": "1001", "Salesforce URL": f"{DOMAIN}/00O5g00000AAAAA/view", "有効": "○"},
        ],
    )
    _write_ini(tmp_path, master)
    file_paths = _make_file_paths(tmp_path, master)
    _patch_read_file_paths(monkeypatch, file_paths, tmp_path)

    def fake_run_fetch(
        entries,
        *,
        related_depth=1,
        related_max=40,
        row_limit=True,
        pii_keywords=(),
        pii_person_objects=(),
        describe_csv_path=None,
        dry_run=False,
        site_for=None,
        describe_store=None,
        report_store=None,
        refresh_set=None,
        column_fix_overrides=None,
    ):
        rec = _record_stub(key=entries[0].key, summary="", report_id="00O", url=entries[0].url)
        return [
            FetchOutcome(
                entry=entries[0],
                status="ok",
                record=rec,
                warnings=(),
                error=None,
            )
        ]

    def fake_run_tables(*args, **kwargs):
        raise RuntimeError("xlsx を書き込めません")

    monkeypatch.setattr(run_module, "run_fetch", fake_run_fetch)
    monkeypatch.setattr(run_module, "run_tables", fake_run_tables)

    code = run_module.run([], site_for=lambda _u: (_ for _ in ()).throw(AssertionError()))

    assert code == 1


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
    file_paths = _make_file_paths(tmp_path, master)
    _patch_read_file_paths(monkeypatch, file_paths, tmp_path)

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
    # 後始末（dry-run を明示解除）
    assert not runtime.is_dry_run()


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
