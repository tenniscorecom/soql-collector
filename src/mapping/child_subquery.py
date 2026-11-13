"""レポートの列が子オブジェクトの項目のとき、 SELECT の子リレーション副問い合わせで取る。

「子から取る」 （ ``src/mapping/child_base.py`` ） とは別の形:

    SELECT Name, (SELECT LastName, Email FROM Contacts) FROM Account

- FROM は主オブジェクト （ 親 ） のまま。 親が 1 行、 子が 0 件でも 1 行出る
- 子の列は SELECT の中の 1 つの副問い合わせにまとめる
- クロスフィルタの副問い合わせ （ WHERE 側 ） と同時に使える

**いつこの形にするか** （ 上のものほど優先） :

- **A** … 「列の直し.xlsx」 の 「直したSOQL列名」 に
  ``<子リレーション名>.<子の項目>`` （ 例 ``Contacts.LastName`` ） と
  人が書いた列があるとき。 その列は子の副問い合わせで取る
- **B** … 人の指定が無くても、 子の列があり、 かつ クロスフィルタがあるとき
  （ 従来は 「子から取るレポートのクロスフィルタは未対応」 で missing になっていた ）
- **C** … それ以外。 従来どおり 「子から取る」 形

**A の判定。** 「直したSOQL列名」 の接頭辞が主オブジェクトの
``childRelationships`` の ``relationshipName`` に大文字小文字無視で一致
すれば、 その列は子の副問い合わせで取る。 出力は describe の綴りにする。
接頭辞が主オブジェクトの参照項目の ``relationshipName`` にも一致するなら
曖昧なので要確認を出し、 参照項目 （ 今の動き ） として扱う。

**A・B で副問い合わせにしたときの決まり。**

- 子リレーションが 2 種類以上になるなら作れないので、 missing にする
- 子の列の絞り込み・並び順は副問い合わせの外 （ WHERE / ORDER BY ） に
  書けない。 絞り込みは missing、 並び順は checks にする
- ``use_bulk`` は False。 Bulk API 2.0 は子の副問い合わせを受け付けない
- 状態は最良でも要確認
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from src.mapping.child_base import child_relationships, is_parent_reference_prefix
from src.mapping.column_key import resolve_column_key_to_soql

# 判定結果の 「形」 。 副問い合わせか、 子から取るか
MODE_SUBQUERY = "副問い合わせ"
MODE_CHILD_BASE = "子から取る"

# 「接頭辞.項目」 の形。 人が「直したSOQL列名」に書いた子リレーションの
# 指定を見分けるのに使う
_PREFIXED_PATTERN = re.compile(r"^(?P<prefix>[^.]+)\.(?P<path>.+)$")

# 副問い合わせで取る列の備考。  列の対応のシートに出す
NOTE_SUBQUERY = "子の副問い合わせで取る（子リレーション: {relation}）"

# 副問い合わせで取るときの、 人が直した列の備考
NOTE_HUMAN_SUBQUERY = "直したSOQL列名（人が直した）"

# 人が直した値が、 子の項目定義に無いときの備考
NOTE_HUMAN_UNRESOLVED = "人が直した（要確認: たどれない）"

# 接頭辞が 子リレーションにも 参照項目にも一致したときの、 要確認の文言
NOTE_AMBIGUOUS_PREFIX = (
    "「{prefix}」は子リレーション {relation} にも主オブジェクトの参照項目にも"
    "一致するので、参照項目として扱った: {column_key}"
)

# 子の項目定義が取れないときの、 要確認の文言
NOTE_NO_CHILD_DESCRIBE = (
    "子オブジェクト {child} の項目定義が取れないため、項目 {field} は確かめられない"
)

# 子の項目に無い項目名だったときの、 要確認の文言
NOTE_NO_CHILD_FIELD = "子オブジェクト {child} に項目 {field} が無い"

# 子リレーションが 2 種類以上になって作れないときの理由
NOTE_MULTIPLE_RELATIONS = "子リレーションが複数あるので 1 つの副問い合わせにできません: {names}"


@dataclass(frozen=True)
class ChildSubquery:
    """SOQL の SELECT に入れる、 子リレーションの副問い合わせ。

    Attributes:
        relation_name: 子リレーション名（ describe の綴り。 例 ``Contacts`` ）。
        child_object: 子オブジェクト名（ 例 ``Contact`` ）。
        fields: 子の項目（ 例 ``("LastName", "Owner.Name")`` ）。
    """

    relation_name: str
    child_object: str
    fields: tuple[str, ...]


@dataclass(frozen=True)
class ChildShapeResult:
    """「副問い合わせ」か「子から取る」かの判定結果。

    Attributes:
        mode: ``副問い合わせ`` か ``子から取る`` か。
        subquery: 副問い合わせの計画。 副問い合わせにできないときは ``None`` 。
        column_map: 判定後の列対応表。 子の列を書き換えてある。
        child_column_keys: 副問い合わせで取る列の列キー。
        notes: 作れなかった理由（ ``missing`` に入る ）。
        checks: 要確認の理由（ ``checks`` に入る ）。
    """

    mode: str
    subquery: ChildSubquery | None = None
    column_map: list[dict[str, str]] = field(default_factory=list)
    child_column_keys: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    checks: tuple[str, ...] = ()


@dataclass(frozen=True)
class _ChildColumn:
    """副問い合わせで取る 1 列。"""

    column_key: str
    relation_name: str
    child_object: str
    field_path: str = ""
    field_type: str = ""
    from_human: bool = False
    verified: bool = True


def apply_child_shape(
    *,
    column_map: list[dict[str, str]],
    main_object: str,
    main_describe: dict[str, Any] | None,
    fetch_object: Callable[[str], dict[str, Any] | None] | None,
    human_fixes: Mapping[str, str],
    has_cross_filter: bool,
) -> ChildShapeResult:
    """レポートの列が子の項目なら、 副問い合わせか 子から取るかを決めて
    列対応表を書き換える。

    Args:
        column_map: 列対応表。 書き換えないときはそのまま返す。
        main_object: 主オブジェクト名。
        main_describe: 主オブジェクトの describe。
        fetch_object: 子オブジェクトの describe を取る関数。
        human_fixes: 列キー → 人が「直したSOQL列名」に書いた値。
        has_cross_filter: レポートにクロスフィルタがあるか。

    Returns:
        ``ChildShapeResult`` 。 ``mode`` が ``子から取る`` のときは
        ``column_map`` は渡されたものと同じ。
    """
    if not main_object or not column_map:
        return ChildShapeResult(mode=MODE_CHILD_BASE, column_map=column_map)

    relationships = child_relationships(main_describe)
    if not relationships:
        return ChildShapeResult(mode=MODE_CHILD_BASE, column_map=column_map)

    checks: list[str] = []
    human_columns, human_checks = _collect_human_child_columns(
        column_map,
        relationships,
        main_describe,
        human_fixes,
    )
    checks.extend(human_checks)
    auto_columns = _collect_auto_child_columns(
        column_map,
        relationships,
        main_object,
        main_describe,
        human_fixes,
    )

    if human_columns:
        relation_names = sorted({column.relation_name for column in human_columns})
        if len(relation_names) > 1:
            return _too_many_relations_result(column_map, human_columns, relation_names, checks)
        relation_name = relation_names[0]
        # 同じ子リレーションの列 （ 人が書いていないもの ） も一緒に取る
        human_keys = {column.column_key for column in human_columns}
        chosen = human_columns + [
            column
            for column in auto_columns
            if column.relation_name == relation_name and column.column_key not in human_keys
        ]
    elif auto_columns and has_cross_filter:
        relation_names = sorted({column.relation_name for column in auto_columns})
        if len(relation_names) > 1:
            return _too_many_relations_result(column_map, auto_columns, relation_names, checks)
        chosen = auto_columns
    else:
        # C: 従来どおり 「子から取る」 形
        return ChildShapeResult(mode=MODE_CHILD_BASE, column_map=column_map, checks=tuple(checks))

    relation_name = chosen[0].relation_name
    child_object = chosen[0].child_object
    resolved = _resolve_child_columns(
        chosen,
        child_object=child_object,
        fetch_object=fetch_object,
        checks=checks,
    )
    if not resolved:
        # 子の項目が 1 つも取れなければ副問い合わせにしない
        return ChildShapeResult(mode=MODE_CHILD_BASE, column_map=column_map, checks=tuple(checks))

    fields = tuple(column.field_path for column in resolved)
    new_map = _rewrite_column_map(
        column_map,
        resolved,
        relation_name=relation_name,
        child_object=child_object,
    )
    return ChildShapeResult(
        mode=MODE_SUBQUERY,
        subquery=ChildSubquery(
            relation_name=relation_name,
            child_object=child_object,
            fields=fields,
        ),
        column_map=new_map,
        child_column_keys=tuple(column.column_key for column in resolved),
        notes=(),
        checks=tuple(checks),
    )


def _too_many_relations_result(
    column_map: list[dict[str, str]],
    columns: list[_ChildColumn],
    relation_names: list[str],
    checks: list[str],
) -> ChildShapeResult:
    """子リレーションが 2 種類以上なので作れないときの結果。"""
    note = NOTE_MULTIPLE_RELATIONS.format(names=", ".join(relation_names))
    return ChildShapeResult(
        mode=MODE_SUBQUERY,
        subquery=None,
        column_map=_mark_unknown(column_map, columns, note),
        child_column_keys=tuple(column.column_key for column in columns),
        notes=(note,),
        checks=tuple(checks),
    )


# ── A: 人が「直したSOQL列名」に書いた列 ─────────────────────────────


def _collect_human_child_columns(
    rows: list[dict[str, str]],
    relationships: list[dict[str, Any]],
    main_describe: dict[str, Any] | None,
    human_fixes: Mapping[str, str],
) -> tuple[list[_ChildColumn], list[str]]:
    """人が「直したSOQL列名」に書いた、 子リレーションの列を拾う。

    接頭辞が主オブジェクトの参照項目にも一致するなら曖昧なので、 要確認を
    出して参照項目 （ 今の動き ） として扱う。
    """
    columns: list[_ChildColumn] = []
    checks: list[str] = []
    for row in rows:
        column_key = row.get("列キー", "")
        fixed = (human_fixes.get(column_key) or "").strip()
        match = _PREFIXED_PATTERN.match(fixed)
        if match is None:
            continue
        prefix = match.group("prefix")
        relationship = _relationship_by_relationship_name(relationships, prefix)
        if relationship is None:
            continue
        relation_name = str(relationship.get("relationshipName") or "")
        child_object = str(relationship.get("childSObject") or "")
        if is_parent_reference_prefix(prefix, main_describe):
            checks.append(
                NOTE_AMBIGUOUS_PREFIX.format(
                    prefix=prefix,
                    relation=relation_name,
                    column_key=column_key,
                )
            )
            continue
        columns.append(
            _ChildColumn(
                column_key=column_key,
                relation_name=relation_name,
                child_object=child_object,
                field_path=match.group("path"),
                from_human=True,
            )
        )
    return columns, checks


# ── B: 子の列の自動判定 ───────────────────────────────────────────


def _collect_auto_child_columns(
    rows: list[dict[str, str]],
    relationships: list[dict[str, Any]],
    main_object: str,
    main_describe: dict[str, Any] | None,
    human_fixes: Mapping[str, str],
) -> list[_ChildColumn]:
    """列キーの接頭辞が子リレーションに一致する列を拾う。

    接頭辞が主オブジェクト名と一致するもの、 主オブジェクトの親参照に一致
    するものは除く。 リレーションが特定できない列 （ 同じ子オブジェクトへの
    参照が複数 ） も除く。 人が「直したSOQL列名」を書いている列は、 書いた
    値をそのまま子の項目として使う。
    """
    columns: list[_ChildColumn] = []
    for row in rows:
        column_key = row.get("列キー", "")
        prefix = _column_prefix(column_key)
        if not prefix or prefix.lower() == main_object.strip().lower():
            continue
        if is_parent_reference_prefix(prefix, main_describe):
            continue
        matched = [
            relationship
            for relationship in relationships
            if _relationship_matches_prefix(relationship, prefix)
        ]
        if len(matched) != 1:
            continue
        relationship = matched[0]
        human_value = (human_fixes.get(column_key) or "").strip()
        columns.append(
            _ChildColumn(
                column_key=column_key,
                relation_name=str(relationship.get("relationshipName") or ""),
                child_object=str(relationship.get("childSObject") or ""),
                field_path=human_value,
                from_human=bool(human_value),
            )
        )
    return columns


# ── 子の項目の解決 ────────────────────────────────────────────────


def _resolve_child_columns(
    columns: list[_ChildColumn],
    *,
    child_object: str,
    fetch_object: Callable[[str], dict[str, Any] | None] | None,
    checks: list[str],
) -> list[_ChildColumn]:
    """子の項目（ パス ）を、 子の describe で確かめる。

    人が指定した値はそのまま使うが、 子の項目定義に無いときは要確認を足す。
    自動の列は、 列キーから子の項目として解決し直す。 解決できない列は
    副問い合わせに入れず、 元の 「(不明)」 のままにする。
    """
    child_describe = fetch_object(child_object) if fetch_object is not None else None
    resolved: list[_ChildColumn] = []
    for column in columns:
        if column.from_human:
            field_type, verified = _check_human_field(
                column,
                child_object=child_object,
                child_describe=child_describe,
                checks=checks,
            )
            resolved.append(
                _ChildColumn(
                    column_key=column.column_key,
                    relation_name=column.relation_name,
                    child_object=column.child_object,
                    field_path=column.field_path,
                    field_type=field_type,
                    from_human=True,
                    verified=verified,
                )
            )
            continue
        field_path, field_type = _resolve_auto_field_path(
            column,
            child_object=child_object,
            child_describe=child_describe,
            fetch_object=fetch_object,
        )
        if not field_path:
            continue
        resolved.append(
            _ChildColumn(
                column_key=column.column_key,
                relation_name=column.relation_name,
                child_object=column.child_object,
                field_path=field_path,
                field_type=field_type,
            )
        )
    return resolved


def _resolve_auto_field_path(
    column: _ChildColumn,
    *,
    child_object: str,
    child_describe: dict[str, Any] | None,
    fetch_object: Callable[[str], dict[str, Any] | None] | None,
) -> tuple[str, str]:
    """自動の列の、 子の項目のパスと型。 解決できなければ空文字 2 つ。"""
    if child_describe is None:
        return "", ""
    column_key = column.column_key
    prefix, separator, rest = column_key.partition(".")
    if not separator:
        return "", ""
    # 接頭辞がリレーション名のときは、 子オブジェクト名に置き換えてから渡す
    if prefix.strip().lower() == column.relation_name.strip().lower():
        column_key = f"{child_object}.{rest}"
    soql_name, field_type, _note, _id_column = resolve_column_key_to_soql(
        main_object=child_object,
        column_key=column_key,
        main_describe=child_describe,
        fetch_object=fetch_object,
    )
    return soql_name or "", field_type or ""


def _check_human_field(
    column: _ChildColumn,
    *,
    child_object: str,
    child_describe: dict[str, Any] | None,
    checks: list[str],
) -> tuple[str, bool]:
    """人が指定した子の項目が、 子の項目定義に無いときの要確認を足す。

    Returns:
        ``(項目の型, 項目定義で確かめられたか)`` 。
    """
    field_name = column.field_path.split(".", 1)[0]
    if child_describe is None:
        checks.append(NOTE_NO_CHILD_DESCRIBE.format(child=child_object, field=column.field_path))
        return "", False
    fields = child_describe.get("fields") or []
    matched = next(
        (
            field
            for field in fields
            if isinstance(field, dict)
            and str(field.get("name") or "").strip().lower() == field_name.strip().lower()
        ),
        None,
    )
    if matched is None:
        checks.append(NOTE_NO_CHILD_FIELD.format(child=child_object, field=column.field_path))
        return "", False
    return str(matched.get("type") or ""), True


# ── 列対応表の書き換え ────────────────────────────────────────────


def _rewrite_column_map(
    rows: list[dict[str, str]],
    columns: list[_ChildColumn],
    *,
    relation_name: str,
    child_object: str,
) -> list[dict[str, str]]:
    """子の列を、 副問い合わせで取る形 （ ``Contacts.LastName`` ） に書き換える。"""
    targets = {column.column_key: column for column in columns}
    rewritten: list[dict[str, str]] = []
    for row in rows:
        new_row = dict(row)
        column = targets.get(new_row.get("列キー", ""))
        if column is None:
            rewritten.append(new_row)
            continue
        if column.from_human:
            # 人が直した値は、 「自動のSOQL列名」 に見せたままにする
            new_row["自動の値"] = new_row.get("対応フィールドAPI名", "")
        new_row["対応フィールドAPI名"] = f"{relation_name}.{column.field_path}"
        new_row["所属オブジェクト"] = child_object
        new_row["型"] = column.field_type
        if column.from_human:
            new_row["備考"] = NOTE_HUMAN_SUBQUERY if column.verified else NOTE_HUMAN_UNRESOLVED
        else:
            new_row["備考"] = NOTE_SUBQUERY.format(relation=relation_name)
        rewritten.append(new_row)
    return rewritten


def _mark_unknown(
    rows: list[dict[str, str]],
    columns: list[_ChildColumn],
    note: str,
) -> list[dict[str, str]]:
    """副問い合わせにできない子の列を、 ``(不明)`` にして理由を書く。"""
    targets = {column.column_key for column in columns}
    rewritten: list[dict[str, str]] = []
    for row in rows:
        new_row = dict(row)
        if new_row.get("列キー", "") in targets:
            new_row["対応フィールドAPI名"] = "(不明)"
            new_row["型"] = ""
            new_row["備考"] = note
        rewritten.append(new_row)
    return rewritten


# ── 共通ヘルパー ──────────────────────────────────────────────────


def _column_prefix(column_key: str) -> str:
    """列キーの最初の ``.`` より前の部分。"""
    return column_key.split(".", 1)[0].strip()


def _relationship_by_relationship_name(
    relationships: list[dict[str, Any]],
    name: str,
) -> dict[str, Any] | None:
    """``relationshipName`` が一致するリレーションを 1 つ返す。 無ければ ``None`` 。"""
    target = name.strip().lower()
    if not target:
        return None
    for relationship in relationships:
        relation_name = str(relationship.get("relationshipName") or "").strip().lower()
        if relation_name and relation_name == target:
            return relationship
    return None


def _relationship_matches_prefix(relationship: dict[str, Any], prefix: str) -> bool:
    """リレーションの ``childSObject`` か ``relationshipName`` が接頭辞に
    一致するか。 大文字小文字は無視する。
    """
    target = prefix.strip().lower()
    child = str(relationship.get("childSObject") or "").strip().lower()
    name = str(relationship.get("relationshipName") or "").strip().lower()
    return bool(target) and (child == target or name == target)
