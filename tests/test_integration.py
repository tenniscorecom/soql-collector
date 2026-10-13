"""新機能の通し（ 統合 ） テスト。
``run_fetch`` → ``run_tables`` まで通して、 ``SOQL調査.xlsx`` と
``列名の対応.csv`` を検証する。 偽物の Salesforce クライアントで実 API には
繋がない。
"""

from __future__ import annotations

import csv
from pathlib import Path

from openpyxl import load_workbook

DOMAIN = "https://example--sandbox.sandbox.my.salesforce.com/lightning/r/Report"


# ── 偽物の Salesforce クライアント ─────────────────────────────────


class _ReportStub:
    def __init__(
        self, describe: dict, *, get_table=None, get_error: BaseException | None = None
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
    def __init__(
        self,
        describe: dict,
        object_describes: dict[str, dict] | None = None,
        *,
        get_table=None,
        get_error: BaseException | None = None,
    ) -> None:
        self._object_describes = object_describes or {}
        self.report = _ReportStub(describe, get_table=get_table, get_error=get_error)
        self.describe_object_calls: list[str] = []

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


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        columns = next(reader)
        rows = [dict(zip(columns, line, strict=True)) for line in reader]
    return columns, rows


def _read_xlsx(path: Path) -> dict[str, list[dict[str, str]]]:
    """xlsx を ``{シート名: [行 dict, ...]}`` で読む。

    空セルは ``None`` ではなく空文字として扱う。
    """
    workbook = load_workbook(path)
    result: dict[str, list[dict[str, str]]] = {}
    for sheet_name in workbook.sheetnames:
        sheet = workbook[sheet_name]
        rows_iter = sheet.iter_rows(values_only=True)
        try:
            headers = list(next(rows_iter))
        except StopIteration:
            result[sheet_name] = []
            continue
        result[sheet_name] = [
            {header: ("" if value is None else value) for header, value in zip(headers, row, strict=False)}
            for row in rows_iter
        ]
    workbook.close()
    return result


def _describe(
    report_type: str,
    columns: list[str],
    *,
    format_: str = "TABULAR",
    column_labels: dict[str, str] | None = None,
) -> dict:
    label_map = column_labels or {}
    return {
        "reportMetadata": {
            "reportType": {"type": report_type},
            "reportFormat": format_,
            "detailColumns": columns,
        },
        "reportExtendedMetadata": {
            "detailColumnInfo": {col: {"label": label_map.get(col, col)} for col in columns},
        },
    }


def _object_describes() -> dict[str, dict]:
    return {
        "Opportunity": {
            "name": "Opportunity",
            "fields": [
                {"name": "Name", "label": "商談名", "type": "string"},
                {"name": "Amount", "label": "金額", "type": "currency"},
                {
                    "name": "Contact__c",
                    "label": "取引先責任者",
                    "type": "reference",
                    "referenceTo": ["Contact"],
                    "relationshipName": "Contact__r",
                },
            ],
        },
        "Contact": {
            "name": "Contact",
            "fields": [
                {"name": "Name", "label": "氏名", "type": "string"},
                {"name": "Email", "label": "メール", "type": "email"},
                {"name": "Phone", "label": "電話", "type": "phone"},
            ],
        },
    }


class _Row:
    def __init__(self, count: int) -> None:
        self._count = count

    def __len__(self) -> int:
        return self._count


# ── 統合テスト ─────────────────────────────────────────────────────


def test_e2e_run_tables_writes_survey_and_similar(tmp_path: Path) -> None:
    """``run_fetch`` → ``run_tables`` で 一覧 と 似たレポート が出る。

    シナリオ:
    - 1001: 商談名と取引先責任者（ Contact.Email を持つ ） を出力。 get は 2000 行超
    - 1002: 1001 と同じ構造で 違う ID
    """
    from src import pii
    from src.fetch import run_fetch
    from src.master import MasterEntry
    from src.settings import Settings
    from src.tables import run_tables

    settings = Settings(
        master_xlsx_path=tmp_path / "master.xlsx",
        output_dir=tmp_path / "output",
        related_max=40,
        related_depth=1,
        credential_prefix="",
        row_limit=True,
        pii_keywords=pii.DEFAULT_KEYWORDS,
        pii_person_objects=pii.DEFAULT_PERSON_OBJECTS,
        similar_column_similarity=0.8,
    )

    from comken.exceptions import SalesforceReportTruncatedError

    client = _FakeClient(
        describe=_describe(
            "Opportunity",
            ["Opp.Name", "Contact.Email"],
            column_labels={"Contact.Email": "メール"},
        ),
        object_describes=_object_describes(),
        get_error=SalesforceReportTruncatedError("00O000000000001", 2000),
    )
    site_for = _site_for(client)

    # 1001: get が Truncated
    entry_1001 = MasterEntry(
        key="1001",
        summary="テスト1001",
        url=f"{DOMAIN}/00O000000000001/view",
        enabled=True,
    )
    outcome_1001 = run_fetch(settings, [entry_1001], site_for=site_for)[0]
    assert outcome_1001.status == "ok"
    assert outcome_1001.record is not None
    assert outcome_1001.record.row_check is not None
    assert outcome_1001.record.row_check.status == "超えている"

    # 1002: 同じ構造で 違う ID、 get は 10 行
    client2 = _FakeClient(
        describe=_describe(
            "Opportunity",
            ["Opp.Name", "Contact.Email"],
            column_labels={"Contact.Email": "メール"},
        ),
        object_describes=_object_describes(),
        get_table=_Row(10),
    )
    entry_1002 = MasterEntry(
        key="1002",
        summary="テスト1002",
        url=f"{DOMAIN}/00O000000000002/view",
        enabled=True,
    )
    outcome_1002 = run_fetch(settings, [entry_1002], site_for=_site_for(client2))[0]
    assert outcome_1002.status == "ok"
    assert outcome_1002.record is not None
    assert outcome_1002.record.row_check is not None
    assert outcome_1002.record.row_check.status == "超えていない"
    assert outcome_1002.record.row_check.rows == 10

    # run_tables で全部書く
    output = tmp_path / "output"
    output.mkdir(parents=True, exist_ok=True)
    records = [outcome_1001.record, outcome_1002.record]
    run_tables(
        output,
        records,
        pii_config=pii.PIIConfig(
            keywords=settings.pii_keywords, person_objects=settings.pii_person_objects
        ),
        similar_threshold=settings.similar_column_similarity,
    )

    # xlsx が作られている
    xlsx_path = output / "SOQL調査.xlsx"
    assert xlsx_path.exists()
    sheets = _read_xlsx(xlsx_path)

    # 一覧シート: 1001 は 超えている、 1002 は 超えていない
    overview = sheets["一覧"]
    by_key = {row["管理番号"]: row for row in overview}
    assert by_key["1001"]["2000件超"] == "超えている"
    assert by_key["1002"]["2000件超"] == "超えていない"
    assert by_key["1002"]["行数"] == "10"
    # Contact.Email が 出力列にあるので 個人情報 は あり
    assert by_key["1001"]["個人情報"] == "あり"
    assert by_key["1002"]["個人情報"] == "あり"
    # 該当列に 「 メール 」 が出る
    assert "メール" in by_key["1001"]["個人情報の該当列"]

    # 似たレポートシート: 1001 と 1002 は 「 同一 」 で 1 グループ
    similar_rows = sheets["似たレポート"]
    assert len(similar_rows) == 2  # 基準 + 対象
    categories = [row["区分"] for row in similar_rows]
    assert "基準" in categories
    assert "同一" in categories
    integrations = {row["統合の見込み"] for row in similar_rows}
    assert "1本にまとめられる" in integrations

    # 列名の対応.csv も作られている
    assert (output / "列名の対応.csv").exists()


def test_e2e_single_record_no_similar_sheet(tmp_path: Path) -> None:
    """1 件だけのときは 似たレポート シートは見出しだけ ( 1 冊全部できる )。"""
    from src import pii
    from src.fetch import run_fetch
    from src.master import MasterEntry
    from src.settings import Settings
    from src.tables import run_tables

    settings = Settings(
        master_xlsx_path=tmp_path / "master.xlsx",
        output_dir=tmp_path / "output",
        related_max=40,
        related_depth=1,
        credential_prefix="",
        row_limit=True,
        pii_keywords=pii.DEFAULT_KEYWORDS,
        pii_person_objects=pii.DEFAULT_PERSON_OBJECTS,
        similar_column_similarity=0.8,
    )
    client = _FakeClient(
        describe=_describe("Opportunity", ["Opp.Name"]),
        object_describes=_object_describes(),
    )
    entry = MasterEntry(
        key="1001",
        summary="テスト",
        url=f"{DOMAIN}/00O000000000001/view",
        enabled=True,
    )
    outcome = run_fetch(settings, [entry], site_for=_site_for(client))[0]
    output = tmp_path / "output"
    output.mkdir(parents=True, exist_ok=True)
    run_tables(
        output,
        [outcome.record],
        pii_config=pii.PIIConfig(
            keywords=settings.pii_keywords, person_objects=settings.pii_person_objects
        ),
        similar_threshold=settings.similar_column_similarity,
    )
    # xlsx はある ( 1 冊全部できる )
    assert (output / "SOQL調査.xlsx").exists()
    sheets = _read_xlsx(output / "SOQL調査.xlsx")
    # 一覧シートに 1 行
    assert len(sheets["一覧"]) == 1
    # 似たレポートシートは該当なしでデータ行 0 ( 見出しは出る )
    assert sheets["似たレポート"] == []


def test_e2e_resolved_correspondence_has_pii_columns(tmp_path: Path) -> None:
    """``項目の対応`` シートに 個人情報 / 個人情報の根拠 列が加わっている。"""
    from src import pii
    from src.fetch import run_fetch
    from src.master import MasterEntry
    from src.settings import Settings
    from src.tables import RESOLVED_COLUMNS, run_tables

    settings = Settings(
        master_xlsx_path=tmp_path / "master.xlsx",
        output_dir=tmp_path / "output",
        related_max=40,
        related_depth=1,
        credential_prefix="",
        row_limit=True,
        pii_keywords=pii.DEFAULT_KEYWORDS,
        pii_person_objects=pii.DEFAULT_PERSON_OBJECTS,
        similar_column_similarity=0.8,
    )
    client = _FakeClient(
        # 「 メール 」 ラベルが Contact.Email と一致する想定 ( 既存テストと同形 )
        describe=_describe(
            "Opportunity", ["Contact.Email"], column_labels={"Contact.Email": "メール"}
        ),
        object_describes=_object_describes(),
    )
    entry = MasterEntry(
        key="1001",
        summary="テスト",
        url=f"{DOMAIN}/00O000000000001/view",
        enabled=True,
    )
    outcome = run_fetch(settings, [entry], site_for=_site_for(client))[0]
    output = tmp_path / "output"
    output.mkdir(parents=True, exist_ok=True)
    run_tables(
        output,
        [outcome.record],
        pii_config=pii.PIIConfig(
            keywords=settings.pii_keywords, person_objects=settings.pii_person_objects
        ),
        similar_threshold=settings.similar_column_similarity,
    )

    # RESOLVED_COLUMNS 定数で 個人情報 / 個人情報の根拠 の存在を保証
    assert "個人情報" in RESOLVED_COLUMNS
    assert "個人情報の根拠" in RESOLVED_COLUMNS
