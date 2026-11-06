"""``--rebuild`` （保存データだけで作り直す） のテスト。

Salesforce には繋がない。 保存データ（ ``data/レポート定義/`` のレポート定義と
``data/項目定義.csv`` の項目定義 ） だけを用意して、 ``run(["--rebuild"])`` が
保存データだけで出力を作ることを確かめる。

確かめること:

- 保存データが揃っていれば Salesforce のクライアントは 1 度も作られず、
  ``SOQL調査.xlsx`` と ``SOQL資料/{管理番号}.md`` が出る
- ``列の直し.xlsx`` の 「直したSOQL列名」 が --rebuild の出力（SOQL） に反映される
- レポート定義が保存データに無いレポートは 失敗になり、 終了コード 1、
  理由に 「実行.bat で取り直して」 が入る。 他のレポートは出る
- 項目定義（describe）が保存データに無いときは、 普通の実行で describe が
  404 になったときと同じ扱い（ レポートは失敗にせず、 警告だけ残る ）
- 主オブジェクト候補 (a) が保存データに無く (b) が保存データにあれば、 (b) で
  主オブジェクトが決まる（ 普通の実行と同じ挙動 ）
- 組織は 接続先クラスの ``DOMAIN_URL`` から決める（ 保存データのファイル名から
  推測しない ）
- ``--rebuild`` 以外の引数は今まで通り 2
- ``--rebuild`` では件数チェックの API を呼ばない
- ``--rebuild`` の後も レポート定義 / 項目定義 の保存ファイルが変わらない
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from comken.exceptions import SalesforceRequestError
from openpyxl import Workbook, load_workbook
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table as XlsxTable
from openpyxl.worksheet.table import TableStyleInfo

from src.column_fix_xlsx import COLUMN_FIX_COLUMNS
from src.describe_store import DescribeStore, upsert_object_rows
from src.fetch import offline_site_for, run_fetch
from src.fetch.report_slim import make_slim_report
from src.master import MasterEntry
from src.report_store import ReportStore
from src.soql_builder import STATUS_NONE

DOMAIN = "https://example.my.salesforce.com/lightning/r/Report"
# 接続先の組織（ URL のホスト名 ）。 保存データもこの名前で分ける
ORG = "example.my.salesforce.com"
ORG_DOMAIN_URL = f"https://{ORG}"
# サンドボックスに差し替えたときの組織。 本番（ ``ORG`` ）よりファイル名の
# 並び順で **後ろ** にする。 ファイル名から組織を推測する実装だと、 本番の
# 定義を掴んでしまうため（ テストが現行の作り方を弾くように ).
SANDBOX_ORG = "zzz.sandbox.my.salesforce.com"
SANDBOX_DOMAIN_URL = f"https://{SANDBOX_ORG}"
REPORT_ID = "00O5g00000AAAAA"
OTHER_REPORT_ID = "00O5g00000BBBBB"


# ── ヘルパー ─────────────────────────────────────────────────────────────


def _build_master(path: Path, rows: list[dict[str, str]]) -> None:
    """テスト用の管理表 xlsx を作る。"""
    workbook = Workbook()
    active = workbook.active
    if active is not None:
        workbook.remove(active)
    sheet = workbook.create_sheet("PY_管理表")
    headers = ["ID", "概要", "Salesforce URL", "有効"]
    sheet.append(headers)
    for row in rows:
        sheet.append([row.get(col, "") for col in headers])
    table = XlsxTable(
        displayName="PY_T_ReportEntry",
        ref=f"A1:{get_column_letter(len(headers))}{len(rows) + 1}",
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


def _write_ini(tmp_path: Path) -> None:
    """テスト用 ``config.ini`` を書く。 ``[FILES]`` の 7 つのキーはすべて必須。"""
    (tmp_path / "config.ini").write_text(
        "[FILES]\n"
        f"MASTER_XLSX_PATH = {tmp_path / 'master.xlsx'}\n"
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


def _make_file_paths(tmp_path: Path) -> Any:
    """``run()`` 用の ``FilePaths`` を作る。"""
    from src.run import FilePaths

    return FilePaths(
        master_xlsx_path=tmp_path / "master.xlsx",
        output_dir=tmp_path / "output",
        describe_csv_path=tmp_path / "data" / "項目定義.csv",
        column_fix_xlsx_path=tmp_path / "input" / "列の直し.xlsx",
        xlsx_name="SOQL調査.xlsx",
        markdown_dir="SOQL資料",
        diagram_dir="関係図",
    )


def _patch_config(monkeypatch: Any, tmp_path: Path) -> None:
    """comken の config を ``tmp_path`` の ``config.ini`` から読むようにする。"""
    from comken_state import reset_cached_config

    reset_cached_config()
    monkeypatch.setattr("sys.argv", [str(tmp_path / "fake_main.py")])
    monkeypatch.setenv("PYTHONPATH", "F:\\dev\\comken")


def _field(name: str, label: str, type_: str = "string") -> dict[str, Any]:
    """``describe_object`` の戻り値相当の項目 dict を作る。"""
    return {
        "name": name,
        "label": label,
        "type": type_,
        "custom": False,
        "referenceTo": [],
        "relationshipName": None,
        "picklistValues": [],
    }


def _task_describe() -> dict[str, Any]:
    """主オブジェクト（ Task ） の describe 相当を作る。"""
    return {
        "name": "Task",
        "label": "活動",
        "custom": False,
        "fields": [
            _field("Subject", "件名"),
            _field("Description", "説明"),
        ],
    }


def _account_describe() -> dict[str, Any]:
    """主オブジェクト（ Account ） の describe 相当を作る。"""
    return {
        "name": "Account",
        "label": "顧客",
        "custom": False,
        "fields": [
            _field("Name", "顧客名"),
        ],
    }


def _report_metadata(
    report_id: str = REPORT_ID,
    *,
    report_type: str = "Task",
    detail_columns: tuple[str, ...] = ("SUBJECT", "MYSTERY"),
    column_info: dict[str, str] | None = None,
) -> dict[str, Any]:
    """レポート describe 相当を作る。"""
    info = column_info or {"SUBJECT": "件名", "MYSTERY": "謎の列"}
    return {
        "reportMetadata": {
            "id": report_id,
            "name": "テスト",
            "reportType": {"type": report_type},
            "reportFormat": "TABULAR",
            "detailColumns": list(detail_columns),
        },
        "reportExtendedMetadata": {
            "detailColumnInfo": info,
        },
    }


def _save_report_definition(
    file_paths: Any,
    report_id: str,
    metadata: dict[str, Any],
    *,
    org: str = ORG,
) -> None:
    """レポート定義を 保存データ に書く。"""
    store = ReportStore.load(file_paths.describe_csv_path)
    store.put(org, report_id, make_slim_report(metadata), ())


def _save_object_describe(file_paths: Any, describe: dict[str, Any], *, org: str = ORG) -> None:
    """オブジェクトの項目定義を 保存データ （ 項目定義.csv ） に書く。"""
    upsert_object_rows(file_paths.describe_csv_path, org, describe["name"], describe)


def _write_column_fix(path: Path, rows: list[dict[str, str]]) -> None:
    """``列の直し.xlsx`` を書く（ 人が直した値を入れるテスト用 ）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    active = workbook.active
    if active is not None:
        workbook.remove(active)
    sheet = workbook.create_sheet("列の直し")
    sheet.append(list(COLUMN_FIX_COLUMNS))
    for row in rows:
        sheet.append([row.get(col, "") for col in COLUMN_FIX_COLUMNS])
    table = XlsxTable(
        displayName="ColumnFix",
        ref=f"A1:{get_column_letter(len(COLUMN_FIX_COLUMNS))}{len(rows) + 1}",
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


def _read_xlsx(path: Path) -> dict[str, list[dict[str, object]]]:
    """``load_workbook`` でシート名 → データ行 dict の list を読む。"""
    workbook = load_workbook(path)
    result: dict[str, list[dict[str, object]]] = {}
    for sheet_name in workbook.sheetnames:
        sheet = workbook[sheet_name]
        all_rows = list(sheet.iter_rows(values_only=True))
        header_idx = -1
        for index, row in enumerate(all_rows):
            if not row:
                continue
            if all(isinstance(value, str) and value for value in row):
                header_idx = index
                break
        if header_idx < 0:
            result[sheet_name] = []
            continue
        headers = list(all_rows[header_idx])
        result[sheet_name] = [
            {
                str(header): ("" if value is None else value)
                for header, value in zip(headers, row, strict=False)
            }
            for row in all_rows[header_idx + 1 :]
        ]
    workbook.close()
    return result


def _fake_base_site_for(domain_url: str) -> Any:
    """``site_for`` 互換の関数。 返すのは サイト **クラス** （ ``DOMAIN_URL`` を持つ ）。

    クラスは インスタンス化すると ``AssertionError`` になる（= ログインする）。
    ``--rebuild`` では クラス属性の ``DOMAIN_URL`` しか読まない。
    """

    class _BaseSite:
        DOMAIN_URL = domain_url

        def __init__(self, *args: object, **kwargs: object) -> None:
            raise AssertionError(f"Salesforce に接続してはいけない: {self.DOMAIN_URL}")

    def resolver(url: str) -> type:
        return _BaseSite

    return resolver


def _run_rebuild(file_paths: Any, domain_url: str = ORG_DOMAIN_URL) -> int:
    """``--rebuild`` で ``run()`` を 1 回実行する。"""
    from src.run import run

    return run(
        ["--rebuild"],
        site_for=_fake_base_site_for(domain_url),
        file_paths=file_paths,
    )


def _setup(tmp_path: Path, monkeypatch: Any, rows: list[dict[str, str]]) -> Any:
    """管理表と config.ini を作って ``FilePaths`` を返す。"""
    _build_master(tmp_path / "master.xlsx", rows)
    _write_ini(tmp_path)
    _patch_config(monkeypatch, tmp_path)
    return _make_file_paths(tmp_path)


def _one_report_row() -> list[dict[str, str]]:
    """レポート 1 件だけの管理表の行。"""
    return [
        {
            "ID": "1001",
            "概要": "活動一覧",
            "Salesforce URL": f"{DOMAIN}/{REPORT_ID}/view",
            "有効": "○",
        }
    ]


def _two_report_rows() -> list[dict[str, str]]:
    """レポート 2 件の管理表の行。"""
    return [
        {
            "ID": "1001",
            "概要": "活動一覧",
            "Salesforce URL": f"{DOMAIN}/{REPORT_ID}/view",
            "有効": "○",
        },
        {
            "ID": "1002",
            "概要": "別のレポート",
            "Salesforce URL": f"{DOMAIN}/{OTHER_REPORT_ID}/view",
            "有効": "○",
        },
    ]


def _entry() -> MasterEntry:
    """レポート 1 件の管理表の行（ ``run_fetch`` に直接渡す用 ）。"""
    return MasterEntry(
        key="1001",
        summary="活動一覧",
        url=f"{DOMAIN}/{REPORT_ID}/view",
        enabled=True,
    )


def _fetch_settings(tmp_path: Path, name: str = "describe") -> dict[str, Any]:
    """``run_fetch`` に渡す kwargs を返す。"""
    return {
        "related_depth": 1,
        "related_max": 40,
        "pii_keywords": (),
        "pii_person_objects": (),
        "describe_csv_path": tmp_path / f"{name}.csv",
    }


# ── 普通の実行との比較に使う偽物 ─────────────────────────────────────────


class _FakeReportAPI:
    """``client.report`` の偽物。"""

    def __init__(self, metadata: dict[str, Any]) -> None:
        self._metadata = metadata

    def describe(self, report_id: str) -> dict[str, Any]:
        return self._metadata

    def get(self, report_id: str, *args: Any, **kwargs: Any) -> list[Any]:
        return []


class _FakeClient:
    """``describe_object`` が指定のオブジェクトで 404 を返す偽物のクライアント。"""

    def __init__(self, metadata: dict[str, Any], errors: dict[str, BaseException]) -> None:
        self.report = _FakeReportAPI(metadata)
        self._errors = errors

    def describe_object(self, name: str) -> dict[str, Any]:
        error = self._errors.get(name)
        if error is not None:
            raise error
        return {"name": name, "label": name, "custom": False, "fields": []}


class _FakeSite:
    """``with site_for(url)() as client:`` の形を持つ偽物のサイト。"""

    def __init__(self, client: _FakeClient) -> None:
        self._client = client

    def __call__(self) -> _FakeSite:
        return self

    def __enter__(self) -> _FakeClient:
        return self._client

    def __exit__(self, *args: object) -> None:
        return None


def _fake_site_for(client: _FakeClient) -> Any:
    """``site_for`` 互換の関数（ 普通の実行用 ）。"""
    site = _FakeSite(client)

    def resolver(url: str) -> _FakeSite:
        return site

    return resolver


# ── テスト本体 ───────────────────────────────────────────────────────────


def test_rebuild_builds_outputs_from_saved_data_without_client(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """保存データが揃っていれば Salesforce のクライアントは 1 度も作られず、
    ``SOQL調査.xlsx`` と ``SOQL資料/{管理番号}.md`` が出る。
    """
    file_paths = _setup(tmp_path, monkeypatch, _one_report_row())
    _save_report_definition(file_paths, REPORT_ID, _report_metadata())
    _save_object_describe(file_paths, _task_describe())

    # サイトクラスは 1 度も作られない（ 作られたら AssertionError ）
    code = _run_rebuild(file_paths)

    assert code == 0
    out = tmp_path / "output"
    assert (out / "SOQL調査.xlsx").exists()
    assert (out / "SOQL資料" / "1001.md").exists()


def test_rebuild_applies_human_fixed_soql_column_name(tmp_path: Path, monkeypatch: Any) -> None:
    """``列の直し.xlsx`` の 「直したSOQL列名」 が --rebuild の出力（ SOQL ） に
    反映される。
    """
    file_paths = _setup(tmp_path, monkeypatch, _one_report_row())
    _save_report_definition(file_paths, REPORT_ID, _report_metadata())
    _save_object_describe(file_paths, _task_describe())
    _write_column_fix(
        file_paths.column_fix_xlsx_path,
        [
            {
                "状態": "要対応",
                "主オブジェクト": "Task",
                "表示名": "謎の列",
                "列キー": "MYSTERY",
                "直したSOQL列名": "Description",
            }
        ],
    )

    assert _run_rebuild(file_paths) == 0

    md_text = (tmp_path / "output" / "SOQL資料" / "1001.md").read_text(encoding="utf-8")
    assert "Description" in md_text
    sheets = _read_xlsx(tmp_path / "output" / "SOQL調査.xlsx")
    by_key = {row["列キー"]: row for row in sheets["列の対応"]}
    assert by_key["MYSTERY"]["使うSOQL列名"] == "Description"
    assert by_key["MYSTERY"]["どこから"] == "人が直した"


def test_rebuild_fails_when_report_definition_is_missing(
    tmp_path: Path, monkeypatch: Any, caplog: Any
) -> None:
    """レポート定義が保存データに無いレポートは 失敗になり、 終了コード 1、
    理由に 「実行.bat で取り直して」 が入る。 他のレポートは出る。
    """
    file_paths = _setup(tmp_path, monkeypatch, _two_report_rows())
    # 1002 のレポート定義は 保存データ に無い
    _save_report_definition(file_paths, REPORT_ID, _report_metadata())
    _save_object_describe(file_paths, _task_describe())

    with caplog.at_level(logging.ERROR, logger="src.fetch.flow"):
        code = _run_rebuild(file_paths)

    assert code == 1
    out = tmp_path / "output"
    assert (out / "SOQL資料" / "1001.md").exists()
    assert not (out / "SOQL資料" / "1002.md").exists()
    # 失敗の理由に 取り直しの案内が出る（ Salesforce のエラー其它に混ぜない ）
    assert "理由=保存データに無いので 実行.bat で取り直してください" in caplog.text
    assert "レポート定義" in caplog.text


def test_rebuild_warns_when_object_describe_is_missing(
    tmp_path: Path, monkeypatch: Any, caplog: Any
) -> None:
    """項目定義（ describe ） が保存データに無いときは、 普通の実行で describe が
    404 になったときと同じ扱い（ レポートは失敗にせず、 警告だけ残る ）。
    """
    file_paths = _setup(tmp_path, monkeypatch, _one_report_row())
    # レポート定義は 保存データ に無い … ではなく、 項目定義だけが無い
    _save_report_definition(file_paths, REPORT_ID, _report_metadata())

    with caplog.at_level(logging.WARNING, logger="src.fetch.flow"):
        code = _run_rebuild(file_paths)

    # レポート全体は失敗しない（ 終了コードは 0 ）
    assert code == 0
    out = tmp_path / "output"
    assert (out / "SOQL資料" / "1001.md").exists()
    # 警告に 「保存データに無い」 が出る
    assert "オブジェクト Task は保存データに無い" in caplog.text
    # 主オブジェクトは不明（ 普通の実行の 404 と同じ形 ）
    sheets = _read_xlsx(out / "SOQL調査.xlsx")
    overview = {row["管理番号"]: row for row in sheets["一覧"]}
    assert overview["1001"]["主オブジェクト"] == "(不明)"
    assert overview["1001"]["SOQLの状態"] == STATUS_NONE


def test_rebuild_missing_object_describe_has_same_record_as_normal_404(tmp_path: Path) -> None:
    """--rebuild で項目定義が無いときの record は、 普通の実行で describe が
    404 のときの record と、 警告文以外は同じ（ 主オブジェクト不明・SOQL は
    作れず・列対応表も同じ ）。
    """
    metadata = _report_metadata()

    # 普通の実行: describe_object("Task") が 404
    client = _FakeClient(
        metadata,
        {"Task": SalesforceRequestError("GET", "/sobjects/Task/describe", 404, "Not Found")},
    )
    normal = run_fetch(
        [_entry()],
        site_for=_fake_site_for(client),
        describe_store=DescribeStore.empty(tmp_path / "normal.csv"),
        report_store=ReportStore.empty(),
        row_limit=False,
        **_fetch_settings(tmp_path, "normal"),
    )[0]

    # --rebuild: レポート定義は保存データにあり、 項目定義は無い
    report_store = ReportStore.load(tmp_path / "data" / "項目定義.csv")
    report_store.put(ORG, REPORT_ID, make_slim_report(metadata), ())
    rebuild = run_fetch(
        [_entry()],
        site_for=offline_site_for(_fake_base_site_for(ORG_DOMAIN_URL)),
        describe_store=DescribeStore.empty(tmp_path / "rebuild.csv"),
        report_store=report_store,
        row_limit=False,
        allow_refresh=False,
        **_fetch_settings(tmp_path, "rebuild"),
    )[0]

    assert normal.status == "ok"
    assert rebuild.status == "ok"
    normal_record = normal.record
    rebuild_record = rebuild.record
    assert normal_record is not None
    assert rebuild_record is not None
    # 主オブジェクトは両方とも不明
    assert normal_record.main_object is None
    assert rebuild_record.main_object == normal_record.main_object
    # SOQL の状態も同じ（ 作れず ）
    assert normal_record.soql_draft.status == STATUS_NONE
    assert rebuild_record.soql_draft.status == normal_record.soql_draft.status
    # 列対応表・主＋関連オブジェクトも同じ
    assert rebuild_record.column_map == normal_record.column_map
    assert rebuild_record.objects == normal_record.objects
    # 警告は describe の 1 行だけが違う（ 残りは同じ ）
    assert len(rebuild_record.warnings) == len(normal_record.warnings)
    assert rebuild_record.warnings[1:] == normal_record.warnings[1:]
    assert "保存データに無い" in rebuild_record.warnings[0]
    assert "404" in normal_record.warnings[0]


def test_rebuild_uses_column_key_candidate_when_report_type_is_missing(
    tmp_path: Path, monkeypatch: Any, caplog: Any
) -> None:
    """主オブジェクト候補 (a) が保存データに無く、 (b) の候補は保存データにある
    とき、 (b) で主オブジェクトが決まり、 レポートは成功する
    （ カスタムレポートタイプの典型）。
    """
    file_paths = _setup(tmp_path, monkeypatch, _one_report_row())
    _save_report_definition(
        file_paths,
        REPORT_ID,
        _report_metadata(
            report_type="CustomType__c",
            detail_columns=("Account.Name",),
            column_info={"Account.Name": "顧客名"},
        ),
    )
    # 候補 (a) の CustomType__c は 保存データ に無い。 (b) の Account はある
    _save_object_describe(file_paths, _account_describe())

    with caplog.at_level(logging.WARNING, logger="src.fetch.flow"):
        code = _run_rebuild(file_paths)

    assert code == 0
    sheets = _read_xlsx(tmp_path / "output" / "SOQL調査.xlsx")
    overview = {row["管理番号"]: row for row in sheets["一覧"]}
    assert overview["1001"]["主オブジェクト"] == "Account"
    # (a) の失敗は (b) が成功したときは出さない（ 普通の実行と同じ挙動 ）
    assert "オブジェクト CustomType__c" not in caplog.text


def test_rebuild_uses_org_of_connected_site_not_saved_file_name(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """接続先がサンドボックスなら、 サンドボックスの組織の保存データを使う。
    同じレポート ID の本番の定義が保存データに無くても、 本番の定義は使わない。
    """
    file_paths = _setup(tmp_path, monkeypatch, _one_report_row())
    # 本番の定義（ ファイル名の並び順で先に来る ） と サンドボックスの定義
    _save_report_definition(file_paths, REPORT_ID, _report_metadata(report_type="Task"), org=ORG)
    _save_report_definition(
        file_paths,
        REPORT_ID,
        _report_metadata(
            report_type="Account",
            detail_columns=("NAME",),
            column_info={"NAME": "顧客名"},
        ),
        org=SANDBOX_ORG,
    )
    _save_object_describe(file_paths, _task_describe(), org=ORG)
    _save_object_describe(file_paths, _account_describe(), org=SANDBOX_ORG)

    # 接続先はサンドボックス（ base のサイトクラスの DOMAIN_URL ）
    assert _run_rebuild(file_paths, domain_url=SANDBOX_DOMAIN_URL) == 0

    sheets = _read_xlsx(tmp_path / "output" / "SOQL調査.xlsx")
    overview = {row["管理番号"]: row for row in sheets["一覧"]}
    assert overview["1001"]["主オブジェクト"] == "Account"


def test_rebuild_option_is_read_from_sys_argv(tmp_path: Path, monkeypatch: Any) -> None:
    """``python main.py --rebuild`` のように 引数を渡さない呼び出しでも、
    ``sys.argv`` から ``--rebuild`` を読む。
    """
    from src.run import run

    file_paths = _setup(tmp_path, monkeypatch, _one_report_row())
    _save_report_definition(file_paths, REPORT_ID, _report_metadata())
    _save_object_describe(file_paths, _task_describe())
    monkeypatch.setattr("sys.argv", [str(tmp_path / "main.py"), "--rebuild"])

    code = run(None, site_for=_fake_base_site_for(ORG_DOMAIN_URL), file_paths=file_paths)

    assert code == 0
    assert (tmp_path / "output" / "SOQL調査.xlsx").exists()


def test_run_with_other_argv_returns_2(tmp_path: Path, monkeypatch: Any) -> None:
    """``--rebuild`` 以外の引数は 今まで通り 2。"""
    from src.run import run

    file_paths = _setup(tmp_path, monkeypatch, _one_report_row())
    _save_report_definition(file_paths, REPORT_ID, _report_metadata())
    _save_object_describe(file_paths, _task_describe())

    assert run(["fetch"], site_for=_fake_base_site_for(ORG_DOMAIN_URL), file_paths=file_paths) == 2
    assert (
        run(
            ["--rebuild", "x"],
            site_for=_fake_base_site_for(ORG_DOMAIN_URL),
            file_paths=file_paths,
        )
        == 2
    )
    # 出力が作られていない
    assert not (tmp_path / "output" / "SOQL調査.xlsx").exists()


def test_rebuild_does_not_call_row_count_api(tmp_path: Path, monkeypatch: Any) -> None:
    """``--rebuild`` では 件数チェックの API を呼ばない。

    呼んだら オフライン用の例外で失敗するので、 終了コード 0 と
    「2000件超」 が 「未実施」 であることを確かめる。
    """
    file_paths = _setup(tmp_path, monkeypatch, _one_report_row())
    _save_report_definition(file_paths, REPORT_ID, _report_metadata())
    _save_object_describe(file_paths, _task_describe())

    assert _run_rebuild(file_paths) == 0

    sheets = _read_xlsx(tmp_path / "output" / "SOQL調査.xlsx")
    overview = {row["管理番号"]: row for row in sheets["一覧"]}
    assert overview["1001"]["2000件超"] == "未実施"
    assert overview["1001"]["行数"] == ""


def test_rebuild_does_not_change_saved_data(tmp_path: Path, monkeypatch: Any) -> None:
    """``--rebuild`` の後も レポート定義 / 項目定義 の保存ファイルが変わらない。
    今回調べないレポートのレポート定義も消さない。
    """
    file_paths = _setup(tmp_path, monkeypatch, _one_report_row())
    _save_report_definition(file_paths, REPORT_ID, _report_metadata())
    _save_object_describe(file_paths, _task_describe())
    # 管理表に無いレポートのレポート定義。 今回調べないので通常実行では消される
    unused_report_id = "00O5g00000ZZZZZ"
    _save_report_definition(file_paths, unused_report_id, _report_metadata(unused_report_id))

    describe_path = file_paths.describe_csv_path
    store_dir = describe_path.parent / "レポート定義"
    before_csv = describe_path.read_bytes()
    before_json = {path.name: path.read_bytes() for path in sorted(store_dir.glob("*.json"))}

    assert _run_rebuild(file_paths) == 0

    assert describe_path.read_bytes() == before_csv
    after_json = {path.name: path.read_bytes() for path in sorted(store_dir.glob("*.json"))}
    assert after_json == before_json
    # 今回調べないレポートのレポート定義も残っている
    assert f"{ORG}__{unused_report_id}.json" in after_json
