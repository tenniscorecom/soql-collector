"""人が手で書いた子リレーションの CSV （ ``input/子リレーション.csv`` ） 。

会社の中の情報を開発側に持ち出せないので、 人が会社側で CSV に手入力して
使えるようにしている。 読み込んだ行は主オブジェクトの describe の
``childRelationships`` に足し、 クロスフィルタの副問い合わせの材料にする。

- 場所: ``列の直し.xlsx`` と同じフォルダ （ ``input/子リレーション.csv`` ）
- 列: 親オブジェクト / リレーション名 / 子オブジェクト / 参照項目 / メモ
- 文字コード: Excel で開いて編集・保存できるようにする。 読み込みは cp932 と
  UTF-8 （ BOM 付き ） のどちらも読める （ comken の CSV の自動判定に任せる ）
- 値は SOQL と .py に入るので、 識別子 （ ``^[A-Za-z_][A-Za-z0-9_]*$`` ） に
  限る。 満たさない行は使わず、 警告を出す
- 手入力の値は保存データ （ ``data/子リレーション.csv`` ） には書かない
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from comken.core.table import Table
from comken.exceptions import CSVError
from comken.toolbox.csv import CSV

logger = logging.getLogger(__name__)


# ファイル名。 ``列の直し.xlsx`` と同じフォルダに置く
CHILD_RELATIONSHIP_CSV_NAME = "子リレーション.csv"

# CSV の列。 この順で書く
CHILD_RELATIONSHIP_COLUMNS: tuple[str, ...] = (
    "親オブジェクト",
    "リレーション名",
    "子オブジェクト",
    "参照項目",
    "メモ",
)

# SOQL に入れる値として使える識別子。 引用符や空白が入ると SOQL が壊れるため
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class ManualChildRelationship:
    """手入力の子リレーション 1 行。"""

    parent_object: str
    relationship_name: str
    child_object: str
    reference_field: str
    note: str = ""


def child_relationship_csv_path(column_fix_xlsx_path: Path) -> Path:
    """``列の直し.xlsx`` と同じフォルダの ``子リレーション.csv`` のパス。"""
    return column_fix_xlsx_path.parent / CHILD_RELATIONSHIP_CSV_NAME


def ensure_child_relationship_file(path: Path) -> None:
    """ファイルが無ければ、 見出しだけのファイルを作る。 已有なら何もしない。"""
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with CSV(path, columns=list(CHILD_RELATIONSHIP_COLUMNS)) as csv:
        csv.replace(Table(list(CHILD_RELATIONSHIP_COLUMNS), []))
    logger.info("手入力の子リレーションのファイルを作りました: %s", path)


def load_manual_child_relationships(path: Path) -> tuple[ManualChildRelationship, ...]:
    """``子リレーション.csv`` を読んで、 使ってよい行だけを返す。

    - ファイルが無ければ空タプル
    - 見出しが違うときは ``CSVError`` を上げる （ 停止させる ）
    - 親・リレーション名・子オブジェクト・参照項目のどれかが空の行と、 値が
      識別子 （ ``^[A-Za-z_][A-Za-z0-9_]*$`` ） に合わない行は使わず、 警告を出す
    """
    if not path.exists():
        return ()
    try:
        with CSV(path) as csv:
            table = csv.read()
    except OSError as exc:
        raise CSVError(f"子リレーションの CSV を読めません: {path}\n{exc}") from exc

    if list(table.columns) != list(CHILD_RELATIONSHIP_COLUMNS):
        raise CSVError(
            f"子リレーションの CSV の見出しが想定と違います: {path}\n"
            f"期待: {list(CHILD_RELATIONSHIP_COLUMNS)}\n"
            f"実際: {list(table.columns)}\n"
            "対処: ファイルを別名に退避してから再実行してください。"
        )

    rows: list[ManualChildRelationship] = []
    for index, raw in enumerate(table.to_rows(), start=2):
        entry = _build_row(raw, line_number=index)
        if entry is not None:
            rows.append(entry)
    return tuple(rows)


def _build_row(raw: dict[str, Any], *, line_number: int) -> ManualChildRelationship | None:
    """CSV の 1 行を ``ManualChildRelationship`` にする。 使えない行は ``None``。"""
    values = {column: str(raw.get(column) or "").strip() for column in CHILD_RELATIONSHIP_COLUMNS}
    targets = (
        ("親オブジェクト", values["親オブジェクト"]),
        ("リレーション名", values["リレーション名"]),
        ("子オブジェクト", values["子オブジェクト"]),
        ("参照項目", values["参照項目"]),
    )
    empty = [name for name, value in targets if not value]
    if empty:
        logger.warning(
            "子リレーション.csv の %d 行目は使いません（%s が空）",
            line_number,
            "・".join(empty),
        )
        return None
    invalid = [name for name, value in targets if not IDENTIFIER_PATTERN.fullmatch(value)]
    if invalid:
        logger.warning(
            "子リレーション.csv の %d 行目は使いません（%s が識別子 （英字・数字・_ だけ）でない）",
            line_number,
            "・".join(invalid),
        )
        return None
    return ManualChildRelationship(
        parent_object=values["親オブジェクト"],
        relationship_name=values["リレーション名"],
        child_object=values["子オブジェクト"],
        reference_field=values["参照項目"],
        note=values["メモ"],
    )


def merge_manual_child_relationships(
    child_relationships: list[dict[str, Any]],
    manual_rows: Sequence[ManualChildRelationship],
    parent_object: str,
) -> list[dict[str, Any]]:
    """主オブジェクトの子リレーションに手入力の行を合わせる。

    - 親が一致 （ 大文字小文字無視 ） する行だけを使う
    - (子オブジェクト, 参照項目) が同じ既存要素があれば置き換え、 無ければ足す
    - 足した要素・置き換えた要素には ``"手入力": True`` を付ける
      （出所として区別できるようにするため）

    戻り値は新しいリスト。 引数の ``child_relationships`` は書き換えない。
    """
    merged = [dict(item) for item in child_relationships]
    target = parent_object.strip().lower()
    for row in manual_rows:
        if row.parent_object.strip().lower() != target:
            continue
        element = {
            "childSObject": row.child_object,
            "relationshipName": row.relationship_name,
            "field": row.reference_field,
            "手入力": True,
        }
        replaced = False
        for index, existing in enumerate(merged):
            if _same_child_and_field(existing, row):
                merged[index] = {**existing, **element}
                replaced = True
                break
        if not replaced:
            merged.append(element)
    return merged


def _same_child_and_field(existing: dict[str, Any], row: ManualChildRelationship) -> bool:
    """既存要素と手入力の行が、 同じ (子オブジェクト, 参照項目) かどうか。"""
    child = str(existing.get("childSObject") or "").strip().lower()
    field = str(existing.get("field") or "").strip().lower()
    return (
        child == row.child_object.strip().lower() and field == row.reference_field.strip().lower()
    )
