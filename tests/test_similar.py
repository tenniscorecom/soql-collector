"""``similar`` 判定のテスト。"""

from __future__ import annotations

from src import similar
from src.fetch import ReportRecord


def _make_record(
    key: str,
    *,
    report_type: str = "Opportunity",
    format_: str = "TABULAR",
    detail_columns: tuple[str, ...] = (),
    groupings_down: tuple[str, ...] = (),
    groupings_across: tuple[str, ...] = (),
    aggregates: tuple[str, ...] = (),
    sort_by: tuple[tuple[str, str], ...] = (),
    filters: tuple[tuple[str, str, str], ...] = (),
    boolean_filter: str = "",
    standard_date=None,
    standard_filters=None,
    cross_filters=None,
    top_rows=None,
    scope: str = "",
    summary: str = "",
    report_id: str | None = None,
) -> ReportRecord:
    """テスト用の ``ReportRecord`` を作る（ slim 済み ``report`` を持つ ）。"""
    report_id_value = report_id or f"00O{key:0>12}"
    report_metadata: dict = {
        "reportType": {"type": report_type},
        "reportFormat": format_,
        "detailColumns": list(detail_columns),
        "reportFilters": [
            {"column": col, "operator": op, "value": val} for col, op, val in filters
        ],
    }
    if boolean_filter:
        report_metadata["reportBooleanFilter"] = boolean_filter
    if groupings_down:
        report_metadata["groupingsDown"] = [{"name": name} for name in groupings_down]
    if groupings_across:
        report_metadata["groupingsAcross"] = [{"name": name} for name in groupings_across]
    if aggregates:
        report_metadata["aggregates"] = list(aggregates)
    if sort_by:
        report_metadata["sortBy"] = [
            {"sortColumn": col, "sortOrder": order} for col, order in sort_by
        ]
    if standard_date is not None:
        report_metadata["standardDateFilter"] = standard_date
    if standard_filters is not None:
        report_metadata["standardFilters"] = standard_filters
    if cross_filters is not None:
        report_metadata["crossFilters"] = cross_filters
    if top_rows is not None:
        report_metadata["topRows"] = top_rows
    if scope:
        report_metadata["scope"] = scope
    return ReportRecord(
        key=key,
        summary=summary or f"テスト{key}",
        report_id=report_id_value,
        url=f"https://example/r/Report/{report_id_value}/view",
        main_object=report_type,
        report={"reportMetadata": report_metadata, "reportExtendedMetadata": {}},
        objects={},
        relations=(),
        column_map=[],
        warnings=(),
    )


# ── 同一 ─────────────────────────────────────────────────────────────


def test_identical_records_become_one_group() -> None:
    """構造と条件が同じ 2 件のレポートは 「 同一 」 で 1 つのグループ。"""
    rec_a = _make_record(
        "1001",
        detail_columns=("Opp.Name", "Opp.Amount"),
        filters=(("Opp.Stage", "equals", "Closed"),),
    )
    rec_b = _make_record(
        "1002",
        report_id="00O000000000002",
        detail_columns=("Opp.Name", "Opp.Amount"),
        filters=(("Opp.Stage", "equals", "Closed"),),
    )
    groups = similar.find_similar_groups([rec_a, rec_b], column_similarity_threshold=0.8)
    assert len(groups) == 1
    assert groups[0].base_key == "1001"
    assert groups[0].members == ("1001", "1002")
    pair = groups[0].pairs[0]
    assert pair.category == "同一"
    assert pair.integration == "1本にまとめられる"


# ── 条件のみ違い ─────────────────────────────────────────────────


def test_value_only_diff_yields_conditions_arg() -> None:
    """同じ列キー・同じ演算子で値だけ違う → 条件を引数にして 1 本にできそう。"""
    rec_a = _make_record(
        "1001",
        detail_columns=("Opp.Name",),
        filters=(("Opp.Amount", "greater", "100"),),
    )
    rec_b = _make_record(
        "1002",
        report_id="00O000000000002",
        detail_columns=("Opp.Name",),
        filters=(("Opp.Amount", "greater", "200"),),
    )
    groups = similar.find_similar_groups([rec_a, rec_b], column_similarity_threshold=0.8)
    assert len(groups) == 1
    pair = groups[0].pairs[0]
    assert pair.category == "条件のみ違い"
    assert pair.integration == "条件を引数にして1本にできそう"
    assert "値" in pair.diff_text


def test_different_column_key_yields_review() -> None:
    """絞り込みの列キーが違う → 要確認。"""
    rec_a = _make_record(
        "1001",
        detail_columns=("Opp.Name",),
        filters=(("Opp.Stage", "equals", "Closed"),),
    )
    rec_b = _make_record(
        "1002",
        report_id="00O000000000002",
        detail_columns=("Opp.Name",),
        filters=(("Opp.Amount", "greater", "100"),),
    )
    groups = similar.find_similar_groups([rec_a, rec_b], column_similarity_threshold=0.8)
    pair = groups[0].pairs[0]
    assert pair.category == "条件のみ違い"
    assert pair.integration == "要確認"


# ── 列のみ違い ─────────────────────────────────────────────────


def test_column_only_diff_includes_subset_note() -> None:
    """片方がもう片方を含むときは 「 含む 」 の注記。"""
    rec_a = _make_record(
        "1001",
        detail_columns=("Opp.Name", "Opp.Amount"),
    )
    rec_b = _make_record(
        "1002",
        report_id="00O000000000002",
        detail_columns=("Opp.Name",),
    )
    groups = similar.find_similar_groups([rec_a, rec_b], column_similarity_threshold=0.8)
    pair = groups[0].pairs[0]
    assert pair.category == "列のみ違い"
    # 基準 (1001) と対象 (1002) で追加 / 削除のいずれかが必ず出る
    assert ("追加" in pair.diff_text) or ("削除" in pair.diff_text)
    assert "含む" in pair.diff_text
    assert pair.integration == "要確認"


# ── 近い ──────────────────────────────────────────────────────────


def test_jaccard_above_threshold_marks_similar() -> None:
    """出力列の Jaccard が閾値以上なら 「 近い 」。 条件を少しズラして
    列のみ違い にしないこと。"""
    rec_a = _make_record(
        "1001",
        detail_columns=("A", "B", "C", "D"),
        filters=(("Opp.Stage", "equals", "Closed"),),
    )
    rec_b = _make_record(
        "1002",
        report_id="00O000000000002",
        detail_columns=("A", "B", "C", "E"),
        filters=(("Opp.Stage", "equals", "Open"),),
    )
    # Jaccard = 3/5 = 0.6 < 0.8 → 該当なし
    groups_none = similar.find_similar_groups([rec_a, rec_b], column_similarity_threshold=0.8)
    assert groups_none == []
    # 0.5 まで下げれば 「 近い 」
    groups_yes = similar.find_similar_groups([rec_a, rec_b], column_similarity_threshold=0.5)
    assert len(groups_yes) == 1
    pair = groups_yes[0].pairs[0]
    assert pair.category == "近い"


# ── 別タイプ・別形式は比較しない ──────────────────────────────────


def test_different_report_type_does_not_compare() -> None:
    rec_a = _make_record("1001", report_type="Opportunity")
    rec_b = _make_record("1002", report_id="00O000000000002", report_type="Account")
    groups = similar.find_similar_groups([rec_a, rec_b], column_similarity_threshold=0.8)
    assert groups == []


def test_different_format_does_not_compare() -> None:
    rec_a = _make_record("1001", format_="TABULAR")
    rec_b = _make_record("1002", report_id="00O000000000002", format_="SUMMARY")
    groups = similar.find_similar_groups([rec_a, rec_b], column_similarity_threshold=0.8)
    assert groups == []


# ── 3 本以上のグループ ──────────────────────────────────────────


def test_three_records_one_group_with_two_pairs() -> None:
    """3 本の同一レポートは 1 グループに 2 ペア。"""
    rec_a = _make_record("1001", detail_columns=("Opp.Name",))
    rec_b = _make_record("1002", report_id="00O000000000002", detail_columns=("Opp.Name",))
    rec_c = _make_record("1003", report_id="00O000000000003", detail_columns=("Opp.Name",))
    groups = similar.find_similar_groups([rec_a, rec_b, rec_c], column_similarity_threshold=0.8)
    assert len(groups) == 1
    assert groups[0].members == ("1001", "1002", "1003")
    # 基準 1001 起点で 1002 / 1003 と比較する
    pair_keys = sorted([(p.key_a, p.key_b) for p in groups[0].pairs])
    assert pair_keys == [("1001", "1002"), ("1001", "1003")]


# ── 1 件 ───────────────────────────────────────────────────────


def test_single_record_produces_no_group() -> None:
    rec_a = _make_record("1001", detail_columns=("Opp.Name",))
    groups = similar.find_similar_groups([rec_a], column_similarity_threshold=0.8)
    assert groups == []


# ── 条件差分の文章化 ──────────────────────────────────────────


def test_diff_text_includes_added_removed_filters() -> None:
    rec_a = _make_record(
        "1001",
        detail_columns=("Opp.Name",),
        filters=(
            ("Opp.Stage", "equals", "Closed"),
            ("Opp.Type", "equals", "New"),
        ),
    )
    rec_b = _make_record(
        "1002",
        report_id="00O000000000002",
        detail_columns=("Opp.Name",),
        filters=(("Opp.Stage", "equals", "Closed"),),
    )
    groups = similar.find_similar_groups([rec_a, rec_b], column_similarity_threshold=0.8)
    diff = groups[0].pairs[0].diff_text
    assert "削除" in diff
    assert "Opp.Type" in diff


# ── 日付の範囲だけ違う ─────────────────────────────────────────


def test_date_range_only_diff_yields_conditions_arg() -> None:
    """``standardDateFilter`` の日付の範囲だけ違う → 条件を引数にして 1 本に。"""
    rec_a = _make_record(
        "1001",
        detail_columns=("Opp.Name",),
        standard_date={"startDate": "2024-01-01", "endDate": "2024-01-31"},
    )
    rec_b = _make_record(
        "1002",
        report_id="00O000000000002",
        detail_columns=("Opp.Name",),
        standard_date={"startDate": "2024-02-01", "endDate": "2024-02-29"},
    )
    groups = similar.find_similar_groups([rec_a, rec_b], column_similarity_threshold=0.8)
    pair = groups[0].pairs[0]
    assert pair.category == "条件のみ違い"
    assert pair.integration == "条件を引数にして1本にできそう"
