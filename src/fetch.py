"""ID ごとに describe を取って、 ID ごとの JSON に書く。

流れ（IDごと）:

1. 管理番号 → 管理表の行（無ければ呼び出し側の ``cli`` でエラー）
2. URL から ``site_for(url)`` で接続先を決め、 ``with site() as client:`` で開く
3. ``metadata = client.report.describe(report_id)`` （レポートの describe）
4. ``主オブジェクト名 = metadata["reportMetadata"]["reportType"]["type"]``
   ``client.describe_object(主オブジェクト名)`` が HTTP エラー
   （``SalesforceRequestError``）や ``ValueError`` になったら、 主オブジェクトは
   「特定できず」として警告に残し、 レポート describe だけで続ける。 401 /
   403 は握りつぶさず、 その ID の失敗にする
5. 関連オブジェクト（参照先を 1 段）: 主オブジェクトの describe の ``fields`` の
   うち、 ``referenceTo`` が空でなく ``relationshipName`` がある項目の
   ``referenceTo`` （ リスト。 ポリモーフィックなら複数 ）を集め、 重複を除き、
   主オブジェクト自身は除く。 件数は ``config.ini`` の ``[LIMITS] RELATED_MAX``
   （ 既定 40 ）で打ち切り、 超えた名前は警告に残す
6. ``client.report.describe_fields_with_object_status(metadata)`` で列対応表を取る

出力（ ``[FILES] OUTPUT_DIR`` の ``{管理番号}.json`` ）は SOQL 組立に必要な
**構造だけ** を書く:

- レポートの ``reportMetadata`` は ``REPORT_METADATA_KEYS`` にあるキーだけを
  **そのまま** 残す（ SELECT / WHERE / 日付条件 / サブクエリ / GROUP BY /
  集計 / ORDER BY / 件数 / 独自グループ / 独自計算式に必要なもの ）
- レポートの ``reportExtendedMetadata`` は ``detailColumnInfo`` /
  ``groupingColumnInfo`` / ``aggregateColumnInfo`` の 3 キーだけ残す
- オブジェクトは ``name`` / ``label`` / ``custom`` / ``fields`` だけ
- 項目は ``name`` / ``label`` / ``type`` / ``custom`` / ``referenceTo`` /
  ``relationshipName`` / ``picklist`` / ``picklistTotal`` の 8 キーだけ

``describe`` の **原本** は JSON に残さない。 落としたキーの名前は
``report.droppedKeys`` に**出現順で**入れる（ 最初の実行で、 必要なものを
落としていないか確かめる用 ）。 一時ファイル経由で書き込む
（ ``comken.core.files.atomic_write`` ）。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from comken.core.dates import now as comken_now
from comken.core.files import atomic_write
from comken.exceptions import (
    ComkenError,
    SalesforceError,
    SalesforceReportIDNotFoundError,
    SalesforceRequestError,
)
from comken.toolbox.salesforce.report import report_id_from_url

from src.master import MasterEntry
from src.settings import Settings

logger = logging.getLogger(__name__)

# ``describe_object`` が 401 / 403 を返したとき、 その ID 全体を失敗させる
# ステータスコード（ comken の ``ReportAPI.describe`` と同じく ）。
FATAL_STATUS_CODES: frozenset[int] = frozenset({401, 403})

# 接続先判定の既定実装（ comken の ``site_for`` ）。 テストでは差し替える。
SiteFor = Callable[[str], type]

# ``_slim_report`` で残す ``reportMetadata`` のキー（ SOQL 組立に必要なものだけ ）。
# SELECT・ WHERE・ 日付条件・ サブクエリ・ GROUP BY・ 集計・ ORDER BY・ 件数・
# 独自グループ・ 独自計算式に必要な情報を網羅する。
REPORT_METADATA_KEYS: tuple[str, ...] = (
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
)

# ``_slim_report`` で残す ``reportExtendedMetadata`` のキー。
# 3 つだけ（ ``detailColumnInfo`` / ``groupingColumnInfo`` /
# ``aggregateColumnInfo`` ）。
REPORT_EXTENDED_METADATA_KEYS: tuple[str, ...] = (
    "detailColumnInfo",
    "groupingColumnInfo",
    "aggregateColumnInfo",
)


@dataclass(frozen=True)
class FetchOutcome:
    """1 ID の取得結果。"""

    entry: MasterEntry
    status: str  # "ok" / "failed" / "dry-run"
    output_path: Path | None
    warnings: tuple[str, ...]
    error: str | None


def run_fetch(
    settings: Settings,
    entries: list[MasterEntry],
    *,
    dry_run: bool = False,
    site_for: SiteFor | None = None,
) -> list[FetchOutcome]:
    """``entries`` の各エントリについて、describe を取り JSON に書く。

    Args:
        settings: config.ini から読んだ設定。
        entries: 処理対象の管理表エントリ（既に ``--all`` 絞り込み済み）。
        dry_run: True なら接続せず、対象だけ表示用 ``FetchOutcome`` を返す。
        site_for: URL → Salesforce サイトクラスの関数。テスト用差し替え。

    Returns:
        入力と同じ順の ``FetchOutcome`` リスト。失敗 ID を含む個別では continue する。
    """
    if site_for is None:
        from comken.toolbox.salesforce.sites import site_for as default_site_for

        site_for = default_site_for

    assert site_for is not None  # 上の分岐で必ず代入される

    # 同じ実行内で重複する describe_object を 1 回にまとめる（User / Account 等）
    object_cache: dict[str, dict[str, Any]] = {}

    outcomes: list[FetchOutcome] = []
    for entry in entries:
        if dry_run:
            outcomes.append(
                FetchOutcome(
                    entry=entry,
                    status="dry-run",
                    output_path=settings.output_dir / f"{entry.key}.json",
                    warnings=(),
                    error=None,
                )
            )
            continue
        outcomes.append(_fetch_one(settings, entry, site_for=site_for, cache=object_cache))
    return outcomes


# ── JSON 用の slim ──────────────────────────────────────────────────────────
# JSON には describe の **原本** ではなく、 SOQL を組むのに必要な **構造だけ**
# （ レポートの ``reportMetadata`` / ``reportExtendedMetadata`` と、 オブジェクト
# の ``name`` / ``label`` / ``custom`` / ``fields`` ）を書く。 原本は JSON に
# 残さない。 下の 2 つの slim 関数は、 ``describe_object`` が返した dict（ と
# ``client.report.describe`` が返した dict ）を**書き換えずに**、 別の dict に
# コピーして絞る。 関連オブジェクト名の収集（ ``_collect_related_object_names`` ）
# は slim 前の ``fields[].referenceTo`` / ``relationshipName`` を使うので、
# slim は JSON 書く直前にだけ行う。
#
# レポート describe の絞り込みは **allowlist 方式** （ 「残すキーを選ぶ」 ）。
# 落とされたキーの名前は ``droppedKeys`` に**出現順で**入れる。 目的: 最初の
# 実行で本物の返り値を見て 「必要なものを落としていないか」 を確かめられる。
# ``droppedKeys`` のサブキーは 3 つ（ ``reportMetadata`` /
# ``reportExtendedMetadata`` / ``top`` ）常に存在する（ 1 つも落ちていない
# ときも空のリストとして ）。 一方入力に存在しないキーは「落とされた」のでは
# なく「元から無い」ので ``droppedKeys`` には入らない。


def _slim_report(metadata: object) -> dict[str, Any]:
    """``client.report.describe()`` の戻り値から SOQL 組立に必要な構造だけを返す。

    - ``reportMetadata``: ``REPORT_METADATA_KEYS`` にあるキーだけを**そのまま**
      残す。 値は元の参照のまま（ ``slimmed[key] is metadata[key]`` ）
    - ``reportExtendedMetadata``: ``REPORT_EXTENDED_METADATA_KEYS`` の 3 キー
      だけを**そのまま**残す
    - それ以外のトップレベルキー（ ``reportTypeMetadata`` — そのレポートタイプで
      使える全列の一覧で巨大、 ``attributes``、 グラフ・表示・フォルダ・説明など
      の管理用キー）はすべて落とす
    - 落としたキーの名前は ``droppedKeys`` に**出現順で**入れる
    - 入力が dict でない／ ``reportMetadata`` と ``reportExtendedMetadata`` が
      無い／ dict でない場合は、 例外を上げず省略する（ そのキーは droppedKeys
      には入らない ）
    - 入力の dict は書き換えない（ 別 dict にコピー ）
    """
    dropped: dict[str, list[str]] = {
        "reportMetadata": [],
        "reportExtendedMetadata": [],
        "top": [],
    }
    if not isinstance(metadata, dict):
        return {"droppedKeys": dropped}

    result: dict[str, Any] = {}

    report_metadata_raw = metadata.get("reportMetadata")
    if isinstance(report_metadata_raw, dict):
        slimmed_metadata: dict[str, Any] = {}
        for key, value in report_metadata_raw.items():
            if key in REPORT_METADATA_KEYS:
                slimmed_metadata[key] = value
            else:
                dropped["reportMetadata"].append(key)
        result["reportMetadata"] = slimmed_metadata

    report_extended_raw = metadata.get("reportExtendedMetadata")
    if isinstance(report_extended_raw, dict):
        slimmed_extended: dict[str, Any] = {}
        for key, value in report_extended_raw.items():
            if key in REPORT_EXTENDED_METADATA_KEYS:
                slimmed_extended[key] = value
            else:
                dropped["reportExtendedMetadata"].append(key)
        result["reportExtendedMetadata"] = slimmed_extended

    # トップレベル: ``reportMetadata`` と ``reportExtendedMetadata`` 以外を落とす
    for key in metadata:
        if key in ("reportMetadata", "reportExtendedMetadata"):
            continue
        dropped["top"].append(key)

    result["droppedKeys"] = dropped
    return result


def _slim_object(describe: object) -> dict[str, Any]:
    """``client.describe_object()`` の戻り値から、SOQL 組立に必要な構造だけを返す。

    - 残す: ``name`` / ``label`` / ``custom`` / ``fields``
    - 落とす: ``childRelationships`` / ``recordTypeInfos`` / ``urls`` /
      ``supportedScopes`` など
    - ``fields`` の各項目は次の8キーだけ（**この順**）:
      ``name`` / ``label`` / ``type`` / ``custom`` / ``referenceTo`` /
      ``relationshipName`` / ``picklist`` / ``picklistTotal``
    - ``picklist``: 元の ``picklistValues`` のうち ``active`` が真の ``value``
      を最大 30 件。 ``picklistTotal``: active な選択肢の総数（30 を超えたかが
      分かる）。 選択肢が無ければ ``[]`` と ``0``
    - 入力の dict は書き換えない（別 dict にコピー）
    """
    if not isinstance(describe, dict):
        return {}
    name_raw = describe.get("name")
    label_raw = describe.get("label")
    custom_raw = describe.get("custom")
    fields_raw = describe.get("fields")

    fields_list: list[Any] = fields_raw if isinstance(fields_raw, list) else []
    slimmed_fields: list[dict[str, Any]] = [
        _slim_field(field) for field in fields_list if isinstance(field, dict)
    ]

    return {
        "name": name_raw if isinstance(name_raw, str) else "",
        "label": label_raw if isinstance(label_raw, str) else "",
        "custom": custom_raw if isinstance(custom_raw, bool) else False,
        "fields": slimmed_fields,
    }


def _slim_field(field: dict[str, Any]) -> dict[str, Any]:
    """``fields`` の各項目を8キーに絞る。キーの**順番**もそのまま返す。"""
    picklist, picklist_total = _extract_picklist(field.get("picklistValues"))

    reference_to_raw = field.get("referenceTo")
    reference_to: list[Any] = reference_to_raw if isinstance(reference_to_raw, list) else []

    relationship_name_raw = field.get("relationshipName")
    relationship_name: str | None = (
        relationship_name_raw if isinstance(relationship_name_raw, str) else None
    )

    custom_raw = field.get("custom")
    custom = custom_raw if isinstance(custom_raw, bool) else False

    return {
        "name": field.get("name", ""),
        "label": field.get("label", ""),
        "type": field.get("type", ""),
        "custom": custom,
        "referenceTo": reference_to,
        "relationshipName": relationship_name,
        "picklist": picklist,
        "picklistTotal": picklist_total,
    }


def _extract_picklist(picklist_values: object) -> tuple[list[str], int]:
    """``picklistValues`` から ``active`` が真の ``value`` を取り出す。

    Returns:
        (最大 30 件の値リスト, active な選択肢の総数)
    """
    if not isinstance(picklist_values, list):
        return [], 0
    active: list[str] = []
    for entry in picklist_values:
        if not isinstance(entry, dict):
            continue
        if not entry.get("active"):
            continue
        value = entry.get("value")
        if isinstance(value, str) and value:
            active.append(value)
    return active[:30], len(active)


def _fetch_one(
    settings: Settings,
    entry: MasterEntry,
    *,
    site_for: SiteFor,
    cache: dict[str, dict[str, Any]],
) -> FetchOutcome:
    """1 ID の describe を取り、JSON に書く。失敗したら ``status="failed"`` を返す。"""
    warnings: list[str] = []
    output_path = settings.output_dir / f"{entry.key}.json"

    # URL → レポート ID
    try:
        report_id = report_id_from_url(entry.url)
    except SalesforceReportIDNotFoundError:
        return FetchOutcome(
            entry=entry,
            status="failed",
            output_path=None,
            warnings=(),
            error=(
                f"管理表の URL からレポート ID を取り出せません: {entry.url}"
                f"（管理番号 {entry.key}）"
            ),
        )

    # 接続先決定。``site_for`` が ``SalesforceError``（未登録組織）などの
    # ``ComkenError`` を出すと、その ID だけの失敗にする（他の ID は続ける）
    try:
        site_class = site_for(entry.url)
    except ComkenError as exc:
        return FetchOutcome(
            entry=entry,
            status="failed",
            output_path=None,
            warnings=(),
            error=f"Salesforce の接続先を決定できません: {exc}",
        )

    # 接続〜取得
    objects: dict[str, dict[str, Any]] = {}
    main_object_name: str | None = None
    metadata: dict[str, Any] = {}
    column_map: list[dict[str, Any]] = []

    try:
        with site_class() as client:
            try:
                metadata = client.report.describe(report_id)
            except SalesforceRequestError as exc:
                return FetchOutcome(
                    entry=entry,
                    status="failed",
                    output_path=None,
                    warnings=(),
                    error=(
                        f"レポート describe に失敗（HTTP {exc.status_code}）: {report_id}"
                        f"\n{exc.detail}"
                    ),
                )

            try:
                fields_table, object_error = client.report.describe_fields_with_object_status(
                    metadata
                )
            except SalesforceRequestError as exc:
                return FetchOutcome(
                    entry=entry,
                    status="failed",
                    output_path=None,
                    warnings=(),
                    error=(
                        f"列対応表の取得に失敗（HTTP {exc.status_code}）: {report_id}\n{exc.detail}"
                    ),
                )

            if object_error:
                warnings.append(object_error)
            column_map = fields_table.to_rows()

            main_object_name = _main_object_name(metadata)

            # 主オブジェクト
            if main_object_name:
                describe = _fetch_object_cached(client, main_object_name, cache, warnings)
                if describe is not None:
                    objects[main_object_name] = describe
                # describe が None の場合は cache 側に警告が残っているので追加しない

            # 関連オブジェクト
            if main_object_name and main_object_name in objects:
                related_names = _collect_related_object_names(
                    objects[main_object_name], exclude=main_object_name
                )
                related_names, skipped = _apply_related_limit(related_names, settings.related_max)
                if skipped:
                    warnings.append(
                        f"関連オブジェクトが {len(related_names) + len(skipped)} 件あり"
                        f"上限 {settings.related_max} を超えたため、"
                        f"次の {len(skipped)} 件は取得しません: {', '.join(skipped)}"
                    )
                for related_name in related_names:
                    describe = _fetch_object_cached(client, related_name, cache, warnings)
                    if describe is not None:
                        objects[related_name] = describe
    except SalesforceRequestError as exc:
        # 接続失敗・認証失敗・致命 HTTP
        return FetchOutcome(
            entry=entry,
            status="failed",
            output_path=None,
            warnings=(),
            error=(f"Salesforce への接続でエラー（HTTP {exc.status_code}）: {exc.detail}"),
        )
    except SalesforceError as exc:
        return FetchOutcome(
            entry=entry,
            status="failed",
            output_path=None,
            warnings=(),
            error=f"Salesforce エラー: {exc}",
        )

    main_object = main_object_name if main_object_name and main_object_name in objects else None
    if main_object_name and main_object_name not in objects:
        warnings.append(
            f"主オブジェクト {main_object_name} の describe に失敗したため"
            f"関連オブジェクトは取得していません"
        )

    # JSON に書く直前に slim する。 ``metadata`` 自体と ``objects`` は
    # ``_collect_related_object_names`` （関連オブジェクト名の収集）で
    # 原本の ``fields[].referenceTo`` / ``relationshipName`` を使うので、
    # ここでは別 dict にコピーして絞る（原本の dict は壊さない）。
    payload = {
        "管理番号": entry.key,
        "概要": entry.summary,
        "レポートID": report_id,
        "URL": entry.url,
        "取得日時": comken_now().isoformat(timespec="seconds"),
        "主オブジェクト": main_object,
        "report": _slim_report(metadata),
        "objects": {name: _slim_object(describe) for name, describe in objects.items()},
        "column_map": column_map,
        "warnings": warnings,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with atomic_write(output_path) as tmp:
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        # フォルダ・権限・ディスクの失敗。その ID だけの失敗にして残りは続ける
        return FetchOutcome(
            entry=entry,
            status="failed",
            output_path=None,
            warnings=tuple(warnings),
            error=f"JSON の書き出しに失敗しました: {output_path}\n{exc}",
        )

    return FetchOutcome(
        entry=entry,
        status="ok",
        output_path=output_path,
        warnings=tuple(warnings),
        error=None,
    )


def _main_object_name(metadata: dict[str, Any]) -> str | None:
    """``metadata["reportMetadata"]["reportType"]["type"]`` を取り出す。

    レポートタイプ（カスタムレポートタイプなど）は ``type`` を持たないことが
    あるため、その場合は ``None``。comken の ``ReportAPI.describe`` の戻り値
    と同じ読み方。
    """
    if not isinstance(metadata, dict):
        return None
    report_metadata = metadata.get("reportMetadata")
    if not isinstance(report_metadata, dict):
        return None
    report_type = report_metadata.get("reportType")
    if not isinstance(report_type, dict):
        return None
    name = report_type.get("type")
    return name if isinstance(name, str) and name else None


def _collect_related_object_names(describe: dict[str, Any], *, exclude: str | None) -> list[str]:
    """主オブジェクトの ``fields`` から、参照先のオブジェクト名（1 段）を集める。

    - ``referenceTo`` が空でなく ``relationshipName`` がある項目だけ対象
    - ポリモーフィックな参照は ``referenceTo`` の複数エントリすべてを含める
    - 出現順を保つ（重複は先勝ち）
    - ``exclude`` と一致する名前は除く（主オブジェクト自身）
    """
    fields = describe.get("fields") if isinstance(describe, dict) else None
    if not isinstance(fields, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for field in fields:
        if not isinstance(field, dict):
            continue
        relationship_name = field.get("relationshipName")
        if not relationship_name:
            continue
        reference_to = field.get("referenceTo")
        if not isinstance(reference_to, list) or not reference_to:
            continue
        for name in reference_to:
            if not isinstance(name, str) or not name:
                continue
            if exclude is not None and name == exclude:
                continue
            if name in seen:
                continue
            seen.add(name)
            result.append(name)
    return result


def _apply_related_limit(names: list[str], limit: int) -> tuple[list[str], list[str]]:
    """先頭 ``limit`` 件を返し、残りをスキップ対象として返す。"""
    if limit < 0:
        return list(names), []
    if len(names) <= limit:
        return list(names), []
    return names[:limit], names[limit:]


def _fetch_object_cached(
    client: Any,
    name: str,
    cache: dict[str, dict[str, Any]],
    warnings: list[str],
) -> dict[str, Any] | None:
    """``client.describe_object(name)`` を呼び、結果をキャッシュする。

    戻り値:
        成功時は describe の dict。失敗時（HTTP エラー / ``ValueError``）は
        ``None``。401 / 403 は ``SalesforceRequestError`` をそのまま上位へ
        送出する（呼び出し側で ID 全体を失敗扱い）。
    """
    if name in cache:
        cached = cache[name]
        describe = cached.get("describe")
        warning = cached.get("warning")
        if warning:
            warnings.append(warning)
        return describe if isinstance(describe, dict) else None

    try:
        describe = client.describe_object(name)
    except SalesforceRequestError as exc:
        if exc.status_code in FATAL_STATUS_CODES:
            raise
        warning = f"オブジェクト {name} の describe に失敗（HTTP {exc.status_code}: {exc.detail}）"
        cache[name] = {"describe": None, "warning": warning}
        warnings.append(warning)
        return None
    except ValueError as exc:
        warning = f"オブジェクト {name} の describe に失敗: {exc}"
        cache[name] = {"describe": None, "warning": warning}
        warnings.append(warning)
        return None

    if not isinstance(describe, dict):
        # comken の ``describe_object`` は dict 以外なら空 dict を返すが、
        # 念のため空 dict と同じ扱いにする
        describe = {}

    cache[name] = {"describe": describe, "warning": None}
    return describe
