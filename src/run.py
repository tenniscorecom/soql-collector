"""src/run.py — main.py から呼ばれる入口。

``実行.bat`` または ``python main.py`` から呼ばれると、 ``argv`` を
``sys.argv[1:]`` として受け取り、 管理表の「有効」列が ``○`` の行すべての
describe を取って JSON に書く。 個別に取りたいときは `config.ini` の
``[OBJECTS] NAMES`` に書く （組織は管理表の URL から決める）。

終了コード:

- 0 … 全件成功
- 1 … 1 件以上のレポート・オブジェクトが失敗した
- 2 … 引数が付いた / 管理表に「有効」が ○ の行が無い

`main.py` はこの ``run()`` の戻り値を ``SystemExit`` に流す。
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from comken.core.dates import now as comken_now
from comken.core.files import atomic_write
from comken.exceptions import ComkenError, SalesforceError, SalesforceRequestError
from comken.runtime import is_dry_run
from comken.toolbox.salesforce.report import report_id_from_url

from src.fetch import _slim_object, run_fetch
from src.master import MasterEntry, filter_enabled, read_master
from src.settings import Settings, load_settings
from src.tables import run_tables

logger = logging.getLogger(__name__)

# ``run_fetch`` の ``site_for`` と同じ型（テストで差し替えるため）
SiteFor = Callable[[str], type]


@dataclass(frozen=True)
class _ObjectOutcome:
    """個別オブジェクトの取得結果。"""

    name: str
    status: str  # "ok" / "failed"
    output_path: Path | None
    error: str | None


def run(argv: list[str] | None = None, *, site_for: SiteFor | None = None) -> int:
    """``実行.bat`` / RPA から呼ばれる入口。

    Args:
        argv: コマンドライン引数。 ``None`` なら ``sys.argv[1:]`` を使う。
            1 件でも指定されたら何もしないでエラー終了（終了コード 2）。
        site_for: URL → Salesforce サイトクラスの関数。テスト用差し替え。

    Returns:
        0 … 全件成功
        1 … 失敗した ID またはオブジェクトが 1 件以上ある
        2 … 引数エラー / 設定や対象の不足
    """
    if argv is None:
        argv = sys.argv[1:]
    if argv:
        logger.error("引数は使いません。取るレポートは管理表の『有効』列が ○ の行で決まります")
        return 2

    settings = load_settings()
    entries = filter_enabled(read_master(settings.master_xlsx_path))

    if is_dry_run():
        _log_dry_run(settings, entries)
        return 0

    if not entries and not settings.objects_names:
        logger.error("管理表に『有効』が ○ の行がありません")
        return 2

    # レポートの取得 （ ``object_cache`` を個別オブジェクトの取得と共有する ）
    object_cache: dict[str, dict[str, Any]] = {}
    ok_keys: list[str] = []
    failed_keys: list[str] = []

    if entries:
        for outcome in run_fetch(settings, entries, site_for=site_for, object_cache=object_cache):
            _log_outcome(outcome)
            if outcome.status == "ok":
                ok_keys.append(outcome.entry.key)
            elif outcome.status == "failed":
                failed_keys.append(outcome.entry.key)

    # 個別オブジェクトの取得
    object_failures = 0
    if settings.objects_names:
        outcomes = _run_objects(
            settings,
            site_for=site_for,
            cache=object_cache,
        )
        for outcome in outcomes:
            if outcome.status == "failed":
                object_failures += 1

    # CSV は取れた ID に関わらず作り直す （オブジェクトだけ取れたときも項目表を
    # 反映するため）。 取れた ID があるときは per-ID CSV をそのぶんだけ作る。
    try:
        run_tables(settings.output_dir, only_keys=ok_keys)
    except Exception as exc:
        # CSV の失敗は全体の失敗にはしない（ JSON は書けたので）
        logger.error("CSV の生成に失敗しました: %s", exc)

    return 1 if (failed_keys or object_failures) else 0


# ── 個別オブジェクト ─────────────────────────────────────────────────────


def _run_objects(
    settings: Settings,
    *,
    site_for: SiteFor | None,
    cache: dict[str, dict[str, Any]],
) -> list[_ObjectOutcome]:
    """``[OBJECTS] NAMES`` の各オブジェクトの describe を取って ``objects/`` に書く。

    戻り値の ``_ObjectOutcome`` には ``ok`` / ``failed`` が混ざり得る。
    ``failed`` は ``name`` 単位（ 401 / 403 以外 ）。
    """
    names = settings.objects_names

    if site_for is None:
        from comken.toolbox.salesforce.sites import site_for as default_site_for

        site_for = default_site_for
    assert site_for is not None

    # 組織の決定
    site_class, org_error = _resolve_object_site(settings, site_for=site_for)
    if org_error is not None or site_class is None:
        logger.error(org_error)
        return [
            _ObjectOutcome(name=name, status="failed", output_path=None, error=org_error)
            for name in names
        ]

    # 出力先
    objects_dir = settings.output_dir / "objects"

    result: list[_ObjectOutcome] = []
    try:
        with site_class() as client:
            for name in names:
                outcome = _fetch_one_object(
                    name,
                    client=client,
                    cache=cache,
                    objects_dir=objects_dir,
                )
                result.append(outcome)
                if outcome.status == "failed":
                    # 401 / 403 は ``_fetch_one_object`` が ``SalesforceRequestError``
                    # を上位へ送出する（残りの名前は取らない）
                    pass
    except SalesforceRequestError as exc:
        # 接続失敗・認証失敗（ 401 / 403 ）
        fatal_message = f"Salesforce への接続でエラー（HTTP {exc.status_code}）: {exc.detail}"
        logger.error(fatal_message)
        return [
            _ObjectOutcome(name=name, status="failed", output_path=None, error=fatal_message)
            for name in names
        ]
    except SalesforceError as exc:
        message = f"Salesforce エラー: {exc}"
        logger.error(message)
        return [
            _ObjectOutcome(name=name, status="failed", output_path=None, error=message)
            for name in names
        ]

    return result


def _resolve_object_site(settings: Settings, *, site_for: SiteFor) -> tuple[Any, str | None]:
    """``[OBJECTS]`` の ``ORG_ID`` から組織の Salesforce サイトクラスを決める。

    - ``ORG_ID`` が指定されている場合: 管理表のその管理番号の行の URL を使う
      （管理表に無い管理番号・URL が空ならエラー）
    - ``ORG_ID`` が無い場合: 管理表の **全行** （「有効」が × の行も含む） の
      URL を ``site_for`` にかけ、組織が 1 種類だけならそれを使う。 2 種類以上
      ならエラー、 0 種類もエラー

    戻り値の ``site_class`` は ``with site_class() as client:`` の形で使える
    もの。 本番では ``type[SalesforceBase]`` （ クラス ） が返り、 テストでは
    ``__call__`` を持つインスタンスが返ることもある（ どちらでも ``with
    site_class() as client:`` 経由でクライアントを取得できる ）。
    """
    entries = read_master(settings.master_xlsx_path)

    if settings.objects_org_id is not None:
        target = next(
            (e for e in entries if e.key == settings.objects_org_id),
            None,
        )
        if target is None:
            return None, (
                f"[OBJECTS] ORG_ID の管理番号 {settings.objects_org_id} は管理表にありません"
            )
        if not target.url:
            return None, (f"[OBJECTS] ORG_ID の管理番号 {settings.objects_org_id} のURL が空です")
        try:
            return site_for(target.url), None
        except ComkenError as exc:
            return None, f"[OBJECTS] 組織の決定に失敗しました: {exc}"

    # ORG_ID 無し。 全行（× の行も含む）の URL で組織を集める。
    # ``site_for`` はテストでインスタンスを返すことがあるので、
    # 組織キーは ``type()`` で正規化し、 実際の ``site_class`` は
    # 最初に見つかったものを保持する（ ``with site_class() as client:`` の
    # 形で使える形 ： クラス）
    by_site: dict[type, Any] = {}
    for entry in entries:
        if not entry.url:
            continue
        try:
            site_class = site_for(entry.url)
        except ComkenError:
            # 未登録組織の URL は飛ばす
            continue
        key = site_class if isinstance(site_class, type) else type(site_class)
        by_site.setdefault(key, site_class)

    if not by_site:
        return None, "管理表に取れる URL がありません"

    if len(by_site) > 1:
        org_names = ", ".join(sorted(key.__name__ for key in by_site))
        return None, (
            f"管理表に複数の組織があります: {org_names}\n"
            "[OBJECTS] ORG_ID に管理番号を書いてください"
        )

    return next(iter(by_site.values())), None


def _fetch_one_object(
    name: str,
    *,
    client: Any,
    cache: dict[str, dict[str, Any]],
    objects_dir: Path,
) -> _ObjectOutcome:
    """1 オブジェクトの describe を取って ``objects/{name}.json`` に書く。

    - 401 / 403 の ``SalesforceRequestError`` はそのまま上位へ送出する
      （残りの名前は取らない）
    - その他の ``SalesforceRequestError``・ ``ValueError`` は ``failed`` にして
      その名前だけ失敗とする
    """
    output_path = objects_dir / f"{name}.json"

    cached_entry = cache.get(name)
    if cached_entry is not None:
        describe = cached_entry.get("describe") if isinstance(cached_entry, dict) else None
    else:
        try:
            describe = client.describe_object(name)
        except SalesforceRequestError as exc:
            if exc.status_code in {401, 403}:
                raise
            message = (
                f"[failed] オブジェクト {name} の describe に失敗"
                f"（HTTP {exc.status_code}: {exc.detail}）"
            )
            logger.error(message)
            cache[name] = {"describe": None, "warning": message}
            return _ObjectOutcome(name=name, status="failed", output_path=None, error=message)
        except ValueError as exc:
            message = f"[failed] オブジェクト {name}: {exc}"
            logger.error(message)
            cache[name] = {"describe": None, "warning": message}
            return _ObjectOutcome(name=name, status="failed", output_path=None, error=message)
        if not isinstance(describe, dict):
            describe = {}
        cache[name] = {"describe": describe, "warning": None}

    if not isinstance(describe, dict):
        message = f"[failed] オブジェクト {name}: describe を取得できませんでした"
        logger.error(message)
        return _ObjectOutcome(name=name, status="failed", output_path=None, error=message)

    payload = {
        "取得日時": comken_now().isoformat(timespec="seconds"),
        "オブジェクト": name,
        "object": _slim_object(describe),
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with atomic_write(output_path) as tmp:
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        message = (
            f"[failed] オブジェクト {name}: JSON の書き出しに失敗しました: {output_path}\n{exc}"
        )
        logger.error(message)
        return _ObjectOutcome(name=name, status="failed", output_path=None, error=message)

    return _ObjectOutcome(name=name, status="ok", output_path=output_path, error=None)


# ── ログ ────────────────────────────────────────────────────────────────


def _log_outcome(outcome: Any) -> None:
    """``FetchOutcome`` を ``[ok]`` / ``[failed]`` でログに出す。"""
    if outcome.status == "ok":
        logger.info(
            "[ok] 管理番号=%s 出力先=%s 警告=%d 件",
            outcome.entry.key,
            outcome.output_path,
            len(outcome.warnings),
        )
        for warning in outcome.warnings:
            logger.warning("  - %s", warning)
    elif outcome.status == "failed":
        logger.error(
            "[failed] 管理番号=%s: %s",
            outcome.entry.key,
            outcome.error,
        )


def _log_dry_run(settings: Settings, entries: list[MasterEntry]) -> None:
    """dry-run のログを出す（ 接続も書き込みもしない ）。"""
    for entry in entries:
        try:
            report_id = report_id_from_url(entry.url)
        except Exception:
            report_id = "(URL 不正)"
        logger.info(
            "[dry-run] 管理番号=%s 概要=%s レポートID=%s 出力先=%s",
            entry.key,
            entry.summary,
            report_id,
            settings.output_dir / f"{entry.key}.json",
        )
    for name in settings.objects_names:
        logger.info(
            "[dry-run] オブジェクト=%s 出力先=%s",
            name,
            settings.output_dir / "objects" / f"{name}.json",
        )
