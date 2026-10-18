"""src/report_store.py — レポート定義（describe）を実行をまたいで JSON に貯める。

オブジェクトの describe は ``describe_store.py`` が CSV に貯めて再利用する。 レポートの
describe は、 以前は毎回 Salesforce に聞いていた。 ``列の直し.xlsx`` を直してもう一度実行する
たびに、 有効な全レポート分を取り直すことになる。 取れたものは貯めて、 次からは API を呼ばない。

- 保存先: ``describe_csv_path`` と同じ ``data/`` の中の ``レポート定義/``
  （1 レポート 1 ファイル、 ``{組織}__{レポートID}.json``）
- 貯めるのは ``_slim_report`` で SOQL 組立に要る部分だけに絞った定義と、 そのとき落としたキー名
  （警告表の「レポート定義の中で使わなかったキー」を、 貯めたものから読んでも変えないため）。
  レポートの行データは取らない（describe の応答に入っていない）
- **期限は無い。** 作り直したいときは ``レポート定義/`` フォルダごと（または 1 ファイル）消す。
  調査はサンドボックスで行い、 本番のレポートを直しながら回す使い方ではない前提
- 壊れたファイル・書けない場所は、 警告を出して「貯めていない」扱いにする（キャッシュは速くする
  ためだけのもので、 無くても動く）
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 保存先のフォルダ名（``describe_csv_path`` の親の下）
REPORT_STORE_DIRNAME = "レポート定義"

# ファイルの形が変わったときに、 古いファイルを読まずに取り直すための印
_FORMAT_VERSION = 1

# ファイル名に使えない文字（組織はホスト名、 レポート ID は英数字だが、 念のため揃える）
_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]")


@dataclass(frozen=True)
class CachedReport:
    """貯めてあったレポート定義。"""

    slim_report: dict[str, Any]
    dropped_keys: tuple[str, ...]


class ReportStore:
    """レポート定義の保存場所。 ``folder`` が ``None`` のときは何も貯めない（毎回 API を使う）。"""

    def __init__(self, folder: Path | None) -> None:
        self._folder = folder
        #: 貯めてあったものを使った件数（実行後のログ用）
        self.hit_count = 0
        #: Salesforce から取り直した件数（実行後のログ用）
        self.api_count = 0

    @classmethod
    def load(cls, describe_csv_path: Path) -> ReportStore:
        """``describe_csv_path`` と同じ ``data/`` の中の ``レポート定義/`` を保存場所にする。"""
        return cls(describe_csv_path.parent / REPORT_STORE_DIRNAME)

    @classmethod
    def empty(cls) -> ReportStore:
        """何も貯めない保存場所（テストや単発の実行用）。"""
        return cls(None)

    def get(self, org: str, report_id: str) -> CachedReport | None:
        """貯めてあれば返す。 無い・壊れているときは ``None``（呼び出し側が API で取る）。"""
        if self._folder is None:
            return None
        path = self._path_for(org, report_id)
        if not path.exists():
            return None
        cached = _read(path)
        if cached is not None:
            self.hit_count += 1
        return cached

    def put(
        self,
        org: str,
        report_id: str,
        slim_report: dict[str, Any],
        dropped_keys: tuple[str, ...],
    ) -> None:
        """API で取り直した定義を貯める。 中身が空（壊れた応答）のときは貯めない。"""
        self.api_count += 1
        if self._folder is None or not slim_report.get("reportMetadata"):
            return
        document = {
            "形式": _FORMAT_VERSION,
            "組織": org,
            "レポートID": report_id,
            "取得日時": datetime.now(UTC).isoformat(timespec="seconds"),
            "落としたキー": list(dropped_keys),
            "レポート定義": slim_report,
        }
        _write(self._path_for(org, report_id), document)

    def _path_for(self, org: str, report_id: str) -> Path:
        assert self._folder is not None
        name = _UNSAFE_FILENAME_CHARS.sub("_", f"{org}__{report_id}")
        return self._folder / f"{name}.json"


def _read(path: Path) -> CachedReport | None:
    """1 ファイルを読む。 形が違う・壊れているときは警告を出して ``None``。"""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("貯めてあったレポート定義を読めないため取り直します: %s（%s）", path, exc)
        return None
    slim_report = document.get("レポート定義") if isinstance(document, dict) else None
    dropped = document.get("落としたキー") if isinstance(document, dict) else None
    if (
        not isinstance(document, dict)
        or document.get("形式") != _FORMAT_VERSION
        or not isinstance(slim_report, dict)
        or not isinstance(slim_report.get("reportMetadata"), dict)
        or not isinstance(slim_report.get("reportExtendedMetadata"), dict)
        or not isinstance(dropped, list)
    ):
        logger.warning("貯めてあったレポート定義の形が違うため取り直します: %s", path)
        return None
    return CachedReport(
        slim_report=slim_report,
        dropped_keys=tuple(str(key) for key in dropped),
    )


def _write(path: Path, document: dict[str, Any]) -> None:
    """途中で止まっても半端なファイルが残らないよう、 一時ファイルに書いてから置き換える。"""
    temporary = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        os.replace(temporary, path)
    except OSError as exc:
        logger.warning(
            "レポート定義を貯められませんでした（次回も API で取ります）: %s（%s）", path, exc
        )
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass  # 一時ファイルの後片付けに失敗しても、 取得は止めない
