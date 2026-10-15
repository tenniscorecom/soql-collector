"""trial/fetch_one_report.py — レポートを 1 件だけ取る単品サンプル。

管理表を使わず、 コマンドライン引数で 1 件のレポート URL を指定して取る。
本体 (``src/``) の ``run_fetch`` と ``run_tables`` をそのまま呼ぶ薄い例で、
``output_dir`` だけ ``output/single/`` に差し替えて、 本体の ``output/`` や
全体の ``列名の対応.csv`` を汚さないようにする。

``trial/`` は **名前空間パッケージ** （ ``__init__.py`` を **置かない** ） として
配布し、 ``python -m trial.fetch_one_report`` でそのまま動かせる。

使い方:
    python -m trial.fetch_one_report <レポートのURL> [管理番号]

``管理番号`` を省略すると ``sample``。 出力は ``output/single/`` 配下に
``SOQL調査.xlsx`` と ``列名の対応.csv`` が書かれる （ 取れたとき ） 。
per-ID の CSV は作らない。
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable

from src.fetch import run_fetch
from src.master import MasterEntry
from src.run import FilePaths, read_file_paths, read_survey_config
from src.tables import run_tables

logger = logging.getLogger(__name__)

# ``run_fetch`` の ``site_for`` と同じ型（ テスト用差し替え ）
SiteFor = Callable[[str], type]


def main(argv: list[str] | None = None, *, site_for: SiteFor | None = None) -> int:
    """レポートを 1 件だけ取る。 終了コードは run.py の ``run()`` と同じ。

    Args:
        argv: コマンドライン引数。 ``None`` なら ``sys.argv[1:]`` を使う。
        site_for: URL → Salesforce サイトクラスの関数。 テスト用差し替え。

    Returns:
        0 … 成功
        1 … 失敗
        2 … 引数エラー ( argparse が ``SystemExit(2)`` を投げる )
    """
    parser = argparse.ArgumentParser(
        prog="python -m trial.fetch_one_report",
        description="レポートを 1 件だけ取る単品サンプル。",
    )
    parser.add_argument("url", help="レポートの URL")
    parser.add_argument(
        "key",
        nargs="?",
        default="sample",
        help="管理番号（ 既定: sample ）",
    )
    args = parser.parse_args(argv)

    # 出力先だけ ``single/`` にずらす。 本体の ``output/`` を汚さず、 全体の
    # ``対応表.csv`` にも混ざらない。
    base_paths = _read_trial_file_paths()
    single_output_dir = base_paths.output_dir / "single"
    file_paths = FilePaths(
        master_xlsx_path=base_paths.master_xlsx_path,
        output_dir=single_output_dir,
        describe_csv_path=base_paths.describe_csv_path,
        column_fix_xlsx_path=base_paths.column_fix_xlsx_path,
        xlsx_name=base_paths.xlsx_name,
        markdown_dir=base_paths.markdown_dir,
        diagram_dir=base_paths.diagram_dir,
    )

    entry = MasterEntry(key=args.key, summary="単品サンプル", url=args.url, enabled=True)
    from src.describe_store import DescribeStore

    describe_store = DescribeStore.load(file_paths.describe_csv_path)
    refresh_set: set[tuple[str, str]] = set()
    outcomes = run_fetch(
        [entry],
        describe_csv_path=file_paths.describe_csv_path,
        site_for=site_for,
        describe_store=describe_store,
        refresh_set=refresh_set,
    )

    ok_keys: list[str] = []
    failed_count = 0
    records = []
    for outcome in outcomes:
        if outcome.status == "ok":
            ok_keys.append(outcome.entry.key)
            records.append(outcome.record)
            print(f"[ok] 管理番号={outcome.entry.key} 警告={len(outcome.warnings)} 件")
            for warning in outcome.warnings:
                print(f"  - 警告: {warning}")
        else:
            failed_count += 1
            print(f"[failed] 管理番号={outcome.entry.key}: {outcome.error}")

    if records:
        try:
            _, _, _, similar_threshold = read_survey_config()
            run_tables(
                file_paths.output_dir,
                records,
                only_keys=ok_keys,
                similar_threshold=similar_threshold,
                xlsx_name=file_paths.xlsx_name,
            )
        except Exception as exc:
            logger.error("CSV の生成に失敗しました: %s", exc)

    return 1 if failed_count else 0


def _read_trial_file_paths() -> FilePaths:
    """trial 用の ``FilePaths`` を ``src.run.read_file_paths`` から読む。"""
    return read_file_paths()


if __name__ == "__main__":
    from comken import comken_logger

    comken_logger.setup_local_logging()
    sys.exit(main())
