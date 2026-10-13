"""``src/column_map_csv`` のテスト。

人が手で見比べる ``列の対応.csv`` の読み、 上書き適用、 マージ書き出しを
``tmp_path`` で検証する。 実 Salesforce には繋がない。
"""

from __future__ import annotations

import csv
import logging
from datetime import date
from pathlib import Path

import pytest
from comken.exceptions import CSVError

from src import column_map_csv
from src.column_map_csv import (
    COLUMN_MAP_CSV_COLUMNS,
    ColumnMapRow,
    ObservedColumnRow,
    apply_overrides,
    load_column_map,
    merge_and_write,
)

# ── ヘルパー ─────────────────────────────────────────────────────────────


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


def _make_object_describe(name: str, fields: list[dict]) -> dict:
    """``slim_object`` 互換の describe を作る （ ``fields`` を持つ dict ）。"""
    return {"name": name, "fields": fields}


# ── 読み込み ────────────────────────────────────────────────────────────


def test_load_column_map_returns_empty_when_file_missing(tmp_path: Path) -> None:
    """ファイルが存在しないときは空 dict を返す。"""
    path = tmp_path / "missing.csv"
    assert load_column_map(path) == {}


def test_load_column_map_raises_on_bad_header(tmp_path: Path) -> None:
    """見出しが違うと ``CSVError``。 ファイルは変更しない。"""
    path = tmp_path / "列の対応.csv"
    path.write_text(
        "主オブジェクト,列キー,SOQL列名\nOpportunity,Opp.Name,Name\n",
        encoding="utf-8-sig",
        newline="",
    )
    with pytest.raises(CSVError) as exc_info:
        load_column_map(path)
    # 「別名に退避してから再実行」の対処が含まれる
    assert "退避" in str(exc_info.value)
    # ファイルは変更しない
    assert path.read_text(encoding="utf-8-sig") == (
        "主オブジェクト,列キー,SOQL列名\nOpportunity,Opp.Name,Name\n"
    )


def test_load_column_map_reads_rows(tmp_path: Path) -> None:
    """正常系。 キーは ``(主オブジェクト小文字, 列キー)`` で返る。"""
    path = tmp_path / "列の対応.csv"
    _write_initial_csv(
        path,
        [
            {
                "主オブジェクト": "Opportunity",
                "列キー": "Opp.Name",
                "表示名": "商談名",
                "SOQL列名": "Name",
                "メモ": "手動",
                "使っているレポート数": "2",
                "更新日": "2026-01-01",
            },
        ],
    )
    rows = load_column_map(path)
    assert ("opportunity", "Opp.Name") in rows
    row = rows[("opportunity", "Opp.Name")]
    assert row.soql_name == "Name"
    assert row.display_name == "商談名"
    assert row.note == "手動"
    assert row.report_count == 2
    assert row.updated_at == "2026-01-01"


def test_load_column_map_skips_empty_key_rows(tmp_path: Path) -> None:
    """主オブジェクトか列キーが空の行は読み飛ばす。"""
    path = tmp_path / "列の対応.csv"
    _write_initial_csv(
        path,
        [
            {"主オブジェクト": "Opportunity", "列キー": "Opp.Name", "SOQL列名": "Name"},
            {"主オブジェクト": "", "列キー": "Opp.X", "SOQL列名": "X"},
            {"主オブジェクト": "Opportunity", "列キー": "", "SOQL列名": "X"},
        ],
    )
    rows = load_column_map(path)
    assert len(rows) == 1
    assert ("opportunity", "Opp.Name") in rows


# ── 上書き適用 ─────────────────────────────────────────────────────────


def test_apply_overrides_no_existing_is_noop() -> None:
    """``existing`` が空のときは ``column_map`` を触らずコピーだけ返す。"""
    column_map = [
        {
            "列キー": "Opp.Name",
            "表示名": "商談名",
            "所属オブジェクト": "Opportunity",
            "対応フィールドAPI名": "(不明)",
            "型": "",
            "備考": "対応フィールドなし",
        }
    ]
    objects = {"Opportunity": _make_object_describe("Opportunity", [])}
    warnings: list[str] = []
    result = apply_overrides(column_map, {}, "Opportunity", objects, warnings)
    assert len(result) == 1
    assert result[0]["対応フィールドAPI名"] == "(不明)"
    assert warnings == []


def test_apply_overrides_overrides_api_name_and_type(tmp_path: Path) -> None:
    """CSV に ``SOQL列名`` が入っていれば、 ``対応フィールドAPI名`` を上書きし、
    ``型`` を ``Account.Name`` のようなドット記法から引く。
    """
    column_map = [
        {
            "列キー": "Opp.Amount",
            "表示名": "金額",
            "所属オブジェクト": "Opportunity",
            "対応フィールドAPI名": "(不明)",
            "型": "",
            "参照先オブジェクト": "",
            "リレーション名": "",
            "備考": "対応フィールドなし",
        },
        {
            "列キー": "Opp.Account",
            "表示名": "取引先",
            "所属オブジェクト": "Opportunity",
            "対応フィールドAPI名": "(不明)",
            "型": "",
            "参照先オブジェクト": "",
            "リレーション名": "",
            "備考": "対応フィールドなし",
        },
    ]
    objects = {
        "Opportunity": _make_object_describe(
            "Opportunity",
            [
                {"name": "Amount", "type": "currency"},
                {
                    "name": "AccountId",
                    "type": "reference",
                    "relationshipName": "Account",
                    "referenceTo": ["Account"],
                },
            ],
        ),
        "Account": _make_object_describe(
            "Account",
            [{"name": "Name", "type": "string"}],
        ),
    }
    existing = {
        ("opportunity", "Opp.Amount"): ColumnMapRow(
            main_object="Opportunity",
            column_key="Opp.Amount",
            display_name="金額",
            soql_name="Amount",
            note="",
            report_count=0,
            updated_at="",
        ),
        ("opportunity", "Opp.Account"): ColumnMapRow(
            main_object="Opportunity",
            column_key="Opp.Account",
            display_name="取引先",
            soql_name="Account.Name",
            note="",
            report_count=0,
            updated_at="",
        ),
    }
    warnings: list[str] = []
    result = apply_overrides(column_map, existing, "Opportunity", objects, warnings)

    amount_row = next(r for r in result if r["列キー"] == "Opp.Amount")
    assert amount_row["対応フィールドAPI名"] == "Amount"
    assert amount_row["型"] == "currency"
    assert amount_row["備考"] == "列の対応.csv の値"

    account_row = next(r for r in result if r["列キー"] == "Opp.Account")
    assert account_row["対応フィールドAPI名"] == "Account.Name"
    # ``Account`` は ``objects`` にあるので たどれて ``Name`` の ``string`` が引ける
    assert account_row["型"] == "string"
    assert account_row["備考"] == "列の対応.csv の値"
    assert warnings == []


def test_apply_overrides_keeps_value_when_soql_missing() -> None:
    """CSV に ``SOQL列名`` が無い行は触らない。"""
    column_map = [
        {
            "列キー": "Opp.Name",
            "表示名": "商談名",
            "所属オブジェクト": "Opportunity",
            "対応フィールドAPI名": "(不明)",
            "型": "",
            "備考": "対応フィールドなし",
        }
    ]
    objects = {"Opportunity": _make_object_describe("Opportunity", [])}
    existing = {
        ("opportunity", "Opp.Name"): ColumnMapRow(
            main_object="Opportunity",
            column_key="Opp.Name",
            display_name="",
            soql_name="",  # 手動で空のままにしている
            note="保留中",
            report_count=0,
            updated_at="",
        ),
    }
    warnings: list[str] = []
    result = apply_overrides(column_map, existing, "Opportunity", objects, warnings)
    # 触らないので元のまま
    assert result[0]["対応フィールドAPI名"] == "(不明)"
    assert result[0]["備考"] == "対応フィールドなし"
    assert warnings == []


def test_apply_overrides_warns_when_type_not_found(tmp_path: Path) -> None:
    """SOQL 列名が ``objects`` に無いパスを通ると WARNING が出て、 値はそのまま使う。"""
    column_map = [
        {
            "列キー": "Opp.Foo",
            "表示名": "Foo",
            "所属オブジェクト": "Opportunity",
            "対応フィールドAPI名": "(不明)",
            "型": "",
            "参照先オブジェクト": "",
            "リレーション名": "",
            "備考": "対応フィールドなし",
        }
    ]
    objects = {
        "Opportunity": _make_object_describe("Opportunity", [{"name": "Name", "type": "string"}]),
    }
    existing = {
        ("opportunity", "Opp.Foo"): ColumnMapRow(
            main_object="Opportunity",
            column_key="Opp.Foo",
            display_name="Foo",
            soql_name="DoesNotExist",
            note="",
            report_count=0,
            updated_at="",
        ),
    }
    warnings: list[str] = []
    result = apply_overrides(column_map, existing, "Opportunity", objects, warnings)
    # 上書きは適用される。 ただし ``型`` が引けないので空のまま
    assert result[0]["対応フィールドAPI名"] == "DoesNotExist"
    assert result[0]["型"] == ""
    assert any("列の対応.csv の SOQL列名が項目定義に見つかりません" in w for w in warnings)
    assert any("Opportunity / Opp.Foo / DoesNotExist" in w for w in warnings)


def test_apply_overrides_resolves_dot_notation_through_relationship(tmp_path: Path) -> None:
    """``Account.Owner.Name`` のようなドット記法を ``Opportunity`` から
    ``Account`` → ``User`` へとたどって型を引く。 ``Account`` の
    ``relationshipName`` が ``Account`` で ``objects`` にあるものを使う。
    """
    column_map = [
        {
            "列キー": "Opp.Account.Owner",
            "表示名": "所有者",
            "所属オブジェクト": "Opportunity",
            "対応フィールドAPI名": "(不明)",
            "型": "",
            "参照先オブジェクト": "",
            "リレーション名": "",
            "備考": "対応フィールドなし",
        }
    ]
    objects = {
        "Opportunity": _make_object_describe(
            "Opportunity",
            [
                {
                    "name": "AccountId",
                    "type": "reference",
                    "relationshipName": "Account",
                    "referenceTo": ["Account"],
                },
            ],
        ),
        "Account": _make_object_describe(
            "Account",
            [
                {
                    "name": "OwnerId",
                    "type": "reference",
                    "relationshipName": "Owner",
                    "referenceTo": ["User"],
                },
            ],
        ),
        "User": _make_object_describe("User", [{"name": "Name", "type": "string"}]),
    }
    existing = {
        ("opportunity", "Opp.Account.Owner"): ColumnMapRow(
            main_object="Opportunity",
            column_key="Opp.Account.Owner",
            display_name="所有者",
            soql_name="Account.Owner.Name",
            note="",
            report_count=0,
            updated_at="",
        ),
    }
    warnings: list[str] = []
    result = apply_overrides(column_map, existing, "Opportunity", objects, warnings)
    assert result[0]["対応フィールドAPI名"] == "Account.Owner.Name"
    assert result[0]["型"] == "string"
    assert warnings == []


def test_apply_overrides_uses_main_object_as_key_for_related_columns(tmp_path: Path) -> None:
    """``column_map`` 行の ``所属オブジェクト`` が ``Account`` （ 関連経由で
    自動解決された状態 ） でも、 鍵は引数で渡した ``main_object`` (``Opportunity``)
    なので、 ``(Opportunity, 列キー)`` として保存された手入力が拾われる。
    ``所属オブジェクト`` を見ると 別のキーを引いてしまい手入力が消える、
    という旧実装のバグを踏ませないことが目的。
    """
    column_map = [
        {
            "列キー": "Opp.Account.Name",
            "表示名": "取引先名",
            "所属オブジェクト": "Account",  # 自動解決で関連側に振られた
            "対応フィールドAPI名": "Account.Name",
            "型": "string",
            "備考": "関連オブジェクト経由",
        },
    ]
    objects = {
        "Opportunity": _make_object_describe(
            "Opportunity",
            [
                {
                    "name": "AccountId",
                    "type": "reference",
                    "relationshipName": "Account",
                    "referenceTo": ["Account"],
                },
            ],
        ),
        "Account": _make_object_describe(
            "Account",
            [
                {"name": "Name", "type": "string"},
                {"name": "Custom__c", "type": "string"},
            ],
        ),
    }
    # ユーザーが ``(Opportunity, Opp.Account.Name)`` の行に ``Account.Custom__c``
    # を入れた （ 自動で取れた ``Account.Name`` ではなく ）
    existing = {
        ("opportunity", "Opp.Account.Name"): ColumnMapRow(
            main_object="Opportunity",
            column_key="Opp.Account.Name",
            display_name="取引先名",
            soql_name="Account.Custom__c",
            note="手動",
            report_count=1,
            updated_at="",
        ),
    }
    warnings: list[str] = []
    result = apply_overrides(column_map, existing, "Opportunity", objects, warnings)
    # 手入力が優先される。 自動の ``Account.Name`` ではなく ``Account.Custom__c``
    assert result[0]["対応フィールドAPI名"] == "Account.Custom__c"
    assert result[0]["備考"] == "列の対応.csv の値"
    assert result[0]["型"] == "string"
    assert warnings == []


def test_apply_overrides_uses_main_object_as_key_when_owner_empty(tmp_path: Path) -> None:
    """``column_map`` 行の ``所属オブジェクト`` が空でも、 ``main_object``
    引数を鍵に使う。 これで「 手前側の列が主オブジェクトなのに空文字 」 という
    ケースを上書きできる。
    """
    column_map = [
        {
            "列キー": "Opp.Amount",
            "表示名": "金額",
            "所属オブジェクト": "",  # 空でも key として使わない
            "対応フィールドAPI名": "(不明)",
            "型": "",
            "備考": "対応フィールドなし",
        },
    ]
    objects = {"Opportunity": _make_object_describe("Opportunity", [])}
    existing = {
        ("opportunity", "Opp.Amount"): ColumnMapRow(
            main_object="Opportunity",
            column_key="Opp.Amount",
            display_name="金額",
            soql_name="Amount",
            note="",
            report_count=0,
            updated_at="",
        ),
    }
    warnings: list[str] = []
    result = apply_overrides(column_map, existing, "Opportunity", objects, warnings)
    assert result[0]["対応フィールドAPI名"] == "Amount"
    assert result[0]["備考"] == "列の対応.csv の値"


# ── _merge_and_write_after_run ────────────────────────────────


class _FakeRecord:
    """``_merge_and_write_after_run`` が ``getattr(record, "main_object", ...)``
    と ``record.column_map`` を読む互換の偽 record。"""

    def __init__(self, main_object: str | None, column_map: list[dict[str, str]]) -> None:
        self.main_object = main_object
        self.column_map = column_map


def test_merge_and_write_after_run_keys_by_main_object(tmp_path: Path) -> None:
    """``_merge_and_write_after_run`` は ``record.main_object`` を鍵に使う。
    ``column_map`` 行の ``所属オブジェクト`` が ``Account`` でも CSV では
    ``(Opportunity, 列キー)`` の1行だけに出る。 旧実装だと ``Account`` 主体の
    行ができて、 手入力と別行になる・二重化する。
    """
    path = tmp_path / "列の対応.csv"
    record = _FakeRecord(
        main_object="Opportunity",
        column_map=[
            {
                "列キー": "Opp.Account.Name",
                "表示名": "取引先名",
                "所属オブジェクト": "Account",  # 自動解決で Account に振られた
                "対応フィールドAPI名": "Account.Name",
                "備考": "関連オブジェクト経由",
            },
            {
                "列キー": "Opp.Amount",
                "表示名": "金額",
                "所属オブジェクト": "Opportunity",
                "対応フィールドAPI名": "Amount",
                "備考": "",
            },
        ],
    )
    column_map_csv._merge_and_write_after_run(
        path=path,
        existing={},
        records=[record],
        today=date(2026, 1, 1),
    )

    _, rows = _read_csv(path)
    assert len(rows) == 2
    by_key = {(r["主オブジェクト"], r["列キー"]): r for r in rows}
    # 関連経由の列も主オブジェクトが Opportunity に出る
    assert ("Opportunity", "Opp.Account.Name") in by_key
    assert ("Opportunity", "Opp.Amount") in by_key
    # Account 主体の行は作られない
    assert all(r["主オブジェクト"] == "Opportunity" for r in rows)


def test_merge_and_write_after_run_keeps_manual_value_across_runs(tmp_path: Path) -> None:
    """2 実行シナリオ:

    1. 1 回目: 自動が ``Account.Name`` を解決した （ ``所属オブジェクト`` =
       ``Account`` ）。 CSV は新規で ``(Opportunity, Opp.Account.Name)`` の
       行に ``Account.Name`` が書かれる。
    2. ユーザーが手で ``SOQL列名`` を ``Custom__c`` に書き換える。
    3. 2 回目: 同じ自動結果で再走。 「 自動が取った ``Account.Name`` 」 で
       手入力 ``Custom__c`` が上書きされず、 行も二重化しない。

    旧実装は 1 回目で ``(Account, Opp.Account.Name)`` を作ってユーザーの手入力
    と別キーにしていた。 そのため 2 回目でも手入力が拾われず、 行が二重になる。
    """
    path = tmp_path / "列の対応.csv"
    auto_column_map = [
        {
            "列キー": "Opp.Account.Name",
            "表示名": "取引先名",
            "所属オブジェクト": "Account",
            "対応フィールドAPI名": "Account.Name",
            "備考": "関連オブジェクト経由",
        },
    ]

    # 1 回目: 既存ファイル無し
    column_map_csv._merge_and_write_after_run(
        path=path,
        existing={},
        records=[_FakeRecord("Opportunity", auto_column_map)],
        today=date(2026, 1, 1),
    )

    # ユーザーが手で CSV を編集して ``Custom__c`` に書き換え
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
                "更新日": "2026-01-01",
            },
        ],
    )
    existing = load_column_map(path)

    # 2 回目: 同じ自動結果で再走
    column_map_csv._merge_and_write_after_run(
        path=path,
        existing=existing,
        records=[_FakeRecord("Opportunity", auto_column_map)],
        today=date(2026, 1, 2),
    )

    _, rows = _read_csv(path)
    assert len(rows) == 1
    assert rows[0]["主オブジェクト"] == "Opportunity"
    assert rows[0]["列キー"] == "Opp.Account.Name"
    # 手動値の ``Custom__c`` が残り、 ``Account.Name`` で上書きされない
    assert rows[0]["SOQL列名"] == "Custom__c"
    # 件数と更新日は今回分
    assert rows[0]["使っているレポート数"] == "1"
    assert rows[0]["更新日"] == "2026-01-02"


def test_merge_and_write_after_run_keeps_manual_when_auto_becomes_resolvable(
    tmp_path: Path,
) -> None:
    """2 実行シナリオ:

    1. 最初は自動では取れなかったが、 ユーザーが ``(Opportunity, 列キー)`` の
       行に手で ``Custom__c`` を入れる。
    2. 次の実行で自動でも ``Account.Name`` が取れた。 手入力が消えず、 行も
       二重化しない。

    旧実装は 2 回目に ``(Account, 列キー)`` の行を新規追加するため、 二重化する。
    """
    path = tmp_path / "列の対応.csv"
    path.parent.mkdir(parents=True, exist_ok=True)

    # 1 回目: ユーザーが CSV に手で ``Custom__c`` を入れた （ 自動は取れていない ）
    _write_initial_csv(
        path,
        [
            {
                "主オブジェクト": "Opportunity",
                "列キー": "Opp.Account.Name",
                "表示名": "取引先名",
                "SOQL列名": "Custom__c",
                "メモ": "手動",
                "使っているレポート数": "0",
                "更新日": "2026-01-01",
            },
        ],
    )
    existing = load_column_map(path)

    # 2 回目: 自動でも ``Account.Name`` が取れた
    auto_column_map = [
        {
            "列キー": "Opp.Account.Name",
            "表示名": "取引先名",
            "所属オブジェクト": "Account",
            "対応フィールドAPI名": "Account.Name",
            "備考": "関連オブジェクト経由",
        },
    ]
    column_map_csv._merge_and_write_after_run(
        path=path,
        existing=existing,
        records=[_FakeRecord("Opportunity", auto_column_map)],
        today=date(2026, 1, 2),
    )

    _, rows = _read_csv(path)
    assert len(rows) == 1
    assert rows[0]["主オブジェクト"] == "Opportunity"
    assert rows[0]["列キー"] == "Opp.Account.Name"
    # 手動値 ``Custom__c`` は消えない
    assert rows[0]["SOQL列名"] == "Custom__c"


def test_merge_and_write_after_run_skips_records_without_main_object(tmp_path: Path) -> None:
    """主オブジェクトが分からない record （ 例: 主オブジェクト不明でスキップ
    した case ） は、 鍵が作れないのでスキップする。 行は書かれず、 ファイルは
    見出しだけになる。
    """
    path = tmp_path / "列の対応.csv"
    column_map_csv._merge_and_write_after_run(
        path=path,
        existing={},
        records=[
            _FakeRecord("", []),  # main_object が空
            _FakeRecord(None, []),  # main_object が None
        ],
        today=date(2026, 1, 1),
    )
    # 行は出てこない （ observed が空 ）
    columns, rows = _read_csv(path)
    assert columns == list(COLUMN_MAP_CSV_COLUMNS)
    assert rows == []


# ── マージ書き出し ───────────────────────────────────────────────────────


def test_merge_and_write_creates_initial_rows(tmp_path: Path) -> None:
    """既存ファイルが無いときは全行を足す。"""
    path = tmp_path / "列の対応.csv"
    observed = [
        ObservedColumnRow(
            main_object="Opportunity",
            column_key="Opp.Name",
            display_name="商談名",
            soql_name="Name",
            note="",
        ),
        ObservedColumnRow(
            main_object="Opportunity",
            column_key="Opp.Unknown",
            display_name="不明列",
            soql_name="",
            note="対応フィールドなし",
        ),
    ]
    merge_and_write(path, {}, observed, today=date(2026, 1, 1))

    columns, rows = _read_csv(path)
    assert columns == list(COLUMN_MAP_CSV_COLUMNS)
    by_key = {(r["主オブジェクト"], r["列キー"]): r for r in rows}
    assert by_key[("Opportunity", "Opp.Name")]["SOQL列名"] == "Name"
    assert by_key[("Opportunity", "Opp.Name")]["メモ"] == ""
    assert by_key[("Opportunity", "Opp.Name")]["表示名"] == "商談名"
    assert by_key[("Opportunity", "Opp.Name")]["使っているレポート数"] == "1"
    assert by_key[("Opportunity", "Opp.Name")]["更新日"] == "2026-01-01"
    assert by_key[("Opportunity", "Opp.Unknown")]["SOQL列名"] == ""
    assert by_key[("Opportunity", "Opp.Unknown")]["メモ"] == "対応フィールドなし"


def test_merge_and_write_keeps_existing_value_when_observed_also_has_one(tmp_path: Path) -> None:
    """既存ファイルに値が入っているセルは、 自動の結果が「 正しそう 」 でも
    上書きしない。
    """
    path = tmp_path / "列の対応.csv"
    _write_initial_csv(
        path,
        [
            {
                "主オブジェクト": "Opportunity",
                "列キー": "Opp.Amount",
                "表示名": "金額",
                "SOQL列名": "OldCustom__c",
                "メモ": "手動で決めた",
                "使っているレポート数": "5",
                "更新日": "2025-12-01",
            },
        ],
    )
    existing = load_column_map(path)
    observed = [
        ObservedColumnRow(
            main_object="Opportunity",
            column_key="Opp.Amount",
            display_name="金額 (新)",
            soql_name="Amount",  # 自動は "Amount" を取った
            note="自動結果",
        ),
    ]
    merge_and_write(path, existing, observed, today=date(2026, 1, 1))

    _, rows = _read_csv(path)
    assert len(rows) == 1
    row = rows[0]
    # 既存の値は壊さない
    assert row["SOQL列名"] == "OldCustom__c"
    assert row["メモ"] == "手動で決めた"
    # 表示名は触らない
    assert row["表示名"] == "金額"
    # 件数と更新日は今回分
    assert row["使っているレポート数"] == "1"
    assert row["更新日"] == "2026-01-01"


def test_merge_and_write_fills_empty_cells_only(tmp_path: Path) -> None:
    """既存行の ``SOQL列名`` が空で、 自動の結果が有れば埋める。 ``表示名`` /
    ``メモ`` も同じ。"""
    path = tmp_path / "列の対応.csv"
    _write_initial_csv(
        path,
        [
            {
                "主オブジェクト": "Opportunity",
                "列キー": "Opp.Amount",
                "表示名": "金額",
                "SOQL列名": "",
                "メモ": "",
                "使っているレポート数": "0",
                "更新日": "",
            },
        ],
    )
    existing = load_column_map(path)
    observed = [
        ObservedColumnRow(
            main_object="Opportunity",
            column_key="Opp.Amount",
            display_name="金額",
            soql_name="Amount",
            note="",
        ),
    ]
    merge_and_write(path, existing, observed, today=date(2026, 1, 1))

    _, rows = _read_csv(path)
    row = rows[0]
    assert row["SOQL列名"] == "Amount"
    assert row["メモ"] == ""
    assert row["表示名"] == "金額"
    assert row["使っているレポート数"] == "1"
    assert row["更新日"] == "2026-01-01"


def test_merge_and_write_keeps_unobserved_rows(tmp_path: Path) -> None:
    """今回の実行で出てこなかった行はそのまま残る。"""
    path = tmp_path / "列の対応.csv"
    _write_initial_csv(
        path,
        [
            {
                "主オブジェクト": "Opportunity",
                "列キー": "Opp.Old",
                "表示名": "旧列",
                "SOQL列名": "Old__c",
                "メモ": "今は使われない",
                "使っているレポート数": "3",
                "更新日": "2025-01-01",
            },
        ],
    )
    existing = load_column_map(path)
    observed: list[ObservedColumnRow] = []  # 今回の実行では 1 行も取れなかった
    merge_and_write(path, existing, observed, today=date(2026, 1, 1))

    _, rows = _read_csv(path)
    assert len(rows) == 1
    row = rows[0]
    assert row["列キー"] == "Opp.Old"
    assert row["SOQL列名"] == "Old__c"
    assert row["メモ"] == "今は使われない"
    # 今回出てこなかった行は丸ごとそのまま残る（ 「 丸ごとそのまま残る 」 ）
    assert row["使っているレポート数"] == "3"
    assert row["更新日"] == "2025-01-01"


def test_merge_and_write_permission_error_warns_only(tmp_path: Path, caplog) -> None:
    """書き込みが ``PermissionError`` のとき WARNING だけ出て、 他は進まない。"""
    path = tmp_path / "列の対応.csv"
    existing: dict[tuple[str, str], ColumnMapRow] = {}
    observed = [
        ObservedColumnRow(
            main_object="Opportunity",
            column_key="Opp.Name",
            display_name="商談名",
            soql_name="Name",
            note="",
        ),
    ]

    caplog.set_level(logging.WARNING)

    def _raise_perm(p: Path, rows: list[ColumnMapRow]) -> None:
        raise PermissionError("他のプロセスが使用中です")

    # writer を注入して ``PermissionError`` を投げる
    merge_and_write(
        path,
        existing,
        observed,
        today=date(2026, 1, 1),
        writer=_raise_perm,
    )

    assert any(
        "列の対応.csv を書けませんでした" in record.getMessage() for record in caplog.records
    )
    # ファイルは作られない
    assert not path.exists()


def test_merge_and_write_failed_header_raises(tmp_path: Path) -> None:
    """既存ファイルが見出し違いのときは ``CSVError`` を上げる。 既存ファイルは変更しない。"""
    path = tmp_path / "列の対応.csv"
    path.write_text(
        "主オブジェクト,列キー,SOQL列名\nOpportunity,Opp.Name,Name\n",
        encoding="utf-8-sig",
        newline="",
    )
    # ``load_column_map`` で見出し違いを弾く
    with pytest.raises(CSVError):
        load_column_map(path)
    # ファイルは変更されていない
    assert path.read_text(encoding="utf-8-sig") == (
        "主オブジェクト,列キー,SOQL列名\nOpportunity,Opp.Name,Name\n"
    )


# ── Settings 経由のテスト ─────────────────────────────────────────────


def test_load_settings_default_column_map_csv_path(tmp_path: Path) -> None:
    """``[FILES] COLUMN_MAP_CSV_PATH`` 未設定のとき既定値 ``./data/列の対応.csv``。"""
    from src.settings import load_settings

    master_path = tmp_path / "master.xlsx"
    (tmp_path / "config.ini").write_text(
        f"[FILES]\nMASTER_XLSX_PATH = {master_path}\n",
        encoding="utf-8",
    )
    settings = load_settings(tmp_path)
    assert settings.column_map_csv_path == (tmp_path / "data" / "列の対応.csv").resolve()


def test_load_settings_soql_count_default_disabled(tmp_path: Path) -> None:
    """``[CHECKS] SOQL_COUNT`` が無いとき既定の ``×``（ False ）。"""
    from src.settings import load_settings

    master_path = tmp_path / "master.xlsx"
    (tmp_path / "config.ini").write_text(
        f"[FILES]\nMASTER_XLSX_PATH = {master_path}\n",
        encoding="utf-8",
    )
    settings = load_settings(tmp_path)
    assert settings.soql_count is False
