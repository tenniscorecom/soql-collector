"""src/report_store.py — レポートの describe の原本と件数を保存する。

- ``元データ/レポート/{組織}__{レポートID}.json``: ``client.report.describe`` の返り値そのまま
- ``元データ/件数.csv``: 組織 / レポートID / 判定 / 行数 / 理由 / 取得日時。 行データは保存しない

``force=True`` で作ると、 実行の最初に保存を見ない（全部取り直す）。 保存は上書きされ、
同じ実行の中で一度取ったものはメモリから返す。 取り直しに失敗したものの保存は残る。
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from comken.core.table import Table
from comken.exceptions import CSVError
from comken.toolbox.csv import CSV

from src.raw_store import RAW_DIRNAME, now_iso, raw_file_path, read_original, write_original

logger = logging.getLogger(__name__)

# 保存先のフォルダ名（``元データ/`` の下）
REPORT_STORE_DIRNAME = "レポート"

# 件数を保存する CSV のファイル名（``元データ/`` の下）
ROW_COUNT_CSV_NAME = "件数.csv"

# 件数の CSV の列（この順）
ROW_COUNT_CSV_COLUMNS: tuple[str, ...] = (
    "組織",
    "レポートID",
    "判定",
    "行数",
    "理由",
    "取得日時",
)


@dataclass(frozen=True)
class SavedRowCount:
    """保存してあった件数の判定。"""

    status: str
    rows: int | None
    reason: str | None
    fetched_at: str


class ReportStore:
    """レポートの原本と件数の保存場所。 ``folder`` が ``None`` なら保存せずメモリだけで動く。"""

    def __init__(
        self,
        folder: Path | None,
        row_count_path: Path | None = None,
        *,
        force: bool = False,
    ) -> None:
        self._folder = folder
        self._row_count_path = row_count_path
        self._force = force
        self._memory: dict[tuple[str, str], dict[str, Any]] = {}
        self._fetched_at: dict[tuple[str, str], str] = {}
        self._saved_row_counts: dict[tuple[str, str], SavedRowCount] = (
            _load_row_counts(row_count_path) if row_count_path is not None else {}
        )
        self._refreshed_row_counts: set[tuple[str, str]] = set()
        #: 保存から読んだ件数（実行後のログ用）
        self.hit_count = 0
        #: ``report.describe`` を呼んだ回数（実行後のログ用）
        self.api_count = 0
        #: ``report.get`` を呼んだ回数（実行後のログ用）
        self.row_count_api_count = 0
        #: レポートの describe に失敗した、 または中身が空だった件数（終了コードの判定に使う）
        self.failure_count = 0

    @classmethod
    def load(cls, describe_csv_path: Path, *, force: bool = False) -> ReportStore:
        """``describe_csv_path`` と同じフォルダの ``元データ/`` を保存場所にする。"""
        raw_dir = describe_csv_path.parent / RAW_DIRNAME
        return cls(raw_dir / REPORT_STORE_DIRNAME, raw_dir / ROW_COUNT_CSV_NAME, force=force)

    @classmethod
    def empty(cls) -> ReportStore:
        """何も保存しない保存場所（テストや単発の実行用）。"""
        return cls(None)

    def get(self, org: str, report_id: str) -> dict[str, Any] | None:
        """レポートの原本を返す。 無い・壊れているときは ``None``（呼び出し側が API で取る）。"""
        key = (org, report_id)
        if key in self._memory:
            return self._memory[key]
        if self._folder is None or self._force:
            return None
        saved = read_original(raw_file_path(self._folder, org, report_id))
        if saved is None:
            return None
        self.hit_count += 1
        self._memory[key] = saved.original
        self._fetched_at[key] = saved.fetched_at
        return saved.original

    def put(self, org: str, report_id: str, original: dict[str, Any]) -> None:
        """API で取った原本を保存する。 中身が空（壊れた応答）のときは保存しない。"""
        self.api_count += 1
        if not (isinstance(original, dict) and original.get("reportMetadata")):
            self.failure_count += 1
            return
        key = (org, report_id)
        fetched_at = now_iso()
        self._memory[key] = original
        self._fetched_at[key] = fetched_at
        if self._folder is not None:
            write_original(raw_file_path(self._folder, org, report_id), org, original, fetched_at)

    def record_failure(self) -> None:
        """レポートの describe に失敗したことを数える（原本は保存しない）。"""
        self.api_count += 1
        self.failure_count += 1

    def fetched_at(self, org: str, report_id: str) -> str:
        """レポートの原本を取った日時。 分からなければ空文字。"""
        return self._fetched_at.get((org, report_id), "")

    def get_row_count(self, org: str, report_id: str) -> SavedRowCount | None:
        """保存した件数の判定を返す。 無いときは ``None``。"""
        key = (org, report_id)
        if self._force and key not in self._refreshed_row_counts:
            return None
        return self._saved_row_counts.get(key)

    def put_row_count(
        self,
        org: str,
        report_id: str,
        *,
        status: str,
        rows: int | None,
        reason: str | None,
    ) -> None:
        """件数の判定を保存する。 書けないときは警告だけ出す（次の取得でまた取る）。"""
        key = (org, report_id)
        self._saved_row_counts[key] = SavedRowCount(status, rows, reason, now_iso())
        self._refreshed_row_counts.add(key)
        self._write_row_counts()

    def prune(self, keep_report_ids: Collection[str]) -> list[str]:
        """``keep_report_ids`` に無いレポートの原本と件数を消し、 消したファイル名を返す。

        今回調べないレポート（管理表から消えた・「有効」が ``×``・「調査」が ``○`` でない）の
        保存を残さないための掃除。 消すのはこのストアのフォルダの、 このストアが作る形の
        ファイル名（``{組織}__{レポートID}.json``）だけ。
        ``keep_report_ids`` が空のときは何も消さない（管理表を読み違えても、 全部は消えない）。
        """
        if not keep_report_ids:
            return []
        self._prune_row_counts(keep_report_ids)
        if self._folder is None or not self._folder.is_dir():
            return []
        removed: list[str] = []
        for path in sorted(self._folder.glob("*.json")):
            if "__" not in path.stem or path.stem.rsplit("__", 1)[-1] in keep_report_ids:
                continue
            try:
                path.unlink()
            except OSError as exc:
                logger.warning("対象外のレポートの元データを消せませんでした: %s（%s）", path, exc)
                continue
            removed.append(path.name)
        return removed

    def _prune_row_counts(self, keep_report_ids: Collection[str]) -> None:
        kept = {
            key: value for key, value in self._saved_row_counts.items() if key[1] in keep_report_ids
        }
        if len(kept) == len(self._saved_row_counts):
            return
        self._saved_row_counts = kept
        self._write_row_counts()

    def _write_row_counts(self) -> None:
        if self._row_count_path is None:
            return
        rows = [
            {
                "組織": org,
                "レポートID": report_id,
                "判定": saved.status,
                "行数": "" if saved.rows is None else str(saved.rows),
                "理由": saved.reason or "",
                "取得日時": saved.fetched_at,
            }
            for (org, report_id), saved in sorted(self._saved_row_counts.items())
        ]
        try:
            self._row_count_path.parent.mkdir(parents=True, exist_ok=True)
            with CSV(self._row_count_path, columns=list(ROW_COUNT_CSV_COLUMNS)) as csv:
                csv.replace(Table(list(ROW_COUNT_CSV_COLUMNS), rows))
        except (OSError, CSVError) as exc:
            logger.warning(
                "件数を保存できませんでした（次の取得でまた取ります）: %s（%s）",
                self._row_count_path,
                exc,
            )


def _load_row_counts(path: Path) -> dict[tuple[str, str], SavedRowCount]:
    """``件数.csv`` を読む。 無ければ空。 見出しが違うときは ``CSVError``。"""
    if not path.exists():
        return {}
    with CSV(path) as csv:
        table = csv.read()
    if list(table.columns) != list(ROW_COUNT_CSV_COLUMNS):
        raise CSVError(
            f"{path.name} の見出しが想定と違います: {path}\n"
            f"期待: {list(ROW_COUNT_CSV_COLUMNS)}\n"
            f"実際: {list(table.columns)}\n"
            "対処: ファイルを別名に退避してから、取得.bat で取り直してください。"
        )
    result: dict[tuple[str, str], SavedRowCount] = {}
    for row in table.to_rows():
        rows_text = row["行数"].strip()
        if rows_text and not rows_text.isdigit():
            logger.warning(
                "件数.csv の行数が数字でないので読み飛ばします: レポートID=%s", row["レポートID"]
            )
            continue
        result[(row["組織"], row["レポートID"])] = SavedRowCount(
            status=row["判定"],
            rows=int(rows_text) if rows_text else None,
            reason=row["理由"] or None,
            fetched_at=row["取得日時"],
        )
    return result
