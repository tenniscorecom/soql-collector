"""列対応表の 1 行が 「どこから来た値か」 「状態はどう」 を返すヘルパー。

``column_source()`` は自動 / 人が直した / 取れず を、 ``column_status()`` は
OK / 要対応 / 要確認 を、 ``column_candidates()`` は備考の 「複数候補あり: …」
から候補部分だけを返す。 xlsx の列の対応シートと AI 用 Markdown が同じ
判定を使うため、 ここで返す文字列は両方で参照される。

「複数候補から自動で確定」 （ 主オブジェクトに最も近い候補を自動で採用した
とき ） の行も、 状態は要確認、 候補の列には他の候補を出す。
"""

from __future__ import annotations

import re

from src.mapping.owner import OWNER_POLYMORPHIC_NOTE

# 複数候補の中で経路が最短のものを自動で採用したときの、 備考の先頭。
# この文字列で始まれば 「自動で確定した行」 と分かる
AUTO_PATH_NOTE = "複数候補から自動で確定（経路が最短）"

# 自動確定の備考の定型。 ``{AUTO_PATH_NOTE}: 採用 {採用した列} / 他の候補: {他の候補}``
_AUTO_PATH_SEPARATOR = " / 他の候補: "

# 備考の末尾に付く所有者ポリモーフィックの注意書き （ ``（…）`` ）。
# 自動確定の 「他の候補」 を読み出すときに取り除く
_OWNER_NOTE_SUFFIX = f"（{OWNER_POLYMORPHIC_NOTE}）"

# 末尾の （…） を取り除く正規表現
_TRAILING_PARENTHESES = re.compile(r"（[^（）]*）$")


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
    たどれない → 要確認、 複数候補から自動で確定した → 要確認、 それ以外 → OK。
    """
    soql = row["対応フィールドAPI名"]
    note = row["備考"]
    if not soql or soql == "(不明)":
        return "要確認" if "複数候補あり" in note else "要対応"
    if "人が直した（要確認: たどれない）" in note:
        return "要確認"
    if auto_path_parts(note) is not None:
        return "要確認"
    return "OK"


def column_candidates(row: dict[str, str]) -> str:
    """備考の候補の部分だけを返す。 無ければ空文字。

    「複数候補あり: …」 のときはその候補、 複数候補から自動で確定した行では
    採用しなかった側の候補を返す。
    """
    note = row["備考"]
    if "複数候補あり" in note:
        return note.replace("複数候補あり: ", "")
    parts = auto_path_parts(note)
    if parts is not None:
        return parts[1]
    return ""


def auto_path_parts(note: str) -> tuple[str, str] | None:
    """備考から自動確定の ``(採用した列, 他の候補)`` を取り出す。

    自動で確定した行でないときは ``None`` 。 候補の区切りは
    ``{AUTO_PATH_NOTE}: 採用 X / 他の候補: Y`` の形。
    """
    if not note.startswith(AUTO_PATH_NOTE):
        return None
    body = note[len(AUTO_PATH_NOTE) :].strip()
    if body.startswith(":"):
        body = body[1:].strip()
    if _AUTO_PATH_SEPARATOR not in body:
        return body, ""
    adopted, others = body.split(_AUTO_PATH_SEPARATOR, 1)
    others = others.strip()
    # 所有者ポリモーフィックの注意書きが末尾に付いていたら取り除く
    if others.endswith(_OWNER_NOTE_SUFFIX):
        others = others[: -len(_OWNER_NOTE_SUFFIX)].strip()
    else:
        others = _TRAILING_PARENTHESES.sub("", others).strip()
    return adopted.strip(), others
