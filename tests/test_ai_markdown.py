"""``ai_markdown`` のテスト。

``ReportRecord`` を直接作って Markdown 文字列の全文を期待値と比べる （偽
record だけでネットワークには繋がない） 。 仕様の見出し順・表・エスケープ
・注意の箇条書きを 1 つずつ確かめる
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from src import pii
from src.ai_markdown import (
    MARKDOWN_SUBDIR,
    build_markdown,
)
from src.fetch import RelationRecord, ReportRecord, RowCheck
from src.soql_builder import (
    CROSS_FILTER_LINK_DIRECT,
    CROSS_FILTER_LINK_NOT_DIRECT,
    CROSS_FILTER_LINK_UNVERIFIED,
    CrossFilterLink,
    SoqlDraft,
)
from src.soql_similar import SimilarSoql
from src.tables import run_tables

# ── ヘルパー ─────────────────────────────────────────────────────────────


def _make_object_describe() -> dict[str, dict]:
    """テストで使う偽 describe。 Task / Account / User / Contact の 4 種。"""
    return {
        "Task": {
            "name": "Task",
            "label": "活動",
            "custom": False,
            "fields": [
                {
                    "name": "Subject",
                    "label": "件名",
                    "type": "string",
                    "custom": False,
                    "referenceTo": [],
                    "relationshipName": None,
                    "picklist": [],
                    "picklistTotal": 0,
                },
                {
                    "name": "WhatId",
                    "label": "関連先",
                    "type": "reference",
                    "custom": False,
                    "referenceTo": ["Account"],
                    "relationshipName": "What",
                    "picklist": [],
                    "picklistTotal": 0,
                },
                {
                    "name": "OwnerId",
                    "label": "担当者",
                    "type": "reference",
                    "custom": False,
                    "referenceTo": ["User"],
                    "relationshipName": "Owner",
                    "picklist": [],
                    "picklistTotal": 0,
                },
            ],
        },
        "Account": {
            "name": "Account",
            "label": "取引先",
            "custom": False,
            "fields": [
                {
                    "name": "Name",
                    "label": "取引先名",
                    "type": "string",
                    "custom": False,
                    "referenceTo": [],
                    "relationshipName": None,
                    "picklist": [],
                    "picklistTotal": 0,
                },
                {
                    "name": "OwnerId",
                    "label": "所有者",
                    "type": "reference",
                    "custom": False,
                    "referenceTo": ["User"],
                    "relationshipName": "Owner",
                    "picklist": [],
                    "picklistTotal": 0,
                },
            ],
        },
        "User": {
            "name": "User",
            "label": "ユーザ",
            "custom": False,
            "fields": [
                {
                    "name": "Name",
                    "label": "氏名",
                    "type": "string",
                    "custom": False,
                    "referenceTo": [],
                    "relationshipName": None,
                    "picklist": [],
                    "picklistTotal": 0,
                },
            ],
        },
        "Contact": {
            "name": "Contact",
            "label": "取引先責任者",
            "custom": False,
            "fields": [
                {
                    "name": "Name",
                    "label": "氏名",
                    "type": "string",
                    "custom": False,
                    "referenceTo": [],
                    "relationshipName": None,
                    "picklist": [],
                    "picklistTotal": 0,
                },
            ],
        },
    }


def _make_record(
    key: str = "1001",
    *,
    main_object: str | None = "Task",
    summary: str | None = None,
    report_id: str = "00O000000000001",
    column_map: list[dict[str, str]] | None = None,
    relations: tuple[RelationRecord, ...] = (),
    warnings: tuple[str, ...] = (),
    row_check: RowCheck | None = None,
    pii_status: str = "なし",
    report_metadata_override: dict | None = None,
    soql_draft: object | None = None,
) -> ReportRecord:
    """TABULAR シナリオ用の典型 ``ReportRecord`` を作る。"""
    summary_text = summary if summary is not None else f"テスト{key}"
    if column_map is None:
        column_map = [
            {
                "列キー": "SUBJECT",
                "表示名": "件名",
                "所属オブジェクト": "Task",
                "対応フィールドAPI名": "Subject",
                "型": "string",
                "参照先オブジェクト": "",
                "リレーション名": "",
                "備考": "",
            },
            {
                "列キー": "WHAT.NAME",
                "表示名": "取引先名",
                "所属オブジェクト": "Account",
                "対応フィールドAPI名": "What.Name",
                "型": "string",
                "参照先オブジェクト": "",
                "リレーション名": "What",
                "備考": "",
            },
        ]
    report_metadata: dict = {
        "id": report_id,
        "name": summary_text,
        "reportType": {"type": main_object or "Task"},
        "reportFormat": "TABULAR",
        "scope": "",
        "detailColumns": ["SUBJECT", "WHAT.NAME"],
        "reportFilters": [
            {"column": "SUBJECT", "operator": "contains", "value": "A"},
            {"column": "WHAT.NAME", "operator": "equals", "value": "B"},
        ],
        "reportBooleanFilter": "1 AND (2 OR 3)",
        "standardDateFilter": None,
        "standardFilters": [],
        "crossFilters": [],
        "groupingsDown": [],
        "groupingsAcross": [],
        "aggregates": [],
        "sortBy": [{"sortColumn": "SUBJECT", "sortOrder": "Asc"}],
        "topRows": None,
    }
    if report_metadata_override is not None:
        for key_name, value in report_metadata_override.items():
            report_metadata[key_name] = value
    report = {
        "reportMetadata": report_metadata,
        "reportExtendedMetadata": {
            "detailColumnInfo": {
                "SUBJECT": {"label": "件名"},
                "WHAT.NAME": {"label": "取引先名"},
            },
        },
    }
    objects: dict[str, dict] = {}
    if main_object:
        objects = dict(_make_object_describe().items())
    return ReportRecord(
        key=key,
        summary=summary_text,
        report_id=report_id,
        url=f"https://example.my.salesforce.com/lightning/r/Report/{report_id}/view",
        main_object=main_object,
        report=report,
        objects=objects,
        relations=relations,
        column_map=column_map,
        warnings=warnings,
        row_check=row_check,
        pii=pii.ReportPIIResult(status=pii_status, matching_columns=()),
        soql_draft=soql_draft,
    )


# ── TABULAR の全文比較 ────────────────────────────────────────────────────


def test_ai_markdown_tabular_full_text() -> None:
    """TABULAR の record （列 3 本のうち 1 本が複数候補、 絞り込み 2 件+組立式、
    並び順、 関連 1 段） の Markdown が期待値と完全に一致する。
    """
    column_map = [
        {
            "列キー": "SUBJECT",
            "表示名": "件名",
            "所属オブジェクト": "Task",
            "対応フィールドAPI名": "Subject",
            "型": "string",
            "参照先オブジェクト": "",
            "リレーション名": "",
            "備考": "",
        },
        {
            "列キー": "WHAT.NAME",
            "表示名": "取引先名",
            "所属オブジェクト": "Account",
            "対応フィールドAPI名": "What.Name",
            "型": "string",
            "参照先オブジェクト": "",
            "リレーション名": "What",
            "備考": "",
        },
        {
            "列キー": "AMBIGUOUS",
            "表示名": "曖昧な列",
            "所属オブジェクト": "",
            "対応フィールドAPI名": "(不明)",
            "型": "",
            "参照先オブジェクト": "",
            "リレーション名": "",
            "備考": "複数候補あり: Account.Owner（浅い）, Contact.Owner",
        },
    ]
    relations = (
        RelationRecord(
            parent="Task", field="WhatId", relationship_name="What", child="Account", depth=1
        ),
    )
    record = _make_record(
        column_map=column_map,
        relations=relations,
        row_check=RowCheck(status="超えていない", rows=42, reason=None),
        report_metadata_override={
            "detailColumns": ["SUBJECT", "WHAT.NAME", "AMBIGUOUS"],
        },
    )

    expected = (
        "> このファイルを丸ごと AI に貼ってください。"
        "SOQL の下書きを、下の資料と照らして直すよう頼みます。\n"
        "\n"
        "# 1001 テスト1001\n"
        "\n"
        "- レポートID: 00O000000000001\n"
        "- 主オブジェクト: Task\n"
        "- レポート形式: TABULAR\n"
        "- 2000件超: 超えていない（42件）\n"
        "- 個人情報: なし\n"
        "\n"
        "## 列の対応\n"
        "| 表示名 | 使う SOQL 列名 | どこから | 候補 |\n"
        "| --- | --- | --- | --- |\n"
        "| 件名 | Subject | 自動 |  |\n"
        "| 取引先名 | What.Name | 自動 |  |\n"
        "| 曖昧な列 | (不明) | 取れず | Account.Owner（浅い）, Contact.Owner |\n"
        "\n"
        "## 絞り込み\n"
        "組立式: `1 AND (2 OR 3)`\n"
        "| 番号 | 表示名 | SOQL列名 | 演算子 | 値 |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| 1 | 件名 | Subject | contains | A |\n"
        "| 2 | 取引先名 | What.Name | equals | B |\n"
        "\n"
        "## 並び順\n"
        "- SUBJECT Asc\n"
        "\n"
        "## グループ・集計\n"
        "なし\n"
        "\n"
        "## 関連オブジェクトの経路\n"
        "- Task → What（Account）\n"
        "\n"
        "## 似たレポート\n"
        "なし\n"
        "\n"
        "## 似たSOQL\n"
        "なし\n"
        "\n"
        "## 警告\n"
        "なし\n"
        "\n"
        "関係図 （Mermaid）: \n"
        "../関係図/Task.md\n"
        "\n"
        "```\n"
        "Task\n"
        "└ What → Account\n"
        "```\n"
        "\n"
        "## 子リレーション（サブクエリの材料）\n"
        "\n"
        "保存データに無い。実行.bat で取り直すと出る\n"
        "\n"
        "## SOQL を組むときの注意\n"
        "\n"
        "- 取れなかった列がある\n"
        "- 複数候補の列がある"
    )

    assert build_markdown(record) == expected


# ── 似たSOQL ───────────────────────────────────────────────────────


def test_markdown_has_similar_soql_section() -> None:
    """生成した SOQL が似ている相手が居るとき、 「似たSOQL」 節に 1 相手 1 行で出る。"""
    record = _make_record(
        soql_draft=SoqlDraft(
            soql="SELECT Subject FROM Task",
            status="作成済み",
            missing=(),
            checks=(),
            select_items=("Subject",),
            where_items=(),
            order_by="",
        ),
    )
    similar_soql_lookup = {
        "1001": [
            SimilarSoql(key="1002", category="SOQL同一", diff=""),
            SimilarSoql(key="1003", category="WHERE以降が同じ", diff="SELECT 追加: Amount"),
        ]
    }

    rendered = build_markdown(record, similar_soql_lookup=similar_soql_lookup)

    assert "## 似たSOQL" in rendered
    assert "- 1002: SOQL同一" in rendered
    assert "- 1003: WHERE以降が同じ（SELECT 追加: Amount）" in rendered


def test_markdown_similar_soql_section_is_none_without_lookup() -> None:
    """相手が居ないときは 「似たSOQL」 節が 「なし」 1 行。"""
    record = _make_record()

    rendered = build_markdown(record)

    assert "## 似たSOQL\nなし" in rendered


# ── SUMMARY のグループ・集計 ────────────────────────────────────────────


def test_ai_markdown_summary_section_and_notes() -> None:
    """SUMMARY の record はグループ・集計の節が出て、 「集計レポート」 の
    注意も出る。
    """
    report_metadata_override = {
        "reportFormat": "SUMMARY",
        "detailColumns": ["SUBJECT"],
        "groupingsDown": [
            {"name": "Opp.CloseDate", "sortOrder": "Asc", "dateGranularity": "FISCAL_QUARTER"},
        ],
        "groupingsAcross": [
            {"name": "Opp.StageName", "sortOrder": "Desc", "dateGranularity": "DAY"},
        ],
        "aggregates": ["s!Opp.Amount", "n!Opp.RollUp"],
        "sortBy": [],
    }
    record = _make_record(report_metadata_override=report_metadata_override)
    markdown = build_markdown(record)

    # グループ・集計: 「SOQL では ...」 のヒントと全グループ・集計が出る
    assert "## グループ・集計" in markdown
    assert "SOQL では GROUP BY と集計関数で表すか、 明細を取って集計する" in markdown
    assert "- 行グループ: Opp.CloseDate (Asc, FISCAL_QUARTER)" in markdown
    assert "- 列グループ: Opp.StageName (Desc, DAY)" in markdown
    assert "- 集計: s!Opp.Amount" in markdown
    assert "- 集計: n!Opp.RollUp" in markdown
    # 注意に 「集計レポート」 が出る
    assert "## SOQL を組むときの注意" in markdown
    assert "- 集計レポート" in markdown


# ── 主オブジェクト不明 ─────────────────────────────────────────────────────


def test_ai_markdown_main_object_unknown() -> None:
    """主オブジェクトが特定できなかった record は、 ヘッダーに理由が付き、
    列の対応は表示名だけで 使う SOQL 列名は ``(不明)`` になる。
    """
    column_map = [
        {
            "列キー": "SUBJECT",
            "表示名": "件名",
            "所属オブジェクト": "",
            "対応フィールドAPI名": "(不明)",
            "型": "",
            "参照先オブジェクト": "",
            "リレーション名": "",
            "備考": "レポートタイプ 'Foo' からは主オブジェクト候補を抽出できなかったため自動判定できません。手動で確認してください",
        },
    ]
    record = _make_record(
        key="2002",
        main_object=None,
        column_map=column_map,
        warnings=(
            "レポートタイプ 'Foo' からは主オブジェクト候補を抽出できなかったため"
            "自動判定できません。手動で確認してください",
        ),
    )
    markdown = build_markdown(record)

    # 主オブジェクト行に理由がそのまま入る
    assert "- 主オブジェクト: 不明（" in markdown
    assert "レポートタイプ 'Foo'" in markdown
    # 列の対応: 表示名だけ、 使う SOQL 列名は "(不明)" 、 候補は空、 どこから は 取れず
    assert "| 件名 | (不明) | 取れず |  |" in markdown
    # 警告セクションにも同じ理由が出る
    assert "## 警告" in markdown
    assert "- レポートタイプ 'Foo'" in markdown


# ── セルエスケープ ────────────────────────────────────────────────────────


def test_ai_markdown_escape_pipe_and_newline_in_cell() -> None:
    """絞り込みの値や候補の ``|`` / 改行が Markdown 表でエスケープされる。"""
    column_map = [
        {
            "列キー": "WEIRD",
            "表示名": "PIPE|NEWLINE",
            "所属オブジェクト": "",
            "対応フィールドAPI名": "Subject",
            "型": "string",
            "参照先オブジェクト": "",
            "リレーション名": "",
            "備考": "複数候補あり: Foo|Bar\n2行目",
        },
    ]
    record = _make_record(
        column_map=column_map,
        report_metadata_override={"detailColumns": ["WEIRD"]},
    )
    markdown = build_markdown(record)

    # 列の対応: 表示名の ``|`` が ``\|`` 、 候補欄の改行が空白、 候補の ``|`` も
    # エスケープ
    assert "| PIPE\\|NEWLINE | Subject | 自動 | Foo\\|Bar 2行目 |" in markdown


# ── run_tables で書き出されること ─────────────────────────────────────────


def test_run_tables_writes_markdown_per_record(tmp_path: Path) -> None:
    """``run_tables`` を呼ぶと ``OUTPUT_DIR/SOQL資料/{管理番号}.md`` が作られ、
    失敗したレポートの古いファイルは消えない。
    """
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    md_dir = output_dir / MARKDOWN_SUBDIR
    md_dir.mkdir()
    # 「取れなかった」 想定の古いファイルを 1 件置いておく
    stale = md_dir / "9999.md"
    stale.write_text("古いファイル", encoding="utf-8")

    record = _make_record(key="1001")
    outcome = run_tables(output_dir, [record])

    # SOQL資料/{key}.md が作られている
    new_file = md_dir / "1001.md"
    assert new_file.exists()
    content = new_file.read_text(encoding="utf-8")
    # 先頭に AI への案内ブロックが必ず入る
    assert content.startswith(
        "> このファイルを丸ごと AI に貼ってください。"
        "SOQL の下書きを、下の資料と照らして直すよう頼みます。"
    )
    assert "# 1001 テスト1001" in content
    # 失敗ファイル はそのまま残っている
    assert stale.exists()
    # 既存の xlsx / csv には影響しない
    assert (output_dir / "SOQL調査.xlsx").exists()
    assert (output_dir / "列名の対応.csv").exists()
    # 書き出したファイル群に含まれている
    assert new_file in outcome.wrote


# ── SOQL の下書き ──────────────────────────────────────────────────────


def test_markdown_has_soql_section_for_created_draft() -> None:
    """``作成済み`` のドラフトなら ``## SOQL の下書き`` 節が出る。"""
    record = _make_record(
        soql_draft=SoqlDraft(
            soql="SELECT Subject, What.Name\nFROM Task\nWHERE Subject LIKE '%A%'",
            status="作成済み",
            missing=(),
            checks=(),
        ),
    )
    rendered = build_markdown(record)
    assert "## SOQL の下書き" in rendered
    assert "状態: 作成済み" in rendered
    # SOQL 本文は ```sql コードブロックで囲む
    assert "```sql" in rendered
    assert "SELECT Subject, What.Name" in rendered
    assert "FROM Task" in rendered
    # AI への補い方の指示も添える
    assert "この下書きを、下の資料と照らして直してください" in rendered


def test_markdown_soql_section_keeps_incomplete_marker_for_partial() -> None:
    """``一部作れず`` のドラフトは SOQL の 1 行目に未完成の印をそのまま残す。

    AI に渡す Markdown で 「この SOQL はそのままでは実行できない」 と一目で
    分かるように、 印はそのまま載せる。 ``作れなかった部分`` のリストも出る。
    """
    record = _make_record(
        soql_draft=SoqlDraft(
            soql=(
                "【未完成】作れなかった部分があります。このままでは実行できません。\n"
                "SELECT Subject\nFROM Task"
            ),
            status="一部作れず",
            missing=("列: 商談名 ( Opp.Name ) — 対応フィールドなし",),
            checks=(),
        ),
    )
    rendered = build_markdown(record)
    assert "状態: 一部作れず" in rendered
    assert "【未完成】" in rendered
    assert "作れなかった部分:" in rendered
    assert "列: 商談名" in rendered


def test_markdown_soql_section_omits_body_for_none_status() -> None:
    """``作れず`` は本文が無いので 「SOQL: なし」 と書く。"""
    record = _make_record(
        soql_draft=SoqlDraft(
            soql="",
            status="作れず",
            missing=("主オブジェクトが不明のため作れず",),
            checks=(),
        ),
    )
    rendered = build_markdown(record)
    assert "状態: 作れず" in rendered
    assert "SOQL: なし" in rendered


def test_markdown_soql_section_omitted_without_draft() -> None:
    """``soql_draft`` が無い record はセクションを出さない。"""
    record = _make_record()  # soql_draft=None
    rendered = build_markdown(record)
    assert "## SOQL の下書き" not in rendered


def test_markdown_soql_section_includes_checks_when_needs_check() -> None:
    """``要確認`` には SOQL 本文は出るが 「要確認:」 のリストも添える。"""
    record = _make_record(
        soql_draft=SoqlDraft(
            soql="SELECT Subject FROM Task",
            status="要確認",
            missing=(),
            checks=("スコープ=MyTerritory: 実行ユーザーで結果が変わる。",),
        ),
    )
    rendered = build_markdown(record)
    assert "状態: 要確認" in rendered
    assert "要確認:" in rendered
    assert "スコープ=MyTerritory" in rendered


# ── 壊れた実装でテストが落ちることを確かめる ────────────────────────────────


def test_ai_markdown_broken_implementation_fails_full_text() -> None:
    """出力する列の節を実装から抜くと、 全文比較のテストが落ちることを 1 度
    確かめる （壊れたまま戻さない） 。 ``build_markdown`` の戻り値から
    ``## 出力する列`` の節を擬似的に抜くヘルパーを使う
    """
    from src.ai_markdown import build_markdown as original_build_markdown

    record = _make_record()
    rendered = original_build_markdown(record)
    # 壊れた実装を模す: 出力する列の節を削る
    head, _, tail = rendered.partition("## 出力する列")
    _, _, rest = tail.partition("## 絞り込み")
    broken = head + "## 絞り込み" + rest

    # 壊れた実装は 「出力する列」 が消えている
    assert "## 出力する列" not in broken
    # 期待値 （=本物の実装の戻り値） とは一致しない
    assert broken != rendered


# ── 取り方 ────────────────────────────────────────────────────────────


def test_markdown_has_fetch_method_section_for_bulk() -> None:
    """``use_bulk`` が True のときは 「取り方」 に Bulk の注意が出る。"""
    record = _make_record(
        soql_draft=SoqlDraft(
            soql="SELECT Subject FROM Task",
            status="作成済み",
            missing=(),
            checks=(),
        ),
    )
    rendered = build_markdown(record)
    assert "## 取り方" in rendered
    assert "- Bulk API 2.0 で取る（USE_BULK = True）" in rendered
    assert (
        "- SELECT の中の子サブクエリ・集計関数・GROUP BY・OFFSET・TYPEOF を使うように直したら "
        "USE_BULK を False にする"
    ) in rendered


def test_markdown_has_fetch_method_section_for_rest() -> None:
    """``use_bulk`` が False のときは 理由と REST の注意が出る。"""
    record = _make_record(
        soql_draft=SoqlDraft(
            soql="SELECT Subject FROM Task",
            status="要確認",
            missing=(),
            checks=(),
            use_bulk=False,
            bulk_blockers=("サブクエリを使うため",),
        ),
    )
    rendered = build_markdown(record)
    assert "## 取り方" in rendered
    assert "- REST の query() で取る（USE_BULK = False）: サブクエリを使うため" in rendered
    assert "- REST の注意: 値が文字列にならず数値・真偽値のまま返る" in rendered
    assert "- REST の注意: 0 件だと列が空" in rendered
    assert "- REST の注意: 全行 null の参照項目は列が出ない" in rendered


def test_markdown_fetch_method_section_omitted_without_draft() -> None:
    """下書きが無い record は 「取り方」 を出さない。"""
    record = _make_record()
    assert "## 取り方" not in build_markdown(record)


# ── 子リレーション（ サブクエリの材料 ） ─────────────────────────────────


def test_markdown_has_child_relationship_section() -> None:
    """主オブジェクトの子リレーションが 表 で 出る。"""
    record = _make_record()
    objects = dict(record.objects)
    task = dict(objects["Task"])
    task["childRelationships"] = [
        {
            "childSObject": "TaskComment",
            "field": "ParentId",
            "relationshipName": "Comments",
        }
    ]
    objects["Task"] = task
    record = dataclasses.replace(record, objects=objects)

    rendered = build_markdown(record)
    assert "## 子リレーション（サブクエリの材料）" in rendered
    assert "| リレーション名 | 子オブジェクト | 参照項目 |" in rendered
    assert "| Comments | TaskComment | ParentId |" in rendered
    assert "例: (SELECT Id FROM Comments)" in rendered
    assert "SELECT の中に書いたら USE_BULK を False にする" in rendered


def test_markdown_child_relationship_section_says_missing_when_no_data() -> None:
    """子リレーションが無いときは 「保存データに無い」 の 1 行。"""
    rendered = build_markdown(_make_record())
    assert "## 子リレーション（サブクエリの材料）" in rendered
    assert "保存データに無い。実行.bat で取り直すと出る" in rendered


def test_markdown_child_relationship_section_limits_rows() -> None:
    """100 行を超える分は 「ほか N 件」 にまとめる。"""
    record = _make_record()
    objects = dict(record.objects)
    task = dict(objects["Task"])
    task["childRelationships"] = [
        {
            "childSObject": f"Child{index}",
            "field": "ParentId",
            "relationshipName": f"Rel{index}",
        }
        for index in range(120)
    ]
    objects["Task"] = task
    record = dataclasses.replace(record, objects=objects)

    rendered = build_markdown(record)
    assert "| Rel99 | Child99 | ParentId |" in rendered
    assert "| Rel100 |" not in rendered
    assert "ほか 20 件" in rendered


# ── クロスフィルタの表示 ───────────────────────────────────────────────


def test_markdown_cross_filter_shows_includes_and_json() -> None:
    """クロスフィルタは includesObject と 要素全体 の JSON で 出る。"""
    cross_filter = {
        "criteria": [{"column": "CommentBody", "operator": "equals", "value": "要対応"}],
        "includesObject": True,
        "primaryEntityField": "WhatId",
        "relatedEntity": "TaskComment",
        "relatedEntityJoinField": "ParentId",
    }
    record = _make_record(report_metadata_override={"crossFilters": [cross_filter]})
    rendered = build_markdown(record)

    assert "- クロスフィルタ: 含む " in rendered
    assert '"relatedEntity": "TaskComment"' in rendered
    assert '"includesObject": true' in rendered


def test_markdown_cross_filter_shows_not_includes_and_unknown() -> None:
    """``includesObject`` が False / 不明 のときは それぞれ の 表示 になる。"""
    excluded = {
        "criteria": [],
        "includesObject": False,
        "primaryEntityField": "WhatId",
        "relatedEntity": "TaskComment",
        "relatedEntityJoinField": "ParentId",
    }
    unknown = {"primaryEntityField": "WhatId"}
    record = _make_record(report_metadata_override={"crossFilters": [excluded, unknown]})
    rendered = build_markdown(record)

    assert "- クロスフィルタ: 含まない " in rendered
    assert "- クロスフィルタ: 不明 " in rendered


# ── クロスフィルタの繋がりの表 ───────────────────────────────────────────


def _cross_filter_links() -> tuple[CrossFilterLink, ...]:
    """表のテスト用の判定を 2 件作る。"""
    return (
        CrossFilterLink(
            index=1,
            status=CROSS_FILTER_LINK_DIRECT,
            primary_field="Id",
            child_object="TaskComment",
            join_field="ParentId",
            relationship_name="Comments",
            converted=True,
            reason="",
        ),
        CrossFilterLink(
            index=2,
            status=CROSS_FILTER_LINK_NOT_DIRECT,
            primary_field="WhatId",
            child_object="TaskComment",
            join_field="ParentId",
            relationship_name="",
            converted=False,
            reason="主側の項目 WhatId の型が id でない（型: reference）",
        ),
    )


def test_markdown_has_cross_filter_link_section() -> None:
    """判定が 1 件でもあれば 「クロスフィルタの繋がり」 の表が 出る。"""
    record = _make_record(
        soql_draft=SoqlDraft(
            soql="SELECT Subject FROM Task",
            status="要確認",
            missing=(),
            checks=(),
            cross_filter_links=_cross_filter_links(),
        ),
    )
    rendered = build_markdown(record)

    assert "## クロスフィルタの繋がり" in rendered
    assert "| 1 | 直接 | Id | TaskComment | ParentId | Comments | 入れた |  |" in rendered
    assert (
        "| 2 | 直接でない | WhatId | TaskComment | ParentId |  | 入れていない | "
        "主側の項目 WhatId の型が id でない（型: reference） |" in rendered
    )
    # 「取り方」 の直後
    assert rendered.index("## 取り方") < rendered.index("## クロスフィルタの繋がり")
    assert rendered.index("## クロスフィルタの繋がり") < rendered.index(
        "## 子リレーション（サブクエリの材料）"
    )


def test_markdown_has_no_cross_filter_link_section_without_links() -> None:
    """判定が 0 件ならセクションは出さない。"""
    record = _make_record(
        soql_draft=SoqlDraft(
            soql="SELECT Subject FROM Task",
            status="作成済み",
            missing=(),
            checks=(),
        ),
    )
    rendered = build_markdown(record)

    assert "## クロスフィルタの繋がり" not in rendered


def test_markdown_cross_filter_link_cell_is_escaped() -> None:
    """表のセルは ``|`` と改行をエスケープする。"""
    link = CrossFilterLink(
        index=1,
        status=CROSS_FILTER_LINK_UNVERIFIED,
        primary_field="Id",
        child_object="TaskComment",
        join_field="ParentId",
        relationship_name="Comments",
        converted=False,
        reason="理由|に\n改行",
    )
    record = _make_record(
        soql_draft=SoqlDraft(
            soql="SELECT Subject FROM Task",
            status="一部作れず",
            missing=(),
            checks=(),
            cross_filter_links=(link,),
        ),
    )
    rendered = build_markdown(record)

    assert "理由\\|に 改行" in rendered


# ── 注意 ──────────────────────────────────────────────────────────────


def test_markdown_notes_for_rest_with_too_many_rows() -> None:
    """2000件超でも REST のときは 注意文が変わる。"""
    record = _make_record(
        row_check=RowCheck(status="超えている", rows=None, reason=None),
        soql_draft=SoqlDraft(
            soql="SELECT Subject FROM Task",
            status="要確認",
            missing=(),
            checks=(),
            use_bulk=False,
            bulk_blockers=("サブクエリを使うため",),
        ),
    )
    rendered = build_markdown(record)
    assert "- 2000件超だが REST で取る（ページ送りで全件取れる。往復は増える）" in rendered
    assert "- 2000件超なので Bulk で取る" not in rendered


def test_markdown_notes_for_cross_filter_and_aggregate() -> None:
    """クロスフィルタと 集計レポート の 注意文。

    副問い合わせにできていないときは、 理由が 「クロスフィルタの繋がり」 に
    あることを示す文になる。
    """
    record = _make_record(
        report_metadata_override={
            "crossFilters": [
                {
                    "criteria": [],
                    "includesObject": True,
                    "primaryEntityField": "WhatId",
                    "relatedEntity": "TaskComment",
                    "relatedEntityJoinField": "ParentId",
                }
            ],
            "reportFormat": "SUMMARY",
            "groupingsDown": [],
            "groupingsAcross": [],
            "aggregates": [],
        },
    )
    rendered = build_markdown(record)
    assert (
        "- クロスフィルタは副問い合わせにできていない（理由は「クロスフィルタの繋がり」）"
        in rendered
    )
    assert "- 集計レポート" in rendered
    assert "- GROUP BY と集計関数を使うなら USE_BULK を False にする" in rendered


def test_markdown_notes_say_subquery_when_one_link_is_converted() -> None:
    """1 件でも副問い合わせに変換できたときは、 「副問い合わせにした」 の文になる。"""
    link = CrossFilterLink(
        index=1,
        status=CROSS_FILTER_LINK_DIRECT,
        primary_field="Id",
        child_object="TaskComment",
        join_field="ParentId",
        relationship_name="Comments",
        converted=True,
        reason="",
    )
    record = _make_record(
        report_metadata_override={
            "crossFilters": [
                {
                    "criteria": [],
                    "includesObject": True,
                    "primaryEntityField": "Id",
                    "relatedEntity": "TaskComment",
                    "relatedEntityJoinField": "ParentId",
                }
            ],
        },
        soql_draft=SoqlDraft(
            soql="SELECT Subject FROM Task",
            status="要確認",
            missing=(),
            checks=(),
            use_bulk=False,
            bulk_blockers=("副問い合わせのため",),
            cross_filter_links=(link,),
        ),
    )
    rendered = build_markdown(record)
    assert "- クロスフィルタは副問い合わせにした（REST で取る）" in rendered
    assert "副問い合わせにできていない" not in rendered
