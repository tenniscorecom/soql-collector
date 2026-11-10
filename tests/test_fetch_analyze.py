"""取得と解析を分けた実行のテスト。

偽の client で ``run()`` を実際に通し、 API の呼び出し回数と、 保存・出力のファイルを見る。
"""

from __future__ import annotations

import csv
import io
import json
import logging
from pathlib import Path
from typing import Any

import pytest
from comken.exceptions import (
    SalesforceReportFormatError,
    SalesforceReportTruncatedError,
    SalesforceRequestError,
)
from openpyxl import Workbook, load_workbook
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table as XlsxTable
from openpyxl.worksheet.table import TableStyleInfo

from src.ai_markdown import AI_FILE_MAX_BYTES, split_markdown
from src.column_fix_xlsx import COLUMN_FIX_COLUMNS
from src.run import FilePaths, run
from src.tables.ai_csv import write_limited_csv

ORG = "example.my.salesforce.com"
DOMAIN_URL = f"https://{ORG}"
REPORT_URL = f"{DOMAIN_URL}/lightning/r/Report"
REPORT_A = "00O5g00000AAAAA"
REPORT_B = "00O5g00000BBBBB"


# ── 偽物 ─────────────────────────────────────────────────────────────────


def _field(name: str, label: str, type_: str = "string", **extra: Any) -> dict[str, Any]:
    return {
        "name": name,
        "label": label,
        "type": type_,
        "custom": False,
        "referenceTo": [],
        "relationshipName": None,
        "picklistValues": [],
        "filterable": True,
        "sortable": True,
        **extra,
    }


def _reference(name: str, label: str, target: str, relationship: str) -> dict[str, Any]:
    return _field(name, label, "reference", referenceTo=[target], relationshipName=relationship)


def _task() -> dict[str, Any]:
    return {
        "name": "Task",
        "label": "活動",
        "custom": False,
        "recordTypeInfos": [{"name": "絞ると落ちるキー"}],
        "childRelationships": [
            {"childSObject": "TaskComment", "field": "ParentId", "relationshipName": "Comments"}
        ],
        "fields": [
            _field("Id", "活動ID", "id"),
            _field("Subject", "件名"),
            _reference("OwnerId", "担当者ID", "User", "Owner"),
        ],
    }


def _user() -> dict[str, Any]:
    return {
        "name": "User",
        "label": "ユーザー",
        "custom": False,
        "fields": [_field("Id", "ID", "id"), _field("Email", "メール")],
    }


def _task_comment() -> dict[str, Any]:
    return {
        "name": "TaskComment",
        "label": "活動コメント",
        "custom": False,
        "fields": [_reference("ParentId", "親", "Task", "Parent"), _field("CommentBody", "本文")],
    }


def _report(report_id: str = REPORT_A) -> dict[str, Any]:
    return {
        "reportMetadata": {
            "id": report_id,
            "name": "活動レポート",
            "reportFormat": "TABULAR",
            "reportType": {"type": "Task"},
            "detailColumns": ["SUBJECT"],
            "reportFilters": [],
            "sortBy": [],
            "groupingsDown": [],
            "groupingsAcross": [],
            "aggregates": [],
            "絞ると落ちるキー": "残る",
        },
        "reportExtendedMetadata": {"detailColumnInfo": {"SUBJECT": {"label": "件名"}}},
    }


def _not_found(name: str) -> SalesforceRequestError:
    return SalesforceRequestError("GET", f"/{name}", 404, "Not Found")


class _CountingReportAPI:
    def __init__(self, parent: _CountingClient) -> None:
        self._parent = parent

    def describe(self, report_id: str) -> dict[str, Any]:
        self._parent.report_describe_calls.append(report_id)
        result = self._parent.reports[report_id]
        if isinstance(result, BaseException):
            raise result
        return result

    def get(self, report_id: str, *args: Any, **kwargs: Any) -> list[dict[str, str]]:
        self._parent.report_get_calls.append(report_id)
        result = self._parent.row_results.get(report_id, 1500)
        if isinstance(result, BaseException):
            raise result
        return [{"行": "x"}] * result


class _CountingClient:
    """呼び出しを数える偽物。"""

    def __init__(
        self,
        reports: dict[str, Any] | None = None,
        objects: dict[str, Any] | None = None,
        row_results: dict[str, Any] | None = None,
    ) -> None:
        self.reports = reports if reports is not None else {REPORT_A: _report()}
        self.objects = objects if objects is not None else {"Task": _task(), "User": _user()}
        self.row_results = row_results or {}
        self.report_describe_calls: list[str] = []
        self.report_get_calls: list[str] = []
        self.describe_object_calls: list[str] = []
        self.report = _CountingReportAPI(self)

    def describe_object(self, name: str) -> dict[str, Any]:
        self.describe_object_calls.append(name)
        result = self.objects.get(name)
        if result is None:
            raise _not_found(name)
        if isinstance(result, BaseException):
            raise result
        return result

    def total_calls(self) -> int:
        return (
            len(self.report_describe_calls)
            + len(self.report_get_calls)
            + len(self.describe_object_calls)
        )


def _site_for(client: _CountingClient) -> Any:
    class _Site:
        DOMAIN_URL = DOMAIN_URL

        def __enter__(self) -> _CountingClient:
            return client

        def __exit__(self, *args: object) -> None:
            return None

    return lambda url: _Site


def _must_not_connect_site_for() -> Any:
    """クラスを作るとテストが落ちる ``site_for`` （作る = ログインする）。"""

    class _Site:
        DOMAIN_URL = DOMAIN_URL

        def __init__(self) -> None:
            raise AssertionError("Salesforce に接続してはいけない")

    return lambda url: _Site


# ── 準備 ─────────────────────────────────────────────────────────────────


def _build_master(path: Path, report_ids: tuple[str, ...]) -> None:
    workbook = Workbook()
    active = workbook.active
    assert active is not None
    workbook.remove(active)
    sheet = workbook.create_sheet("PY_管理表")
    headers = ["ID", "概要", "Salesforce URL", "有効"]
    sheet.append(headers)
    for index, report_id in enumerate(report_ids, start=1):
        sheet.append([str(1000 + index), f"概要{index}", f"{REPORT_URL}/{report_id}/view", "○"])
    table = XlsxTable(
        displayName="PY_T_ReportEntry",
        ref=f"A1:{get_column_letter(len(headers))}{len(report_ids) + 1}",
    )
    table.tableStyleInfo = TableStyleInfo(name="TableStyleMedium2", showRowStripes=True)
    sheet.add_table(table)
    workbook.save(path)


def _setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    report_ids: tuple[str, ...] = (REPORT_A,),
    *,
    row_limit: str = "○",
) -> FilePaths:
    from comken_state import reset_cached_config

    tmp_path.mkdir(parents=True, exist_ok=True)
    _build_master(tmp_path / "master.xlsx", report_ids)
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
        "RELATED_DEPTH = 1\n"
        "RELATED_MAX = 40\n"
        f"ROW_LIMIT_CHECK = {row_limit}\n"
        "SIMILAR_COLUMN_THRESHOLD = 0.8\n"
        "[PII]\n"
        "KEYWORDS = 氏名, 名前\n"
        "PERSON_OBJECTS = Contact, Lead\n",
        encoding="utf-8",
    )
    reset_cached_config()
    monkeypatch.setattr("sys.argv", [str(tmp_path / "fake_main.py")])
    return FilePaths(
        master_xlsx_path=tmp_path / "master.xlsx",
        output_dir=tmp_path / "output",
        describe_csv_path=tmp_path / "data" / "項目定義.csv",
        column_fix_xlsx_path=tmp_path / "input" / "列の直し.xlsx",
        xlsx_name="SOQL調査.xlsx",
        markdown_dir="SOQL資料",
        diagram_dir="関係図",
    )


def _write_column_fix(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    active = workbook.active
    assert active is not None
    workbook.remove(active)
    sheet = workbook.create_sheet("列の直し")
    sheet.append(list(COLUMN_FIX_COLUMNS))
    for row in rows:
        sheet.append([row.get(column, "") for column in COLUMN_FIX_COLUMNS])
    table = XlsxTable(
        displayName="ColumnFix",
        ref=f"A1:{get_column_letter(len(COLUMN_FIX_COLUMNS))}{len(rows) + 1}",
    )
    table.tableStyleInfo = TableStyleInfo(name="TableStyleMedium2", showRowStripes=True)
    sheet.add_table(table)
    workbook.save(path)


def _raw_dir(file_paths: FilePaths) -> Path:
    return file_paths.describe_csv_path.parent / "元データ"


def _snapshot(folder: Path) -> dict[str, tuple[bytes, int]]:
    return {
        path.relative_to(folder).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in folder.rglob("*")
        if path.is_file()
    }


def _original(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))["原本"]


def _analyze(file_paths: FilePaths) -> int:
    return run(["--analyze"], site_for=_must_not_connect_site_for(), file_paths=file_paths)


def _outputs_without_times(output_dir: Path) -> dict[str, str]:
    """生成 .py・Markdown・列名の対応・AI チャット用を、 日時の行を除いて読む。"""
    result: dict[str, str] = {}
    for path in sorted(output_dir.rglob("*")):
        if path.suffix not in {".py", ".md", ".csv"}:
            continue
        lines = [
            line
            for line in path.read_text(encoding="utf-8-sig").splitlines()
            if "元データの取得日時" not in line
        ]
        if path.suffix == ".csv" and "項目定義" in path.parts:
            lines = [line.rsplit(",", 1)[0] for line in lines]  # 最後の列が取得日時
        if path.name == "列名の対応.csv":
            lines = [line.rsplit(",", 1)[0] for line in lines]  # 最後の列が更新日
        result[path.relative_to(output_dir).as_posix()] = "\n".join(lines)
    return result


# ── 取得の段 ─────────────────────────────────────────────────────────────


def test_first_fetch_saves_originals_and_makes_no_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file_paths = _setup(tmp_path, monkeypatch)
    client = _CountingClient()

    assert run(["--fetch"], site_for=_site_for(client), file_paths=file_paths) == 0

    assert not file_paths.output_dir.exists()
    raw_dir = _raw_dir(file_paths)
    report = _original(raw_dir / "レポート" / f"{ORG}__{REPORT_A}.json")
    assert report["reportMetadata"]["絞ると落ちるキー"] == "残る"
    task = _original(raw_dir / "オブジェクト" / f"{ORG}__task.json")
    assert task["recordTypeInfos"] == [{"name": "絞ると落ちるキー"}]
    assert (raw_dir / "オブジェクト" / f"{ORG}__user.json").exists()
    count_text = (raw_dir / "件数.csv").read_text(encoding="utf-8-sig")
    assert "超えていない" in count_text
    assert "1500" in count_text
    # 行データは保存しない
    assert '"行"' not in count_text


def test_second_fetch_calls_no_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    file_paths = _setup(tmp_path, monkeypatch)
    assert run(["--fetch"], site_for=_site_for(_CountingClient()), file_paths=file_paths) == 0

    second = _CountingClient()
    assert run(["--fetch"], site_for=_site_for(second), file_paths=file_paths) == 0

    assert second.total_calls() == 0


def test_force_calls_everything_again_but_each_object_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file_paths = _setup(tmp_path, monkeypatch, (REPORT_A, REPORT_B))
    reports = {REPORT_A: _report(REPORT_A), REPORT_B: _report(REPORT_B)}
    first = _CountingClient(reports=reports)
    assert run(["--fetch"], site_for=_site_for(first), file_paths=file_paths) == 0

    forced = _CountingClient(reports=reports)
    forced.objects["Task"] = {**_task(), "label": "新しい表示名"}
    assert run(["--fetch", "--force"], site_for=_site_for(forced), file_paths=file_paths) == 0

    assert sorted(forced.report_describe_calls) == [REPORT_A, REPORT_B]
    assert sorted(forced.report_get_calls) == [REPORT_A, REPORT_B]
    assert forced.describe_object_calls.count("Task") == 1
    assert forced.describe_object_calls.count("User") == 1
    task = _original(_raw_dir(file_paths) / "オブジェクト" / f"{ORG}__task.json")
    assert task["label"] == "新しい表示名"


def test_api_failure_is_not_saved_and_is_tried_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file_paths = _setup(tmp_path, monkeypatch)
    first = _CountingClient(objects={"Task": _task()})  # User は 404
    assert run(["--fetch"], site_for=_site_for(first), file_paths=file_paths) == 0
    assert first.describe_object_calls.count("User") == 1
    assert not (_raw_dir(file_paths) / "オブジェクト" / f"{ORG}__user.json").exists()

    second = _CountingClient()
    assert run(["--fetch"], site_for=_site_for(second), file_paths=file_paths) == 0
    assert second.describe_object_calls == ["User"]
    assert (_raw_dir(file_paths) / "オブジェクト" / f"{ORG}__user.json").exists()


def test_report_404_is_not_saved_and_fetch_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file_paths = _setup(tmp_path, monkeypatch)
    client = _CountingClient(reports={REPORT_A: _not_found(REPORT_A)})

    assert run(["--fetch"], site_for=_site_for(client), file_paths=file_paths) == 1

    assert not (_raw_dir(file_paths) / "レポート" / f"{ORG}__{REPORT_A}.json").exists()
    good = _CountingClient()
    assert run(["--fetch"], site_for=_site_for(good), file_paths=file_paths) == 0
    assert good.report_describe_calls == [REPORT_A]


def test_transient_row_count_error_is_not_saved_but_format_error_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """通信エラーは取り直す。 形式のせいで判定できないものは何度取っても同じなので保存する。"""
    file_paths = _setup(tmp_path, monkeypatch)
    first = _CountingClient(row_results={REPORT_A: RuntimeError("一時的な失敗")})
    assert run(["--fetch"], site_for=_site_for(first), file_paths=file_paths) == 0

    second = _CountingClient(
        row_results={REPORT_A: SalesforceReportFormatError(REPORT_A, "MULTI_BLOCK")}
    )
    assert run(["--fetch"], site_for=_site_for(second), file_paths=file_paths) == 0
    assert second.report_get_calls == [REPORT_A]

    third = _CountingClient()
    assert run(["--fetch"], site_for=_site_for(third), file_paths=file_paths) == 0
    assert third.report_get_calls == []


def test_fetch_does_not_get_row_count_when_row_limit_check_is_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file_paths = _setup(tmp_path, monkeypatch, row_limit="×")
    client = _CountingClient()

    assert run(["--fetch"], site_for=_site_for(client), file_paths=file_paths) == 0

    assert client.report_get_calls == []
    assert not (_raw_dir(file_paths) / "件数.csv").exists()


def test_fetch_prunes_reports_not_in_master(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    both = _setup(tmp_path, monkeypatch, (REPORT_A, REPORT_B))
    reports = {REPORT_A: _report(REPORT_A), REPORT_B: _report(REPORT_B)}
    assert (
        run(["--fetch"], site_for=_site_for(_CountingClient(reports=reports)), file_paths=both) == 0
    )
    report_b_path = _raw_dir(both) / "レポート" / f"{ORG}__{REPORT_B}.json"
    assert report_b_path.exists()

    # 解析では消えない
    only_a = _setup(tmp_path, monkeypatch, (REPORT_A,))
    assert _analyze(only_a) == 0
    assert report_b_path.exists()

    # 取得で消える
    assert run(["--fetch"], site_for=_site_for(_CountingClient()), file_paths=only_a) == 0
    assert not report_b_path.exists()
    assert REPORT_B not in (_raw_dir(only_a) / "件数.csv").read_text(encoding="utf-8-sig")


def test_fetch_saves_object_on_column_fix_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """列の直しに書いた経路のオブジェクトも保存する（解析の段で要るため）。"""
    file_paths = _setup(tmp_path, monkeypatch)
    _write_column_fix(
        file_paths.column_fix_xlsx_path,
        [
            {
                "状態": "要対応",
                "主オブジェクト": "Task",
                "表示名": "件名",
                "列キー": "SUBJECT",
                "直したSOQL列名": "Owner.Manager.Email",
            }
        ],
    )
    user = _user()
    user["fields"].append(_reference("ManagerId", "上司ID", "Manager__c", "Manager"))
    manager = {
        "name": "Manager__c",
        "label": "上司",
        "custom": True,
        "fields": [_field("Email", "メール")],
    }
    client = _CountingClient(objects={"Task": _task(), "User": user, "Manager__c": manager})

    assert run(["--fetch"], site_for=_site_for(client), file_paths=file_paths) == 0

    assert (_raw_dir(file_paths) / "オブジェクト" / f"{ORG}__manager__c.json").exists()
    # 保存だけで同じ経路が解ける
    assert _analyze(file_paths) == 0
    text = (file_paths.output_dir / "SOQL資料" / "1001.md").read_text(encoding="utf-8")
    assert "Owner.Manager.Email" in text


def test_fetch_saves_cross_filter_child(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    file_paths = _setup(tmp_path, monkeypatch)
    report = _report()
    report["reportMetadata"]["crossFilters"] = [
        {
            "criteria": [],
            "includesObject": True,
            "primaryEntityField": "Id",
            "relatedEntity": "TaskComment",
            "relatedEntityJoinField": "ParentId",
        }
    ]
    client = _CountingClient(
        reports={REPORT_A: report},
        objects={"Task": _task(), "User": _user(), "TaskComment": _task_comment()},
    )

    assert run(["--fetch"], site_for=_site_for(client), file_paths=file_paths) == 0

    assert (_raw_dir(file_paths) / "オブジェクト" / f"{ORG}__taskcomment.json").exists()
    assert _analyze(file_paths) == 0
    text = (file_paths.output_dir / "SOQL資料" / "1001.md").read_text(encoding="utf-8")
    assert "Id IN (SELECT ParentId FROM TaskComment)" in text
    assert "項目定義/TaskComment.csv" in text


def test_fetch_saves_child_of_child_base_form(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file_paths = _setup(tmp_path, monkeypatch)
    report = _report()
    report["reportMetadata"]["detailColumns"] = ["SUBJECT", "TaskComment.CommentBody"]
    report["reportExtendedMetadata"]["detailColumnInfo"]["TaskComment.CommentBody"] = {
        "label": "本文"
    }
    client = _CountingClient(
        reports={REPORT_A: report},
        objects={"Task": _task(), "User": _user(), "TaskComment": _task_comment()},
    )

    assert run(["--fetch"], site_for=_site_for(client), file_paths=file_paths) == 0

    assert (_raw_dir(file_paths) / "オブジェクト" / f"{ORG}__taskcomment.json").exists()
    assert _analyze(file_paths) == 0
    text = (file_paths.output_dir / "SOQL資料" / "1001.md").read_text(encoding="utf-8")
    assert "FROM TaskComment" in text


# ── 解析の段 ─────────────────────────────────────────────────────────────


def test_analyze_connects_to_nothing_and_changes_no_saved_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file_paths = _setup(tmp_path, monkeypatch)
    assert run(["--fetch"], site_for=_site_for(_CountingClient()), file_paths=file_paths) == 0
    before = _snapshot(_raw_dir(file_paths))

    # サイトクラスを作ると AssertionError で落ちる
    assert _analyze(file_paths) == 0

    assert _snapshot(_raw_dir(file_paths)) == before
    assert (file_paths.output_dir / "SOQL調査.xlsx").exists()
    assert list((file_paths.output_dir / "SOQLレポート").glob("*.py"))


def test_run_gives_same_output_as_fetch_then_analyze(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    one_shot = _setup(tmp_path / "a", monkeypatch)
    assert run([], site_for=_site_for(_CountingClient()), file_paths=one_shot) == 0
    one_shot_output = _outputs_without_times(one_shot.output_dir)

    separate = _setup(tmp_path / "b", monkeypatch)
    assert run(["--fetch"], site_for=_site_for(_CountingClient()), file_paths=separate) == 0
    assert _analyze(separate) == 0
    separate_output = _outputs_without_times(separate.output_dir)

    assert one_shot_output
    assert any(name.endswith(".py") for name in one_shot_output)
    assert any(name.startswith("SOQL資料/") for name in one_shot_output)
    assert "列名の対応.csv" in one_shot_output
    assert one_shot_output == separate_output


def test_analyze_twice_gives_same_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    file_paths = _setup(tmp_path, monkeypatch)
    assert run(["--fetch"], site_for=_site_for(_CountingClient()), file_paths=file_paths) == 0

    assert _analyze(file_paths) == 0
    first = {
        path.relative_to(file_paths.output_dir).as_posix(): path.read_bytes()
        for path in file_paths.output_dir.rglob("*")
        if path.suffix in {".py", ".md", ".csv"}
    }
    assert _analyze(file_paths) == 0
    second = {
        path.relative_to(file_paths.output_dir).as_posix(): path.read_bytes()
        for path in file_paths.output_dir.rglob("*")
        if path.suffix in {".py", ".md", ".csv"}
    }

    assert first == second


@pytest.mark.parametrize(
    ("row_result", "expected_cell", "expected_rows"),
    [
        (1500, "超えていない", "1500"),
        (SalesforceReportTruncatedError(REPORT_A, 2000), "超えている", ""),
        (SalesforceReportFormatError(REPORT_A, "MULTI_BLOCK"), "判定不可", ""),
    ],
)
def test_analyze_restores_all_row_check_statuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    row_result: Any,
    expected_cell: str,
    expected_rows: str,
) -> None:
    file_paths = _setup(tmp_path, monkeypatch)
    client = _CountingClient(row_results={REPORT_A: row_result})
    assert run(["--fetch"], site_for=_site_for(client), file_paths=file_paths) == 0

    assert _analyze(file_paths) == 0

    overview = load_workbook(file_paths.output_dir / "SOQL調査.xlsx")["一覧"]
    rows = list(overview.values)
    header_index = next(i for i, row in enumerate(rows) if "管理番号" in row)
    header = rows[header_index]
    row = dict(zip(header, rows[header_index + 1], strict=True))
    assert expected_cell in str(row["2000件超"])
    assert str(row["行数"] or "") == expected_rows


def test_analyze_makes_soql_for_report_of_1000_rows_or_less(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """件数が 1000 件以下でも SOQL を作る。"""
    file_paths = _setup(tmp_path, monkeypatch)
    client = _CountingClient(row_results={REPORT_A: 1000})
    assert run(["--fetch"], site_for=_site_for(client), file_paths=file_paths) == 0

    assert _analyze(file_paths) == 0

    assert list((file_paths.output_dir / "SOQLレポート").glob("*.py"))
    text = (file_paths.output_dir / "SOQL資料" / "1001.md").read_text(encoding="utf-8")
    assert "```sql" in text


def test_analyze_fails_when_report_is_missing_and_warns_when_object_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    file_paths = _setup(tmp_path, monkeypatch, (REPORT_A, REPORT_B))
    reports = {REPORT_A: _report(REPORT_A), REPORT_B: _not_found(REPORT_B)}
    client = _CountingClient(reports=reports, objects={"Task": _task()})  # User も無い
    assert run(["--fetch"], site_for=_site_for(client), file_paths=file_paths) == 1

    caplog.clear()
    assert _analyze(file_paths) == 1

    assert "保存データに無いので 取得.bat で取ってください" in caplog.text
    assert "オブジェクト User は保存データに無い" in caplog.text
    assert (file_paths.output_dir / "SOQL資料" / "1001.md").exists()
    assert not (file_paths.output_dir / "SOQL資料" / "1002.md").exists()


def test_run_analyzes_what_is_saved_and_exit_code_reflects_fetch_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file_paths = _setup(tmp_path, monkeypatch, (REPORT_A, REPORT_B))
    reports = {REPORT_A: _report(REPORT_A), REPORT_B: _not_found(REPORT_B)}

    assert run([], site_for=_site_for(_CountingClient(reports=reports)), file_paths=file_paths) == 1

    assert (file_paths.output_dir / "SOQL資料" / "1001.md").exists()


def test_fetch_logs_api_call_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    file_paths = _setup(tmp_path, monkeypatch)

    with caplog.at_level(logging.INFO, logger="src.run"):
        assert run(["--fetch"], site_for=_site_for(_CountingClient()), file_paths=file_paths) == 0

    assert (
        "API 呼び出し: レポート describe=1 回 / 件数=1 回 / オブジェクト describe=2 回"
        in caplog.text
    )


def test_markdown_shows_when_the_data_was_fetched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file_paths = _setup(tmp_path, monkeypatch)
    assert run(["--fetch"], site_for=_site_for(_CountingClient()), file_paths=file_paths) == 0
    assert _analyze(file_paths) == 0

    line = next(
        line
        for line in (file_paths.output_dir / "SOQL資料" / "1001.md")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.startswith("- 元データの取得日時:")
    )
    assert "不明" not in line
    assert line.count("+00:00") == 3


@pytest.mark.parametrize(
    "argv", [["x"], ["--force"], ["--force", "--fetch"], ["--fetch", "--analyze"]]
)
def test_unknown_arguments_exit_2_without_connecting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    file_paths = _setup(tmp_path, monkeypatch)
    assert run(argv, site_for=_must_not_connect_site_for(), file_paths=file_paths) == 2
    assert not _raw_dir(file_paths).exists()


# ── AI チャット用 ─────────────────────────────────────────────────────────


def test_ai_chat_files_are_made_on_analyze_not_on_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file_paths = _setup(tmp_path, monkeypatch)
    assert run(["--fetch"], site_for=_site_for(_CountingClient()), file_paths=file_paths) == 0
    ai_dir = file_paths.output_dir / "AIチャット用"
    assert not ai_dir.exists()

    assert _analyze(file_paths) == 0

    for name in ("はじめに.md", "一覧.csv", "列の対応.csv", "子リレーション.csv"):
        assert (ai_dir / name).exists(), name
    assert (ai_dir / "項目定義" / "Task.csv").exists()
    assert (ai_dir / "項目定義" / "User.csv").exists()
    # Excel で開いても化けないよう BOM 付き
    assert (ai_dir / "一覧.csv").read_bytes().startswith(b"\xef\xbb\xbf")
    children = (ai_dir / "子リレーション.csv").read_text(encoding="utf-8-sig")
    assert "親,リレーション名,子,参照項目,出どころ" in children
    assert "Task,Comments,TaskComment,ParentId,Salesforce" in children
    guide = (ai_dir / "はじめに.md").read_text(encoding="utf-8")
    assert "行データは入っていません" in guide
    assert "絞り込みの値は入っています" in guide
    text = (file_paths.output_dir / "SOQL資料" / "1001.md").read_text(encoding="utf-8")
    assert "## このレポートで一緒に渡すファイル" in text
    assert "- ../AIチャット用/項目定義/Task.csv" in text
    assert "- ../AIチャット用/列の対応.csv" in text


def test_all_ai_chat_files_are_within_the_limit_when_objects_are_large(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """大きなオブジェクトでも 1 ファイル 100KB 以内に分かれ、 全部の行が残る。"""
    file_paths = _setup(tmp_path, monkeypatch)
    task = _task()
    task["fields"].extend(_field(f"Long{i}__c", "長い表示名" * 20) for i in range(1500))
    client = _CountingClient(objects={"Task": task, "User": _user()})
    assert run(["--fetch"], site_for=_site_for(client), file_paths=file_paths) == 0

    assert _analyze(file_paths) == 0

    ai_dir = file_paths.output_dir / "AIチャット用"
    parts = sorted((ai_dir / "項目定義").glob("Task_*.csv"))
    assert len(parts) >= 2
    assert not (ai_dir / "項目定義" / "Task.csv").exists()
    for path in ai_dir.rglob("*"):
        if path.is_file():
            assert path.stat().st_size <= AI_FILE_MAX_BYTES, path
    names: list[str] = []
    for path in parts:
        reader = csv.DictReader(io.StringIO(path.read_text(encoding="utf-8-sig"), newline=""))
        names.extend(row["項目API名"] for row in reader)
    assert len(names) == len(task["fields"])
    assert len(set(names)) == len(names)
    # 分けたファイル名が資料に載る
    text = (file_paths.output_dir / "SOQL資料" / "1001.md").read_text(encoding="utf-8")
    assert "- ../AIチャット用/項目定義/Task_1.csv" in text
    assert "- ../AIチャット用/項目定義/Task_2.csv" in text


def test_stale_split_csv_is_removed_when_object_gets_smaller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file_paths = _setup(tmp_path, monkeypatch)
    folder = file_paths.output_dir / "AIチャット用" / "項目定義"
    folder.mkdir(parents=True)
    (folder / "Task_2.csv").write_text("古い分割", encoding="utf-8")
    assert run(["--fetch"], site_for=_site_for(_CountingClient()), file_paths=file_paths) == 0

    assert _analyze(file_paths) == 0

    assert not (folder / "Task_2.csv").exists()
    assert (folder / "Task.csv").exists()


def test_large_markdown_is_split_and_names_are_listed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file_paths = _setup(tmp_path, monkeypatch)
    report = _report()
    columns = [f"COL{i}" for i in range(1500)]
    report["reportMetadata"]["detailColumns"] = columns
    report["reportExtendedMetadata"]["detailColumnInfo"] = {
        column: {"label": "長い表示名" * 20 + column} for column in columns
    }
    client = _CountingClient(reports={REPORT_A: report})
    assert run(["--fetch"], site_for=_site_for(client), file_paths=file_paths) == 0

    # 列が (不明) のままなので警告は出るが、 レポートは失敗ではない
    assert _analyze(file_paths) == 0

    folder = file_paths.output_dir / "SOQL資料"
    parts = sorted(folder.glob("1001*.md"))
    assert len(parts) >= 2
    for path in parts:
        assert path.stat().st_size <= AI_FILE_MAX_BYTES, path
    first = (folder / "1001.md").read_text(encoding="utf-8")
    assert "- このレポートの続き: 1001_2.md" in first
    assert "1001.md の続き 2/" in (folder / "1001_2.md").read_text(encoding="utf-8")
    # 全部の列が どこかのファイルに残る
    joined = "".join(path.read_text(encoding="utf-8") for path in parts)
    for column in (columns[0], columns[-1]):
        assert column in joined


def test_split_markdown_keeps_small_content_and_splits_at_headings() -> None:
    assert split_markdown("# a\n\n## b\nc", 1000) == ["# a\n\n## b\nc"]

    content = "# 題\n\n## 一\n" + "あ" * 30 + "\n\n## 二\n" + "い" * 30
    parts = split_markdown(content, 120)
    assert len(parts) == 2
    assert parts[1].startswith("## 二")
    assert "\n".join(parts) == content


def test_split_markdown_splits_long_section_at_line_breaks() -> None:
    content = "## 一\n" + "\n".join("あ" * 20 for _ in range(10))
    parts = split_markdown(content, 200)
    assert len(parts) >= 2
    assert all(len(part.encode("utf-8")) <= 200 for part in parts)
    assert "\n".join(parts) == content


def test_write_limited_csv_warns_when_single_row_is_over_the_limit(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    rows = [{"列": "あ" * AI_FILE_MAX_BYTES}, {"列": "い"}]

    paths = write_limited_csv(tmp_path / "x.csv", ("列",), rows)

    assert [path.name for path in paths] == ["x_1.csv", "x_2.csv"]
    assert "分けられません" in caplog.text
