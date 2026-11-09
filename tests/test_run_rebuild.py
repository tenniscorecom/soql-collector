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
from src.fetch.offline import OfflineDescribeMissing
from src.fetch.report_slim import make_slim_report
from src.master import MasterEntry
from src.report_store import ReportStore
from src.soql_builder import (
    CROSS_FILTER_LINK_UNVERIFIED,
    STATUS_NONE,
    STATUS_PARTIAL,
)

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

    def __init__(
        self,
        metadata: dict[str, Any],
        errors: dict[str, BaseException],
        describes: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.report = _FakeReportAPI(metadata)
        self._errors = errors
        self._describes = describes or {}

    def describe_object(self, name: str) -> dict[str, Any]:
        error = self._errors.get(name)
        if error is not None:
            raise error
        describe = self._describes.get(name)
        if describe is not None:
            return describe
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


# ── クロスフィルタの副問い合わせ ────────────────────────────────────────


def _task_with_children_describe() -> dict[str, Any]:
    """子リレーションを持つ主オブジェクト（ Task ） の describe 相当。

    副問い合わせの主側項目は ``id`` 型でなければならないので、 ``Id`` を
    持たせる。 ``WhatId`` は ``reference`` 型のまま残す。
    """
    return {
        "name": "Task",
        "label": "活動",
        "custom": False,
        "fields": [
            _field("Id", "活動ID", "id"),
            _field("Subject", "件名"),
            _field("WhatId", "関連先", "reference"),
        ],
        "childRelationships": [
            {
                "childSObject": "TaskComment",
                "field": "ParentId",
                "relationshipName": "Comments",
            }
        ],
    }


def _task_comment_describe() -> dict[str, Any]:
    """子オブジェクト（ TaskComment ） の describe 相当。"""
    return {
        "name": "TaskComment",
        "label": "活動コメント",
        "custom": False,
        "fields": [
            _field("ParentId", "親", "reference"),
            _field("CommentBody", "本文"),
        ],
    }


def _cross_filter_metadata(report_id: str = REPORT_ID) -> dict[str, Any]:
    """クロスフィルタつきのレポート describe 相当。"""
    metadata = _report_metadata(
        report_id,
        detail_columns=("SUBJECT",),
        column_info={"SUBJECT": "件名"},
    )
    # ``make_slim_report`` が残すのは dict の値だけ。 表示名で解決させる
    metadata["reportExtendedMetadata"]["detailColumnInfo"] = {"SUBJECT": {"label": "件名"}}
    metadata["reportMetadata"]["crossFilters"] = [
        {
            "criteria": [{"column": "CommentBody", "operator": "equals", "value": "要対応"}],
            "includesObject": True,
            "primaryEntityField": "Id",
            "relatedEntity": "TaskComment",
            "relatedEntityJoinField": "ParentId",
        }
    ]
    return metadata


def test_rebuild_makes_same_cross_filter_subquery_soql(tmp_path: Path) -> None:
    """クロスフィルタの副問い合わせは、 普通の実行と ``--rebuild`` で同じ SOQL になる。

    子リレーションと子オブジェクトの項目定義は 保存データ に残るため、
    作り直しても同じ副問い合わせCCI を出せる。
    """
    metadata = _cross_filter_metadata()
    csv_path = tmp_path / "data" / "項目定義.csv"

    client = _FakeClient(
        metadata,
        {},
        describes={"Task": _task_with_children_describe(), "TaskComment": _task_comment_describe()},
    )
    normal = run_fetch(
        [_entry()],
        site_for=_fake_site_for(client),
        describe_store=DescribeStore.empty(csv_path),
        report_store=ReportStore.load(csv_path),
        row_limit=False,
        **_fetch_settings(tmp_path, "normal"),
    )[0]

    # 保存データ （ 項目定義と子リレーション） が揃っている
    assert csv_path.exists()
    assert (csv_path.parent / "子リレーション.csv").exists()

    rebuild = run_fetch(
        [_entry()],
        site_for=offline_site_for(_fake_base_site_for(ORG_DOMAIN_URL)),
        describe_store=DescribeStore.load(csv_path),
        report_store=ReportStore.load(csv_path),
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
    # 副問い合わせの SOQL が同じ
    assert normal_record.soql_draft.soql == (
        "SELECT Subject\n"
        "FROM Task\n"
        "WHERE Id IN (SELECT ParentId FROM TaskComment WHERE CommentBody = '要対応')"
    )
    assert rebuild_record.soql_draft.soql == normal_record.soql_draft.soql
    # 取り方も同じ（ REST ）
    assert rebuild_record.soql_draft.use_bulk is False
    assert normal_record.soql_draft.use_bulk == rebuild_record.soql_draft.use_bulk


def test_rebuild_cross_filter_child_object_not_in_objects(tmp_path: Path) -> None:
    """副問い合わせの 子オブジェクトは ``objects`` に入らない。"""
    metadata = _cross_filter_metadata()
    csv_path = tmp_path / "data" / "項目定義.csv"

    client = _FakeClient(
        metadata,
        {},
        describes={"Task": _task_with_children_describe(), "TaskComment": _task_comment_describe()},
    )
    normal = run_fetch(
        [_entry()],
        site_for=_fake_site_for(client),
        describe_store=DescribeStore.empty(csv_path),
        report_store=ReportStore.load(csv_path),
        row_limit=False,
        **_fetch_settings(tmp_path, "normal"),
    )[0]

    record = normal.record
    assert record is not None
    # 子オブジェクトは objects に入らない（関係図・関連表の出力を変えないため）
    assert "TaskComment" not in record.objects
    assert set(record.objects) == {"Task"}
    # 代わりに cross_filter_objects に入る
    assert "TaskComment" in record.cross_filter_objects


def test_rebuild_cross_filter_without_child_describe_is_incomplete(tmp_path: Path) -> None:
    """子の項目定義が保存データに無いときは、 警告が残り クロスフィルタは未完成。"""
    metadata = _cross_filter_metadata()
    csv_path = tmp_path / "data" / "項目定義.csv"

    # 主オブジェクト （ 子リレーションつき ） だけ保存する。 子は無い
    store = DescribeStore.empty(csv_path)
    store.record_api_success(ORG, "Task", _task_with_children_describe())
    report_store = ReportStore.load(csv_path)
    report_store.put(ORG, REPORT_ID, make_slim_report(metadata), ())

    outcome = run_fetch(
        [_entry()],
        site_for=offline_site_for(_fake_base_site_for(ORG_DOMAIN_URL)),
        describe_store=DescribeStore.load(csv_path),
        report_store=report_store,
        row_limit=False,
        allow_refresh=False,
        **_fetch_settings(tmp_path, "rebuild"),
    )[0]

    assert outcome.status == "ok"
    record = outcome.record
    assert record is not None
    # 警告が出る（ 既存どおり）
    assert any(
        "TaskComment" in warning and "保存データに無い" in warning for warning in record.warnings
    )
    # クロスフィルタは未完成 になる
    assert record.soql_draft.status == STATUS_PARTIAL
    assert record.soql_draft.soql.startswith("【未完成】")
    assert any("クロスフィルタ" in message for message in record.soql_draft.missing)
    # 取り方は Bulk のまま
    assert record.soql_draft.use_bulk is True


# ── 保存済みの項目定義から子リレーションを取り直す ──────────────────────


class _CountingClient(_FakeClient):
    """``describe_object`` の呼び出し（ オブジェクト名 ） を記録する偽物。"""

    def __init__(
        self,
        metadata: dict[str, Any],
        errors: dict[str, BaseException],
        describes: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(metadata, errors, describes)
        self.describe_object_calls: list[str] = []

    def describe_object(self, name: str) -> dict[str, Any]:
        self.describe_object_calls.append(name)
        return super().describe_object(name)


class _OfflineCountingClient(_CountingClient):
    """``describe_object`` を呼ぶと 「保存データに無い」 を出す偽物。

    ``--rebuild`` の接続しないクライアントと同じ動き。 何が呼ばれたかを
    数えるため、 呼び出しは記録してから例外を投げる。
    """

    def describe_object(self, name: str) -> dict[str, Any]:
        self.describe_object_calls.append(name)
        raise OfflineDescribeMissing(name)


def _entry2() -> MasterEntry:
    """2 つ目のレポートの管理表の行（ ``run_fetch`` に直接渡す用 ）。"""
    return MasterEntry(
        key="1002",
        summary="別のレポート",
        url=f"{DOMAIN}/{OTHER_REPORT_ID}/view",
        enabled=True,
    )


def _save_saved_data(csv_path: Path, metadata: dict[str, Any]) -> None:
    """レポート定義と主オブジェクトの項目定義を 保存データ に書く。"""
    report_store = ReportStore.load(csv_path)
    report_store.put(ORG, REPORT_ID, make_slim_report(metadata), ())
    # 項目定義.csv だけ（ 子リレーション.csv はまだ無い ）
    upsert_object_rows(csv_path, ORG, "Task", _task_with_children_describe())


def test_cross_filter_refreshes_child_relationships_from_saved_item_definition(
    tmp_path: Path,
) -> None:
    """項目定義.csv だけがある状態でも、 取り直しのあとにクロスフィルタが
    副問い合わせになり、 子リレーション.csv ができる。
    """
    metadata = _cross_filter_metadata()
    csv_path = tmp_path / "data" / "項目定義.csv"
    _save_saved_data(csv_path, metadata)

    client = _CountingClient(
        metadata,
        {},
        describes={"Task": _task_with_children_describe(), "TaskComment": _task_comment_describe()},
    )
    outcome = run_fetch(
        [_entry()],
        site_for=_fake_site_for(client),
        describe_store=DescribeStore.load(csv_path),
        report_store=ReportStore.load(csv_path),
        row_limit=False,
        **_fetch_settings(tmp_path, "normal"),
    )[0]

    assert outcome.status == "ok"
    record = outcome.record
    assert record is not None
    # 主オブジェクトは 1 回だけ API から取り直す
    assert client.describe_object_calls.count("Task") == 1
    # 取り直しのあとに子リレーションの CSV ができる
    child_csv = csv_path.parent / "子リレーション.csv"
    assert child_csv.exists()
    assert "TaskComment" in child_csv.read_text(encoding="utf-8")
    # クロスフィルタは副問い合わせになる
    assert record.soql_draft.soql == (
        "SELECT Subject\n"
        "FROM Task\n"
        "WHERE Id IN (SELECT ParentId FROM TaskComment WHERE CommentBody = '要対応')"
    )
    assert record.soql_draft.use_bulk is False


def test_cross_filter_refresh_is_done_once_for_two_reports_of_same_main_object(
    tmp_path: Path,
) -> None:
    """主オブジェクトが同じレポートが 2 件でも、 取り直しは 1 回だけ。"""
    metadata = _cross_filter_metadata()
    metadata2 = _cross_filter_metadata(OTHER_REPORT_ID)
    csv_path = tmp_path / "data" / "項目定義.csv"
    _save_saved_data(csv_path, metadata)
    report_store = ReportStore.load(csv_path)
    report_store.put(ORG, OTHER_REPORT_ID, make_slim_report(metadata2), ())

    client = _CountingClient(
        metadata,
        {},
        describes={"Task": _task_with_children_describe(), "TaskComment": _task_comment_describe()},
    )
    outcomes = run_fetch(
        [_entry(), _entry2()],
        site_for=_fake_site_for(client),
        describe_store=DescribeStore.load(csv_path),
        report_store=report_store,
        row_limit=False,
        **_fetch_settings(tmp_path, "normal"),
    )

    assert all(outcome.status == "ok" for outcome in outcomes)
    # 主オブジェクトの describe_object は 1 回だけ
    assert client.describe_object_calls.count("Task") == 1
    # 2 件とも副問い合わせになる
    for outcome in outcomes:
        record = outcome.record
        assert record is not None
        assert "IN (SELECT ParentId FROM TaskComment" in record.soql_draft.soql


def test_cross_filter_does_not_call_api_when_child_relationship_csv_exists(
    tmp_path: Path,
) -> None:
    """子リレーション.csv がある次の実行では、 主オブジェクトを取り直さない。"""
    metadata = _cross_filter_metadata()
    csv_path = tmp_path / "data" / "項目定義.csv"
    _save_saved_data(csv_path, metadata)

    # 1 回目: 子リレーション.csv を作る
    first_client = _CountingClient(
        metadata,
        {},
        describes={"Task": _task_with_children_describe(), "TaskComment": _task_comment_describe()},
    )
    run_fetch(
        [_entry()],
        site_for=_fake_site_for(first_client),
        describe_store=DescribeStore.load(csv_path),
        report_store=ReportStore.load(csv_path),
        row_limit=False,
        **_fetch_settings(tmp_path, "first"),
    )
    assert first_client.describe_object_calls.count("Task") == 1
    assert (csv_path.parent / "子リレーション.csv").exists()

    # 2 回目: 保存データ （ 子リレーション.csv 込み ） だけで解決できる
    second_client = _CountingClient(
        metadata,
        {},
        describes={"Task": _task_with_children_describe(), "TaskComment": _task_comment_describe()},
    )
    outcomes = run_fetch(
        [_entry()],
        site_for=_fake_site_for(second_client),
        describe_store=DescribeStore.load(csv_path),
        report_store=ReportStore.load(csv_path),
        row_limit=False,
        **_fetch_settings(tmp_path, "second"),
    )

    assert outcomes[0].status == "ok"
    record = outcomes[0].record
    assert record is not None
    # 主オブジェクトは API から取り直さない
    assert "Task" not in second_client.describe_object_calls
    # 副問い合わせは同じものが出る
    assert "IN (SELECT ParentId FROM TaskComment" in record.soql_draft.soql


def test_rebuild_with_only_item_definition_does_not_call_api(tmp_path: Path) -> None:
    """``--rebuild`` で項目定義.csv だけのときは API を呼ばず、 クロスフィルタは
    missing のまま。
    """
    metadata = _cross_filter_metadata()
    csv_path = tmp_path / "data" / "項目定義.csv"
    _save_saved_data(csv_path, metadata)

    client = _OfflineCountingClient(metadata, {}, {})
    outcome = run_fetch(
        [_entry()],
        site_for=_fake_site_for(client),
        describe_store=DescribeStore.load(csv_path),
        report_store=ReportStore.load(csv_path),
        row_limit=False,
        allow_refresh=False,
        **_fetch_settings(tmp_path, "rebuild"),
    )[0]

    assert outcome.status == "ok"
    record = outcome.record
    assert record is not None
    # 主オブジェクトは API から取らない
    assert "Task" not in client.describe_object_calls
    # クロスフィルタは missing のまま
    assert record.soql_draft.status == STATUS_PARTIAL
    assert record.soql_draft.soql.startswith("【未完成】")
    assert any("クロスフィルタ" in message for message in record.soql_draft.missing)
    assert record.soql_draft.use_bulk is True


def test_cross_filter_refresh_failure_keeps_saved_item_definition(tmp_path: Path) -> None:
    """取り直しが失敗しても、 CSV の値のまま続けて 警告だけ残す。"""
    metadata = _cross_filter_metadata()
    csv_path = tmp_path / "data" / "項目定義.csv"
    _save_saved_data(csv_path, metadata)

    client = _CountingClient(
        metadata,
        {"Task": SalesforceRequestError("GET", "/sobjects/Task/describe", 500, "Boom")},
        describes={"TaskComment": _task_comment_describe()},
    )
    outcome = run_fetch(
        [_entry()],
        site_for=_fake_site_for(client),
        describe_store=DescribeStore.load(csv_path),
        report_store=ReportStore.load(csv_path),
        row_limit=False,
        **_fetch_settings(tmp_path, "normal"),
    )[0]

    assert outcome.status == "ok"
    record = outcome.record
    assert record is not None
    # 取り直しは 1 回だけ
    assert client.describe_object_calls.count("Task") == 1
    # 主オブジェクトは CSV の値のまま
    assert record.main_object == "Task"
    # 失敗の理由が警告に残る
    assert any("Task" in warning and "500" in warning for warning in record.warnings)
    # クロスフィルタは missing のまま
    assert record.soql_draft.status == STATUS_PARTIAL
    assert any("クロスフィルタ" in message for message in record.soql_draft.missing)


# ── 子リレーションの取り直し（ クロスフィルタの無いレポートも ） ──────────


def _run_normal(file_paths: Any, client: Any) -> int:
    """``run()`` を 1 回実行する（ Salesforce に繋ぐ ） 。"""
    from src.run import run

    return run([], site_for=_fake_site_for(client), file_paths=file_paths)


def test_object_without_children_does_not_call_api_on_second_run(tmp_path: Path) -> None:
    """子が 0 件のオブジェクトも、 1 回目に 子リレーション.csv が残るので
    2 回目の実行では API を呼ばない。
    """
    # 列は 1 つだけ（ 全部解決する列にする。 そうしないと 「(不明) の列」 の
    # 取り直しが別に起きる ）
    metadata = _report_metadata(
        detail_columns=("SUBJECT",),
        column_info={"SUBJECT": "件名"},
    )
    # ``make_slim_report`` が残すのは dict の値だけ。 表示名で解決させる
    metadata["reportExtendedMetadata"]["detailColumnInfo"] = {"SUBJECT": {"label": "件名"}}
    csv_path = tmp_path / "data" / "項目定義.csv"

    first_client = _CountingClient(metadata, {}, describes={"Task": _task_describe()})
    run_fetch(
        [_entry()],
        site_for=_fake_site_for(first_client),
        describe_store=DescribeStore.empty(csv_path),
        report_store=ReportStore.load(csv_path),
        row_limit=False,
        **_fetch_settings(tmp_path, "first"),
    )
    assert first_client.describe_object_calls.count("Task") == 1
    # 子が 0 件でも 子リレーション.csv に 1 行残る
    child_csv = csv_path.parent / "子リレーション.csv"
    assert child_csv.exists()
    assert "Task" in child_csv.read_text(encoding="utf-8")

    second_client = _CountingClient(metadata, {}, describes={"Task": _task_describe()})
    outcomes = run_fetch(
        [_entry()],
        site_for=_fake_site_for(second_client),
        describe_store=DescribeStore.load(csv_path),
        report_store=ReportStore.load(csv_path),
        row_limit=False,
        **_fetch_settings(tmp_path, "second"),
    )

    assert outcomes[0].status == "ok"
    # 主オブジェクトは API から取り直さない
    assert "Task" not in second_client.describe_object_calls


def test_report_without_cross_filter_refreshes_child_relationships(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """クロスフィルタの無いレポートでも、 項目定義.csv だけの状態から
    1 回取り直して、 Markdown に子リレーションの表が 出る。
    """
    # 列は 1 つだけ（ 全部解決する列にする。 そうしないと 「(不明) の列」 の
    # 取り直しが別に起きて、 どちらの理由で取り直したのか分からなくなる ）
    metadata = _report_metadata(
        detail_columns=("SUBJECT",),
        column_info={"SUBJECT": "件名"},
    )
    metadata["reportExtendedMetadata"]["detailColumnInfo"] = {"SUBJECT": {"label": "件名"}}
    file_paths = _setup(tmp_path, monkeypatch, _one_report_row())
    _save_report_definition(file_paths, REPORT_ID, metadata)
    # 項目定義.csv だけ（ 子リレーション.csv は無い ）
    _save_object_describe(file_paths, _task_with_children_describe())

    client = _CountingClient(
        metadata,
        {},
        describes={"Task": _task_with_children_describe()},
    )
    assert _run_normal(file_paths, client) == 0

    # 主オブジェクトは 1 回だけ取り直す
    assert client.describe_object_calls.count("Task") == 1
    # 取り直しのあとに 子リレーション.csv ができる
    child_csv = file_paths.describe_csv_path.parent / "子リレーション.csv"
    assert child_csv.exists()
    assert "TaskComment" in child_csv.read_text(encoding="utf-8")
    # Markdown に 子リレーションの表が 出る
    md_text = (tmp_path / "output" / "SOQL資料" / "1001.md").read_text(encoding="utf-8")
    assert "## 子リレーション（サブクエリの材料）" in md_text
    assert "| Comments | TaskComment | ParentId |" in md_text


def test_rebuild_with_old_saved_data_marks_cross_filter_unverified(tmp_path: Path) -> None:
    """``--rebuild`` で 子リレーション.csv が無い（ 古い保存データ ） のときは、
    クロスフィルタの判定が 「確認できず」 になる。
    """
    metadata = _cross_filter_metadata()
    csv_path = tmp_path / "data" / "項目定義.csv"
    _save_saved_data(csv_path, metadata)

    client = _OfflineCountingClient(metadata, {}, {})
    outcome = run_fetch(
        [_entry()],
        site_for=_fake_site_for(client),
        describe_store=DescribeStore.load(csv_path),
        report_store=ReportStore.load(csv_path),
        row_limit=False,
        allow_refresh=False,
        **_fetch_settings(tmp_path, "rebuild"),
    )[0]

    assert outcome.status == "ok"
    record = outcome.record
    assert record is not None
    link = record.soql_draft.cross_filter_links[0]
    assert link.status == CROSS_FILTER_LINK_UNVERIFIED
    assert link.converted is False
    assert "childRelationships が空" in link.reason
