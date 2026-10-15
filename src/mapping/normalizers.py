"""``src.mapping`` 共通の正規化ヘルパー。

表示名や識別子の大小・空白・``_`` のゆらぎを揃えて照合する前に通す。
``src.mapping`` の他のモジュールから ``_`` 接頭辞で参照される内部関数だけを
置く。 公開シンボルはない。
"""

from __future__ import annotations

import re


def normalize_label(label: str) -> str:
    """表示名の前後空白を落として小文字化する（ 内部用 ）。

    Salesforce の表示名は前後に空白が入ることがあり、 また大小文字の
    ゆらぎは実フィールド側と一致しないことがあるため、 突き合わせの前に
    もう一段正規化する。 完全一致しなかったときのフォールバックは敢えて
    行わない — 不一致は「 不一致 」 のまま残し、 誤った候補を押し付けない
    ことを優先するため。
    """
    return label.strip().lower()


def normalize_identifier(value: str) -> str:
    """識別子の比較用に正規化する （ 内部用 ） 。 大文字小文字と ``_`` を無視する。"""
    return value.strip().lower().replace("_", "")


# レポートの列表示名に 「 オブジェクト: 項目名 」 の形 （ 半角・全角コロン、 前後の空白あり/なし ）
# があるかを判定する正規表現。 コロンは ``:`` （ U+003A ） と ``：`` （ U+FF1A ） の両方を
# 受け付ける
_OBJECT_LABEL_PATTERN = re.compile(r"^\s*(?P<object>.+?)\s*[:：]\s*(?P<field>.+?)\s*$")


def split_object_label(label: str) -> tuple[str | None, str]:
    """表示名が ``「 オブジェクト: 項目名 」`` 形式なら ``(オブジェクト, 項目)`` に分解する。

    コロンは半角・全角どちらでも受け付ける。 そうでないときは ``(None, label)`` を返す。
    """
    match = _OBJECT_LABEL_PATTERN.match(label)
    if match is None:
        return None, label
    object_part = match.group("object")
    field_part = match.group("field")
    return object_part, field_part
