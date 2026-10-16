"""通し（e2e） テスト: ``run_fetch`` → ``run_tables`` → ``SOQL調査.xlsx``
と ``列名の対応.csv`` の検証。

脳内スクラッチパッドの再現シナリオ （ Task / Account / User の偽 describe
と、 列 SUBJECT / ACCOUNT.NAME / OWNER.NAME、 絞り込み 2 件、 組立式
"1 OR 2"、 並び順、 topRows を持つレポート） を ``run_fetch`` から
``run_tables`` まで通して動かし、 xlsx のシートと csv の中身を厳密に確かめる。

ネットワークには出ない。 ``site_for`` を偽のクライアントに差し替える。
"""

from __future__ import annotations

import csv
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.worksheet.table import Table as XlsxTable
from openpyxl.worksheet.table import TableStyleInfo

DOMAIN = "https://example.my.salesforce.com/lightning/r/Report"

# ── ヘルパー ─────────────────────────────────────────────────────────────


def _build_master(path: Path) -> None:
    """``MASTER_XLSX_PATH`` の指す xlsx を作る。"""
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


def _write_ini(tmp_path: Path, master: Path) -> None:
    """テスト用 ``config.ini`` を ``tmp_path`` に書く。 ``[FILES]`` の 7 つの
    キーはすべて必須なので、 このヘルパでは全部書く。
    """
    (tmp_path / "config.ini").write_text(
        "[FILES]\n"
        f"MASTER_XLSX_PATH = {master.name}\n"
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


def _f(
    name: str,
    label: str,
    type_: str = "string",
    ref: list[str] | None = None,
    rel: str | None = None,
) -> dict:
    """``_slim_object`` を通す前の describe 用 ``field`` dict を作る。"""
    return {
        "name": name,
        "label": label,
        "type": type_,
        "custom": False,
        "referenceTo": ref or [],
        "relationshipName": rel,
        "picklistValues": [],
    }


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
        return self._object_describes.get(name, {"name": name, "fields": []})


class _FakeSite:
    def __init__(self, client: _FakeClient) -> None:
        self._client = client

    def __call__(self) -> _FakeSite:
        return self

    def __enter__(self) -> _FakeClient:
        return self._client

    def __exit__(self, *args: object) -> None:
        return None


def _read_xlsx(path: Path) -> dict[str, list[dict[str, object]]]:
    """xlsx を ``{シート名: [行 dict, ...]}`` で読む。

    一覧 / 列の対応 シートは 1 行目=案内、 2 行目=空、 3 行目=テーブル見出し、 4 行目以降=データ
    という構造。 関係図シートは 1 行目=テーブル見出し、 2 行目以降=データ 。
    それぞれ自動的に見つける
    """
    workbook = load_workbook(path)
    result: dict[str, list[dict[str, object]]] = {}
    for sheet_name in workbook.sheetnames:
        sheet = workbook[sheet_name]
        # シートの 1 行目に Excel テーブルの参照があればその見出しを探す
        all_rows = list(sheet.iter_rows(values_only=True))
        # テーブルヘッダーの候補: ある行が文字列リストで、 後の行が同数の値を持つ
        header_idx = -1
        for i, row in enumerate(all_rows):
            if not row:
                continue
            # 全て文字列で埋まっている最初の行
            if all(isinstance(v, str) and v for v in row):
                header_idx = i
                break
        if header_idx < 0:
            result[sheet_name] = []
            continue
        headers = list(all_rows[header_idx])
        data_rows = all_rows[header_idx + 1 :]
        result[sheet_name] = [
            {
                str(header): ("" if value is None else value)
                for header, value in zip(headers, row, strict=False)
            }
            for row in data_rows
        ]
    workbook.close()
    return result


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    """CSV を ``(columns, rows)`` で読む。"""
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        columns = next(reader)
        rows = [dict(zip(columns, line, strict=True)) for line in reader]
    return columns, rows


def _site_for(client: _FakeClient):
    """``site_for(url)`` 互換の関数。"""

    def resolver(url: str) -> _FakeSite:
        return _FakeSite(client)

    return resolver


# ── 共通シナリオ ───────────────────────────────────────────────────────


def _e2e_describe() -> dict:
    """脳内スクラッチパッドの再現シナリオのレポート describe を返す。"""
    return {
        "reportMetadata": {
            "id": "00O000000000001",
            "name": "テスト",
            "reportType": {"type": "Task"},
            "reportFormat": "TABULAR",
            "detailColumns": ["SUBJECT", "ACCOUNT.NAME", "OWNER.NAME"],
            "reportFilters": [
                {"column": "SUBJECT", "operator": "contains", "value": "A"},
                {"column": "ACCOUNT.NAME", "operator": "equals", "value": "B"},
            ],
            "reportBooleanFilter": "1 OR 2",
            "sortBy": [{"sortColumn": "SUBJECT", "sortOrder": "Asc"}],
            "topRows": {"rowLimit": 10, "direction": "Asc"},
        },
        "reportExtendedMetadata": {
            "detailColumnInfo": {
                "SUBJECT": {"label": "件名"},
                "ACCOUNT.NAME": {"label": "取引先名"},
                "OWNER.NAME": {"label": "氏名"},
            }
        },
    }


def _e2e_object_describes() -> dict[str, dict]:
    """``Task`` / ``Account`` / ``User`` の偽 describe を返す。"""
    return {
        "Task": {
            "name": "Task",
            "label": "活動",
            "custom": False,
            "fields": [
                _f("Subject", "件名"),
                _f("WhatId", "関連先", "reference", ["Account"], "What"),
                _f("OwnerId", "担当者", "reference", ["User"], "Owner"),
            ],
        },
        "Account": {
            "name": "Account",
            "label": "取引先",
            "custom": False,
            "fields": [
                _f("Name", "取引先名"),
                _f("Phone", "電話", "phone"),
                _f("OwnerId", "所有者", "reference", ["User"], "Owner"),
                _f("ParentId", "親", "reference", ["Account"], "Parent"),
            ],
        },
        "User": {
            "name": "User",
            "label": "ユーザ",
            "custom": False,
            "fields": [
                _f("Name", "氏名"),
                _f("Email", "メール", "email"),
                _f("ManagerId", "マネージャ", "reference", ["User"], "Manager"),
            ],
        },
    }


def _run_e2e(tmp_path: Path) -> Path:
    """再現シナリオを実行し、 出力ディレクトリを返す。"""
    from src.fetch import run_fetch
    from src.master import MasterEntry
    from src.tables import run_tables

    master = tmp_path / "master.xlsx"
    _build_master(master)
    _write_ini(tmp_path, master)

    file_paths = _load_file_paths_for_test(tmp_path)
    output_dir = tmp_path / "output"

    client = _FakeClient(_e2e_describe(), _e2e_object_describes())

    entry = MasterEntry(
        key="1001",
        summary="テスト",
        url=f"{DOMAIN}/00O000000000001/view",
        enabled=True,
    )
    outcomes = run_fetch(
        [entry],
        describe_csv_path=file_paths.describe_csv_path,
        site_for=_site_for(client),
    )
    assert all(o.status == "ok" for o in outcomes), [(o.status, o.error) for o in outcomes]
    run_tables(output_dir, [o.record for o in outcomes if o.record])
    return output_dir


def _load_file_paths_for_test(tmp_path: Path):
    """``config.ini`` を ``tmp_path`` プロジェクトに見立てて ``FilePaths`` を読み込む。"""
    import sys

    from comken_state import reset_cached_config

    from src.run import read_file_paths

    reset_cached_config()
    original_argv = sys.argv
    sys.argv = [str(tmp_path / "fake_main.py")]
    try:
        return read_file_paths()
    finally:
        sys.argv = original_argv
        reset_cached_config()


# ── テスト本体 ───────────────────────────────────────────────────────────


def test_e2e_columns_sheet_項目_is_resolved(tmp_path: Path) -> None:
    """``列の対応`` シートで ``SUBJECT`` / ``ACCOUNT.NAME`` / ``OWNER.NAME`` が解決されて出る。"""
    output_dir = _run_e2e(tmp_path)

    sheets = _read_xlsx(output_dir / "SOQL調査.xlsx")
    rows = sheets["列の対応"]
    by_key = {row["列キー"]: row for row in rows}

    assert by_key["SUBJECT"]["使うSOQL列名"] == "Subject"
    assert by_key["ACCOUNT.NAME"]["使うSOQL列名"] == "What.Name"
    assert by_key["OWNER.NAME"]["使うSOQL列名"] == "Owner.Name"
    for k in ("SUBJECT", "ACCOUNT.NAME", "OWNER.NAME"):
        assert by_key[k]["状態"] == "OK"


def test_e2e_aggregate_table_sort_by_and_top_rows(tmp_path: Path) -> None:
    """``ACCOUNT.NAME`` → ``What.Name`` のように関連経由の解決は ``並び順`` ではなく
    ``列の対応`` シート側で見る （集計シートは廃止済み）。"""
    output_dir = _run_e2e(tmp_path)
    sheets = _read_xlsx(output_dir / "SOQL調査.xlsx")
    # 3 枚構成
    assert list(sheets.keys())[:3] == ["一覧", "列の対応", "関係図"]


def test_e2e_field_table_段_is_filled(tmp_path: Path) -> None:
    """関係図シートに Task → Account / User の木が出る。"""
    output_dir = _run_e2e(tmp_path)
    sheets = _read_xlsx(output_dir / "SOQL調査.xlsx")
    rows = sheets["関係図"]
    # 主オブジェクト Task
    task_rows = [r for r in rows if r["主オブジェクト"] == "Task"]
    assert any(not r["1段目"] for r in task_rows)
    # 1 段目の列に Account と User が出る（「オブジェクト（リレーション名）」の形）
    targets = {r["1段目"].split("（")[0] for r in task_rows if r["1段目"]}
    assert "Account" in targets
    assert "User" in targets


def test_e2e_related_table(tmp_path: Path) -> None:
    """関係図シートは、 深い段ほど右の「N 段目」の列に出る。"""
    output_dir = _run_e2e(tmp_path)
    sheets = _read_xlsx(output_dir / "SOQL調査.xlsx")
    rows = sheets["関係図"]
    # e2e は 1〜2 段までの関係を含む （Account.Owner 経由で 2 段目が出る）
    for row in rows:
        assert "3段目" not in row
        filled = [name for name in ("1段目", "2段目") if row.get(name)]
        assert len(filled) <= 1  # 1 行に入るノードは 1 つ（深さの列だけ埋まる）


def test_e2e_column_names_csv(tmp_path: Path) -> None:
    """``列名の対応.csv`` は解決できた列だけが出る (管理番号列を持たない)。"""
    output_dir = _run_e2e(tmp_path)
    columns, rows = _read_csv(output_dir / "列名の対応.csv")
    assert "管理番号" not in columns
    assert "概要" not in columns
    soql_names = {row["SOQL列名"] for row in rows}
    assert soql_names == {"Subject", "What.Name", "Owner.Name"}
    for row in rows:
        assert row["SOQL列名"] != "(不明)"


def test_e2e_column_names_xlsx_resolved_correspondence(tmp_path: Path) -> None:
    """``列の対応`` シートにも同じ解決済み列が出る。"""
    output_dir = _run_e2e(tmp_path)
    sheets = _read_xlsx(output_dir / "SOQL調査.xlsx")
    rows = sheets["列の対応"]
    soql_names = {row["使うSOQL列名"] for row in rows}
    assert "Subject" in soql_names
    assert "What.Name" in soql_names
    assert "Owner.Name" in soql_names


# ── 集計 / 条件 シートは廃止済み ───────────────────────────────
# 集計 / 条件 / グループ・並び順は 「列の対応」 シートで SOQL として表現し、
# SOQL資料/{管理番号}.md に集約した。 旧テストは削除。


def test_e2e_columns_sheet_with_summary_report(tmp_path: Path) -> None:
    """SUMMARY / MATRIX レポートでも列の対応シートは作られ、 バケット等の
    集計系は SOQL として表現されない。 代わりに Markdown に集約される。
    """
    from src.fetch import run_fetch
    from src.master import MasterEntry
    from src.tables import run_tables

    master = tmp_path / "master.xlsx"
    _build_master(master)
    _write_ini(tmp_path, master)
    file_paths = _load_file_paths_for_test(tmp_path)
    output_dir = tmp_path / "output"

    describe = {
        "reportMetadata": {
            "reportType": {"type": "Task"},
            "reportFormat": "MATRIX",
            "detailColumns": ["SUBJECT"],
            "groupingsDown": [
                {"name": "Opp.CloseDate", "sortOrder": "Asc"},
            ],
            "aggregates": ["s!Opp.Amount"],
        },
        "reportExtendedMetadata": {
            "detailColumnInfo": {"SUBJECT": {"label": "件名"}},
        },
    }
    client = _FakeClient(describe, _e2e_object_describes())
    entry = MasterEntry(
        key="1001",
        summary="テスト",
        url=f"{DOMAIN}/00O000000000001/view",
        enabled=True,
    )
    outcomes = run_fetch(
        [entry],
        describe_csv_path=file_paths.describe_csv_path,
        site_for=_site_for(client),
    )
    run_tables(output_dir, [o.record for o in outcomes if o.record])

    sheets = _read_xlsx(output_dir / "SOQL調査.xlsx")
    # 3 枚構成
    assert "集計" not in sheets
    assert "条件" not in sheets
    # 列の対応シートに SUBJECT の行がある
    rows = sheets["列の対応"]
    subject_rows = [r for r in rows if r["列キー"] == "SUBJECT"]
    assert subject_rows
    # 使う SOQL 列名に Subject が入る
    assert subject_rows[0]["使うSOQL列名"] == "Subject"


def test_e2e_columns_sheet_top_rows_is_human_readable(tmp_path: Path) -> None:
    """``topRows`` の dict が ``"N件 direction"`` の形に整形される。"""
    from src.fetch import run_fetch
    from src.master import MasterEntry
    from src.tables import run_tables

    master = tmp_path / "master.xlsx"
    _build_master(master)
    _write_ini(tmp_path, master)
    file_paths = _load_file_paths_for_test(tmp_path)
    output_dir = tmp_path / "output"

    describe = {
        "reportMetadata": {
            "reportType": {"type": "Task"},
            "reportFormat": "TABULAR",
            "detailColumns": ["SUBJECT"],
            "topRows": {"rowLimit": 10, "direction": "Asc"},
        },
        "reportExtendedMetadata": {
            "detailColumnInfo": {"SUBJECT": {"label": "件名"}},
        },
    }
    client = _FakeClient(describe, _e2e_object_describes())
    entry = MasterEntry(
        key="1001",
        summary="テスト",
        url=f"{DOMAIN}/00O000000000001/view",
        enabled=True,
    )
    outcomes = run_fetch(
        [entry],
        describe_csv_path=file_paths.describe_csv_path,
        site_for=_site_for(client),
    )
    run_tables(output_dir, [o.record for o in outcomes if o.record])

    # topRows は Markdown に出るので、 ファイルの中身を覗く
    md_path = output_dir / "SOQL資料" / "1001.md"
    content = md_path.read_text(encoding="utf-8")
    assert "10件 Asc" in content
