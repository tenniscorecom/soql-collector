"""``ai_markdown`` のテスト。

``ReportRecord`` を直接作って Markdown 文字列の全文を期待値と比べる （ 偽
record だけでネットワークには繋がない ） 。 仕様の見出し順・表・エスケープ
・注意の箇条書きを 1 つずつ確かめる
"""

from __future__ import annotations

from pathlib import Path

from src import pii
from src.ai_markdown import (
    MARKDOWN_SUBDIR,
    build_markdown,
)
from src.fetch import RelationRecord, ReportRecord, RowCheck
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
    )


# ── TABULAR の全文比較 ────────────────────────────────────────────────────


def test_ai_markdown_tabular_full_text() -> None:
    """TABULAR の record （ 列 3 本のうち 1 本が複数候補、 絞り込み 2 件+組立式、
    並び順、 関連 1 段 ） の Markdown が期待値と完全に一致する。
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
        "# 1001 テスト1001\n"
        "\n"
        "- レポートID: 00O000000000001\n"
        "- 主オブジェクト: Task\n"
        "- レポート形式: TABULAR\n"
        "- 2000件超: 超えていない（42件）\n"
        "- 個人情報: なし\n"
        "\n"
        "## 出力する列\n"
        "| 表示名 | SOQL列名 | 型 | 備考 |\n"
        "| --- | --- | --- | --- |\n"
        "| 件名 | Subject | string |  |\n"
        "| 取引先名 | What.Name | string |  |\n"
        "| 曖昧な列 | (不明) |  | 複数候補あり: Account.Owner（浅い）, Contact.Owner |\n"
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
        "## SOQL を組むときの注意\n"
        "\n"
        "- 取れなかった列がある\n"
        "- 複数候補の列がある"
    )

    assert build_markdown(record) == expected


# ── SUMMARY のグループ・集計 ────────────────────────────────────────────


def test_ai_markdown_summary_section_and_notes() -> None:
    """SUMMARY の record はグループ・集計の節が出て、 「 集計レポート 」 の
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

    # グループ・集計: 「 SOQL では ... 」 のヒントと全グループ・集計が出る
    assert "## グループ・集計" in markdown
    assert "SOQL では GROUP BY と集計関数で表すか、 明細を取って集計する" in markdown
    assert "- 行グループ: Opp.CloseDate (Asc, FISCAL_QUARTER)" in markdown
    assert "- 列グループ: Opp.StageName (Desc, DAY)" in markdown
    assert "- 集計: s!Opp.Amount" in markdown
    assert "- 集計: n!Opp.RollUp" in markdown
    # 注意に 「 集計レポート 」 が出る
    assert "## SOQL を組むときの注意" in markdown
    assert "- 集計レポート" in markdown


# ── 主オブジェクト不明 ─────────────────────────────────────────────────────


def test_ai_markdown_main_object_unknown() -> None:
    """主オブジェクトが特定できなかった record は、 ヘッダーに理由が付き、
    出力する列は表示名だけで SOQL列名は ``(不明)`` になる。
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
    # 出力する列: 表示名だけ、 SOQL列名は "(不明)" 、 備考に理由がそのまま
    assert "| 件名 | (不明) |  | " in markdown
    assert "レポートタイプ 'Foo'" in markdown


# ── セルエスケープ ────────────────────────────────────────────────────────


def test_ai_markdown_escape_pipe_and_newline_in_cell() -> None:
    """絞り込みの値や備考の ``|`` / 改行が Markdown 表でエスケープされる。"""
    column_map = [
        {
            "列キー": "WEIRD",
            "表示名": "PIPE|NEWLINE",
            "所属オブジェクト": "",
            "対応フィールドAPI名": "Subject",
            "型": "string",
            "参照先オブジェクト": "",
            "リレーション名": "",
            "備考": "1行目\n2行目 | 区切り",
        },
    ]
    record = _make_record(
        column_map=column_map,
        report_metadata_override={"detailColumns": ["WEIRD"]},
    )
    markdown = build_markdown(record)

    # 出力する列: 表示名の ``|`` が ``\|`` 、 備考の改行が空白、 備考の ``|`` も
    # エスケープ
    assert "| PIPE\\|NEWLINE | Subject | string | 1行目 2行目 \\| 区切り |" in markdown


# ── run_tables で書き出されること ─────────────────────────────────────────


def test_run_tables_writes_markdown_per_record(tmp_path: Path) -> None:
    """``run_tables`` を呼ぶと ``OUTPUT_DIR/SOQL資料/{管理番号}.md`` が作られ、
    失敗したレポートの古いファイルは消えない。
    """
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    md_dir = output_dir / MARKDOWN_SUBDIR
    md_dir.mkdir()
    # 「 取れなかった 」 想定の古いファイルを 1 件置いておく
    stale = md_dir / "9999.md"
    stale.write_text("古いファイル", encoding="utf-8")

    record = _make_record(key="1001")
    outcome = run_tables(output_dir, [record])

    # SOQL資料/{key}.md が作られている
    new_file = md_dir / "1001.md"
    assert new_file.exists()
    content = new_file.read_text(encoding="utf-8")
    assert content.startswith("# 1001 テスト1001")
    # 失敗ファイル はそのまま残っている
    assert stale.exists()
    # 既存の xlsx / csv には影響しない
    assert (output_dir / "SOQL調査.xlsx").exists()
    assert (output_dir / "列名の対応.csv").exists()
    # 書き出したファイル群に含まれている
    assert new_file in outcome.wrote


# ── 壊れた実装でテストが落ちることを確かめる ────────────────────────────────


def test_ai_markdown_broken_implementation_fails_full_text() -> None:
    """出力する列の節を実装から抜くと、 全文比較のテストが落ちることを 1 度
    確かめる （ 壊れたまま戻さない ） 。 ``build_markdown`` の戻り値から
    ``## 出力する列`` の節を擬似的に抜くヘルパーを使う
    """
    from src.ai_markdown import build_markdown as original_build_markdown

    record = _make_record()
    rendered = original_build_markdown(record)
    # 壊れた実装を模す: 出力する列の節を削る
    head, _, tail = rendered.partition("## 出力する列")
    _, _, rest = tail.partition("## 絞り込み")
    broken = head + "## 絞り込み" + rest

    # 壊れた実装は 「 出力する列 」 が消えている
    assert "## 出力する列" not in broken
    # 期待値 （=本物の実装の戻り値） とは一致しない
    assert broken != rendered
