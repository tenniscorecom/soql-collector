"""1 レポートの終端処理（PII 評価・警告・SOQL 下書き・``FetchOutcome`` の組み立て）。"""

from __future__ import annotations

import logging
from typing import Any

from src import mapping, pii, soql_builder
from src.fetch.models import FetchOutcome, ReportRecord
from src.fetch.report_slim import SlimReport, dropped_keys_warning_text
from src.fetch.row_check import ROW_CHECK_UNKNOWN
from src.fetch.site_run import FetchState
from src.master import MasterEntry

logger = logging.getLogger(__name__)


def build_fetch_outcome(
    *,
    entry: MasterEntry,
    report_id: str,
    state: FetchState,
    pii_keywords: tuple[str, ...],
    pii_person_objects: tuple[str, ...],
) -> FetchOutcome:
    """``_fetch_one`` の終端処理 (PII / 警告 / SOQL / FetchOutcome 組み立て) を行う。"""
    main_object_name = state.main_object_name
    objects = state.objects
    main_object = main_object_name if main_object_name and main_object_name in objects else None
    warnings = state.warnings
    if main_object_name and main_object_name not in objects:
        warnings.append(
            f"主オブジェクト {main_object_name} の describe に失敗したため"
            f"関連オブジェクトは取得していません"
        )

    dropped_keys_warning = dropped_keys_warning_text(state.dropped_keys)
    if dropped_keys_warning:
        warnings.append(dropped_keys_warning)

    # PII 評価: 出力列だけを対象にする。 絞り込みにだけ使う列は対象外。
    pii_config = pii.PIIConfig(
        keywords=pii_keywords,
        person_objects=pii_person_objects,
    )
    pii_result = _evaluate_record_pii(state.slim_report, state.column_map, objects, pii_config)
    _log_pii(pii_result)

    # 2000 件超判定が 判定不可 なら、 警告表にも 1 行入れる
    if (
        state.row_check is not None
        and state.row_check.status == ROW_CHECK_UNKNOWN
        and state.row_check.reason
    ):
        warnings.append(f"2000件超の判定ができませんでした: {state.row_check.reason}")

    soql_draft = soql_builder.build_soql(
        ReportRecord(
            key=entry.key,
            summary=entry.summary,
            report_id=report_id,
            url=entry.url,
            main_object=main_object,
            report=state.slim_report,
            objects=objects,
            relations=tuple(state.relations),
            column_map=state.column_map,
            warnings=tuple(warnings),
        )
    )

    record = ReportRecord(
        key=entry.key,
        summary=entry.summary,
        report_id=report_id,
        url=entry.url,
        main_object=main_object,
        report=state.slim_report,
        objects=objects,
        relations=tuple(state.relations),
        column_map=state.column_map,
        warnings=tuple(warnings),
        row_check=state.row_check,
        pii=pii_result,
        soql_draft=soql_draft,
    )
    return FetchOutcome(
        entry=entry,
        status="ok",
        record=record,
        warnings=tuple(warnings),
        error=None,
    )


def _evaluate_record_pii(
    slim_report: SlimReport,
    column_map: list[dict[str, str]],
    slim_objects: dict[str, dict[str, Any]],
    config: pii.PIIConfig,
) -> pii.ReportPIIResult:
    """``ReportRecord`` 用の PII 評価を行う。 出力列のみ対象。"""
    refs = _collect_output_column_refs(slim_report, column_map)
    return pii.evaluate_report_pii(refs, config, slim_objects)


def _collect_output_column_refs(
    slim_report: SlimReport,
    column_map: list[dict[str, str]],
) -> list[pii.ColumnRef]:
    """レポートの出力列（detailColumns / groupingsDown / groupingsAcross /
    集計対象） について PII 評価の入力 ``ColumnRef`` を作る。

    絞り込みにだけ使う列は対象外。 ``column_map`` が見つからない列は未解決として
    扱う。
    """
    column_map_by_key = {col["列キー"]: col for col in column_map}
    refs: list[pii.ColumnRef] = []
    for column_key in dict.fromkeys(mapping.output_column_keys(slim_report["reportMetadata"])):
        col = column_map_by_key.get(column_key, {})
        field_api = col.get("対応フィールドAPI名", "")
        refs.append(
            pii.ColumnRef(
                column_key=column_key,
                label=col.get("表示名") or column_key,
                field_name=mapping.field_name_from_soql(field_api),
                field_type=col.get("型", ""),
                owner_object=col.get("所属オブジェクト", ""),
                resolved=bool(field_api) and field_api != "(不明)",
            )
        )
    return refs


def _log_pii(pii_result: pii.ReportPIIResult) -> None:
    """個人情報判定の結果を 1 行で出す。 列名だけ、 値は出さない。"""
    columns_text = ", ".join(label for label, _ in pii_result.matching_columns) or "-"
    logger.info("  個人情報: %s 根拠列=%s", pii_result.status, columns_text)
