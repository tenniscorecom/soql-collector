"""``src.mapping`` のテスト。



comken の ``test_salesforce.py::TestDescribeFields`` から、 soql-collector に

移したロジックに当たる部分を純粋関数のテストとして移植している。

HTTP や ``ReportAPI`` は使わないので、 モックも context manager も要らない。

"""

from __future__ import annotations

import pytest

from src.mapping import (
    build_column_map,
    candidate_failure_reason,
    collect_describable_columns,
    report_type_candidates,
)

# ── report_type_candidates ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("report_type", "expected"),
    [
        ("CustomEntity$Project__c", ["Project__c"]),
        (
            "CustomEntityCustomEntity$Project__c$Task__c",
            ["Project__c", "Task__c"],
        ),
        ("CustomEntity$Project__c@Project__c.Account__c", ["Project__c"]),
        ("Account@Contact", ["Account"]),
        ("Opportunity", ["Opportunity"]),
        ("CustomEntity$", []),
    ],
)
def test_report_type_candidates(report_type: str, expected: list[str]) -> None:
    """``reportType.type`` から ``$`` / ``@`` を取り除いて主オブジェクト

    候補を取り出す。 ``CustomEntity`` の繰り返しだけのトークンは除外し、

    重複は出現順を保ったまま除く。

    """

    assert report_type_candidates(report_type) == expected


# ── build_column_map ───────────────────────────────────────────────────


def _make_metadata(
    report_type: str = "Opportunity",
    *,
    detail_columns: list[str] | None = None,
    filters: list[dict] | None = None,
    groupings_down: list[dict] | None = None,
    groupings_across: list[dict] | None = None,
    aggregates: list[str] | None = None,
    detail_column_labels: dict[str, str] | None = None,
) -> dict:
    """テスト用の ``client.report.describe()`` 戻り値相当を作る。



    ``detail_column_labels`` で ``detailColumns`` 各列の表示名を上書きできる。

    指定が無ければ列キーをそのまま表示名にする。

    """

    detail_columns = detail_columns or ["NAME", "AMOUNT", "STAGE_NAME", "UNKNOWN_LABEL"]

    report_metadata: dict = {
        "reportType": {"type": report_type},
        "reportFormat": "TABULAR",
        "detailColumns": detail_columns,
    }

    if filters is not None:
        report_metadata["reportFilters"] = filters

    if groupings_down is not None:
        report_metadata["groupingsDown"] = groupings_down

    if groupings_across is not None:
        report_metadata["groupingsAcross"] = groupings_across

    if aggregates is not None:
        report_metadata["aggregates"] = aggregates

    labels = detail_column_labels or {}

    detail_info = {
        column_key: {"label": labels.get(column_key, column_key)} for column_key in detail_columns
    }

    extended: dict = {"detailColumnInfo": detail_info}

    return {
        "reportMetadata": report_metadata,
        "reportExtendedMetadata": extended,
    }


def _describe_with_fields(fields: list[dict]) -> dict:
    """テスト用の Object Describe 相当を作る。"""

    return {"name": "Opportunity", "fields": fields}


def test_build_column_map_fills_api_name_and_type_when_label_matches() -> None:
    """表示名が一致する列は、 実フィールドの API 名・型を埋める。"""

    metadata = _make_metadata(
        detail_column_labels={"NAME": "商談名", "AMOUNT": "金額"},
    )

    describe = _describe_with_fields(
        [
            {"name": "Name", "label": "商談名", "type": "Text"},
            {"name": "Amount", "label": "金額", "type": "Currency"},
        ]
    )

    rows = build_column_map(metadata, describe, None)

    # 一致した行は API 名・型が入り、 備考は空

    name_row = next(row for row in rows if row["列キー"] == "NAME")

    assert name_row["対応フィールドAPI名"] == "Name"

    assert name_row["型"] == "Text"

    assert name_row["備考"] == ""

    amount_row = next(row for row in rows if row["列キー"] == "AMOUNT")

    assert amount_row["対応フィールドAPI名"] == "Amount"

    assert amount_row["型"] == "Currency"

    assert amount_row["備考"] == ""


def test_build_column_map_marks_unmatched_label_as_unknown() -> None:
    """一致しない表示名は API 名を ``"(不明)"`` にして対応フィールドなしと注記する。"""

    metadata = _make_metadata(
        detail_columns=["NAME", "AMOUNT"],
        detail_column_labels={"NAME": "商談名", "AMOUNT": "金額"},
    )

    describe = _describe_with_fields([{"name": "Name", "label": "商談名", "type": "Text"}])

    rows = build_column_map(metadata, describe, None)

    # AMOUNT は Object Describe に存在しない

    amount_row = next(row for row in rows if row["列キー"] == "AMOUNT")

    assert amount_row["対応フィールドAPI名"] == "(不明)"

    assert amount_row["型"] == ""

    assert amount_row["備考"] == "対応フィールドなし"


def test_build_column_map_marks_multiple_candidates_without_picking_one() -> None:
    """同じ表示名のフィールドが複数ある列は、 誤った候補を押し付けずに

    「複数候補あり」と注記する。

    """

    metadata = _make_metadata(
        detail_columns=["STAGE_NAME"],
        detail_column_labels={"STAGE_NAME": "フェーズ"},
    )

    describe = _describe_with_fields(
        [
            {"name": "StageName", "label": "フェーズ", "type": "Picklist"},
            {"name": "CustomStage__c", "label": "フェーズ", "type": "Text"},
        ]
    )

    rows = build_column_map(metadata, describe, None)

    stage_row = next(row for row in rows if row["列キー"] == "STAGE_NAME")

    assert stage_row["対応フィールドAPI名"] == "(不明)"

    assert stage_row["型"] == ""

    # どちらを採るか決めかねるため、 両方の候補を鎖+項目で見せる

    assert "StageName" in stage_row["備考"]

    assert "CustomStage__c" in stage_row["備考"]

    assert stage_row["備考"].startswith("複数候補あり: ")


def test_build_column_map_returns_unknown_when_main_describe_is_none() -> None:
    """主オブジェクトの describe が無い（特定失敗など）ときは全列 ``(不明)``

    ＋理由の備考にする。

    """

    metadata = _make_metadata()

    reason = "主オブジェクトを特定できないため自動判定できません。手動で確認してください"

    rows = build_column_map(metadata, None, reason)

    assert len(rows) == 4

    for row in rows:
        assert row["対応フィールドAPI名"] == "(不明)"

        assert row["型"] == ""

        assert row["備考"] == reason


def test_build_column_map_reason_takes_precedence_over_describe() -> None:
    """describe があっても reason を渡したら reason を優先（呼び出し側の契約）。"""

    metadata = _make_metadata(
        detail_columns=["NAME"],
        detail_column_labels={"NAME": "商談名"},
    )

    describe = _describe_with_fields([{"name": "Name", "label": "商談名", "type": "Text"}])

    rows = build_column_map(metadata, describe, "上書き理由")

    assert rows[0]["備考"] == "上書き理由"

    # reason が優先されるとき、 API 名は埋めない（ 縮退ルートのまま ）

    assert rows[0]["対応フィールドAPI名"] == "(不明)"

    assert rows[0]["型"] == ""


def test_build_column_map_includes_filter_only_columns() -> None:
    """SELECT に無くフィルタだけにある列も対応表へ残す。"""

    metadata = _make_metadata(
        detail_columns=["NAME"],
        detail_column_labels={"NAME": "商談名"},
        filters=[{"column": "OWNER", "operator": "equals", "value": "005xxx"}],
    )

    metadata["reportExtendedMetadata"]["detailColumnInfo"]["OWNER"] = {"label": "所有者ID"}

    describe = _describe_with_fields(
        [
            {"name": "Name", "label": "商談名", "type": "Text"},
            {"name": "OwnerId", "label": "所有者ID", "type": "reference"},
        ]
    )

    rows = build_column_map(metadata, describe, None)

    owner_row = next(row for row in rows if row["列キー"] == "OWNER")

    assert owner_row["対応フィールドAPI名"] == "OwnerId"

    assert owner_row["型"] == "reference"


def test_build_column_map_includes_grouping_and_aggregate_columns() -> None:
    """``SUMMARY`` / ``MATRIX`` の ``groupingsDown`` / ``groupingsAcross`` /

    ``aggregates`` の列も対応表へ残す。

    """

    metadata = _make_metadata(
        detail_columns=["NAME"],
        detail_column_labels={"NAME": "商談名"},
        groupings_down=[{"name": "STAGE_NAME", "sortOrder": "Asc"}],
        groupings_across=[{"name": "CLOSE_DATE"}],
        aggregates=["s!AMOUNT"],
    )

    metadata["reportExtendedMetadata"]["detailColumnInfo"]["NAME"] = {"label": "商談名"}

    metadata["reportExtendedMetadata"]["groupingColumnInfo"] = {
        "STAGE_NAME": {"label": "フェーズ名"},
        "CLOSE_DATE": {"label": "完了予定日"},
    }

    metadata["reportExtendedMetadata"]["aggregateColumnInfo"] = {
        "AMOUNT": {"label": "金額"},
    }

    describe = _describe_with_fields(
        [
            {"name": "Name", "label": "商談名", "type": "Text"},
            {"name": "StageName", "label": "フェーズ名", "type": "picklist"},
            {"name": "CloseDate", "label": "完了予定日", "type": "date"},
            {"name": "Amount", "label": "金額", "type": "currency"},
        ]
    )

    rows = build_column_map(metadata, describe, None)

    by_key = {row["列キー"]: row for row in rows}

    assert by_key["STAGE_NAME"]["対応フィールドAPI名"] == "StageName"

    assert by_key["STAGE_NAME"]["型"] == "picklist"

    assert by_key["CLOSE_DATE"]["対応フィールドAPI名"] == "CloseDate"

    assert by_key["CLOSE_DATE"]["型"] == "date"

    assert by_key["AMOUNT"]["対応フィールドAPI名"] == "Amount"

    assert by_key["AMOUNT"]["型"] == "currency"


def test_build_column_map_aggregate_key_without_exclamation_is_ignored() -> None:
    """``"!"`` 区切りが無い集計キー（想定外の形式）は列として扱わずスキップする。"""

    metadata = _make_metadata(
        detail_columns=["NAME"],
        aggregates=["RowCount"],
    )

    describe = _describe_with_fields([{"name": "Name", "label": "NAME", "type": "Text"}])

    rows = build_column_map(metadata, describe, None)

    assert "RowCount" not in [row["列キー"] for row in rows]


# ── 理由文言ヘルパー ─────────────────────────────────────────────────────


def test_candidate_failure_reason_with_candidates_lists_original_and_candidates() -> None:
    """候補を試して全滅したときは、 元の値（repr 形式）と分割した候補が

    両方とも理由に入る。 候補分割をしない壊れた実装だと元の値がそのまま

    候補として並ぶだけで ``Foo__c`` / ``Bar__c`` 個別には現れない。

    """

    text = candidate_failure_reason("CustomEntity$Foo__c$Bar__c", ["Foo__c", "Bar__c"])

    assert "'CustomEntity$Foo__c$Bar__c'" in text

    assert "'Foo__c'" in text

    assert "'Bar__c'" in text

    assert "手動で確認" in text


def test_candidate_failure_reason_without_candidates_lists_only_original() -> None:
    """候補が空のときは、 元の値だけが入った理由を返す。"""

    text = candidate_failure_reason("CustomEntity$", [])

    assert "'CustomEntity$'" in text

    assert "候補を抽出できなかった" in text

    assert "手動で確認" in text


# ── collect_describable_columns ─────────────────────────────────────────


def test_collect_describable_columns_includes_all_sources_in_order_without_duplicates() -> None:
    """``detailColumns`` に加え、 ``reportFilters`` / ``groupingsDown`` /

    ``groupingsAcross`` / ``aggregates`` から列を集め、 出現順を保ったまま

    重複を除く。

    """

    metadata = {
        "detailColumns": ["A", "B"],
        "reportFilters": [
            {"column": "F"},
            {"column": "B"},  # B は ``detailColumns`` に既出
        ],
        "groupingsDown": [{"name": "G"}],
        "groupingsAcross": [{"name": "H"}],
        "aggregates": ["s!I", "RowCount"],  # RowCount は ``"!"`` 無しで無視
    }

    result = collect_describable_columns(metadata)

    assert result == ["A", "B", "F", "G", "H", "I"]


# ── 関連オブジェクト経由の解決 ─────────────────────────────────────────


from dataclasses import dataclass as _dc  # noqa: E402


def _fake_relation(
    parent: str, field: str, relationship_name: str, child: str, depth: int
) -> object:
    """``RelationRecord`` 互換のオブジェクトを ``dataclasses`` で作る。"""

    @_dc(frozen=True)
    class _R:
        parent: str

        field: str

        relationship_name: str | None

        child: str

        depth: int

    return _R(parent, field, relationship_name, child, depth)


def test_build_column_map_resolves_via_related_object_single_candidate() -> None:
    """主オブジェクトでラベル一致しない列が、 関連オブジェクトで 1 件だけ

    ラベル一致するなら、 鎖つき SOQL 名で採用 （ 備考: 「関連オブジェクト経由」 ）。



    例: Task の列「Account Name」 → Account.Name （ 鎖 ``What`` ） 。

    """

    metadata = _make_metadata(
        detail_columns=["ACCOUNT.NAME"],
        detail_column_labels={"ACCOUNT.NAME": "Account Name"},
    )

    task_describe = _describe_with_fields([])  # Task に「Account Name」一致なし

    account_describe = {
        "name": "Account",
        "fields": [{"name": "Name", "label": "Account Name", "type": "Text"}],
    }

    relations = [
        _fake_relation("Task", "WhatId", "What", "Account", 1),
    ]

    rows = build_column_map(
        metadata,
        task_describe,
        None,
        related_describes={"Account": account_describe},
        relations=relations,
        main_object="Task",
    )

    row = next(r for r in rows if r["列キー"] == "ACCOUNT.NAME")

    assert row["所属オブジェクト"] == "Account"

    assert row["対応フィールドAPI名"] == "What.Name"

    assert row["型"] == "Text"

    assert row["備考"] == "関連オブジェクト経由"


def test_build_column_map_resolves_via_related_object_with_prefix_filter() -> None:
    """候補が複数あり、 列キー接頭辞で 1 つに絞れるケース。



    Account と User が両方とも ``"項目名"`` というラベルを持つ列で、

    Task→Account （ rel="What" ） / Task→User （ rel="Owner" ） の 2 経路。

    列キー ``OWNER.NAME`` は接頭辞 ``Owner`` が鎖 ``Owner`` の最初の

    リレーション名と一致するため User 経由が採用される。

    """

    metadata = _make_metadata(
        detail_columns=["OWNER.NAME"],
        detail_column_labels={"OWNER.NAME": "項目名"},
    )

    task_describe = _describe_with_fields([])

    account_describe = {
        "name": "Account",
        "fields": [{"name": "Name", "label": "項目名", "type": "Text"}],
    }

    user_describe = {
        "name": "User",
        "fields": [{"name": "Name", "label": "項目名", "type": "Text"}],
    }

    relations = [
        _fake_relation("Task", "WhatId", "What", "Account", 1),
        _fake_relation("Task", "OwnerId", "Owner", "User", 1),
    ]

    rows = build_column_map(
        metadata,
        task_describe,
        None,
        related_describes={"Account": account_describe, "User": user_describe},
        relations=relations,
        main_object="Task",
    )

    row = next(r for r in rows if r["列キー"] == "OWNER.NAME")

    # 接頭辞 ``Owner`` が鎖 ``Owner`` （ Task→User 直 ） の最初のリレーション名と一致

    assert row["対応フィールドAPI名"] == "Owner.Name"

    assert row["所属オブジェクト"] == "User"

    assert row["備考"] == "列キーの接頭辞で絞り込み"


def test_build_column_map_multiple_candidates_unresolved() -> None:
    """候補が複数あり、 接頭辞でも絞れないときは ``"(不明)"`` のまま、

    備考に「 複数候補あり: ... 」 を入れる。



    Account と User が両方とも ``"項目名"`` というラベルを持つ列で、

    列キー接頭辞 ``WHAT`` も ``OWNER`` も含まない列キー （ 例: ``NAME`` ） 。

    """

    metadata = _make_metadata(
        detail_columns=["NAME"],
        detail_column_labels={"NAME": "項目名"},
    )

    task_describe = _describe_with_fields([])

    account_describe = {
        "name": "Account",
        "fields": [{"name": "Name", "label": "項目名", "type": "Text"}],
    }

    user_describe = {
        "name": "User",
        "fields": [{"name": "Name", "label": "項目名", "type": "Text"}],
    }

    relations = [
        _fake_relation("Task", "WhatId", "What", "Account", 1),
        _fake_relation("Task", "OwnerId", "Owner", "User", 1),
    ]

    rows = build_column_map(
        metadata,
        task_describe,
        None,
        related_describes={"Account": account_describe, "User": user_describe},
        relations=relations,
        main_object="Task",
    )

    row = next(r for r in rows if r["列キー"] == "NAME")

    assert row["対応フィールドAPI名"] == "(不明)"

    assert row["備考"].startswith("複数候補あり: ")

    # 候補が両方候補テキストに含まれる （ 最大 5 件 ）

    assert "What.Name" in row["備考"]

    assert "Owner.Name" in row["備考"]


def test_build_column_map_unmatched_label_via_related() -> None:
    """主・関連のどちらにもラベルが一致しない列は 「対応フィールドなし」 。"""

    metadata = _make_metadata(
        detail_columns=["ACCOUNT.INDUSTRY"],
        detail_column_labels={"ACCOUNT.INDUSTRY": "業種"},
    )

    task_describe = _describe_with_fields([])

    account_describe = {
        "name": "Account",
        "fields": [{"name": "Name", "label": "Account Name", "type": "Text"}],
    }

    relations = [
        _fake_relation("Task", "AccountId", "Account", "Account", 1),
    ]

    rows = build_column_map(
        metadata,
        task_describe,
        None,
        related_describes={"Account": account_describe},
        relations=relations,
        main_object="Task",
    )

    row = next(r for r in rows if r["列キー"] == "ACCOUNT.INDUSTRY")

    assert row["対応フィールドAPI名"] == "(不明)"

    assert row["備考"] == "対応フィールドなし"


# ── C: 関連オブジェクト経由の解決が落ちると C のテストが落ちる ──────────


def test_build_column_map_related_resolution_detects_main_only_regression(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``build_column_map`` が関連オブジェクトを見ずに主オブジェクトだけで

    解こうとする壊れた実装に差し替えると、 C のテスト

    （ ``Account Name`` → ``What.Name`` ） が落ち、 本物の実装では通る

    ことを直接確かめる。



    検出方法: ``build_column_map`` を ``main_describe`` だけ参照する形に

    パッチして、 C のテスト条件 （ Task 主で Account.Name を関連経由解決 ）

    で動かす。

    """

    import src.mapping as mapping_module

    real = mapping_module.build_column_map

    def main_only_build_column_map(
        metadata: dict,
        main_describe: dict | None,
        reason: str | None,
        **kwargs: object,
    ) -> list[dict[str, str]]:

        # 関連オブジェクト ／ relations を渡さずに元のロジックを走らせる

        return real(
            metadata,
            main_describe,
            reason,
            related_describes=None,
            relations=None,
            main_object=kwargs.get("main_object"),
        )

    monkeypatch.setattr(mapping_module, "build_column_map", main_only_build_column_map)

    # C のテスト条件: Task 主、 Account.Name を 「 Account Name 」 で解決

    metadata = _make_metadata(
        detail_columns=["ACCOUNT.NAME"],
        detail_column_labels={"ACCOUNT.NAME": "Account Name"},
    )

    task_describe = _describe_with_fields([])

    account_describe = {
        "name": "Account",
        "fields": [{"name": "Name", "label": "Account Name", "type": "Text"}],
    }

    relations = [
        _fake_relation("Task", "WhatId", "What", "Account", 1),
    ]

    rows = mapping_module.build_column_map(
        metadata,
        task_describe,
        None,
        related_describes={"Account": account_describe},
        relations=relations,
        main_object="Task",
    )

    # main_only 実装だと、 Task に 「 Account Name 」 が無いため 「 対応フィールドなし 」 。

    row = next(r for r in rows if r["列キー"] == "ACCOUNT.NAME")

    assert row["対応フィールドAPI名"] == "(不明)"


# ── FK_ 列キー ──────────────────────────────────────────────────────────


def _fk_opportunity_with_contact_ref() -> dict:
    """``Contact__c`` 参照を持つ ``Opportunity`` 風の main_describe。"""

    return _describe_with_fields(
        [
            {"name": "Name", "label": "Name", "type": "Text"},
            {
                "name": "Contact__c",
                "label": "Contact",
                "type": "reference",
                "referenceTo": ["Contact"],
                "relationshipName": "Contact__r",
            },
        ]
    )


def _contact_with_email() -> dict:
    """``Email__c`` を持つ ``Contact`` 風の describe。"""

    return {
        "name": "Contact",
        "fields": [{"name": "Email__c", "label": "Email", "type": "Email"}],
    }


def test_build_column_map_resolves_fk_single_reference() -> None:
    """``FK_Contact.Email__c`` のような列キーは、 主の ``Contact__c`` 参照を

    通って ``Contact__r.Email__c`` として解決する （ 備考: 「FK_ から」 ） 。

    """

    metadata = _make_metadata(
        detail_columns=["FK_Contact.Email__c"],
        detail_column_labels={"FK_Contact.Email__c": "Contact Email"},
    )

    def fetch_object(name: str) -> dict | None:

        return _contact_with_email() if name == "Contact" else None

    rows = build_column_map(
        metadata,
        _fk_opportunity_with_contact_ref(),
        None,
        related_describes={},
        relations=[],
        main_object="Opportunity",
        fetch_object=fetch_object,
    )

    row = next(r for r in rows if r["列キー"] == "FK_Contact.Email__c")

    assert row["対応フィールドAPI名"] == "Contact__r.Email__c"

    assert row["型"] == "Email"

    assert row["備考"] == "FK_ から"


def test_build_column_map_resolves_fk_with_dollar_prefix() -> None:
    """``FK_$Contact.Email__c`` の先頭の ``$`` を外して ``Contact.Email__c``

    として解く。 結果は ``$`` なしと同じ。

    """

    metadata = _make_metadata(
        detail_columns=["FK_$Contact.Email__c"],
        detail_column_labels={"FK_$Contact.Email__c": "Contact Email"},
    )

    def fetch_object(name: str) -> dict | None:

        return _contact_with_email() if name == "Contact" else None

    rows = build_column_map(
        metadata,
        _fk_opportunity_with_contact_ref(),
        None,
        related_describes={},
        relations=[],
        main_object="Opportunity",
        fetch_object=fetch_object,
    )

    row = next(r for r in rows if r["列キー"] == "FK_$Contact.Email__c")

    assert row["対応フィールドAPI名"] == "Contact__r.Email__c"

    assert row["備考"] == "FK_ から"


def test_build_column_map_resolves_fk_multiple_references() -> None:
    """主オブジェクトに同じ対象への参照項目が 2 つあるときは、 ``(不明)``

    のまま備考に「 複数候補あり: ... 」 を入れる。 表示名照合には進まない。

    """

    metadata = _make_metadata(
        detail_columns=["FK_Contact.Email__c"],
        detail_column_labels={"FK_Contact.Email__c": "Contact Email"},
    )

    opportunity_describe = _describe_with_fields(
        [
            {
                "name": "Contact__c",
                "label": "Contact",
                "type": "reference",
                "referenceTo": ["Contact"],
                "relationshipName": "Contact__r",
            },
            {
                "name": "Sub_Contact__c",
                "label": "Sub Contact",
                "type": "reference",
                "referenceTo": ["Contact"],
                "relationshipName": "Sub_Contact__r",
            },
        ]
    )

    def fetch_object(name: str) -> dict | None:

        return _contact_with_email() if name == "Contact" else None

    rows = build_column_map(
        metadata,
        opportunity_describe,
        None,
        related_describes={},
        relations=[],
        main_object="Opportunity",
        fetch_object=fetch_object,
    )

    row = next(r for r in rows if r["列キー"] == "FK_Contact.Email__c")

    assert row["対応フィールドAPI名"] == "(不明)"

    assert row["備考"].startswith("複数候補あり: ")

    # 両方の候補が鎖つきの SOQL セグメントと項目名で出る

    assert "Contact__r.Email__c（Contact__c）" in row["備考"]

    assert "Sub_Contact__r.Email__c（Sub_Contact__c）" in row["備考"]


def test_build_column_map_falls_back_when_fk_has_no_reference() -> None:
    """主オブジェクトに FK_ の対象オブジェクトへの参照が無いときは表示名

    照合にフォールバックし、 照合でも決まらなければ備考に FK_ の理由が出る。

    """

    metadata = _make_metadata(
        detail_columns=["FK_Contact.Email__c"],
        detail_column_labels={"FK_Contact.Email__c": "Contact Email"},
    )

    # Contact__c を持たない Opportunity

    opportunity_describe = _describe_with_fields(
        [{"name": "Name", "label": "Name", "type": "Text"}]
    )

    rows = build_column_map(
        metadata,
        opportunity_describe,
        None,
        related_describes={},
        relations=[],
        main_object="Opportunity",
        fetch_object=None,
    )

    row = next(r for r in rows if r["列キー"] == "FK_Contact.Email__c")

    assert row["対応フィールドAPI名"] == "(不明)"

    assert "FK_ で Contact を参照する項目が Opportunity にありません" in row["備考"]


def test_build_column_map_skips_fk_without_dot() -> None:
    """``FK_NAME`` のような ``.`` を含まない列キーは FK_ 扱いせず、 表示名

    照合に任せる。 既存挙動を変えずに「 対応フィールドあり 」 のまま。

    """

    metadata = _make_metadata(
        detail_columns=["FK_NAME"],
        detail_column_labels={"FK_NAME": "Name"},
    )

    opportunity_describe = _describe_with_fields(
        [{"name": "Name", "label": "Name", "type": "Text"}]
    )

    rows = build_column_map(
        metadata,
        opportunity_describe,
        None,
        related_describes={},
        relations=[],
        main_object="Opportunity",
        fetch_object=None,
    )

    row = next(r for r in rows if r["列キー"] == "FK_NAME")

    assert row["対応フィールドAPI名"] == "Name"

    assert row["備考"] == ""


def test_build_column_map_resolves_fk_multi_hop() -> None:
    """``FK_Contact.Account.Name`` は ``Contact__r.Account.Name`` に解決。

    経路上の Account は ``fetch_object`` が補完する。

    """

    metadata = _make_metadata(
        detail_columns=["FK_Contact.Account.Name"],
        detail_column_labels={"FK_Contact.Account.Name": "Contact Account Name"},
    )

    contact_describe = {
        "name": "Contact",
        "fields": [
            {
                "name": "AccountId",
                "label": "Account ID",
                "type": "reference",
                "referenceTo": ["Account"],
                "relationshipName": "Account",
            },
        ],
    }

    account_describe = {
        "name": "Account",
        "fields": [{"name": "Name", "label": "Name", "type": "Text"}],
    }

    def fetch_object(name: str) -> dict | None:

        if name == "Contact":
            return contact_describe

        if name == "Account":
            return account_describe

        return None

    rows = build_column_map(
        metadata,
        _fk_opportunity_with_contact_ref(),
        None,
        related_describes={},
        relations=[],
        main_object="Opportunity",
        fetch_object=fetch_object,
    )

    row = next(r for r in rows if r["列キー"] == "FK_Contact.Account.Name")

    assert row["対応フィールドAPI名"] == "Contact__r.Account.Name"

    assert row["備考"] == "FK_ から"


def test_build_column_map_fk_falls_back_to_label_when_label_matches() -> None:
    """FK_ の対象への参照が無いが、 表示名が主オブジェクトの別項目と

    一致するときは、 そちらが優先される （ 失敗理由は備考に残らない ） 。

    """

    metadata = _make_metadata(
        detail_columns=["FK_Contact.Email__c"],
        detail_column_labels={"FK_Contact.Email__c": "Email"},
    )

    # Contact__c を持たないが、 Email というラベルの Email__c 項目は持つ

    opportunity_describe = _describe_with_fields(
        [{"name": "Email__c", "label": "Email", "type": "Email"}]
    )

    rows = build_column_map(
        metadata,
        opportunity_describe,
        None,
        related_describes={},
        relations=[],
        main_object="Opportunity",
        fetch_object=None,
    )

    row = next(r for r in rows if r["列キー"] == "FK_Contact.Email__c")

    # 表示名照合で決まり、 FK_ の失敗理由は消える

    assert row["対応フィールドAPI名"] == "Email__c"

    assert "FK_ で" not in row["備考"]


# --- C0: 末端 reference を nameField に置換する仕様 ---


def _custom_name_field_describe() -> dict:
    """Custom_Name__c への参照を 1 つだけ持つ Task__c 風 describe。"""

    return {
        "name": "Task__c",
        "fields": [
            {
                "name": "Custom_Name__c",
                "type": "reference",
                "referenceTo": ["Foo__c"],
                "relationshipName": "Custom_Name__r",
            },
        ],
    }


def _foo_describe_with_name_field() -> dict:
    """nameField が True の Name 項目を持つ Foo__c 風 describe。"""

    return {
        "name": "Foo__c",
        "fields": [
            {"name": "Id", "type": "id", "nameField": False},
            {"name": "Name", "type": "string", "nameField": True, "label": "Name"},
        ],
    }


def test_resolve_reference_last_segment_replaced_with_name_field() -> None:
    """Task__c.Custom_Name__c のような単一参照の末端は、 参照先の

    nameField 項目 (Foo__c.Name) に置き換えて Custom_Name__r.Name

    にする。 id_column には元の ID 列 SOQL 名が入る。

    """

    from src.mapping import _resolve_column_key_to_soql

    def fetch_object(name: str) -> dict | None:

        return _foo_describe_with_name_field() if name == "Foo__c" else None

    soql_name, field_type, failure_note, id_column = _resolve_column_key_to_soql(
        main_object="Task__c",
        column_key="Task__c.Custom_Name__c",
        main_describe=_custom_name_field_describe(),
        fetch_object=fetch_object,
    )

    assert failure_note is None

    assert soql_name == "Custom_Name__r.Name"

    # 名前項目の型 (string) がそのまま型として返る

    assert field_type == "string"

    # 元の ID 列の SOQL 名を保持

    assert id_column == "Custom_Name__c"


def test_resolve_reference_last_segment_uses_casenumber_when_name_field_is_casenumber() -> None:
    """nameField が True の項目が CaseNumber のように Name 以外

    のときは、 その項目で置換する (Case の例)。

    """

    from src.mapping import _resolve_column_key_to_soql

    main_describe = {
        "name": "X__c",
        "fields": [
            {
                "name": "Case__c",
                "type": "reference",
                "referenceTo": ["Case"],
                "relationshipName": "Case__r",
            },
        ],
    }

    case_describe = {
        "name": "Case",
        "fields": [
            {"name": "CaseNumber", "type": "string", "nameField": True, "label": "ケース番号"},
        ],
    }

    def fetch_object(name: str) -> dict | None:

        return case_describe if name == "Case" else None

    soql_name, field_type, failure_note, id_column = _resolve_column_key_to_soql(
        main_object="X__c",
        column_key="X__c.Case__c",
        main_describe=main_describe,
        fetch_object=fetch_object,
    )

    assert failure_note is None

    assert soql_name == "Case__r.CaseNumber"

    assert field_type == "string"

    assert id_column == "Case__c"


def test_resolve_polymorphic_reference_falls_back_to_dot_name() -> None:
    """OwnerId のようなポリモーフィック参照 (referenceTo が 2 つ

    以上) は、 名前項目を決められないので Owner.Name 固定のフォール

    バックになる。

    """

    from src.mapping import _resolve_column_key_to_soql

    main_describe = {
        "name": "Task__c",
        "fields": [
            {
                "name": "OwnerId",
                "type": "reference",
                "referenceTo": ["User", "Group"],
                "relationshipName": "Owner",
            },
        ],
    }

    soql_name, field_type, failure_note, id_column = _resolve_column_key_to_soql(
        main_object="Task__c",
        column_key="Task__c.OwnerId",
        main_describe=main_describe,
        fetch_object=lambda _name: None,
    )

    assert failure_note is None

    assert soql_name == "Owner.Name"

    assert field_type == "string"

    assert id_column == "OwnerId"


def test_resolve_reference_last_segment_when_target_describe_missing() -> None:
    """fetch_object が None を返すときは参照先の describe が無いので

    ('Name', 'string') フォールバックで Custom_Name__r.Name になる。

    """

    from src.mapping import _resolve_column_key_to_soql

    soql_name, field_type, failure_note, id_column = _resolve_column_key_to_soql(
        main_object="Task__c",
        column_key="Task__c.Custom_Name__c",
        main_describe=_custom_name_field_describe(),
        fetch_object=lambda _name: None,
    )

    assert failure_note is None

    assert soql_name == "Custom_Name__r.Name"

    assert field_type == "string"

    assert id_column == "Custom_Name__c"


def test_resolve_reference_last_segment_in_multihop_chain() -> None:
    """多段 Contact.Account.Owner で、 末端 Owner が OwnerId の

    relationshipName でマッチしたときも同じく参照先の名前項目に置き換

    えて Account.Owner.Name になる。 id_column は Account.Owner。

    """

    from src.mapping import _resolve_column_key_to_soql

    contact_describe = {
        "name": "Contact",
        "fields": [
            {
                "name": "AccountId",
                "type": "reference",
                "referenceTo": ["Account"],
                "relationshipName": "Account",
            },
        ],
    }

    account_describe = {
        "name": "Account",
        "fields": [
            {
                "name": "OwnerId",
                "type": "reference",
                "referenceTo": ["User"],
                "relationshipName": "Owner",
            },
        ],
    }

    user_describe = {
        "name": "User",
        "fields": [
            {"name": "Name", "type": "string", "nameField": True, "label": "Name"},
        ],
    }

    def fetch_object(name: str) -> dict | None:

        if name == "Account":
            return account_describe

        if name == "User":
            return user_describe

        return None

    soql_name, field_type, failure_note, id_column = _resolve_column_key_to_soql(
        main_object="Contact",
        column_key="Contact.Account.Owner",
        main_describe=contact_describe,
        fetch_object=fetch_object,
    )

    assert failure_note is None

    assert soql_name == "Account.Owner.Name"

    assert field_type == "string"

    assert id_column == "Account.OwnerId"  # 末端 Owner は OwnerId の relationshipName


def test_resolve_non_reference_last_segment_unchanged() -> None:
    """末端が参照でない項目 (Contact.Account.Name) は今まで通り。

    id_column は None で、 SOQL は Account.Name のまま。

    """

    from src.mapping import _resolve_column_key_to_soql

    contact_describe = {
        "name": "Contact",
        "fields": [
            {
                "name": "AccountId",
                "type": "reference",
                "referenceTo": ["Account"],
                "relationshipName": "Account",
            },
        ],
    }

    account_describe = {
        "name": "Account",
        "fields": [
            {"name": "Name", "type": "string", "nameField": True, "label": "Name"},
        ],
    }

    def fetch_object(name: str) -> dict | None:

        return account_describe if name == "Account" else None

    soql_name, field_type, failure_note, id_column = _resolve_column_key_to_soql(
        main_object="Contact",
        column_key="Contact.Account.Name",
        main_describe=contact_describe,
        fetch_object=fetch_object,
    )

    assert failure_note is None

    assert soql_name == "Account.Name"

    assert field_type == "string"

    # 末端が参照じゃないので id_column は付かない

    assert id_column is None


def test_build_column_map_resolves_fk_with_reference_to_name_replacement() -> None:
    """FK_Contact.Account__c (Contact の Account__c が参照) ->

    Contact__r.Account__r.Name。 備考は

    'FK_ から(参照先の名前。ID なら Contact__r.Account__c)'。

    """

    opportunity_describe = _describe_with_fields(
        [
            {
                "name": "Contact__c",
                "type": "reference",
                "referenceTo": ["Contact"],
                "relationshipName": "Contact__r",
            },
        ]
    )

    contact_describe = {
        "name": "Contact",
        "fields": [
            {
                "name": "Account__c",
                "type": "reference",
                "referenceTo": ["Account"],
                "relationshipName": "Account__r",
            },
        ],
    }

    account_describe = {
        "name": "Account",
        "fields": [
            {"name": "Name", "type": "string", "nameField": True, "label": "Name"},
        ],
    }

    def fetch_object(name: str) -> dict | None:

        if name == "Contact":
            return contact_describe

        if name == "Account":
            return account_describe

        return None

    metadata = _make_metadata(
        detail_columns=["FK_Contact.Account__c"],
        detail_column_labels={"FK_Contact.Account__c": "Contact Account"},
    )

    rows = build_column_map(
        metadata,
        opportunity_describe,
        None,
        related_describes={},
        relations=[],
        main_object="Opportunity",
        fetch_object=fetch_object,
    )

    row = next(r for r in rows if r["列キー"] == "FK_Contact.Account__c")

    assert row["対応フィールドAPI名"] == "Contact__r.Account__r.Name"

    assert row["型"] == "string"

    assert row["備考"] == "FK_ から（参照先の名前。ID なら Contact__r.Account__c）"

    # 末端が名前項目なので参照先 / リレーション名は空のまま

    assert row["参照先オブジェクト"] == ""

    assert row["リレーション名"] == ""


def test_build_column_map_resolves_column_key_with_reference_to_name_replacement() -> None:
    """Task__c.Custom_Name__c (列キー) -> Custom_Name__r.Name。

    備考は '列キーから(参照先の名前。ID なら Custom_Name__c)'。

    """

    metadata = _make_metadata(
        detail_columns=["Task__c.Custom_Name__c"],
        detail_column_labels={"Task__c.Custom_Name__c": "Custom Name"},
    )

    def fetch_object(name: str) -> dict | None:

        return _foo_describe_with_name_field() if name == "Foo__c" else None

    rows = build_column_map(
        metadata,
        _custom_name_field_describe(),
        None,
        related_describes={},
        relations=[],
        main_object="Task__c",
        fetch_object=fetch_object,
    )

    row = next(r for r in rows if r["列キー"] == "Task__c.Custom_Name__c")

    assert row["対応フィールドAPI名"] == "Custom_Name__r.Name"

    assert row["型"] == "string"

    assert row["備考"] == "列キーから（参照先の名前。ID なら Custom_Name__c）"

    # 末端が名前項目なので参照先 / リレーション名は空のまま

    assert row["参照先オブジェクト"] == ""

    assert row["リレーション名"] == ""


def test_build_column_map_resolves_polymorphic_reference_in_column_key() -> None:
    """Task__c.OwnerId (ポリモーフィック) -> Owner.Name。

    備考は '列キーから(参照先の名前。ID なら OwnerId)'。

    """

    main_describe = {
        "name": "Task__c",
        "fields": [
            {
                "name": "OwnerId",
                "type": "reference",
                "referenceTo": ["User", "Group"],
                "relationshipName": "Owner",
            },
        ],
    }

    metadata = _make_metadata(
        detail_columns=["Task__c.OwnerId"],
        detail_column_labels={"Task__c.OwnerId": "Owner"},
    )

    rows = build_column_map(
        metadata,
        main_describe,
        None,
        related_describes={},
        relations=[],
        main_object="Task__c",
        fetch_object=lambda _name: None,
    )

    row = next(r for r in rows if r["列キー"] == "Task__c.OwnerId")

    assert row["対応フィールドAPI名"] == "Owner.Name"

    assert row["型"] == "string"

    assert row["備考"] == "列キーから（参照先の名前。ID なら OwnerId）"


# ── 「 オブジェクト: 項目名 」 形式の照合 ─────────────────────


def test_build_column_map_resolves_object_colon_label_main() -> None:
    """「 タスク: 作業会社名 」 形式の表示名 → 主オブジェクト Task の項目から拾う。"""
    main_describe = {
        "name": "Task",
        "label": "タスク",
        "fields": [
            {"name": "Subject", "label": "件名", "type": "Text"},
            {"name": "Custom_Work_Company__c", "label": "作業会社名", "type": "Text"},
        ],
    }
    metadata = _make_metadata(
        detail_columns=["TASK.CUSTOM_WORK_COMPANY__C"],
        detail_column_labels={"TASK.CUSTOM_WORK_COMPANY__C": "タスク: 作業会社名"},
    )
    rows = build_column_map(metadata, main_describe, None)
    row = next(r for r in rows if r["列キー"] == "TASK.CUSTOM_WORK_COMPANY__C")
    assert row["対応フィールドAPI名"] == "Custom_Work_Company__c"


def test_build_column_map_resolves_object_colon_label_related() -> None:
    """「 取引先: 取引先名 」 形式 → 関連 Account の項目から拾う。"""
    main_describe = {
        "name": "Opportunity",
        "label": "商談",
        "fields": [
            {
                "name": "AccountId",
                "type": "reference",
                "referenceTo": ["Account"],
                "relationshipName": "Account",
            },
        ],
    }
    account_describe = {
        "name": "Account",
        "label": "取引先",
        "fields": [
            {"name": "Name", "label": "取引先名", "type": "Text"},
            {"name": "Industry", "label": "業種", "type": "Text"},
        ],
    }
    metadata = _make_metadata(
        detail_columns=["OPP.ACCOUNT.NAME"],
        detail_column_labels={"OPP.ACCOUNT.NAME": "取引先: 取引先名"},
    )
    rows = build_column_map(
        metadata,
        main_describe,
        None,
        related_describes={"Account": account_describe},
        relations=[_fake_relation("Opportunity", "AccountId", "Account", "Account", 1)],
        main_object="Opportunity",
    )
    row = next(r for r in rows if r["列キー"] == "OPP.ACCOUNT.NAME")
    assert row["対応フィールドAPI名"] == "Account.Name"


def test_build_column_map_resolves_object_colon_label_zenkaku_colon() -> None:
    """全角コロン 「 ： 」 も同じように扱われる。"""
    main_describe = {
        "name": "Task",
        "label": "タスク",
        "fields": [
            {"name": "Subject", "label": "件名", "type": "Text"},
            {"name": "Custom_Work_Company__c", "label": "作業会社名", "type": "Text"},
        ],
    }
    metadata = _make_metadata(
        detail_columns=["TASK.CUSTOM_WORK_COMPANY__C"],
        detail_column_labels={"TASK.CUSTOM_WORK_COMPANY__C": "タスク：作業会社名"},
    )
    rows = build_column_map(metadata, main_describe, None)
    row = next(r for r in rows if r["列キー"] == "TASK.CUSTOM_WORK_COMPANY__C")
    assert row["対応フィールドAPI名"] == "Custom_Work_Company__c"


def test_build_column_map_object_colon_label_falls_back_when_object_unknown() -> None:
    """コロン前のオブジェクトが見つからないときは、 後ろだけで照合。"""
    main_describe = {
        "name": "Task",
        "label": "タスク",
        "fields": [
            {"name": "Subject", "label": "件名", "type": "Text"},
        ],
    }
    metadata = _make_metadata(
        detail_columns=["TASK.SUBJECT"],
        detail_column_labels={"TASK.SUBJECT": "存在しないオブジェクト: 件名"},
    )
    rows = build_column_map(metadata, main_describe, None)
    row = next(r for r in rows if r["列キー"] == "TASK.SUBJECT")
    # 後ろだけの照合で Subject が見つかる
    assert row["対応フィールドAPI名"] == "Subject"


def test_build_column_map_resolves_object_colon_label_multiple_candidates() -> None:
    """「 取引先: 件名 」 形式で、 取引先に同名項目が複数あれば、 候補として残る。"""
    main_describe = {
        "name": "Opportunity",
        "label": "商談",
        "fields": [
            {
                "name": "AccountId",
                "type": "reference",
                "referenceTo": ["Account"],
                "relationshipName": "Account",
            },
        ],
    }
    account_describe = {
        "name": "Account",
        "label": "取引先",
        "fields": [
            {"name": "Name", "label": "取引先名", "type": "Text"},
            {"name": "Subject__c", "label": "件名", "type": "Text"},
            {"name": "OtherSubject__c", "label": "件名", "type": "Text"},
        ],
    }
    metadata = _make_metadata(
        detail_columns=["OPP.ACCOUNT.SUBJECT__C"],
        detail_column_labels={"OPP.ACCOUNT.SUBJECT__C": "取引先: 件名"},
    )
    rows = build_column_map(
        metadata,
        main_describe,
        None,
        related_describes={"Account": account_describe},
        relations=[_fake_relation("Opportunity", "AccountId", "Account", "Account", 1)],
        main_object="Opportunity",
    )
    row = next(r for r in rows if r["列キー"] == "OPP.ACCOUNT.SUBJECT__C")
    # 候補が複数あるときは 「 複数候補あり 」 備考
    assert "複数候補あり" in row["備考"]
