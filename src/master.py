"""レポート管理表（Excel）から、管理番号ごとの最小行 dict を読む。

soql-collector が管理表で必要とする列は ``ID``・``概要``・``Salesforce URL``・
``有効`` の4つだけ。ダウンローダーはもっと広い列を読む前提なので、ここでは
最小の読み取りを直接書く。comken の ``Excel`` 経由で開くので、シートは
``PY_管理表`` を探す（comken の ``Excel.data_sheet('管理表')`` が ``PY_`` プレ
フィックスを自動で補う）。

「有効」が ``×`` の行は ``filter_enabled`` で落とす。URL が壊れている行は
行ごと読み込みエラーにせず、``fetch`` 側で個別に失敗として扱う（他の ID を止め
ない）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from comken.core.text import is_enabled_mark
from comken.toolbox.excel import Excel

logger = logging.getLogger(__name__)

# 管理表のシート名（comken 規約で実ファイルは ``PY_管理表``）。
SHEET_NAME = "管理表"

# 読む列。
COL_KEY = "ID"
COL_SUMMARY = "概要"
COL_URL = "Salesforce URL"
COL_ENABLED = "有効"


@dataclass(frozen=True)
class MasterEntry:
    """管理表の1行から拾う最小情報。"""

    key: str
    summary: str
    url: str
    enabled: bool


def read_master(path: Path) -> list[MasterEntry]:
    """管理表を読んで、行のリストを返す（管理表に並んでいる順を保つ）。

    空行（全列空）は読み飛ばす。``ID`` が空の行も飛ばす（管理番号が特定
    できない行は対象外）。
    """
    logger.debug("レポート管理表読込開始: path=%s", path)
    with Excel(path, read_only=True) as excel_obj:
        sheet = excel_obj.data_sheet(SHEET_NAME)
        raw_rows = sheet.table().read()
    entries: list[MasterEntry] = []
    for raw in raw_rows:
        if all(raw.get(col) in (None, "") for col in (COL_KEY, COL_SUMMARY, COL_URL, COL_ENABLED)):
            continue
        key = str(raw.get(COL_KEY) or "").strip()
        if not key:
            continue
        summary = str(raw.get(COL_SUMMARY) or "").strip()
        url = str(raw.get(COL_URL) or "").strip()
        enabled_text = str(raw.get(COL_ENABLED) or "")
        enabled = _is_enabled(enabled_text)
        entries.append(MasterEntry(key=key, summary=summary, url=url, enabled=enabled))
    logger.info("レポート管理表読込完了: path=%s, 件数=%d", path, len(entries))
    return entries


def filter_enabled(entries: list[MasterEntry]) -> list[MasterEntry]:
    """「有効」が真の行だけを残す。"""
    return [entry for entry in entries if entry.enabled]


def _is_enabled(value: object) -> bool:
    """管理表の「有効」列の値を、真とみなすかどうか判定する。

    ``comken.core.text.is_enabled_mark`` に委譲する。 ``bool`` / ``int`` /
    ``str`` を ``○`` / ``有効`` / ``はい`` / ``true`` 等で真とみなし、 ``×`` /
    ``無効`` / 空文字 / ``false`` 等で偽とみなす。 判定基準は comken 側の
    1 か所 (``is_enabled_mark``) に集約し、 ``Salesforceレポートダウン
    ローダー`` の ``report_master._to_bool`` とも語集合をそろえる。
    """
    return is_enabled_mark(value)
