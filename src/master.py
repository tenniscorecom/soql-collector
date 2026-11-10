"""レポート管理表（Excel）から、管理番号ごとの最小行 dict を読む。

soql-collector が管理表で必要とする列は ``ID``・``概要``・``Salesforce URL``・
``有効`` の4つと、あれば ``調査`` と ``2000件超`` と ``集計形式``。ダウンローダーは
もっと広い列を読む前提なので、ここでは最小の読み取りを直接書く。comken の ``Excel``
経由で開くので、シートは ``PY_管理表`` を探す（comken の
``Excel.data_sheet('管理表')`` が ``PY_`` プレフィックスを自動で補う）。

「有効」が ``×`` の行は ``filter_survey_targets`` で落とす。
**``調査`` 列がある管理表では**、 さらに「調査」が ``○`` でない行
（有効だが今回は調べない行）も落とす。
``調査`` 列が無い管理表は、 今までどおり「有効」だけで決まる。URL が壊れている行は
行ごと読み込みエラーにせず、``fetch`` 側で個別に失敗として扱う（他の ID を止め
ない）。

``2000件超`` 列は任意。 本番の件数が 2000 件を超えるかどうかは、 サンドボックスの
2000 行チェックでは分からない（ サンドボックスで 2000 件以下でも本番では超えることが
ある） ので、 本番を知っている人が書く。 値は ``○`` / ``×`` で読み、 列が無い・セルが
空のとき・``不明`` と書かれたときは「不明」（ ``None`` ） として扱う。

``集計形式`` 列も任意。 値は ``サマリー`` / ``マトリックス`` / ``無し`` の 3 つ。
形式はレポート定義（ ``reportMetadata.reportFormat`` ） から分かるので、 この列は
人が記入する確認用。 列が無い・セルが空のときは ``None`` 。 3 つ以外の値を書いた
行は ``None`` にして WARNING を 1 件出す（ 行は捨てない ） 。
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
# 任意の列。 あれば「有効」のうち今回調べる行を ``○`` で選ぶ（downloader の「有効」と意味を分ける）
COL_SURVEY = "調査"
# 任意の列。 本番の件数が 2000 件を超えるかどうか。 本当の件数を知っている人が書く
COL_EXCEEDS_ROW_LIMIT = "2000件超"
# 任意の列。 レポートの集計形式。 人が記入する確認用
COL_REPORT_FORMAT = "集計形式"

# 「2000件超」列に「本番の件数が分からない」と書くときの印。 空欄と同じ扱い
# （ ``None`` ） にする。 「×」は「2000 件以下だと分かっている」を表すので、
# 分からないことを表す別の語が要る
UNKNOWN_MARK = "不明"

# 「集計形式」列に書ける値
REPORT_FORMAT_SUMMARY = "サマリー"
REPORT_FORMAT_MATRIX = "マトリックス"
REPORT_FORMAT_NONE = "無し"

# 管理表の「集計形式」列の値 → レポートの ``reportMetadata.reportFormat`` の対応
REPORT_FORMAT_BY_MASTER_VALUE: dict[str, str] = {
    REPORT_FORMAT_SUMMARY: "SUMMARY",
    REPORT_FORMAT_MATRIX: "MATRIX",
    REPORT_FORMAT_NONE: "TABULAR",
}


@dataclass(frozen=True)
class MasterEntry:
    """管理表の1行から拾う最小情報。"""

    key: str
    summary: str
    url: str
    enabled: bool
    #: 今回調べる行か。 ``調査`` 列が無い管理表では、 すべて ``True``
    survey: bool = True
    #: 本番の件数が 2000 件を超えるか。 管理表に「2000件超」列が無い・セルが空の
    #: とき・セルが「不明」のときは ``None`` （ 「不明」 ）
    exceeds_row_limit: bool | None = None
    #: 管理表の「集計形式」列の値。 ``サマリー`` / ``マトリックス`` / ``無し`` の
    #: どれか。 列が無い・セルが空・想定外の値のときは ``None`` （ 記入が無い ）
    report_format: str | None = None


def read_master(path: Path) -> list[MasterEntry]:
    """管理表を読んで、行のリストを返す（管理表に並んでいる順を保つ）。

    空行（全列空）は読み飛ばす。``ID`` が空の行も飛ばす（管理番号が特定
    できない行は対象外）。
    """
    logger.debug("レポート管理表読込開始: path=%s", path)
    with Excel(path, read_only=True) as excel_obj:
        sheet = excel_obj.data_sheet(SHEET_NAME)
        table = sheet.table().read()
    raw_rows = list(table)
    has_survey_column = COL_SURVEY in table.columns
    has_exceeds_column = COL_EXCEEDS_ROW_LIMIT in table.columns
    has_report_format_column = COL_REPORT_FORMAT in table.columns
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
        survey = _is_enabled(raw.get(COL_SURVEY)) if has_survey_column else True
        exceeds_row_limit = (
            _read_exceeds_row_limit(raw.get(COL_EXCEEDS_ROW_LIMIT)) if has_exceeds_column else None
        )
        report_format = (
            _read_report_format(raw.get(COL_REPORT_FORMAT), key=key)
            if has_report_format_column
            else None
        )
        entries.append(
            MasterEntry(
                key=key,
                summary=summary,
                url=url,
                enabled=enabled,
                survey=survey,
                exceeds_row_limit=exceeds_row_limit,
                report_format=report_format,
            )
        )
    logger.info("レポート管理表読込完了: path=%s, 件数=%d", path, len(entries))
    return entries


def filter_survey_targets(entries: list[MasterEntry]) -> list[MasterEntry]:
    """今回調べる行（「有効」が真で、 ``調査`` 列があれば「調査」も真）だけを残す。"""
    return [entry for entry in entries if entry.enabled and entry.survey]


def _is_enabled(value: object) -> bool:
    """管理表の「有効」列の値を、真とみなすかどうか判定する。

    ``comken.core.text.is_enabled_mark`` に委譲する。 ``bool`` / ``int`` /
    ``str`` を ``○`` / ``有効`` / ``はい`` / ``true`` 等で真とみなし、 ``×`` /
    ``無効`` / 空文字 / ``false`` 等で偽とみなす。 判定基準は comken 側の
    1 か所 (``is_enabled_mark``) に集約し、 ``Salesforceレポートダウン
    ローダー`` の ``report_master._to_bool`` とも語集合をそろえる。
    """
    return is_enabled_mark(value)


def _read_exceeds_row_limit(value: object) -> bool | None:
    """管理表の「2000件超」列の値を ``True`` / ``False`` / ``None`` に直す。

    セルが空のときは ``None`` （ 「本番の件数は分からない」 ） 。 ``不明``
    （ 前後の空白は無視） と書かれたときも同じ扱い。 値があるときは
    ``_is_enabled`` （ ``is_enabled_mark`` ） で真偽を判定する。 判定の
    語集合を「有効」列とそろえるため、 別の判定は作らない。
    """
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text or text == UNKNOWN_MARK:
            return None
    return _is_enabled(value)


def _read_report_format(value: object, *, key: str) -> str | None:
    """管理表の「集計形式」列の値を、 書ける値に直す。

    セルが空のときは ``None`` （ 「記入が無い」 ） 。 「サマリー」/「マトリックス」/
    「無し」 のどれかならそのまま返す。 それ以外の値は ``None`` にして
    WARNING を 1 件出す（ 行は捨てない ） 。
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text not in REPORT_FORMAT_BY_MASTER_VALUE:
        logger.warning(
            "管理表の「集計形式」列の値が想定外です（その行は集計形式なしとして扱います）: "
            "管理番号=%s, 値=%s",
            key,
            text,
        )
        return None
    return text


def report_format_warning(master_value: str | None, report_format: str | None) -> str | None:
    """管理表の「集計形式」列と レポートの ``reportFormat`` の食い違いの警告を返す。

    両方そろっていて 対応と違うときだけ、 警告 1 件分の文言を返す。 どちらか
    無ければ ``None`` （ 警告しない ） 。 形式が ``MULTI_BLOCK`` など対応に
    無い値でも、 食い違いとして扱う。
    """
    if master_value is None:
        return None
    expected = REPORT_FORMAT_BY_MASTER_VALUE.get(master_value)
    if expected is None:
        return None
    actual = str(report_format or "").strip()
    if not actual or actual == expected:
        return None
    return f"管理表の集計形式は {master_value} だが、レポートは {actual}"
