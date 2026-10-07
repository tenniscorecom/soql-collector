"""ID ごとに describe を取って、ID ごとの JSON に書く。

流れ（IDごと）:

1. 管理番号 → 管理表の行（無ければ呼び出し側の ``cli`` でエラー）
2. URL から ``site_for(url)`` で接続先を決め、 ``with site() as client:`` で開く
3. ``metadata = client.report.describe(report_id)`` （レポートの describe）
4. ``主オブジェクト名 = metadata["reportMetadata"]["reportType"]["type"]``
   ``client.describe_object(主オブジェクト名)`` が HTTP エラー
   （``SalesforceRequestError``）や ``ValueError`` になったら、主オブジェクトは
   「特定できず」として警告に残し、レポート describe だけで続ける。401 / 403 は
   握りつぶさず、その ID の失敗にする
5. 関連オブジェクト（参照先を 1 段）: 主オブジェクトの describe の ``fields`` の
   うち、``referenceTo`` が空でなく ``relationshipName`` がある項目の
   ``referenceTo``（リスト。ポリモーフィックなら複数）を集め、重複を除き、
   主オブジェクト自身は除く。件数は ``config.ini`` の ``[LIMITS] RELATED_MAX``
   （既定 40）で打ち切り、超えた名前は警告に残す
6. ``client.report.describe_fields_with_object_status(metadata)`` で列対応表を取る

出力（``[FILES] OUTPUT_DIR`` の ``{管理番号}.json`` ）は一時ファイル経由で
書き込む（``comken.core.files.atomic_write``）。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from comken.core.dates import now as comken_now
from comken.core.files import atomic_write
from comken.exceptions import (
    ComkenError,
    SalesforceError,
    SalesforceReportIDNotFoundError,
    SalesforceRequestError,
)
from comken.toolbox.salesforce.report import report_id_from_url

from soql_collector.master import MasterEntry
from soql_collector.settings import Settings

logger = logging.getLogger(__name__)

# ``describe_object`` が 401 / 403 を返したとき、その ID 全体を失敗させる
# ステータスコード（comken の ``ReportAPI.describe`` と同じく）。
FATAL_STATUS_CODES: frozenset[int] = frozenset({401, 403})

# 接続先判定の既定実装（comken の ``site_for``）。テストでは差し替える。
SiteFor = Callable[[str], type]


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

    payload = {
        "管理番号": entry.key,
        "概要": entry.summary,
        "レポートID": report_id,
        "URL": entry.url,
        "取得日時": comken_now().isoformat(timespec="seconds"),
        "主オブジェクト": main_object,
        "report": metadata,
        "objects": objects,
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
