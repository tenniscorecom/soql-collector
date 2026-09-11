"""コマンドラインインターフェース。"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from soql_collector.service import (
    STATUSES,
    build_saved_report,
    collect_one,
    export_csv,
    import_master,
    report_id_from_text,
)
from soql_collector.settings import load_settings
from soql_collector.store import WorkbookStore, now_text

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="soql_collector", description="Salesforce レポート情報を蓄積して SOQL 化を支援します"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("import-master", help="管理表を取り込む")
    command.add_argument("path", type=Path)
    command = commands.add_parser("collect", help="Report Describe を取得する")
    command.add_argument("--url")
    command.add_argument("--master", action="store_true")
    command.add_argument("--org")
    command.add_argument("--dry-run", action="store_true")
    command = commands.add_parser("show", help="レポート詳細を表示する")
    command.add_argument("target")
    command = commands.add_parser("list", help="蓄積済み一覧を表示する")
    command.add_argument("--status", choices=sorted(STATUSES))
    command.add_argument("--org")
    command = commands.add_parser("note", help="メモを追記する")
    command.add_argument("target")
    command.add_argument("memo", nargs="+")
    command = commands.add_parser("mark", help="状態を変更する")
    command.add_argument("target")
    command.add_argument("status", choices=sorted(STATUSES))
    command = commands.add_parser("confirm-mapping", help="列マッピングを確認済みにする")
    command.add_argument("site")
    command.add_argument("report_type")
    command.add_argument("column_key")
    command.add_argument("field_api_name")
    command.add_argument("field_type")
    command = commands.add_parser("build-soql", help="SOQL ドラフトを組み立てる")
    command.add_argument("target")
    command.add_argument("--apply", action="store_true")
    command = commands.add_parser("export-csv", help="旧形式 CSV を出力する")
    command.add_argument("path", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = build_parser().parse_args(argv)
    settings = load_settings()
    store = WorkbookStore(settings.excel_path)
    if args.command == "import-master":
        logger.info("%d 件取り込みました", import_master(store, args.path))
        return 0
    if args.command == "collect":
        urls = [args.url] if args.url else [row["URL"] for row in store.read_rows("Master")]
        if not urls:
            logger.error("対象 URL がありません")
            return 2
        for url in urls:
            collect_one(settings, store, url, args.org, args.dry_run)
        return 0
    if args.command == "list":
        rows = [
            row
            for row in store.read_rows("Reports")
            if (not args.status or row["状態"] == args.status)
            and (not args.org or row["組織"] == args.org)
        ]
        for row in rows:
            logger.info("%s\t%s\t%s", row["レポートID"], row["状態"], row["概要"])
        return 0
    report_id = report_id_from_text(getattr(args, "target", ""))
    if args.command == "show":
        report = store.find_report(report_id)
        if report is None:
            logger.error("未取得: %s", report_id)
            return 2
        logger.info("%s", report)
        for sheet in ("Filters", "Groupings", "Notes"):
            logger.info(
                "%s: %s",
                sheet,
                [row for row in store.read_rows(sheet) if row["レポートID"] == report_id],
            )
        return 0
    if args.command == "note":
        store.add_note(report_id, "memo", " ".join(args.memo))
        return 0
    if args.command == "mark":
        report = store.find_report(report_id)
        if report is None:
            logger.error("未取得: %s", report_id)
            return 2
        old = report["状態"]
        report["状態"] = args.status
        store.upsert("Reports", ("レポートID",), report)
        store.add_note(report_id, "status_change", f"{old} -> {args.status}")
        return 0
    if args.command == "confirm-mapping":
        existing = next(
            (
                row
                for row in store.read_rows("FieldMappings")
                if (row["サイト"], row["レポートタイプ"], row["列キー"])
                == (args.site, args.report_type, args.column_key)
            ),
            {},
        )
        existing.update(
            {
                "サイト": args.site,
                "レポートタイプ": args.report_type,
                "列キー": args.column_key,
                "フィールドAPI名": args.field_api_name,
                "型": args.field_type,
                "確認状態": "確認済み",
                "確認日": now_text(),
            }
        )
        store.upsert("FieldMappings", ("サイト", "レポートタイプ", "列キー"), existing)
        return 0
    if args.command == "build-soql":
        draft = build_saved_report(store, report_id, args.apply)
        if draft is None:
            logger.error("未取得: %s", report_id)
            return 2
        logger.info("%s", draft.soql or "SOQL を自動組立できません")
        return 0 if draft.soql else 2
    if args.command == "export-csv":
        export_csv(store, args.path)
        return 0
    return 2
