"""解析の段（保存データだけで出力を作る） 用 のオフライン側の部品。

Salesforce に接続しない代わりに、 「API が必要になった」 ことを
``OfflineApiError`` で知らせる。 レポート定義が無いときは 握りつぶさず、
レポート 1 件を失敗扱いにして 終了コード 1 にする。 途中まで作った
不完全な record は残さない。

ただし **オブジェクトの項目定義** が無いだけは、 レポート全体を失敗に
しない。 取得の段で describe が 404 などになったときと同じ扱い
（ 警告を 1 つ残して、 そのオブジェクトは不明のまま ） にする。
describe の成功したものは必ず保存されているので、 保存データに無い
＝ 取得の段でも取れなかったもの、 とみなせるため。

- ``OfflineApiError``: レポート定義が無いなど、 API が必要になったときの例外
- ``OfflineDescribeMissing``: オブジェクトの項目定義が保存データに無いときの例外。
  取得の段の describe 失敗と同じ経路で処理する
- ``OfflineClient``: API を呼ぶと ``OfflineApiError`` /
  ``OfflineDescribeMissing`` を出すクライアント
- ``OfflineSite`` / ``offline_site_for()``: 接続しない ``site_for``
"""

from __future__ import annotations

from typing import Any

from comken.exceptions import SalesforceError

from src.fetch.models import SiteFor

# 足りないもの（= API を呼ばないと取れないもの）の説明
MISSING_REPORT_DEFINITION = "レポートの元データ"
MISSING_ROW_COUNT = "レポートの行データ（2000件チェック）"


class OfflineApiError(SalesforceError):
    """保存データに無いので、 API を呼ぶ必要になったときの例外。

    解析の段（保存データだけで出力を作る） のときだけ送出される。 取得の段では
    出ない。 握りつぶさず、 レポート 1 件を失敗扱いにする。

    Attributes:
        missing: 足りなかったもの（ レポート定義 等）。
    """

    def __init__(self, missing: str) -> None:
        super().__init__(
            f"保存データに無いので 取得.bat で取ってください（足りないもの: {missing}）"
        )
        self.missing = missing


class OfflineDescribeMissing(SalesforceError):
    """オブジェクトの項目定義が保存データに無いときの例外。

    取得の段で ``describe_object`` が 404 などになったときと同じ扱いにする
    （ ``object_fetch`` 側で ``record_api_failure`` して警告を 1 つ残し、
    ``None`` を返す ）。 レポート全体の失敗にはしない。

    Attributes:
        name: 項目定義が無かったオブジェクト名。
    """

    def __init__(self, name: str) -> None:
        super().__init__(
            f"オブジェクト {name} は保存データに無い（取得.bat で取れることがあります）"
        )
        self.name = name


class _OfflineReportAPI:
    """``client.report`` の代わり。 呼ぶと ``OfflineApiError`` を出す。"""

    def describe(self, report_id: str) -> dict[str, Any]:
        raise OfflineApiError(f"{MISSING_REPORT_DEFINITION}（レポートID: {report_id}）")

    def get(self, report_id: str, *args: Any, **kwargs: Any) -> Any:
        raise OfflineApiError(MISSING_ROW_COUNT)


class OfflineClient:
    """API を呼ばない Salesforce クライアントの代わり。

    ``report.describe`` を呼ぶと ``OfflineApiError`` を出す（ レポート定義が
    保存データに無いとき。 握りつぶさず、そのレポートを失敗にする ）。
    ``describe_object`` を呼ぶと ``OfflineDescribeMissing`` を出す（ 項目定義が
    保存データに無いとき。 取得の段の describe 失敗と同じ扱いにする ）。
    """

    def __init__(self) -> None:
        self.report = _OfflineReportAPI()

    def describe_object(self, name: str) -> dict[str, Any]:
        raise OfflineDescribeMissing(name)


class OfflineSite:
    """``with site_for(url)() as client:`` の形を持つ、 接続しないサイト。

    ``DOMAIN_URL`` は本来のサイトクラスのもの。 組織（ describe の保存のキー ）
    を URL からではなくこの値から決めるために持つ。 取得の段と同じコードで
    同じ組織になるようにする。
    """

    def __init__(self, domain_url: str) -> None:
        self.DOMAIN_URL = domain_url

    def __call__(self) -> OfflineSite:
        return self

    def __enter__(self) -> OfflineClient:
        return OfflineClient()

    def __exit__(self, *args: object) -> None:
        return None


def offline_site_for(base_site_for: SiteFor) -> SiteFor:
    """解析の段 用 の ``site_for`` を返す。 接続も 認証もしない。

    ``base_site_for`` は URL → サイトクラス の関数 （ 取得の段と同じもの ）。
    呼び出して クラス属性の ``DOMAIN_URL`` だけを読む。 **クラスは作らない。**
    作ると ログインしてしまう。

    Args:
        base_site_for: 通常の実行で使う ``site_for`` （ URL → サイトクラス ）。

    Returns:
        ``site_for`` 互換の関数。 返すのは ``OfflineSite`` （ 組織が分かる
        ``DOMAIN_URL`` を持つ ）。
    """

    def site_for(url: str) -> OfflineSite:
        base_site_class = base_site_for(url)
        return OfflineSite(getattr(base_site_class, "DOMAIN_URL", ""))

    return site_for
