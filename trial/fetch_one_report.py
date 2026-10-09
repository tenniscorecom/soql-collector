"""trial/fetch_one_report.py — レポートを 1 件だけ取る単品サンプル。

管理表を使わず、 コマンドライン引数で 1 件のレポート URL を指定して取る。
本体 (``src/``) の ``run_fetch`` と ``run_tables`` をそのまま呼ぶ薄い例で、
``output_dir`` だけ ``output/single/`` に差し替えて、 本体の ``output/`` や
全体の ``対応表.csv`` を汚さないようにする。

``trial/`` は **名前空間パッケージ** （ ``__init__.py`` を **置かない** ） として
配布し、 ``python -m trial.fetch_one_report`` でそのまま動かせる。

使い方:
    python -m trial.fetch_one_report <レポートのURL> [管理番号]

``管理番号`` を省略すると ``sample``。 出力は ``output/single/`` 配下に
``対応表_{管理番号}.csv`` / ``列名の対応_{管理番号}.txt`` を含む 8 種類の CSV
（ 取れたとき ） 。
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import sys
from collections.abc import Callable

from src import pii
from src.fetch import run_fetch
from src.master import MasterEntry
from src.settings import load_settings
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
    base_settings = load_settings()
    settings = dataclasses.replace(base_settings, output_dir=base_settings.output_dir / "single")

    entry = MasterEntry(key=args.key, summary="単品サンプル", url=args.url, enabled=True)
    outcomes = run_fetch(settings, [entry], site_for=site_for)

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
            run_tables(
                settings.output_dir,
                records,
                only_keys=ok_keys,
                pii_config=pii.PIIConfig(
                    keywords=settings.pii_keywords,
                    person_objects=settings.pii_person_objects,
                ),
                similar_threshold=settings.similar_column_similarity,
            )
        except Exception as exc:
            logger.error("CSV の生成に失敗しました: %s", exc)

    return 1 if failed_count else 0


if __name__ == "__main__":
    from comken import comken_logger

    comken_logger.setup_local_logging()
    sys.exit(main())
