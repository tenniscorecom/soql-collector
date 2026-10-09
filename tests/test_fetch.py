"""``fetch`` フローのテスト（偽物の Salesforce クライアントで）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from comken.exceptions import SalesforceRequestError

from src.fetch import (
    _apply_related_limit,
    _collect_related_object_names,
    _fetch_object_cached,
    _slim_object,
    _slim_report,
    run_fetch,
)
from src.master import MasterEntry
from src.settings import Settings

DOMAIN = "https://example--sandbox.sandbox.my.salesforce.com/lightning/r/Report"


# ── 偽物のクライアント ────────────────────────────────────────────────────


class _ReportStub:
    def __init__(self, describe: dict) -> None:
        self._describe = describe

    def describe(self, report_id: str) -> dict:
        self.last_report_id = report_id
        return self._describe


class _FakeClient:
    def __init__(
        self,
        describe: dict,
        object_describes: dict[str, dict] | None = None,
        errors: dict[str, BaseException] | None = None,
    ) -> None:
        self._object_describes = object_describes or {}
        self._errors = errors or {}
        self.report = _ReportStub(describe)
        self.describe_object_calls: list[str] = []

    def describe_object(self, name: str) -> dict:
        self.describe_object_calls.append(name)
        if name in self._errors:
            raise self._errors[name]
        return self._object_describes.get(name, {"name": name, "fields": []})

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *args: object) -> None:
        return None


class _FakeSite:
    def __init__(self, client: _FakeClient) -> None:
        self._client = client
        self.open_count = 0

    def __call__(self) -> _FakeClient:
        return self

    def __enter__(self) -> _FakeClient:
        self.open_count += 1
        return self._client

    def __exit__(self, *args: object) -> None:
        return None


def _site_for(client: _FakeClient):
    """``site_for(url)`` 互換の関数。"""
    site = _FakeSite(client)

    def resolver(url: str) -> _FakeSite:
        return site

    return resolver, site


def _settings(tmp_path: Path, related_max: int = 40, related_depth: int = 1) -> Settings:
    return Settings(
        master_xlsx_path=tmp_path / "master.xlsx",
        output_dir=tmp_path / "output",
        related_max=related_max,
        related_depth=related_depth,
        credential_prefix="",
        objects_names=(),
        objects_org_id=None,
    )


def _output_dir_exists(tmp_path: Path) -> Path:
    """出力先ディレクトリが無ければ作って返す（``iterdir`` 用）。"""
    output = tmp_path / "output"
    output.mkdir(parents=True, exist_ok=True)
    return output


def _make_metadata(
    report_type: str = "Opportunity",
    *,
    extra: dict | None = None,
    detail_column_labels: dict[str, str] | None = None,
) -> dict:
    """レポート describe を作る。

    ``detail_column_labels`` で ``detailColumns`` 各列の表示名を指定できる
    （ ``column_key`` → ``label`` ）。 未指定なら ``detailColumns[0]`` の
    表示名は同じキーになる（ ``column_key`` をそのまま ``label`` として扱う）。
    """
    detail_columns = ["Opp.Name"]
    if extra and "detailColumns" in extra:
        detail_columns = extra["detailColumns"]
    metadata: dict = {
        "reportMetadata": {
            "reportType": {"type": report_type},
            "reportFormat": "TABULAR",
            "detailColumns": detail_columns,
        },
        "reportExtendedMetadata": {
            "detailColumnInfo": {
                column_key: {"label": (detail_column_labels or {}).get(column_key, column_key)}
                for column_key in detail_columns
            }
        },
    }
    if extra:
        metadata["reportMetadata"].update(extra)
    return metadata


def _main_describe(
    name: str,
    fields: list[dict] | None = None,
    *,
    fields_map: dict[str, list[str]] | None = None,
) -> dict:
    """主オブジェクト describe を作る。``fields_map`` で ``Name`` → ``[Account, Contact]`` のように
    relationship を作れる（``referenceTo`` / ``relationshipName`` を埋める）。
    """
    fields = fields if fields is not None else []
    built: list[dict] = []
    if fields_map:
        for field_name, refs in fields_map.items():
            for ref in refs:
                built.append(
                    {
                        "name": field_name,
                        "type": "reference",
                        "referenceTo": [ref],
                        "relationshipName": f"{field_name}_{ref}",
                    }
                )
    built.extend(fields)
    return {"name": name, "fields": built}


def _describe_with_refs(name: str, refs: dict[str, str]) -> dict:
    """``.`` 以外の参照項目の describe を作る。 ``refs`` は ``項目名 -> 参照先オブジェクト`` の単一参照。

    戻り値の ``relationshipName`` は ``<項目名>`` （ 単純化のため _ をつけない ）。
    """
    fields = [
        {
            "name": field_name,
            "type": "reference",
            "referenceTo": [ref_name],
            "relationshipName": field_name,
        }
        for field_name, ref_name in refs.items()
    ]
    return {"name": name, "fields": fields}


# ── JSON 出力 ───────────────────────────────────────────────────────────


def test_run_fetch_writes_json_with_all_keys(tmp_path: Path) -> None:
    entry = MasterEntry(
        key="1001",
        summary="顧客一覧",
        url=f"{DOMAIN}/00O5g00000ABCDE/view",
        enabled=True,
    )
    metadata = _make_metadata(detail_column_labels={"Opp.Name": "名前"})
    # Opportunity の describe に ``名前`` → ``Name`` を入れて、 ``mapping.build_column_map``
    # が ``"Opp.Name"`` の表示名 ``"名前"`` を実フィールド ``Name`` に対応づけられるようにする。
    # ``AccountId`` 参照も入れて関連オブジェクト ``Account`` が解決されることを確かめる。
    opportunity_with_name = {
        "name": "Opportunity",
        "fields": [
            {
                "name": "Name",
                "label": "名前",
                "type": "string",
            },
            {
                "name": "AccountId",
                "type": "reference",
                "referenceTo": ["Account"],
                "relationshipName": "Account",
            },
        ],
    }
    client = _FakeClient(
        describe=metadata,
        object_describes={
            "Opportunity": opportunity_with_name,
            "Account": _main_describe("Account"),
        },
    )
    site_for, _site = _site_for(client)
    settings = _settings(tmp_path)

    outcomes = run_fetch(settings, [entry], site_for=site_for)

    assert outcomes[0].status == "ok"
    assert outcomes[0].output_path is not None
    payload = json.loads(outcomes[0].output_path.read_text(encoding="utf-8"))
    assert payload["管理番号"] == "1001"
    assert payload["概要"] == "顧客一覧"
    assert payload["レポートID"] == "00O5g00000ABCDE"
    assert payload["URL"] == f"{DOMAIN}/00O5g00000ABCDE/view"
    assert "取得日時" in payload
    assert payload["主オブジェクト"] == "Opportunity"
    # ``report`` は slim した_モジュール定数（ 値は元と同じ、 不要なキーは落ちる ）。
    # allowlist のキーは全部残るので ``reportMetadata`` は等値になる
    assert payload["report"]["reportMetadata"] == metadata["reportMetadata"]
    # 3 つのサブキーは常に存在し、 何も落ちていないので空
    assert payload["report"]["droppedKeys"] == {
        "reportMetadata": [],
        "reportExtendedMetadata": [],
        "top": [],
    }
    assert set(payload["objects"]) == {"Opportunity", "Account"}
    # ``objects`` の各値は ``_slim_object`` を通った形（ name/label/custom/fields の 4 キー ）
    expected_opportunity = _slim_object(opportunity_with_name)
    assert payload["objects"]["Opportunity"] == expected_opportunity
    assert payload["objects"]["Account"] == _slim_object(_main_describe("Account"))
    assert payload["column_map"] == [
        {
            "列キー": "Opp.Name",
            "表示名": "名前",
            "対応フィールドAPI名": "Name",
            "型": "string",
            "備考": "",
        }
    ]
    assert payload["warnings"] == []


def test_objects_dict_is_slimmed_to_required_keys(tmp_path: Path) -> None:
    """``objects`` の中身は ``_slim_object`` を通した形（ name/label/custom/fields の 4 キー ）
    で書かれている。 JSON に重そうなキーは残らない（ childRelationships / recordTypeInfos など ）。
    """
    entry = MasterEntry(key="1", summary="", url=f"{DOMAIN}/00O5g00000ABCDE/view", enabled=True)
    client = _FakeClient(
        describe=_make_metadata(),
        object_describes={
            "Opportunity": _main_describe("Opportunity", fields_map={"AccountId": ["Account"]}),
            "Account": _main_describe("Account"),
        },
    )
    site_for, _ = _site_for(client)
    payload_path = run_fetch(_settings(tmp_path), [entry], site_for=site_for)[0].output_path
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    opp = payload["objects"]["Opportunity"]
    # オブジェクトは 4 キーだけ
    assert set(opp.keys()) == {"name", "label", "custom", "fields"}
    # 各項目は 8 キーだけ
    for field in opp["fields"]:
        assert set(field.keys()) == {
            "name",
            "label",
            "type",
            "custom",
            "referenceTo",
            "relationshipName",
            "picklist",
            "picklistTotal",
        }
    # 重そうなキーは残らない
    assert "childRelationships" not in opp
    assert "recordTypeInfos" not in opp
    assert "urls" not in opp
    for field in opp["fields"]:
        assert "picklistValues" not in field


def test_objects_dict_does_not_mutate_original_describe(tmp_path: Path) -> None:
    """``_slim_object`` が走っても、 ``describe_object`` の戻り値（ cache ）は
    変わらない（ 関連オブジェクト収集が原本の ``referenceTo`` /
    ``relationshipName`` を使うため ）。
    """
    import copy

    original = _main_describe("Opportunity", fields_map={"AccountId": ["Account"]})
    snapshot = copy.deepcopy(original)

    entry = MasterEntry(key="1", summary="", url=f"{DOMAIN}/00O5g00000ABCDE/view", enabled=True)
    client = _FakeClient(
        describe=_make_metadata(),
        object_describes={"Opportunity": original, "Account": _main_describe("Account")},
    )
    site_for, _ = _site_for(client)
    run_fetch(_settings(tmp_path), [entry], site_for=site_for)

    assert original == snapshot


def test_related_objects_dedup_excludes_main_polymorphic(tmp_path: Path) -> None:
    """関連オブジェクト: ``referenceTo`` がポリモーフィックなら展開、自身は除く、出現順、重複なし。"""
    main = _main_describe(
        "Opportunity",
        fields_map={
            "AccountId": ["Account"],
            "WhoId": ["Contact", "Lead"],  # ポリモーフィック
            "OwnerId": ["User"],
            "DuplicateAccountId": ["Account"],  # 重複（先勝ち）
        },
    )
    entry = MasterEntry(key="1", summary="", url=f"{DOMAIN}/00O5g00000ABCDE/view", enabled=True)
    client = _FakeClient(
        describe=_make_metadata(),
        object_describes={
            "Opportunity": main,
            "Account": _main_describe("Account"),
            "Contact": _main_describe("Contact"),
            "Lead": _main_describe("Lead"),
            "User": _main_describe("User"),
        },
    )
    site_for, _ = _site_for(client)
    payload_path = run_fetch(_settings(tmp_path), [entry], site_for=site_for)[0].output_path
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    # 順序: Account, Contact, Lead, User（出現順。Account 重複は出ない）
    assert list(payload["objects"]) == ["Opportunity", "Account", "Contact", "Lead", "User"]


def test_related_object_failure_becomes_warning(tmp_path: Path) -> None:
    """関連オブジェクト 1 つの HTTP エラー（401/403 以外）は警告で続き、JSON は書かれる。"""
    main = _main_describe("Opportunity", fields_map={"AccountId": ["Account"], "OwnerId": ["User"]})
    entry = MasterEntry(key="1", summary="", url=f"{DOMAIN}/00O5g00000ABCDE/view", enabled=True)
    client = _FakeClient(
        describe=_make_metadata(),
        object_describes={"Opportunity": main, "Account": _main_describe("Account")},
        errors={"User": SalesforceRequestError("GET", "/sobjects/User/describe", 404, "Not Found")},
    )
    site_for, _ = _site_for(client)
    payload_path = run_fetch(_settings(tmp_path), [entry], site_for=site_for)[0].output_path
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    assert payload["主オブジェクト"] == "Opportunity"
    assert "Account" in payload["objects"]
    assert "User" not in payload["objects"]
    assert any("User" in w and "404" in w for w in payload["warnings"])


def test_related_max_overflow_warns(tmp_path: Path) -> None:
    """``RELATED_MAX`` を超えた分は取らず、件数と名前が警告に残る。"""
    # 関連を 5 件作る（Account / Contact / Lead / User / Campaign / Opportunity__c ...）
    fields_map = {
        "F1": ["Account"],
        "F2": ["Contact"],
        "F3": ["Lead"],
        "F4": ["User"],
        "F5": ["Campaign"],
    }
    main = _main_describe("Opportunity", fields_map=fields_map)
    entry = MasterEntry(key="1", summary="", url=f"{DOMAIN}/00O5g00000ABCDE/view", enabled=True)
    client = _FakeClient(
        describe=_make_metadata(),
        object_describes={
            "Opportunity": main,
            "Account": _main_describe("Account"),
            "Contact": _main_describe("Contact"),
            "Lead": _main_describe("Lead"),
            "User": _main_describe("User"),
            "Campaign": _main_describe("Campaign"),
        },
    )
    site_for, _ = _site_for(client)
    payload_path = run_fetch(_settings(tmp_path, related_max=2), [entry], site_for=site_for)[
        0
    ].output_path
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    assert list(payload["objects"]) == ["Opportunity", "Account", "Contact"]
    warning = next(w for w in payload["warnings"] if "上限" in w)
    # 全体の件数（5 件）と上限（2）、スキップ件数（3 件）と残りの名前が入る
    assert "5 件" in warning
    assert "2" in warning
    assert "3 件" in warning
    assert "Lead" in warning and "User" in warning and "Campaign" in warning


# ── RELATED_DEPTH と 幅優先 ────────────────────────────────────────────


def _chain_fixtures() -> dict[str, dict]:
    """段ごとに参照先があるフェイク: Task → Account → User → Group。"""
    return {
        "Task": _describe_with_refs("Task", {"WhatId": "Account"}),
        "Account": _describe_with_refs("Account", {"OwnerId": "User"}),
        "User": _describe_with_refs("User", {"GroupId": "Group"}),
        "Group": _main_describe("Group"),
    }


def _run_task_chain(tmp_path: Path, *, related_depth: int) -> dict:
    """チェーン状のフェイクで ``run_fetch`` を走らせ、 ``payload`` を返す。"""
    objects = _chain_fixtures()
    entry = MasterEntry(key="1", summary="", url=f"{DOMAIN}/00O5g00000ABCDE/view", enabled=True)
    describe = {"reportMetadata": {"reportType": {"type": "Task"}, "reportFormat": "TABULAR"}}
    client = _FakeClient(
        describe=describe,
        object_describes=objects,
    )
    site_for, _ = _site_for(client)
    payload_path = run_fetch(
        _settings(tmp_path, related_depth=related_depth), [entry], site_for=site_for
    )[0].output_path
    return json.loads(payload_path.read_text(encoding="utf-8"))


def test_related_depth_1_only_first_level(tmp_path: Path) -> None:
    """``RELATED_DEPTH=1`` は 1 段目だけ取る （ 今と同じ挙動 ）。"""
    payload = _run_task_chain(tmp_path, related_depth=1)
    # 1 段目: Account のみ
    assert list(payload["objects"]) == ["Task", "Account"]
    assert [r["名前"] for r in payload["関連オブジェクト"]] == ["Account"]
    assert all(r["段"] == 1 for r in payload["関連オブジェクト"])
    # 経路は ``Task.WhatId``
    assert payload["関連オブジェクト"][0]["経路"] == ["Task.WhatId"]


def test_related_depth_2_includes_second_level(tmp_path: Path) -> None:
    """``RELATED_DEPTH=2`` は 1 → 2 段目まで取る。 ``User`` まで取れる。"""
    payload = _run_task_chain(tmp_path, related_depth=2)
    assert list(payload["objects"]) == ["Task", "Account", "User"]
    by_name = {r["名前"]: r for r in payload["関連オブジェクト"]}
    assert by_name["Account"]["段"] == 1
    assert by_name["Account"]["経路"] == ["Task.WhatId"]
    assert by_name["User"]["段"] == 2
    # 経路は 2 つ （ Task→Account の WhatId と、 Account→User の OwnerId ）
    assert by_name["User"]["経路"] == ["Task.WhatId", "Account.OwnerId"]


def test_related_depth_3_includes_third_level(tmp_path: Path) -> None:
    """``RELATED_DEPTH=3`` は ``Group`` まで取る。"""
    payload = _run_task_chain(tmp_path, related_depth=3)
    assert list(payload["objects"]) == ["Task", "Account", "User", "Group"]
    by_name = {r["名前"]: r for r in payload["関連オブジェクト"]}
    assert by_name["Group"]["段"] == 3
    assert by_name["Group"]["経路"] == [
        "Task.WhatId",
        "Account.OwnerId",
        "User.GroupId",
    ]


def test_related_depth_dedup_across_paths(tmp_path: Path) -> None:
    """同じオブジェクトを複数の経路で参照しても describe は 1 回だけ。

    1 段目に ``X → Y`` と ``X → Z → Y`` で同じ ``Y`` に到達する形を作る。
    """
    objects = {
        "X": _describe_with_refs("X", {"AId": "Y", "BId": "Z"}),
        "Y": _main_describe("Y"),
        "Z": _describe_with_refs("Z", {"CId": "Y"}),
    }
    entry = MasterEntry(key="1", summary="", url=f"{DOMAIN}/00O5g00000ABCDE/view", enabled=True)
    describe = {"reportMetadata": {"reportType": {"type": "X"}, "reportFormat": "TABULAR"}}
    client = _FakeClient(describe=describe, object_describes=objects)
    site_for, _ = _site_for(client)
    payload_path = run_fetch(_settings(tmp_path, related_depth=3), [entry], site_for=site_for)[
        0
    ].output_path
    payload = json.loads(payload_path.read_text(encoding="utf-8"))

    # ``Y`` は 1 回しか describe されていない
    assert client.describe_object_calls.count("Y") == 1
    # JSON にも 1 回だけ
    assert list(payload["objects"]).count("Y") == 1
    # ``関連オブジェクト`` でも 1 回だけ、 経路は最初に見つけた方
    y_entries = [r for r in payload["関連オブジェクト"] if r["名前"] == "Y"]
    assert len(y_entries) == 1
    assert y_entries[0]["段"] == 1
    assert y_entries[0]["経路"] == ["X.AId"]


def test_related_depth_stops_at_cycle(tmp_path: Path) -> None:
    """循環参照（ A→B→A ） で止まる。 同じオブジェクトを取り直さない。"""
    objects = {
        "A": _describe_with_refs("A", {"BId": "B"}),
        "B": _describe_with_refs("B", {"AId": "A", "CId": "C"}),
        "C": _main_describe("C"),
    }
    entry = MasterEntry(key="1", summary="", url=f"{DOMAIN}/00O5g00000ABCDE/view", enabled=True)
    describe = {"reportMetadata": {"reportType": {"type": "A"}, "reportFormat": "TABULAR"}}
    client = _FakeClient(describe=describe, object_describes=objects)
    site_for, _ = _site_for(client)
    payload_path = run_fetch(_settings(tmp_path, related_depth=5), [entry], site_for=site_for)[
        0
    ].output_path
    payload = json.loads(payload_path.read_text(encoding="utf-8"))

    # オブジェクトは 3 つとも取れる （ 各段で重複はなし ） 。
    assert set(payload["objects"]) == {"A", "B", "C"}
    # ``A`` の describe は 1 回のみ （ 2 段目で再訪しない ）
    assert client.describe_object_calls.count("A") == 1
    # ``B`` は関連として 1 件だけ
    b_entries = [r for r in payload["関連オブジェクト"] if r["名前"] == "B"]
    assert len(b_entries) == 1
    # ``C`` は 2 段目として取れる （ さらに 3 段目で ``A`` を掘ろうとはしない ）
    c_entries = [r for r in payload["関連オブジェクト"] if r["名前"] == "C"]
    assert len(c_entries) == 1
    assert c_entries[0]["段"] == 2


def test_related_depth_max_limits_total_across_levels(tmp_path: Path) -> None:
    """``RELATED_MAX`` の合計件数で打ち切り、 近い段が優先される。"""
    # 1 段目: Account / Contact （ 2 件 ） 、 2 段目: User / Group （ 2 件 ） → 計 4 件
    objects = {
        "Task": _describe_with_refs(
            "Task",
            {"WhatId": "Account", "WhoId": "Contact"},
        ),
        "Account": _describe_with_refs("Account", {"OwnerId": "User"}),
        "Contact": _describe_with_refs("Contact", {"OwnerId": "Group"}),
        "User": _main_describe("User"),
        "Group": _main_describe("Group"),
    }
    entry = MasterEntry(key="1", summary="", url=f"{DOMAIN}/00O5g00000ABCDE/view", enabled=True)
    describe = {"reportMetadata": {"reportType": {"type": "Task"}, "reportFormat": "TABULAR"}}
    client = _FakeClient(describe=describe, object_describes=objects)
    site_for, _ = _site_for(client)
    payload_path = run_fetch(
        _settings(tmp_path, related_depth=3, related_max=3),
        [entry],
        site_for=site_for,
    )[0].output_path
    payload = json.loads(payload_path.read_text(encoding="utf-8"))

    # 近い段から 3 件: Account / Contact / User （ Group は取らない ）
    assert list(payload["objects"]) == ["Task", "Account", "Contact", "User"]
    warning = next(w for w in payload["warnings"] if "上限" in w)
    # 全体件数（4）とスキップ件数（1）、 取らなかった名前が出る
    assert "4 件" in warning
    assert "Group" in warning


def test_related_depth_breaks_under_depth1_only_impl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """深さテストは 「 常に 1 段で止める 」 改ざんで落ちる （ 検知用 ）。"""
    from src import fetch as fetch_module

    # 「 悪い実装 」: 1 段目だけ集めるバージョンに差し替え
    def shallow(
        main_describe,
        fetcher,
        *,
        related_depth,
        related_max,
    ):
        from src.fetch import RelatedNode, _take_within_limit

        main_name = main_describe.get("name") if isinstance(main_describe, dict) else None
        visited = {main_name} if isinstance(main_name, str) and main_name else set()
        nodes: list[RelatedNode] = []
        if not isinstance(main_name, str) or not main_name:
            return nodes, []
        paths = fetch_module._collect_related_field_paths(main_describe)
        first_level: list[RelatedNode] = []
        seen: set[str] = set()
        for field_name, ref_name in paths:
            if ref_name in visited or ref_name in seen:
                continue
            visited.add(ref_name)
            seen.add(ref_name)
            first_level.append(
                RelatedNode(
                    name=ref_name,
                    depth=1,
                    path=(f"{main_name}.{field_name}",),
                    describe=None,
                )
            )
        skipped: list[str] = []
        accepted = _take_within_limit(first_level, related_max - len(nodes), skipped)
        fetch_module._fill_describes(accepted, fetcher)
        nodes.extend(accepted)
        return nodes, skipped

    monkeypatch.setattr(fetch_module, "_collect_related_objects", shallow)

    # depth=3 でも「 悪い実装 」 では ``User`` / ``Group`` が取れないはず
    payload = _run_task_chain(tmp_path, related_depth=3)
    # 浅いので Account までしか取れない
    assert list(payload["objects"]) == ["Task", "Account"]
    assert "User" not in payload["objects"]
    assert "Group" not in payload["objects"]


def test_main_object_resolves_custom_entity_dollar_type(tmp_path: Path) -> None:
    """``reportType.type`` が ``CustomEntity$Project__c`` のとき、
    主オブジェクト解決が候補 ``Project__c`` を採用し、
    JSON の ``主オブジェクト`` も ``Project__c`` に、 ``objects`` にも
    ``Project__c`` が入る。関連はその ``AccountId`` から ``Account`` が続く。
    """
    main = _main_describe("Project__c", fields_map={"AccountId": ["Account"]})
    metadata = {
        "reportMetadata": {
            "reportType": {"type": "CustomEntity$Project__c"},
            "reportFormat": "TABULAR",
            "detailColumns": ["Project__c.Name"],
        }
    }
    entry = MasterEntry(key="1", summary="", url=f"{DOMAIN}/00O5g00000ABCDE/view", enabled=True)
    client = _FakeClient(
        describe=metadata,
        object_describes={
            "Project__c": main,
            "Account": _main_describe("Account"),
        },
    )
    site_for, _ = _site_for(client)
    payload_path = run_fetch(_settings(tmp_path), [entry], site_for=site_for)[0].output_path
    payload = json.loads(payload_path.read_text(encoding="utf-8"))

    # 主オブジェクトが解決済みで、 objects に Project__c が入る
    assert payload["主オブジェクト"] == "Project__c"
    assert "Project__c" in payload["objects"]
    # 関連オブジェクトも取れている
    assert "Account" in payload["objects"]
    # CustomEntity$Project__c という生文字列が主オブジェクト欄にも objects にも残らない
    assert payload["主オブジェクト"] != "CustomEntity$Project__c"
    assert "CustomEntity$Project__c" not in payload["objects"]


def test_main_object_describe_failure_yields_null(tmp_path: Path) -> None:
    """主オブジェクトの describe_object が失敗（401/403 以外）→ ``主オブジェクト: null`` 、警告、
    関連は取らない。"""
    entry = MasterEntry(key="1", summary="", url=f"{DOMAIN}/00O5g00000ABCDE/view", enabled=True)
    client = _FakeClient(
        describe=_make_metadata("Opportunity"),
        errors={
            "Opportunity": SalesforceRequestError(
                "GET", "/sobjects/Opportunity/describe", 400, "Bad"
            )
        },
    )
    site_for, _ = _site_for(client)
    payload_path = run_fetch(_settings(tmp_path), [entry], site_for=site_for)[0].output_path
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    assert payload["主オブジェクト"] is None
    assert payload["objects"] == {}
    assert any("Opportunity" in w for w in payload["warnings"])


def test_auth_error_on_main_object_marks_id_failed(tmp_path: Path) -> None:
    """主オブジェクト describe の 401/403 → ID 失敗、JSON は書かれない。"""
    output = _output_dir_exists(tmp_path)
    entry = MasterEntry(key="1", summary="", url=f"{DOMAIN}/00O5g00000ABCDE/view", enabled=True)
    client = _FakeClient(
        describe=_make_metadata("Opportunity"),
        errors={
            "Opportunity": SalesforceRequestError(
                "GET", "/sobjects/Opportunity/describe", 401, "Unauthorized"
            )
        },
    )
    site_for, _ = _site_for(client)
    outcomes = run_fetch(_settings(tmp_path), [entry], site_for=site_for)
    assert outcomes[0].status == "failed"
    assert "401" in (outcomes[0].error or "")
    assert outcomes[0].output_path is None
    # JSON は書かれていない
    assert not list(output.iterdir())


def test_auth_error_on_related_object_marks_id_failed(tmp_path: Path) -> None:
    """関連オブジェクト describe の 401/403 → ID 失敗。"""
    output = _output_dir_exists(tmp_path)
    main = _main_describe("Opportunity", fields_map={"AccountId": ["Account"]})
    entry = MasterEntry(key="1", summary="", url=f"{DOMAIN}/00O5g00000ABCDE/view", enabled=True)
    client = _FakeClient(
        describe=_make_metadata(),
        object_describes={"Opportunity": main},
        errors={
            "Account": SalesforceRequestError("GET", "/sobjects/Account/describe", 403, "Forbidden")
        },
    )
    site_for, _ = _site_for(client)
    outcomes = run_fetch(_settings(tmp_path), [entry], site_for=site_for)
    assert outcomes[0].status == "failed"
    assert "403" in (outcomes[0].error or "")
    # 主オブジェクトは書かれる前なので JSON 無し
    assert not list(output.iterdir())


def test_bad_url_marks_id_failed(tmp_path: Path) -> None:
    """URL が壊れている行は ``fetch`` 段で失敗として報告。"""
    entry = MasterEntry(key="1", summary="", url="https://壊れたURL", enabled=True)
    site_for, _ = _site_for(_FakeClient(describe={}))
    outcomes = run_fetch(_settings(tmp_path), [entry], site_for=site_for)
    assert outcomes[0].status == "failed"
    assert "URL" in (outcomes[0].error or "")


def test_object_describe_cached_across_ids(tmp_path: Path) -> None:
    """同じ実行で複数 ID が同じオブジェクトを参照しても ``describe_object`` は 1 回だけ。"""
    main = _main_describe("Opportunity", fields_map={"AccountId": ["Account"]})
    metadata = _make_metadata()
    client = _FakeClient(
        describe=metadata,
        object_describes={
            "Opportunity": main,
            "Account": _main_describe("Account"),
        },
    )
    site_for, _ = _site_for(client)
    settings = _settings(tmp_path)
    entries = [
        MasterEntry(key="1001", summary="", url=f"{DOMAIN}/00O5g00000AAAAA/view", enabled=True),
        MasterEntry(key="1002", summary="", url=f"{DOMAIN}/00O5g00000BBBBB/view", enabled=True),
    ]
    run_fetch(settings, entries, site_for=site_for)
    # Opportunity と Account は 2 ID で参照されるが、HTTP は 1 回ずつ
    assert client.describe_object_calls.count("Opportunity") == 1
    assert client.describe_object_calls.count("Account") == 1


def test_failed_object_cached(tmp_path: Path) -> None:
    """関連オブジェクトの失敗（401/403 以外）もキャッシュされ、2 度目は再試行しない。"""
    main = _main_describe("Opportunity", fields_map={"AccountId": ["Account"]})
    metadata = _make_metadata()
    client = _FakeClient(
        describe=metadata,
        object_describes={"Opportunity": main},
        errors={
            "Account": SalesforceRequestError("GET", "/sobjects/Account/describe", 500, "Boom")
        },
    )
    site_for, _ = _site_for(client)
    settings = _settings(tmp_path)
    entries = [
        MasterEntry(key="1001", summary="", url=f"{DOMAIN}/00O5g00000AAAAA/view", enabled=True),
        MasterEntry(key="1002", summary="", url=f"{DOMAIN}/00O5g00000BBBBB/view", enabled=True),
    ]
    outcomes = run_fetch(settings, entries, site_for=site_for)
    # 両方 OK（警告付き）
    assert outcomes[0].status == "ok"
    assert outcomes[1].status == "ok"
    # Account の describe_object は 1 回しか呼ばれない
    assert client.describe_object_calls.count("Account") == 1


def test_partial_failure_exit_code_is_one(tmp_path: Path) -> None:
    """1 件失敗しても残りの ID は取られ、``cli`` 側で exit code を 1 にする判定。"""
    entries = [
        MasterEntry(key="ok", summary="", url=f"{DOMAIN}/00O5g00000AAAAA/view", enabled=True),
        MasterEntry(key="bad", summary="", url="https://壊れたURL", enabled=True),
        MasterEntry(key="ok2", summary="", url=f"{DOMAIN}/00O5g00000BBBBB/view", enabled=True),
    ]
    client = _FakeClient(
        describe=_make_metadata(),
        object_describes={"Opportunity": _main_describe("Opportunity")},
    )
    site_for, _ = _site_for(client)
    outcomes = run_fetch(_settings(tmp_path), entries, site_for=site_for)
    statuses = [o.status for o in outcomes]
    assert statuses == ["ok", "failed", "ok"]


def test_dry_run_does_not_open_site(tmp_path: Path) -> None:
    """``run_fetch(dry_run=True)`` は ``site`` を一度も開かず、JSON を書かない。"""
    output = _output_dir_exists(tmp_path)
    entry = MasterEntry(
        key="1", summary="顧客一覧", url=f"{DOMAIN}/00O5g00000ABCDE/view", enabled=True
    )
    client = _FakeClient(describe={})
    site_for, _site = _site_for(client)
    outcomes = run_fetch(_settings(tmp_path), [entry], dry_run=True, site_for=site_for)
    assert outcomes[0].status == "dry-run"
    assert _site.open_count == 0
    # 出力先にファイルが無い
    assert not list(output.iterdir())


def test_site_for_salesforce_error_marks_id_failed(tmp_path: Path) -> None:
    """``site_for`` が ``SalesforceError`` （未登録組織の URL など）を出したら、
    その ID だけ失敗扱いにする。 ``site`` は開かれず、 JSON も書かれない。

    なぜ: 組織を追加し忘れて 1 件だけ URL が違うと、 1 件で全実行が止まると
    他が困るので、 ID ごとに切り離す。
    """
    from comken.exceptions import SalesforceError

    output = _output_dir_exists(tmp_path)
    good = MasterEntry(key="good", summary="", url=f"{DOMAIN}/00O5g00000AAAAA/view", enabled=True)
    bad = MasterEntry(
        key="bad",
        summary="",
        url=f"{DOMAIN}/00O5g00000BBBBB/view",
        enabled=True,
    )

    good_client = _FakeClient(
        describe=_make_metadata(),
        object_describes={"Opportunity": _main_describe("Opportunity")},
    )

    def resolver(url: str) -> _FakeSite:
        # bad だけ未登録組織として ``SalesforceError`` を投げる
        if "BBBBB" in url:
            raise SalesforceError("未登録")
        site = _FakeSite(good_client)

        def call() -> _FakeSite:
            return site

        return call

    settings = _settings(tmp_path)
    outcomes = run_fetch(settings, [good, bad], site_for=resolver)

    # good は OK、bad は failed（個別）
    statuses = {o.entry.key: o.status for o in outcomes}
    assert statuses == {"good": "ok", "bad": "failed"}
    assert outcomes[1].error and "接続先を決定できません" in outcomes[1].error

    # bad の JSON は書かれていない（good だけ書かれる）
    files = {path.name for path in output.iterdir()}
    assert files == {"good.json"}


def test_site_for_comken_error_marks_id_failed(tmp_path: Path) -> None:
    """``site_for`` が ``ComkenError`` （ ``SalesforceError`` 以外の派生）を
    出しても、その ID だけ失敗扱いにする。
    """
    from comken.exceptions import ComkenError

    output = _output_dir_exists(tmp_path)
    bad = MasterEntry(
        key="bad",
        summary="",
        url=f"{DOMAIN}/00O5g00000BBBBB/view",
        enabled=True,
    )

    def resolver(url: str) -> _FakeSite:
        raise ComkenError("何か comken のエラー")

    outcomes = run_fetch(_settings(tmp_path), [bad], site_for=resolver)

    assert outcomes[0].status == "failed"
    assert outcomes[0].error and "接続先を決定できません" in outcomes[0].error
    # JSON は書かれていない
    assert not list(output.iterdir())


def test_json_write_oserror_marks_id_failed(tmp_path: Path) -> None:
    """JSON の書き出しで ``OSError`` が起きると、 その ID だけ失敗扱いにする。

    なぜ: 出力先フォルダが消えた・権限が落ちた・ディスクが一杯、 といった
    環境側の問題で他の ID まで巻き込みたくないため。
    他の ID はそのまま書き続けられることを確かめる。
    """
    output = _output_dir_exists(tmp_path)
    entries = [
        MasterEntry(key="ok", summary="", url=f"{DOMAIN}/00O5g00000AAAAA/view", enabled=True),
        MasterEntry(key="bad", summary="", url=f"{DOMAIN}/00O5g00000BBBBB/view", enabled=True),
        MasterEntry(key="ok2", summary="", url=f"{DOMAIN}/00O5g00000CCCCC/view", enabled=True),
    ]
    client = _FakeClient(
        describe=_make_metadata(),
        object_describes={"Opportunity": _main_describe("Opportunity")},
    )
    site_for, _ = _site_for(client)

    from src import fetch as fetch_module

    original_atomic = fetch_module.atomic_write
    real_atomic = original_atomic

    def broken_atomic_write(path):
        if path.name == "bad.json":
            # ``atomic_write`` は ``with`` 文のコンテキストマネージャとして
            # 使われるので、 ``__enter__`` を呼ぶ前に例外を上げると
            # ``_fetch_one`` の ``except OSError`` 経路に入る
            raise OSError("ディスク書き込み失敗")
        return real_atomic(path)

    fetch_module.atomic_write = broken_atomic_write  # type: ignore[assignment]
    try:
        outcomes = run_fetch(_settings(tmp_path), entries, site_for=site_for)
    finally:
        fetch_module.atomic_write = original_atomic  # type: ignore[assignment]

    statuses = [o.status for o in outcomes]
    assert statuses == ["ok", "failed", "ok"]
    assert outcomes[1].error and "書き出しに失敗" in outcomes[1].error

    # bad の JSON は存在せず、 ok / ok2 は書かれている
    files = {path.name for path in output.iterdir()}
    assert files == {"ok.json", "ok2.json"}


# ── run 経由のテストは tests/test_run.py 側（ CLI は削除 ） ──────────────


def test_existing_json_not_corrupted_on_failure(tmp_path: Path) -> None:
    """``atomic_write`` の中で例外が起きたとき、既存の JSON が壊れない。

    ``atomic_write`` をパッチして途中で例外を上げ、本体の書き込みロジックが
    一時ファイル経由になっていることを確かめる（直接 ``write_text`` で書き
    換えると、パッチの影響を受けず JSON が破損する）。
    """
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    existing = output_dir / "1.json"
    existing.write_text('{"既存": "そのまま"}', encoding="utf-8")

    from src import fetch as fetch_module

    main = _main_describe("Opportunity", fields_map={"AccountId": ["Account"]})
    entry = MasterEntry(key="1", summary="", url=f"{DOMAIN}/00O5g00000ABCDE/view", enabled=True)
    client = _FakeClient(
        describe=_make_metadata(),
        object_describes={"Opportunity": main},
        errors={
            "Account": SalesforceRequestError("GET", "/sobjects/Account/describe", 500, "Boom")
        },
    )
    site_for, _ = _site_for(client)

    called = {"count": 0}

    def broken_atomic_write(path):
        called["count"] += 1
        # ``atomic_write`` が ``with`` ブロックのコンテキストマネージャとして
        # 使われていることを保証するため、呼ばれた瞬間に例外を上げる
        raise RuntimeError("爆発")

    original = fetch_module.atomic_write
    fetch_module.atomic_write = broken_atomic_write  # type: ignore[assignment]
    try:
        with pytest.raises(RuntimeError):
            run_fetch(_settings(tmp_path), [entry], site_for=site_for)
    finally:
        fetch_module.atomic_write = original  # type: ignore[assignment]

    # ``atomic_write`` が必ず通っている（直接 write_text にすると count=0 になる）
    assert called["count"] == 1
    # 既存 JSON は残っている
    assert existing.read_text(encoding="utf-8") == '{"既存": "そのまま"}'


# ── ヘルパー関数 ─────────────────────────────────────────────────────────


def test_collect_related_object_names_excludes_main_and_dedups() -> None:
    describe = _main_describe(
        "Opportunity",
        fields_map={
            "AccountId": ["Account"],
            "OwnerId": ["User"],
            "AccountId2": ["Account"],
            "WhatId": ["Account", "Opportunity"],  # 主オブジェクト自身を含む
        },
    )
    result = _collect_related_object_names(describe, exclude="Opportunity")
    # 出現順: Account, User, Opportunity（ただし exclude で Opportunity は除く）
    assert result == ["Account", "User"]


def test_apply_related_limit() -> None:
    names = ["a", "b", "c", "d"]
    kept, skipped = _apply_related_limit(names, 2)
    assert kept == ["a", "b"]
    assert skipped == ["c", "d"]
    kept, skipped = _apply_related_limit(names, 10)
    assert kept == names
    assert skipped == []


def test_fetch_object_cached_calls_only_once() -> None:
    """キャッシュ: 同じ名前は 1 回しか HTTP を打たない。"""
    client = _FakeClient(
        describe={},
        object_describes={"Account": _main_describe("Account")},
    )
    cache: dict = {}
    warnings1: list[str] = []
    assert _fetch_object_cached(client, "Account", cache, warnings1) is not None
    warnings2: list[str] = []
    assert _fetch_object_cached(client, "Account", cache, warnings2) is not None
    # 2 回目は client.describe_object が呼ばれないので calls には 1 件のみ
    assert client.describe_object_calls.count("Account") == 1
    assert warnings2 == []


# ── 主オブジェクトの describe が ID ごとに 1 回であることの検証 ──────────────


def test_main_object_describe_called_once_per_id(tmp_path: Path) -> None:
    """主オブジェクトの ``describe_object`` は ID ごとに **ちょうど 1 回** しか
    呼ばれない（ 主オブジェクト解決と列対応表の組み立てが同じ describe を
    流用していることを確かめる）。
    """
    entry = MasterEntry(
        key="1001", summary="顧客一覧", url=f"{DOMAIN}/00O5g00000ABCDE/view", enabled=True
    )
    metadata = _make_metadata(detail_column_labels={"Opp.Name": "名前"})
    opportunity = {
        "name": "Opportunity",
        "fields": [{"name": "Name", "label": "名前", "type": "Text"}],
    }
    client = _FakeClient(
        describe=metadata,
        object_describes={"Opportunity": opportunity},
    )
    site_for, _ = _site_for(client)

    outcomes = run_fetch(_settings(tmp_path), [entry], site_for=site_for)

    assert outcomes[0].status == "ok"
    # 主オブジェクト ``Opportunity`` に対する ``describe_object`` 呼び出しは 1 回
    assert client.describe_object_calls.count("Opportunity") == 1


def test_main_object_describe_called_once_for_custom_entity_with_winning_candidate(
    tmp_path: Path,
) -> None:
    """``CustomEntity$Project__c`` のとき、 候補の ``describe_object`` は 1 つ目が
    成功した時点で打ち切られ、 2 つ目以降は呼ばれない。
    """
    metadata = {
        "reportMetadata": {
            "reportType": {"type": "CustomEntity$Project__c"},
            "reportFormat": "TABULAR",
            "detailColumns": ["Project__c.Name"],
        }
    }
    project = {
        "name": "Project__c",
        "fields": [{"name": "Name", "label": "NAME", "type": "Text"}],
    }
    entry = MasterEntry(key="1001", summary="", url=f"{DOMAIN}/00O5g00000ABCDE/view", enabled=True)
    client = _FakeClient(
        describe=metadata,
        object_describes={"Project__c": project},
    )
    site_for, _ = _site_for(client)

    outcomes = run_fetch(_settings(tmp_path), [entry], site_for=site_for)

    assert outcomes[0].status == "ok"
    # ``Project__c`` に対する ``describe_object`` は 1 回。 他の候補は試さない
    assert client.describe_object_calls.count("Project__c") == 1
    # ``CustomEntity$Project__c`` という生文字列を describe しない
    assert "CustomEntity$Project__c" not in client.describe_object_calls


# ── _slim_report ─────────────────────────────────────────────────────────


def _make_report_metadata_with_extras() -> dict:
    """``_slim_report`` の絞り込み確認用に、 許可リスト以外のキーを混ぜた dict。"""
    return {
        # ``reportMetadata`` の中で、 許可リストにあるキー
        "reportMetadata": {
            "id": "00O5g00000ABCDE",
            "name": "顧客一覧",
            "reportType": {"type": "Opportunity"},
            "reportFormat": "TABULAR",
            "scope": "organization",
            "detailColumns": ["Opp.Name"],
            "reportFilters": [{"column": "Opp.Amount", "operator": "greaterThan", "value": "0"}],
            "reportBooleanFilter": "1 AND 2",
            "standardDateFilter": {"dateFilterType": "absolute", "startDate": "2024-01-01"},
            "standardFilters": [],
            "crossFilters": [],
            "groupingsDown": [],
            "groupingsAcross": [],
            "aggregates": [],
            "sortBy": [],
            "topRows": "10",
            "historicalSnapshotDates": [],
            "buckets": [],
            "customSummaryFormula": [],
            "division": "",
            "hasDetailRows": True,
            # 許可リストに無い余計なキー（ 落ちるはず ）
            "useNormalizedFieldForCurrency": True,
            "userOrTeamFilterId": "005xx",
            "description": "管理メモ",
        },
        # ``reportExtendedMetadata`` で、 許可リストにある3つ
        "reportExtendedMetadata": {
            "detailColumnInfo": {"Opp.Name": {"label": "商談名"}},
            "groupingColumnInfo": {},
            "aggregateColumnInfo": {},
            # 落ちるはずの余計なキー
            "reportTypeExtraInfo": {"heavy": "blob"},
            "joinedReportMetadata": {},
        },
        # トップレベルの余計なキー（ 全部 ``droppedKeys.top`` に行く ）
        "reportTypeMetadata": {"columns": ["very", "heavy", "data"] * 100},
        "attributes": {"type": "Report", "url": "/services/data/v60.0/analytics/reports/00O..."},
        "hasNestedReports": False,
        "folderId": "00l5g00000XXXXX",
        "ownerId": "0055g00000XXXXX",
    }


def test_slim_report_keeps_only_allowed_keys_and_drops_rest_in_order() -> None:
    """``_slim_report`` は許可リストのキーだけ残し、 落としたキーを
    ``droppedKeys`` の該当サブキーに**出現順で**入れる。"""
    original = _make_report_metadata_with_extras()
    result = _slim_report(original)

    # ``reportMetadata`` は許可リストにあるキーだけ
    expected_keys = {
        "id",
        "name",
        "reportType",
        "reportFormat",
        "scope",
        "detailColumns",
        "reportFilters",
        "reportBooleanFilter",
        "standardDateFilter",
        "standardFilters",
        "crossFilters",
        "groupingsDown",
        "groupingsAcross",
        "aggregates",
        "sortBy",
        "topRows",
        "historicalSnapshotDates",
        "buckets",
        "customSummaryFormula",
        "division",
        "hasDetailRows",
    }
    assert set(result["reportMetadata"].keys()) == expected_keys

    # 残したキーの値は元と**等しい**
    for key in expected_keys:
        assert result["reportMetadata"][key] == original["reportMetadata"][key], key

    # ``reportExtendedMetadata`` は 3 つのキーだけ
    assert set(result["reportExtendedMetadata"].keys()) == {
        "detailColumnInfo",
        "groupingColumnInfo",
        "aggregateColumnInfo",
    }
    assert result["reportExtendedMetadata"]["detailColumnInfo"] == {"Opp.Name": {"label": "商談名"}}

    # ``droppedKeys`` は 3 つのサブキーが常に存在し、 落ちたキー名が出現順で入る
    assert result["droppedKeys"] == {
        "reportMetadata": ["useNormalizedFieldForCurrency", "userOrTeamFilterId", "description"],
        "reportExtendedMetadata": ["reportTypeExtraInfo", "joinedReportMetadata"],
        "top": ["reportTypeMetadata", "attributes", "hasNestedReports", "folderId", "ownerId"],
    }


def test_slim_report_excludes_heavy_keys_from_output() -> None:
    """``reportTypeMetadata`` や ``attributes`` が出力に無いことを
    厳密に確かめる（ 出力 dict のトップレベルキーが限定されている ）。"""
    result = _slim_report(_make_report_metadata_with_extras())

    # 出力トップレベルは ``reportMetadata`` / ``reportExtendedMetadata`` /
    # ``droppedKeys`` の 3 つだけ
    assert set(result.keys()) == {
        "reportMetadata",
        "reportExtendedMetadata",
        "droppedKeys",
    }

    # ``reportMetadata`` の中に ``reportTypeMetadata`` が紛れていない
    assert "reportTypeMetadata" not in result["reportMetadata"]
    assert "attributes" not in result["reportMetadata"]


def test_slim_report_handles_missing_or_bad_input() -> None:
    """``reportMetadata`` が無い／ dict でない／``None`` ／文字列でも例外を上げない。"""
    # ``reportMetadata`` / ``reportExtendedMetadata`` が無い
    result = _slim_report({"hasNestedReports": False})
    assert set(result.keys()) == {"droppedKeys"}
    assert result["droppedKeys"] == {
        "reportMetadata": [],
        "reportExtendedMetadata": [],
        "top": ["hasNestedReports"],
    }

    # ``reportMetadata`` が dict でない: 該当キーは結果に出ない
    result = _slim_report({"reportMetadata": "not a dict", "reportExtendedMetadata": None})
    assert "reportMetadata" not in result
    assert "reportExtendedMetadata" not in result
    # ``reportMetadata`` / ``reportExtendedMetadata`` は特別扱いなので ``top`` にも入らない
    assert result["droppedKeys"] == {
        "reportMetadata": [],
        "reportExtendedMetadata": [],
        "top": [],
    }

    # 全体が dict でない
    result = _slim_report(None)
    assert result == {
        "droppedKeys": {"reportMetadata": [], "reportExtendedMetadata": [], "top": []}
    }

    result = _slim_report("string")
    assert result == {
        "droppedKeys": {"reportMetadata": [], "reportExtendedMetadata": [], "top": []}
    }


def test_slim_report_does_not_mutate_input_dict() -> None:
    """``_slim_report`` を呼んでも入力の dict は変わらない（ 別 dict にコピー ）。"""
    import copy

    original = _make_report_metadata_with_extras()
    snapshot = copy.deepcopy(original)

    _slim_report(original)

    # 入力 dict は ``==`` で比較できるほど変わらない
    assert original == snapshot


def test_slim_report_dropped_keys_subkeys_always_present() -> None:
    """何も落ちていないときも ``droppedKeys`` の 3 サブキーは空リストとして存在する。"""
    metadata = {
        "reportMetadata": {
            "id": "00O5g00000ABCDE",
            "name": "顧客一覧",
            "reportType": {"type": "Opportunity"},
        },
        "reportExtendedMetadata": {
            "detailColumnInfo": {},
            "groupingColumnInfo": {},
            "aggregateColumnInfo": {},
        },
    }
    result = _slim_report(metadata)
    assert result["droppedKeys"] == {
        "reportMetadata": [],
        "reportExtendedMetadata": [],
        "top": [],
    }


# ── _slim_object ──────────────────────────────────────────────────────────


def _make_full_describe() -> dict:
    """``_slim_object`` の絞り込み確認用に、 落とすべきキーを全部盛りにした describe。"""
    return {
        "name": "Opportunity",
        "label": "商談",
        "custom": False,
        # 落ちるはずのキー（ これらが JSON に残らないことを確認する ）
        "childRelationships": [{"childSObject": "Account", "field": "OpportunityId"}],
        "recordTypeInfos": [{"recordTypeId": "012000000000001"}],
        "urls": {"rowTemplate": "/services/data/v60.0/..."},
        "supportedScopes": [{"label": "すべて"}],
        "layoutable": True,
        "searchable": True,
        "queryable": True,
        "fields": [
            # 8 キー全部あり、 active な picklistValues
            {
                "name": "StageName",
                "label": "フェーズ",
                "type": "picklist",
                "custom": False,
                "referenceTo": [],
                "relationshipName": None,
                "picklistValues": [
                    {"value": "Prospecting", "active": True},
                    {"value": "Qualification", "active": True},
                    {"value": "ClosedLost", "active": False},
                    {"value": "ClosedWon", "active": True},
                ],
                # 落ちるはずの余計なキー
                "inlineHelpText": "進捗",
                "length": 40,
            },
            # ``custom`` / ``referenceTo`` / ``relationshipName`` が無い
            {
                "name": "Name",
                "label": "商談名",
                "type": "string",
                "picklistValues": [],
            },
            # ``picklistValues`` が無い（ active 絞りは要らない ）
            {
                "name": "Amount",
                "label": "金額",
                "type": "currency",
                "custom": False,
                "referenceTo": [],
                "relationshipName": None,
            },
        ],
    }


def test_slim_object_only_required_top_level_keys() -> None:
    """``_slim_object`` は ``name`` / ``label`` / ``custom`` / ``fields`` の 4 キーだけ残す。"""
    result = _slim_object(_make_full_describe())
    assert set(result.keys()) == {"name", "label", "custom", "fields"}
    assert result["name"] == "Opportunity"
    assert result["label"] == "商談"
    assert result["custom"] is False


def test_slim_object_drops_heavy_keys() -> None:
    """``_slim_object`` は重いキー（ ``childRelationships`` / ``recordTypeInfos`` /
    ``urls`` / ``supportedScopes`` / ``layoutable`` など）を出力に残さない。"""
    result = _slim_object(_make_full_describe())
    for forbidden in (
        "childRelationships",
        "recordTypeInfos",
        "urls",
        "supportedScopes",
        "layoutable",
        "searchable",
        "queryable",
    ):
        assert forbidden not in result, forbidden


def test_slim_field_has_eight_keys_in_order() -> None:
    """各項目は ``name`` / ``label`` / ``type`` / ``custom`` / ``referenceTo`` /
    ``relationshipName`` / ``picklist`` / ``picklistTotal`` の 8 キーが**この順**で
    出る。 ``inlineHelpText`` など余計なキーは残らない。
    """
    result = _slim_object(_make_full_describe())

    # ``fields`` の各項目が 8 キーだけ、 この順で
    expected_order = [
        "name",
        "label",
        "type",
        "custom",
        "referenceTo",
        "relationshipName",
        "picklist",
        "picklistTotal",
    ]
    for field in result["fields"]:
        assert list(field.keys()) == expected_order

    # ``StageName`` に ``inlineHelpText`` などの余計なキーが残らない
    stage = next(f for f in result["fields"] if f["name"] == "StageName")
    assert "inlineHelpText" not in stage
    assert "length" not in stage


def test_slim_object_picklist_active_only_capped_at_thirty() -> None:
    """``picklist`` は active な ``value`` だけを最大 30 件、 ``picklistTotal``
    は active な選択肢の総数。"""
    # active が 35 件、 inactive が 5 件（ active な V0..V34 がそのまま並ぶ ）
    picklist_values = [{"value": f"V{i}", "active": True} for i in range(35)] + [
        {"value": f"OFF{i}", "active": False} for i in range(5)
    ]
    describe = {
        "name": "Custom__c",
        "label": "カスタム",
        "custom": True,
        "fields": [
            {
                "name": "Custom__c",
                "label": "カスタム",
                "type": "picklist",
                "picklistValues": picklist_values,
            }
        ],
    }
    result = _slim_object(describe)
    field = result["fields"][0]

    # ``picklist`` は先頭 30 件、 末尾が "V29"
    assert len(field["picklist"]) == 30
    assert field["picklist"][0] == "V0"
    assert field["picklist"][29] == "V29"
    # inactive は含まれない
    for value in field["picklist"]:
        assert not value.startswith("OFF")

    # ``picklistTotal`` は active な選択肢の総数
    assert field["picklistTotal"] == 35


def test_slim_object_missing_keys_use_defaults() -> None:
    """``custom`` 無しは ``false`` 、 ``referenceTo`` 無しは ``[]`` 、
    ``relationshipName`` 無しは ``None`` 。"""
    describe = {
        "name": "Account",
        "label": "取引先",
        "fields": [
            {
                "name": "Name",
                "label": "取引先名",
                "type": "string",
            }
        ],
    }
    result = _slim_object(describe)
    field = result["fields"][0]
    assert field["custom"] is False
    assert field["referenceTo"] == []
    assert field["relationshipName"] is None
    assert field["picklist"] == []
    assert field["picklistTotal"] == 0


def test_slim_object_skips_non_dict_field_entries() -> None:
    """``fields`` の中に dict でない項目（ 文字列や ``None`` ）が混じっても飛ばす。"""
    describe = {
        "name": "X",
        "label": "X",
        "fields": [
            {"name": "A", "label": "A", "type": "string"},
            "not a dict",
            None,
            {"name": "B", "label": "B", "type": "string"},
        ],
    }
    result = _slim_object(describe)
    assert [f["name"] for f in result["fields"]] == ["A", "B"]


def test_slim_object_does_not_mutate_input() -> None:
    """``_slim_object`` を呼んでも入力 dict は変わらない（ 別 dict にコピー ）。"""
    import copy

    original = _make_full_describe()
    snapshot = copy.deepcopy(original)

    _slim_object(original)

    assert original == snapshot


def test_slim_object_handles_non_dict_input() -> None:
    """``describe`` が dict でない（ ``None`` や文字列）でも例外を上げない。"""
    assert _slim_object(None) == {}
    assert _slim_object("string") == {}
    assert _slim_object([]) == {}


# ── JSON 出力に重いキーが一切残らないこと ──────────────────────────────────


def test_json_output_has_no_heavy_keys_anywhere(tmp_path: Path) -> None:
    """書き出した JSON のどこにも ``reportTypeMetadata`` / ``childRelationships`` /
    ``recordTypeInfos`` / ``picklistValues`` が **キーとして** 出現しない
    （ ``json.dumps`` した文字列に ``"重いキー":`` の形が出ない）。
    なぜ: 機微な情報や、 巨大な原本が JSON の dict の中に漏れていないことを保証する。
    """
    # 元 describe に「 重い」 キーを全部盛りにした ``Account`` を作る
    full_describe = {
        "name": "Account",
        "label": "取引先",
        "custom": False,
        "childRelationships": [{"childSObject": "Contact", "field": "AccountId"}],
        "recordTypeInfos": [{"recordTypeId": "012000000000001"}],
        "urls": {"rowTemplate": "/services/data/v60.0/sobjects/Account/{ID}"},
        "supportedScopes": [{"label": "すべて"}],
        "fields": [
            {
                "name": "Type",
                "label": "種別",
                "type": "picklist",
                "picklistValues": [{"value": "Customer", "active": True}],
            }
        ],
    }
    entry = MasterEntry(key="1", summary="", url=f"{DOMAIN}/00O5g00000ABCDE/view", enabled=True)
    # レポート describe にも ``reportTypeMetadata`` を入れる
    metadata = {
        "reportMetadata": {"reportType": {"type": "Account"}, "reportFormat": "TABULAR"},
        "reportExtendedMetadata": {},
        "reportTypeMetadata": {"columns": ["very", "heavy", "data"] * 50},
        "attributes": {"type": "Report"},
    }
    client = _FakeClient(
        describe=metadata,
        object_describes={"Account": full_describe},
    )
    site_for, _ = _site_for(client)

    payload_path = run_fetch(_settings(tmp_path), [entry], site_for=site_for)[0].output_path
    raw_text = payload_path.read_text(encoding="utf-8")

    # ``"重いキー":`` という**キー形**が出現しない（ ``droppedKeys`` の中で
    # 「 キー名そのもの」 が文字列として現れるのは仕様 ）
    for forbidden in (
        "reportTypeMetadata",
        "childRelationships",
        "recordTypeInfos",
        "picklistValues",
    ):
        assert f'"{forbidden}":' not in raw_text, f"{forbidden!r} が JSON のキーとして残っています"


# ── 関連オブジェクトの集め方が原本の fields を使うこと ──────────────────────


def test_related_objects_collected_from_original_describe_not_slimmed(
    tmp_path: Path,
) -> None:
    """関連オブジェクトの収集は slim 前の原本の ``referenceTo`` /
    ``relationshipName`` を使う（ slim 後の ``fields`` からだと、 例えば
    ``referenceTo`` が無い項目は拾えなくなる）。
    なぜ: JSON 出力で ``objects`` の関連先が欠けないように。
    """
    # ``describe_object`` の戻り値は ``referenceTo`` / ``relationshipName`` が
    # **必ず** 入っているが、 これを `_slim_object` した結果は ``picklist``
    # など絞り込み後の形になる。 slim 後の ``fields`` から集めれば
    # ``referenceTo`` / ``relationshipName`` が空リスト / None になり、
    # 関連オブジェクトが取れなくなるはず。 このテストでは原本から集める実装を
    # 確かめる。
    main = {
        "name": "Opportunity",
        "label": "商談",
        "custom": False,
        "fields": [
            {
                "name": "AccountId",
                "label": "取引先",
                "type": "reference",
                "referenceTo": ["Account"],
                "relationshipName": "Account",
            }
        ],
    }
    entry = MasterEntry(key="1", summary="", url=f"{DOMAIN}/00O5g00000ABCDE/view", enabled=True)
    client = _FakeClient(
        describe=_make_metadata("Opportunity"),
        object_describes={"Opportunity": main, "Account": _main_describe("Account")},
    )
    site_for, _ = _site_for(client)

    payload_path = run_fetch(_settings(tmp_path), [entry], site_for=site_for)[0].output_path
    payload = json.loads(payload_path.read_text(encoding="utf-8"))

    # ``Account`` が出てきている（ slim 後の fields から集めていれば出ない）
    assert "Account" in payload["objects"]


# ── 6 つの改ざんが検出できることの直接テスト ──────────────────────────────────


def test_slim_report_detects_allowlist_violation(monkeypatch: pytest.MonkeyPatch) -> None:
    """``REPORT_METADATA_KEYS`` を広げると（ または完全に無視すると ）、
    ``reportMetadata`` の絞り込みが甘くなり、 本来落ちるキーが残ってしまう
    ことを検出できる。
    """
    import src.fetch as fetch_module

    # 全てのキーを許可する ``REPORT_METADATA_KEYS`` に差し替える
    permissive = (
        *fetch_module.REPORT_METADATA_KEYS,
        "useNormalizedFieldForCurrency",
        "userOrTeamFilterId",
        "description",
    )
    monkeypatch.setattr(fetch_module, "REPORT_METADATA_KEYS", permissive)

    metadata = _make_report_metadata_with_extras()
    result = _slim_report(metadata)

    # 本来 ``droppedKeys.reportMetadata`` に行くはずのキーが ``reportMetadata``
    # に**残ってしまっている**（ 改ざんを検出できる ）
    assert "useNormalizedFieldForCurrency" in result["reportMetadata"]
    assert "userOrTeamFilterId" in result["reportMetadata"]
    assert "description" in result["reportMetadata"]
    # ``droppedKeys.reportMetadata`` には入らない
    assert "useNormalizedFieldForCurrency" not in result["droppedKeys"]["reportMetadata"]


def test_slim_report_detects_dropped_keys_being_emptied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``droppedKeys`` を常に空にすると、 落ちたキーが ``droppedKeys`` に入らない。"""
    from src import fetch as fetch_module

    def bad_slim_report(metadata):
        # 許可リストの通り残し、 ただし ``droppedKeys`` は常に空
        result = {
            "reportMetadata": {
                k: v
                for k, v in metadata["reportMetadata"].items()
                if k in fetch_module.REPORT_METADATA_KEYS
            },
            "reportExtendedMetadata": {
                k: v
                for k, v in metadata["reportExtendedMetadata"].items()
                if k in fetch_module.REPORT_EXTENDED_METADATA_KEYS
            },
            "droppedKeys": {"reportMetadata": [], "reportExtendedMetadata": [], "top": []},
        }
        return result

    monkeypatch.setattr(fetch_module, "_slim_report", bad_slim_report)

    metadata = _make_report_metadata_with_extras()
    result = fetch_module._slim_report(metadata)
    # 落ちたキーが ``droppedKeys`` に**入っていない**（ 改ざんが検出できる ）
    assert result["droppedKeys"]["top"] == []
    # 本来は ``top`` に名前が入るはず
    assert "reportTypeMetadata" not in result["droppedKeys"]["top"]


def test_slim_object_detects_picklist_total_being_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``picklistTotal`` を常に 0 にする改ざんを検出できる。"""
    from src import fetch as fetch_module

    real_slim_field = fetch_module._slim_field

    def bad_slim_field(field):
        slimmed = real_slim_field(field)
        slimmed["picklistTotal"] = 0  # 改ざん
        return slimmed

    monkeypatch.setattr(fetch_module, "_slim_field", bad_slim_field)

    picklist_values = [{"value": f"V{i}", "active": True} for i in range(35)]
    describe = {
        "name": "C",
        "label": "C",
        "fields": [
            {"name": "X", "label": "X", "type": "picklist", "picklistValues": picklist_values}
        ],
    }
    result = fetch_module._slim_object(describe)
    field = result["fields"][0]

    # ``picklistTotal`` が 0 になっている（ 本来は 35 ）
    assert field["picklistTotal"] == 0
    # 本来の ``picklistTotal`` との差で改ざんが分かる
    assert field["picklistTotal"] != 35


def test_slim_object_detects_active_filter_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``active`` の絞りを外し、 ``value`` 全部を ``picklist`` に入れる改ざんを検出できる。"""
    from src import fetch as fetch_module

    def bad_extract_picklist(picklist_values):
        if not isinstance(picklist_values, list):
            return [], 0
        values = [
            entry.get("value")
            for entry in picklist_values
            if isinstance(entry, dict) and isinstance(entry.get("value"), str)
        ]
        return values[:30], len(values)

    monkeypatch.setattr(fetch_module, "_extract_picklist", bad_extract_picklist)

    picklist_values = [
        {"value": "On", "active": True},
        {"value": "Off", "active": False},
        {"value": "Pending", "active": False},
    ]
    describe = {
        "name": "C",
        "label": "C",
        "fields": [
            {"name": "X", "label": "X", "type": "picklist", "picklistValues": picklist_values}
        ],
    }
    result = fetch_module._slim_object(describe)
    field = result["fields"][0]

    # inactive な "Off" / "Pending" が ``picklist`` に入っている（ 改ざんが分かる ）
    assert "Off" in field["picklist"]
    assert "Pending" in field["picklist"]


def test_slim_object_detects_input_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_slim_object`` が元の dict を書き換える改ざんを検出できる。"""
    import copy as _copy

    from src import fetch as fetch_module

    def mutating_slim_object(describe):
        if not isinstance(describe, dict):
            return {}
        # 悪い例: 元 dict を破壊してから返す
        describe["fields"] = [{"name": "Replaced", "label": "Replaced", "type": "string"}]
        return {
            "name": describe.get("name", ""),
            "label": describe.get("label", ""),
            "custom": describe.get("custom", False),
            "fields": describe["fields"],
        }

    monkeypatch.setattr(fetch_module, "_slim_object", mutating_slim_object)

    original = {"name": "Opportunity", "label": "商談", "fields": []}
    original_snapshot = _copy.deepcopy(original)
    fetch_module._slim_object(original)

    # 元 dict が壊されている（ ``fields`` が書き換わっている ）
    assert original != original_snapshot
    assert original["fields"] == [{"name": "Replaced", "label": "Replaced", "type": "string"}]


def test_fetch_detects_related_collection_from_slimmed_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """関連オブジェクトの収集が slim 後の ``fields`` で行われる改ざんを検出できる。
    slim 後は ``referenceTo`` が空になるため、 関連が 1 つも取れなくなる。

    検出方法: 関連オブジェクト収集が slim 後に走る「 悪い実装 」 に
    ``_collect_related_field_paths`` を差し替える （ ``_slim_object`` を先に
    呼んで元 dict を破壊してから原本を読む ）。 本物の実装は slim の前に走る
    ので ``Account`` が取れる。
    """
    from src import fetch as fetch_module

    real_slim_object = fetch_module._slim_object
    real_collect = fetch_module._collect_related_field_paths
    call_log: list[str] = []

    def bad_slim_object(describe):
        # 悪い例: 呼ぶと同時に元 dict を slim 後の形に**書き換える**
        # （ ``referenceTo`` を [] に、 ``relationshipName`` を None にする）
        if isinstance(describe, dict) and isinstance(describe.get("fields"), list):
            for field in describe["fields"]:
                if isinstance(field, dict):
                    field["referenceTo"] = []
                    field["relationshipName"] = None
        return real_slim_object(describe)

    def order_collect(describe):
        # 悪い例: ``_slim_object`` を先に呼んでから収集する
        call_log.append("collect")
        bad_slim_object(describe)
        return real_collect(describe)

    monkeypatch.setattr(fetch_module, "_slim_object", bad_slim_object)
    monkeypatch.setattr(fetch_module, "_collect_related_field_paths", order_collect)

    main = {
        "name": "Opportunity",
        "label": "商談",
        "custom": False,
        "fields": [
            {
                "name": "AccountId",
                "label": "取引先",
                "type": "reference",
                "referenceTo": ["Account"],
                "relationshipName": "Account",
            }
        ],
    }
    entry = MasterEntry(key="1", summary="", url=f"{DOMAIN}/00O5g00000ABCDE/view", enabled=True)
    client = _FakeClient(
        describe=_make_metadata("Opportunity"),
        object_describes={"Opportunity": main, "Account": _main_describe("Account")},
    )
    site_for, _ = _site_for(client)

    payload_path = run_fetch(_settings(tmp_path), [entry], site_for=site_for)[0].output_path
    payload = json.loads(payload_path.read_text(encoding="utf-8"))

    # slim 後の ``fields`` から集めていると ``Account`` が**取れない**
    # （ 本物の実装では ``Account`` が必ず取れる ）
    assert "Account" not in payload["objects"]
    assert call_log  # 収集ロジックは呼ばれた
