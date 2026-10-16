"""``fetch`` フローのテスト（偽物の Salesforce クライアントで）。"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from comken.exceptions import SalesforceRequestError

from src.column_fix_xlsx import ColumnFixRow as ColumnMapRow
from src.describe_store import DescribeStore
from src.fetch import (
    _fetch_object_cached,
    _slim_object,
    _slim_report,
    run_fetch,
)
from src.master import MasterEntry

DOMAIN = "https://example--sandbox.sandbox.my.salesforce.com/lightning/r/Report"


# ── 偽物のクライアント ────────────────────────────────────────────────────


class _ReportStub:
    def __init__(
        self,
        describe: dict,
        *,
        get_table: object | None = None,
        get_error: BaseException | None = None,
    ) -> None:
        self._describe = describe
        self._get_table = get_table if get_table is not None else []
        self._get_error = get_error
        self.get_calls: list[str] = []

    def describe(self, report_id: str) -> dict:
        self.last_report_id = report_id
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
        errors: dict[str, BaseException] | None = None,
        *,
        get_table: object | None = None,
        get_error: BaseException | None = None,
        request_responses: list[tuple[str, tuple[object, dict[str, str]]]] | None = None,
        request_errors: list[tuple[str, BaseException]] | None = None,
    ) -> None:
        self._object_describes = object_describes or {}
        self._errors = errors or {}
        self.report = _ReportStub(describe, get_table=get_table, get_error=get_error)
        self.describe_object_calls: list[str] = []
        # ``client.request("GET", path, ...)`` を受け取るスタブ。 ``path``
        # 完全一致した順に返す。 ``request_errors`` も同じ ``path`` の
        # 順に対応。 どちらかが空なら ``(None, {})`` / 例外なしで動く
        self._request_responses = request_responses or []
        self._request_errors = request_errors or {}
        # 順序消費用のカーソル
        self._request_index = 0
        self.request_calls: list[tuple[str, str]] = []

    def data_path(self, path: str) -> str:
        # テストではバージョン前置換はせず、 パスをそのまま返す
        return path

    def request(
        self,
        method: str,
        path: str,
        body: object = None,
        component: str = "other",
        headers: dict[str, str] | None = None,
        data: str | None = None,
    ) -> tuple[object, dict[str, str]]:
        self.request_calls.append((method, path))
        if path in self._request_errors:
            raise self._request_errors[path]
        if self._request_index < len(self._request_responses):
            entry = self._request_responses[self._request_index]
            self._request_index += 1
            response_path, payload = entry
            assert response_path == path, f"path mismatch: expected {response_path!r}, got {path!r}"
            return payload
        return None, {}

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


def _settings(tmp_path: Path, related_max: int = 40, related_depth: int = 1) -> dict:
    """``run_fetch`` に渡す kwargs を返す。"""
    return {
        "related_max": related_max,
        "related_depth": related_depth,
        "row_limit": True,
        "pii_keywords": ("name", "電話"),
        "pii_person_objects": ("Contact",),
        "describe_csv_path": tmp_path / "describe.csv",
    }


def _output_dir_exists(tmp_path: Path) -> Path:
    """出力先ディレクトリが無ければ作って返す（``iterdir`` 用）。"""
    output = tmp_path / "output"
    output.mkdir(parents=True, exist_ok=True)
    return output


def _record_to_payload(record: object) -> dict:
    """``ReportRecord`` を旧 JSON と同じ形の dict に変換する互換ヘルパー。

    JSON 廃止前はテストが ``payload["..."]`` で読んでいたため、 既存テストの
    読み取り箇所を最小限の差分で揃えるために用意する。 ``取得日時`` は
    record には載せない （CSV では使わない、 JSON にも残さない）。
    """
    from src.fetch import ReportRecord

    assert isinstance(record, ReportRecord)
    return {
        "管理番号": record.key,
        "概要": record.summary,
        "レポートID": record.report_id,
        "URL": record.url,
        "主オブジェクト": record.main_object,
        "report": record.report,
        "objects": record.objects,
        "関連オブジェクト": [
            {
                "親": rel.parent,
                "参照項目": rel.field,
                "リレーション名": rel.relationship_name or "",
                "子": rel.child,
                "段": rel.depth,
            }
            for rel in record.relations
        ],
        "column_map": record.column_map,
        "warnings": list(record.warnings),
    }


def _make_metadata(
    report_type: str = "Opportunity",
    *,
    extra: dict | None = None,
    detail_column_labels: dict[str, str] | None = None,
) -> dict:
    """レポート describe を作る。

    ``detail_column_labels`` で ``detailColumns`` 各列の表示名を指定できる
    （``column_key`` → ``label``）。 未指定なら ``detailColumns[0]`` の
    表示名は同じキーになる（``column_key`` をそのまま ``label`` として扱う）。
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

    戻り値の ``relationshipName`` は ``<項目名>`` （単純化のため _ をつけない）。
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

    outcomes = run_fetch([entry], site_for=site_for, **_settings(tmp_path))

    assert outcomes[0].status == "ok"
    assert outcomes[0].record is not None
    payload = _record_to_payload(outcomes[0].record)
    assert payload["管理番号"] == "1001"
    assert payload["概要"] == "顧客一覧"
    assert payload["レポートID"] == "00O5g00000ABCDE"
    assert payload["URL"] == f"{DOMAIN}/00O5g00000ABCDE/view"
    assert payload["主オブジェクト"] == "Opportunity"
    # ``report`` は slim した_モジュール定数（値は元と同じ、 不要なキーは落ちる）。
    # allowlist のキーは全部残るので ``reportMetadata`` は等値になる
    assert payload["report"]["reportMetadata"] == metadata["reportMetadata"]
    # ``record.report`` には許可リスト外キーの追跡は含まれない （warnings に出る）
    assert set(payload["report"].keys()) == {"reportMetadata", "reportExtendedMetadata"}
    assert set(payload["objects"]) == {"Opportunity", "Account"}
    # ``objects`` の各値は ``_slim_object`` を通った形（name/label/custom/fields の 4 キー）
    expected_opportunity = _slim_object(opportunity_with_name)
    assert payload["objects"]["Opportunity"] == expected_opportunity
    assert payload["objects"]["Account"] == _slim_object(_main_describe("Account"))
    assert payload["column_map"] == [
        {
            "列キー": "Opp.Name",
            "表示名": "名前",
            "所属オブジェクト": "Opportunity",
            "対応フィールドAPI名": "Name",
            "型": "string",
            "参照先オブジェクト": "",
            "リレーション名": "",
            "備考": "",
        }
    ]
    assert payload["warnings"] == []


def test_objects_dict_is_slimmed_to_required_keys(tmp_path: Path) -> None:
    """``objects`` の中身は ``_slim_object`` を通した形（name/label/custom/fields の 4 キー）
    で書かれている。 JSON に重そうなキーは残らない（childRelationships / recordTypeInfos など）。
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
    record = run_fetch([entry], site_for=site_for, **_settings(tmp_path))[0].record
    payload = _record_to_payload(record)
    opp = payload["objects"]["Opportunity"]
    # オブジェクトは 4 キーだけ
    assert set(opp.keys()) == {"name", "label", "custom", "fields"}
    # 各項目は 9 キーだけ
    for field in opp["fields"]:
        assert set(field.keys()) == {
            "name",
            "label",
            "type",
            "custom",
            "referenceTo",
            "relationshipName",
            "nameField",
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
    """``_slim_object`` が走っても、 ``describe_object`` の戻り値（cache）は
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
    run_fetch([entry], site_for=site_for, **_settings(tmp_path))

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
    record = run_fetch([entry], site_for=site_for, **_settings(tmp_path))[0].record
    payload = _record_to_payload(record)
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
    record = run_fetch([entry], site_for=site_for, **_settings(tmp_path))[0].record
    payload = _record_to_payload(record)
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
    record = run_fetch([entry], site_for=site_for, **_settings(tmp_path, related_max=2))[0].record
    payload = _record_to_payload(record)
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
    record = run_fetch(
        [entry], site_for=site_for, **_settings(tmp_path, related_depth=related_depth)
    )[0].record
    return _record_to_payload(record)


def test_related_depth_1_only_first_level(tmp_path: Path) -> None:
    """``RELATED_DEPTH=1`` は 1 段目だけ取る （今と同じ挙動）。"""
    payload = _run_task_chain(tmp_path, related_depth=1)
    # 1 段目: Account のみ
    assert list(payload["objects"]) == ["Task", "Account"]
    assert payload["関連オブジェクト"] == [
        {
            "親": "Task",
            "参照項目": "WhatId",
            "リレーション名": "WhatId",
            "子": "Account",
            "段": 1,
        }
    ]


def test_related_depth_2_includes_second_level(tmp_path: Path) -> None:
    """``RELATED_DEPTH=2`` は 1 → 2 段目まで取る。 ``User`` まで取れる。"""
    payload = _run_task_chain(tmp_path, related_depth=2)
    assert list(payload["objects"]) == ["Task", "Account", "User"]
    assert payload["関連オブジェクト"] == [
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
    ]


def test_related_depth_3_includes_third_level(tmp_path: Path) -> None:
    """``RELATED_DEPTH=3`` は ``Group`` まで取る。"""
    payload = _run_task_chain(tmp_path, related_depth=3)
    assert list(payload["objects"]) == ["Task", "Account", "User", "Group"]
    assert payload["関連オブジェクト"][-1] == {
        "親": "User",
        "参照項目": "GroupId",
        "リレーション名": "GroupId",
        "子": "Group",
        "段": 3,
    }


def test_related_depth_dedup_across_paths(tmp_path: Path) -> None:
    """同じオブジェクトを複数の経路で参照しても describe は 1 回だけ、
    ただし ``関連オブジェクト`` には **取れたオブジェクト同士の参照を全部**
    載せるので、 ``Y`` への参照は 2 本 （``X`` からと ``Z`` から） 出る。

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
    record = run_fetch([entry], site_for=site_for, **_settings(tmp_path, related_depth=3))[0].record
    payload = _record_to_payload(record)

    # ``Y`` は 1 回しか describe されていない
    assert client.describe_object_calls.count("Y") == 1
    # JSON にも 1 回だけ
    assert list(payload["objects"]).count("Y") == 1
    # ``関連オブジェクト`` には ``Y`` への参照が 2 本
    # （X.AId→Y と Z.CId→Y） 載る
    y_entries = [r for r in payload["関連オブジェクト"] if r["子"] == "Y"]
    assert len(y_entries) == 2
    y_pairs = sorted((r["親"], r["参照項目"]) for r in y_entries)
    assert y_pairs == [("X", "AId"), ("Z", "CId")]
    # 両方の段は 1 （X / Z どちらも 1 段目で取れている）
    assert all(r["段"] == 1 for r in y_entries)


def test_related_depth_stops_at_cycle(tmp_path: Path) -> None:
    """循環参照（A→B→A） で止まる。 同じオブジェクトを取り直さない。"""
    objects = {
        "A": _describe_with_refs("A", {"BId": "B"}),
        "B": _describe_with_refs("B", {"AId": "A", "CId": "C"}),
        "C": _main_describe("C"),
    }
    entry = MasterEntry(key="1", summary="", url=f"{DOMAIN}/00O5g00000ABCDE/view", enabled=True)
    describe = {"reportMetadata": {"reportType": {"type": "A"}, "reportFormat": "TABULAR"}}
    client = _FakeClient(describe=describe, object_describes=objects)
    site_for, _ = _site_for(client)
    record = run_fetch([entry], site_for=site_for, **_settings(tmp_path, related_depth=5))[0].record
    payload = _record_to_payload(record)

    # オブジェクトは 3 つとも取れる （各段で重複はなし） 。
    assert set(payload["objects"]) == {"A", "B", "C"}
    # ``A`` の describe は 1 回のみ （2 段目で再訪しない）
    assert client.describe_object_calls.count("A") == 1
    # ``B`` は関連として 1 件だけ
    b_entries = [r for r in payload["関連オブジェクト"] if r["子"] == "B"]
    assert len(b_entries) == 1
    # ``C`` は 2 段目として取れる （さらに 3 段目で ``A`` を掘ろうとはしない）
    c_entries = [r for r in payload["関連オブジェクト"] if r["子"] == "C"]
    assert len(c_entries) == 1
    assert c_entries[0]["段"] == 2


def test_related_depth_max_limits_total_across_levels(tmp_path: Path) -> None:
    """``RELATED_MAX`` の合計件数で打ち切り、 近い段が優先される。"""
    # 1 段目: Account / Contact （2 件） 、 2 段目: User / Group （2 件） → 計 4 件
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
    record = run_fetch(
        [entry],
        site_for=site_for,
        **_settings(tmp_path, related_depth=3, related_max=3),
    )[0].record
    payload = _record_to_payload(record)

    # 近い段から 3 件: Account / Contact / User （Group は取らない）
    assert list(payload["objects"]) == ["Task", "Account", "Contact", "User"]
    # 関連オブジェクトの行数も 3 本 （Group への行は出ない）
    assert len(payload["関連オブジェクト"]) == 3
    children = [r["子"] for r in payload["関連オブジェクト"]]
    assert children == ["Account", "Contact", "User"]
    warning = next(w for w in payload["warnings"] if "上限" in w)
    # 全体件数（4）とスキップ件数（1）、 取らなかった名前が出る
    assert "4 件" in warning
    assert "Group" in warning


# ── 関連オブジェクト: 親→子の行の組み立て ────────────────────────────────


def test_related_objects_n_chain_has_one_row_per_edge(tmp_path: Path) -> None:
    """Task→Account→User の 3 段チェーンで、 ``関連オブジェクト`` には親→子の
    参照 3 本が並ぶ （JSON のリスト順は 浅い段 → 親の出現順）。
    """
    payload = _run_task_chain(tmp_path, related_depth=3)
    assert payload["関連オブジェクト"] == [
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
        {
            "親": "User",
            "参照項目": "GroupId",
            "リレーション名": "GroupId",
            "子": "Group",
            "段": 3,
        },
    ]


def test_related_objects_multiple_parents_to_same_child_yields_separate_rows(
    tmp_path: Path,
) -> None:
    """同じ子へ複数の親から行く場合は、 親ごとに 1 行ずつ別行にする。

    主 = Task （1 段目）。 Task の WhatId と WhoId の **2 つのフィールドが
    両方 ``User`` を指す**ようにすると、 「Task→User」 が 2 本出る。
    同じ段内で同じ親から複数のフィールドで同じ子を指している。
    """
    main = {
        "name": "Task",
        "fields": [
            {
                "name": "WhatId",
                "type": "reference",
                "referenceTo": ["User"],
                "relationshipName": "What",
            },
            {
                "name": "WhoId",
                "type": "reference",
                "referenceTo": ["User"],
                "relationshipName": "Who",
            },
        ],
    }
    objects = {
        "Task": main,
        "User": _main_describe("User"),
    }
    entry = MasterEntry(key="1", summary="", url=f"{DOMAIN}/00O5g00000ABCDE/view", enabled=True)
    describe = {"reportMetadata": {"reportType": {"type": "Task"}, "reportFormat": "TABULAR"}}
    client = _FakeClient(describe=describe, object_describes=objects)
    site_for, _ = _site_for(client)
    record = run_fetch(
        [entry], site_for=site_for, **_settings(tmp_path, related_depth=1, related_max=10)
    )[0].record
    payload = _record_to_payload(record)

    # 子 = User の行が全部出てくる
    user_rows = [r for r in payload["関連オブジェクト"] if r["子"] == "User"]
    assert user_rows == [
        {
            "親": "Task",
            "参照項目": "WhatId",
            "リレーション名": "What",
            "子": "User",
            "段": 1,
        },
        {
            "親": "Task",
            "参照項目": "WhoId",
            "リレーション名": "Who",
            "子": "User",
            "段": 1,
        },
    ]


def test_related_objects_polymorphic_reference_yields_separate_rows(
    tmp_path: Path,
) -> None:
    """ポリモーフィックな参照は ``referenceTo`` のエントリごとに 1 行ずつ。

    主 = Task。 Task.OwnerId は Who ロールで Contact / Lead / User への
    ポリモーフィック参照 （relationshipName = ``OwnerId``）、 すべて 1 段目
    に解決対象として並ぶ。 ここでは分かりやすくするため、 各子は
    ``referenceTo`` の最初の対象だけを ``relationshipName`` 付きで持つ形に
    する （Contact / Lead / User それぞれに対応）。
    """
    main = {
        "name": "Task",
        "fields": [
            {
                "name": "WhoId",
                "type": "reference",
                "referenceTo": ["Contact", "Lead"],
                "relationshipName": "Who",
            }
        ],
    }
    objects = {
        "Task": main,
        "Contact": _main_describe("Contact"),
        "Lead": _main_describe("Lead"),
    }
    entry = MasterEntry(key="1", summary="", url=f"{DOMAIN}/00O5g00000ABCDE/view", enabled=True)
    describe = {"reportMetadata": {"reportType": {"type": "Task"}, "reportFormat": "TABULAR"}}
    client = _FakeClient(describe=describe, object_describes=objects)
    site_for, _ = _site_for(client)
    record = run_fetch(
        [entry], site_for=site_for, **_settings(tmp_path, related_depth=1, related_max=10)
    )[0].record
    payload = _record_to_payload(record)

    assert payload["関連オブジェクト"] == [
        {
            "親": "Task",
            "参照項目": "WhoId",
            "リレーション名": "Who",
            "子": "Contact",
            "段": 1,
        },
        {
            "親": "Task",
            "参照項目": "WhoId",
            "リレーション名": "Who",
            "子": "Lead",
            "段": 1,
        },
    ]


def test_related_table_omits_child_dropped_by_limit(tmp_path: Path) -> None:
    """``RELATED_MAX`` で打ち切られた子は ``関連オブジェクト`` の行にも出ない。

    上限に達したあとに参照された子は describe を取らないので ``objects`` に
    入らず、 ``関連オブジェクト`` にも行が出ない。 警告文には名前が残る。
    """
    objects = {
        "Task": _describe_with_refs(
            "Task",
            {"WhatId": "Account", "WhoId": "Contact"},
        ),
        "Account": _main_describe("Account"),
        # Contact は ``RELATED_MAX=1`` のため skipped （HTTP も打たれない）
        "Contact": _main_describe("Contact"),
    }
    entry = MasterEntry(key="1", summary="", url=f"{DOMAIN}/00O5g00000ABCDE/view", enabled=True)
    describe = {"reportMetadata": {"reportType": {"type": "Task"}, "reportFormat": "TABULAR"}}
    client = _FakeClient(describe=describe, object_describes=objects)
    site_for, _ = _site_for(client)
    # RELATED_MAX=1 で Account だけ採用、 Contact は skipped。
    record = run_fetch(
        [entry], site_for=site_for, **_settings(tmp_path, related_depth=3, related_max=1)
    )[0].record
    payload = _record_to_payload(record)

    # 関連オブジェクトは Account の 1 本だけ
    assert payload["関連オブジェクト"] == [
        {
            "親": "Task",
            "参照項目": "WhatId",
            "リレーション名": "WhatId",
            "子": "Account",
            "段": 1,
        }
    ]
    # Contact は ``objects`` にも入らない
    assert "Contact" not in payload["objects"]
    # 警告に Contact が残る
    warning = next(w for w in payload["warnings"] if "上限" in w)
    assert "Contact" in warning
    # HTTP は 「Task の主オブジェクト + 上限内の Account」 の 2 件のみ
    assert client.describe_object_calls == ["Task", "Account"]


# ── B: skipped の重複排除 ──────────────────────────────────────────────


def test_collect_related_objects_skipped_dedup() -> None:
    """``related_max=1`` で同じ名前が複数回 skipped になっても、 重複を除いた
    出現順で 1 回しか残らない。 出現順も保たれる。
    """
    from src.fetch import _collect_related_objects

    main = _main_describe(
        "Task",
        fields_map={
            # Account が先頭で shallow に採用される。 残る User / Contact は skipped
            "AccountId": ["Account"],
            "AId": ["User"],
            "BId": ["User"],
            "CId": ["User"],
            "Did": ["Contact"],
        },
    )
    objects: dict[str, dict] = {
        "Task": main,
        "Account": _main_describe("Account"),
        "Contact": _main_describe("Contact"),
        "User": _main_describe("User"),
    }

    nodes, _, skipped = _collect_related_objects(
        main,
        lambda n: objects.get(n),
        related_depth=1,
        related_max=1,
    )

    # Account が 1 件採用され、 User / Contact が skipped
    assert [node.name for node in nodes] == ["Account"]
    # User / Contact は重複を除いて出現順を保つ
    assert skipped == ["User", "Contact"]


# ── 主＋関連オブジェクト同士の参照（主への参照・自己参照） ───────────────


def test_collect_related_objects_includes_main_and_self_references_in_relations() -> None:
    """再現例: 主=Task、 Account.OwnerId→User、 Account.ParentId→Account
    （自己参照）、 Account.TaskId→Task （主への参照）、 User.ManagerId→User
    （自己参照）。 ``nodes`` には主(Task)を含まないが、 ``relations`` には
    主への参照（段 0） と自己参照も全部載る。 行の段は **子の段** （主=0）。
    """
    from src.fetch import RelationRecord, _collect_related_objects

    D: dict[str, dict] = {
        "Task": _describe_with_refs("Task", {"WhatId": "Account", "OwnerId": "User"}),
        "Account": {
            "name": "Account",
            "fields": [
                {
                    "name": "OwnerId",
                    "type": "reference",
                    "referenceTo": ["User"],
                    "relationshipName": "Owner",
                },
                {
                    "name": "ParentId",
                    "type": "reference",
                    "referenceTo": ["Account"],
                    "relationshipName": "Parent",
                },
                {
                    "name": "TaskId",
                    "type": "reference",
                    "referenceTo": ["Task"],
                    "relationshipName": "T",
                },
            ],
        },
        "User": _describe_with_refs("User", {"ManagerId": "User"}),
    }

    nodes, relations, skipped = _collect_related_objects(
        D["Task"],
        lambda n: D.get(n),
        related_depth=3,
        related_max=40,
    )

    # ``nodes`` は主を含まない。 段は浅い順 （Account と User は 1 段目）
    assert [(node.name, node.depth) for node in nodes] == [("Account", 1), ("User", 1)]
    # ``relations`` は 6 行: 主→子、 Account→子、 User→子
    assert relations == [
        RelationRecord(
            parent="Task",
            field="WhatId",
            relationship_name="WhatId",
            child="Account",
            depth=1,
        ),
        RelationRecord(
            parent="Task",
            field="OwnerId",
            relationship_name="OwnerId",
            child="User",
            depth=1,
        ),
        RelationRecord(
            parent="Account",
            field="OwnerId",
            relationship_name="Owner",
            child="User",
            depth=1,
        ),
        RelationRecord(
            parent="Account",
            field="ParentId",
            relationship_name="Parent",
            child="Account",
            depth=1,
        ),
        RelationRecord(
            parent="Account",
            field="TaskId",
            relationship_name="T",
            child="Task",
            depth=0,
        ),
        RelationRecord(
            parent="User",
            field="ManagerId",
            relationship_name="ManagerId",
            child="User",
            depth=1,
        ),
    ]
    assert skipped == []


def test_collect_related_objects_skipped_child_excluded_from_relations() -> None:
    """``related_max=1`` で Account だけ取れて User は ``skipped`` 。
    User を含む行は出ない（User は取れていないため、 親にも子にもならない）。
    Account 側の ``User`` への行も User が取れていないので出ない。
    """
    from src.fetch import _collect_related_objects

    D: dict[str, dict] = {
        "Task": _describe_with_refs("Task", {"WhatId": "Account", "OwnerId": "User"}),
        "Account": _describe_with_refs("Account", {"OwnerId": "User"}),
        "User": _describe_with_refs("User", {"ManagerId": "User"}),
    }

    nodes, relations, skipped = _collect_related_objects(
        D["Task"],
        lambda n: D.get(n),
        related_depth=3,
        related_max=1,
    )

    # ``nodes`` は Account だけ
    assert [node.name for node in nodes] == ["Account"]
    # User を含む行は出ない
    assert all(r.child != "User" for r in relations)
    # Account は出ている （Task→Account 1 行だけ）
    account_rows = [r for r in relations if r.child == "Account"]
    assert len(account_rows) == 1
    assert account_rows[0].parent == "Task"
    assert account_rows[0].field == "WhatId"
    # User は skipped
    assert "User" in skipped


def test_run_fetch_emits_main_and_self_references_in_related_objects(
    tmp_path: Path,
) -> None:
    """``run_fetch`` の JSON 出力 ``関連オブジェクト`` に、 主＋関連オブジェクト
    同士の参照が全部載ることを end-to-end で確かめる。 上の再現例と
    同じフェイクを使う。
    """
    main = {
        "name": "Task",
        "fields": [
            {
                "name": "WhatId",
                "type": "reference",
                "referenceTo": ["Account"],
                "relationshipName": "What",
            },
            {
                "name": "OwnerId",
                "type": "reference",
                "referenceTo": ["User"],
                "relationshipName": "Owner",
            },
        ],
    }
    objects = {
        "Task": main,
        "Account": {
            "name": "Account",
            "fields": [
                {
                    "name": "OwnerId",
                    "type": "reference",
                    "referenceTo": ["User"],
                    "relationshipName": "Owner",
                },
                {
                    "name": "ParentId",
                    "type": "reference",
                    "referenceTo": ["Account"],
                    "relationshipName": "Parent",
                },
                {
                    "name": "TaskId",
                    "type": "reference",
                    "referenceTo": ["Task"],
                    "relationshipName": "T",
                },
            ],
        },
        "User": {
            "name": "User",
            "fields": [
                {
                    "name": "ManagerId",
                    "type": "reference",
                    "referenceTo": ["User"],
                    "relationshipName": "Manager",
                }
            ],
        },
    }
    entry = MasterEntry(key="1", summary="", url=f"{DOMAIN}/00O5g00000ABCDE/view", enabled=True)
    describe = {"reportMetadata": {"reportType": {"type": "Task"}, "reportFormat": "TABULAR"}}
    client = _FakeClient(describe=describe, object_describes=objects)
    site_for, _ = _site_for(client)
    record = run_fetch(
        [entry], site_for=site_for, **_settings(tmp_path, related_depth=3, related_max=40)
    )[0].record
    payload = _record_to_payload(record)

    # ``objects`` には主 + 取れた関連だけ。 順序: Task → Account → User
    assert list(payload["objects"]) == ["Task", "Account", "User"]
    # ``関連オブジェクト`` 6 行、 並び固定
    assert payload["関連オブジェクト"] == [
        {"親": "Task", "参照項目": "WhatId", "リレーション名": "What", "子": "Account", "段": 1},
        {"親": "Task", "参照項目": "OwnerId", "リレーション名": "Owner", "子": "User", "段": 1},
        {"親": "Account", "参照項目": "OwnerId", "リレーション名": "Owner", "子": "User", "段": 1},
        {
            "親": "Account",
            "参照項目": "ParentId",
            "リレーション名": "Parent",
            "子": "Account",
            "段": 1,
        },
        {"親": "Account", "参照項目": "TaskId", "リレーション名": "T", "子": "Task", "段": 0},
        {"親": "User", "参照項目": "ManagerId", "リレーション名": "Manager", "子": "User", "段": 1},
    ]


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
    record = run_fetch([entry], site_for=site_for, **_settings(tmp_path))[0].record
    payload = _record_to_payload(record)

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
    関連は取らない。

    列キー先頭のフォールバック （``Opp``） も試すようにしたので、
    ``Opp`` も同じエラーにして 両方が全滅するシナリオを確かめる
    """
    entry = MasterEntry(key="1", summary="", url=f"{DOMAIN}/00O5g00000ABCDE/view", enabled=True)
    client = _FakeClient(
        describe=_make_metadata("Opportunity"),
        errors={
            "Opportunity": SalesforceRequestError(
                "GET", "/sobjects/Opportunity/describe", 400, "Bad"
            ),
            "Opp": SalesforceRequestError("GET", "/sobjects/Opp/describe", 400, "Bad"),
        },
    )
    site_for, _ = _site_for(client)
    record = run_fetch([entry], site_for=site_for, **_settings(tmp_path))[0].record
    payload = _record_to_payload(record)
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
    outcomes = run_fetch([entry], site_for=site_for, **_settings(tmp_path))
    assert outcomes[0].status == "failed"
    assert "401" in (outcomes[0].error or "")
    assert outcomes[0].record is None
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
    outcomes = run_fetch([entry], site_for=site_for, **_settings(tmp_path))
    assert outcomes[0].status == "failed"
    assert "403" in (outcomes[0].error or "")
    # 主オブジェクトは書かれる前なので JSON 無し
    assert not list(output.iterdir())


def test_bad_url_marks_id_failed(tmp_path: Path) -> None:
    """URL が壊れている行は ``fetch`` 段で失敗として報告。"""
    entry = MasterEntry(key="1", summary="", url="https://壊れたURL", enabled=True)
    site_for, _ = _site_for(_FakeClient(describe={}))
    outcomes = run_fetch([entry], site_for=site_for, **_settings(tmp_path))
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
    entries = [
        MasterEntry(key="1001", summary="", url=f"{DOMAIN}/00O5g00000AAAAA/view", enabled=True),
        MasterEntry(key="1002", summary="", url=f"{DOMAIN}/00O5g00000BBBBB/view", enabled=True),
    ]
    run_fetch(entries, site_for=site_for, **_settings(tmp_path))
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
    entries = [
        MasterEntry(key="1001", summary="", url=f"{DOMAIN}/00O5g00000AAAAA/view", enabled=True),
        MasterEntry(key="1002", summary="", url=f"{DOMAIN}/00O5g00000BBBBB/view", enabled=True),
    ]
    outcomes = run_fetch(entries, site_for=site_for, **_settings(tmp_path))
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
    outcomes = run_fetch(entries, site_for=site_for, **_settings(tmp_path))
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
    outcomes = run_fetch([entry], dry_run=True, site_for=site_for, **_settings(tmp_path))
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

    outcomes = run_fetch([good, bad], site_for=resolver, **_settings(tmp_path))

    # good は OK、bad は failed（個別）
    statuses = {o.entry.key: o.status for o in outcomes}
    assert statuses == {"good": "ok", "bad": "failed"}
    assert outcomes[1].error and "接続先を決定できません" in outcomes[1].error

    # bad は失敗で record 無し、 good は record あり。 ファイルはどれも書かれない
    assert outcomes[0].record is not None
    assert outcomes[1].record is None
    files = {path.name for path in output.iterdir()}
    assert files == set()


def test_site_for_comken_error_marks_id_failed(tmp_path: Path) -> None:
    """``site_for`` が ``ComkenError`` （``SalesforceError`` 以外の派生）を
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

    outcomes = run_fetch([bad], site_for=resolver, **_settings(tmp_path))

    assert outcomes[0].status == "failed"
    assert outcomes[0].error and "接続先を決定できません" in outcomes[0].error
    # JSON は書かれていない
    assert not list(output.iterdir())


def test_run_fetch_does_not_write_any_files(tmp_path: Path) -> None:
    """``run_fetch`` は JSON を含めて **何もファイルを書き出さない** 。
    出力は ``Record`` だけ。 結果は ``outcomes[i].record`` で取り出す。
    """
    output = tmp_path / "output"
    output.mkdir()
    entry = MasterEntry(key="ok", summary="", url=f"{DOMAIN}/00O5g00000ABCDE/view", enabled=True)
    client = _FakeClient(
        describe=_make_metadata(),
        object_describes={"Opportunity": _main_describe("Opportunity")},
    )
    site_for, _ = _site_for(client)

    outcomes = run_fetch([entry], site_for=site_for, **_settings(tmp_path))

    assert outcomes[0].status == "ok"
    assert outcomes[0].record is not None
    # ファイルは何も書かれていない
    assert not list(output.iterdir())


# ── run 経由のテストは tests/test_run.py 側（CLI は削除） ──────────────


def test_run_fetch_does_not_use_atomic_write(tmp_path: Path) -> None:
    """``run_fetch`` は ``atomic_write`` を import すらしていない。
    結果を ``ReportRecord`` として返すだけで、 ファイル I/O は一切しない。
    """
    from src import fetch as fetch_module

    # ``atomic_write`` が ``src.fetch`` に存在しないことを直接確かめる
    assert not hasattr(fetch_module, "atomic_write"), (
        "fetch は JSON を書かないので atomic_write を import しないこと"
    )

    entry = MasterEntry(key="1", summary="", url=f"{DOMAIN}/00O5g00000ABCDE/view", enabled=True)
    client = _FakeClient(
        describe=_make_metadata(),
        object_describes={"Opportunity": _main_describe("Opportunity")},
    )
    site_for, _ = _site_for(client)

    outcomes = run_fetch([entry], site_for=site_for, **_settings(tmp_path))
    assert outcomes[0].status == "ok"
    assert outcomes[0].record is not None
    # 出力先ディレクトリも空のまま （``run_fetch`` はフォルダも作らない）
    assert not list((tmp_path / "output").iterdir()) if (tmp_path / "output").exists() else True


# ── ヘルパー関数 ─────────────────────────────────────────────────────────


def test_fetch_object_cached_calls_only_once(tmp_path: Path) -> None:
    """キャッシュ: 同じ ``(org, object)`` は 1 回しか HTTP を打たない。"""
    client = _FakeClient(
        describe={},
        object_describes={"Account": _main_describe("Account")},
    )
    from src.describe_store import DescribeStore

    store = DescribeStore.empty(tmp_path / "describe.csv")
    org = "example.my.salesforce.com"
    warnings1: list[str] = []
    assert _fetch_object_cached(client, org, "Account", store, warnings1) is not None
    warnings2: list[str] = []
    assert _fetch_object_cached(client, org, "Account", store, warnings2) is not None
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

    outcomes = run_fetch([entry], site_for=site_for, **_settings(tmp_path))

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

    outcomes = run_fetch([entry], site_for=site_for, **_settings(tmp_path))

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
            "topRows": {"rowLimit": 10, "direction": "Desc"},
            "historicalSnapshotDates": [],
            "buckets": [],
            "customSummaryFormula": [],
            "division": "",
            "hasDetailRows": True,
            # 許可リストに無い余計なキー（落ちるはず）
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
        # トップレベルの余計なキー（全部 ``_collect_dropped_keys()`` に出る）
        "reportTypeMetadata": {"columns": ["very", "heavy", "data"] * 100},
        "attributes": {"type": "Report", "url": "/services/data/v60.0/analytics/reports/00O..."},
        "hasNestedReports": False,
        "folderId": "00l5g00000XXXXX",
        "ownerId": "0055g00000XXXXX",
    }


def test_slim_report_keeps_only_allowed_keys() -> None:
    """_slim_report は許可リストのキーだけ残し、 それ以外を落とす。"""
    original = _make_report_metadata_with_extras()
    result = _slim_report(original)

    # reportMetadata は許可リストにあるキーだけ
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

    # reportExtendedMetadata は 3 つのキーだけ
    assert set(result["reportExtendedMetadata"].keys()) == {
        "detailColumnInfo",
        "groupingColumnInfo",
        "aggregateColumnInfo",
    }
    assert result["reportExtendedMetadata"]["detailColumnInfo"] == {"Opp.Name": {"label": "商談名"}}

    # 落としたキー名は result に含めない （warnings 側で別管理）
    assert set(result.keys()) == {"reportMetadata", "reportExtendedMetadata"}


def test_slim_report_excludes_heavy_keys_from_output() -> None:
    """reportTypeMetadata や attributes が出力に無いことを
    厳密に確かめる（出力 dict のトップレベルキーが限定されている）。"""
    result = _slim_report(_make_report_metadata_with_extras())

    # 出力トップレベルは reportMetadata / reportExtendedMetadata の 2 つだけ
    assert set(result.keys()) == {
        "reportMetadata",
        "reportExtendedMetadata",
    }

    # reportMetadata の中に reportTypeMetadata が紛れていない
    assert "reportTypeMetadata" not in result["reportMetadata"]
    assert "attributes" not in result["reportMetadata"]


def test_slim_report_handles_missing_or_bad_input() -> None:
    """reportMetadata が無い／ dict でない／None ／文字列でも例外を上げず、
    内側が検証せずに読めるよう、 必ず 2 つの空の dict を持つ形で返す。"""
    empty = {"reportMetadata": {}, "reportExtendedMetadata": {}}
    # reportMetadata / reportExtendedMetadata が無い
    assert _slim_report({"hasNestedReports": False}) == empty

    # reportMetadata が dict でない
    assert _slim_report({"reportMetadata": "not a dict", "reportExtendedMetadata": None}) == empty

    # 全体が dict でない
    assert _slim_report(None) == empty
    assert _slim_report("string") == empty


def test_slim_report_normalizes_scalars_and_top_rows() -> None:
    """単純な値は str に、 上位N の件数は正の整数か None にそろえる（境界）。"""
    result = _slim_report(
        {
            "reportMetadata": {
                "reportFormat": None,
                "scope": 3,
                "reportBooleanFilter": "1 AND 2",
                "topRows": {"rowLimit": "10", "direction": "Desc"},
            }
        }
    )
    metadata = result["reportMetadata"]
    assert metadata["reportFormat"] == ""
    assert metadata["scope"] == ""
    assert metadata["reportBooleanFilter"] == "1 AND 2"
    assert metadata["topRows"] == {"rowLimit": 10, "direction": "Desc"}
    for bad in (True, 0, -1, "abc", None):
        top_rows = _slim_report({"reportMetadata": {"topRows": {"rowLimit": bad}}})
        assert top_rows["reportMetadata"]["topRows"]["rowLimit"] is None, bad


def test_slim_report_normalizes_list_items() -> None:
    """リストとして読むキーの中身の型をそろえる（境界）。 そろっていれば元の参照のまま。"""
    detail_columns = ["A", "B"]
    result = _slim_report(
        {
            "reportMetadata": {
                "detailColumns": detail_columns,
                "aggregates": ["s!Amount", None, "", 3],
                "reportFilters": [{"column": "A"}, "bad", None],
                "groupingsDown": "not a list",
                "reportType": "Opportunity",
            },
            "reportExtendedMetadata": {
                "detailColumnInfo": {"A": {"label": "A"}, "B": "bad"},
                "groupingColumnInfo": "bad",
            },
        }
    )
    metadata = result["reportMetadata"]
    assert metadata["detailColumns"] is detail_columns
    assert metadata["aggregates"] == ["s!Amount"]
    # 絞り込みは位置を保つ（組立式の番号と対応しているため）
    assert metadata["reportFilters"] == [{"column": "A"}, {}, {}]
    assert metadata["groupingsDown"] == []
    assert metadata["reportType"] is None
    assert result["reportExtendedMetadata"] == {"detailColumnInfo": {"A": {"label": "A"}}}


def test_slim_report_does_not_mutate_input_dict() -> None:
    """_slim_report を呼んでも入力の dict は変わらない（別 dict にコピー）。"""
    import copy

    original = _make_report_metadata_with_extras()
    snapshot = copy.deepcopy(original)

    _slim_report(original)

    # 入力 dict は == で比較できるほど変わらない
    assert original == snapshot


def test_collect_dropped_keys_lists_in_order() -> None:
    """_collect_dropped_keys() は落としたキー名を出現順で返す。"""
    from src.fetch import _collect_dropped_keys

    original = _make_report_metadata_with_extras()
    dropped = _collect_dropped_keys(original)

    # 3 箇所から出現順で並ぶ
    assert dropped == (
        "useNormalizedFieldForCurrency",
        "userOrTeamFilterId",
        "description",  # reportMetadata 内
        "reportTypeExtraInfo",
        "joinedReportMetadata",  # reportExtendedMetadata 内
        "reportTypeMetadata",
        "attributes",
        "hasNestedReports",
        "folderId",
        "ownerId",  # トップレベル
    )


def test_collect_dropped_keys_handles_missing_or_bad_input() -> None:
    """_collect_dropped_keys() は例外を上げず、 空タプルを返す。"""
    from src.fetch import _collect_dropped_keys

    # 何も落ちていない
    assert _collect_dropped_keys({"reportMetadata": {"id": "x"}}) == ()
    # トップレベルだけ
    assert _collect_dropped_keys({"topOnly": 1}) == ("topOnly",)
    # reportMetadata が dict でない
    assert _collect_dropped_keys({"reportMetadata": "x"}) == ()
    # 入力が dict でない
    assert _collect_dropped_keys(None) == ()
    assert _collect_dropped_keys("string") == ()


def test_collect_dropped_keys_empty_when_all_allowed() -> None:
    """何も落ちていないときは空タプル。"""
    from src.fetch import _collect_dropped_keys

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
    assert _collect_dropped_keys(metadata) == ()


# ── standardDateFilter の 「実質指定なし」 落とし ──────────────────────


def test_is_undated_filter_recognizes_empty_specs() -> None:
    """「実質指定なし」 の判定: startDate / endDate が空、 durationValue が空か CUSTOM で True。"""
    from src.fetch import _is_undated_filter

    # startDate / endDate が None
    assert (
        _is_undated_filter(
            {
                "column": "CLOSE_DATE",
                "durationValue": "CUSTOM",
                "startDate": None,
                "endDate": None,
            }
        )
        is True
    )
    # startDate / endDate が空文字
    assert (
        _is_undated_filter(
            {
                "column": "CLOSE_DATE",
                "durationValue": "CUSTOM",
                "startDate": "",
                "endDate": "",
            }
        )
        is True
    )
    # durationValue が空（空の column だけ）
    assert _is_undated_filter({"column": "CLOSE_DATE", "startDate": "", "endDate": ""}) is True
    # 空白のみ
    assert (
        _is_undated_filter(
            {
                "column": "CLOSE_DATE",
                "durationValue": "  ",
                "startDate": "  ",
                "endDate": "  ",
            }
        )
        is True
    )
    # dict で無い → 「無かった」 ものとして True
    assert _is_undated_filter(None) is True
    assert _is_undated_filter("string") is True
    assert _is_undated_filter([]) is True


def test_is_undated_filter_keeps_relative_duration() -> None:
    """相対期間 （THIS_YEAR / LAST_N_DAYS:30 等） は 「指定あり」 なので False。"""
    from src.fetch import _is_undated_filter

    assert (
        _is_undated_filter(
            {
                "column": "CLOSE_DATE",
                "durationValue": "THIS_YEAR",
                "startDate": None,
                "endDate": None,
            }
        )
        is False
    )
    assert (
        _is_undated_filter(
            {
                "column": "CLOSE_DATE",
                "durationValue": "LAST_N_DAYS:30",
                "startDate": None,
                "endDate": None,
            }
        )
        is False
    )
    # durationValue の前後空白は無視
    assert (
        _is_undated_filter(
            {
                "column": "CLOSE_DATE",
                "durationValue": "  THIS_YEAR  ",
                "startDate": None,
                "endDate": None,
            }
        )
        is False
    )


def test_is_undated_filter_keeps_when_one_side_present() -> None:
    """開始 or 終了のどちらかが入っていれば 「指定あり」 で False。"""
    from src.fetch import _is_undated_filter

    assert (
        _is_undated_filter(
            {
                "column": "CLOSE_DATE",
                "durationValue": "CUSTOM",
                "startDate": "2025-01-01",
                "endDate": None,
            }
        )
        is False
    )
    assert (
        _is_undated_filter(
            {
                "column": "CLOSE_DATE",
                "durationValue": "CUSTOM",
                "startDate": None,
                "endDate": "2025-12-31",
            }
        )
        is False
    )


def test_slim_report_drops_undated_standard_date_filter() -> None:
    """「実質指定なし」 の ``standardDateFilter`` は ``_slim_report`` で落ちる。

    実物の describe 戻り値と同じ形 （``startDate`` / ``endDate`` が ``None``） で
    確認する
    """
    raw_metadata = {
        "reportMetadata": {
            "id": "00O5g00000ABCDE",
            "standardDateFilter": {
                "column": "CLOSE_DATE",
                "durationValue": "CUSTOM",
                "startDate": None,
                "endDate": None,
            },
        },
        "reportExtendedMetadata": {},
    }
    result = _slim_report(raw_metadata)
    assert "standardDateFilter" not in result["reportMetadata"]


def test_slim_report_keeps_relative_standard_date_filter() -> None:
    """相対期間 （THIS_YEAR） の ``standardDateFilter`` は残る。"""
    raw_metadata = {
        "reportMetadata": {
            "id": "00O5g00000ABCDE",
            "standardDateFilter": {
                "column": "CLOSE_DATE",
                "durationValue": "THIS_YEAR",
                "startDate": None,
                "endDate": None,
            },
        },
        "reportExtendedMetadata": {},
    }
    result = _slim_report(raw_metadata)
    assert "standardDateFilter" in result["reportMetadata"]
    assert result["reportMetadata"]["standardDateFilter"]["durationValue"] == "THIS_YEAR"


def test_slim_report_keeps_one_sided_standard_date_filter() -> None:
    """startDate / endDate のどちらかが入っていれば残る。"""
    start_only = {
        "reportMetadata": {
            "id": "00O5g00000ABCDE",
            "standardDateFilter": {
                "column": "CLOSE_DATE",
                "durationValue": "CUSTOM",
                "startDate": "2025-01-01",
                "endDate": None,
            },
        },
        "reportExtendedMetadata": {},
    }
    end_only = {
        "reportMetadata": {
            "id": "00O5g00000ABCDE",
            "standardDateFilter": {
                "column": "CLOSE_DATE",
                "durationValue": "CUSTOM",
                "startDate": None,
                "endDate": "2025-12-31",
            },
        },
        "reportExtendedMetadata": {},
    }
    assert "standardDateFilter" in _slim_report(start_only)["reportMetadata"]
    assert "standardDateFilter" in _slim_report(end_only)["reportMetadata"]


def test_slim_report_drops_when_only_column_present() -> None:
    """``column`` だけ入って他が全部空 （典型的には UI の 「クリア」 跡） も「実質指定なし」 として落ちる。"""
    raw_metadata = {
        "reportMetadata": {
            "id": "00O5g00000ABCDE",
            "standardDateFilter": {
                "column": "CLOSE_DATE",
                "durationValue": "",
                "startDate": "",
                "endDate": "",
            },
        },
        "reportExtendedMetadata": {},
    }
    result = _slim_report(raw_metadata)
    assert "standardDateFilter" not in result["reportMetadata"]


# ── _slim_object ──────────────────────────────────────────────────────────


def _make_full_describe() -> dict:
    """``_slim_object`` の絞り込み確認用に、 落とすべきキーを全部盛りにした describe。"""
    return {
        "name": "Opportunity",
        "label": "商談",
        "custom": False,
        # 落ちるはずのキー（これらが JSON に残らないことを確認する）
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
            # ``picklistValues`` が無い（active 絞りは要らない）
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


def test_slim_field_has_nine_keys_in_order() -> None:
    """各項目は ``name`` / ``label`` / ``type`` / ``custom`` / ``referenceTo`` /
    ``relationshipName`` / ``nameField`` / ``picklist`` / ``picklistTotal`` の 9 キーが
    **この順**で出る。 ``inlineHelpText`` など余計なキーは残らない。
    """
    result = _slim_object(_make_full_describe())

    # ``fields`` の各項目が 9 キーだけ、 この順で
    expected_order = [
        "name",
        "label",
        "type",
        "custom",
        "referenceTo",
        "relationshipName",
        "nameField",
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
    # active が 35 件、 inactive が 5 件（active な V0..V34 がそのまま並ぶ）
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
    """``custom`` / ``nameField`` 無しは ``False`` 、 ``referenceTo`` 無しは ``[]`` 、
    ``relationshipName`` 無しは空文字（内側が None を考えずに読めるように）。"""
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
    assert field["relationshipName"] == ""
    assert field["nameField"] is False
    assert field["picklist"] == []
    assert field["picklistTotal"] == 0


def test_slim_object_skips_non_dict_field_entries() -> None:
    """``fields`` の中に dict でない項目（文字列や ``None``）が混じっても飛ばす。"""
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
    """``_slim_object`` を呼んでも入力 dict は変わらない（別 dict にコピー）。"""
    import copy

    original = _make_full_describe()
    snapshot = copy.deepcopy(original)

    _slim_object(original)

    assert original == snapshot


def test_slim_object_handles_non_dict_input() -> None:
    """``describe`` が dict でない（``None`` や文字列）でも例外を上げず、 空の形で返す。"""
    empty = {"name": "", "label": "", "custom": False, "fields": []}
    assert _slim_object(None) == empty
    assert _slim_object("string") == empty
    assert _slim_object([]) == empty


# ── JSON 出力に重いキーが一切残らないこと ──────────────────────────────────


def test_record_report_has_no_heavy_keys(tmp_path: Path) -> None:
    """``ReportRecord.report`` のどこにも ``reportTypeMetadata`` /
    ``childRelationships`` / ``recordTypeInfos`` / ``picklistValues`` が
    **キーとして** 出現しない（ ``json.dumps`` した文字列に
    ``"重いキー":`` の形が出ない）。
    なぜ: 機微な情報や、 巨大な原本が record の中に漏れていないことを保証する。
    """
    # 元 describe に「重い」 キーを全部盛りにした ``Account`` を作る
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

    import json as _json

    record = run_fetch([entry], site_for=site_for, **_settings(tmp_path))[0].record
    raw_text = _json.dumps({"report": record.report, "objects": record.objects}, ensure_ascii=False)

    # ``"重いキー":`` という**キー形**が出現しない（ 出力 record の中に
    # 「キー名そのもの」 が文字列として現れるのは無い ）
    for forbidden in (
        "reportTypeMetadata",
        "childRelationships",
        "recordTypeInfos",
        "picklistValues",
    ):
        assert f'"{forbidden}":' not in raw_text, (
            f"{forbidden!r} が record のキーとして残っています"
        )


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

    record = run_fetch([entry], site_for=site_for, **_settings(tmp_path))[0].record
    payload = _record_to_payload(record)

    # ``Account`` が出てきている（slim 後の fields から集めていれば出ない）
    assert "Account" in payload["objects"]


# ── 6 つの改ざんが検出できることの直接テスト ──────────────────────────────────


def test_slim_report_detects_allowlist_violation(monkeypatch: pytest.MonkeyPatch) -> None:
    """``REPORT_METADATA_KEYS`` を広げると（または完全に無視すると）、
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

    # 本来 ``_collect_dropped_keys`` 側に記録されるはずのキーが ``reportMetadata``
    # に**残ってしまっている**（改ざんを検出できる）
    assert "useNormalizedFieldForCurrency" in result["reportMetadata"]
    assert "userOrTeamFilterId" in result["reportMetadata"]
    assert "description" in result["reportMetadata"]


def test_slim_report_detects_collect_dropped_keys_being_emptied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_collect_dropped_keys`` を常に空にすると、 落ちたキーが記録されない
    ことを検出できる。
    """
    from src import fetch as fetch_module

    def bad_collect(metadata: object) -> tuple[str, ...]:
        # 何も返さない
        return ()

    monkeypatch.setattr(fetch_module, "_collect_dropped_keys", bad_collect)

    metadata = _make_report_metadata_with_extras()
    dropped = fetch_module._collect_dropped_keys(metadata)
    # 落ちたキーが**記録されていない**（改ざんが検出できる）
    assert dropped == ()


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

    # ``picklistTotal`` が 0 になっている（本来は 35）
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

    # inactive な "Off" / "Pending" が ``picklist`` に入っている（改ざんが分かる）
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

    # 元 dict が壊されている（``fields`` が書き換わっている）
    assert original != original_snapshot
    assert original["fields"] == [{"name": "Replaced", "label": "Replaced", "type": "string"}]


def test_fetch_detects_related_collection_from_slimmed_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """関連オブジェクトの収集が slim 後の ``fields`` で行われる改ざんを検出できる。
    slim 後は ``referenceTo`` が空になるため、 関連が 1 つも取れなくなる。

    検出方法: 関連オブジェクト収集が slim 後に走る「悪い実装」 に
    ``_related_ref_pairs`` を差し替える （ ``_slim_object`` を先に呼んで元
    dict を破壊してから原本を読む ）。 本物の実装は slim の前に走るので
    ``Account`` が取れる。
    """
    from src import fetch as fetch_module

    real_slim_object = fetch_module._slim_object
    real_collect = fetch_module._related_ref_pairs
    call_log: list[str] = []

    def bad_slim_object(describe):
        # 悪い例: 呼ぶと同時に元 dict を slim 後の形に**書き換える**
        # （``referenceTo`` を [] に、 ``relationshipName`` を None にする）
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
    monkeypatch.setattr(fetch_module, "_related_ref_pairs", order_collect)

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

    record = run_fetch([entry], site_for=site_for, **_settings(tmp_path))[0].record
    payload = _record_to_payload(record)

    # slim 後の ``fields`` から集めていると ``Account`` が**取れない**
    # （本物の実装では ``Account`` が必ず取れる）
    assert "Account" not in payload["objects"]
    assert call_log  # 収集ロジックは呼ばれた


# ── ログに機微な値を漏らさない ──────────────────────────────────────────────

# 目印の文字列。 行データがログや出力に漏れていないことを検知するために使う
_LOG_SECRET = "LOG-SECRET-ROW-VALUE"


def test_run_fetch_does_not_leak_row_data_to_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """``run_fetch`` を DEBUG ログで走らせても、 レポートの行データ
    （個人情報を含みうる ``Table`` の値そのもの） がログに一切出ないこと。
    なぜ: 1 レポート分の経過ログを INFO で出すようにしたため、 うっかり
    値そのものを ``logger.info`` に渡していないことを保証する。
    """
    caplog.set_level(logging.DEBUG)

    class _TableWithSecret:
        def __init__(self) -> None:
            self._rows = [
                {"col": f"value {_LOG_SECRET} 1"},
                {"col": f"value {_LOG_SECRET} 2"},
            ]

        def __len__(self) -> int:
            return 2

    entry = MasterEntry(
        key="1001",
        summary="顧客一覧",
        url=f"{DOMAIN}/00O5g00000ABCDE/view",
        enabled=True,
    )
    metadata = _make_metadata()
    client = _FakeClient(
        describe=metadata,
        object_describes={"Opportunity": _main_describe("Opportunity")},
        get_table=_TableWithSecret(),
    )
    site_for, _ = _site_for(client)

    outcomes = run_fetch([entry], site_for=site_for, **_settings(tmp_path))

    assert outcomes[0].status == "ok"
    # ログに値そのものが漏れていない （DEBUG 以上全部）
    for record in caplog.records:
        assert _LOG_SECRET not in record.getMessage(), (
            f"行データ {_LOG_SECRET!r} がログに漏れた: {record.getMessage()}"
        )


def test_run_fetch_emits_each_outcome_warning_once_at_warning_level(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """``outcome.warnings`` の各要素が ``logger.warning("  - %s", warning)`` で
    ちょうど 1 回ずつ WARNING ログに出る。 ``_fetch_object_cached`` の
    フォールバック WARNING を消しているので、 同じ文言が二重に出ない。
    """
    caplog.set_level(logging.WARNING)

    main = _main_describe("Opportunity", fields_map={"AccountId": ["Account"], "OwnerId": ["User"]})
    entry = MasterEntry(key="1", summary="", url=f"{DOMAIN}/00O5g00000ABCDE/view", enabled=True)
    client = _FakeClient(
        describe=_make_metadata(),
        object_describes={"Opportunity": main, "Account": _main_describe("Account")},
        errors={"User": SalesforceRequestError("GET", "/sobjects/User/describe", 404, "Not Found")},
    )
    site_for, _ = _site_for(client)

    expected_warning = "オブジェクト User の describe に失敗（HTTP 404: Not Found）"

    outcomes = run_fetch([entry], site_for=site_for, **_settings(tmp_path))

    assert outcomes[0].status == "ok"
    # warnings に User の失敗がちょうど 1 件だけ積まれている
    assert outcomes[0].warnings == (expected_warning,)
    # WARNING レベルで 「-」 プレフィックス付きで 1 回出る
    prefixed_warning_records = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING and record.getMessage().startswith("  - ")
    ]
    assert len(prefixed_warning_records) == 1, (
        f"プレフィックス付き WARNING は 1 件のはず、 "
        f"実際 {[r.getMessage() for r in prefixed_warning_records]}"
    )
    assert expected_warning in prefixed_warning_records[0].getMessage()
    # 同じ文言が他の WARNING にも出ないこと （二重ログ防止）
    duplicated = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING and expected_warning in record.getMessage()
    ]
    assert len(duplicated) == 1, (
        f"同一 WARNING が複数回出た: {[r.getMessage() for r in duplicated]}"
    )


# ── 列の対応.csv の経路補完 （End-to-end） ────────────────────────────────


def _path_opp_with_account() -> dict:
    """``Account.Owner.Name`` を辿れるよう ``relationshipName`` を ``Account``
    / ``Owner`` にした Opportunity の describe。 _main_describe は
    ``AccountId_Account`` の形を作るので、 このテスト専用に手で書く。
    """
    return {
        "name": "Opportunity",
        "fields": [
            {
                "name": "AccountId",
                "type": "reference",
                "referenceTo": ["Account"],
                "relationshipName": "Account",
            },
            {
                "name": "Name",
                "type": "string",
                "label": "名前",
            },
        ],
    }


def _path_account_with_owner() -> dict:
    """User への参照を持ち、 ``relationshipName = "Owner"`` な Account の describe。"""
    return {
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


def _path_owner_metadata() -> dict:
    """``Account.Owner.Name`` （人が書いた） を確認するためのメタデータ。

    ``detailColumns = ["Opp.Account.Owner"]`` を使い、 自動では解決できず
    ``(不明)`` のままになる。 そこに人が ``Account.Owner.Name`` を書いて、
    経路の補完の振る舞いを見る。 ``detailColumnInfo`` の label は
    ``column_key`` をそのまま表示名として流して使う （ 列キー→表示名の解決に
    影響しないように ） 。
    """
    return {
        "reportMetadata": {
            "reportType": {"type": "Opportunity"},
            "reportFormat": "TABULAR",
            "detailColumns": ["Opp.Account.Owner"],
        },
        "reportExtendedMetadata": {
            "detailColumnInfo": {"Opp.Account.Owner": {"label": "所有者"}},
        },
    }


def _path_owner_overrides() -> dict[tuple[str, str], ColumnMapRow]:
    """人が ``列の直し.xlsx`` に書いた ``Account.Owner.Name``。"""
    return {
        ("opportunity", "Opp.Account.Owner"): ColumnMapRow(
            status="OK",
            main_object="Opportunity",
            display_name="所有者",
            column_key="Opp.Account.Owner",
            auto_soql_name="",
            candidates="",
            fixed_soql_name="Account.Owner.Name",
            note="",
            used_in_reports="",
            updated_at="",
        ),
    }


def test_run_fetch_fetches_missing_object_along_user_path(tmp_path: Path) -> None:
    """主オブジェクト Opportunity、 取得済みは Opportunity と Account だけ、 人が
    ``Account.Owner.Name`` を書く → User を取りに行き （``describe_object`` が
    User で 1 回だけ呼ばれる ） 、 型が付き、 WARNING なし、 record の
    ``objects`` に User が入る
    """
    user_describe = _main_describe("User", [{"name": "Name", "type": "string"}])

    entry = MasterEntry(key="1", summary="", url=f"{DOMAIN}/00O5g00000ABCDE/view", enabled=True)
    client = _FakeClient(
        describe=_path_owner_metadata(),
        object_describes={
            "Opportunity": _path_opp_with_account(),
            "Account": _path_account_with_owner(),
            "User": user_describe,
        },
    )
    site_for, _ = _site_for(client)
    store = DescribeStore.empty(tmp_path / "describe.csv")

    outcomes = run_fetch(
        [entry],
        site_for=site_for,
        describe_store=store,
        column_fix_overrides=_path_owner_overrides(),
        **_settings(tmp_path),
    )

    assert outcomes[0].status == "ok"
    record = outcomes[0].record
    # User は ``describe_object`` を 1 回叩かれている
    assert client.describe_object_calls.count("User") == 1
    # User が ``record.objects`` にも入った
    assert "User" in record.objects
    # 型は string
    row = next(r for r in record.column_map if r["列キー"] == "Opp.Account.Owner")
    assert row["対応フィールドAPI名"] == "Account.Owner.Name"
    assert row["型"] == "string"
    # 「User の describe に失敗」 系は出ない
    assert not any("User の describe に失敗" in w for w in record.warnings)


def test_run_fetch_caches_fetched_user_across_reports(tmp_path: Path) -> None:
    """同じ実行で 2 本目のレポートが同じ経路 → User は Salesforce に再度問い合わせない

    キャッシュは ``DescribeStore`` を介して行われる。 1 本目の Report で
    User が API 取得 → store.memory_cache に入る → 2 本目では store.lookup
    が即座に返し、 API を叩かない
    """
    user_describe = _main_describe("User", [{"name": "Name", "type": "string"}])

    entry1 = MasterEntry(key="1001", summary="", url=f"{DOMAIN}/00O5g00000AAAAA/view", enabled=True)
    entry2 = MasterEntry(key="1002", summary="", url=f"{DOMAIN}/00O5g00000BBBBB/view", enabled=True)
    client = _FakeClient(
        describe=_path_owner_metadata(),
        object_describes={
            "Opportunity": _path_opp_with_account(),
            "Account": _path_account_with_owner(),
            "User": user_describe,
        },
    )
    site_for, _ = _site_for(client)
    store = DescribeStore.empty(tmp_path / "describe.csv")

    outcomes = run_fetch(
        [entry1, entry2],
        site_for=site_for,
        describe_store=store,
        column_fix_overrides=_path_owner_overrides(),
        **_settings(tmp_path),
    )

    # 2 件とも成功
    assert outcomes[0].status == "ok"
    assert outcomes[1].status == "ok"
    # User は 1 回しか API を叩かれていない （共有ストアでキャッシュヒット）
    assert client.describe_object_calls.count("User") == 1


def test_run_fetch_uses_describe_csv_cache_for_user(tmp_path: Path) -> None:
    """``describe.csv`` （``DescribeStore.csv_index``） に User が既にあれば
    Salesforce に問い合わせない。 ``_run_fetch`` の前に ``store.csv_index`` に
    User の describe を入れて、 simulate する
    """
    user_describe = _main_describe("User", [{"name": "Name", "type": "string"}])

    entry = MasterEntry(key="1", summary="", url=f"{DOMAIN}/00O5g00000ABCDE/view", enabled=True)
    client = _FakeClient(
        describe=_path_owner_metadata(),
        object_describes={
            "Opportunity": _path_opp_with_account(),
            "Account": _path_account_with_owner(),
            # User は object_describes に **入れない**。 CSV からの読みだけで
            # 済ませるため
        },
    )
    site_for, _ = _site_for(client)
    store = DescribeStore.empty(tmp_path / "describe.csv")
    # pre-populate csv_index with User. キーは ``org_from_url`` で取り出される
    # URL のホスト名 （小文字） とオブジェクト名を lower で揃える。
    # ``DOMAIN`` のホストは ``example--sandbox.sandbox.my.salesforce.com``
    from src.describe_store import org_from_url

    org = org_from_url(entry.url)
    store.csv_index[(org, "user")] = user_describe

    outcomes = run_fetch(
        [entry],
        site_for=site_for,
        describe_store=store,
        column_fix_overrides=_path_owner_overrides(),
        **_settings(tmp_path),
    )

    assert outcomes[0].status == "ok"
    # User はキャッシュから返るので API は叩かれない
    assert "User" not in client.describe_object_calls
    # 型は引ける
    row = next(r for r in outcomes[0].record.column_map if r["列キー"] == "Opp.Account.Owner")
    assert row["型"] == "string"


def test_run_fetch_typo_in_path_warns_with_candidates(tmp_path: Path) -> None:
    """``Account.Ownr.Name`` （打ち間違い） → 「 Ownr が Account に見つかり
    ません」 と候補 （Owner など） が WARNING に出る、 値は残る
    """
    account_describe = {
        "name": "Account",
        "fields": [
            {
                "name": "OwnerId",
                "type": "reference",
                "relationshipName": "Owner",
                "referenceTo": ["User"],
            },
            {
                "name": "Custom__c",
                "type": "string",
                "relationshipName": "CustomRel",
                "referenceTo": ["User"],
            },
        ],
    }

    entry = MasterEntry(key="1", summary="", url=f"{DOMAIN}/00O5g00000ABCDE/view", enabled=True)
    client = _FakeClient(
        describe=_path_owner_metadata(),
        object_describes={
            "Opportunity": _path_opp_with_account(),
            "Account": account_describe,
        },
    )
    site_for, _ = _site_for(client)
    store = DescribeStore.empty(tmp_path / "describe.csv")

    overrides = {
        ("opportunity", "Opp.Account.Owner"): ColumnMapRow(
            status="OK",
            main_object="Opportunity",
            display_name="所有者",
            column_key="Opp.Account.Owner",
            auto_soql_name="",
            candidates="",
            fixed_soql_name="Account.Ownr.Name",  # 打ち間違い
            note="",
            used_in_reports="",
            updated_at="",
        ),
    }

    outcomes = run_fetch(
        [entry],
        site_for=site_for,
        describe_store=store,
        column_fix_overrides=overrides,
        **_settings(tmp_path),
    )
    assert outcomes[0].status == "ok"
    record = outcomes[0].record
    # 値は残っている
    row = next(r for r in record.column_map if r["列キー"] == "Opp.Account.Owner")
    assert row["対応フィールドAPI名"] == "Account.Ownr.Name"
    assert row["型"] == ""
    # 「たどれません」 文と候補が出 る
    matching = [w for w in record.warnings if "「Ownr」が Account に見つかりません" in w]
    assert len(matching) == 1
    assert "Owner" in matching[0]
    assert "CustomRel" in matching[0]


def test_run_fetch_path_fetch_404_warns_and_record_ok(tmp_path: Path) -> None:
    """経路上の参照先 （User） が ``describe_object`` で 404 → 「 参照先 User
    の項目定義を取得できません 」 の WARNING、 値は残る、 そのレポートは成功
    """
    entry = MasterEntry(key="1", summary="", url=f"{DOMAIN}/00O5g00000ABCDE/view", enabled=True)
    client = _FakeClient(
        describe=_path_owner_metadata(),
        object_describes={
            "Opportunity": _path_opp_with_account(),
            "Account": _path_account_with_owner(),
        },
        errors={"User": SalesforceRequestError("GET", "/sobjects/User/describe", 404, "Not Found")},
    )
    site_for, _ = _site_for(client)
    store = DescribeStore.empty(tmp_path / "describe.csv")

    outcomes = run_fetch(
        [entry],
        site_for=site_for,
        describe_store=store,
        column_fix_overrides=_path_owner_overrides(),
        **_settings(tmp_path),
    )
    assert outcomes[0].status == "ok"
    record = outcomes[0].record
    row = next(r for r in record.column_map if r["列キー"] == "Opp.Account.Owner")
    # 値は残る
    assert row["対応フィールドAPI名"] == "Account.Owner.Name"
    assert row["型"] == ""
    # 「参照先 User の項目定義を取得できません」 文
    matching = [w for w in record.warnings if "参照先 User の項目定義を取得できません" in w]
    assert len(matching) == 1


def test_run_fetch_path_fetch_401_marks_report_failed(tmp_path: Path) -> None:
    """経路上の参照先 （User） が ``describe_object`` で 401 → レポート全体が
    失敗扱いになる。 record は None
    """
    entry = MasterEntry(key="1", summary="", url=f"{DOMAIN}/00O5g00000ABCDE/view", enabled=True)
    client = _FakeClient(
        describe=_path_owner_metadata(),
        object_describes={
            "Opportunity": _path_opp_with_account(),
            "Account": _path_account_with_owner(),
        },
        errors={
            "User": SalesforceRequestError("GET", "/sobjects/User/describe", 401, "Unauthorized")
        },
    )
    site_for, _ = _site_for(client)
    store = DescribeStore.empty(tmp_path / "describe.csv")

    outcomes = run_fetch(
        [entry],
        site_for=site_for,
        describe_store=store,
        column_fix_overrides=_path_owner_overrides(),
        **_settings(tmp_path),
    )

    assert outcomes[0].status == "failed"
    assert outcomes[0].record is None
    assert "401" in (outcomes[0].error or "")


def test_run_fetch_handles_polymorphic_what(tmp_path: Path) -> None:
    """ポリモーフィック （``What.Name`` で What → Account / Opportunity）:
    取得済みの ``referenceTo`` の順に試して最初の候補の ``Name`` で型を引く
    """
    metadata = {
        "reportMetadata": {
            "reportType": {"type": "Opportunity"},
            "reportFormat": "TABULAR",
            "detailColumns": ["Opp.What"],
        },
        "reportExtendedMetadata": {
            "detailColumnInfo": {"Opp.What": {"label": "What"}},
        },
    }
    opp_describe = {
        "name": "Opportunity",
        "fields": [
            {
                "name": "WhatId",
                "type": "reference",
                "relationshipName": "What",
                "referenceTo": ["Account", "Opportunity"],
            },
        ],
    }
    account_describe = _main_describe("Account", [{"name": "Name", "type": "string"}])

    entry = MasterEntry(key="1", summary="", url=f"{DOMAIN}/00O5g00000ABCDE/view", enabled=True)
    client = _FakeClient(
        describe=metadata,
        object_describes={
            "Opportunity": opp_describe,
            "Account": account_describe,
            # 念のため Opportunity にも Name を置くが、 ポリモーフィックでは
            # 先頭の Account が優先されることを確認するため
            "Opportunity2": _main_describe("Opportunity", [{"name": "Name", "type": "string"}]),
        },
    )
    site_for, _ = _site_for(client)
    store = DescribeStore.empty(tmp_path / "describe.csv")

    overrides = {
        ("opportunity", "Opp.What"): ColumnMapRow(
            status="OK",
            main_object="Opportunity",
            display_name="What",
            column_key="Opp.What",
            auto_soql_name="",
            candidates="",
            fixed_soql_name="What.Name",
            note="",
            used_in_reports="",
            updated_at="",
        ),
    }

    outcomes = run_fetch(
        [entry],
        site_for=site_for,
        describe_store=store,
        column_fix_overrides=overrides,
        **_settings(tmp_path),
    )
    assert outcomes[0].status == "ok"
    row = next(r for r in outcomes[0].record.column_map if r["列キー"] == "Opp.What")
    # Account が先頭なので Account.Name で string が引ける
    assert row["対応フィールドAPI名"] == "What.Name"
    assert row["型"] == "string"


def test_run_fetch_path_too_long_warns(tmp_path: Path) -> None:
    """6 段以上なら 「経路が長すぎます」 WARNING で止める。 値は残る"""
    metadata = {
        "reportMetadata": {
            "reportType": {"type": "Opportunity"},
            "reportFormat": "TABULAR",
            "detailColumns": ["Opp.A.B.C.D.E.F"],
        },
        "reportExtendedMetadata": {
            "detailColumnInfo": {"Opp.A.B.C.D.E.F": {"label": "deep"}},
        },
    }
    opp_describe = _main_describe("Opportunity", [{"name": "Name", "type": "string"}])

    entry = MasterEntry(key="1", summary="", url=f"{DOMAIN}/00O5g00000ABCDE/view", enabled=True)
    client = _FakeClient(
        describe=metadata,
        object_describes={"Opportunity": opp_describe},
    )
    site_for, _ = _site_for(client)
    store = DescribeStore.empty(tmp_path / "describe.csv")

    overrides = {
        ("opportunity", "Opp.A.B.C.D.E.F"): ColumnMapRow(
            status="OK",
            main_object="Opportunity",
            display_name="deep",
            column_key="Opp.A.B.C.D.E.F",
            auto_soql_name="",
            candidates="",
            fixed_soql_name="A.B.C.D.E.F",
            note="",
            used_in_reports="",
            updated_at="",
        ),
    }

    outcomes = run_fetch(
        [entry],
        site_for=site_for,
        describe_store=store,
        column_fix_overrides=overrides,
        **_settings(tmp_path),
    )
    assert outcomes[0].status == "ok"
    row = next(r for r in outcomes[0].record.column_map if r["列キー"] == "Opp.A.B.C.D.E.F")
    # 値が残り、 型は空
    assert row["対応フィールドAPI名"] == "A.B.C.D.E.F"
    assert row["型"] == ""
    assert any("経路が長すぎます" in w for w in outcomes[0].record.warnings)


def test_run_fetch_user_fix_overrides_auto_resolved(tmp_path: Path) -> None:
    """「直したSOQL列名」 に値があれば自動の結果より常に優先する。

    自動で取れた列でも、 人が ``列の直し.xlsx`` で値を入れていれば自動の値
    は上書きされる （人が入力した値を尊重する方針） 。
    """
    metadata = _make_metadata(detail_column_labels={"Opp.Name": "名前"})
    opp_describe = {
        "name": "Opportunity",
        "fields": [{"name": "Name", "label": "名前", "type": "string"}],
    }

    entry = MasterEntry(key="1", summary="", url=f"{DOMAIN}/00O5g00000ABCDE/view", enabled=True)
    client = _FakeClient(
        describe=metadata,
        object_describes={"Opportunity": opp_describe},
    )
    site_for, _ = _site_for(client)
    store = DescribeStore.empty(tmp_path / "describe.csv")

    overrides = {
        ("opportunity", "Opp.Name"): ColumnMapRow(
            status="OK",
            main_object="Opportunity",
            display_name="",
            column_key="Opp.Name",
            auto_soql_name="",
            candidates="",
            fixed_soql_name="Name_Renamed__c",  # 人の値が自動を上書きする
            note="",
            used_in_reports="",
            updated_at="",
        ),
    }

    outcomes = run_fetch(
        [entry],
        site_for=site_for,
        describe_store=store,
        column_fix_overrides=overrides,
        **_settings(tmp_path),
    )
    assert outcomes[0].status == "ok"
    row = next(r for r in outcomes[0].record.column_map if r["列キー"] == "Opp.Name")
    # 人の値で上書きされた
    assert row["対応フィールドAPI名"] == "Name_Renamed__c"
    # 備考に人が直したことが残る
    assert "人が直した" in row["備考"]


def test_run_fetch_no_fetcher_legacy_behavior(tmp_path: Path) -> None:
    """``column_fix_overrides`` が空のときは、 fetcher は使われず、 WARNING も
    「たどれません」 は出ない （何も上書きしない） 。
    """
    metadata = {
        "reportMetadata": {
            "reportType": {"type": "Opportunity"},
            "reportFormat": "TABULAR",
            "detailColumns": ["Opp.Account.Owner"],
        },
        "reportExtendedMetadata": {
            "detailColumnInfo": {"Opp.Account.Owner": {"label": "所有者"}},
        },
    }
    entry = MasterEntry(key="1", summary="", url=f"{DOMAIN}/00O5g00000ABCDE/view", enabled=True)
    client = _FakeClient(
        describe=metadata,
        # User はサーバ上は存在するが、 経路の補完は走らない
        object_describes={
            "Opportunity": _path_opp_with_account(),
            "Account": _path_account_with_owner(),
            "User": _main_describe("User", [{"name": "Name", "type": "string"}]),
        },
    )
    site_for, _ = _site_for(client)
    store = DescribeStore.empty(tmp_path / "describe.csv")

    # overrides 無し
    outcomes = run_fetch([entry], site_for=site_for, describe_store=store, **_settings(tmp_path))
    assert outcomes[0].status == "ok"
    # fetcher は呼ばれない (apply_overrides の経路補完は走らない)
    assert "User" not in client.describe_object_calls
    # 列は自動が取れなかったまま (不明)
    row = next(r for r in outcomes[0].record.column_map if r["列キー"] == "Opp.Account.Owner")
    assert row["対応フィールドAPI名"] in ("(不明)", "")


# ── 列キーからの主オブジェクト・SOQL 列名の解決 ───────────────────


def _contact_describe() -> dict:
    """列キー経路のテスト用の Contact describe。 Account への参照と
    カスタム Retention への参照 ( `Retention__c` / relationshipName
    `Retention__r` / referenceTo `Retention__c` ) を持つ。
    """
    return {
        "name": "Contact",
        "fields": [
            {"name": "Name", "type": "string", "label": "氏名"},
            {"name": "Email", "type": "email", "label": "メール"},
            {
                "name": "AccountId",
                "type": "reference",
                "referenceTo": ["Account"],
                "relationshipName": "Account",
            },
            {
                "name": "Retention__c",
                "type": "reference",
                "referenceTo": ["Retention__c"],
                "relationshipName": "Retention__r",
            },
        ],
    }


def _retention_describe() -> dict:
    """列キー経路のテスト用の Retention__c describe。"""
    return {
        "name": "Retention__c",
        "fields": [
            {"name": "Status__c", "type": "picklist", "label": "ステータス"},
        ],
    }


def _account_describe() -> dict:
    """列キー経路のテスト用の Account describe。"""
    return {
        "name": "Account",
        "fields": [
            {"name": "Name", "type": "string", "label": "取引先名"},
        ],
    }


def _custom_retention_metadata() -> dict:
    """``Contact_Retention__c`` 形式のレポートタイプ + 列キー ``Contact.*`` の
    メタデータ。**
    """
    detail_columns = [
        "Contact.Name",
        "Contact.Email",
        "Contact.Account.Name",
        "Contact.Retention__c.Status__c",
    ]
    info = {
        "Contact.Name": {"label": "氏名"},
        "Contact.Email": {"label": "メール"},
        "Contact.Account.Name": {"label": "取引先名"},
        "Contact.Retention__c.Status__c": {"label": "ステータス"},
    }
    return {
        "reportMetadata": {
            "reportType": {"type": "Contact_Retention__c"},
            "reportFormat": "TABULAR",
            "detailColumns": detail_columns,
        },
        "reportExtendedMetadata": {"detailColumnInfo": info},
    }


def test_main_object_falls_back_to_column_key_prefix(tmp_path: Path) -> None:
    """``reportType.type`` が describe 404、 列キー先頭の ``Contact`` が
    通る → ``Contact`` が主オブジェクトになり、警告は出ない
    """
    entry = MasterEntry(key="1", summary="", url=f"{DOMAIN}/00O5g00000ABCDE/view", enabled=True)
    client = _FakeClient(
        describe=_custom_retention_metadata(),
        object_describes={
            "Contact": _contact_describe(),
            "Account": _account_describe(),
            "Retention__c": _retention_describe(),
        },
        errors={
            "Contact_Retention__c": SalesforceRequestError(
                "GET", "/sobjects/Contact_Retention__c/describe", 404, "Not Found"
            ),
        },
    )
    site_for, _ = _site_for(client)
    record = run_fetch([entry], site_for=site_for, **_settings(tmp_path))[0].record
    payload = _record_to_payload(record)
    # 列キー先頭の Contact が主オブジェクトに
    assert payload["主オブジェクト"] == "Contact"
    # ``Contact_Retention__c`` の 404 が警告として残らない
    # (``Contact`` 側の警告も無い = 正常な経路扱い)
    assert not any("Contact_Retention__c" in w for w in payload["warnings"])
    assert not any("Contact の describe に失敗" in w for w in payload["warnings"])


def test_column_key_path_walks_soql_and_fetches_intermediate(tmp_path: Path) -> None:
    """列キーが ``Contact.Account.Name`` / ``Contact.Retention__c.Status__c``
    なら SOQL 列名が ``Account.Name`` / ``Retention__r.Status__c`` になり、
    型が付き、 Retention__c は取りに行く （describe 1 回） 。
    """
    entry = MasterEntry(key="1", summary="", url=f"{DOMAIN}/00O5g00000ABCDE/view", enabled=True)
    client = _FakeClient(
        describe=_custom_retention_metadata(),
        object_describes={
            "Contact": _contact_describe(),
            "Account": _account_describe(),
            "Retention__c": _retention_describe(),
        },
        errors={
            "Contact_Retention__c": SalesforceRequestError(
                "GET", "/sobjects/Contact_Retention__c/describe", 404, "Not Found"
            ),
        },
    )
    site_for, _ = _site_for(client)
    record = run_fetch([entry], site_for=site_for, **_settings(tmp_path))[0].record

    rows_by_key = {r["列キー"]: r for r in record.column_map}
    name_row = rows_by_key["Contact.Name"]
    assert name_row["対応フィールドAPI名"] == "Name"
    assert name_row["型"] == "string"
    assert name_row["備考"] == "列キーから"

    email_row = rows_by_key["Contact.Email"]
    assert email_row["対応フィールドAPI名"] == "Email"
    assert email_row["型"] == "email"
    assert email_row["備考"] == "列キーから"

    account_row = rows_by_key["Contact.Account.Name"]
    assert account_row["対応フィールドAPI名"] == "Account.Name"
    assert account_row["型"] == "string"
    assert account_row["備考"] == "列キーから"

    retention_row = rows_by_key["Contact.Retention__c.Status__c"]
    assert retention_row["対応フィールドAPI名"] == "Retention__r.Status__c"
    assert retention_row["型"] == "picklist"
    assert retention_row["備考"] == "列キーから"

    # Retention__c は取りに行った (Account は関連の探索経路で既出に近い回数になる)
    assert client.describe_object_calls.count("Retention__c") == 1


def test_column_key_path_falls_back_to_label_when_first_segment_404(
    tmp_path: Path,
) -> None:
    """列キー ``Contact.Acount.Name`` (Acount は Contact に無い) → 経路の
    補完は失敗、 表示名の照合にフォールバック。 表示名が ``Account Name``
    のときに Account.Name が取れて、 備考は 「関連オブジェクト経由」
    (経路の補完失敗の理由は残らない)

    """
    metadata = {
        "reportMetadata": {
            "reportType": {"type": "Contact"},
            "reportFormat": "TABULAR",
            "detailColumns": ["Contact.Acount.Name"],
        },
        "reportExtendedMetadata": {
            "detailColumnInfo": {"Contact.Acount.Name": {"label": "Account Name"}},
        },
    }
    entry = MasterEntry(key="1", summary="", url=f"{DOMAIN}/00O5g00000ABCDE/view", enabled=True)
    contact = {
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
    account = {
        "name": "Account",
        "fields": [{"name": "Name", "type": "string", "label": "Account Name"}],
    }
    client = _FakeClient(
        describe=metadata,
        object_describes={"Contact": contact, "Account": account},
    )
    site_for, _ = _site_for(client)
    record = run_fetch([entry], site_for=site_for, **_settings(tmp_path))[0].record
    row = next(r for r in record.column_map if r["列キー"] == "Contact.Acount.Name")
    # 表示名で Account.Name が取れている
    assert row["対応フィールドAPI名"] == "Account.Name"
    assert row["型"] == "string"
    # 表示名の照合で決まったので経路の補完失敗理由は **残らない**
    assert "列キーで" not in row["備考"]


def test_column_key_path_falls_back_when_label_also_fails(tmp_path: Path) -> None:
    """列キー ``Contact.Acount.Name`` で経路も表示名もダメ → 「 対応表示で
    はありません 」 の代わりに 経路の補完失敗の理由が備考に入る
    """
    metadata = {
        "reportMetadata": {
            "reportType": {"type": "Contact"},
            "reportFormat": "TABULAR",
            "detailColumns": ["Contact.Acount.Name"],
        },
        "reportExtendedMetadata": {
            "detailColumnInfo": {"Contact.Acount.Name": {"label": "存在しないラベル"}},
        },
    }
    entry = MasterEntry(key="1", summary="", url=f"{DOMAIN}/00O5g00000ABCDE/view", enabled=True)
    contact = {
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
    account = {"name": "Account", "fields": []}
    client = _FakeClient(
        describe=metadata,
        object_describes={"Contact": contact, "Account": account},
    )
    site_for, _ = _site_for(client)
    record = run_fetch([entry], site_for=site_for, **_settings(tmp_path))[0].record
    row = next(r for r in record.column_map if r["列キー"] == "Contact.Acount.Name")
    assert row["対応フィールドAPI名"] == "(不明)"
    # 経路の補完失敗の理由がそのまま備考に入る
    assert "列キーで" in row["備考"]
    assert "Acount" in row["備考"]


def test_standard_form_column_key_falls_back_to_label_matching(tmp_path: Path) -> None:
    """``.`` を含まない列キー (例 ``OPPORTUNITY_NAME``) は列キーからの
    経路を試さず、 既存の表示名引き当てがそのまま動く
    """
    metadata = {
        "reportMetadata": {
            "reportType": {"type": "Opportunity"},
            "reportFormat": "TABULAR",
            "detailColumns": ["OPPORTUNITY_NAME"],
        },
        "reportExtendedMetadata": {
            "detailColumnInfo": {"OPPORTUNITY_NAME": {"label": "商談名"}},
        },
    }
    opportunity = {
        "name": "Opportunity",
        "fields": [{"name": "Name", "type": "string", "label": "商談名"}],
    }
    entry = MasterEntry(key="1", summary="", url=f"{DOMAIN}/00O5g00000ABCDE/view", enabled=True)
    client = _FakeClient(describe=metadata, object_describes={"Opportunity": opportunity})
    site_for, _ = _site_for(client)
    record = run_fetch([entry], site_for=site_for, **_settings(tmp_path))[0].record
    row = next(r for r in record.column_map if r["列キー"] == "OPPORTUNITY_NAME")
    assert row["対応フィールドAPI名"] == "Name"
    assert row["型"] == "string"
    # 列キーからの経路ではない、 既存の表示名引き当てで決まった
    assert row["備考"] == ""


def test_column_key_prefix_picks_2nd_when_first_404(tmp_path: Path) -> None:
    """``Contact_Retention__c`` の describe が 404 、 列キーの先頭 1 番目
    (例 ``Contact_Retention__c``) も 404 、 2 番目 (例 ``Contact``) が
    通る → 2 番目が主オブジェクトになる
    """
    metadata = {
        "reportMetadata": {
            "reportType": {"type": "Contact_Retention__c"},
            "reportFormat": "TABULAR",
            "detailColumns": ["Contact_Retention__c.Name", "Contact.Name", "Contact.Email"],
        },
        "reportExtendedMetadata": {
            "detailColumnInfo": {
                "Contact_Retention__c.Name": {"label": "X"},
                "Contact.Name": {"label": "氏名"},
                "Contact.Email": {"label": "メール"},
            },
        },
    }
    entry = MasterEntry(key="1", summary="", url=f"{DOMAIN}/00O5g00000ABCDE/view", enabled=True)
    client = _FakeClient(
        describe=metadata,
        object_describes={
            "Contact": _contact_describe(),
        },
        errors={
            "Contact_Retention__c": SalesforceRequestError(
                "GET", "/sobjects/Contact_Retention__c/describe", 404, "Not Found"
            ),
        },
    )
    site_for, _ = _site_for(client)
    record = run_fetch([entry], site_for=site_for, **_settings(tmp_path))[0].record
    payload = _record_to_payload(record)
    # 2 番目の ``Contact`` が採用される
    assert payload["主オブジェクト"] == "Contact"


def test_column_key_break_makes_resilience(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``build_column_map`` の列キーからの経路の補完を空にした壊れた実装に
    差し替えると、 列キー経路のテスト ``test_column_key_path_walks_soql_...``
    の主条件 ( ``Contact.Retention__c.Status__c`` が ``Retention__r.Status__c``
    に解決 ) が落ち、 本物の実装では通ることを直接確かめる
    """
    import src.mapping as mapping_module

    real = mapping_module.build_column_map

    def no_column_key_build_column_map(
        metadata: dict,
        main_describe: dict | None,
        reason: str | None,
        *,
        fetch_object: object = None,
        **kwargs: object,
    ) -> list[dict[str, str]]:
        del fetch_object
        # 列キーからの経路の補完を試さない壊れた版
        return real(
            metadata,
            main_describe,
            reason,
            related_describes=None,
            relations=None,
            main_object=kwargs.get("main_object"),
            fetch_object=None,
        )

    monkeypatch.setattr(mapping_module, "build_column_map", no_column_key_build_column_map)

    # テスト条件: ``Contact_Retention__c`` (404) + 列キー ``Contact.*``
    entry = MasterEntry(key="1", summary="", url=f"{DOMAIN}/00O5g00000ABCDE/view", enabled=True)
    client = _FakeClient(
        describe=_custom_retention_metadata(),
        object_describes={
            "Contact": _contact_describe(),
            "Account": _account_describe(),
            "Retention__c": _retention_describe(),
        },
        errors={
            "Contact_Retention__c": SalesforceRequestError(
                "GET", "/sobjects/Contact_Retention__c/describe", 404, "Not Found"
            ),
        },
    )
    site_for, _ = _site_for(client)
    record = run_fetch([entry], site_for=site_for, **_settings(tmp_path))[0].record
    # ``Contact.Retention__c.Status__c`` の SOQL 列名は
    # ``Retention__r.Status__c`` にならず、 経路の補完が空だったために
    # ``(不明)`` のままになる
    row = next(r for r in record.column_map if r["列キー"] == "Contact.Retention__c.Status__c")
    assert row["対応フィールドAPI名"] == "(不明)"


def test_extract_column_key_prefix_candidates_orders_by_frequency() -> None:
    """``_extract_column_key_prefix_candidates`` は ``.`` を含む列キーの
    先頭を 多い順、 同数は最初に出た順 で最大 5 個返す"""
    from src.fetch import _extract_column_key_prefix_candidates

    metadata = {
        "reportMetadata": {
            "reportType": {"type": "Unknown__c"},
            "detailColumns": [
                "Contact.Name",  # Contact 1
                "Contact.Email",  # Contact 2
                "Account.Name",  # Account 1
                "Contact.Account.Industry",  # Contact 3
                "Owner.Name",  # Owner 1
                "OPPORTUNITY_NAME",  # 対象外 (``.`` なし)
            ],
            "reportFilters": [{"column": "Account.Owner"}],  # Account 2
        },
    }
    assert _extract_column_key_prefix_candidates(metadata) == [
        "Contact",  # 3 件
        "Account",  # 2 件
        "Owner",  # 1 件 (出現順が先)
    ]


def test_extract_column_key_prefix_candidates_excludes_fk_prefix() -> None:
    """``FK_`` で始まる列キーは主オブジェクトの代替候補から除外する。

    ``FK_`` の先は主オブジェクトではなく主からの参照先なので、 主候補
    には使えない。 除外してもカウントは進めない。
    """
    from src.fetch import _extract_column_key_prefix_candidates

    metadata = {
        "reportMetadata": {
            "reportType": {"type": "Unknown__c"},
            "detailColumns": [
                "Contact.Name",  # Contact 1
                "FK_Contact.Email",  # FK_ の先は主オブジェクトではない
                "FK_Account.Name",  # 同上
                "FK_Contact.X",  # 同じく除外
                "Account.Name",  # Account 1
            ],
            "reportFilters": [{"column": "Account.Owner"}],  # Account 2
        },
    }
    assert _extract_column_key_prefix_candidates(metadata) == [
        "Account",  # 2 件 (Account.Name + Account.Owner 、 FK_Account.* は除外)
        "Contact",  # 1 件 (出現順が先)
    ]
