"""取得結果を入れるデータクラスと、 接続先判定の型。

fetch パッケージの土台で、 他のファイルから読まれるだけ。
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any

from src import pii
from src.mapping.child_subquery import ChildSubquery
from src.master import MasterEntry


@dataclass(frozen=True)
class RelatedNode:
    """幅優先で掘った関連オブジェクト 1 件。

    ``describe`` は ``fetcher`` で埋めた状態（``describe_object`` が失敗した
    ときは ``None``）。 ``depth`` は主オブジェクトから数えた段数（主 = 0、
    1 段目から ``RELATED_DEPTH`` まで）。
    """

    name: str
    depth: int
    describe: dict[str, Any] | None


@dataclass(frozen=True)
class RelationRecord:
    """主＋関連の **取れた** オブジェクト同士で張られた親→子の参照 1 本。

    ``tables: 関連表.csv`` の 1 行に対応する。
    ポリモーフィックな参照は ``referenceTo`` のエントリごとに 1 本ずつ記録する。
    """

    parent: str  # 親オブジェクト名
    field: str  # 参照項目 API 名
    relationship_name: str | None  # リレーション名（``fields[].relationshipName``）
    child: str  # 子オブジェクト名
    depth: int  # 子の深さ（主 = 0）


@dataclass(frozen=True)
class RowCheck:
    """2000 行チェックの結果。 1 レポート 1 件。

    ``status`` は次のいずれか:

    - ``超えている`` （SalesforceReportTruncatedError）
    - ``超えていない`` （取得でき、 ``rows`` に行数）
    - ``判定不可`` （``reason`` に理由）
    - ``未実施`` （``ROW_LIMIT_ENABLED`` が ``False`` のとき）

    行データ（値そのもの） は **保持しない** 。 メモリ上でも行数だけ数え、
    CSV / ログ / 警告 / 例外メッセージに絶対に出さない。
    """

    status: str
    rows: int | None
    reason: str | None


@dataclass(frozen=True)
class SourceTimes:
    """資料に出す、 元データの取得日時。 分からないものは空文字。"""

    report: str
    main_object: str
    row_count: str


@dataclass(frozen=True)
class ReportRecord:
    """1 ID の取得結果をメモリに保持する記録。 ``frozen`` で再代入しない。

    ``fetch`` が ``run`` に渡して、 ``run`` が ``run_tables`` に渡す。
    ``tables`` はこの record から CSV を組み立てる。 JSON ファイルは作らない。
    """

    key: str  # 管理番号
    summary: str  # 概要
    report_id: str  # レポート ID
    url: str  # レポート URL
    main_object: str | None  # 主オブジェクト名（取得失敗なら ``None``）
    #: レポート describe を ``make_slim_report`` で絞った dict。 CSV 生成や
    #: 警告表 で読む。 落としたキー名はここに含めず ``collect_dropped_keys``
    #: で別途取り出す。
    report: dict[str, Any]
    #: 主＋関連のオブジェクト describe を ``make_slim_object`` で絞った dict。
    #: キー = オブジェクト名、 値 = ``{name, label, custom, fields}``
    objects: dict[str, dict[str, Any]]
    relations: tuple[RelationRecord, ...]  # 主＋関連オブジェクト同士の参照
    column_map: list[dict[str, str]]  # 列対応表
    warnings: tuple[str, ...]  # 警告メッセージ
    #: 2000 行チェックの結果。 ``None`` なら 未実施。 失敗した ID は record ごと無い
    row_check: RowCheck | None = None
    #: レポート単位の PII 判定結果。 調査表と 対応表の 個人情報 列で使う
    pii: pii.ReportPIIResult = dc_field(
        default_factory=lambda: pii.ReportPIIResult(status="なし", matching_columns=())
    )
    #: SOQL の下書き。 ``tables`` が組み立てて持たせ、 次段 （AI に渡す
    #: Markdown / xlsx） で読む。 ``None`` のときは下書き未組み立て
    #: （主オブジェクト不明など、 組み立てそのものが走らなかったケース）
    soql_draft: Any = dc_field(
        default=None,
    )
    #: クロスフィルタの副問い合わせに使う子オブジェクトの describe。
    #: キー = 子オブジェクト名、 値 = ``{name, label, custom, fields,
    #: childRelationships}`` の slim な dict。 ``objects`` には **入れない**
    #: （関係図・関連表・対応表の出力を変えないため） 。
    cross_filter_objects: dict[str, dict[str, Any]] = dc_field(default_factory=dict)
    #: SOQL の FROM に使うオブジェクト名。 レポートの列が子オブジェクトの項目の
    #: ときは子オブジェクト名が入る。 ``None`` のときは主オブジェクト。
    #: ``main_object`` はレポートの親のまま変えない（資料の「主オブジェクト」）。
    soql_from_object: str | None = None
    #: 子から取る形にしたときの、 子から親へ戻るリレーション名（例 ``Account`` ）。
    soql_from_relationship: str = ""
    #: SOQL の SELECT に入れる、 子リレーションの副問い合わせ。
    #: ``None`` のときは副問い合わせにしない（ 子から取る形、または 子の列が無い ）。
    child_subquery: ChildSubquery | None = None
    #: 子の副問い合わせにしたときの要確認。 SOQL の下書きの ``checks`` に入る
    child_subquery_checks: tuple[str, ...] = ()
    #: 接続先の組織。 AI チャット用の項目定義を保存データから引くときに使う
    org: str = ""
    #: 元データの取得日時。 ``None`` のときは資料に出さない
    source_times: SourceTimes | None = None
    #: 本番の件数が 2000 件を超えるか。 管理表の「2000件超」列の値。
    #: ``None`` は「不明」（ 管理表に列が無い・セルが空 ）
    exceeds_row_limit: bool | None = None
    #: 管理表の「集計形式」列の値。 ``サマリー`` / ``マトリックス`` / ``無し`` の
    #: どれか。 ``None`` は「記入が無い」（ 管理表に列が無い・セルが空 ）
    #: 集計かどうかの判断は ``reportFormat`` で行い、 この値は レポートの形式との
    #: 照合と表示にだけ使う
    report_format: str | None = None
    #: そのレポートを RPA でも使っているか。 管理表の「RPA使用」列の値。
    #: ``None`` は「記入が無い」（ 管理表に列が無い・セルが空 ）
    uses_rpa: bool | None = None


@dataclass(frozen=True)
class FetchOutcome:
    """1 ID の取得結果。"""

    entry: MasterEntry
    status: str  # "ok" / "failed" / "dry-run"
    record: ReportRecord | None  # status="ok" のときだけ入る
    warnings: tuple[str, ...]
    error: str | None


# 接続先判定の型。 テストでは差し替える。
# ``with site_for(url)() as client:`` の形で使う。
SiteFor = Callable[[str], Callable[[], AbstractContextManager[Any]]]
