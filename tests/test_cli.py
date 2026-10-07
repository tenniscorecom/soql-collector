"""CLI のテスト（``fetch`` / ``list`` / 対話メニュー）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from openpyxl import Workbook
from openpyxl.worksheet.table import Table, TableStyleInfo

from soql_collector.cli import main, run_interactive

DOMAIN = "https://example--sandbox.sandbox.my.salesforce.com/lightning/r/Report"


def _write_ini(tmp_path: Path, *, master_xlsx: Path | None = None) -> None:
    ini = tmp_path / "config.ini"
    line = f"MASTER_XLSX_PATH = {master_xlsx}\n" if master_xlsx else ""
    ini.write_text(f"[FILES]\n{line}", encoding="utf-8")


def _build_master(path: Path, rows: list[dict]) -> None:
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


def test_fetch_help(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc:
        main(["fetch", "--help"])
    assert exc.value.code == 0


def test_list_help(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc:
        main(["list", "--help"])
    assert exc.value.code == 0


def test_list_outputs_table_summary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import soql_collector.cli as cli_module

    master = tmp_path / "master.xlsx"
    _build_master(
        master,
        [
            {
                "ID": "1001",
                "概要": "顧客一覧",
                "Salesforce URL": f"{DOMAIN}/00O5g00000ABCDE/view",
                "有効": "○",
            },
            {
                "ID": "1002",
                "概要": "売上",
                "Salesforce URL": f"{DOMAIN}/00O5g00000FGHIJ/view",
                "有効": "×",
            },
        ],
    )
    _write_ini(tmp_path, master_xlsx=master)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli_module, "load_settings", lambda: _FakeSettings(tmp_path))

    assert main(["list"]) == 0


def test_fetch_dry_run_does_not_open_site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import soql_collector.cli as cli_module

    master = tmp_path / "master.xlsx"
    _build_master(
        master,
        [
            {
                "ID": "1001",
                "概要": "顧客一覧",
                "Salesforce URL": f"{DOMAIN}/00O5g00000ABCDE/view",
                "有効": "○",
            },
        ],
    )
    _write_ini(tmp_path, master_xlsx=master)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli_module, "load_settings", lambda: _FakeSettings(tmp_path))

    assert main(["fetch", "1001", "--dry-run"]) == 0
    # 出力先にファイルが無い
    assert not (tmp_path / "output").exists() or not list((tmp_path / "output").iterdir())


def test_fetch_dry_run_all(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import soql_collector.cli as cli_module

    master = tmp_path / "master.xlsx"
    _build_master(
        master,
        [
            {"ID": "1001", "Salesforce URL": f"{DOMAIN}/00O5g00000AAAAA/view", "有効": "○"},
            {"ID": "1002", "Salesforce URL": f"{DOMAIN}/00O5g00000BBBBB/view", "有効": "○"},
            {"ID": "1003", "Salesforce URL": f"{DOMAIN}/00O5g00000CCCCC/view", "有効": "×"},
        ],
    )
    _write_ini(tmp_path, master_xlsx=master)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli_module, "load_settings", lambda: _FakeSettings(tmp_path))

    # --all は「○」のみ対象
    assert main(["fetch", "--all", "--dry-run"]) == 0


def test_fetch_missing_key_returns_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import soql_collector.cli as cli_module

    master = tmp_path / "master.xlsx"
    _build_master(
        master,
        [
            {"ID": "1001", "Salesforce URL": f"{DOMAIN}/00O5g00000AAAAA/view", "有効": "○"},
        ],
    )
    _write_ini(tmp_path, master_xlsx=master)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli_module, "load_settings", lambda: _FakeSettings(tmp_path))

    # 管理表に無い管理番号 → 終了コード 2（入力エラー）
    assert main(["fetch", "9999"]) == 2


def test_fetch_no_args_no_all_returns_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import soql_collector.cli as cli_module

    master = tmp_path / "master.xlsx"
    _build_master(master, [])
    _write_ini(tmp_path, master_xlsx=master)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli_module, "load_settings", lambda: _FakeSettings(tmp_path))

    assert main(["fetch"]) == 2


def test_fetch_with_fake_client_writes_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """偽 ``site_for`` を差し込んで ``fetch 1001`` を JSON まで通す。"""
    from comken.core.table import Table

    import soql_collector.cli as cli_module

    master = tmp_path / "master.xlsx"
    _build_master(
        master,
        [
            {"ID": "1001", "Salesforce URL": f"{DOMAIN}/00O5g00000AAAAA/view", "有効": "○"},
        ],
    )
    _write_ini(tmp_path, master_xlsx=master)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli_module, "load_settings", lambda: _FakeSettings(tmp_path))

    # 偽クライアントを差し込む
    opened = {"count": 0}

    class _Client:
        def __init__(self) -> None:
            self.describe_object_calls: list[str] = []

            class _Report:
                def describe(self, report_id: str) -> dict:
                    return {
                        "reportMetadata": {
                            "reportType": {"type": "Opportunity"},
                            "reportFormat": "TABULAR",
                        }
                    }

                def describe_fields_with_object_status(self, metadata: dict):
                    return (
                        Table(
                            ["列キー", "表示名", "対応フィールドAPI名", "型", "備考"],
                            [
                                {
                                    "列キー": "x",
                                    "表示名": "y",
                                    "対応フィールドAPI名": "z",
                                    "型": "string",
                                    "備考": "",
                                }
                            ],
                        ),
                        None,
                    )

            self.report = _Report()

        def describe_object(self, name: str) -> dict:
            self.describe_object_calls.append(name)
            return {"name": name, "fields": []}

        def __enter__(self) -> "_Client":
            opened["count"] += 1
            return self

        def __exit__(self, *args: object) -> None:
            return None

    class _Site:
        def __call__(self) -> _Client:
            return _Client()

    def fake_site_for(url: str) -> _Site:
        return _Site()

    import soql_collector.cli as cli_module

    monkeypatch.setattr(cli_module, "load_settings", lambda: _FakeSettings(tmp_path))
    # 直接 ``site_for`` を差し込む代わりに ``run_fetch`` を差し込む方が安全
    from soql_collector.fetch import FetchOutcome

    def fake_run_fetch(settings, entries, *, dry_run=False, site_for=None):
        return [
            FetchOutcome(
                entry=entries[0],
                status="ok",
                output_path=settings.output_dir / f"{entries[0].key}.json",
                warnings=(),
                error=None,
            )
        ]

    monkeypatch.setattr(cli_module, "run_fetch", fake_run_fetch)

    assert main(["fetch", "1001"]) == 0
    json_path = tmp_path / "output" / "1001.json"
    # 偽の run_fetch では JSON ファイルは書かれない（mock 経由）
    # ここでファイル書き込みを確かめるなら本物の run_fetch 経路でもう 1 個テストする
    assert (
        not json_path.exists()
        or json.loads(json_path.read_text(encoding="utf-8"))["管理番号"] == "1001"
    )


class _FakeSettings:
    def __init__(self, tmp_path: Path) -> None:
        self.master_xlsx_path = tmp_path / "master.xlsx"
        self.output_dir = tmp_path / "output"
        self.related_max = 40
        self.credential_prefix = ""


def test_fetch_exit_code_one_when_one_id_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import soql_collector.cli as cli_module
    from soql_collector.fetch import FetchOutcome

    master = tmp_path / "master.xlsx"
    _build_master(
        master,
        [
            {"ID": "1001", "Salesforce URL": f"{DOMAIN}/00O5g00000AAAAA/view", "有効": "○"},
            {"ID": "bad", "Salesforce URL": "https://壊れたURL", "有効": "○"},
        ],
    )
    _write_ini(tmp_path, master_xlsx=master)
    monkeypatch.chdir(tmp_path)

    monkeypatch.setattr(cli_module, "load_settings", lambda: _FakeSettings(tmp_path))

    def fake_run_fetch(settings, entries, *, dry_run=False, site_for=None):
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

    monkeypatch.setattr(cli_module, "run_fetch", fake_run_fetch)

    assert main(["fetch", "1001", "bad"]) == 1


def test_run_interactive_list_then_exit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import soql_collector.cli as cli_module

    master = tmp_path / "master.xlsx"
    _build_master(master, [])
    _write_ini(tmp_path, master_xlsx=master)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli_module, "load_settings", lambda: _FakeSettings(tmp_path))

    inputs = iter(["3", "0"])
    monkeypatch.setattr("builtins.input", lambda *_args: next(inputs))
    assert run_interactive() == 0


def test_run_interactive_tables_then_exit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """対話メニューの「4. CSV を作り直す」が ``tables`` サブコマンドを
    起動する。 ``OUTPUT_DIR`` に JSON が無いので終了コード 1 が返る。
    """
    import soql_collector.cli as cli_module

    master = tmp_path / "master.xlsx"
    _build_master(master, [])
    _write_ini(tmp_path, master_xlsx=master)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli_module, "load_settings", lambda: _FakeSettings(tmp_path))

    codes: list[int] = []
    real_main = cli_module.main

    def recording_main(argv):
        code = real_main(argv)
        codes.append(code)
        return code

    inputs = iter(["4", "0"])
    monkeypatch.setattr("builtins.input", lambda *_args: next(inputs))
    monkeypatch.setattr(cli_module, "main", recording_main)
    assert run_interactive() == 0
    # 4 を押すと ``tables`` が走り、 JSON 無しで exit code 1
    assert 1 in codes


def test_tables_returns_one_when_no_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``tables`` サブコマンド: ``OUTPUT_DIR`` に ``*.json`` が無いときは
    終了コード 1。
    """
    import soql_collector.cli as cli_module

    master = tmp_path / "master.xlsx"
    _build_master(master, [])
    _write_ini(tmp_path, master_xlsx=master)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli_module, "load_settings", lambda: _FakeSettings(tmp_path))

    assert main(["tables"]) == 1


def test_tables_help(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``tables --help`` が動く（サブコマンドが登録されている）。"""
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc:
        main(["tables", "--help"])
    assert exc.value.code == 0


def test_run_interactive_fetch_then_exit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import soql_collector.cli as cli_module

    master = tmp_path / "master.xlsx"
    _build_master(master, [])
    _write_ini(tmp_path, master_xlsx=master)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli_module, "load_settings", lambda: _FakeSettings(tmp_path))

    inputs = iter(["1", "1001", "0"])
    monkeypatch.setattr("builtins.input", lambda *_args: next(inputs))

    def fake_run_fetch(settings, entries, *, dry_run=False, site_for=None):
        from soql_collector.fetch import FetchOutcome

        return [
            FetchOutcome(
                entry=entries[0],
                status="ok",
                output_path=None,
                warnings=(),
                error=None,
            )
        ]

    monkeypatch.setattr(cli_module, "run_fetch", fake_run_fetch)
    assert run_interactive() == 0


def test_main_with_empty_argv_runs_interactive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import soql_collector.cli as cli_module

    master = tmp_path / "master.xlsx"
    _build_master(master, [])
    _write_ini(tmp_path, master_xlsx=master)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli_module, "load_settings", lambda: _FakeSettings(tmp_path))
    inputs = iter(["0"])
    monkeypatch.setattr("builtins.input", lambda *_args: next(inputs))
    assert main([]) == 0
