"""``tables`` のテスト。 ``ReportRecord`` を直接作って、 CSV の中身を厳密に
確かめる （ ネットワークには出ない ）。
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path

import pytest

from src.fetch import ReportRecord
from src.tables import (
    AGGREGATE_TABLE_COLUMNS,
    COLUMN_NAMES_COLUMNS,
    CONDITION_TABLE_COLUMNS,
    CORRESPONDENCE_COLUMNS,
    FIELD_TABLE_COLUMNS,
    RELATED_TABLE_COLUMNS,
    WARNING_TABLE_COLUMNS,
    TableOutcome,
    run_tables,
)

# ── ヘルパー ────────────────────────────────────────────────────────────────


def _make_record(
    key: str = "1001",
    *,
    main_object: str | None = "Opportunity",
    summary: str | None = None,
    report_id: str = "00O000000000001",
    url: str = "https://example.my.salesforce.com/lightning/r/Report/00O000000000001/view",
) -> ReportRecord:
    """典型的な ``ReportRecord`` を作る。

    参照項目・ポリモーフィック・カスタム項目・選択肢を含む小さい
    ``Opportunity`` describe と、 ``column_map`` を持つ。
    """
    summary_text = summary if summary is not None else f"テスト{key}"
    objects: dict[str, dict] = {}
    if main_object:
        objects[main_object] = {
            "name": main_object,
            "label": "商談",
            "custom": False,
            "fields": [
                {
                    "name": "AccountId",
                    "label": "取引先",
                    "type": "reference",
                    "custom": False,
                    "referenceTo": ["Account"],
                    "relationshipName": "Account",
                    "picklist": [],
                    "picklistTotal": 0,
                },
                {
                    "name": "WhoId",
                    "label": "担当者",
                    "type": "reference",
                    "custom": False,
                    "referenceTo": ["Contact", "Lead"],
                    "relationshipName": None,
                    "picklist": [],
                    "picklistTotal": 0,
                },
                {
                    "name": "Custom__c",
                    "label": "カスタム項目",
                    "type": "picklist",
                    "custom": True,
                    "referenceTo": [],
                    "relationshipName": None,
                    "picklist": ["A", "B", "D"],
                    "picklistTotal": 3,
                },
                {
                    "name": "Name",
                    "label": "商談名",
                    "type": "string",
                    "custom": False,
                    "referenceTo": [],
                    "relationshipName": None,
                    "picklist": [],
                    "picklistTotal": 0,
                },
            ],
        }
    column_map: list[dict[str, str]] = [
        {
            "列キー": "Opp.Account",
            "表示名": "取引先名",
            "所属オブジェクト": "Opportunity",
            "対応フィールドAPI名": "AccountId",
            "型": "reference",
            "参照先オブジェクト": "",
            "リレーション名": "",
            "備考": "",
        },
        {
            "列キー": "Opp.LeadOrContact",
            "表示名": "担当者名",
            "所属オブジェクト": "Opportunity",
            "対応フィールドAPI名": "WhoId",
            "型": "reference",
            "参照先オブジェクト": "",
            "リレーション名": "",
            "備考": "ポリモーフィック",
        },
        {
            "列キー": "Opp.Custom",
            "表示名": "カスタム",
            "所属オブジェクト": "Opportunity",
            "対応フィールドAPI名": "Custom__c",
            "型": "picklist",
            "参照先オブジェクト": "",
            "リレーション名": "",
            "備考": "",
        },
        {
            "列キー": "Opp.Name",
            "表示名": "商談名",
            "所属オブジェクト": "Opportunity",
            "対応フィールドAPI名": "Name",
            "型": "string",
            "参照先オブジェクト": "",
            "リレーション名": "",
            "備考": "",
        },
        {
            "列キー": "Opp.Unknown",
            "表示名": "不明列",
            "所属オブジェクト": "",
            "対応フィールドAPI名": "(不明)",
            "型": "",
            "参照先オブジェクト": "",
            "リレーション名": "",
            "備考": "表示名一致せず",
        },
    ]
    report = {
        "reportMetadata": {
            "reportType": {"type": main_object or "Opportunity"},
            "reportFormat": "TABULAR",
        },
        "reportExtendedMetadata": {},
    }
    return ReportRecord(
        key=key,
        summary=summary_text,
        report_id=report_id,
        url=url,
        main_object=main_object,
        report=report,
        objects=objects,
        relations=(),
        column_map=column_map,
        warnings=(),
    )


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    """CSV を ``(columns, rows)`` で読む。 ``csv.DictReader`` 経由。"""
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.reader(file)
        columns = next(reader)
        rows = [dict(zip(columns, line, strict=True)) for line in reader]
    return columns, rows


# ── 対応表（ per-ID ） ─────────────────────────────────────────────────────────


def test_correspondence_per_id_csv_basic(tmp_path: Path) -> None:
    """参照項目には ``参照先オブジェクト`` と ``リレーション名`` が埋まる。"""
    output = tmp_path / "output"
    output.mkdir()
    record = _make_record("1001", main_object="Opportunity")

    outcome: TableOutcome = run_tables(output, [record], only_keys=["1001"])
    assert isinstance(outcome, TableOutcome)

    assert (output / "対応表_1001.csv").exists()
    columns, rows = _read_csv(output / "対応表_1001.csv")
    assert columns == list(CORRESPONDENCE_COLUMNS)
    assert len(rows) == 5

    # 1 行目: AccountId（ referenceTo=[Account], relationshipName=Account）
    row = rows[0]
    assert row["管理番号"] == "1001"
    assert row["レポートID"] == "00O000000000001"
    assert row["レポートタイプ"] == "Opportunity"
    assert row["主オブジェクト"] == "Opportunity"
    assert row["列キー"] == "Opp.Account"
    assert row["表示名"] == "取引先名"
    assert row["所属オブジェクト"] == "Opportunity"
    assert row["対応フィールドAPI名"] == "AccountId"
    assert row["型"] == "reference"
    assert row["参照先オブジェクト"] == "Account"
    assert row["リレーション名"] == "Account"

    # 2 行目: WhoId （ ポリモーフィック。 ``referenceTo`` は ``|`` 区切り ）
    row = rows[1]
    assert row["対応フィールドAPI名"] == "WhoId"
    assert row["参照先オブジェクト"] == "Contact|Lead"
    assert row["リレーション名"] == ""

    # 3 行目: Custom__c （ picklist。 referenceTo / relationshipName は空 ）
    row = rows[2]
    assert row["対応フィールドAPI名"] == "Custom__c"
    assert row["参照先オブジェクト"] == ""
    assert row["リレーション名"] == ""

    # 4 行目: Name （ 参照無し ）
    row = rows[3]
    assert row["対応フィールドAPI名"] == "Name"
    assert row["参照先オブジェクト"] == ""
    assert row["リレーション名"] == ""

    # 5 行目: (不明) （ 対応フィールドが取れなかった列 ）
    row = rows[4]
    assert row["対応フィールドAPI名"] == "(不明)"
    assert row["所属オブジェクト"] == ""
    assert row["参照先オブジェクト"] == ""
    assert row["リレーション名"] == ""


def test_correspondence_main_object_null(tmp_path: Path) -> None:
    """主オブジェクトが ``None`` でも、 対応表に行が出る
    （ 主オブジェクトと参照先の列は空のまま ）。
    """
    output = tmp_path / "output"
    output.mkdir()
    record = _make_record("1001", main_object=None)

    run_tables(output, [record], only_keys=["1001"])

    _, rows = _read_csv(output / "対応表_1001.csv")
    assert len(rows) == 5
    for row in rows:
        assert row["主オブジェクト"] == ""
        assert row["参照先オブジェクト"] == ""
        assert row["リレーション名"] == ""


def test_correspondence_writes_csv_with_utf8_bom_and_crlf(tmp_path: Path) -> None:
    """CSV が UTF-8 BOM + CRLF で書かれていることをバイト列で確認する。
    なぜ: Excel で開ける形式にしておく必要があるため。
    """
    output = tmp_path / "output"
    output.mkdir()
    record = _make_record("1001")

    run_tables(output, [record], only_keys=["1001"])

    path = output / "対応表_1001.csv"
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "BOM が無い"
    assert b"\r\n" in raw
    assert b"\n\r" not in raw.replace(b"\r\n", b"")


def test_correspondence_uses_atomic_write(tmp_path: Path) -> None:
    """CSV 書き込みが ``comken.core.files.atomic_write`` 経由である
    （ 途中で失敗したら既存 CSV が壊れない ） ことを、 ``atomic_write`` を
    パッチして例外を上げて確かめる。
    """
    import comken.toolbox.csv.file as csv_file_mod

    output = tmp_path / "output"
    output.mkdir()

    # 既存の CSV を置いておき、 途中で例外が上がっても壊れないことを確かめる
    existing = output / "対応表_1001.csv"
    existing.write_text("既存\nそのまま\n", encoding="utf-8")

    real_atomic = csv_file_mod.atomic_write
    called = {"count": 0}

    def broken_atomic_write(path: Path) -> object:
        called["count"] += 1
        raise OSError("爆発")

    csv_file_mod.atomic_write = broken_atomic_write  # type: ignore[assignment]
    try:
        record = _make_record("1001")
        with pytest.raises(OSError):
            run_tables(output, [record], only_keys=["1001"])
    finally:
        csv_file_mod.atomic_write = real_atomic  # type: ignore[assignment]

    # ``atomic_write`` が必ず通っている （ ``CSV._write`` の中で呼ばれる ）
    assert called["count"] >= 1
    # 既存 CSV は残っている（上書きされていない）
    assert existing.read_text(encoding="utf-8") == "既存\nそのまま\n"


# ── 対応表（ 全体 ） ─────────────────────────────────────────────────────────


def test_overall_correspondence_concatenates_and_sorts(tmp_path: Path) -> None:
    """全 record の ``対応表.csv`` は、 管理番号の昇順で連結される。"""
    output = tmp_path / "output"
    output.mkdir()
    record_1002 = _make_record("1002")
    record_1001 = _make_record("1001")
    record_1003 = _make_record("1003")

    run_tables(output, [record_1002, record_1001, record_1003])

    _, rows = _read_csv(output / "対応表.csv")
    keys = [row["管理番号"] for row in rows]
    assert keys == ["1001"] * 5 + ["1002"] * 5 + ["1003"] * 5


# ── 項目表 ─────────────────────────────────────────────────────────────────


def test_field_table_dedups_same_object_across_records(tmp_path: Path) -> None:
    """同じオブジェクトが複数の record に出ても、 項目表では 1 回だけにする。"""
    output = tmp_path / "output"
    output.mkdir()
    record_1 = _make_record("1001", main_object="Opportunity")
    record_2 = _make_record("1002", main_object="Opportunity")

    run_tables(output, [record_1, record_2])

    _, rows = _read_csv(output / "項目表.csv")
    object_rows = [row for row in rows if row["オブジェクト"] == "Opportunity"]
    assert len(object_rows) == 4
    field_names = {row["項目API名"] for row in object_rows}
    assert field_names == {"AccountId", "WhoId", "Custom__c", "Name"}


def test_field_table_picklist_active_only_and_truncated(tmp_path: Path) -> None:
    """``picklist`` （ active な ``value`` だけの配列） を ``|`` 区切りにする。
    ``picklistTotal`` が 30 を超えるとき末尾に ``…`` を 1 個足して切る。
    ``custom=True`` は ``○``、 ``custom=False`` は空。
    """
    output = tmp_path / "output"
    output.mkdir()

    picklist = [f"V{i}" for i in range(31)]
    record = _make_record("1001", main_object="Opportunity")
    # Custom__c を 31 件の ``picklist`` 配列に差し替え
    for field in record.objects["Opportunity"]["fields"]:
        if field["name"] == "Custom__c":
            field["picklist"] = picklist
            field["picklistTotal"] = 31
            break

    run_tables(output, [record], only_keys=["1001"])

    _, rows = _read_csv(output / "項目表.csv")
    custom_row = next(row for row in rows if row["項目API名"] == "Custom__c")
    choices = custom_row["選択肢"].split("|")
    assert len(choices) == 31
    assert choices[-1] == "…"
    assert choices[0] == "V0"
    assert choices[29] == "V29"
    assert custom_row["カスタム"] == "○"

    name_row = next(row for row in rows if row["項目API名"] == "Name")
    assert name_row["カスタム"] == ""
    assert name_row["選択肢"] == ""


def test_field_table_picklist_total_at_boundary(tmp_path: Path) -> None:
    """``picklistTotal`` の境界値: 30 ちょうどなら ``…`` は付かず、
    31 なら ``…`` が 1 個付く。
    """
    output = tmp_path / "output"
    output.mkdir()
    record = _make_record("1001", main_object="Opportunity")

    record.objects["Opportunity"]["fields"].append(
        {
            "name": "Picked__c",
            "label": "ちょうど30",
            "type": "picklist",
            "custom": True,
            "referenceTo": [],
            "relationshipName": None,
            "picklist": [f"V{i}" for i in range(30)],
            "picklistTotal": 30,
        }
    )
    run_tables(output, [record], only_keys=["1001"])

    _, rows = _read_csv(output / "項目表.csv")
    row = next(r for r in rows if r["項目API名"] == "Picked__c")
    choices = row["選択肢"].split("|")
    assert len(choices) == 30
    assert "…" not in row["選択肢"]
    assert choices[0] == "V0"
    assert choices[29] == "V29"


def test_field_table_picklist_total_exceeds_list_size(tmp_path: Path) -> None:
    """``picklist`` が 30 個に絞られていても ``picklistTotal`` が 31 以上なら
    末尾に ``…`` が 1 個付く。
    """
    output = tmp_path / "output"
    output.mkdir()
    record = _make_record("1001", main_object="Opportunity")
    record.objects["Opportunity"]["fields"].append(
        {
            "name": "BigPick__c",
            "label": "大きい選択肢",
            "picklist": [f"V{i}" for i in range(30)],
            "picklistTotal": 50,
        }
    )
    run_tables(output, [record], only_keys=["1001"])

    _, rows = _read_csv(output / "項目表.csv")
    row = next(r for r in rows if r["項目API名"] == "BigPick__c")
    choices = row["選択肢"].split("|")
    assert len(choices) == 31
    assert choices[-1] == "…"
    assert choices[0] == "V0"


def test_field_table_polymorphic_reference_and_relationship(tmp_path: Path) -> None:
    """ポリモーフィックな参照項目は ``参照先オブジェクト`` を ``|`` 区切りで出す。
    ``relationshipName`` が無いときは空のまま。
    """
    output = tmp_path / "output"
    output.mkdir()
    record = _make_record("1001", main_object="Opportunity")

    run_tables(output, [record], only_keys=["1001"])

    _, rows = _read_csv(output / "項目表.csv")
    who_row = next(row for row in rows if row["項目API名"] == "WhoId")
    assert who_row["参照先オブジェクト"] == "Contact|Lead"
    assert who_row["リレーション名"] == ""

    account_row = next(row for row in rows if row["項目API名"] == "AccountId")
    assert account_row["参照先オブジェクト"] == "Account"
    assert account_row["リレーション名"] == "Account"


def test_field_table_columns_and_sort(tmp_path: Path) -> None:
    """項目表の列順・オブジェクト名の昇順ソートを確かめる。"""
    output = tmp_path / "output"
    output.mkdir()

    record_a = _make_record("1001", main_object="Account")
    record_a.objects["Account"]["label"] = "取引先"
    record_a.objects["Account"]["fields"] = [
        {"name": "Name", "label": "取引先名", "type": "string"}
    ]
    record_o = _make_record("1002", main_object="Opportunity")
    record_o.objects["Opportunity"]["label"] = "商談"
    record_o.objects["Opportunity"]["fields"] = [
        {"name": "Name", "label": "商談名", "type": "string"}
    ]
    run_tables(output, [record_a, record_o])

    columns, rows = _read_csv(output / "項目表.csv")
    assert columns == list(FIELD_TABLE_COLUMNS)
    assert columns[1] == "段"
    objects = [row["オブジェクト"] for row in rows]
    assert objects == ["Account", "Opportunity"]


# ── ファイル単位の挙動 ─────────────────────────────────────────────────────


def test_empty_records_writes_empty_csvs(tmp_path: Path) -> None:
    """``records`` が空のときは CSV は見出しだけになる。"""
    output = tmp_path / "output"
    output.mkdir()

    outcome = run_tables(output, [])

    correspondence_path = output / "対応表.csv"
    field_path = output / "項目表.csv"
    related_path = output / "関連表.csv"
    condition_path = output / "条件表.csv"
    aggregate_path = output / "集計表.csv"
    warning_path = output / "警告表.csv"
    column_names_path = output / "列名の対応.csv"
    assert correspondence_path.exists()
    assert field_path.exists()
    assert related_path.exists()
    assert condition_path.exists()
    assert aggregate_path.exists()
    assert warning_path.exists()
    assert column_names_path.exists()

    _, corr_rows = _read_csv(correspondence_path)
    _, field_rows = _read_csv(field_path)
    _, related_rows = _read_csv(related_path)
    assert corr_rows == []
    assert field_rows == []
    assert related_rows == []

    # per-ID CSV は無い
    assert list(output.glob("対応表_*.csv")) == []

    # 全 CSV が書かれたパスの集合 （ 順序は実装都合 ）
    expected = {
        correspondence_path,
        field_path,
        related_path,
        condition_path,
        aggregate_path,
        warning_path,
        column_names_path,
    }
    assert set(outcome.wrote) == expected
    assert outcome.skipped == ()


def test_only_keys_filters_per_id_csv(tmp_path: Path) -> None:
    """``only_keys`` を指定すると、 per-ID CSV はその管理番号だけ作られる。"""
    output = tmp_path / "output"
    output.mkdir()
    record_1001 = _make_record("1001")
    record_1002 = _make_record("1002")
    record_1003 = _make_record("1003")

    run_tables(output, [record_1001, record_1002, record_1003], only_keys=["1001", "1003"])

    # per-ID CSV は 1001 / 1003 だけ
    per_id = sorted(p.name for p in output.glob("対応表_*.csv"))
    assert per_id == ["対応表_1001.csv", "対応表_1003.csv"]

    # per-ID の txt も同様
    per_id_txt = sorted(p.name for p in output.glob("列名の対応_*.txt"))
    assert per_id_txt == ["列名の対応_1001.txt", "列名の対応_1003.txt"]

    # 全体対応表には 3 つぶん全ての行
    _, corr_rows = _read_csv(output / "対応表.csv")
    keys = {row["管理番号"] for row in corr_rows}
    assert keys == {"1001", "1002", "1003"}

    # 項目表にも 3 つぶん全ての Opportunity
    _, rows = _read_csv(output / "項目表.csv")
    assert any(row["オブジェクト"] == "Opportunity" for row in rows)


def test_output_dir_missing_does_not_raise(tmp_path: Path) -> None:
    """``output_dir`` が存在しないときは例外を出さず、 フォルダを作ってからの
    空 CSV （ 見出しのみ ） を書く。
    """
    output = tmp_path / "missing"  # 存在しない
    assert not output.exists()

    outcome = run_tables(output, [])

    assert output.exists()
    correspondence_path = output / "対応表.csv"
    field_path = output / "項目表.csv"
    assert correspondence_path.exists()
    assert field_path.exists()
    _, corr_rows = _read_csv(correspondence_path)
    _, field_rows = _read_csv(field_path)
    assert corr_rows == []
    assert field_rows == []
    assert outcome.skipped == ()


# ── ログの出し方 ───────────────────────────────────────────────────────────


def test_tables_logs_counts(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """``tables`` は CSV のパスだけをログに書き、 describe の中身は出さない。
    なぜ: ログに機微な情報が混ざらないことを保証するため。
    """
    output = tmp_path / "output"
    output.mkdir()
    record = _make_record("1001")

    with caplog.at_level(logging.INFO):
        run_tables(output, [record], only_keys=["1001"])

    messages = [record.getMessage() for record in caplog.records]
    assert any("対応表_1001.csv" in m for m in messages)
    assert any("対応表.csv" in m for m in messages)
    assert any("項目表.csv" in m for m in messages)
    # describe の中身 （ "AccountId" / "Custom__c" など ） はログに含まれない
    for field_token in ("AccountId", "WhoId", "Custom__c", "商談名"):
        for message in messages:
            assert field_token not in message, (
                f"describe の中身 {field_token!r} が漏れています: {message}"
            )


# ── 関連表 ──────────────────────────────────────────────────────────────


def _make_record_with_relations(key: str, main: str, related_entries: list[dict]) -> ReportRecord:
    """関係を ``relations`` に持つ ``ReportRecord`` を作る （ 関連表用 ） 。"""
    record = _make_record(key, main_object=main)

    objects: dict[str, dict] = dict(record.objects)
    objects["Account"] = {
        "name": "Account",
        "label": "取引先",
        "custom": False,
        "fields": [{"name": "Name", "label": "取引先名", "type": "string"}],
    }
    objects["User"] = {
        "name": "User",
        "label": "ユーザ",
        "custom": False,
        "fields": [{"name": "Alias", "label": "別名", "type": "string"}],
    }

    from src.fetch import RelationRecord

    relations = tuple(
        RelationRecord(
            parent=entry["親"],
            field=entry["参照項目"],
            relationship_name=entry["リレーション名"],
            child=entry["子"],
            depth=entry["段"],
        )
        for entry in related_entries
    )

    return ReportRecord(
        key=record.key,
        summary=record.summary,
        report_id=record.report_id,
        url=record.url,
        main_object=record.main_object,
        report=record.report,
        objects=objects,
        relations=relations,
        column_map=record.column_map,
        warnings=record.warnings,
    )


def test_related_table_one_row_per_edge(tmp_path: Path) -> None:
    """``relations`` の各エントリが 1 行ずつ ``関連表.csv`` に乗る。"""
    output = tmp_path / "output"
    output.mkdir()
    record = _make_record_with_relations(
        "1001",
        "Task",
        [
            {
                "親": "Task",
                "参照項目": "WhatId",
                "リレーション名": "WhatId",
                "子": "Account",
                "段": 1,
            },
            {
                "親": "Account",
                "参照項目": "OwnerId",
                "リレーション名": "OwnerId",
                "子": "User",
                "段": 2,
            },
        ],
    )

    run_tables(output, [record])

    columns, rows = _read_csv(output / "関連表.csv")
    assert columns == list(RELATED_TABLE_COLUMNS)
    assert rows == [
        {
            "管理番号": "1001",
            "親オブジェクト": "Task",
            "参照項目API名": "WhatId",
            "リレーション名": "WhatId",
            "子オブジェクト": "Account",
            "段": "1",
        },
        {
            "管理番号": "1001",
            "親オブジェクト": "Account",
            "参照項目API名": "OwnerId",
            "リレーション名": "OwnerId",
            "子オブジェクト": "User",
            "段": "2",
        },
    ]


def test_related_table_orders_by_management_key(tmp_path: Path) -> None:
    """``関連表.csv`` は「 管理番号 → 段 → 親の出現順 」 で並ぶ。"""
    output = tmp_path / "output"
    output.mkdir()

    record_1001 = _make_record_with_relations(
        "1001",
        "Task",
        [
            {
                "親": "Task",
                "参照項目": "WhatId",
                "リレーション名": "WhatId",
                "子": "Account",
                "段": 1,
            },
            {
                "親": "Account",
                "参照項目": "OwnerId",
                "リレーション名": "OwnerId",
                "子": "User",
                "段": 2,
            },
        ],
    )
    record_1003 = _make_record_with_relations(
        "1003",
        "Opportunity",
        [
            {
                "親": "Opportunity",
                "参照項目": "AccountId",
                "リレーション名": "Account",
                "子": "Account",
                "段": 1,
            },
        ],
    )

    run_tables(output, [record_1001, record_1003])

    _, rows = _read_csv(output / "関連表.csv")
    keys = [row["管理番号"] for row in rows]
    assert keys == ["1001", "1001", "1003"]


# ── 条件表 ──────────────────────────────────────────────────────────────


def test_condition_table_basic(tmp_path: Path) -> None:
    """``reportFilters`` / ``standardDateFilter`` / ``crossFilters`` / ``topRows`` が
    1 行ずつ展開される。 ``reportBooleanFilter`` は 1 行にそのまま入る。
    """
    output = tmp_path / "output"
    output.mkdir()
    record = _make_record("1001", main_object="Opportunity")
    record.report["reportMetadata"]["reportFilters"] = [
        {"column": "Opp.Amount", "operator": "greaterThan", "value": "0"},
        {"column": "Opp.StageName", "operator": "equals", "value": "Closed Won"},
    ]
    record.report["reportMetadata"]["reportBooleanFilter"] = "1 AND 2"
    record.report["reportMetadata"]["standardDateFilter"] = {
        "column": "Opp.CloseDate",
        "durationValue": "LAST_N_DAYS:30",
        "startDate": "2024-01-01",
        "endDate": "2024-01-31",
    }
    record.report["reportMetadata"]["crossFilters"] = [
        {
            "primaryEntityField": "Opp.AccountId",
            "operator": "WITH",
            "relatedEntity": "OpportunityContactRole",
            "relatedEntityJoinField": "OpportunityId",
            "criteria": [],
        }
    ]
    record.report["reportMetadata"]["topRows"] = "10"

    run_tables(output, [record])

    _, rows = _read_csv(output / "条件表.csv")
    by_kind: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        by_kind.setdefault(row["種別"], []).append(row)

    # 絞り込みは 2 行
    assert len(by_kind["絞り込み"]) == 2
    assert {row["列キー"] for row in by_kind["絞り込み"]} == {"Opp.Amount", "Opp.StageName"}
    assert "組立式" in by_kind
    assert by_kind["組立式"][0]["値"] == "1 AND 2"
    assert "日付" in by_kind
    assert by_kind["日付"][0]["列キー"] == "Opp.CloseDate"
    assert "クロス" in by_kind
    assert "WITH" in by_kind["クロス"][0]["値"]
    assert "上位N" in by_kind
    assert by_kind["上位N"][0]["値"] == "10"


def test_condition_table_columns_match(tmp_path: Path) -> None:
    """条件表の列順を確かめる。"""
    output = tmp_path / "output"
    output.mkdir()
    record = _make_record("1001", main_object="Opportunity")
    record.report["reportMetadata"]["reportFilters"] = [
        {"column": "X", "operator": "equals", "value": "y"}
    ]

    run_tables(output, [record])

    columns, _ = _read_csv(output / "条件表.csv")
    assert columns == list(CONDITION_TABLE_COLUMNS)


# ── 集計表 ──────────────────────────────────────────────────────────────


def test_aggregate_table_basic(tmp_path: Path) -> None:
    """グルーピング・集計・並び順が 1 行ずつ展開される。"""
    output = tmp_path / "output"
    output.mkdir()
    record = _make_record("1001", main_object="Opportunity")
    record.report["reportMetadata"]["groupingsDown"] = [
        {"name": "Opp.StageName", "sortOrder": "Asc"}
    ]
    record.report["reportMetadata"]["aggregates"] = ["s!Opp.Amount"]
    record.report["reportMetadata"]["sortBy"] = [{"sortColumn": "Opp.Name", "sortOrder": "Desc"}]

    run_tables(output, [record])

    _, rows = _read_csv(output / "集計表.csv")
    by_kind = {row["種別"]: row for row in rows}

    assert "形式" in by_kind
    assert by_kind["形式"]["内容"] == "TABULAR"
    assert "グルーピング(行)" in by_kind
    assert by_kind["グルーピング(行)"]["名前"] == "Opp.StageName"
    assert "集計" in by_kind
    assert by_kind["集計"]["名前"] == "s!Opp.Amount"
    assert "並び順" in by_kind
    assert by_kind["並び順"]["名前"] == "Opp.Name"


def test_aggregate_table_columns_match(tmp_path: Path) -> None:
    """集計表の列順を確かめる。"""
    output = tmp_path / "output"
    output.mkdir()
    record = _make_record("1001", main_object="Opportunity")

    run_tables(output, [record])

    columns, _ = _read_csv(output / "集計表.csv")
    assert columns == list(AGGREGATE_TABLE_COLUMNS)


# ── 警告表 ──────────────────────────────────────────────────────────────


def test_warning_table_collects_record_warnings(tmp_path: Path) -> None:
    """``record.warnings`` が 1 行ずつ警告表に入る。"""
    output = tmp_path / "output"
    output.mkdir()
    record = _make_record("1001", main_object="Opportunity")
    record_warned = ReportRecord(
        key=record.key,
        summary=record.summary,
        report_id=record.report_id,
        url=record.url,
        main_object=record.main_object,
        report=record.report,
        objects=record.objects,
        relations=record.relations,
        column_map=record.column_map,
        warnings=("警告 A", "警告 B"),
    )

    run_tables(output, [record_warned])

    columns, rows = _read_csv(output / "警告表.csv")
    assert columns == list(WARNING_TABLE_COLUMNS)
    assert {row["警告"] for row in rows} == {"警告 A", "警告 B"}


# ── 列名の対応 ───────────────────────────────────────────────────────────


def test_column_names_csv_resolved_only(tmp_path: Path) -> None:
    """``列名の対応.csv`` は ``(不明)`` を除外し、 解決できた列だけ出す。"""
    output = tmp_path / "output"
    output.mkdir()
    record = _make_record("1001", main_object="Opportunity")

    run_tables(output, [record])

    columns, rows = _read_csv(output / "列名の対応.csv")
    assert columns == list(COLUMN_NAMES_COLUMNS)
    # (不明) は除く
    assert all(row["SOQL列名"] != "(不明)" for row in rows)
    # 解決できた 4 件
    assert len(rows) == 4
    assert {row["SOQL列名"] for row in rows} == {"AccountId", "WhoId", "Custom__c", "Name"}


def test_column_names_csv_marks_duplicate_label(tmp_path: Path) -> None:
    """同じ表示名が複数の列にあると、 備考に「表示名が重複」 が付く。"""
    output = tmp_path / "output"
    output.mkdir()
    record = _make_record("1001", main_object="Opportunity")
    record.column_map.append(
        {
            "列キー": "Opp.Dup",
            "表示名": "商談名",
            "所属オブジェクト": "Opportunity",
            "対応フィールドAPI名": "OtherField__c",
            "型": "string",
            "参照先オブジェクト": "",
            "リレーション名": "",
            "備考": "",
        }
    )

    run_tables(output, [record])

    _, rows = _read_csv(output / "列名の対応.csv")
    dup_rows = [row for row in rows if row["表示名"] == "商談名"]
    assert len(dup_rows) == 2
    assert all(row["備考"] == "表示名が重複" for row in dup_rows)


def test_column_names_txt_format(tmp_path: Path) -> None:
    """``列名の対応_{管理番号}.txt`` は ``COLUMN_NAMES = {`` で始まり、
    解決できなかった列が ``# 未対応: ...`` のコメント行で末尾に並ぶ。
    """
    output = tmp_path / "output"
    output.mkdir()
    record = _make_record("1001", main_object="Opportunity")

    run_tables(output, [record], only_keys=["1001"])

    path = output / "列名の対応_1001.txt"
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# 管理番号: 1001\n")
    assert "COLUMN_NAMES = {\n" in text
    assert '    "AccountId": "取引先名",\n' in text
    # (不明) 列はコメント行に
    assert "# 未対応: Opp.Unknown" in text
    assert text.rstrip().endswith("}")


# ── 条件表: 項目列を対応表から埋める / 読める文字列への整形 ──────────────


def test_condition_table_resolves_item_from_column_map(tmp_path: Path) -> None:
    """``絞り込み`` の ``項目`` 列は ``column_map`` から解決した SOQL 名で埋まる。
    解決できなかった列キー（ ``column_map`` に無いもの ） は空のまま。
    """
    output = tmp_path / "output"
    output.mkdir()
    record = _make_record("1001", main_object="Opportunity")
    record.report["reportMetadata"]["reportFilters"] = [
        {"column": "Opp.Name", "operator": "contains", "value": "A"},
        {"column": "Opp.Missing", "operator": "equals", "value": "B"},
    ]

    run_tables(output, [record])

    _, rows = _read_csv(output / "条件表.csv")
    by_col: dict[str, dict[str, str]] = {}
    for row in rows:
        if row["種別"] == "絞り込み":
            by_col[row["列キー"]] = row

    assert by_col["Opp.Name"]["項目"] == "Name"
    # 対応表に無い列キーは空のまま
    assert by_col["Opp.Missing"]["項目"] == ""


def test_condition_table_top_rows_dict_is_human_readable(tmp_path: Path) -> None:
    """``topRows`` が dict（ ``rowLimit`` と ``direction`` ） のとき、
    そのまま ``str()`` せず ``"N件 direction"`` の形に整形する。
    """
    output = tmp_path / "output"
    output.mkdir()
    record = _make_record("1001", main_object="Opportunity")
    record.report["reportMetadata"]["topRows"] = {"rowLimit": 10, "direction": "Asc"}

    run_tables(output, [record])

    _, rows = _read_csv(output / "条件表.csv")
    top_row = next(row for row in rows if row["種別"] == "上位N")
    assert top_row["値"] == "10件 Asc"


def test_condition_table_top_rows_missing_keys_does_not_crash(tmp_path: Path) -> None:
    """``topRows`` の dict が ``rowLimit`` / ``direction`` どちらか欠けていても
    例外を上げず、 ある方だけ出力する。
    """
    output = tmp_path / "output"
    output.mkdir()
    record = _make_record("1001", main_object="Opportunity")

    # rowLimit のみ
    record.report["reportMetadata"]["topRows"] = {"rowLimit": 5}
    run_tables(output, [record])
    _, rows = _read_csv(output / "条件表.csv")
    top_row = next(row for row in rows if row["種別"] == "上位N")
    assert top_row["値"] == "5件"

    # direction のみ
    record.report["reportMetadata"]["topRows"] = {"direction": "Desc"}
    run_tables(output, [record])
    _, rows = _read_csv(output / "条件表.csv")
    top_row = next(row for row in rows if row["種別"] == "上位N")
    assert top_row["値"] == "Desc"


def test_condition_table_cross_filters_criteria_list_is_json(tmp_path: Path) -> None:
    """``crossFilters.criteria`` が dict のリストなら、 Python repr ではなく
    JSON 文字列として整形する。
    """
    output = tmp_path / "output"
    output.mkdir()
    record = _make_record("1001", main_object="Opportunity")
    record.report["reportMetadata"]["crossFilters"] = [
        {
            "primaryEntityField": "Opp.AccountId",
            "operator": "WITH",
            "relatedEntity": "OpportunityContactRole",
            "relatedEntityJoinField": "OpportunityId",
            "criteria": [{"column": "Role", "operator": "equals", "value": "X"}],
        }
    ]

    run_tables(output, [record])

    _, rows = _read_csv(output / "条件表.csv")
    cross_row = next(row for row in rows if row["種別"] == "クロス")
    # criteria は JSON 化され、 Python repr 特有の "['Role', 'equals']" は出ない
    assert "['Role'" not in cross_row["値"]
    assert '"Role"' in cross_row["値"]
    assert "WITH" in cross_row["値"]


def test_condition_table_standard_filters_is_json(tmp_path: Path) -> None:
    """``standardFilters`` は dict のリストで来る。 ``str(dict)`` ではなく
    JSON 文字列として整形する（ Python repr 対策 ）。
    """
    output = tmp_path / "output"
    output.mkdir()
    record = _make_record("1001", main_object="Opportunity")
    record.report["reportMetadata"]["standardFilters"] = [
        {"name": "Account.Industry", "value": "Banking"}
    ]

    run_tables(output, [record])

    _, rows = _read_csv(output / "条件表.csv")
    std_row = next(row for row in rows if row["種別"] == "標準フィルタ")
    # Python repr だと ``{'name': 'Account.Industry', ...}`` になる。
    # JSON は ``"name": "Account.Industry", ...`` で、 キー側はダブル
    # クォートで囲まれる。
    assert "{'name'" not in std_row["値"]
    assert '"name": "Account.Industry"' in std_row["値"]


# ── 集計表: 読める文字列への整形 ────────────────────────────────────────


def test_aggregate_table_groupings_down_with_date_granularity(tmp_path: Path) -> None:
    """``groupingsDown`` が ``name`` / ``sortOrder`` / ``dateGranularity`` を持つ
    とき、 ``dateGranularity`` があろうが ``sortOrder`` があろうが 1 行ずつ
    展開される（ Python repr 化しない ）。
    """
    output = tmp_path / "output"
    output.mkdir()
    record = _make_record("1001", main_object="Opportunity")
    record.report["reportMetadata"]["groupingsDown"] = [
        {
            "name": "Opp.CloseDate",
            "sortOrder": "Asc",
            "dateGranularity": "FISCAL_QUARTER",
        }
    ]

    run_tables(output, [record])

    _, rows = _read_csv(output / "集計表.csv")
    g_row = next(row for row in rows if row["種別"] == "グルーピング(行)")
    assert g_row["名前"] == "Opp.CloseDate"
    assert g_row["内容"] == "Asc"


def test_aggregate_table_sort_by_uses_sort_column_key(tmp_path: Path) -> None:
    """``sortBy`` はキー名 ``sortColumn`` / ``sortOrder`` （ Analytics API ）で
    読む。 名前が空のまま落ちないこと。
    """
    output = tmp_path / "output"
    output.mkdir()
    record = _make_record("1001", main_object="Opportunity")
    record.report["reportMetadata"]["sortBy"] = [{"sortColumn": "SUBJECT", "sortOrder": "Asc"}]

    run_tables(output, [record])

    _, rows = _read_csv(output / "集計表.csv")
    sort_row = next(row for row in rows if row["種別"] == "並び順")
    assert sort_row["名前"] == "SUBJECT"
    assert sort_row["内容"] == "Asc"


def test_aggregate_table_buckets_uses_developer_name_and_label(tmp_path: Path) -> None:
    """``buckets`` はキー名 ``developerName`` / ``label`` / ``bucketType``
    （ Analytics API ）で読む。 Python repr ではなく人が読める文字列にする。
    """
    output = tmp_path / "output"
    output.mkdir()
    record = _make_record("1001", main_object="Opportunity")
    record.report["reportMetadata"]["buckets"] = [
        {
            "developerName": "AmountBucket",
            "label": "金額帯",
            "bucketType": "number",
        }
    ]

    run_tables(output, [record])

    _, rows = _read_csv(output / "集計表.csv")
    b_row = next(row for row in rows if row["種別"] == "バケット")
    assert b_row["名前"] == "AmountBucket"
    assert "金額帯" in b_row["内容"]
    assert "number" in b_row["内容"]


def test_aggregate_table_custom_summary_formula(tmp_path: Path) -> None:
    """``customSummaryFormula`` は ``formula`` / ``label`` の dict リストで
    1 行ずつ展開される。 ラベルが ``内容`` に入る（ Python repr 化しない ）。
    """
    output = tmp_path / "output"
    output.mkdir()
    record = _make_record("1001", main_object="Opportunity")
    record.report["reportMetadata"]["customSummaryFormula"] = [
        {"formula": "RowCount", "label": "件数", "dataType": "number"}
    ]

    run_tables(output, [record])

    _, rows = _read_csv(output / "集計表.csv")
    csf_row = next(row for row in rows if row["種別"] == "独自計算式")
    assert csf_row["名前"] == "RowCount"
    assert csf_row["内容"] == "件数"


def test_aggregate_table_aggregates_is_list_of_strings(tmp_path: Path) -> None:
    """``aggregates`` は文字列のリスト。 dict 混じりは無視して 1 行ずつ展開。"""
    output = tmp_path / "output"
    output.mkdir()
    record = _make_record("1001", main_object="Opportunity")
    record.report["reportMetadata"]["aggregates"] = [
        "s!Opp.Amount",
        "n!Opp.RollUp",
        {"name": "Bad"},  # dict は無視
    ]

    run_tables(output, [record])

    _, rows = _read_csv(output / "集計表.csv")
    agg_rows = [row for row in rows if row["種別"] == "集計"]
    assert len(agg_rows) == 2
    assert {row["名前"] for row in agg_rows} == {"s!Opp.Amount", "n!Opp.RollUp"}


# ── 項目表: 段を relations から埋める ──────────────────────────────────


def test_field_table_dan_filled_from_relations_and_main(tmp_path: Path) -> None:
    """``項目表.csv`` の ``段`` は record の ``main_object`` （ 段 0 ） と
    ``relations`` の ``段`` から埋める。 同じオブジェクトが複数の record
    に出るときは最小の段を採用。
    """
    from src.fetch import RelationRecord

    output = tmp_path / "output"
    output.mkdir()

    objects_main = {
        "Task": {
            "name": "Task",
            "label": "活動",
            "custom": False,
            "fields": [{"name": "Subject", "label": "件名", "type": "string"}],
        },
        "Account": {
            "name": "Account",
            "label": "取引先",
            "custom": False,
            "fields": [{"name": "Name", "label": "取引先名", "type": "string"}],
        },
    }
    record_1 = ReportRecord(
        key="1001",
        summary="テスト1",
        report_id="00O000000000001",
        url="https://example/r/Report/00O000000000001/view",
        main_object="Task",
        report={},
        objects=objects_main,
        relations=(
            RelationRecord(
                parent="Task",
                field="WhatId",
                relationship_name="What",
                child="Account",
                depth=1,
            ),
        ),
        column_map=[],
        warnings=(),
    )

    # 同じ Account が別 record の主オブジェクト として 段 0 で出るケース
    objects_main2 = {
        "Account": {
            "name": "Account",
            "label": "取引先",
            "custom": False,
            "fields": [{"name": "Name", "label": "取引先名", "type": "string"}],
        },
    }
    record_2 = ReportRecord(
        key="1002",
        summary="テスト2",
        report_id="00O000000000002",
        url="https://example/r/Report/00O000000000002/view",
        main_object="Account",
        report={},
        objects=objects_main2,
        relations=(),
        column_map=[],
        warnings=(),
    )

    run_tables(output, [record_1, record_2])

    _, rows = _read_csv(output / "項目表.csv")
    by_obj: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        by_obj.setdefault(row["オブジェクト"], []).append(row)
    # Task は record_1 の主オブジェクトなので 段 0
    assert all(row["段"] == "0" for row in by_obj["Task"])
    # Account は record_1 では 段 1、 record_2 では 段 0。 最小の 0 を採用。
    assert all(row["段"] == "0" for row in by_obj["Account"])


# ── 既存テストで ``topRows="10"`` が壊れていないこと ────────────────────


def test_condition_table_top_rows_string_passthrough(tmp_path: Path) -> None:
    """``topRows`` が dict でない（ 文字列など ） ときは従来どおり ``str()``
    する。 既存テスト ``topRows="10"`` の挙動を壊さない。
    """
    output = tmp_path / "output"
    output.mkdir()
    record = _make_record("1001", main_object="Opportunity")
    record.report["reportMetadata"]["topRows"] = "10"

    run_tables(output, [record])

    _, rows = _read_csv(output / "条件表.csv")
    top_row = next(row for row in rows if row["種別"] == "上位N")
    assert top_row["値"] == "10"
