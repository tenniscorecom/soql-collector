"""``src.row_size`` の単体テスト。 件数の 3 段判定を 1 つずつ確かめる。"""

from __future__ import annotations

from src.fetch.models import RowCheck
from src.row_check_status import ROW_CHECK_OVER, ROW_CHECK_UNDER, ROW_CHECK_UNKNOWN
from src.row_size import (
    SIZE_OVER_1000,
    SIZE_OVER_2000,
    SIZE_UNDER_1000,
    SIZE_UNKNOWN,
    decide_row_size,
)


def _under(rows: int) -> RowCheck:
    """「超えていない」 の 2000 行チェック。"""
    return RowCheck(status=ROW_CHECK_UNDER, rows=rows, reason=None)


def _over() -> RowCheck:
    """「超えている」 の 2000 行チェック。"""
    return RowCheck(status=ROW_CHECK_OVER, rows=None, reason=None)


def _unknown() -> RowCheck:
    """「判定不可」 の 2000 行チェック。"""
    return RowCheck(status=ROW_CHECK_UNKNOWN, rows=None, reason="判定できません")


# ── 1 段目: 管理表が ○ ──────────────────────────────────────────────────


def test_master_mark_means_over_2000() -> None:
    """管理表の「2000件超」列が ○ なら 2000 件超。"""
    size = decide_row_size(True, None)

    assert size.over_2000 is True
    assert size.over_1000 is True
    assert size.size == SIZE_OVER_2000


def test_master_mark_wins_over_row_check() -> None:
    """管理表が ○ なら、 サンドボックスの行数が小さくても 2000 件超。"""
    size = decide_row_size(True, _under(50))

    assert size.over_2000 is True
    assert size.size == SIZE_OVER_2000


# ── 2 段目: 管理表が × ──────────────────────────────────────────────────


def test_master_cross_with_small_row_count_is_under_1000() -> None:
    """管理表が × で、 サンドボックスの行数が 1000 件以下なら 1000 件以下。"""
    size = decide_row_size(False, _under(50))

    assert size.over_1000 is False
    assert size.over_2000 is False
    assert size.size == SIZE_UNDER_1000


def test_master_cross_with_row_count_over_1000() -> None:
    """管理表が × でも、 サンドボックスの行数が 1000 件超なら 1000 件超。"""
    size = decide_row_size(False, _under(1500))

    assert size.over_1000 is True
    assert size.over_2000 is False
    assert size.size == SIZE_OVER_1000


def test_master_cross_with_row_count_exactly_1000() -> None:
    """1000 件ちょうどは 1000 件以下 （ 1000 件を超えると Bulk なので境界は含めない ）。"""
    size = decide_row_size(False, _under(1000))

    assert size.over_1000 is False
    assert size.size == SIZE_UNDER_1000


def test_master_cross_wins_over_row_check_over() -> None:
    """管理表が × なら 2000 件超 にはしない。 サンドボックスより管理表を採る。"""
    size = decide_row_size(False, _over())

    assert size.over_2000 is False
    assert size.over_1000 is True
    assert size.size == SIZE_OVER_1000


def test_master_cross_without_usable_row_check() -> None:
    """管理表が × で 2000 行チェックが使えなくても 2000 件以下だと分かっている。"""
    for row_check in (None, _unknown()):
        size = decide_row_size(False, row_check)

        assert size.over_2000 is False
        assert size.over_1000 is None
        assert size.size == SIZE_UNKNOWN


# ── 3 段目: 管理表が無い ────────────────────────────────────────────────


def test_without_master_row_check_over_means_over_2000() -> None:
    """管理表が無くても、 2000 行チェックが「超えている」なら 2000 件超。"""
    size = decide_row_size(None, _over())

    assert size.over_2000 is True
    assert size.over_1000 is True
    assert size.size == SIZE_OVER_2000


def test_without_master_row_count_decides_1000() -> None:
    """管理表が無くても、 2000 行チェックの行数で 1000 件超 / 以下を分ける。"""
    assert decide_row_size(None, _under(50)).over_1000 is False
    assert decide_row_size(None, _under(1500)).over_1000 is True
    assert decide_row_size(None, _under(1500)).size == SIZE_OVER_1000


def test_without_master_unusable_row_check_is_unknown() -> None:
    """管理表も 2000 行チェックも判断材料が無ければ不明。"""
    for row_check in (None, _unknown()):
        size = decide_row_size(None, row_check)

        assert size.over_1000 is None
        assert size.over_2000 is None
        assert size.size == SIZE_UNKNOWN


# ── 根拠 ──────────────────────────────────────────────────────────────────


def test_basis_mentions_where_the_count_came_from() -> None:
    """根拠には、 管理表かサンドボックスかが書いてある。"""
    assert "管理表" in decide_row_size(True, None).basis
    assert "管理表" in decide_row_size(False, _under(50)).basis
    assert "サンドボックス" in decide_row_size(None, _under(50)).basis
