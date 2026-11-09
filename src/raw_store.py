"""src/raw_store.py — Salesforce から取った原本を JSON で保存する共通の部品。

解析で新しい項目が要るようになっても取り直さずに済むよう、 API の返り値は絞らずに
そのまま保存する。 ファイルの形は ``{"形式": 1, "組織": ..., "取得日時": ...,
"原本": <API の返り値そのまま>}`` 。 行データは入らない（describe の応答に無い）。

保存先は ``[FILES] DESCRIBE_CSV_PATH`` の親フォルダの下の ``元データ/`` 。

保存は速く・取り直さないためのもので、 壊れたファイルや書けない場所は警告にして
「保存していない」 扱いにする。
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

# 元データを置くフォルダ名（``DESCRIBE_CSV_PATH`` の親の下）
RAW_DIRNAME = "元データ"

# ファイルの形が変わったときに、 古いファイルを読まずに取り直すための印
FORMAT_VERSION = 1

# ファイル名に使えない文字（組織はホスト名、 ID と API 名は英数字だが、 念のため揃える）
_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]")


@dataclass(frozen=True)
class SavedOriginal:
    """保存してあった原本と、 それを Salesforce から取った日時。"""

    original: dict[str, Any]
    fetched_at: str


def now_iso() -> str:
    """取得日時の文字列（秒精度の ISO 8601）を返す。"""
    return datetime.now(UTC).isoformat(timespec="seconds")


def raw_file_path(folder: Path, org: str, name: str) -> Path:
    """``{組織}__{名前}.json`` のパスを返す。"""
    safe_name = _UNSAFE_FILENAME_CHARS.sub("_", f"{org}__{name}")
    return folder / f"{safe_name}.json"


def read_original(path: Path) -> SavedOriginal | None:
    """原本のファイルを読む。 無い・壊れている・形が違うときは ``None``。"""
    if not path.exists():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning(
            "保存した元データを読めないので、無いものとして扱います: %s（%s）", path, exc
        )
        return None
    original = document.get("原本") if isinstance(document, dict) else None
    if (
        not isinstance(document, dict)
        or document.get("形式") != FORMAT_VERSION
        or not isinstance(original, dict)
    ):
        logger.warning("保存した元データの形が違うので、無いものとして扱います: %s", path)
        return None
    return SavedOriginal(original=original, fetched_at=str(document.get("取得日時") or ""))


def write_original(path: Path, org: str, original: dict[str, Any], fetched_at: str) -> None:
    """原本を保存する。 半端なファイルが残らないよう、 一時ファイルから置き換える。"""
    document = {
        "形式": FORMAT_VERSION,
        "組織": org,
        "取得日時": fetched_at,
        "原本": original,
    }
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
            "元データを保存できませんでした（次の取得でまた取ります）: %s（%s）", path, exc
        )
        _remove_temporary(temporary)


def _remove_temporary(temporary: Path) -> None:
    """一時ファイルの後片付け。 消せなくても取得は止めない（保存失敗の警告は出してある）。"""
    try:
        temporary.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("一時ファイルを消せませんでした: %s（%s）", temporary, exc)
