"""境界で読む値を ``str`` に正規化する小さなヘルパー。

``_coerce_str`` は **境界** （ API / openpyxl / CSV の生の値 ） でだけ使う。
内側で自分の dataclass の ``str`` 属性を読むときに重ねがけしない。

各モジュールに重複して定義していた ``_coerce_str`` をここに集約する
（ 2026-10-15 の整理 ）。
"""

from __future__ import annotations


def coerce_str(value: object) -> str:
    """``None`` を空文字に、 それ以外はそのまま ``str`` にする。

    openpyxl のセル値・ ``client.request`` の JSON 値・ ``csv.reader`` の値
    など **境界の値** を ``str`` に正規化するために使う。 自分の dataclass
    の ``str`` 属性や、 既に ``str`` と分かっている値を渡す必要はない
    （ 内側では ``str(...)`` を直接書いて良い ）
    """
    if value is None:
        return ""
    return str(value)
