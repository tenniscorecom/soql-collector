"""ID ごとに describe を取って、 結果を ``ReportRecord`` （メモリ上の記録）
として返す。

流れ （IDごと） :

1. 管理番号 → 管理表の行 （無ければ呼び出し側の ``cli`` でエラー）
2. URL から ``site_for(url)`` で接続先を決め、 ``with site() as client:`` で開く
3. ``metadata = client.report.describe(report_id)`` （レポートの describe）
4. 主オブジェクトの決定: ``reportType.type`` が空なら特定できず。 ``$`` / ``@``
   を含まなければその値、 含めば ``mapping.report_type_candidates()`` の候補を
   ``fetch_object_cached()`` で順に ``describe_object`` して、 最初に成功した
   ものを主オブジェクトにする。 HTTP エラー （``SalesforceRequestError``） や
   ``ValueError`` で主オブジェクトが特定できなかった／ describe が取れなかった
   ときは、 列対応表を全列 ``(不明)`` ＋理由の備考に縮退する。 401 / 403 は
   握りつぶさず、 その ID の失敗にする
5. 関連オブジェクト （参照先を ``RELATED_DEPTH`` 段まで） : 主オブジェクトの
   describe の ``fields`` を起点に幅優先で参照を辿り、 ``relationshipName``
   が空でなく ``referenceTo`` が空でない項目を **1 段ずつ** 掘る。 同じ名前は
   浅い段から先に登録される。 件数は ``RELATED_MAX`` （既定 40） で全段の
   合計を打ち切り、 超えた名前は ``warnings`` に残す
6. ``mapping.build_column_map(metadata, main_describe, reason, ...)`` で列対応表
   を組み立てる。 主オブジェクトの describe は **ID ごとに 1 回** だけ取得する
   （``fetch_object_cached`` のキャッシュを経由するため、 複数 ID が同じ
   主オブジェクトを参照しても HTTP は 1 回）

**出力は CSV だけ**。 ``ReportRecord`` （frozen dataclass） に詰めて
``FetchOutcome.record`` で返す。 ``run_tables`` がこの record を受け取って
CSV を組み立てる。 許可リストでレポート describe から落としたキー名は
``_fetch_one`` の ``warnings`` に
「レポート定義の中で使わなかったキー: ...」 の形でも 1 行入れる
（SOQL の組み立てはしない方針。 落としたキー名は引き続き
可視化のための参考情報として残す）。

ファイルの構成:

- ``flow.py``: 全体の流れ（``run_fetch`` と 1 ID 分の ``_fetch_one``）
- ``site_run.py``: 接続先に入ったあとの取得・列対応表の組み立て
- ``outcome.py``: 終端処理（PII・警告・SOQL 下書き・``FetchOutcome``）
- ``main_object.py``: 主オブジェクトの決定
- ``related.py``: 関連オブジェクトの掘り下げ
- ``object_fetch.py``: オブジェクト describe の取得（ストア → API）
- ``report_slim.py``: レポート・オブジェクト describe の整形
- ``row_check.py``: 2000 行チェック
- ``models.py``: データクラス（``ReportRecord`` など）
"""

from src.fetch.flow import (
    RELATED_DEPTH,
    RELATED_MAX,
    ROW_LIMIT_ENABLED,
    SURVEY_ON_SANDBOX,
    default_site_for,
    run_fetch,
)
from src.fetch.models import (
    FetchOutcome,
    RelatedNode,
    RelationRecord,
    ReportRecord,
    RowCheck,
    SiteFor,
)
from src.fetch.object_fetch import FATAL_STATUS_CODES

__all__ = [
    "FATAL_STATUS_CODES",
    "RELATED_DEPTH",
    "RELATED_MAX",
    "ROW_LIMIT_ENABLED",
    "SURVEY_ON_SANDBOX",
    "FetchOutcome",
    "RelatedNode",
    "RelationRecord",
    "ReportRecord",
    "RowCheck",
    "SiteFor",
    "default_site_for",
    "run_fetch",
]
