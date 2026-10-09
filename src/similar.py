"""似たレポートの判定ロジック（ 純粋関数 ）。

``ReportRecord`` のリストを受けて、 同じ（ レポートタイプ, 形式 ） のレポート
同士を総当たりで比較し、 区分を振る。 区分は次のとおり:

- ``同一``: 構造も条件も同じ（ ID が違うだけ ）
- ``条件のみ違い``: 構造が同じで条件が違う
- ``列のみ違い``: 条件が同じで出力列の集合が違う（ 片方がもう片方を含む場合は 「 含む 」 と書く ）
- ``近い``: 上のどれでもないが出力列の集合の Jaccard が閾値以上
- どれにも該当しないペアは出さない

区分のあるペアでつながるレポートを 「 類似グループ 」 にまとめ、 最小の管理番号
を基準にする。 レポート単位の 統合の見込み:

- ``同一`` → ``1本にまとめられる``
- ``条件のみ違い`` で違いが値だけ → ``条件を引数にして1本にできそう``
- それ以外 → ``要確認``
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

# ── データ構造 ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Structure:
    """レポートの 「 構造 」 （ 出力に出る列の形 ） 。"""

    report_type: str
    format: str
    detail_columns: tuple[str, ...]
    groupings_down: tuple[str, ...]
    groupings_across: tuple[str, ...]
    aggregates: tuple[str, ...]
    sort_by: tuple[tuple[str, str], ...]  # (sortColumn, sortOrder) の順


@dataclass(frozen=True)
class _Condition:
    """レポートの 「 条件 」 （ 絞り込みなど ）。"""

    filters: tuple[tuple[str, str, str], ...]  # (column, operator, value) をソート
    boolean_filter: str
    standard_date: str  # 正規化された JSON 文字列
    standard_filters: str  # 正規化された JSON 文字列
    cross_filters: str  # 正規化された JSON 文字列
    top_rows: str  # 正規化された JSON 文字列
    scope: str


@dataclass(frozen=True)
class _ReportKey:
    """レポートの （ レポートタイプ, 形式 ） 。 比較対象を決めるキー。"""

    report_type: str
    format: str


@dataclass(frozen=True)
class _PairResult:
    """2 レポート間の比較結果 1 件分。"""

    key_a: str
    key_b: str
    category: str  # 区分
    diff_text: str  # 基準との違い（ 人向け ）
    integration: str  # 統合の見込み


@dataclass(frozen=True)
class SimilarGroup:
    """類似グループ 1 件。 基準の管理番号と、 グループ内のレポートの並び。"""

    group_number: int
    base_key: str
    pairs: tuple[_PairResult, ...]
    members: tuple[str, ...]  # 管理番号のリスト（ 基準先頭 ）


# ── 公開 API ────────────────────────────────────────────────────────────


def find_similar_groups(
    records: Iterable[object],
    *,
    column_similarity_threshold: float,
) -> list[SimilarGroup]:
    """``records`` から類似グループを抽出する（ 純粋関数 ）。

    戻り値の各 ``SimilarGroup`` は:

    - 1 件の 基準 レポート（ 最小の管理番号 ） と 1 件以上の 比較対象 を含む
    - 基準と各比較対象の ``_PairResult`` が ``pairs`` に入る
    - グループは 1 から連番、 基準の管理番号の昇順で採番
    """
    items = [(getattr(r, "key", ""), r) for r in records]
    items.sort(key=lambda t: t[0])

    # （ レポートタイプ, 形式 ） でバケットに分ける
    by_bucket: dict[_ReportKey, list[tuple[str, _Structure, _Condition]]] = {}
    for key, record in items:
        structure, condition = _extract(record)
        if structure is None or condition is None:
            continue
        bucket_key = _ReportKey(structure.report_type, structure.format)
        by_bucket.setdefault(bucket_key, []).append((key, structure, condition))

    pair_results: list[_PairResult] = []
    for bucket in by_bucket.values():
        if len(bucket) < 2:
            continue
        pair_results.extend(_compare_bucket(bucket, column_similarity_threshold))

    return _group_pairs(items, pair_results)


# ── 比較 ────────────────────────────────────────────────────────────────


def _compare_bucket(
    bucket: list[tuple[str, _Structure, _Condition]],
    threshold: float,
) -> list[_PairResult]:
    """同じバケット内のレポートを総当たりで比較する。"""
    results: list[_PairResult] = []
    for i in range(len(bucket)):
        for j in range(i + 1, len(bucket)):
            key_a, struct_a, cond_a = bucket[i]
            key_b, struct_b, cond_b = bucket[j]
            pair = _compare_pair(key_a, struct_a, cond_a, key_b, struct_b, cond_b, threshold)
            if pair is not None:
                results.append(pair)
    return results


def _compare_pair(
    key_a: str,
    struct_a: _Structure,
    cond_a: _Condition,
    key_b: str,
    struct_b: _Structure,
    cond_b: _Condition,
    threshold: float,
) -> _PairResult | None:
    """2 レポートを 区分 に振る。 該当なしは None。"""
    same_structure = _structures_equal(struct_a, struct_b)
    same_condition = _conditions_equal(cond_a, cond_b)

    if same_structure and same_condition:
        return _PairResult(
            key_a=key_a,
            key_b=key_b,
            category="同一",
            diff_text="",
            integration="1本にまとめられる",
        )

    if same_structure and not same_condition:
        diff = _describe_condition_diff(cond_a, cond_b, struct_a)
        value_only = _is_value_only_diff(cond_a, cond_b)
        integration = "条件を引数にして1本にできそう" if value_only else "要確認"
        return _PairResult(
            key_a=key_a,
            key_b=key_b,
            category="条件のみ違い",
            diff_text=diff,
            integration=integration,
        )

    if same_condition and not same_structure:
        diff = _describe_column_diff(struct_a, struct_b)
        integration = "要確認"
        return _PairResult(
            key_a=key_a,
            key_b=key_b,
            category="列のみ違い",
            diff_text=diff,
            integration=integration,
        )

    # 構造も条件も違う
    similarity = _jaccard(struct_a.detail_columns, struct_b.detail_columns)
    if similarity >= threshold:
        return _PairResult(
            key_a=key_a,
            key_b=key_b,
            category="近い",
            diff_text=f"出力列の類似度 {similarity:.2f}",
            integration="要確認",
        )
    return None


# ── 構造と条件の抽出 ──────────────────────────────────────────────────


def _extract(record: object) -> tuple[_Structure | None, _Condition | None]:
    """``ReportRecord`` から 構造と条件を取り出す。 取れなければ ``(None, None)``。"""
    report = getattr(record, "report", None)
    if not isinstance(report, dict):
        return None, None
    metadata = report.get("reportMetadata")
    if not isinstance(metadata, dict):
        return None, None
    structure = _extract_structure(metadata)
    condition = _extract_condition(metadata)
    return structure, condition


def _extract_structure(metadata: dict[str, Any]) -> _Structure:
    """``reportMetadata`` から 構造の正規形を作る。"""
    report_type = ""
    report_type_raw = metadata.get("reportType")
    if isinstance(report_type_raw, dict):
        type_value = report_type_raw.get("type")
        if isinstance(type_value, str):
            report_type = type_value

    format_value = metadata.get("reportFormat")
    format_str = format_value if isinstance(format_value, str) else ""

    return _Structure(
        report_type=report_type,
        format=format_str,
        detail_columns=_string_tuple(metadata.get("detailColumns")),
        groupings_down=_grouping_names(metadata.get("groupingsDown")),
        groupings_across=_grouping_names(metadata.get("groupingsAcross")),
        aggregates=_string_tuple(metadata.get("aggregates")),
        sort_by=_sort_pairs(metadata.get("sortBy")),
    )


def _extract_condition(metadata: dict[str, Any]) -> _Condition:
    """``reportMetadata`` から 条件の正規形を作る。"""
    filters_raw = metadata.get("reportFilters")
    filters: list[tuple[str, str, str]] = []
    if isinstance(filters_raw, list):
        for filt in filters_raw:
            if not isinstance(filt, dict):
                continue
            column = filt.get("column", "")
            operator = filt.get("operator", "")
            value = filt.get("value", "")
            filters.append(
                (
                    column if isinstance(column, str) else "",
                    operator if isinstance(operator, str) else "",
                    _normalize_value(value),
                )
            )
    filters_tuple = tuple(sorted(filters))

    boolean_filter = metadata.get("reportBooleanFilter")
    boolean_str = boolean_filter if isinstance(boolean_filter, str) else ""

    return _Condition(
        filters=filters_tuple,
        boolean_filter=boolean_str,
        standard_date=_normalize_json(metadata.get("standardDateFilter")),
        standard_filters=_normalize_json(metadata.get("standardFilters")),
        cross_filters=_normalize_json(metadata.get("crossFilters")),
        top_rows=_normalize_json(metadata.get("topRows")),
        scope=_scope(metadata.get("scope")),
    )


# ── 構造の比較用ヘルパー ──────────────────────────────────────────────


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _grouping_names(value: object) -> tuple[str, ...]:
    """``groupingsDown`` / ``groupingsAcross`` から ``name`` だけ集める。"""
    if not isinstance(value, list):
        return ()
    names: list[str] = []
    for grouping in value:
        if isinstance(grouping, dict):
            name = grouping.get("name")
            if isinstance(name, str):
                names.append(name)
    return tuple(names)


def _sort_pairs(value: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, list):
        return ()
    pairs: list[tuple[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        column = item.get("sortColumn", "")
        order = item.get("sortOrder", "")
        pairs.append(
            (
                column if isinstance(column, str) else "",
                order if isinstance(order, str) else "",
            )
        )
    return tuple(pairs)


def _scope(value: object) -> str:
    return value if isinstance(value, str) else ""


def _normalize_value(value: object) -> str:
    """絞り込みの ``value`` を比較用に正規化する。"""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return _coerce_str(value)
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return _coerce_str(value)


def _normalize_json(value: object) -> str:
    """list / dict を正規化（ 順序非依存 ） した JSON 文字列にする。"""
    if value is None or value == "" or value == [] or value == {}:
        return ""
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return _coerce_str(value)


def _coerce_str(value: object) -> str:
    if value is None:
        return ""
    return str(value)


# ── 構造・条件の比較 ──────────────────────────────────────────────────


def _structures_equal(a: _Structure, b: _Structure) -> bool:
    return (
        a.report_type == b.report_type
        and a.format == b.format
        and a.detail_columns == b.detail_columns
        and a.groupings_down == b.groupings_down
        and a.groupings_across == b.groupings_across
        and a.aggregates == b.aggregates
        and a.sort_by == b.sort_by
    )


def _conditions_equal(a: _Condition, b: _Condition) -> bool:
    return (
        a.filters == b.filters
        and a.boolean_filter == b.boolean_filter
        and a.standard_date == b.standard_date
        and a.standard_filters == b.standard_filters
        and a.cross_filters == b.cross_filters
        and a.top_rows == b.top_rows
        and a.scope == b.scope
    )


def _jaccard(a: tuple[str, ...], b: tuple[str, ...]) -> float:
    """集合の Jaccard 類似度。"""
    set_a = set(a)
    set_b = set(b)
    if not set_a and not set_b:
        return 1.0
    union = set_a | set_b
    if not union:
        return 1.0
    return len(set_a & set_b) / len(union)


# ── 条件差分の文章化 ─────────────────────────────────────────────────


def _describe_condition_diff(
    a: _Condition,
    b: _Condition,
    structure: _Structure,
) -> str:
    """条件差分を人が読める 1 文にする。"""
    parts: list[str] = []
    set_a = {f[0] for f in a.filters}
    set_b = {f[0] for f in b.filters}
    added = set_b - set_a
    removed = set_a - set_b

    if added:
        added_text = ", ".join(
            _format_filter(structure, a, b, col, "added") for col in sorted(added)
        )
        parts.append("追加: " + added_text)
    if removed:
        removed_text = ", ".join(
            _format_filter(structure, a, b, col, "removed") for col in sorted(removed)
        )
        parts.append("削除: " + removed_text)

    # 値だけ違うフィルタ
    common = set_a & set_b
    for col in sorted(common):
        op_a = next((f for f in a.filters if f[0] == col), ("", "", ""))
        op_b = next((f for f in b.filters if f[0] == col), ("", "", ""))
        if op_a[1] == op_b[1] and op_a[2] != op_b[2]:
            parts.append(f"値: {col} {op_a[1]} {op_a[2]} → {op_b[2]}")
        elif op_a[1] != op_b[1]:
            parts.append(f"演算子: {col} {op_a[1]} → {op_b[1]}")

    if a.boolean_filter != b.boolean_filter:
        parts.append(f"組立式: {a.boolean_filter!r} → {b.boolean_filter!r}")

    if a.standard_date != b.standard_date:
        parts.append(f"日付フィルタ: {a.standard_date} → {b.standard_date}")

    if a.standard_filters != b.standard_filters:
        parts.append(f"標準フィルタ: {a.standard_filters} → {b.standard_filters}")

    if a.cross_filters != b.cross_filters:
        parts.append(f"クロスフィルタ: {a.cross_filters} → {b.cross_filters}")

    if a.top_rows != b.top_rows:
        parts.append(f"上位N: {a.top_rows} → {b.top_rows}")

    if a.scope != b.scope:
        parts.append(f"scope: {a.scope!r} → {b.scope!r}")

    if not parts:
        return "条件の細部が異なる"
    return "、".join(parts)


def _format_filter(
    structure: _Structure,
    a: _Condition,
    b: _Condition,
    column: str,
    mode: str,
) -> str:
    """追加 / 削除フィルタの 1 行を組み立てる。"""
    target = b if mode == "added" else a
    op = next((f for f in target.filters if f[0] == column), ("", "", ""))
    return f"{column} {op[1]} {op[2]}"


def _is_value_only_diff(a: _Condition, b: _Condition) -> bool:
    """2 つの条件の差分が 「 値だけ 」 （ 同じ列キー・同じ演算子で値が違う、
    または日付の範囲だけ違う ） かを判定する。
    """
    if a.boolean_filter != b.boolean_filter:
        return False
    if a.standard_filters != b.standard_filters:
        return False
    if a.cross_filters != b.cross_filters:
        return False
    if a.top_rows != b.top_rows:
        return False
    if a.scope != b.scope:
        return False

    set_a = {f[0] for f in a.filters}
    set_b = {f[0] for f in b.filters}
    if set_a != set_b:
        return False
    for col in set_a:
        op_a = next((f for f in a.filters if f[0] == col), ("", "", ""))
        op_b = next((f for f in b.filters if f[0] == col), ("", "", ""))
        if op_a[1] != op_b[1]:
            return False
        if op_a[2] == op_b[2]:
            continue
    return True


# ── 列差分の文章化 ───────────────────────────────────────────────────


def _describe_column_diff(a: _Structure, b: _Structure) -> str:
    """出力列の差分を人が読める 1 文にする。"""
    set_a = set(a.detail_columns)
    set_b = set(b.detail_columns)
    added = sorted(set_b - set_a)
    removed = sorted(set_a - set_b)

    parts: list[str] = []
    if added:
        relation = "（片方がもう片方を含む）" if (set_a <= set_b or set_b <= set_a) else ""
        parts.append("追加: " + ", ".join(added) + relation)
    if removed:
        relation = "（片方がもう片方を含む）" if (set_a <= set_b or set_b <= set_a) else ""
        parts.append("削除: " + ", ".join(removed) + relation)
    if not parts:
        return "出力列の形が異なる"
    return "、".join(parts)


# ── グルーピング ─────────────────────────────────────────────────────


def _group_pairs(
    items: list[tuple[str, object]],
    pairs: list[_PairResult],
) -> list[SimilarGroup]:
    """``_PairResult`` のグラフを連結成分に分け、 グループにまとめる。"""
    if not pairs:
        return []

    # 隣接リスト
    adjacency: dict[str, set[str]] = {}
    for pair in pairs:
        adjacency.setdefault(pair.key_a, set()).add(pair.key_b)
        adjacency.setdefault(pair.key_b, set()).add(pair.key_a)

    # 連結成分
    visited: set[str] = set()
    components: list[list[str]] = []
    for node in adjacency:
        if node in visited:
            continue
        component: list[str] = []
        stack = [node]
        while stack:
            current = stack.pop()
            if current in visited:
                continue
            visited.add(current)
            component.append(current)
            for neighbor in adjacency.get(current, ()):  # type: ignore[arg-type]
                if neighbor not in visited:
                    stack.append(neighbor)
        components.append(component)

    # 管理番号の昇順で基準を決める
    components.sort(key=lambda comp: min(comp))

    # ペアをコンポーネントごとに振り分け
    pair_by_keys: dict[tuple[str, str], _PairResult] = {}
    for pair in pairs:
        sorted_keys: tuple[str, str] = tuple(sorted([pair.key_a, pair.key_b]))  # type: ignore[assignment]
        pair_by_keys[sorted_keys] = pair

    groups: list[SimilarGroup] = []
    for group_index, component in enumerate(components, start=1):
        component_sorted = sorted(component)
        base = component_sorted[0]
        others = component_sorted[1:]
        group_pairs: list[_PairResult] = []
        for other in others:
            key: tuple[str, str] = tuple(sorted([base, other]))  # type: ignore[assignment]
            pair = pair_by_keys.get(key)
            if pair is not None:
                # base 起点に整える
                if pair.key_a == base:
                    group_pairs.append(pair)
                else:
                    group_pairs.append(
                        _PairResult(
                            key_a=pair.key_a,
                            key_b=pair.key_b,
                            category=pair.category,
                            diff_text=pair.diff_text,
                            integration=pair.integration,
                        )
                    )
        groups.append(
            SimilarGroup(
                group_number=group_index,
                base_key=base,
                pairs=tuple(group_pairs),
                members=tuple(component_sorted),
            )
        )
    return groups
