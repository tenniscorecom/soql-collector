"""``列名の対応.csv`` の行作りと、 既存の CSV への積み上げ。"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

from comken.core.table import Table
from comken.exceptions import CSVError
from comken.toolbox.csv import CSV

from src.tables.constants import COLUMN_NAMES_COLUMNS

if TYPE_CHECKING:
    from src.fetch import ReportRecord

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _ResolvedField:
    """``(主オブジェクト, SOQL列名)`` で集約した 1 つの解決済みフィールド。"""

    main_object: str
    soql_name: str
    display_name: str
    field_type: str
    reference_to: str
    pii_status: str
    pii_basis: str
    keys: tuple[str, ...]
    other_label_note: str


def aggregate_resolved_fields(
    records: Sequence[ReportRecord],
    pii_lookups: dict[str, dict[str, tuple[str, str]]],
) -> list[_ResolvedField]:
    """全 record の解決済み column_map エントリを ``(主オブジェクト, SOQL列名)`` で集約する。"""
    groups: dict[tuple[str, str], list[tuple[str, dict, str]]] = {}
    for record in records:
        key = record.key
        main_object_text = record.main_object or "(不明)"
        column_map = record.column_map
        pii_lookup = pii_lookups.get(key, {})
        for col in column_map:
            field_api = col["対応フィールドAPI名"]
            if not field_api or field_api == "(不明)":
                continue
            # 人が直した値で上書きされている場合、 備考に 「直したSOQL列名」 を含むが
            # CSV に出すのは実 SOQL 列名で良い
            pii_status, pii_basis = pii_lookup.get(col["列キー"], ("", ""))
            dedup_key = (main_object_text, field_api)
            groups.setdefault(dedup_key, []).append((key, col, pii_status + "\t" + pii_basis))

    fields: list[_ResolvedField] = []
    for (main_object, soql_name), contributions in groups.items():
        keys = sorted({row[0] for row in contributions})
        label_count: dict[str, int] = {}
        label_min_key: dict[str, str] = {}
        for key, col, _ in contributions:
            label = col["表示名"]
            label_count[label] = label_count.get(label, 0) + 1
            if label not in label_min_key or key < label_min_key[label]:
                label_min_key[label] = key
        best_label = ""
        best_count = -1
        best_min_key = ""
        for label, count in label_count.items():
            min_key = label_min_key[label]
            if count > best_count or (count == best_count and min_key < best_min_key):
                best_label = label
                best_count = count
                best_min_key = min_key

        other_parts: list[str] = []
        for label, min_key in label_min_key.items():
            if label == best_label:
                continue
            other_parts.append(f"{label}（{min_key}）")
        other_note = ""
        if other_parts:
            other_note = "別の表示名あり: " + ", ".join(other_parts)

        first_key = keys[0]
        first_col = next(col for key, col, _ in contributions if key == first_key)
        first_pii = next(pii for key, _, pii in contributions if key == first_key)
        pii_status, pii_basis = first_pii.split("\t", 1)

        fields.append(
            _ResolvedField(
                main_object=main_object,
                soql_name=soql_name,
                display_name=best_label,
                field_type=first_col["型"],
                reference_to=first_col["参照先オブジェクト"],
                pii_status=pii_status,
                pii_basis=pii_basis,
                keys=tuple(keys),
                other_label_note=other_note,
            )
        )

    fields.sort(key=lambda f: (f.main_object, f.soql_name))
    return fields


def resolved_fields_to_csv_rows(
    fields: Sequence[_ResolvedField], today: date
) -> list[dict[str, str]]:
    """``列名の対応.csv`` 用の行 dict を作る。"""
    rows: list[dict[str, str]] = []
    iso = today.isoformat()
    for field in fields:
        rows.append(
            {
                "主オブジェクト": field.main_object,
                "SOQL列名": field.soql_name,
                "表示名": field.display_name,
                "備考": field.other_label_note,
                "更新日": iso,
            }
        )
    return rows


def merge_column_names_csv(
    path: Path,
    new_rows: Sequence[dict[str, str]],
    replaced_keys: Iterable[str],
) -> None:
    """``列名の対応.csv`` を既存ファイルと ``new_rows`` でマージして書き直す。

    並びは **主オブジェクト → SOQL列名** 。
    既存ファイルの見出しが ``COLUMN_NAMES_COLUMNS`` と違うときは ``CSVError`` を上げる。
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    existing_rows: list[dict[str, str]] = []
    if path.exists():
        try:
            with CSV(path) as csv:
                table = csv.read()
        except CSVError as exc:
            raise CSVError(
                f"既存の {path.name} を読み込めません: {path}\n{exc}"
                "\n対処: ファイルを別名に退避してから再実行してください。"
            ) from exc
        if list(table.columns) != list(COLUMN_NAMES_COLUMNS):
            raise CSVError(
                f"既存の {path.name} の見出しが想定と違います: {path}\n"
                f"期待: {list(COLUMN_NAMES_COLUMNS)}\n"
                f"実際: {list(table.columns)}"
                "\n対処: ファイルを別名に退避してから再実行してください。"
            )
        existing_rows = [dict(row) for row in table.to_rows()]

    new_keys = set(replaced_keys)
    if not new_keys:
        merged = list(existing_rows)
    else:
        new_pairs = {(row.get("主オブジェクト", ""), row.get("SOQL列名", "")) for row in new_rows}
        kept_old_rows = [
            row
            for row in existing_rows
            if (
                row.get("主オブジェクト", ""),
                row.get("SOQL列名", ""),
            )
            not in new_pairs
        ]
        merged = kept_old_rows + list(new_rows)

    merged.sort(key=lambda row: (row.get("主オブジェクト", ""), row.get("SOQL列名", "")))
    _write_csv(path, COLUMN_NAMES_COLUMNS, merged)


def _write_csv(path: Path, columns: Sequence[str], rows: list[dict[str, str]]) -> None:
    """comken の ``CSV`` クラスで UTF-8 BOM + CRLF の CSV を書く。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with CSV(path, columns=list(columns)) as csv:
        csv.replace(Table(list(columns), rows))
