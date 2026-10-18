"""主オブジェクトの決定（レポートタイプ → 列キーの先頭の 2 段）。"""

from __future__ import annotations

from typing import Any

from src import mapping
from src.describe_store import DescribeStore
from src.fetch.object_fetch import fetch_object_cached
from src.fetch.report_slim import SlimReport

# ``reportType.type`` から主オブジェクトが取れなかったときの代替候補として、
# 列キーの先頭を最大何個試すか。 多すぎても 1 度目で見つかることが多いので、
# この値で十分
_MAIN_OBJECT_PREFIX_LIMIT: int = 5


def choose_main(
    client: Any,
    org: str,
    slim_report: SlimReport,
    store: DescribeStore,
    warnings: list[str],
) -> tuple[str | None, dict[str, Any] | None, str | None, set[tuple[str, str]]]:
    """``metadata`` から主オブジェクトを決める （2 段: レポートタイプ → 列キー先頭）。

    ``reportType.type`` から候補が取れなかったとき （カスタムレポート
    タイプのレポートタイプ自体が主オブジェクトの API 名でないケース） に
    備え、 **列キーの先頭** を代替の候補として試す。 次の順で
    ``fetch_object_cached()`` を呼んで、 最初に describe が取れた候補を
    採用する:

    (a) ``mapping.report_type_candidates(reportType.type)`` の候補
    (b) (a) が全滅したとき、 列キーの先頭 （``.`` を含む列キーの最初の
        ``.`` より前） を多い順、 同数は最初に出た順で最大
        ``_MAIN_OBJECT_PREFIX_LIMIT`` 個

    (a) で決まれば従来通り、 (b) で決まれば警告を出さずに正常な経路として
    扱う。 どちらもだめなら ``mapping.candidate_failure_reason()`` の形
    で両方のリストを並べた理由を返す。

    ``fetch_object_cached()`` の 401 / 403 はそのまま上位へ
    ``SalesforceRequestError`` が送出され、 ID 全体が失敗扱いになる（呼び出し
    側で ``try`` している前提）。

    (a) の ``describe_object`` が 404 / ValueError などで失敗したときの
    WARNING は、 (b) が **成功する** なら出さない (正常な経路として扱う) 。
    (a) (b) ともに全滅したときは (a) の WARNING も積む。

    Returns:
        ``(main_object_name | None, main_describe | None, reason | None,
        csv_used_keys)``
        - ``main_object_name``: 採用した主オブジェクト名。 describe が取れ
          なかったときは ``None``
        - ``main_describe``: 主オブジェクトの describe dict。 特定・取得に失敗
          したときは ``None``
        - ``reason``: ``main_describe`` が ``None`` のときの理由（``None``
          なら取得成功）
        - ``csv_used_keys``: このレポートで参照されたオブジェクトのうち、
          **CSV から読んだもの** （今回 API で取ったものではない） の
          ``(org, object_lower)`` の集合。 取り直し判定用
    """
    object_name = (slim_report["reportMetadata"].get("reportType") or {}).get("type", "")

    csv_used_keys: set[tuple[str, str]] = set()

    # (a) ``reportType.type`` から候補。 失敗 WARNING は一時バッファに
    # 溜め、 (b) が成功したら捨てる (正常な経路扱い) 。
    candidates_a = mapping.report_type_candidates(object_name)
    a_warnings: list[str] = []
    for candidate in candidates_a:
        describe = fetch_object_cached(client, org, candidate, store, a_warnings, csv_used_keys)
        if describe is not None:
            return candidate, describe, None, csv_used_keys

    # (b) 列キーの先頭から候補。 こちらは「正常な経路」 なので WARNING を
    # 直接 ``warnings`` に積む。 ただし (b) で出た WARNING は (a) が全滅
    # してもそのまま残す。
    candidates_b = _extract_column_key_prefix_candidates(slim_report)
    b_warnings: list[str] = []
    for candidate in candidates_b:
        describe = fetch_object_cached(client, org, candidate, store, b_warnings, csv_used_keys)
        if describe is not None:
            warnings.extend(b_warnings)
            return candidate, describe, None, csv_used_keys

    # 両方だめ: (a) の WARNING も積んでから理由を組み立てる
    warnings.extend(a_warnings)
    warnings.extend(b_warnings)
    reason = _main_object_failure_reason(object_name, candidates_a, candidates_b)
    return None, None, reason, csv_used_keys


def _extract_column_key_prefix_candidates(
    slim_report: SlimReport,
    *,
    limit: int = _MAIN_OBJECT_PREFIX_LIMIT,
) -> list[str]:
    """``metadata`` の列キーのうち ``.`` を含むものについて最初の ``.`` よ
    り前を集計し、 多い順（同数は最初に出た順） で ``limit`` 個返す。

    列キーは ``mapping.collect_describable_columns`` が集める全ソース
    （``detailColumns`` / ``reportFilters`` / ``groupingsDown`` /
    ``groupingsAcross`` / ``aggregates`` / ``standardDateFilter`` /
    ``sortBy``） を使う。 ``.`` を含まない列キーは対象外（主オブジェクトと
    同じ名前 1 つのキーや、 ``OPPORTUNITY_NAME`` のような値）。

    ``FK_`` で始まる列キーの先頭は主オブジェクトではなく主からの参照先を
    指しているので、 候補からは外す。

    ``choose_main`` の (b) 段階で主オブジェクトの候補を組み立てるのに使う。
    """
    columns = mapping.collect_describable_columns(slim_report["reportMetadata"])
    counts: dict[str, int] = {}
    first_seen: dict[str, int] = {}
    for index, column_key in enumerate(columns):
        if "." not in column_key:
            continue
        if column_key.startswith("FK_"):
            # ``FK_`` の先は主オブジェクトではなく主からの参照先
            continue
        prefix = column_key.split(".", 1)[0].strip()
        if not prefix:
            continue
        counts[prefix] = counts.get(prefix, 0) + 1
        if prefix not in first_seen:
            first_seen[prefix] = index
    # 多い順、 同数は最初に出た順
    sorted_prefixes = sorted(
        counts.keys(),
        key=lambda p: (-counts[p], first_seen[p]),
    )
    return sorted_prefixes[:limit]


def _main_object_failure_reason(
    original: str,
    candidates_a: list[str],
    candidates_b: list[str],
) -> str:
    """``choose_main`` の (a) / (b) が両方全滅したときの理由を組み立てる。

    既存 ``mapping.candidate_failure_reason`` のスタイルを保ちつつ、 両方
    のリストを 「``(a)`` / ``(b)``」 と分かる形で添える。 ``(a)`` も
    ``(b)`` も無い（``reportType.type`` が空で列キーにも ``.`` が無い）
    ときは、 「主オブジェクト候補を抽出できなかった」 の旨を返す。
    """
    if candidates_a and candidates_b:
        a_text = ", ".join(repr(c) for c in candidates_a)
        b_text = ", ".join(repr(c) for c in candidates_b)
        return (
            f"レポートタイプ {original!r} の主オブジェクト候補 {a_text} と"
            f" 列キー先頭の候補 {b_text} を順に確認しましたが、"
            "どれも Object Describe できず自動判定できません。手動で確認してください"
        )
    if candidates_a:
        return mapping.candidate_failure_reason(original, candidates_a)
    if candidates_b:
        b_text = ", ".join(repr(c) for c in candidates_b)
        return (
            f"レポートタイプ {original!r} の主オブジェクトは特定できず、"
            f" 列キー先頭の候補 {b_text} を順に確認しましたが、"
            "どれも Object Describe できず自動判定できません。手動で確認してください"
        )
    return (
        f"レポートタイプ {original!r} からは主オブジェクト候補を抽出できず、"
        "列キーにも主オブジェクトの手がかりが無いため自動判定できません。"
        "手動で確認してください"
    )
