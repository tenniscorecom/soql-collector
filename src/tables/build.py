"""``run_tables``: ``ReportRecord`` から xlsx / CSV / Markdown を組み立てて書く。"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from src import ai_markdown, mapping, relation_diagram, report_py, similar, soql_similar
from src.tables.ai_csv import write_ai_chat_files
from src.tables.column_names_csv import (
    aggregate_resolved_fields,
    merge_column_names_csv,
    resolved_fields_to_csv_rows,
)
from src.tables.columns_rows import build_columns_sheet_rows_from_records
from src.tables.constants import COLUMN_NAMES_CSV_NAME, XLSX_NAME
from src.tables.diagram_rows import build_relation_diagram_rows
from src.tables.survey_rows import (
    build_survey_table_rows_from_records,
    build_warning_table_rows_from_records,
)
from src.tables.xlsx_out import write_xlsx

if TYPE_CHECKING:
    from src.describe_store import DescribeStore
    from src.fetch import ReportRecord

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TableOutcome:
    """tables 実行の結果。"""

    wrote: tuple[Path, ...]
    skipped: tuple[Path, ...]


def run_tables(
    output_dir: Path,
    records: Iterable[ReportRecord],
    *,
    only_keys: Iterable[str] | None = None,
    similar_threshold: float = 0.8,
    xlsx_name: str = XLSX_NAME,
    markdown_dir: str = ai_markdown.MARKDOWN_SUBDIR,
    diagram_dir: str = relation_diagram.DIAGRAM_SUBDIR,
    describe_store: DescribeStore | None = None,
) -> TableOutcome:
    """``records`` から ``SOQL調査.xlsx`` と ``列名の対応.csv`` と Markdown を組み立てる。

    ダウンローダー用の SOQL レポート Python ファイル
    （ ``SOQLレポート/report_<管理番号>.py`` ） も一緒に書く。

    Args:
        output_dir: 出力フォルダ。 無ければ作る。
        records: ``fetch.ReportRecord`` のイテラブル。
        only_keys: 互換性のために受け取るが今回は使われない。
        similar_threshold: 似たレポート判定の Jaccard 下限。
        xlsx_name: 出力する xlsx のファイル名。 既定 ``SOQL調査.xlsx``。
        markdown_dir: ``SOQL資料/{管理番号}.md`` を置くフォルダ名
            （``config.ini [FILES] MARKDOWN_DIR``）。
        diagram_dir: ``関係図/{主オブジェクト}.md`` を置くフォルダ名
            （``config.ini [FILES] DIAGRAM_DIR``）。
        describe_store: 保存した原本を引くストア。 渡すと AI チャット用のファイルも書く。

    Returns:
        ``TableOutcome``: 書いたファイル一覧。 ``skipped`` は常に空。
    """
    del only_keys  # per-ID CSV は作らないので無視
    record_list = list(records)

    sorted_records = sorted(record_list, key=lambda r: r.key)
    ok_keys = [r.key for r in sorted_records]

    # 個人情報は取得時に列ごとに判定済み（判定し直さない）
    pii_lookups = {record.key: record.pii.by_column for record in sorted_records}

    # 似たレポート判定
    similar_groups = similar.find_similar_groups(
        sorted_records, column_similarity_threshold=similar_threshold
    )
    key_to_group: dict[str, tuple[int, str, str]] = {}
    for group in similar_groups:
        base_integration = next(
            (p.integration for p in group.pairs if p.key_b == group.base_key),
            "1本にまとめられる",
        )
        key_to_group[group.base_key] = (group.group_number, group.base_key, base_integration)
        for pair in group.pairs:
            if pair.key_b != group.base_key:
                key_to_group[pair.key_b] = (
                    group.group_number,
                    group.base_key,
                    pair.integration,
                )

    # 似たSOQL判定（生成した SOQL 同士を比べる。 レポート定義の比較とは別）
    similar_soql_lookup = soql_similar.find_similar_soql(sorted_records)

    # 解決済みフィールドの集約
    resolved_fields = aggregate_resolved_fields(sorted_records, pii_lookups)

    # 警告・要対応件数
    warning_rows = build_warning_table_rows_from_records(sorted_records)
    unresolved_counts = {
        record.key: sum(mapping.column_status(col) != "OK" for col in record.column_map)
        for record in sorted_records
    }

    # 関係図
    diagrams = relation_diagram.build_main_object_diagrams(sorted_records)
    relation_diagram_rows = build_relation_diagram_rows(diagrams)

    # 各シートの行を作る
    survey_rows = build_survey_table_rows_from_records(
        sorted_records,
        similar_lookup=key_to_group,
        similar_soql_lookup=similar_soql_lookup,
        unresolved_counts=unresolved_counts,
        warnings=warning_rows,
        markdown_dir=markdown_dir,
    )
    columns_rows = build_columns_sheet_rows_from_records(
        sorted_records,
        pii_lookups=pii_lookups,
    )

    wrote: list[Path] = []

    if sorted_records:
        xlsx_path = output_dir / xlsx_name
        write_xlsx(
            xlsx_path,
            survey=survey_rows,
            columns=columns_rows,
            relation_diagram=relation_diagram_rows,
        )
        wrote.append(xlsx_path)
        logger.info(
            "Excel を書き出し: %s（一覧=%d / 列の対応=%d / 関係図=%d）",
            xlsx_path,
            len(survey_rows),
            len(columns_rows),
            len(relation_diagram_rows),
        )

    # 列名の対応.csv （積み上げ）
    column_names_path = output_dir / COLUMN_NAMES_CSV_NAME
    if ok_keys:
        column_names_rows = resolved_fields_to_csv_rows(resolved_fields, _today_local())
        merge_column_names_csv(column_names_path, column_names_rows, ok_keys)
        logger.info(
            "CSV を書き出し: %s（%d 行）",
            column_names_path,
            len(column_names_rows),
        )
    elif column_names_path.exists():
        logger.info(
            "CSV は既存を維持: %s（record なしのため新規/置換なし）",
            column_names_path,
        )
    if column_names_path.exists():
        wrote.append(column_names_path)

    # AI チャット用のファイル。 分割後のファイル名を Markdown に載せるので、 Markdown より先に書く
    companion_files: dict[str, tuple[str, ...]] | None = None
    if describe_store is not None and sorted_records:
        ai_chat_files = write_ai_chat_files(
            output_dir,
            sorted_records,
            survey_rows=survey_rows,
            columns_rows=columns_rows,
            describe_store=describe_store,
            markdown_dir=markdown_dir,
        )
        wrote.extend(ai_chat_files.wrote)
        companion_files = ai_chat_files.companion_files

    # 1 レポート 1 つの Markdown 資料を ``{markdown_dir}/{管理番号}.md`` に書く。
    # AI に SOQL を組ませるときにそのまま渡す用途。 既存のファイルはそのまま
    # 残し、 今回取れた record の分だけ上書きする。 似たレポートの情報を
    # ``similar_lookup`` で渡すと、 Markdown の 「似たレポート」 セクションに
    # 反映される。 似た SOQL の情報は ``similar_soql_lookup`` で渡す
    wrote.extend(
        ai_markdown.write_ai_markdowns(
            output_dir,
            sorted_records,
            similar_lookup=key_to_group,
            similar_soql_lookup=similar_soql_lookup,
            diagrams=diagrams,
            subdir=markdown_dir,
            diagram_subdir=diagram_dir,
            companion_files=companion_files,
        )
    )

    # 主オブジェクトごとの関係図（Mermaid）を ``{diagram_dir}/{主オブジェクト}.md`` に書く。
    # 各 ``{markdown_dir}/{管理番号}.md`` からこのファイルへリンクしているので、 一緒に出す
    wrote.extend(
        relation_diagram.write_main_object_diagrams(output_dir, diagrams, subdir=diagram_dir)
    )

    # ダウンローダー用の SOQL レポート Python ファイル。 ``SOQLレポート/`` に
    # 1 レポート 1 ファイルで書く（ SOQL が作れなかったレポートは作らない）
    wrote.extend(report_py.write_report_py_files(output_dir, sorted_records))

    return TableOutcome(wrote=tuple(wrote), skipped=())


def _today_local() -> date:
    """「今日の日付」 をこの PC のローカル時間で返す。"""
    return datetime.now(tz=UTC).astimezone().date()
