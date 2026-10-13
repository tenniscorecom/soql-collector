"""ダウンローダーとの契約テスト。

``tests/contract/列名の対応.contract.csv`` はバイト単位で見本であり、
``run_tables`` が ``列名の対応.csv`` を書いた結果が **バイト単位で一致** する
ことを確かめる。 並び・BOM・CRLF・列順・表示名の多数決（ ``違う表示名の
最小管理番号`` ）もまとめて検証される。

合わせて、 ``列の対応.csv`` （ 人が手で直す方 ） の扱いも、
``_merge_and_write_after_run`` 経由で固定する。
"""

from __future__ import annotations

import csv
import logging
from datetime import date
from pathlib import Path

import pytest

from src import column_map_csv
from src.column_map_csv import COLUMN_MAP_CSV_COLUMNS
from src.fetch import ReportRecord
from src.tables import run_tables

# ── ヘルパー ─────────────────────────────────────────────────────────────


CONTRACT_PATH = Path(__file__).parent / "contract" / "列名の対応.contract.csv"


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    """UTF-8 BOM 付き + CRLF の CSV を ``(columns, rows)`` で読む。"""
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        columns = next(reader)
        rows = [dict(zip(columns, line, strict=True)) for line in reader]
    return columns, rows


def _write_initial_csv(path: Path, rows: list[dict[str, str]]) -> None:
    """手作業の ``列の対応.csv`` を書く。 テスト用。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(COLUMN_MAP_CSV_COLUMNS))
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _make_record(
    key: str,
    *,
    main_object: str,
    column_map: list[dict[str, str]],
) -> ReportRecord:
    """``run_tables`` / ``_merge_and_write_after_run`` が読む属性だけを持った
    ``ReportRecord`` を作る。 他のテストで ``_FakeRecord`` を ``getattr`` 経由
    で読んでいるので、 公式 dataclass のインスタンスでも動く。
    """
    return ReportRecord(
        key=key,
        summary=f"テスト{key}",
        report_id=f"00O{key}",
        url=f"https://example.my.salesforce.com/lightning/r/Report/{key}/view",
        main_object=main_object,
        report={"reportMetadata": {"detailColumns": []}, "reportExtendedMetadata": {}},
        objects={},
        relations=(),
        column_map=column_map,
        warnings=(),
        row_check=None,
    )


def _column_map_entry(
    *,
    column_key: str,
    display_name: str,
    soql_name: str,
    owner_object: str = "",
    note: str = "",
) -> dict[str, str]:
    """``column_map`` の 1 行を作る。 自動が取れた ``soql_name`` を空にしたり
    ``(不明)`` にしたりして 4 種のシナリオを組み立てる。
    """
    return {
        "列キー": column_key,
        "表示名": display_name,
        "所属オブジェクト": owner_object,
        "対応フィールドAPI名": soql_name,
        "備考": note,
    }


# ── バイト一致の契約テスト ─────────────────────────────────────────────


def _build_contract_records() -> list[ReportRecord]:
    """``tests/contract/列名の対応.contract.csv`` とバイト一致する
    ``run_tables`` の出力を生む record 群を作る。

    シナリオ:
    - 1001 ( Opportunity ) : Account.Name / Amount / Name の 3 列
    - 1002 ( Opportunity ) : Amount （ 表示名 「 売上金額 」 ）
    - 1003 ( Opportunity ) : Amount （ 表示名 「 金額 」 ）
    - 1004 ( Task ) : Subject

    Amount は 2 本が 「 金額 」 ・ 1 本が 「 売上金額 」 。 「 売上金額 」 の
    最小管理番号は 1002 なので、 ``備考`` は
    ``別の表示名あり: 売上金額（1002）`` になる。
    """
    return [
        _make_record(
            "1001",
            main_object="Opportunity",
            column_map=[
                _column_map_entry(
                    column_key="Opp.Account.Name",
                    display_name="取引先名",
                    soql_name="Account.Name",
                    owner_object="Account",
                ),
                _column_map_entry(
                    column_key="Opp.Amount",
                    display_name="金額",
                    soql_name="Amount",
                    owner_object="Opportunity",
                ),
                _column_map_entry(
                    column_key="Opp.Name",
                    display_name="商談名",
                    soql_name="Name",
                    owner_object="Opportunity",
                ),
            ],
        ),
        _make_record(
            "1002",
            main_object="Opportunity",
            column_map=[
                _column_map_entry(
                    column_key="Opp.Amount",
                    display_name="売上金額",
                    soql_name="Amount",
                    owner_object="Opportunity",
                ),
            ],
        ),
        _make_record(
            "1003",
            main_object="Opportunity",
            column_map=[
                _column_map_entry(
                    column_key="Opp.Amount",
                    display_name="金額",
                    soql_name="Amount",
                    owner_object="Opportunity",
                ),
            ],
        ),
        _make_record(
            "1004",
            main_object="Task",
            column_map=[
                _column_map_entry(
                    column_key="Task.Subject",
                    display_name="件名",
                    soql_name="Subject",
                    owner_object="Task",
                ),
            ],
        ),
    ]


def test_run_tables_writes_byte_identical_contract_csv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``run_tables`` が書く ``列名の対応.csv`` は
    ``tests/contract/列名の対応.contract.csv`` と **バイト単位で完全に同じ** 。

    日付は 2026-10-14 に固定し、 出力の文字コード・改行・列順・並び順・表示名
    の多数決（ ``別の表示名あり: …`` ）をまとめて確かめる。
    """
    # 今日の日付を 2026-10-14 に固定
    monkeypatch.setattr("src.tables._today_local", lambda: date(2026, 10, 14))

    output_dir = tmp_path / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    run_tables(output_dir, _build_contract_records())

    actual = (output_dir / "列名の対応.csv").read_bytes()
    expected = CONTRACT_PATH.read_bytes()

    # バイト一致
    assert actual == expected, (
        "列名の対応.csv が契約の見本とバイト単位で一致しません。"
        f"\n実際: {actual!r}\n見本: {expected!r}"
    )


# ── 列の対応.csv のシナリオテスト ─────────────────────────────────────


def test_run_tables_overwrites_manual_when_auto_resolved_with_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """``run_tables`` 経由で、 **自動で取れた列は人の値で上書き** され、
    置き換えの WARNING が出る。

    既存の ``列の対応.csv`` にユーザーが ``Custom__c`` を手で入れていた
    ケースでも、 自動が ``Account.Name`` を出した時点で自動の結果で
    置換され、 メモは空に戻る。
    """
    path = tmp_path / "data" / "列の対応.csv"
    _write_initial_csv(
        path,
        [
            {
                "主オブジェクト": "Opportunity",
                "列キー": "Opp.Account.Name",
                "表示名": "取引先名",
                "SOQL列名": "Custom__c",
                "メモ": "手動",
                "使っているレポート数": "1",
                "更新日": "2025-12-01",
            },
        ],
    )
    existing = column_map_csv.load_column_map(path)

    auto_records = [
        _make_record(
            "1001",
            main_object="Opportunity",
            column_map=[
                _column_map_entry(
                    column_key="Opp.Account.Name",
                    display_name="取引先名",
                    soql_name="Account.Name",
                    owner_object="Account",
                ),
            ],
        ),
    ]

    caplog.set_level(logging.WARNING)
    column_map_csv._merge_and_write_after_run(
        path=path,
        existing=existing,
        records=auto_records,
        today=date(2026, 10, 14),
    )

    _, rows = _read_csv(path)
    assert len(rows) == 1
    row = rows[0]
    # 自動の ``Account.Name`` で上書きされる
    assert row["SOQL列名"] == "Account.Name"
    # メモは空に戻る
    assert row["メモ"] == ""
    # 更新日は今回の日付
    assert row["更新日"] == "2026-10-14"
    # 表示名はレポートの表示名で更新
    assert row["表示名"] == "取引先名"
    # 置き換えの WARNING が出ている
    assert any(
        "列の対応.csv の手入力を自動の結果で置き換えました" in record.getMessage()
        for record in caplog.records
    )


def test_run_tables_keeps_manual_when_auto_unknown(
    tmp_path: Path,
) -> None:
    """``run_tables`` 経由で、 **自動で取れなかった列** は人の値が残り、
    メモは 「 手入力 」 に統一される。
    """
    path = tmp_path / "data" / "列の対応.csv"
    _write_initial_csv(
        path,
        [
            {
                "主オブジェクト": "Opportunity",
                "列キー": "Opp.Mystery",
                "表示名": "謎列",
                "SOQL列名": "OldCustom__c",
                "メモ": "以前の手入力メモ",
                "使っているレポート数": "3",
                "更新日": "2025-12-01",
            },
        ],
    )
    existing = column_map_csv.load_column_map(path)

    auto_records = [
        _make_record(
            "1001",
            main_object="Opportunity",
            column_map=[
                _column_map_entry(
                    column_key="Opp.Mystery",
                    display_name="謎列 (新)",
                    soql_name="(不明)",  # 自動では取れなかった
                    owner_object="Opportunity",
                    note="対応フィールドなし",
                ),
            ],
        ),
    ]

    column_map_csv._merge_and_write_after_run(
        path=path,
        existing=existing,
        records=auto_records,
        today=date(2026, 10, 14),
    )

    _, rows = _read_csv(path)
    assert len(rows) == 1
    row = rows[0]
    # 人の値 ``OldCustom__c`` がそのまま
    assert row["SOQL列名"] == "OldCustom__c"
    # メモは 「 手入力 」 に統一
    assert row["メモ"] == "手入力"
    # 表示名はレポートの表示名で更新される
    assert row["表示名"] == "謎列 (新)"


def test_run_tables_keeps_manual_when_main_object_describe_fails(
    tmp_path: Path,
) -> None:
    """主オブジェクトの describe が失敗して ``main_object`` が ``None`` の
    record は、 ``_merge_and_write_after_run`` 側で完全にスキップされる。
    既存の ``列の対応.csv`` の手入力はそのまま残る。
    """
    path = tmp_path / "data" / "列の対応.csv"
    _write_initial_csv(
        path,
        [
            {
                "主オブジェクト": "Opportunity",
                "列キー": "Opp.Mystery",
                "表示名": "謎列",
                "SOQL列名": "OldCustom__c",
                "メモ": "手動",
                "使っているレポート数": "0",
                "更新日": "2025-12-01",
            },
        ],
    )
    existing = column_map_csv.load_column_map(path)

    # 主オブジェクトが ``None`` の record （ describe 失敗シナリオ ）
    failed_record = ReportRecord(
        key="1001",
        summary="失敗したレポート",
        report_id="00O000000000001",
        url="https://example.my.salesforce.com/lightning/r/Report/1001/view",
        main_object=None,  # describe 失敗
        report={"reportMetadata": {"detailColumns": []}, "reportExtendedMetadata": {}},
        objects={},
        relations=(),
        column_map=[
            _column_map_entry(
                column_key="Opp.Mystery",
                display_name="謎列",
                soql_name="(不明)",
                owner_object="(不明)",
                note="主オブジェクト不明",
            ),
        ],
        warnings=("主オブジェクトが特定できませんでした",),
        row_check=None,
    )

    column_map_csv._merge_and_write_after_run(
        path=path,
        existing=existing,
        records=[failed_record],
        today=date(2026, 10, 14),
    )

    _, rows = _read_csv(path)
    # 既存の手入力行がそのまま残っている
    assert len(rows) == 1
    row = rows[0]
    assert row["主オブジェクト"] == "Opportunity"
    assert row["列キー"] == "Opp.Mystery"
    assert row["SOQL列名"] == "OldCustom__c"
    assert row["メモ"] == "手動"
    assert row["更新日"] == "2025-12-01"


def test_run_tables_does_not_touch_failed_record_rows(
    tmp_path: Path,
) -> None:
    """**取得が失敗した管理番号の行は触られない** 。

    既存ファイルに管理番号 1002 と 1003 の手入力行がある。 この実行では
    1002 が失敗（ ``record`` が作られない ） し、 1003 だけ成功する。
    1002 の行はそのまま残り、 1003 の行は今回取った値で更新される。
    """
    path = tmp_path / "data" / "列の対応.csv"
    _write_initial_csv(
        path,
        [
            {
                "主オブジェクト": "Opportunity",
                "列キー": "Opp.Legacy",
                "表示名": "旧列",
                "SOQL列名": "Legacy__c",
                "メモ": "手動",
                "使っているレポート数": "3",
                "更新日": "2025-01-01",
            },
            {
                "主オブジェクト": "Opportunity",
                "列キー": "Opp.Amount",
                "表示名": "金額",
                "SOQL列名": "OldCustom__c",
                "メモ": "手動",
                "使っているレポート数": "5",
                "更新日": "2025-01-01",
            },
        ],
    )
    existing = column_map_csv.load_column_map(path)

    # 1003 だけが成功 （ 1002 は失敗して ``record`` が無い ）
    success_records = [
        _make_record(
            "1003",
            main_object="Opportunity",
            column_map=[
                _column_map_entry(
                    column_key="Opp.Amount",
                    display_name="金額",
                    soql_name="Amount",
                    owner_object="Opportunity",
                ),
            ],
        ),
    ]

    column_map_csv._merge_and_write_after_run(
        path=path,
        existing=existing,
        records=success_records,
        today=date(2026, 10, 14),
    )

    _, rows = _read_csv(path)
    by_key = {(r["主オブジェクト"], r["列キー"]): r for r in rows}

    # 1002 の 「 旧列 」 は完全にそのまま残る
    legacy_row = by_key[("Opportunity", "Opp.Legacy")]
    assert legacy_row["SOQL列名"] == "Legacy__c"
    assert legacy_row["メモ"] == "手動"
    assert legacy_row["使っているレポート数"] == "3"
    assert legacy_row["更新日"] == "2025-01-01"
    assert legacy_row["表示名"] == "旧列"

    # 1003 の 「 金額 」 は今回取った値で更新
    amount_row = by_key[("Opportunity", "Opp.Amount")]
    assert amount_row["SOQL列名"] == "Amount"
    assert amount_row["メモ"] == ""
    assert amount_row["更新日"] == "2026-10-14"
