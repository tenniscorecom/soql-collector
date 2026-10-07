"""コマンドラインインターフェース。

サブコマンド:

- ``fetch [管理番号 ...] [--all] [--dry-run]``: 指定した管理番号（または「有効」な
  全件）を取得し、``[FILES] OUTPUT_DIR`` の ``{管理番号}.json`` に書く。続いて
  ``tables`` 相当の CSV を作り直す（ ``--dry-run`` のときは CSV を作らない）
- ``list``: 管理表の管理番号・概要・有効・レポート ID を表示する
- ``tables``: ``OUTPUT_DIR`` の ``*.json`` から ``対応表.csv`` / ``項目表.csv``
  を再生成する（ ``fetch`` の最後で自動実行もされる）

``main.py`` から呼ばれる。引数なしで起動すると対話メニュー。
"""

from __future__ import annotations

import argparse
import logging
import sys

from comken.toolbox.salesforce.report import report_id_from_url

from src.fetch import run_fetch
from src.master import filter_enabled, read_master
from src.settings import Settings, load_settings
from src.tables import run_tables

logger = logging.getLogger(__name__)

# 対話メニューの選択肢（番号 → 説明）。
MENU_COMMANDS = (
    ("1", "管理番号を指定して取る", "fetch"),
    ("2", "有効なものをすべて取る", "fetch --all"),
    ("3", "一覧を見る", "list"),
    ("4", "CSV を作り直す", "tables"),
    ("0", "終了", ""),
)


def build_parser() -> argparse.ArgumentParser:
    """argparse の ``ArgumentParser`` を組み立てる。"""
    parser = argparse.ArgumentParser(
        prog="soql-collector",
        description="レポート管理表の ID ごとに Salesforce の describe を取得し JSON に保存します",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    fetch_cmd = commands.add_parser("fetch", help="管理番号ごとに describe を取得して JSON に書く")
    fetch_cmd.add_argument("keys", nargs="*", help="管理番号（例: 1001 1002）。省略時は --all 必須")
    fetch_cmd.add_argument(
        "--all",
        action="store_true",
        help="管理表の「有効」が ○ のものすべてを対象にする",
    )
    fetch_cmd.add_argument(
        "--dry-run",
        action="store_true",
        help="接続せず、対象の管理番号・概要・レポートID・出力先を表示するだけ",
    )

    commands.add_parser("list", help="管理表の一覧を表示する")

    commands.add_parser(
        "tables",
        help="OUTPUT_DIR の JSON から 対応表.csv / 項目表.csv を作り直す",
    )

    return parser


def _prompt_argv() -> list[str]:
    """対話メニューで選んだ結果の argv を組み立てる。"""
    while True:
        logger.info("=== soql-collector ===")
        for number, description, _ in MENU_COMMANDS:
            logger.info("%s. %s", number, description)
        choice = input("番号を選択してください: ").strip()
        if choice == "0" or choice == "":
            return ["__exit__"]
        for number, _, command in MENU_COMMANDS:
            if choice == number:
                if command.startswith("fetch --all"):
                    return ["fetch", "--all"]
                if command == "fetch":
                    keys_text = input("管理番号を空白区切りで入力（Enterで全件）: ").strip()
                    if keys_text:
                        return ["fetch", *keys_text.split()]
                    return ["fetch", "--all"]
                if command == "list":
                    return ["list"]
                if command == "tables":
                    return ["tables"]
        logger.error("番号が不正です: %s", choice)


def run_interactive() -> int:
    """引数なしで起動したときの対話メニュー。"""
    while True:
        argv = _prompt_argv()
        if argv == ["__exit__"]:
            return 0
        code = main(argv)
        if code != 0:
            logger.error("[終了コード %d]", code)


def main(argv: list[str] | None = None) -> int:
    """CLI の入口。終了コードは「失敗 ID が 1 件でもあれば 1」。"""
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(message)s")
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        return run_interactive()
    args = build_parser().parse_args(argv)
    settings = load_settings()
    if args.command == "list":
        return _cmd_list(settings)
    if args.command == "fetch":
        return _cmd_fetch(settings, args)
    if args.command == "tables":
        return _cmd_tables(settings)
    return 2


def _cmd_list(settings: Settings) -> int:
    """管理表の管理番号・概要・有効・レポート ID を表示する。"""
    entries = read_master(settings.master_xlsx_path)
    for entry in entries:
        try:
            report_id = report_id_from_url(entry.url)
        except Exception:
            report_id = "(URL 不正)"
        logger.info(
            "%s\t%s\t%s\t%s",
            entry.key,
            "○" if entry.enabled else "×",
            entry.summary,
            report_id,
        )
    return 0


def _cmd_fetch(settings: Settings, args: argparse.Namespace) -> int:
    """``fetch`` サブコマンドの本体。"""
    if args.dry_run:
        entries, errors = _resolve_entries(settings, args.keys, all_flag=args.all)
        if errors:
            for key, message in errors.items():
                logger.error("管理番号 %s: %s", key, message)
            return 2
        if not entries:
            logger.error("対象のエントリがありません")
            return 2
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
        return 0

    if not args.keys and not args.all:
        logger.error("管理番号を 1 件以上指定するか、 --all を付けてください")
        return 2

    entries, errors = _resolve_entries(settings, args.keys, all_flag=args.all)
    for key, message in errors.items():
        logger.error("管理番号 %s: %s", key, message)

    if not entries and errors:
        return 2
    if not entries:
        logger.error("対象のエントリがありません")
        return 2

    outcomes = run_fetch(settings, entries)
    failed_keys = []
    ok_keys: list[str] = []
    for outcome in outcomes:
        if outcome.status == "ok":
            ok_keys.append(outcome.entry.key)
            logger.info(
                "[ok] 管理番号=%s 出力先=%s 警告=%d 件",
                outcome.entry.key,
                outcome.output_path,
                len(outcome.warnings),
            )
            for warning in outcome.warnings:
                logger.warning("  - %s", warning)
        elif outcome.status == "failed":
            failed_keys.append(outcome.entry.key)
            logger.error(
                "[failed] 管理番号=%s: %s",
                outcome.entry.key,
                outcome.error,
            )

    # 取れた ID についてだけ per-ID CSV を作り直し、 対応表.csv / 項目表.csv は
    # ``OUTPUT_DIR`` の全 JSON から作り直す（ ``run_tables`` 内で両方やる）。
    # 失敗 ID の既存 CSV はそのまま残る（上書き・削除しない）
    if ok_keys:
        try:
            run_tables(settings.output_dir, only_keys=ok_keys)
        except Exception as exc:
            # CSV 生成の失敗は fetch 全体の失敗にはしない（ JSON は書けたので）
            logger.error("CSV の生成に失敗しました: %s", exc)

    return 1 if failed_keys else 0


def _cmd_tables(settings: Settings) -> int:
    """``tables`` サブコマンドの本体。 ``OUTPUT_DIR`` が空なら終了コード 1。"""
    if not settings.output_dir.exists() or not list(settings.output_dir.glob("*.json")):
        logger.error(
            "先に fetch を実行して %s に JSON を作ってください",
            settings.output_dir,
        )
        return 1
    run_tables(settings.output_dir)
    return 0


def _resolve_entries(
    settings: Settings, keys: list[str], *, all_flag: bool
) -> tuple[list, dict[str, str]]:
    """管理表を読み、指定分のエントリ（または有効全件）を返す。

    管理番号が重複している行や、管理表に無い管理番号は ``errors`` に積む
    （他の ID は止めない）。
    """
    entries = read_master(settings.master_xlsx_path)
    by_key: dict[str, list] = {}
    order: list[str] = []
    for entry in entries:
        if entry.key not in by_key:
            order.append(entry.key)
        by_key.setdefault(entry.key, []).append(entry)

    errors: dict[str, str] = {}

    if all_flag:
        selected = filter_enabled(entries)
        duplicates = [key for key, items in by_key.items() if len(items) > 1]
        for key in duplicates:
            errors[key] = "管理番号が重複しています"
        # --all のとき「×」の ID を個別指定に含めても、それは個別指定側で処理する
        return selected, errors

    selected: list = []
    seen: set[str] = set()
    for key in keys:
        if key in seen:
            errors.setdefault(key, "管理番号が重複しています")
            continue
        seen.add(key)
        matches = by_key.get(key)
        if not matches:
            errors[key] = (
                f"管理表に無い管理番号です（[FILES] MASTER_XLSX_PATH: {settings.master_xlsx_path}）"
            )
            continue
        if len(matches) > 1:
            errors[key] = "管理番号が重複しています"
            continue
        # 個別指定は「×」でも取得する
        selected.append(matches[0])

    return selected, errors
