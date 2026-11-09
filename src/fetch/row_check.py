"""2000 行チェック（``client.report.get()`` で行数が上限を超えるかを調べる）。"""

from __future__ import annotations

import logging
from typing import Any

from comken.exceptions import (
    SalesforceError,
    SalesforceReportAccessDeniedError,
    SalesforceReportFormatError,
    SalesforceReportTruncatedError,
)

from src.fetch.models import RowCheck
from src.fetch.offline import OfflineApiError
from src.row_check_status import ROW_CHECK_OVER, ROW_CHECK_UNDER, ROW_CHECK_UNKNOWN

logger = logging.getLogger(__name__)


# 理由欄に出す例外メッセージの最大長（行データや機微情報を漏らさない）
_ERROR_REASON_MAX_LENGTH = 200


# 結合レポートなど、 形式のせいで判定できないときの理由。 何度取っても同じ結果なので保存する
ROW_CHECK_FORMAT_REASON = (
    "この形式のレポートは取得できないため判定できません（結合レポート・明細の列が無いレポートなど）"
)


# レポート row check の状態定数は ``src.row_check_status`` に置いている
# （ ``src.soql_builder`` からも使うので、 循環 import を避けるため ）


def check_row_count(client: Any, report_id: str) -> RowCheck:
    """``client.report.get()`` を呼んで 2000 行チェックを行う。

    comken の ``ReportAPI.get()`` は TABULAR だけでなく SUMMARY / MATRIX の
    レポートも行を返すので、 describe の ``reportFormat`` に関わらず常に
    ``get`` を呼ぶ。

    - ``SalesforceReportTruncatedError`` → ``超えている`` （``rows=None``）
    - 正常終了 → ``超えていない`` （``rows=len(table)``）
    - 形式エラー （結合レポート・明細の列が無いレポートなど） → ``判定不可`` で
      「この形式のレポートは取得できないため判定できません（結合レポート・明細の列が無いレポートなど）」
    - 401 / 403 由来の ``SalesforceError`` は呼び出し元に再送出（レポート全体を
      失敗扱いにする）
    - その他の例外 → ``判定不可`` で 例外名＋短い説明
    """
    try:
        table = client.report.get(report_id)
    except OfflineApiError:
        # 解析の段では 2000 件チェックはしない。
        # 呼ばれても 「判定不可」 に縮退せず、 上位へ送出して失敗にする
        raise
    except SalesforceReportTruncatedError:
        # 2000 行超。 切り捨て検知は正常パスの一部なので reason は None。
        return RowCheck(status=ROW_CHECK_OVER, rows=None, reason=None)
    except SalesforceError as exc:
        if isinstance(exc, SalesforceReportAccessDeniedError):
            # 401 / 403: レポート全体を失敗扱いにするために再送出
            raise
        if isinstance(exc, SalesforceReportFormatError):
            return RowCheck(
                status=ROW_CHECK_UNKNOWN,
                rows=None,
                reason=ROW_CHECK_FORMAT_REASON,
            )
        return RowCheck(
            status=ROW_CHECK_UNKNOWN,
            rows=None,
            reason=_format_error_reason(exc),
        )
    except Exception as exc:
        return RowCheck(
            status=ROW_CHECK_UNKNOWN,
            rows=None,
            reason=_format_error_reason(exc),
        )
    return RowCheck(
        status=ROW_CHECK_UNDER,
        rows=len(table),
        reason=None,
    )


def can_save_row_check(row_check: RowCheck) -> bool:
    """件数の判定を保存してよいか。

    通信エラーなどの 「判定不可」 は API の失敗なので保存しない（次の取得でやり直す）。
    """
    return row_check.status != ROW_CHECK_UNKNOWN or row_check.reason == ROW_CHECK_FORMAT_REASON


def _format_error_reason(exc: BaseException) -> str:
    """例外を 1 行の理由文字列に整形する。 メッセージは上限で切る。"""
    type_name = type(exc).__name__
    message = str(exc).strip()
    if not message:
        return type_name
    if len(message) > _ERROR_REASON_MAX_LENGTH:
        message = message[:_ERROR_REASON_MAX_LENGTH] + "…"
    return f"{type_name}: {message}"


def log_row_check(row_check: RowCheck | None) -> None:
    """2000 件チェックの結果を 1 行で出す。 行数だけ、 値は出さない。"""
    if row_check is None:
        logger.info("  2000件チェック: 未実施")
        return
    rows_text = str(row_check.rows) if row_check.rows is not None else "-"
    logger.info("  2000件チェック: 判定=%s 行数=%s", row_check.status, rows_text)
