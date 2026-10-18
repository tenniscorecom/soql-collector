"""Excel 出力で使い回す、 列幅と見栄えのヘルパー。

``tables.SOQL調査.xlsx`` と ``column_fix_xlsx.列の直し.xlsx`` の両方が
列幅の計算と見出しの固定を共通で持つように、 ここで 1 か所にまとめる。
openpyxl 依存はこのモジュール内でだけ持つ
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

# 列幅の最小 / 最大
MIN_COLUMN_WIDTH = 8
MAX_COLUMN_WIDTH = 60

# 薄い赤 (要対応) / 薄い黄 (要確認) / 薄い緑 (人が書くセル)
FILL_NEEDS_FIX = "F8CBAD"
FILL_NEEDS_CHECK = "FFF2CC"
FILL_USER_INPUT = "C6EFCE"


def display_width(text: str) -> int:
    """表示幅を計算する。 全角は 2 、 半角は 1 としてカウント。"""
    width = 0
    for ch in text:
        if _is_wide(ch):
            width += 2
        else:
            width += 1
    return width


# 全角とみなす Unicode 範囲 ( start, end ) のタプル。 CJK 漢字・ ひらがな・
# カタカナ・ 全角記号を網羅する。 ``_is_wide`` から線形探索で当たる
_WIDE_RANGES: tuple[tuple[int, int], ...] = (
    (0x1100, 0x115F),  # Hangul Jamo
    (0x2E80, 0x303E),  # CJK Radicals / Kangxi / Symbols and Punctuation
    (0x3041, 0x33FF),  # Hiragana / Katakana / CJK Symbols / CJK Compat
    (0x3400, 0x4DBF),  # CJK Unified Ideographs Extension A
    (0x4E00, 0x9FFF),  # CJK Unified Ideographs
    (0xA000, 0xA4CF),  # Yi
    (0xAC00, 0xD7A3),  # Hangul Syllables
    (0xF900, 0xFAFF),  # CJK Compatibility Ideographs
    (0xFE30, 0xFE4F),  # CJK Compatibility Forms
    (0xFF00, 0xFF60),  # Fullwidth Forms
    (0xFFE0, 0xFFE6),  # Fullwidth Signs
)

# 全角扱いする単独コードポイント (句読点・ 踊り字)
_WIDE_SINGLE_CODEPOINTS: frozenset[int] = frozenset({0x3000, 0x3001, 0x3002, 0x3003})


def _is_wide(ch: str) -> bool:
    """全角文字とみなすかの判定。 CJK 漢字・ ひらがな・ カタカナ・ 全角記号。"""
    if not ch:
        return False
    code = ord(ch)
    if code in _WIDE_SINGLE_CODEPOINTS:
        return True
    return any(start <= code <= end for start, end in _WIDE_RANGES)


def adjust_column_widths(
    worksheet: Any,
    headers: Sequence[str],
    rows: Sequence[dict[str, str]],
    *,
    start_row: int,
) -> None:
    """列幅を 「全角は 2 ・ 半角は 1 としてカウント」 した値の最大 + 余白 で決める。

    最小 ``MIN_COLUMN_WIDTH`` 、 最大 ``MAX_COLUMN_WIDTH`` 。 最大を超える
    列は折り返し表示にする
    """
    from openpyxl.styles import Alignment
    from openpyxl.utils import get_column_letter as _gcl

    for index, header in enumerate(headers, start=1):
        max_width = display_width(header)
        for row in rows:
            value = row.get(header, "")
            max_width = max(max_width, display_width(str(value)))
        col_letter = _gcl(index)
        width = max(MIN_COLUMN_WIDTH, min(MAX_COLUMN_WIDTH, max_width + 2))
        worksheet.column_dimensions[col_letter].width = width
        if max_width >= MAX_COLUMN_WIDTH and rows:
            # 折り返し
            for r in range(start_row, start_row + len(rows) + 1):
                cell = worksheet.cell(row=r, column=index)
                cell.alignment = Alignment(
                    wrap_text=cell.alignment.wrap_text or True,
                    horizontal=cell.alignment.horizontal,
                    vertical=cell.alignment.vertical,
                )
