"""生成した SOQL 同士の 「似た SOQL」 判定（純粋関数）。

``src/similar.py`` はレポート定義（``reportMetadata``）同士を比べるもので、
一覧には 「グループN: X が基準」 としか出ない。 ここでは **生成した SOQL**
同士を比べ、 「WHERE 以降が一致（SELECT だけ違う）」 「WHERE だけ違う」 を
一目で分かるようにする。

主オブジェクト（``FROM``）が同じレポート同士の総当たりで判定する。 区分は
上から順に当て、 どれにも当てはまらないペアは出さない:

- ``SOQL同一``: SELECT（集合）・WHERE（集合）・ORDER BY が全部同じ
- ``WHERE以降が同じ``: WHERE（集合）と ORDER BY が同じで SELECT が違う
- ``WHEREだけ違う``: SELECT（集合）と ORDER BY が同じで WHERE が違う

比較するのは ``作成済み`` と ``要確認`` の SOQL だけ。 ``作れず`` と
``一部作れず`` は比べない（ ``一部作れず`` は条件が一部省かれているので、
似て見えても当てにならない ）。

比較は ``SoqlDraft`` に入れた ``select_items`` / ``where_items`` /
``order_by`` で行う（ SOQL の文字列を後からパースしない ）。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.soql_builder import STATUS_NONE, STATUS_PARTIAL, STATUS_SKIPPED

if TYPE_CHECKING:
    from src.fetch import ReportRecord
    from src.soql_builder import SoqlDraft


# 区分
CATEGORY_IDENTICAL = "SOQL同一"
CATEGORY_SAME_WHERE = "WHERE以降が同じ"
CATEGORY_SAME_SELECT = "WHEREだけ違う"


# 一覧シートの 「似たSOQL」 列の文字数上限。 超えたら 3 文字削って 「…」 に置く
CELL_MAX_LENGTH = 200


@dataclass(frozen=True)
class SimilarSoql:
    """似た SOQL の相手 1 件。

    Attributes:
        key: 相手の管理番号。
        category: 区分（ ``SOQL同一`` / ``WHERE以降が同じ`` /
            ``WHEREだけ違う`` ）。
        diff: 自分から見た相手との違い （ 1 文）。 同一なら空文字。
    """

    key: str
    category: str
    diff: str


def find_similar_soql(records: Iterable[ReportRecord]) -> dict[str, list[SimilarSoql]]:
    """``records`` から 「似た SOQL」 の関係を抽出する（純粋関数）。

    比較するのは ``作成済み`` と ``要確認`` の SOQL だけ。 ``soql_draft`` が
    無い record と、 ``作れず`` / ``一部作れず`` / ``対象外（1000件以下）`` の
    record は対象外にする （ ``一部作れず`` は 条件が一部省かれているので、
    似て見えても当てにならない ） 。

    戻り値は ``{管理番号: [相手の SimilarSoql, ...]}`` 。 相手は管理番号の
    昇順。 両方方向に登録する （ A の相手に B、 B の相手に A ） 。 違いの文は
    自分から見た向きで書く。
    """
    items: list[tuple[str, str, SoqlDraft]] = []
    for record in records:
        draft = record.soql_draft
        if draft is None or draft.status in (STATUS_NONE, STATUS_PARTIAL, STATUS_SKIPPED):
            continue
        # FROM で仕切る（ FROM を子に切り替えたレポートもあるため ）
        from_object = record.soql_from_object or record.main_object or ""
        items.append((record.key, from_object, draft))
    items.sort(key=lambda item: item[0])

    # 主オブジェクト（FROM）ごとにバケットに分ける
    by_object: dict[str, list[tuple[str, SoqlDraft]]] = {}
    for key, main_object, draft in items:
        by_object.setdefault(main_object, []).append((key, draft))

    found: dict[str, list[SimilarSoql]] = {}
    for bucket in by_object.values():
        for index_a in range(len(bucket)):
            for index_b in range(index_a + 1, len(bucket)):
                key_a, draft_a = bucket[index_a]
                key_b, draft_b = bucket[index_b]
                category, diff_a, diff_b = _classify(draft_a, draft_b)
                if category is None:
                    continue
                found.setdefault(key_a, []).append(
                    SimilarSoql(key=key_b, category=category, diff=diff_a)
                )
                found.setdefault(key_b, []).append(
                    SimilarSoql(key=key_a, category=category, diff=diff_b)
                )

    result: dict[str, list[SimilarSoql]] = {}
    for key in sorted(found):
        entries = sorted(found[key], key=lambda entry: entry.key)
        result[key] = entries
    return result


def format_entry_text(entry: SimilarSoql) -> str:
    """1 件を 「管理番号: 区分（違い）」 の文字列にする。

    違いが無い（ ``SOQL同一`` ） ときは 「管理番号: 区分」 だけにする。
    """
    if entry.diff:
        return f"{entry.key}: {entry.category}（{entry.diff}）"
    return f"{entry.key}: {entry.category}"


def format_cell_text(entries: Sequence[SimilarSoql]) -> str:
    """一覧シートの 「似たSOQL」 列の文字列にする。

    相手ごとに 「管理番号: 区分（違い）」 を 「; 」 区切りで並べる。
    ``CELL_MAX_LENGTH`` 文字を超えたら 3 文字削って 「…」 に置く。
    """
    text = "; ".join(format_entry_text(entry) for entry in entries)
    if len(text) > CELL_MAX_LENGTH:
        text = text[: CELL_MAX_LENGTH - 3] + "…"
    return text


def format_markdown_lines(entries: Sequence[SimilarSoql]) -> list[str]:
    """Markdown の 「似たSOQL」 の箇条書きの行にする。 1 相手 1 行。"""
    return [f"- {format_entry_text(entry)}" for entry in entries]


# ── 区分の判定 ──────────────────────────────────────────────────────


def _classify(draft_a: SoqlDraft, draft_b: SoqlDraft) -> tuple[str | None, str, str]:
    """2 つの SOQL を 区分 に振る。

    Returns:
        ``(区分, a から見た違い, b から見た違い)`` のタプル。 区分が無い
        ときは ``(None, "", "")`` 。
    """
    select_a = frozenset(draft_a.select_items)
    select_b = frozenset(draft_b.select_items)
    where_a = frozenset(draft_a.where_items)
    where_b = frozenset(draft_b.where_items)
    same_order = draft_a.order_by == draft_b.order_by

    if select_a == select_b and where_a == where_b and same_order:
        return CATEGORY_IDENTICAL, "", ""
    if where_a == where_b and same_order:
        return (
            CATEGORY_SAME_WHERE,
            _describe_select_diff(select_a, select_b),
            _describe_select_diff(select_b, select_a),
        )
    if select_a == select_b and same_order:
        return (
            CATEGORY_SAME_SELECT,
            _describe_where_diff(where_a, where_b),
            _describe_where_diff(where_b, where_a),
        )
    return None, "", ""


# ── 差分の文章化 ───────────────────────────────────────────────────


def _describe_select_diff(mine: frozenset[str], theirs: frozenset[str]) -> str:
    """SELECT の違いを 「SELECT 追加: A, B / 削除: C」 の形にする。"""
    return _describe_diff("SELECT", mine, theirs)


def _describe_where_diff(mine: frozenset[str], theirs: frozenset[str]) -> str:
    """WHERE の違いを 「WHERE 追加: 条件 / 削除: 条件」 の形にする。

    条件は ``where_items`` の 1 要素をそのまま使う（ 改行は空白 1 つに潰す ） 。
    """
    return _describe_diff("WHERE", mine, theirs, flatten=True)


def _describe_diff(
    label: str,
    mine: frozenset[str],
    theirs: frozenset[str],
    *,
    flatten: bool = False,
) -> str:
    """集合の違いを 1 文にする。 「追加」 があるときは 前に、 無いときは
    「削除」 だけを後ろに置く。
    """
    added = _sorted_items(theirs - mine, flatten=flatten)
    removed = _sorted_items(mine - theirs, flatten=flatten)
    if not added and not removed:
        return ""
    if not added:
        return f"{label} 削除: " + ", ".join(removed)
    text = f"{label} 追加: " + ", ".join(added)
    if removed:
        text += " / 削除: " + ", ".join(removed)
    return text


def _sorted_items(items: frozenset[str], *, flatten: bool) -> list[str]:
    """集合を 並んだ文字列のリスト にする。 改行を潰す指定があれば先に潰す。"""
    if flatten:
        return sorted(_flatten_condition(item) for item in items)
    return sorted(items)


def _flatten_condition(condition: str) -> str:
    """条件の中の改行を空白 1 つに潰す。"""
    return condition.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
