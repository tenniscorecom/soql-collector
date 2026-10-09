"""``tables`` のテスト。``OUTPUT_DIR`` に小さな JSON を手で並べて、
対応表・項目表の内容を厳密に確かめる（ネットワークには出ない）。
"""

from __future__ import annotations

import csv
import json
import logging
from pathlib import Path

import pytest

from src.tables import (
    CORRESPONDENCE_COLUMNS,
    FIELD_TABLE_COLUMNS,
    TableOutcome,
    run_tables,
)

# ── ヘルパー ────────────────────────────────────────────────────────────────


def _write_json(output_dir: Path, payload: dict) -> Path:
    """1 件の JSON を ``OUTPUT_DIR/{管理番号}.json`` に書く（テスト用）。"""
    key = str(payload["管理番号"])
    path = output_dir / f"{key}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    """CSV を ``(columns, rows)`` で読む。 ``csv.DictReader`` 経由。"""
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.reader(file)
        columns = next(reader)
        rows = [dict(zip(columns, line, strict=True)) for line in reader]
    return columns, rows


def _make_correspondence_payload(
    key: str = "1001",
    *,
    main_object: str | None = "Opportunity",
    timestamp: str = "2024-01-01T00:00:00",
) -> dict:
    """典型的な JSON を作る。参照項目・ポリモーフィック・カスタム項目・選択肢
    を含む小さい ``Opportunity`` describe。"""
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
                    "picklist": ["A", "B", "D"],  # active な値だけ
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
    return {
        "管理番号": key,
        "概要": f"テスト{sample_key_summary(key)}",
        "レポートID": "00O000000000001",
        "URL": "https://example.my.salesforce.com/lightning/r/Report/00O000000000001/view",
        "取得日時": timestamp,
        "主オブジェクト": main_object,
        "report": {
            "reportMetadata": {
                "reportType": {"type": main_object or "Opportunity"},
                "reportFormat": "TABULAR",
            }
        },
        "objects": objects,
        "column_map": [
            {
                "列キー": "Opp.Account",
                "表示名": "取引先名",
                "対応フィールドAPI名": "AccountId",
                "型": "reference",
                "備考": "",
            },
            {
                "列キー": "Opp.LeadOrContact",
                "表示名": "担当者名",
                "対応フィールドAPI名": "WhoId",
                "型": "reference",
                "備考": "ポリモーフィック",
            },
            {
                "列キー": "Opp.Custom",
                "表示名": "カスタム",
                "対応フィールドAPI名": "Custom__c",
                "型": "picklist",
                "備考": "",
            },
            {
                "列キー": "Opp.Name",
                "表示名": "商談名",
                "対応フィールドAPI名": "Name",
                "型": "string",
                "備考": "",
            },
            {
                "列キー": "Opp.Unknown",
                "表示名": "不明列",
                "対応フィールドAPI名": "(不明)",
                "型": "",
                "備考": "表示名一致せず",
            },
        ],
        "warnings": [],
    }


def sample_key_summary(key: str) -> str:
    """``概要`` 用の文字列を作るだけのヘルパー（テスト本体ではない）。"""
    return key


# ── 対応表（per-ID） ─────────────────────────────────────────────────────────


def test_correspondence_per_id_csv_basic(tmp_path: Path) -> None:
    """参照項目には ``参照先オブジェクト`` と ``リレーション名`` が埋まる。"""
    output = tmp_path / "output"
    output.mkdir()
    _write_json(output, _make_correspondence_payload("1001", main_object="Opportunity"))

    outcome: TableOutcome = run_tables(output, only_keys=["1001"])
    assert isinstance(outcome, TableOutcome)

    assert (output / "対応表_1001.csv").exists()
    columns, rows = _read_csv(output / "対応表_1001.csv")
    assert columns == list(CORRESPONDENCE_COLUMNS)
    assert len(rows) == 5

    # 1 行目: AccountId（referenceTo=[Account], relationshipName=Account）
    row = rows[0]
    assert row["管理番号"] == "1001"
    assert row["レポートID"] == "00O000000000001"
    assert row["レポートタイプ"] == "Opportunity"
    assert row["主オブジェクト"] == "Opportunity"
    assert row["列キー"] == "Opp.Account"
    assert row["表示名"] == "取引先名"
    assert row["対応フィールドAPI名"] == "AccountId"
    assert row["型"] == "reference"
    assert row["参照先オブジェクト"] == "Account"
    assert row["リレーション名"] == "Account"

    # 2 行目: WhoId（ポリモーフィック。 ``referenceTo`` は ``|`` 区切り、
    # ``relationshipName`` は無いので空）
    row = rows[1]
    assert row["対応フィールドAPI名"] == "WhoId"
    assert row["参照先オブジェクト"] == "Contact|Lead"
    assert row["リレーション名"] == ""

    # 3 行目: Custom__c（picklist。 referenceTo / relationshipName は空）
    row = rows[2]
    assert row["対応フィールドAPI名"] == "Custom__c"
    assert row["参照先オブジェクト"] == ""
    assert row["リレーション名"] == ""

    # 4 行目: Name（参照無し）
    row = rows[3]
    assert row["対応フィールドAPI名"] == "Name"
    assert row["参照先オブジェクト"] == ""
    assert row["リレーション名"] == ""

    # 5 行目: (不明)（対応フィールドが取れなかった列）
    row = rows[4]
    assert row["対応フィールドAPI名"] == "(不明)"
    # (不明) は describe に存在しないので空のまま出る
    assert row["参照先オブジェクト"] == ""
    assert row["リレーション名"] == ""


def test_correspondence_main_object_null(tmp_path: Path) -> None:
    """``主オブジェクト`` が ``None`` の JSON でも、対応表に行が出る
    （主オブジェクトと参照先の列は空のまま）。
    """
    output = tmp_path / "output"
    output.mkdir()
    payload = _make_correspondence_payload("1001", main_object=None)
    _write_json(output, payload)

    run_tables(output, only_keys=["1001"])

    _, rows = _read_csv(output / "対応表_1001.csv")
    assert len(rows) == 5
    # 主オブジェクトが空でも各行は出る
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
    _write_json(output, _make_correspondence_payload("1001"))

    run_tables(output, only_keys=["1001"])

    path = output / "対応表_1001.csv"
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "BOM が無い"
    # 行末は ``\r\n``
    assert b"\r\n" in raw
    # ``\n\r`` や ``\n`` だけの改行が無い（ ``\r\n`` の中に ``\n`` が
    # 含まれるので、 ``\n\r`` だけが単独で無ければよい）
    assert b"\n\r" not in raw.replace(b"\r\n", b"")


def test_correspondence_uses_atomic_write(tmp_path: Path) -> None:
    """CSV 書き込みが ``atomic_write`` 経由である（途中で失敗したら既存 CSV が
    壊れない）ことを、 ``atomic_write`` をパッチして例外を上げて確かめる。
    """
    from src import tables as tables_module

    output = tmp_path / "output"
    output.mkdir()
    _write_json(output, _make_correspondence_payload("1001"))

    # 既存の CSV を置いておき、 途中で例外が上がっても壊れないことを確かめる
    existing = output / "対応表_1001.csv"
    existing.write_text("既存\nそのまま\n", encoding="utf-8")

    real_atomic = tables_module.atomic_write
    called = {"count": 0}

    def broken_atomic_write(path):
        called["count"] += 1
        raise OSError("爆発")

    tables_module.atomic_write = broken_atomic_write  # type: ignore[assignment]
    try:
        run_tables(output, only_keys=["1001"])
    except OSError:
        pass
    finally:
        tables_module.atomic_write = real_atomic  # type: ignore[assignment]

    # ``atomic_write`` が必ず通っている
    assert called["count"] >= 1
    # 既存 CSV は残っている（上書きされていない）
    assert existing.read_text(encoding="utf-8") == "既存\nそのまま\n"


# ── 対応表（全体） ─────────────────────────────────────────────────────────


def test_overall_correspondence_concatenates_and_sorts(tmp_path: Path) -> None:
    """全 ID の ``対応表.csv`` は、管理番号の昇順で連結される。"""
    output = tmp_path / "output"
    output.mkdir()
    _write_json(output, _make_correspondence_payload("1002"))
    _write_json(output, _make_correspondence_payload("1001"))  # ファイル名は逆順
    _write_json(output, _make_correspondence_payload("1003"))

    run_tables(output)

    _, rows = _read_csv(output / "対応表.csv")
    keys = [row["管理番号"] for row in rows]
    # 1001 → 1002 → 1003 の順で、各 5 行（column_map の列数）
    assert keys == ["1001"] * 5 + ["1002"] * 5 + ["1003"] * 5


def test_overall_correspondence_skips_broken_json(tmp_path: Path) -> None:
    """壊れた JSON は飛ばして警告ログを出し、他の ID の処理は続ける。"""
    output = tmp_path / "output"
    output.mkdir()
    _write_json(output, _make_correspondence_payload("1001"))
    # 壊れた JSON
    (output / "broken.json").write_text("{壊れた JSON", encoding="utf-8")
    # トップレベルが dict でない
    (output / "list.json").write_text("[1, 2, 3]", encoding="utf-8")
    # ``管理番号`` が無い
    (output / "no_key.json").write_text("{}", encoding="utf-8")

    outcome = run_tables(output)

    # 全体対応表には壊れた JSON が含まれない
    _, rows = _read_csv(output / "対応表.csv")
    keys = {row["管理番号"] for row in rows}
    assert keys == {"1001"}

    # 飛ばしたファイルは skipped に入る
    skipped_names = {path.name for path in outcome.skipped}
    assert skipped_names == {"broken.json", "list.json", "no_key.json"}


# ── 項目表 ─────────────────────────────────────────────────────────────────


def test_field_table_dedups_same_object_across_jsons(tmp_path: Path) -> None:
    """同じオブジェクトが複数の JSON に出ても、項目表では 1 回だけにする。"""
    output = tmp_path / "output"
    output.mkdir()
    payload1 = _make_correspondence_payload("1001", main_object="Opportunity")
    payload2 = _make_correspondence_payload("1002", main_object="Opportunity")
    # 別の JSON でも同じ Opportunity を入れる
    payload2["objects"]["Opportunity"] = payload1["objects"]["Opportunity"]
    _write_json(output, payload1)
    _write_json(output, payload2)

    run_tables(output)

    _, rows = _read_csv(output / "項目表.csv")
    object_rows = [row for row in rows if row["オブジェクト"] == "Opportunity"]
    # Opportunity の fields は 4 件。重複しない
    assert len(object_rows) == 4
    field_names = {row["項目API名"] for row in object_rows}
    assert field_names == {"AccountId", "WhoId", "Custom__c", "Name"}


def test_field_table_prefers_newer_timestamp(tmp_path: Path) -> None:
    """同じオブジェクトが複数の JSON にあるとき、 ``取得日時`` が新しい方の
    describe を使う。"""
    output = tmp_path / "output"
    output.mkdir()

    payload_old = _make_correspondence_payload(
        "1001", main_object="Opportunity", timestamp="2024-01-01T00:00:00"
    )
    payload_old["objects"]["Opportunity"]["label"] = "古いラベル"
    payload_old["objects"]["Opportunity"]["fields"] = [
        {"name": "OldOnly", "label": "古い項目", "type": "string"},
    ]

    payload_new = _make_correspondence_payload(
        "1002", main_object="Opportunity", timestamp="2024-12-01T00:00:00"
    )
    payload_new["objects"]["Opportunity"]["label"] = "新しいラベル"
    payload_new["objects"]["Opportunity"]["fields"] = [
        {"name": "NewOnly", "label": "新しい項目", "type": "string"},
    ]

    _write_json(output, payload_old)
    _write_json(output, payload_new)

    run_tables(output)

    _, rows = _read_csv(output / "項目表.csv")
    opp_rows = [row for row in rows if row["オブジェクト"] == "Opportunity"]
    # 新しい方の describe が使われている
    labels = {row["オブジェクト表示名"] for row in opp_rows}
    assert labels == {"新しいラベル"}
    field_names = {row["項目API名"] for row in opp_rows}
    assert field_names == {"NewOnly"}


def test_field_table_picklist_active_only_and_truncated(tmp_path: Path) -> None:
    """``picklist`` （ active な ``value`` だけの配列） を ``|`` 区切りにする。
    ``picklistTotal`` が 30 を超えるとき末尾に ``…`` を 1 個足して切る。
    ``custom=True`` は ``○``、 ``custom=False`` は空。
    """
    output = tmp_path / "output"
    output.mkdir()

    picklist = [f"V{i}" for i in range(31)]  # 31 値
    payload = _make_correspondence_payload("1001", main_object="Opportunity")
    # Custom__c を 31 件の ``picklist`` 配列に差し替え（ ``picklistTotal`` は 31 ）
    for field in payload["objects"]["Opportunity"]["fields"]:
        if field["name"] == "Custom__c":
            field["picklist"] = picklist
            field["picklistTotal"] = 31
            break

    _write_json(output, payload)

    run_tables(output, only_keys=["1001"])

    _, rows = _read_csv(output / "項目表.csv")
    custom_row = next(row for row in rows if row["項目API名"] == "Custom__c")
    choices = custom_row["選択肢"].split("|")
    # 30 件で打ち切られて末尾に ``…`` （ 31 要素 ）
    assert len(choices) == 31
    assert choices[-1] == "…"
    # 先頭の V0..V29 だけ
    assert choices[0] == "V0"
    assert choices[29] == "V29"
    # カスタム項目は ``○``
    assert custom_row["カスタム"] == "○"

    # 普通の項目は ``カスタム`` が空
    name_row = next(row for row in rows if row["項目API名"] == "Name")
    assert name_row["カスタム"] == ""
    assert name_row["選択肢"] == ""


def test_field_table_picklist_total_at_boundary(tmp_path: Path) -> None:
    """``picklistTotal`` の境界値: 30 ちょうどなら ``…`` は付かず、
    31 なら ``…`` が 1 個付く（ ``picklist`` 自体が 30 個でも、
    ``picklistTotal`` が 31 なら「 後ろにもある」 と検知する ）。"""
    output = tmp_path / "output"
    output.mkdir()
    payload = _make_correspondence_payload("1001", main_object="Opportunity")

    # ``Picked__c`` を追加: ``picklist`` 30 個 / ``picklistTotal`` 30
    payload["objects"]["Opportunity"]["fields"].append(
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
    _write_json(output, payload)

    run_tables(output, only_keys=["1001"])

    _, rows = _read_csv(output / "項目表.csv")
    row = next(r for r in rows if r["項目API名"] == "Picked__c")
    choices = row["選択肢"].split("|")
    # ``…`` は付かない（ 30 ちょうど ）
    assert len(choices) == 30
    assert "…" not in row["選択肢"]
    # 先頭と末尾
    assert choices[0] == "V0"
    assert choices[29] == "V29"


def test_field_table_picklist_total_exceeds_list_size(tmp_path: Path) -> None:
    """``picklist`` が 30 個に絞られていても ``picklistTotal`` が 31 以上なら
    末尾に ``…`` が 1 個付く（ 「 もっと後ろがある」 を検知 ）。"""
    output = tmp_path / "output"
    output.mkdir()
    payload = _make_correspondence_payload("1001", main_object="Opportunity")
    payload["objects"]["Opportunity"]["fields"].append(
        {
            "name": "BigPick__c",
            "label": "大きい選択肢",
            "picklist": [f"V{i}" for i in range(30)],
            "picklistTotal": 50,
        }
    )
    _write_json(output, payload)

    run_tables(output, only_keys=["1001"])

    _, rows = _read_csv(output / "項目表.csv")
    row = next(r for r in rows if r["項目API名"] == "BigPick__c")
    choices = row["選択肢"].split("|")
    # 30 + ``…`` = 31 要素
    assert len(choices) == 31
    assert choices[-1] == "…"
    # 先頭は V0
    assert choices[0] == "V0"


def test_field_table_polymorphic_reference_and_relationship(tmp_path: Path) -> None:
    """ポリモーフィックな参照項目は ``参照先オブジェクト`` を ``|`` 区切りで出す。
    ``relationshipName`` が無いときは空のまま。
    """
    output = tmp_path / "output"
    output.mkdir()
    _write_json(output, _make_correspondence_payload("1001", main_object="Opportunity"))

    run_tables(output, only_keys=["1001"])

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

    payload_a = _make_correspondence_payload("1001", main_object="Account")
    payload_a["objects"]["Account"]["label"] = "取引先"
    payload_a["objects"]["Account"]["fields"] = [
        {"name": "Name", "label": "取引先名", "type": "string"}
    ]
    payload_o = _make_correspondence_payload("1002", main_object="Opportunity")
    payload_o["objects"]["Opportunity"]["label"] = "商談"
    payload_o["objects"]["Opportunity"]["fields"] = [
        {"name": "Name", "label": "商談名", "type": "string"}
    ]
    _write_json(output, payload_a)
    _write_json(output, payload_o)

    run_tables(output)

    columns, rows = _read_csv(output / "項目表.csv")
    # ``段`` 列が先頭近くに入る （ ``オブジェクト`` の次 ）
    assert columns == list(FIELD_TABLE_COLUMNS)
    assert columns[1] == "段"
    objects = [row["オブジェクト"] for row in rows]
    # Account → Opportunity の順
    assert objects == ["Account", "Opportunity"]


def test_field_table_depth_column_from_related(tmp_path: Path) -> None:
    """``関連オブジェクト`` の段と経路から ``段`` 列を埋める。 主=0、 関連=段の数字、
    ``objects/*.json`` だけの個別オブジェクトは空。
    """
    output = tmp_path / "output"
    output.mkdir()

    # レポート JSON: 主=Task(0), 関連=Account(1), 関連=User(2)
    payload = _make_correspondence_payload("1001", main_object="Task")
    payload["objects"]["Account"] = {
        "name": "Account",
        "label": "取引先",
        "custom": False,
        "fields": [{"name": "Name", "label": "取引先名", "type": "string"}],
    }
    payload["objects"]["User"] = {
        "name": "User",
        "label": "ユーザ",
        "custom": False,
        "fields": [{"name": "Alias", "label": "別名", "type": "string"}],
    }
    payload["関連オブジェクト"] = [
        {"名前": "Account", "段": 1, "経路": ["Task.WhatId"]},
        {"名前": "User", "段": 2, "経路": ["Task.WhatId", "Account.OwnerId"]},
    ]
    _write_json(output, payload)

    # 個別オブジェクト: Standalone は段が空
    _write_object_json(
        output,
        "Standalone",
        _make_object_payload("Standalone", label="単独"),
    )

    run_tables(output)

    _, rows = _read_csv(output / "項目表.csv")
    by_obj: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        by_obj.setdefault(row["オブジェクト"], []).append(row)

    # 主オブジェクト
    assert all(r["段"] == "0" for r in by_obj["Task"])
    # 関連 (段)
    assert all(r["段"] == "1" for r in by_obj["Account"])
    assert all(r["段"] == "2" for r in by_obj["User"])
    # 個別オブジェクトは段が空
    assert all(r["段"] == "" for r in by_obj["Standalone"])


# ── ファイル単位の挙動 ─────────────────────────────────────────────────────


def test_no_json_in_output_dir_writes_empty_csvs(tmp_path: Path) -> None:
    """``OUTPUT_DIR`` が空のときは CSV も空（見出しのみ）になる。"""
    output = tmp_path / "output"
    output.mkdir()

    outcome = run_tables(output)

    # CSV 3 つは作られるが見出しだけ
    correspondence_path = output / "対応表.csv"
    field_path = output / "項目表.csv"
    assert correspondence_path.exists()
    assert field_path.exists()

    _, corr_rows = _read_csv(correspondence_path)
    _, field_rows = _read_csv(field_path)
    assert corr_rows == []
    assert field_rows == []

    # per-ID CSV は無い
    assert list(output.glob("対応表_*.csv")) == []

    assert outcome.wrote == (correspondence_path, field_path)
    assert outcome.skipped == ()


def test_only_keys_filters_per_id_csv(tmp_path: Path) -> None:
    """``only_keys`` を指定すると、 per-ID CSV はその管理番号だけ作られ、
    全体 CSV は ``OUTPUT_DIR`` の全 JSON から作られる。
    """
    output = tmp_path / "output"
    output.mkdir()
    _write_json(output, _make_correspondence_payload("1001"))
    _write_json(output, _make_correspondence_payload("1002"))
    _write_json(output, _make_correspondence_payload("1003"))

    run_tables(output, only_keys=["1001", "1003"])

    # per-ID CSV は 1001 / 1003 だけ
    per_id = sorted(p.name for p in output.glob("対応表_*.csv"))
    assert per_id == ["対応表_1001.csv", "対応表_1003.csv"]

    # 全体対応表には 3 つぶん全ての
    _, corr_rows = _read_csv(output / "対応表.csv")
    keys = {row["管理番号"] for row in corr_rows}
    assert keys == {"1001", "1002", "1003"}

    # 項目表にも 3 つぶん全ての Opportunity
    _, rows = _read_csv(output / "項目表.csv")
    assert any(row["オブジェクト"] == "Opportunity" for row in rows)


def test_output_dir_missing_does_not_raise(tmp_path: Path) -> None:
    """``OUTPUT_DIR`` が存在しないときは例外を上げず、 フォルダを作って
    からの空 CSV（見出しのみ）を書く。
    なぜ: ``tables`` を「先に fetch しましたか」 のチェックをしないで
    呼んでもよいように（ ``_cmd_tables`` が空チェックで止めている）。
    """
    output = tmp_path / "missing"  # 存在しない
    assert not output.exists()

    outcome = run_tables(output)

    # フォルダは作られ、 全体対応表・項目表は空で見出しだけ
    assert output.exists()
    correspondence_path = output / "対応表.csv"
    field_path = output / "項目表.csv"
    assert correspondence_path.exists()
    assert field_path.exists()
    _, corr_rows = _read_csv(correspondence_path)
    _, field_rows = _read_csv(field_path)
    assert corr_rows == []
    assert field_rows == []
    # 飛ばしたファイルは無い
    assert outcome.skipped == ()


# ── ログの出し方 ───────────────────────────────────────────────────────────


def test_tables_logs_counts_and_skipped_only(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """``tables`` は CSV のパスと壊れた JSON のファイル名だけをログに書き、
    describe の中身（フィールド名など）は出さない。
    なぜ: ログに機微な情報が混ざらないことを保証するため。
    """
    output = tmp_path / "output"
    output.mkdir()
    _write_json(output, _make_correspondence_payload("1001"))
    (output / "broken.json").write_text("{壊れた", encoding="utf-8")

    with caplog.at_level(logging.INFO):
        run_tables(output)

    messages = [record.getMessage() for record in caplog.records]
    # 出力した CSV のメッセージ
    assert any("対応表_1001.csv" in m for m in messages)
    assert any("対応表.csv" in m for m in messages)
    assert any("項目表.csv" in m for m in messages)
    # 壊れた JSON をスキップした警告
    assert any("壊れた JSON" in m and "broken.json" in m for m in messages)
    # describe の中身（ "AccountId" / "Custom__c" など）はログに含まれない
    for field_token in ("AccountId", "WhoId", "Custom__c", "商談名"):
        for message in messages:
            assert field_token not in message, (
                f"describe の中身 {field_token!r} が漏れています: {message}"
            )


# ── objects/*.json ──────────────────────────────────────────────────────


def _write_object_json(output_dir: Path, name: str, payload: dict) -> Path:
    """``OUTPUT_DIR/objects/{Name}.json`` を書く（ テスト用 ）。"""
    objects_dir = output_dir / "objects"
    objects_dir.mkdir(parents=True, exist_ok=True)
    path = objects_dir / f"{name}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _make_object_payload(
    name: str,
    *,
    label: str = "オブジェクト",
    fields: list[dict] | None = None,
    timestamp: str = "2024-01-01T00:00:00",
) -> dict:
    """``objects/{Name}.json`` 用の payload を作る。"""
    fields = fields if fields is not None else [{"name": "Name", "label": "名前", "type": "string"}]
    return {
        "取得日時": timestamp,
        "オブジェクト": name,
        "object": {"name": name, "label": label, "custom": False, "fields": fields},
    }


def test_field_table_includes_objects_subdirectory(tmp_path: Path) -> None:
    """``objects/*.json`` のオブジェクトが項目表に集約される。"""
    output = tmp_path / "output"
    output.mkdir()
    _write_object_json(
        output,
        "Account",
        _make_object_payload(
            "Account",
            label="取引先",
            fields=[
                {"name": "Name", "label": "取引先名", "type": "string"},
                {"name": "Industry", "label": "業種", "type": "picklist"},
            ],
        ),
    )

    run_tables(output)

    _, rows = _read_csv(output / "項目表.csv")
    account_rows = [r for r in rows if r["オブジェクト"] == "Account"]
    assert len(account_rows) == 2
    field_names = {r["項目API名"] for r in account_rows}
    assert field_names == {"Name", "Industry"}


def test_field_table_prefers_newer_timestamp_across_report_and_object(
    tmp_path: Path,
) -> None:
    """同じオブジェクトがレポート JSON と ``objects/*.json`` の両方にあれば、
    ``取得日時`` が新しい方の describe が使われる。
    """
    output = tmp_path / "output"
    output.mkdir()

    # レポート JSON 側 （ 古 ）
    payload_report = _make_correspondence_payload(
        "1001", main_object="Opportunity", timestamp="2024-01-01T00:00:00"
    )
    payload_report["objects"]["Opportunity"]["label"] = "古いラベル"
    payload_report["objects"]["Opportunity"]["fields"] = [
        {"name": "OldOnly", "label": "古い項目", "type": "string"},
    ]
    _write_json(output, payload_report)

    # objects/ 側（ 新 ）
    _write_object_json(
        output,
        "Opportunity",
        _make_object_payload(
            "Opportunity",
            label="新しいラベル",
            fields=[{"name": "NewOnly", "label": "新しい項目", "type": "string"}],
            timestamp="2024-12-01T00:00:00",
        ),
    )

    run_tables(output)

    _, rows = _read_csv(output / "項目表.csv")
    opp_rows = [r for r in rows if r["オブジェクト"] == "Opportunity"]
    labels = {r["オブジェクト表示名"] for r in opp_rows}
    assert labels == {"新しいラベル"}
    field_names = {r["項目API名"] for r in opp_rows}
    assert field_names == {"NewOnly"}


def test_objects_subdirectory_not_read_as_report_json(tmp_path: Path) -> None:
    """``objects/`` 配下の JSON はレポート JSON として読まれない
    （ 対応表には行が出ない ）。
    """
    output = tmp_path / "output"
    output.mkdir()

    # レポート JSON 側（ `管理番号` 付き ）
    _write_json(output, _make_correspondence_payload("1001", main_object="Opportunity"))

    # objects/ 側（ `オブジェクト` 付き ）
    _write_object_json(
        output,
        "Account",
        _make_object_payload(
            "Account",
            fields=[{"name": "Name", "label": "名前", "type": "string"}],
        ),
    )

    run_tables(output)

    # 対応表はレポート側 のみ
    _, corr_rows = _read_csv(output / "対応表.csv")
    keys = {r["管理番号"] for r in corr_rows}
    assert keys == {"1001"}
    # Account は項目表には出る （ objects/ は正しく拾われる ）
    _, field_rows = _read_csv(output / "項目表.csv")
    objects_in_field = {r["オブジェクト"] for r in field_rows}
    assert "Account" in objects_in_field
    assert "Opportunity" in objects_in_field


def test_objects_subdirectory_handles_missing_folder(tmp_path: Path) -> None:
    """``OUTPUT_DIR/objects/`` が無いときは普通に動く。"""
    output = tmp_path / "output"
    output.mkdir()
    _write_json(output, _make_correspondence_payload("1001", main_object="Opportunity"))

    outcome = run_tables(output)

    # エラーにならず、 既存の挙動と同じく ``対応表.csv`` / ``項目表.csv`` を作る
    assert (output / "対応表.csv").exists()
    assert (output / "項目表.csv").exists()
    assert outcome.skipped == ()
