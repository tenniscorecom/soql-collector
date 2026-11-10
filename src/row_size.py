"""レポートの件数がどの区分かを 3 段で決める。

判定の材料は 2 つ:

- 管理表の「2000件超」列（ ``MasterEntry.exceeds_row_limit`` ）。 本番の件数を
  知っている人が書くので、 サンドボックスの 2000 行チェックより 優先する
- サンドボックスの 2000 行チェック（ ``RowCheck`` ）。 サンドボックスで
  2000 件以下でも本番では超えることがあるので、 決め手にはしない

段階は 3 つ:

1. 管理表が ``○`` なら 2000 件超
2. 管理表が ``×`` なら 2000 件以下。 サンドボックスの 2000 行チェックが
   「超えていない」で行数が取れていれば、 その行数が 1000 件を超えるかで
   1000 件超 / 1000 件以下を分ける
3. 管理表の列が無い（ 空） なら サンドボックスの 2000 行チェックだけで決める。
   「超えている」なら 2000 件超、 「超えていない」なら行数で 1000 件超 /
   1000 件以下を分ける。 判定不可・未実施なら不明

1000 件以下かどうかが分かるのは、 サンドボックスの行数が取れたときだけ。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.row_check_status import ROW_CHECK_OVER, ROW_CHECK_UNDER

if TYPE_CHECKING:
    from src.fetch.models import RowCheck


# 1000 件を超えるレポートを Bulk API 2.0 で取る境目。 これより多いときは
# Bulk、 これ以下のときは通常 SOQL （ REST の query() ） で取る
BULK_MIN_ROWS: int = 1000

# 副問い合わせ （ クロスフィルタ ） があるときの、 通常 SOQL で取れる件数の上限。
# これを超えるなら、 SOQL を分けて取る必要がある
REST_MAX_ROWS_WITH_SUBQUERY: int = 5000


# 件数の区分。 表示にも使う
SIZE_OVER_2000: str = "2000件超"
SIZE_OVER_1000: str = "1000件超"
SIZE_UNDER_1000: str = "1000件以下"
SIZE_UNKNOWN: str = "不明"


@dataclass(frozen=True)
class RowSize:
    """件数の判定結果。

    Attributes:
        over_1000: 1000 件を超えると分かっているか。 ``None`` は分からない。
        over_2000: 2000 件を超えると分かっているか。 ``None`` は分からない。
        basis: 根拠。 管理表かサンドボックスかで表示の文言が変わるので、 必ず
            入れておく。
    """

    over_1000: bool | None
    over_2000: bool | None
    basis: str

    @property
    def size(self) -> str:
        """表示用の区分。 ``SIZE_*`` のいずれか。"""
        if self.over_2000:
            return SIZE_OVER_2000
        if self.over_1000:
            return SIZE_OVER_1000
        if self.over_1000 is False:
            return SIZE_UNDER_1000
        return SIZE_UNKNOWN


def decide_row_size(
    exceeds_row_limit: bool | None,
    row_check: RowCheck | None,
) -> RowSize:
    """管理表の「2000件超」列と 2000 行チェックから、 件数の区分を返す。

    Args:
        exceeds_row_limit: 管理表の「2000件超」列の値。 ``True`` = 2000 件超、
            ``False`` = 2000 件以下、 ``None`` = 列が無い・空 （ 不明 ） 。
        row_check: サンドボックスの 2000 行チェックの結果。 無ければ ``None`` 。

    Returns:
        ``RowSize`` 。 管理表が分かっているときは、 サンドボックスより
        管理表を採る。
    """
    status = row_check.status if row_check is not None else None
    rows = row_check.rows if row_check is not None else None

    if exceeds_row_limit is True:
        # 管理表が ○ なら 2000 件超。 サンドボックスが何と言おうと管理表を採る
        return RowSize(over_1000=True, over_2000=True, basis="管理表の「2000件超」列が ○")

    if exceeds_row_limit is False:
        # 管理表が × なら 2000 件以下。 2000 件超 にはしない
        if status == ROW_CHECK_OVER:
            return RowSize(
                over_1000=True,
                over_2000=False,
                basis="管理表で 2000 件以下・サンドボックスで 2000 件超",
            )
        if status == ROW_CHECK_UNDER and rows is not None:
            return _size_by_rows(rows, "管理表で 2000 件以下・サンドボックスで {} 件")
        return RowSize(over_1000=None, over_2000=False, basis="管理表で 2000 件以下")

    # 管理表の列が無い（ 空） なら サンドボックスの 2000 行チェックだけで決める
    if status == ROW_CHECK_OVER:
        return RowSize(
            over_1000=True,
            over_2000=True,
            basis="サンドボックスの 2000 行チェックで 2000 件超",
        )
    if status == ROW_CHECK_UNDER and rows is not None:
        return _size_by_rows(rows, "サンドボックスの 2000 行チェックで {} 件")
    return RowSize(
        over_1000=None,
        over_2000=None,
        basis="管理表も 2000 行チェックも判断材料が無い",
    )


def _size_by_rows(rows: int, basis: str) -> RowSize:
    """行数から 1000 件超 / 1000 件以下を決める。"""
    over_1000 = rows > BULK_MIN_ROWS
    return RowSize(over_1000=over_1000, over_2000=False, basis=basis.format(rows))
