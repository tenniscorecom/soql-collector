"""レポートの列が子オブジェクトの項目のとき、 SOQL の FROM を子に切り替える。

レポートの主オブジェクト（親）ではなく、 レポートの行が 1 行ずつ示す
オブジェクト（子） から SOQL を取る形:

    SELECT Account.Name, Name FROM Contact

サブクエリ （ ``SELECT (SELECT ... FROM Contacts) FROM Account`` ） と
違い、 レポートと同じ 1 行 1 子の粒になり、 Bulk API 2.0 のまま取れるし、
ダウンローダーの変更も要らない。

発動条件は 「列キーの最初の ``.`` より前が 主オブジェクトの
``childRelationships`` の ``childSObject`` か ``relationshipName`` に
大文字小文字無視で一致し、 かつ主オブジェクト名と一致しない行が 1 つでも
ある」 とき。 解決済みかどうかには依存しない。 子の列が表示名の照合で
親の項目 （ 親の ``Name`` など ） に解決されると、 親の値を取り違えるので、
切り替え前に子の列として作り直す。

切り替えられるのは、 レポートの列が指している直接の子オブジェクトが
**1 つだけ** のときだけ。 2 つ以上 （ あるいは同じ子への参照が複数 ） なら
切り替えず、 列は ``(不明)`` のまま理由を残し、 【未完成】 にする。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from src.mapping.column_key import (
    COLUMN_KEY_PATH_MAX_DEPTH,
    resolve_column_key_to_soql,
)

# 列が 「(不明)」 のままかどうか。 空文字も 「取れず」 として扱う
_UNKNOWN_VALUES = ("", "(不明)")

# 候補の子オブジェクトが複数あるときの備考
NOTE_MULTIPLE_CHILDREN = "子オブジェクトが複数あるので 1 つの SOQL にできない: {names}"

# 同じ子オブジェクトへの参照が複数あるときの備考
NOTE_AMBIGUOUS_REFERENCE = "同じ子オブジェクトへの参照が複数あるので判別できません: {candidates}"

# 子の項目定義が取れないときの備考
NOTE_NO_CHILD_DESCRIBE = "子オブジェクト {child} の項目定義が取れないため子から取れません"

# 子の項目に親への参照項目が無いときの備考
NOTE_NO_REFERENCE_FIELD = "子オブジェクト {child} に参照項目 {field} が無いため子から取れません"

# 親の列に経路を付けた結果、 段数が長すぎるときの備考
NOTE_TOO_DEEP = "子から取る形にしたとき経路が {depth} 段で長すぎます（{limit} 段まで）"


@dataclass(frozen=True)
class ChildBaseResult:
    """FROM の切り替えの結果。

    Attributes:
        switched: 切り替えたか。
        child_object: 切り替えた先の子オブジェクト名。
        relationship_name: 子から親へ戻るリレーション名（例 ``Account`` ）。
            親の列はこの名前を頭に付ける。
        parent_side_name: 親から子へ出るリレーション名（例 ``Contacts`` ）。
            列キーの接頭辞がこれのときは子オブジェクト名に置き換える。
        child_describe: 子オブジェクトの describe。
        column_map: 切り替え後の列対応表。
        notes: 切り替えなかった理由。
    """

    switched: bool
    child_object: str = ""
    relationship_name: str = ""
    parent_side_name: str = ""
    child_describe: dict[str, Any] | None = None
    column_map: list[dict[str, str]] = field(default_factory=list)
    notes: tuple[str, ...] = ()


def apply_child_base(
    *,
    column_map: list[dict[str, str]],
    main_object: str,
    main_describe: dict[str, Any] | None,
    fetch_object: Callable[[str], dict[str, Any] | None] | None,
) -> ChildBaseResult:
    """レポートの列が子の項目なら、 列対応表を 子から取る形 に書き換える。

    Args:
        column_map: 列対応表。 書き換えないときはそのまま返る。
        main_object: 主オブジェクト名。
        main_describe: 主オブジェクトの describe。
        fetch_object: 子オブジェクトの describe を取る関数。

    Returns:
        ``ChildBaseResult`` 。 ``switched`` が False のときは
        ``column_map`` は渡されたものと同じ。
    """
    if not main_object:
        return ChildBaseResult(switched=False, column_map=column_map)

    relationships = _child_relationships(main_describe)
    if not relationships:
        return ChildBaseResult(switched=False, column_map=column_map)

    matched, matched_rows = _collect_matching_children(column_map, relationships, main_object)
    if not matched:
        return ChildBaseResult(switched=False, column_map=column_map)

    if len(matched) > 1:
        names = ", ".join(sorted(matched))
        note = NOTE_MULTIPLE_CHILDREN.format(names=names)
        _apply_note([row for rows in matched_rows.values() for row in rows], note)
        return ChildBaseResult(switched=False, column_map=column_map, notes=(note,))

    child_object, child_relationships = next(iter(matched.items()))
    if len(child_relationships) > 1:
        candidates = ", ".join(
            sorted({_relationship_label(relationship) for relationship in child_relationships})
        )
        note = NOTE_AMBIGUOUS_REFERENCE.format(candidates=candidates)
        _apply_note(matched_rows[child_object], note)
        return ChildBaseResult(switched=False, column_map=column_map, notes=(note,))

    relationship = child_relationships[0]
    child_describe = fetch_object(child_object) if fetch_object is not None else None
    if child_describe is None:
        note = NOTE_NO_CHILD_DESCRIBE.format(child=child_object)
        _apply_note(matched_rows[child_object], note)
        return ChildBaseResult(switched=False, column_map=column_map, notes=(note,))

    reference_field = str(relationship.get("field") or "")
    relationship_name = _relationship_to_parent(child_describe, reference_field)
    if relationship_name is None:
        note = NOTE_NO_REFERENCE_FIELD.format(child=child_object, field=reference_field)
        _apply_note(matched_rows[child_object], note)
        return ChildBaseResult(switched=False, column_map=column_map, notes=(note,))

    new_map = _rewrite_column_map(
        column_map,
        child_object=child_object,
        parent_side_name=str(relationship.get("relationshipName") or ""),
        relationship_name=relationship_name,
        child_describe=child_describe,
        fetch_object=fetch_object,
        main_object=main_object,
    )
    return ChildBaseResult(
        switched=True,
        child_object=child_object,
        relationship_name=relationship_name,
        parent_side_name=str(relationship.get("relationshipName") or ""),
        child_describe=child_describe,
        column_map=new_map,
    )


# ── 候補の集計 ────────────────────────────────────────────────────────────


def _collect_matching_children(
    rows: list[dict[str, str]],
    relationships: list[dict[str, Any]],
    main_object: str,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, str]]]]:
    """子の列が指す子オブジェクトと、 その子リレーションを集める。

    対象は 「列キーの最初の ``.`` より前が子に一致し、 かつ主オブジェクト名と
    一致しない行」 。 解決済みかどうかには依存しない。

    戻り値は ``(子オブジェクト名 -> リレーションのリスト, 子オブジェクト名 ->
    対象にした列のリスト)`` 。
    """
    matched: dict[str, list[dict[str, Any]]] = {}
    matched_rows: dict[str, list[dict[str, str]]] = {}
    seen: dict[str, set[tuple[str, str, str]]] = {}
    for row in rows:
        column_key = row.get("列キー", "")
        if "." not in column_key:
            continue
        prefix = _column_prefix(column_key)
        if not prefix or _is_main_object_prefix(prefix, main_object):
            continue
        for relationship in relationships:
            if not _relationship_matches_prefix(relationship, prefix):
                continue
            child = _child_sobject(relationship)
            if not child:
                continue
            key = _relationship_key(relationship)
            keys = seen.setdefault(child, set())
            if key not in keys:
                keys.add(key)
                matched.setdefault(child, []).append(relationship)
            matched_rows.setdefault(child, []).append(row)
    return matched, matched_rows


def _relationship_key(relationship: dict[str, Any]) -> tuple[str, str, str]:
    """リレーションの同一性を表すキー。"""
    return (
        _child_sobject(relationship),
        str(relationship.get("relationshipName") or "").strip().lower(),
        str(relationship.get("field") or "").strip().lower(),
    )


def _is_main_object_prefix(prefix: str, main_object: str) -> bool:
    """列キーの接頭辞が主オブジェクト名か。 大文字小文字は無視する。"""
    return bool(prefix) and prefix.strip().lower() == main_object.strip().lower()


def _relationship_matches_prefix(relationship: dict[str, Any], prefix: str) -> bool:
    """リレーションの ``childSObject`` か ``relationshipName`` が接頭辞に
    一致するか。 大文字小文字は無視する。
    """
    target = prefix.strip().lower()
    child = str(relationship.get("childSObject") or "").strip().lower()
    name = str(relationship.get("relationshipName") or "").strip().lower()
    return bool(target) and (child == target or name == target)


def _child_sobject(relationship: dict[str, Any]) -> str:
    """リレーションの子オブジェクト名。"""
    return str(relationship.get("childSObject") or "").strip()


def _relationship_label(relationship: dict[str, Any]) -> str:
    """候補として出すための、リレーションの見やすい名前。"""
    name = str(relationship.get("relationshipName") or "").strip()
    child = _child_sobject(relationship)
    field = str(relationship.get("field") or "").strip()
    return f"{name}（{child} / {field}）"


def _relationship_to_parent(child_describe: dict[str, Any], reference_field: str) -> str | None:
    """子の項目から、 親へ戻るリレーション名を得る。

    ``childRelationships`` の ``field`` （ 親を参照する子の項目名 ） が子の
    describe に無ければ ``None`` を返す。 項目名の比較は大文字小文字を無視する
    （ ``childRelationships`` の ``field`` は手入力の値もあるため ）。
    """
    if not reference_field:
        return None
    target = reference_field.strip().lower()
    fields = child_describe.get("fields") or []
    matched = [
        field
        for field in fields
        if isinstance(field, dict) and str(field.get("name") or "").strip().lower() == target
    ]
    if not matched:
        return None
    relationship_name = str(matched[0].get("relationshipName") or "").strip()
    if not relationship_name:
        return None
    return relationship_name


# ── 列対応表の書き換え ────────────────────────────────────────────────────


def _rewrite_column_map(
    column_map: list[dict[str, str]],
    *,
    child_object: str,
    parent_side_name: str,
    relationship_name: str,
    child_describe: dict[str, Any],
    fetch_object: Callable[[str], dict[str, Any] | None] | None,
    main_object: str,
) -> list[dict[str, str]]:
    """列対応表を 「子から取る形」 に書き換える。

    - 子の列 … 解決済みかどうかに関係なく、 子の describe で解決し直す。
      解決できなければ (不明) にして理由を書く
    - 親の列（ 解決済み ） … リレーション名を頭に付ける
    - 親の列（ 未解決 ） … そのまま残す（ 親の値も無いので ）
    """
    child_prefixes = _child_prefixes(child_object, parent_side_name)
    rewritten: list[dict[str, str]] = []
    for row in column_map:
        new_row = dict(row)
        if _is_child_row(new_row, child_prefixes=child_prefixes, main_object=main_object):
            _rewrite_child_column(
                new_row,
                child_object=child_object,
                parent_side_name=parent_side_name,
                child_describe=child_describe,
                fetch_object=fetch_object,
            )
        elif not _is_unknown(row):
            _prefix_parent_column(
                new_row,
                relationship_name=relationship_name,
            )
        rewritten.append(new_row)
    return rewritten


def _child_prefixes(child_object: str, parent_side_name: str) -> set[str]:
    """子の列の接頭辞候補 （ 子オブジェクト名 / リレーション名 ） を小文字で集める。"""
    prefixes: set[str] = set()
    for value in (child_object, parent_side_name):
        text = value.strip().lower()
        if text:
            prefixes.add(text)
    return prefixes


def _is_child_row(
    row: dict[str, str],
    *,
    child_prefixes: set[str],
    main_object: str,
) -> bool:
    """行が子の列かどうか。

    列キーの最初の ``.`` より前が子に一致し、 かつ主オブジェクト名と一致
    しないときが子の列。 解決済みかどうかには依存しない。
    """
    column_key = row.get("列キー", "")
    if "." not in column_key:
        return False
    prefix = _column_prefix(column_key).strip().lower()
    if not prefix or prefix not in child_prefixes:
        return False
    return prefix != main_object.strip().lower()


def _rewrite_child_column(
    row: dict[str, str],
    *,
    child_object: str,
    parent_side_name: str,
    child_describe: dict[str, Any],
    fetch_object: Callable[[str], dict[str, Any] | None] | None,
) -> None:
    """列を、 子の項目として解決し直す。"""
    column_key = row.get("列キー", "")
    child_key = _child_column_key(column_key, child_object, parent_side_name)
    soql_name, field_type, failure_note, _id_column = resolve_column_key_to_soql(
        main_object=child_object,
        column_key=child_key,
        main_describe=child_describe,
        fetch_object=fetch_object,
        max_depth=COLUMN_KEY_PATH_MAX_DEPTH,
    )
    if not soql_name:
        # 親の値を残すと別の値を取ってしまうので、 (不明) にして理由を書く
        row["対応フィールドAPI名"] = "(不明)"
        row["型"] = ""
        row["備考"] = failure_note or "子から取れません"
        return
    row["対応フィールドAPI名"] = soql_name
    row["型"] = field_type or ""
    row["所属オブジェクト"] = child_object
    row["備考"] = f"子から取る（子: {child_object}）"


def _prefix_parent_column(row: dict[str, str], *, relationship_name: str) -> None:
    """親の列に、 子から親へ戻るリレーション名を頭に付ける。"""
    soql_name = row.get("対応フィールドAPI名", "")
    prefixed = f"{relationship_name}.{soql_name}"
    depth = len(prefixed.split("."))
    if depth > COLUMN_KEY_PATH_MAX_DEPTH:
        row["対応フィールドAPI名"] = "(不明)"
        row["備考"] = NOTE_TOO_DEEP.format(depth=depth, limit=COLUMN_KEY_PATH_MAX_DEPTH)
        return
    row["対応フィールドAPI名"] = prefixed


def _child_column_key(column_key: str, child_object: str, parent_side_name: str) -> str:
    """列キーの接頭辞がリレーション名のときは、 子オブジェクト名に置き換える。"""
    prefix, separator, rest = column_key.partition(".")
    if not separator:
        return column_key
    if parent_side_name and prefix.strip().lower() == parent_side_name.strip().lower():
        return f"{child_object}.{rest}"
    return column_key


# ── 共通ヘルパー ──────────────────────────────────────────────────────────


def _is_unknown(row: dict[str, str]) -> bool:
    """列が 「(不明)」 （  TAKELEY も空 ） かどうか。"""
    return row.get("対応フィールドAPI名", "") in _UNKNOWN_VALUES


def _column_prefix(column_key: str) -> str:
    """列キーの最初の ``.`` より前の部分。"""
    return column_key.split(".", 1)[0].strip()


def _child_relationships(main_describe: dict[str, Any] | None) -> list[dict[str, Any]]:
    """describe の ``childRelationships`` を dict のリストで返す。"""
    if not main_describe:
        return []
    return [
        item for item in (main_describe.get("childRelationships") or []) if isinstance(item, dict)
    ]


def _apply_note(
    rows: list[dict[str, str]],
    note: str,
) -> None:
    """対象にした列の備考に理由を書く。

    対象にした列は子の列 （ 親のままでは別の値を取る ） なので、 対応
    フィールドは ``(不明)`` にして親の値を残さない。
    """
    for row in rows:
        row["対応フィールドAPI名"] = "(不明)"
        row["型"] = ""
        row["備考"] = note
