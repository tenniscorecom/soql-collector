"""ダウンローダー用の SOQL レポート Python ファイルを生成する。

``OUTPUT_DIR/SOQLレポート/report_<管理番号>.py`` に書く。 中身は
``SoqlReport`` のサブクラスで、 そのままダウンローダーの
``src/reports/`` に置けば動く。

**文字列はすべて ``repr()`` を通して組み立てる。** レポートから来た値
（概要・レポート名・レポート ID・missing・checks・表示名） をそのまま書くと、
値がコードとして実行される危険があるため。 改行と制御文字は空白に置き換え、
``repr()`` で Python の文字列リテラルにしてから書き出す。 SOQL だけは行の
区切りを残したいので、 1 行ずつ ``repr()`` を通して実際の改行でつなぐ。

**URL は書き出さない。** レポート ID だけを書き、 組織のドメイン
（ URL 全体） は ``.py`` に残さない。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from comken.exceptions import SalesforceReportIDNotFoundError
from comken.toolbox.salesforce.report import report_id_from_url

from src import mapping
from src.soql_builder import format_cross_filter_link_comment

if TYPE_CHECKING:
    from src.fetch import ReportRecord
    from src.soql_builder import SoqlDraft

logger = logging.getLogger(__name__)

# 生成先のフォルダ名 （ ``OUTPUT_DIR`` の下）
REPORT_PY_DIR = "SOQLレポート"

# 生成するファイル名の接頭辞
REPORT_PY_PREFIX = "report_"

# 生成するクラスに書く USE_BULK の行。 取り方（ Bulk API 2.0 か REST の
# query() か ） は下書きの ``use_bulk`` で決める。 副問い合わせ （ クロス
# フィルタ ） を出したときは False にして、 理由をコメントに付ける
USE_BULK_LINE = "    USE_BULK = True  # False にすると通常の SOQL（REST の query()）で取る"

# USE_BULK を False にする時の行の頭。 後ろに理由を書く
USE_BULK_FALSE_PREFIX = "    USE_BULK = False  # "

# 管理番号をファイル名・クラス名に使える形にする（英数字と _ だけ残す）
_INVALID_KEY_CHARS_PATTERN = re.compile(r"[^A-Za-z0-9_]")

# 改行と制御文字。 値の中に混ざるとコードが壊れるので空白に置き換える
_CONTROL_CHARS_PATTERN = re.compile(r"[\x00-\x1f\x7f]")


def write_report_py_files(
    output_dir: Path,
    records: Iterable[ReportRecord],
) -> tuple[Path, ...]:
    """各レポートの SOQL レポート Python ファイルを書き出す。

    SOQL が作れなかった （ ``作れず`` ） レポートはファイルを作らない。

    Args:
        output_dir: 出力フォルダ （ ``OUTPUT_DIR`` ）。
        records: ``fetch.ReportRecord`` のイテラブル。

    Returns:
        書いたファイルのパスのタプル。
    """
    written: list[Path] = []
    used_stems: set[str] = set()
    for record in records:
        draft = record.soql_draft
        if draft is None or not draft.soql:
            continue
        stem = _unique_stem(record.key, used_stems)
        source = build_report_py_source(record, class_name=f"Report{stem}")
        if source is None:
            continue
        report_dir = output_dir / REPORT_PY_DIR
        report_dir.mkdir(parents=True, exist_ok=True)
        path = report_dir / f"{REPORT_PY_PREFIX}{stem}.py"
        path.write_text(source, encoding="utf-8")
        written.append(path)
    if written:
        logger.info(
            "SOQLレポートの Python ファイルを書き出し: %s（%d 件）",
            output_dir / REPORT_PY_DIR,
            len(written),
        )
    return tuple(written)


def build_report_py_source(record: ReportRecord, class_name: str) -> str | None:
    """1 レポート分の Python ソースを組み立てる。

    Args:
        record: ``fetch.ReportRecord`` 。
        class_name: 生成するクラス名。

    Returns:
        Python のソース。 SOQL が作れなかったときは ``None`` 。
    """
    draft = record.soql_draft
    if draft is None or not draft.soql:
        return None

    column_names, duplicated = _collect_column_names(record)
    summary_text = f"{_flatten(record.summary)}（soql-collector が生成。管理番号 {record.key}）。"
    lines: list[str] = [_docstring_literal(summary_text), ""]
    if column_names:
        lines.append("from typing import ClassVar")
        lines.append("")
    lines.append("from src.reports.base import SoqlReport")
    lines.append("")
    lines.append(f"# 状態: {draft.status}")
    for link in draft.cross_filter_links:
        # 理由の中に改行が入っていてもコードが壊れないよう、 空白に置き換える
        lines.append(
            _flatten(format_cross_filter_link_comment(link, main_object=record.main_object or ""))
        )
    for check in draft.checks:
        lines.append(f"# 要確認: {_flatten(check)}")
    for missing in draft.missing:
        lines.append(f"# 作れなかった部分: {_flatten(missing)}")
    lines.append("")
    lines.append("")
    lines.append(f"class {class_name}(SoqlReport):")
    lines.append(f"    KEY = {record.key!r}")
    name = _report_name(record)
    if name:
        lines.append(f"    NAME = {name!r}")
    report_id = _report_id(record)
    if report_id:
        lines.append(f"    REPORT_ID = {report_id!r}")
    lines.append(_use_bulk_line(draft))
    if column_names:
        lines.append("    COLUMN_NAMES: ClassVar[dict[str, str]] = {")
        for soql_name, display_name in column_names:
            lines.append(f"        {soql_name!r}: {display_name!r},")
        lines.append("    }")
    if duplicated:
        lines.append(
            f"    # 見出しが重なるので自動の対応に任せた: {_flatten(', '.join(duplicated))}"
        )
    lines.append("")
    lines.append("    def soql(self) -> str:")
    soql_literal = _soql_triple_quoted(draft.soql)
    lines.append(f"        return {soql_literal}")
    return "\n".join(lines) + "\n"


def _use_bulk_line(draft: SoqlDraft) -> str:
    """``USE_BULK`` の行を組み立てる。

    ``use_bulk`` が ``True`` のときは ``USE_BULK_LINE`` をそのまま使う。
    ``False`` のときは理由をコメントに付ける。 理由の改行・制御文字は
    ``_flatten`` で空白に置き換える。
    """
    if draft.use_bulk:
        return USE_BULK_LINE
    reason = _flatten("; ".join(draft.bulk_blockers)) or "REST の query() で取る"
    return f"{USE_BULK_FALSE_PREFIX}{reason}"


def _report_name(record: ReportRecord) -> str:
    """レポート名を 1 行の文字列にして返す。 名前が無ければ空文字。

    ``reportMetadata.name`` を優先し、 無い・空・文字列でないときは概要
    （ ``record.summary`` ） を使う。 改行と制御文字は空白に置き換えてから
    前後の空白を落とす。
    """
    metadata = record.report.get("reportMetadata", {})
    raw_name = metadata.get("name") if isinstance(metadata, dict) else None
    if isinstance(raw_name, str):
        name = _flatten(raw_name).strip()
        if name:
            return name
    summary = record.summary if isinstance(record.summary, str) else ""
    return _flatten(summary).strip()


def _report_id(record: ReportRecord) -> str:
    """レポート ID を 1 行の文字列にして返す。 取れなければ空文字。

    管理表の URL （ ``record.url`` ） から取り出す。 URL が空・壊れていて
    レポート ID が無いときは空文字を返す（ 呼び出し側はその行を出さない ）。

    レポート ID だけを書き、 URL 全体は書かない。 生成する ``.py`` は
    公開リポジトリに置かれるため、 組織のドメインを残したくない。
    """
    if not record.url:
        logger.warning(
            "レポート URL が空です（REPORT_ID の行は書きません）: 管理番号 %s",
            record.key,
        )
        return ""
    try:
        return report_id_from_url(record.url)
    except SalesforceReportIDNotFoundError:
        logger.warning(
            "レポート ID を取り出せません（REPORT_ID の行は書きません）: 管理番号 %s, URL %s",
            record.key,
            record.url,
        )
        return ""


def _unique_stem(key: str, used: set[str]) -> str:
    """管理番号からファイル名・クラス名の名前の本体（拡張子なし）を作る。

    置換で同名になる管理番号は、 後の方に連番 ``_2`` を付ける。 連番の
    結果が別の管理番号とぶつかる場合は、 未使用の名前が出るまで連番を進める。

    Args:
        key: 管理番号。
        used: 既に使った名前の本体 の集合。 この関数の中で更新する。

    Returns:
        重複しない名前の本体。
    """
    stem = _INVALID_KEY_CHARS_PATTERN.sub("_", key)
    if stem not in used:
        used.add(stem)
        return stem
    count = 2
    while f"{stem}_{count}" in used:
        count += 1
    candidate = f"{stem}_{count}"
    used.add(candidate)
    return candidate


def _collect_column_names(record: ReportRecord) -> tuple[list[tuple[str, str]], list[str]]:
    """SELECT に入る列の ``(SOQL列名, 表示名)`` を集める。

    同じ表示名が 2 列以上あるときは、 その列は ``COLUMN_NAMES`` に入れない
    （同じ見出しが 2 つあると CSV の見出しが重なるため、 ダウンローダーの
    自動の対応に任せる） 。 返り値の 2 番目に外した SOQL 列名を入れる。

    Args:
        record: ``fetch.ReportRecord`` 。

    Returns:
        ``(COLUMN_NAMES に入れる組のリスト, 外した SOQL 列名のリスト)`` 。
    """
    metadata = record.report.get("reportMetadata", {})
    rows_by_key = {row.get("列キー", ""): row for row in record.column_map}
    pairs: list[tuple[str, str]] = []
    seen_fields: set[str] = set()
    for column_key in mapping.select_column_keys(metadata):
        row = rows_by_key.get(column_key)
        if row is None:
            continue
        soql_name = row.get("対応フィールドAPI名", "")
        if not soql_name or soql_name == "(不明)" or soql_name in seen_fields:
            continue
        seen_fields.add(soql_name)
        pairs.append((soql_name, row.get("表示名") or column_key))
    counts = _count_display_names([display_name for _soql_name, display_name in pairs])
    kept = [pair for pair, count in zip(pairs, counts, strict=True) if count < 2]
    duplicated = [
        soql_name
        for (soql_name, _display_name), count in zip(pairs, counts, strict=True)
        if count >= 2
    ]
    return kept, duplicated


def _count_display_names(display_names: list[str]) -> list[int]:
    """表示名ごとの件数を、 元の並び順で返す。"""
    counts: dict[str, int] = {}
    for display_name in display_names:
        counts[display_name] = counts.get(display_name, 0) + 1
    return [counts[display_name] for display_name in display_names]


def _triple_quoted_body(text: str) -> str:
    """3 重引用符の中に入る本文の形に整えた文字列を返す。

    ``repr()`` が出力する引用符の取り方 （ ``'`` か ``"`` か ） によって
    中のエスケープが違うので、 いったん本文だけを取り出して、 3 重引用符
    で壊れない形にそろえる。
    """
    quoted = repr(text)
    body = quoted[1:-1]
    # repr が選んだ引用符に対応するエスケープだけ戻す
    if quoted[0] == "'":
        body = body.replace("\\'", "'")
    else:
        body = body.replace('\\"', '"')
    # 3 重引用符の中で " が 3 つ続くと文字列が壊れるので、 全部 \" にする
    return body.replace('"', '\\"')


def _docstring_literal(text: str) -> str:
    """3 重引用符の docstring にする。

    バックスラッシュと 3 重引用符は、 そのままコードにならない形にする
    （ 中身を整える手順は ``_triple_quoted_body`` にまとめる ）。
    """
    return f'"""{_triple_quoted_body(text)}"""'


def _soql_triple_quoted(soql: str) -> str:
    """SOQL を 3 重引用符の文字列にする。

    SOQL は行の区切りを残したいので、 1 行ずつ ``_triple_quoted_body`` で
    エスケープしてから、 実際の改行でつなぐ。 中身は元の SOQL と 1 文字も
    違わない。
    """
    escaped_lines = [_triple_quoted_body(line) for line in soql.split("\n")]
    return '"""' + "\n".join(escaped_lines) + '"""'


def _flatten(text: str) -> str:
    """改行と制御文字を空白に置き換える。"""
    return _CONTROL_CHARS_PATTERN.sub(" ", text)
