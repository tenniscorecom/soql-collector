"""trial/fetch_objects.py — オブジェクトの項目定義を取り直す単品サンプル。

人が 「 対応が取れなかった 」 と思ったときに、 そのオブジェクトの API 名を
リストで書いて実行するだけで ``data/項目定義.csv`` のキャッシュに項目定義を
入れ、 ``output/single/オブジェクトの項目.csv`` にも展開する。

``trial/`` は **名前空間パッケージ** （ ``__init__.py`` を **持たない** ）。
``python -m trial.fetch_objects`` でそのまま動く。

ファイル先頭の ``OBJECT_NAMES`` / ``ORG_URL`` を書き換えて実行する
（ コマンドライン引数は使わない ）。 1 回の接続でリストの各オブジェクトを
**Salesforce から取り直す** （ キャッシュは読まない ）。 結果は既存の
``DescribeStore`` のキャッシュ ``[FILES] DESCRIBE_CSV_PATH`` （ 既定
``./data/項目定義.csv`` ） に upsert し、 次の通常の実行がそこから使える
ようにする。

終了コード:

- 0 … 全件取得成功
- 1 … 1 件以上のオブジェクトが取得できなかった / 接続が致命エラー
- 2 … OBJECT_NAMES が空 / ORG_URL 未指定で組織が一意に決まらない
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from typing import Any

from comken.core.table import Table
from comken.exceptions import ComkenError, SalesforceError, SalesforceRequestError
from comken.toolbox.csv import CSV

from src.describe_store import DescribeStore, org_from_url
from src.fetch import FATAL_STATUS_CODES
from src.master import read_master
from src.settings import load_settings

logger = logging.getLogger(__name__)

# ``run_fetch`` の ``site_for`` と同じ型（ テスト用差し替え ）
SiteFor = Callable[[str], type]

# 取りたいオブジェクトの API 名。 ここを書き換えて実行する
OBJECT_NAMES: list[str] = ["Account", "User"]
# 組織の URL。 空なら管理表の URL から自動で決める（ 組織が 1 つのとき ）
ORG_URL: str = ""

# 出力 CSV の列名 （ この順 ）
OUTPUT_COLUMNS: tuple[str, ...] = (
    "オブジェクト",
    "オブジェクト表示名",
    "項目API名",
    "項目表示名",
    "型",
    "参照先オブジェクト",
    "リレーション名",
    "カスタム",
    "選択肢",
)


def main(
    *,
    object_names: list[str] | None = None,
    org_url: str | None = None,
    site_for: SiteFor | None = None,
) -> int:
    """オブジェクトの項目定義を取り直す。

    Args:
        object_names: 取りたい API 名のリスト。 ``None`` ならファイル先頭の
            ``OBJECT_NAMES`` を使う。
        org_url: 組織の URL。 ``None`` ならファイル先頭の ``ORG_URL`` を使う。
            空文字なら管理表から自動判定。
        site_for: URL → Salesforce サイトクラスの関数。 テスト用差し替え。

    Returns:
        0 … 全件取得成功
        1 … 1 件以上のオブジェクトが取得できなかった / 接続が致命エラー
        2 … OBJECT_NAMES が空 / ORG_URL 未指定で組織が一意に決まらない
    """
    if object_names is None:
        object_names = list(OBJECT_NAMES)
    if org_url is None:
        org_url = ORG_URL

    if not object_names:
        print("OBJECT_NAMES に API 名を書いてください")
        return 2

    if site_for is None:
        from comken.toolbox.salesforce.sites import site_for as default_site_for

        site_for = default_site_for
    assert site_for is not None  # 上の分岐で必ず代入される

    base_settings = load_settings()

    target_org, connection_url = _resolve_org(org_url, base_settings, site_for)
    if connection_url is None:
        # 0 組織・複数組織で既にメッセージを出して終了
        return 2

    # 出力先 （ 本体の output/ を汚さず single/ に隔離 ）
    output_dir = base_settings.output_dir / "single"
    output_dir.mkdir(parents=True, exist_ok=True)

    # キャッシュは読み込まず、 取得結果を書き込むためだけにストアを作る
    describe_store = DescribeStore.empty(base_settings.describe_csv_path)

    try:
        site_class = site_for(connection_url)
    except ComkenError as exc:
        print(f"Salesforce の接続先を決定できません: {exc}")
        return 1

    results: list[tuple[str, dict]] = []
    failed_count = 0

    try:
        with site_class() as client:
            for obj_name in object_names:
                outcome = _fetch_one_object(client, target_org, obj_name, describe_store)
                if outcome is None:
                    failed_count += 1
                    continue
                obj_name_ok, describe = outcome
                field_count = _count_fields(describe)
                print(f"取得: {obj_name_ok}（項目 {field_count} 件）")
                results.append((obj_name_ok, describe))
    except SalesforceRequestError as exc:
        print(f"Salesforce への接続でエラー（HTTP {exc.status_code}）: {exc.detail}")
        return 1
    except SalesforceError as exc:
        print(f"Salesforce エラー: {exc}")
        return 1

    # 出力 CSV を毎回作り直す
    _write_output_csv(output_dir / "オブジェクトの項目.csv", results)

    return 1 if failed_count else 0


def _resolve_org(
    org_url: str,
    settings: object,
    site_for: SiteFor,
) -> tuple[str, str | None]:
    """接続先の組織と ``site_for`` に渡す URL を決める。

    Returns:
        ``(org, url)`` のタプル。 ``url`` が ``None`` のときは 「 組織が
        0 種類・複数種類 」 で既にエラーメッセージを画面に出した状態。
    """
    if org_url and org_url.strip():
        target_org = org_from_url(org_url)
        return target_org, org_url

    master_path = settings.master_xlsx_path
    entries = read_master(master_path)
    orgs: set[str] = set()
    representative_url: dict[str, str] = {}
    for entry in entries:
        url = entry.url.strip()
        if not url:
            continue
        try:
            site_for(url)
        except ComkenError:
            continue
        org = org_from_url(url)
        orgs.add(org)
        representative_url.setdefault(org, url)

    if not orgs:
        print("管理表から組織が見つかりません: []")
        print("ORG_URL に組織の URL を書いてください")
        return "", None
    if len(orgs) > 1:
        sorted_orgs = sorted(orgs)
        print(f"管理表に複数の組織があります: {sorted_orgs}")
        print("ORG_URL に組織の URL を書いてください")
        return "", None

    target_org = next(iter(orgs))
    return target_org, representative_url[target_org]


def _fetch_one_object(
    client: Any,
    org: str,
    obj_name: str,
    describe_store: DescribeStore,
) -> tuple[str, dict] | None:
    """1 オブジェクトを API から取り、 キャッシュに書いて返す。

    Returns:
        ``(obj_name, describe)`` のタプル。 取得できなかった（ HTTP / ValueError ）
        ときは ``None``。
    """
    try:
        describe = client.describe_object(obj_name)
    except SalesforceRequestError as exc:
        if exc.status_code in FATAL_STATUS_CODES:
            # 401 / 403 は呼び出し側で全体失敗として拾うため再送出
            raise
        print(f"取得できませんでした: {obj_name}（HTTP {exc.status_code}: {exc.detail}）")
        return None
    except ValueError as exc:
        print(f"取得できませんでした: {obj_name}（{exc}）")
        return None

    if not isinstance(describe, dict):
        describe = {}

    # キャッシュの upsert （ 既存値があれば上書き ）
    describe_store.record_api_success(org, obj_name, describe)
    return obj_name, describe


def _count_fields(describe: dict) -> int:
    """``describe`` の ``fields`` 件数。 取得できなければ 0。"""
    fields = describe.get("fields")
    if isinstance(fields, list):
        return len(fields)
    return 0


def _format_picklist_active(field: dict) -> str:
    """``field`` の ``picklistValues`` から active な ``value`` を ``|`` 区切りで返す。"""
    picklist_raw = field.get("picklistValues")
    if not isinstance(picklist_raw, list):
        return ""
    active: list[str] = []
    for entry in picklist_raw:
        if not isinstance(entry, dict):
            continue
        if not entry.get("active"):
            continue
        value = entry.get("value")
        if isinstance(value, str) and value:
            active.append(value)
    return "|".join(active)


def _build_output_rows(
    results: list[tuple[str, dict]],
) -> list[dict[str, str]]:
    """取れた describe 群から出力 CSV の行 dict を作る。

    並びは ``results`` の順、 各 describe の ``fields`` は **出現順** を保つ。
    """
    rows: list[dict[str, str]] = []
    for obj_name, describe in results:
        obj_label_raw = describe.get("label")
        obj_label = obj_label_raw if isinstance(obj_label_raw, str) else ""
        fields = describe.get("fields")
        if not isinstance(fields, list):
            continue
        for field in fields:
            if not isinstance(field, dict):
                continue
            reference_to = field.get("referenceTo")
            reference_text = ""
            if isinstance(reference_to, list) and reference_to:
                reference_text = "|".join(
                    str(item) for item in reference_to if isinstance(item, str) and item
                )
            relationship_name = field.get("relationshipName")
            relationship_text = relationship_name if isinstance(relationship_name, str) else ""
            custom_raw = field.get("custom")
            custom_mark = "○" if isinstance(custom_raw, bool) and custom_raw else ""
            rows.append(
                {
                    "オブジェクト": obj_name,
                    "オブジェクト表示名": obj_label,
                    "項目API名": _coerce_str(field.get("name")),
                    "項目表示名": _coerce_str(field.get("label")),
                    "型": _coerce_str(field.get("type")),
                    "参照先オブジェクト": reference_text,
                    "リレーション名": relationship_text,
                    "カスタム": custom_mark,
                    "選択肢": _format_picklist_active(field),
                }
            )
    return rows


def _coerce_str(value: object) -> str:
    """``None`` を空文字に、 他は ``str`` にする。"""
    if value is None:
        return ""
    return str(value)


def _write_output_csv(path, results: list[tuple[str, dict]]) -> None:
    """出力 CSV を毎回作り直す。 取れたオブジェクトが無ければ見出しだけ書く。"""
    rows = _build_output_rows(results)
    path.parent.mkdir(parents=True, exist_ok=True)
    with CSV(path, columns=list(OUTPUT_COLUMNS)) as csv:
        csv.replace(Table(list(OUTPUT_COLUMNS), rows))


if __name__ == "__main__":
    from comken import comken_logger

    comken_logger.setup_local_logging()
    sys.exit(main())
