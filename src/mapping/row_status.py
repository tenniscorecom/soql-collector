"""列対応表の 1 行が 「どこから来た値か」 「状態はどう」 を返すヘルパー。

``column_source()`` は自動 / 人が直した / 取れず を、 ``column_status()`` は
OK / 要対応 / 要確認 を、 ``column_candidates()`` は備考の 「複数候補あり: …」
から候補部分だけを返す。 xlsx の列の対応シートと AI 用 Markdown が同じ
判定を使うため、 ここで返す文字列は両方で参照される。
"""

from __future__ import annotations


def column_source(row: dict[str, str]) -> str:
    """列の対応の 1 行が、 どこから来た値か （自動 / 人が直した / 取れず） 。

    人が直した値は ``column_fix_xlsx.apply_overrides`` が備考に印を残す。
    xlsx の列の対応シートと AI 用 Markdown が同じ判定を使う。
    """
    note = row["備考"]
    soql = row["対応フィールドAPI名"]
    if "人が直した（要確認: たどれない）" in note:
        return "人が直した（要確認: たどれない）"
    if "直したSOQL列名" in note:
        return "人が直した"
    if soql and soql != "(不明)":
        return "自動"
    return "取れず"


def column_status(row: dict[str, str]) -> str:
    """列の対応の 1 行の状態 （OK / 要対応 / 要確認） 。

    取れず → 要対応 （候補が複数あるときは 要確認） 、 人が直した値が項目定義で
    たどれない → 要確認、 それ以外 → OK。
    """
    soql = row["対応フィールドAPI名"]
    note = row["備考"]
    if not soql or soql == "(不明)":
        return "要確認" if "複数候補あり" in note else "要対応"
    if "人が直した（要確認: たどれない）" in note:
        return "要確認"
    return "OK"


def column_candidates(row: dict[str, str]) -> str:
    """備考の 「複数候補あり: …」 から候補の部分だけを返す。 無ければ空文字。"""
    note = row["備考"]
    return note.replace("複数候補あり: ", "") if "複数候補あり" in note else ""
