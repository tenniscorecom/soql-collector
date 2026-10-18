"""ダウンローダーとの契約テスト。

``tests/contract/列名の対応.contract.csv`` はバイト単位で見本であり、
``run_tables`` が ``列名の対応.csv`` を書いた結果が **バイト単位で一致** する
ことを確かめる。 並び・BOM・CRLF・列順・表示名の多数決（``違う表示名の
最小管理番号``）もまとめて検証される。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from src.fetch import ReportRecord
from src.tables import run_tables

# ── ヘルパー ─────────────────────────────────────────────────────────────


CONTRACT_PATH = Path(__file__).parent / "contract" / "列名の対応.contract.csv"


def _make_record(
    key: str,
    *,
    main_object: str,
    column_map: list[dict[str, str]],
) -> ReportRecord:
    """``run_tables`` が読む属性だけを持った ``ReportRecord`` を作る。"""
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
    """``column_map`` の 1 行を作る（``mapping.build_column_map`` と同じキーをそろえる）。"""
    return {
        "列キー": column_key,
        "表示名": display_name,
        "所属オブジェクト": owner_object,
        "対応フィールドAPI名": soql_name,
        "型": "",
        "参照先オブジェクト": "",
        "リレーション名": "",
        "備考": note,
    }


# ── バイト一致の契約テスト ─────────────────────────────────────────────


def _build_contract_records() -> list[ReportRecord]:
    """``tests/contract/列名の対応.contract.csv`` とバイト一致する
    ``run_tables`` の出力を生む record 群を作る。

    シナリオ:
    - 1001 ( Opportunity ) : Account.Name / Amount / Name の 3 列
    - 1002 ( Opportunity ) : Amount （表示名 「売上金額」）
    - 1003 ( Opportunity ) : Amount （表示名 「金額」）
    - 1004 ( Task ) : Subject

    Amount は 2 本が 「金額」 ・ 1 本が 「売上金額」 。 「売上金額」 の
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

    日付を 2026-10-14 に固定し、 出力の文字コード・改行・列順・並び順・表示名
    の多数決（``別の表示名あり: …``）をまとめて確かめる。
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
