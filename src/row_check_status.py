"""``RowCheck`` （ 2000 行チェック ） の状態定数。

``src.fetch.row_check`` と ``src.soql_builder`` の両方から使うので、 循環
import が無い中立な場所に置いている。 ``src.fetch`` 側は
``src.soql_builder`` を import するので、 定数を ``src.fetch`` 側に置くと
import が循環する。
"""

from __future__ import annotations

# レポート row check の状態定数
# 2000 行超 （ SalesforceReportTruncatedError ）
ROW_CHECK_OVER = "超えている"

# 取得できた。 ``rows`` に行数が入る
ROW_CHECK_UNDER = "超えていない"

# 判定できない （ 例外や形式エラー ）
ROW_CHECK_UNKNOWN = "判定不可"
