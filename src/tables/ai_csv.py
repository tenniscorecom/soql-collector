"""``AIチャット用/`` に、 会社の AI チャットへ渡すファイルを書く。

AI チャットは 1 ファイル 100KB までなので、 超える CSV は行の切れ目で分ける。

- ``はじめに.md``: 各ファイルの説明・読む順・頼み方の例
- ``一覧.csv`` / ``列の対応.csv``: ``SOQL調査.xlsx`` と同じ中身
- ``子リレーション.csv``: 主オブジェクトの子リレーション
- ``項目定義/{オブジェクト}.csv``: 今回のレポートで使ったオブジェクトの全項目

項目定義は保存した原本から作る（選択肢を切り詰めていない値が要るため）。 Salesforce には
接続しない。 分割の数が変わると古いファイルが残って紛らわしいので、 書く前に前回の CSV を消す。
"""

from __future__ import annotations

import csv
import io
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from src.ai_markdown import (
    AI_FILE_MAX_BYTES,
    COMPANION_FILES_HEADING,
    record_child_relationships,
    relationship_source,
)
from src.describe_store import DESCRIBE_CSV_COLUMNS, DescribeStore, describe_to_csv_rows
from src.tables.constants import COLUMNS_SHEET_COLUMNS, SURVEY_COLUMNS

if TYPE_CHECKING:
    from src.fetch import ReportRecord

logger = logging.getLogger(__name__)

AI_CHAT_DIR = "AIチャット用"
GUIDE_FILE_NAME = "はじめに.md"
OVERVIEW_CSV_NAME = "一覧.csv"
COLUMNS_CSV_NAME = "列の対応.csv"
CHILD_RELATIONSHIP_CSV_NAME = "子リレーション.csv"
OBJECT_DEFINITION_DIR = "項目定義"

CHILD_RELATIONSHIP_COLUMNS: tuple[str, ...] = (
    "親",
    "リレーション名",
    "子",
    "参照項目",
    "出どころ",
)

# Excel で開いても文字化けしないよう BOM を付ける
_CSV_BOM = "﻿".encode()
_CSV_LINE_END = "\r\n"

# ファイル名に使えない文字（オブジェクト名は API 名だが、 念のため揃える）
_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]")


@dataclass(frozen=True)
class AiChatFiles:
    """書いたファイルと、 Markdown の 「一緒に渡すファイル」 に載せる相対パス。"""

    wrote: tuple[Path, ...]
    #: ``{管理番号: (OUTPUT_DIR から見た相対パス, ...)}``
    companion_files: dict[str, tuple[str, ...]]


def write_ai_chat_files(
    output_dir: Path,
    records: Sequence[ReportRecord],
    *,
    survey_rows: list[dict[str, str]],
    columns_rows: list[dict[str, str]],
    describe_store: DescribeStore,
    markdown_dir: str,
) -> AiChatFiles:
    """``output_dir/AIチャット用/`` を書き直す。"""
    target_dir = output_dir / AI_CHAT_DIR
    _remove_previous_csvs(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    overview_paths = write_limited_csv(target_dir / OVERVIEW_CSV_NAME, SURVEY_COLUMNS, survey_rows)
    columns_paths = write_limited_csv(
        target_dir / COLUMNS_CSV_NAME, COLUMNS_SHEET_COLUMNS, columns_rows
    )
    child_paths = write_limited_csv(
        target_dir / CHILD_RELATIONSHIP_CSV_NAME,
        CHILD_RELATIONSHIP_COLUMNS,
        build_child_relationship_rows(records),
    )
    object_paths = _write_object_definitions(target_dir, records, describe_store)

    guide_path = target_dir / GUIDE_FILE_NAME
    guide_path.write_text(_build_guide(markdown_dir), encoding="utf-8", newline="\n")

    wrote = [
        guide_path,
        *overview_paths,
        *columns_paths,
        *child_paths,
        *(path for paths in object_paths.values() for path in paths),
    ]
    logger.info("AI チャット用のファイルを書き出し: %s（%d 件）", target_dir, len(wrote))
    return AiChatFiles(
        wrote=tuple(wrote),
        companion_files=_build_companion_files(
            records,
            output_dir,
            common_paths=[*columns_paths, *child_paths],
            object_paths=object_paths,
        ),
    )


def build_child_relationship_rows(records: Sequence[ReportRecord]) -> list[dict[str, str]]:
    """全レポートの主オブジェクトの子リレーションを、 重複を除いて行にする。"""
    rows: dict[tuple[str, str, str, str], dict[str, str]] = {}
    for record in records:
        for relationship in record_child_relationships(record):
            row = {
                "親": record.main_object or "",
                "リレーション名": str(relationship.get("relationshipName") or ""),
                "子": str(relationship.get("childSObject") or ""),
                "参照項目": str(relationship.get("field") or ""),
                "出どころ": relationship_source(relationship),
            }
            rows[(row["親"], row["リレーション名"], row["子"], row["参照項目"])] = row
    return [rows[key] for key in sorted(rows)]


def write_limited_csv(
    path: Path,
    columns: Sequence[str],
    rows: Sequence[dict[str, str]],
) -> tuple[Path, ...]:
    """UTF-8 BOM の CSV を書く。 上限を超えるなら ``名前_1.csv`` のように行の切れ目で分ける。

    comken の CSV は書いたあとの大きさを前もって測れないので、 ここだけ標準の ``csv`` で
    1 行ずつ文字列にして測る。 分けるときは各ファイルに見出しを付ける。
    """
    header = _csv_line(columns)
    base_size = len(_CSV_BOM) + len(header)
    chunks: list[list[bytes]] = [[]]
    chunk_size = base_size
    for row in rows:
        line = _csv_line([row.get(column, "") for column in columns])
        if chunks[-1] and chunk_size + len(line) > AI_FILE_MAX_BYTES:
            chunks.append([])
            chunk_size = base_size
        chunks[-1].append(line)
        chunk_size += len(line)

    paths: list[Path] = []
    for index, lines in enumerate(chunks, start=1):
        chunk_path = (
            path if len(chunks) == 1 else path.with_name(f"{path.stem}_{index}{path.suffix}")
        )
        content = _CSV_BOM + header + b"".join(lines)
        chunk_path.parent.mkdir(parents=True, exist_ok=True)
        chunk_path.write_bytes(content)
        if len(content) > AI_FILE_MAX_BYTES:
            logger.warning(
                "1 行が %d バイトを超えていて分けられません。AI チャットに渡せない可能性: %s",
                AI_FILE_MAX_BYTES,
                chunk_path,
            )
        paths.append(chunk_path)
    return tuple(paths)


def _csv_line(values: Sequence[str]) -> bytes:
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator=_CSV_LINE_END).writerow(values)
    return buffer.getvalue().encode("utf-8")


def _remove_previous_csvs(target_dir: Path) -> None:
    """前回の CSV を消す。 分割の数が減ったときに古い ``_2.csv`` を残さないため。"""
    if not target_dir.is_dir():
        return
    for path in target_dir.rglob("*.csv"):
        path.unlink()


def _write_object_definitions(
    target_dir: Path,
    records: Sequence[ReportRecord],
    describe_store: DescribeStore,
) -> dict[str, tuple[Path, ...]]:
    """``項目定義/{オブジェクト}.csv`` を書く。 戻り値は ``{オブジェクト名の小文字: パス}``。

    同じオブジェクトを複数の組織から読んだときは同じファイルに入れる（組織の列で分かる）。
    """
    rows_by_object: dict[str, list[dict[str, str]]] = {}
    names_by_object: dict[str, str] = {}
    seen: set[tuple[str, str]] = set()
    for record in records:
        for name in _used_object_names(record):
            lower_name = name.lower()
            if (record.org, lower_name) in seen:
                continue
            seen.add((record.org, lower_name))
            cached = describe_store.lookup(record.org, name) or {}
            describe = cached.get("describe")
            if not describe:
                continue
            names_by_object.setdefault(lower_name, name)
            rows_by_object.setdefault(lower_name, []).extend(
                describe_to_csv_rows(record.org, name, describe, str(cached.get("fetched_at", "")))
            )
    folder = target_dir / OBJECT_DEFINITION_DIR
    return {
        lower_name: write_limited_csv(
            folder / f"{_UNSAFE_FILENAME_CHARS.sub('_', names_by_object[lower_name])}.csv",
            DESCRIBE_CSV_COLUMNS,
            rows,
        )
        for lower_name, rows in sorted(rows_by_object.items())
    }


def _used_object_names(record: ReportRecord) -> list[str]:
    """このレポートで使ったオブジェクト（主・関連・クロスフィルタの子・子から取る形の子）。"""
    return list(dict.fromkeys([*record.objects, *record.cross_filter_objects]))


def _build_companion_files(
    records: Sequence[ReportRecord],
    output_dir: Path,
    *,
    common_paths: Sequence[Path],
    object_paths: dict[str, tuple[Path, ...]],
) -> dict[str, tuple[str, ...]]:
    result: dict[str, tuple[str, ...]] = {}
    for record in records:
        paths = list(common_paths)
        for name in _used_object_names(record):
            paths.extend(object_paths.get(name.lower(), ()))
        result[record.key] = tuple(path.relative_to(output_dir).as_posix() for path in paths)
    return result


def _build_guide(markdown_dir: str) -> str:
    material = f"../{markdown_dir}/{{管理番号}}.md"
    objects = f"{OBJECT_DEFINITION_DIR}/{{オブジェクト}}.csv"
    lines = [
        "# AI チャットに渡すファイルの説明",
        "",
        "SOQL を AI に組ませるときの材料です。 どのファイルも 1 つ 100KB 以内です。",
        "",
        "## ファイル",
        "",
        "| ファイル | 中身 |",
        "|---|---|",
        f"| `{material}` | レポート 1 本の資料。 大きいものは `{{管理番号}}_2.md` に続く |",
        f"| `{OVERVIEW_CSV_NAME}` | 全レポートの一覧（状態・取り方・件数） |",
        f"| `{COLUMNS_CSV_NAME}` | 全レポートの列と SOQL 列名の対応、状態、候補 |",
        f"| `{CHILD_RELATIONSHIP_CSV_NAME}` | 親から子へのリレーション。 手入力は人が書いた値 |",
        f"| `{objects}` | オブジェクトの全項目。 大きいものは `_1.csv` のように分かれる |",
        "",
        "## 読む順",
        "",
        f"1. `{material}` を渡す",
        f"2. 資料の「{COMPANION_FILES_HEADING.removeprefix('## ')}」にあるファイルを渡す",
        f"3. 全体の傾向を聞くときだけ `{OVERVIEW_CSV_NAME}` を渡す",
        "",
        "## 頼み方の例",
        "",
        "- 「資料の SOQL の下書きを確かめて、 取れていない列を項目定義から探してください」",
        "- 「このレポートと同じ結果になる SOQL を作ってください。 候補が複数ある列は理由も」",
        "",
        "## 注意",
        "",
        "- レポートの行データは入っていません",
        "- 選択リストの値と、 レポートの絞り込みの値は入っています",
        "- 社外の AI に渡してよいかは、 社内のルールで判断してください",
        "",
    ]
    return "\n".join(lines)
