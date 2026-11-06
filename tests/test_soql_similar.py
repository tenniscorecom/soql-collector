"""``soql_similar`` （生成した SOQL 同士の 「似た SOQL」 判定） のテスト。"""

from __future__ import annotations

from src import soql_similar
from src.fetch import ReportRecord
from src.soql_builder import SoqlDraft


def _make_record(
    key: str,
    *,
    main_object: str = "Opportunity",
    select_items: tuple[str, ...] = ("Name",),
    where_items: tuple[str, ...] = (),
    order_by: str = "",
    status: str = "作成済み",
    with_draft: bool = True,
) -> ReportRecord:
    """SOQL の下書きを持つ ``ReportRecord`` を作る。"""
    draft = None
    if with_draft:
        draft = SoqlDraft(
            soql="SELECT ...",
            status=status,
            missing=(),
            checks=(),
            select_items=select_items,
            where_items=where_items,
            order_by=order_by,
        )
    return ReportRecord(
        key=key,
        summary=f"テスト{key}",
        report_id=f"00O{key:0>12}",
        url=f"https://example/r/Report/00O{key:0>12}/view",
        main_object=main_object,
        report={"reportMetadata": {}, "reportExtendedMetadata": {}},
        objects={},
        relations=(),
        column_map=[],
        warnings=(),
        soql_draft=draft,
    )


# ── SOQL同一 ───────────────────────────────────────────────────────


def test_identical_soql_is_reported_as_identical() -> None:
    """SELECT・WHERE・ORDER BY が全部同じなら 「SOQL同一」。"""
    rec_a = _make_record(
        "1001",
        select_items=("Name", "Amount"),
        where_items=("Stage = 'Closed'",),
        order_by="ORDER BY Name ASC",
    )
    rec_b = _make_record(
        "1002",
        select_items=("Name", "Amount"),
        where_items=("Stage = 'Closed'",),
        order_by="ORDER BY Name ASC",
    )

    result = soql_similar.find_similar_soql([rec_a, rec_b])

    assert list(result) == ["1001", "1002"]
    entry = result["1001"][0]
    assert entry.key == "1002"
    assert entry.category == soql_similar.CATEGORY_IDENTICAL
    # 同一に違いないので 違いは無い
    assert entry.diff == ""


# ── WHERE以降が同じ ───────────────────────────────────────────────


def test_same_where_different_select_is_where_after_same() -> None:
    """WHERE と ORDER BY が同じで SELECT が違うなら 「WHERE以降が同じ」。"""
    rec_a = _make_record("1001", select_items=("Name",), where_items=("Stage = 'Closed'",))
    rec_b = _make_record("1002", select_items=("Name", "Amount"), where_items=("Stage = 'Closed'",))

    result = soql_similar.find_similar_soql([rec_a, rec_b])

    assert result["1001"][0].category == soql_similar.CATEGORY_SAME_WHERE
    # 1001 から見た違い: 1002 にある Amount は 「追加」
    assert result["1001"][0].diff == "SELECT 追加: Amount"
    # 1002 から見た違い: 1001 に無い Amount は 「削除」
    assert result["1002"][0].diff == "SELECT 削除: Amount"


def test_select_diff_shows_added_and_removed() -> None:
    """SELECT の追加と削除の両方があるときは 「追加: … / 削除: …」 の形。"""
    rec_a = _make_record("1001", select_items=("Name", "Stage"))
    rec_b = _make_record("1002", select_items=("Name", "Amount"))

    result = soql_similar.find_similar_soql([rec_a, rec_b])

    assert result["1001"][0].diff == "SELECT 追加: Amount / 削除: Stage"
    assert result["1002"][0].diff == "SELECT 追加: Stage / 削除: Amount"


# ── WHEREだけ違う ─────────────────────────────────────────────────


def test_same_select_different_where_is_where_only_diff() -> None:
    """SELECT と ORDER BY が同じで WHERE が違うなら 「WHEREだけ違う」。"""
    rec_a = _make_record("1001", select_items=("Name",), where_items=("Stage = 'Closed'",))
    rec_b = _make_record("1002", select_items=("Name",), where_items=("Stage = 'Open'",))

    result = soql_similar.find_similar_soql([rec_a, rec_b])

    assert result["1001"][0].category == soql_similar.CATEGORY_SAME_SELECT
    assert result["1001"][0].diff == "WHERE 追加: Stage = 'Open' / 削除: Stage = 'Closed'"
    assert result["1002"][0].diff == "WHERE 追加: Stage = 'Closed' / 削除: Stage = 'Open'"


def test_where_diff_flattens_newlines() -> None:
    """条件の中の改行は空白 1 つに潰して差分の文にする。"""
    rec_a = _make_record("1001", where_items=("Stage = 'Closed'\nAND Amount > 100",))
    rec_b = _make_record("1002", where_items=("Stage = 'Open'",))

    result = soql_similar.find_similar_soql([rec_a, rec_b])

    assert result["1001"][0].diff == (
        "WHERE 追加: Stage = 'Open' / 削除: Stage = 'Closed' AND Amount > 100"
    )


# ── 比較しないもの ─────────────────────────────────────────────────


def test_different_main_object_is_not_compared() -> None:
    """FROM （主オブジェクト） が違うものは比較しない。"""
    rec_a = _make_record("1001", main_object="Opportunity")
    rec_b = _make_record("1002", main_object="Account")

    result = soql_similar.find_similar_soql([rec_a, rec_b])

    assert result == {}


def test_none_status_is_excluded() -> None:
    """``作れず`` の SOQL は比較対象にしない。"""
    rec_a = _make_record("1001", status="作れず")
    rec_b = _make_record("1002")

    result = soql_similar.find_similar_soql([rec_a, rec_b])

    assert result == {}


def test_partial_status_is_excluded_from_both_sides() -> None:
    """``一部作れず`` の SOQL は 相手にも自分にも出ない。

    条件が一部省かれている（ ``missing`` がある）ので、 似て見えても当てに
    ならない。 比較対象は ``作成済み`` と ``要確認`` だけ。
    """
    rec_partial = _make_record("1001", status="一部作れず", select_items=("Name",))
    rec_created = _make_record("1002", select_items=("Name",))
    rec_needs_check = _make_record("1003", status="要確認", select_items=("Name",))

    result = soql_similar.find_similar_soql([rec_partial, rec_created, rec_needs_check])

    # 一部作れず は 自分の相手にも出ない
    assert "1001" not in result
    # かつ 谁的の相手にも出ない
    assert all(entry.key != "1001" for entries in result.values() for entry in entries)
    # 作成済み と 要確認 同士は 比べられる
    assert result["1002"][0].key == "1003"
    assert result["1003"][0].key == "1002"


def test_missing_draft_is_excluded() -> None:
    """``soql_draft`` が無い record は比較対象にしない。"""
    rec_a = _make_record("1001", with_draft=False)
    rec_b = _make_record("1002", with_draft=False)

    result = soql_similar.find_similar_soql([rec_a, rec_b])

    assert result == {}


def test_different_order_by_is_not_compared() -> None:
    """SELECT と WHERE が同じでも ORDER BY が違えば 「似た SOQL」 ではない。

    ORDER BY の比較を実装から抜くと、 この 2 件が 「SOQL同一」 になってしまう
    """
    rec_a = _make_record(
        "1001",
        select_items=("Name",),
        where_items=("Stage = 'Closed'",),
        order_by="ORDER BY Name ASC",
    )
    rec_b = _make_record(
        "1002",
        select_items=("Name",),
        where_items=("Stage = 'Closed'",),
        order_by="ORDER BY Amount DESC",
    )

    result = soql_similar.find_similar_soql([rec_a, rec_b])

    assert result == {}


# ── 集合としての比較 ───────────────────────────────────────────────


def test_select_order_does_not_matter() -> None:
    """SELECT の並び順が違っても同じ扱い。"""
    rec_a = _make_record("1001", select_items=("Name", "Amount"))
    rec_b = _make_record("1002", select_items=("Amount", "Name"))

    result = soql_similar.find_similar_soql([rec_a, rec_b])

    assert result["1001"][0].category == soql_similar.CATEGORY_IDENTICAL


def test_where_order_does_not_matter() -> None:
    """WHERE の並び順が違っても同じ扱い。"""
    rec_a = _make_record("1001", where_items=("Stage = 'Closed'", "Amount > 100"))
    rec_b = _make_record("1002", where_items=("Amount > 100", "Stage = 'Closed'"))

    result = soql_similar.find_similar_soql([rec_a, rec_b])

    assert result["1001"][0].category == soql_similar.CATEGORY_IDENTICAL


# ── 両方向に入る ───────────────────────────────────────────────────


def test_both_directions_are_filled() -> None:
    """A の相手に B、 B の相手に A が入る。"""
    rec_a = _make_record("1001", select_items=("Name",))
    rec_b = _make_record("1002", select_items=("Name", "Amount"))

    result = soql_similar.find_similar_soql([rec_a, rec_b])

    assert result["1001"][0].key == "1002"
    assert result["1002"][0].key == "1001"


def test_partners_are_sorted_by_key() -> None:
    """相手は管理番号の昇順。"""
    records = [
        _make_record("1003", select_items=("Name",)),
        _make_record("1001", select_items=("Name",)),
        _make_record("1002", select_items=("Name",)),
    ]

    result = soql_similar.find_similar_soql(records)

    assert [entry.key for entry in result["1001"]] == ["1002", "1003"]
    assert [entry.key for entry in result["1002"]] == ["1001", "1003"]
    assert [entry.key for entry in result["1003"]] == ["1001", "1002"]


def test_records_without_partner_are_not_in_result() -> None:
    """相手が居ない管理番号は辞書に現れない。"""
    rec_a = _make_record("1001", select_items=("Name",))
    rec_b = _make_record("1002", select_items=("Name",))
    rec_c = _make_record("1003", select_items=("Name", "Amount"), where_items=("A = 1",))

    result = soql_similar.find_similar_soql([rec_a, rec_b, rec_c])

    assert "1003" not in result


# ── 表示の整形 ─────────────────────────────────────────────────────


def test_format_cell_text_joins_with_semicolon() -> None:
    """一覧のセルは 「; 」 区切りで並べる。"""
    entries = [
        soql_similar.SimilarSoql(key="1002", category=soql_similar.CATEGORY_IDENTICAL, diff=""),
        soql_similar.SimilarSoql(
            key="1003", category=soql_similar.CATEGORY_SAME_WHERE, diff="SELECT 追加: Amount"
        ),
    ]

    text = soql_similar.format_cell_text(entries)

    assert text == "1002: SOQL同一; 1003: WHERE以降が同じ（SELECT 追加: Amount）"


def test_format_cell_text_truncates_long_text() -> None:
    """200 文字を超えたら 197 文字＋「…」 に切る。"""
    entries = [
        soql_similar.SimilarSoql(
            key="1002",
            category=soql_similar.CATEGORY_SAME_WHERE,
            diff="SELECT 追加: " + "A" * 300,
        )
    ]

    text = soql_similar.format_cell_text(entries)

    assert len(text) == 198
    assert text.endswith("…")
    assert text[:197] == soql_similar.format_cell_text(entries)[:197]
