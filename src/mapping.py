"""comken から移した、レポートの列 ⇔ Salesforce フィールド対応表の組み立て。

``report_type_candidates()`` がカスタムレポートタイプの ``$`` / ``@`` を
含む値から主オブジェクト候補を抽出し、 ``build_column_map()`` が渡された
describe から列対応表を作る。

``build_column_map()`` は主オブジェクトに加えて、関連オブジェクトの
describe と relations を受け取り、 関連オブジェクトを跨ぐ列も解決する。

2026-10-14: 列キーが ``主オブジェクト.`` で始まる形（ 例:
``Contact.Account.Name`` ） については、 表示名ではなく列キー自体から
主オブジェクトをたどって SOQL 列名を決めるようにした。 カスタムレポート
タイプで人が主オブジェクトを知らなくても SOQL 組立に必要な列名を
取れるようにするため。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.fetch import RelationRecord

# カスタムレポートタイプの ``reportType.type`` に前置される ``CustomEntity``
# プレースホルダを候補抽出時に除くための正規表現。
_CUSTOM_ENTITY_TOKEN_PATTERN = re.compile(r"\A(?:CustomEntity)+\Z")


def report_type_candidates(report_type: str) -> list[str]:
    """``reportType.type`` から主オブジェクトの候補を取り出す（純粋関数）。

    カスタムレポートタイプでは ``CustomEntity$Project__c`` や ``Account@Contact``
    のように ``$`` や ``@`` を含む値が入ることがある。 そのまま
    ``describe_object()`` に渡すと ``ValueError`` で止まるため、 ``$`` / ``@``
    で区切って候補のリストにし、 先頭から ``describe_object()`` が通るものを
    採る。

    ルール:

    - ``@`` が含まれるときは、 最初の ``@`` より左だけを使う
    - 残りを ``$`` で区切る
    - 空文字と、 ``CustomEntity`` を 1 回以上繰り返しただけのトークン
      （ ``CustomEntity`` / ``CustomEntityCustomEntity`` など） を除く
    - 重複は除き、 出現順を保つ
    - ``$`` も ``@`` も無ければ ``[report_type]``

    Examples:
        >>> report_type_candidates("CustomEntity$Project__c")
        ['Project__c']
        >>> report_type_candidates("Account@Contact")
        ['Account']
        >>> report_type_candidates("Opportunity")
        ['Opportunity']
        >>> report_type_candidates("CustomEntity$")
        []
    """
    if "@" in report_type:
        report_type = report_type.split("@", 1)[0]
    tokens = report_type.split("$")
    cleaned = [
        token for token in tokens if token and not _CUSTOM_ENTITY_TOKEN_PATTERN.fullmatch(token)
    ]
    seen: set[str] = set()
    unique: list[str] = []
    for token in cleaned:
        if token not in seen:
            seen.add(token)
            unique.append(token)
    return unique


def candidate_failure_reason(original: str, candidates: list[str]) -> str:
    """``$`` / ``@`` を含む ``reportType.type`` で候補を試したが全滅した
    （または候補が空だった） ときの理由文言を組み立てる。

    元の ``reportType.type`` の値と試した候補の一覧を添え、 「手動で確認」
    の手引きを入れる。 ``build_column_map()`` の備考列にそのまま埋め込む
    1 行分のテキスト。
    """
    if candidates:
        candidate_text = ", ".join(repr(candidate) for candidate in candidates)
        return (
            f"レポートタイプ {original!r} から主オブジェクト候補"
            f" {candidate_text} を順に確認しましたが、どれも Object Describe"
            " できず自動判定できません。手動で確認してください"
        )
    return (
        f"レポートタイプ {original!r} からは主オブジェクト候補を抽出できなかったため"
        "自動判定できません。手動で確認してください"
    )


def _normalize_label(label: str) -> str:
    """表示名の前後空白を落として小文字化する。

    Salesforce の表示名は前後に空白が入ることがあり、 また大小文字の
    ゆらぎは実フィールド側と一致しないことがあるため、 突き合わせの前に
    もう一段正規化する。 完全一致しなかったときのフォールバックは敢えて
    行わない — 不一致は「不一致」のまま残し、 誤った候補を押し付けない
    ことを優先するため。
    """
    return label.strip().lower()


# レポートの列表示名に 「 オブジェクト: 項目名 」 の形 （ 半角・全角コロン、 前後の空白あり/なし ）
# があるかを判定する正規表現。 コロンは ``:`` （ U+003A ） と ``：`` （ U+FF1A ） の両方を
# 受け付ける
_OBJECT_LABEL_PATTERN = re.compile(r"^\s*(?P<object>.+?)\s*[:：]\s*(?P<field>.+?)\s*$")


def _split_object_label(label: str) -> tuple[str | None, str]:
    """表示名が ``「 オブジェクト: 項目名 」`` 形式なら ``(オブジェクト, 項目)`` に分解する。

    コロンは半角・全角どちらでも受け付ける。 そうでないときは ``(None, label)`` を返す。
    """
    match = _OBJECT_LABEL_PATTERN.match(label)
    if match is None:
        return None, label
    object_part = match.group("object")
    field_part = match.group("field")
    return object_part, field_part


def _normalize_identifier(value: str) -> str:
    """識別子の比較用に正規化する。 大文字小文字と ``_`` を無視する。"""
    return value.strip().lower().replace("_", "")


# 列キーから SOQL 列名を取りに行くときのパスの最大段数。 6 段以上なら
# 経路が長すぎると判断して表示名引き当てにフォールバックする
COLUMN_KEY_PATH_MAX_DEPTH: int = 5


def _starts_with_main_object(column_key: str, main_object: str) -> bool:
    """``column_key`` が ``main_object + "."`` で始まるか。 大小文字を無視する。

    ``Contact2.Mainten`` のような一部一致は誤検出しない。 必ず次の文字が
    ``.`` であることを見る。
    """
    if not column_key or not main_object:
        return False
    if len(column_key) <= len(main_object):
        return False
    if column_key[len(main_object)] != ".":
        return False
    return column_key[: len(main_object)].lower() == main_object.lower()


def candidate_relationship_names(fields: list[dict[str, Any]], limit: int = 10) -> list[str]:
    """``fields`` の ``relationshipName`` を出現順で最大 ``limit`` 個返す。

    列キーからの経路たどりに失敗したときの備考で使う。
    """
    return [field["relationshipName"] for field in fields if field.get("relationshipName")][:limit]


def _resolve_name_field_for_reference(
    reference_to: list[str],
    objects: dict[str, dict[str, Any]],
    fetch_object: Callable[[str], dict[str, Any] | None] | None,
) -> tuple[str, str]:
    """参照項目で末端を置換するときの、 参照先の名前項目 ``(name, type)`` を返す。

    レポートの列表示は参照項目の名前項目（ Account なら ``Name`` 、 Case なら
    ``CaseNumber`` ） と同じなので、 SOQL も ID ではなく名前項目で取らないと
    表示値と合わない。 決め方:

    - ``referenceTo`` がちょうど 1 つのときだけ、 参照先の describe を
      ``objects`` （ 既に ``_resolve_column_key_to_soql`` 内で集めたもの ）
      → ``fetch_object`` の順で探し、 ``nameField`` が True の項目を採る
    - ``referenceTo`` が 2 つ以上 （ ポリモーフィック ） ・ describe が取れない
      ・ ``nameField`` 項目が無い、 のいずれかは ``("Name", "string")`` に
      フォールバック
    - ``fetch_object`` が 401 / 403 を投げたら **ここでは握りつぶさず**
      上位へ送出する （ 既存と同じ ）

    戻り値の ``type`` は ``"string"`` 固定にする （ 名前項目が ``text`` /
    ``string`` どちらの組織でもレポート表示は文字列なので、 ``text`` を
    返されると SOQL 組立側で迷うため ） 。
    """
    if len(reference_to) != 1:
        return "Name", "string"
    target = reference_to[0]
    target_describe = objects.get(target)
    if target_describe is None and fetch_object is not None:
        target_describe = fetch_object(target)
        if target_describe is not None:
            objects[target] = target_describe
    if target_describe is None:
        return "Name", "string"
    for field in target_describe.get("fields", []):
        if field.get("nameField") is True and field.get("name"):
            return field["name"], "string"
    return "Name", "string"


def _resolve_column_key_to_soql(
    main_object: str,
    column_key: str,
    main_describe: dict[str, Any],
    fetch_object: Callable[[str], dict[str, Any] | None] | None,
    *,
    max_depth: int = COLUMN_KEY_PATH_MAX_DEPTH,
) -> tuple[str | None, str | None, str | None, str | None]:
    """``column_key`` を ``main_object`` から 1 段ずつたどって
    ``(soql_name, type, failure_note, id_column)`` を返す。

    成功時 ``(soql_name, field_type, None, id_column)`` 。
    失敗時 ``(None, None, note, None)`` 。
    ``note`` は表示名の照合にフォールバックしたときに備考に足す一行分
    （ 「 列キーで『 X 』が Y に見つかりません（ リレーション名の候補: … ） 」 ）。
    呼び出し側で表示名の照合が決まったときはこの note は **使わない**
    （ 「 決まったほうを優先 」 のため ） 。

    ``id_column`` は **末端の項目が参照項目でレポート表示と同じ名前項目に
    置き換えたとき**の元の ID 列の SOQL 名。 置き換えなかったとき / 失敗
    したときは ``None`` 。 例:

    - ``Task__c.Custom_Name__c`` （ 末端が参照 ） → soql_name
      ``Custom_Name__r.Name`` 、 id_column ``Custom_Name__c``
    - ``Contact.Account.Name`` （ 末端が ``Name`` ） → soql_name
      ``Account.Name`` 、 id_column ``None`` （ 置換しない ）

    各段の項目を探す順は ``_walk_soql_path`` と同じ:

    1. ``relationshipName`` が一致する参照項目
    2. ``name`` が一致する項目

    途中の段がマッチしたとき（ 最後の段以外 ） は、 **SOQL 上は** その項目
    の ``relationshipName`` （ 例: ``AccountId`` 参照 → ``Account`` ） に
    置き換える。 最後の段は項目の ``name`` （ ``Account.Owner.Name`` の
    末端 ``Name`` ） が基本だが、 **末端が ``type == "reference"`` かつ
    ``relationshipName`` が空でない参照項目のときは、 参照先の名前項目に
    置き換える** （ レポート表示が名前項目で、 ID 列では値が違うため ） 。

    経路上のオブジェクトが ``main_describe`` 起点の ``objects`` に無い
    ときは ``fetch_object`` で取りに行く。 取得は ``fetch_object`` の内部で
    既存のキャッシュ → API の順で行われる前提。 401 / 403 は
    ``fetch_object`` からそのまま上位へ送出される（ ``_make_overrides_fetcher``
    と同じ挙動 ）。
    """
    if not _starts_with_main_object(column_key, main_object):
        return None, None, None, None
    suffix = column_key[len(main_object) + 1 :]
    parts = suffix.split(".")
    # 空の経路（ ``Contact.`` など ） や ``max_depth`` を超える経路は失敗
    if not parts or any(not p for p in parts):
        return None, None, None, None
    if len(parts) > max_depth:
        return (
            None,
            None,
            (f"列キーで {len(parts)} 段の経路は長すぎます（ {max_depth} 段まで）"),
            None,
        )

    objects: dict[str, dict[str, Any]] = {main_object: main_describe}
    soql_segments: list[str] = []
    current_object = main_object
    for index, part in enumerate(parts):
        describe = objects.get(current_object)
        if describe is None:
            return (
                None,
                None,
                (f"列キーで「{part}」を扱う {current_object} の項目定義が無いためたどれません"),
                None,
            )
        fields: list[dict[str, Any]] = describe.get("fields", [])

        # 参照項目のリレーション名で一致するものを先に、 無ければ項目名で探す
        matched = next((field for field in fields if field.get("relationshipName") == part), None)
        if matched is None:
            matched = next((field for field in fields if field.get("name") == part), None)

        if matched is None:
            candidates = candidate_relationship_names(fields)
            cand_text = ", ".join(candidates) if candidates else "(なし)"
            return (
                None,
                None,
                (
                    f"列キーで「{part}」が {current_object} に見つかりません"
                    f"（リレーション名の候補: {cand_text}）"
                ),
                None,
            )

        if index == len(parts) - 1:
            # 最後の段: SOQL は ``name`` が基本。 ただし ``type == "reference"``
            # かつ ``relationshipName`` が空でない参照項目のときは、 レポート
            # 表示と同じになるよう参照先の名前項目に置き換える
            field_name = matched.get("name")
            if not field_name:
                return None, None, None, None  # 形式が壊れている。 表示名引き当てに任せる
            field_type = matched.get("type") or ""
            relationship = matched.get("relationshipName")
            if field_type == "reference" and relationship:
                reference_to: list[str] = matched.get("referenceTo") or []
                name_field_name, name_field_type = _resolve_name_field_for_reference(
                    reference_to, objects, fetch_object
                )
                # 元の ID 列の SOQL 名を保持 （ 備考で ID 列の代替手段を示す ）
                id_column = ".".join([*soql_segments, field_name])
                soql_segments.append(relationship)
                soql_segments.append(name_field_name)
                return ".".join(soql_segments), name_field_type, None, id_column
            soql_segments.append(field_name)
            return ".".join(soql_segments), field_type, None, None

        # 途中の段: SOQL は ``relationshipName`` （ 参照項目の名前空間 ）
        relationship = matched.get("relationshipName")
        if not relationship:
            return (
                None,
                None,
                (
                    f"列キーで「{part}」は {current_object} の参照項目ではないため"
                    "経路を続けられません"
                ),
                None,
            )
        soql_segments.append(relationship)

        # 次のオブジェクトを決める。 単一参照は ``referenceTo`` の先頭、
        # ``objects`` にある候補があればそれを優先
        reference_to = matched.get("referenceTo") or []
        next_object: str | None = None
        for cand in reference_to:
            if cand in objects:
                next_object = cand
                break
            if fetch_object is not None:
                fetched = fetch_object(cand)
                if fetched is not None:
                    objects[cand] = fetched
                    next_object = cand
                    break
        if next_object is None:
            target = reference_to[0] if reference_to else "(対象不明)"
            return (
                None,
                None,
                (f"列キーで参照先 {target} の項目定義を取得できません"),
                None,
            )
        current_object = next_object

    return None, None, None, None


@dataclass(frozen=True)
class _ChainCandidate:
    """主オブジェクトからある関連オブジェクトまでの候補 1 本。

    ``chain``: ``.`` 区切りのリレーション名シーケンス （ 浅い順 ）。
    ``object_name``: 末端のオブジェクト名。
    ``depth``: 主オブジェクトから数えた段数。
    """

    chain: str
    object_name: str
    depth: int


def build_relation_chains(
    relations: list[RelationRecord],
    main_object: str | None,
) -> dict[str, list[_ChainCandidate]]:
    """``relations`` から「 主→ そのオブジェクトまでのリレーション鎖 」 を作る。

    関連オブジェクト名ごとに、 浅い順に並んだ ``_ChainCandidate`` のリスト
    を返す。 各オブジェクトに **複数の経路** がある場合は、 最も浅い経路を
    先頭にして、 同じ深さなら出現順を保つ。 主オブジェクト自身は対象外
    （ 候補に入れない ）。

    鎖は **リレーション名のシーケンス** で、 例: Task → Account （ rel="What" ）
    → User （ rel="Owner" ） のとき User の鎖は ``"What.Owner"`` 。

    Args:
        relations: ``fetch._collect_related_objects`` が返した
            ``RelationRecord`` のリスト。 各要素は ``parent`` / ``field`` /
            ``relationship_name`` / ``child`` / ``depth`` を持つ。
        main_object: 主オブジェクト名。 ``None`` のときは空 dict を返す。

    Returns:
        ``{子オブジェクト名: [_ChainCandidate, ...]}`` のマッピング。
    """
    if not main_object:
        return {}

    # 主→子の直エッジをまず集める
    adjacency: dict[str, list[tuple[str, str | None, str]]] = {}
    for rel in relations:
        parent = rel.parent
        child = rel.child
        rel_name = rel.relationship_name
        if not (parent and child):
            continue
        if parent == main_object:
            # 主からの直エッジは depth=1 の chain になる
            adjacency.setdefault(parent, []).append((child, rel_name, child))

    # より深い段も集める （ 内部の関連 ）
    for rel in relations:
        parent = rel.parent
        child = rel.child
        rel_name = rel.relationship_name
        if not (parent and child):
            continue
        if parent != main_object:
            adjacency.setdefault(parent, []).append((child, rel_name, child))

    # BFS で主から各オブジェクトまでの鎖を計算
    visited: dict[str, str] = {}  # 子 -> chain (リレーション名鎖)
    visited_depth: dict[str, int] = {}  # 子 -> depth
    queue: list[tuple[str, str, int]] = [(main_object, "", 0)]
    visited[main_object] = ""
    visited_depth[main_object] = 0
    head = 0
    while head < len(queue):
        current, chain, depth = queue[head]
        head += 1
        for child, rel_name, _ in adjacency.get(current, []):
            if child in visited:
                continue
            if not rel_name:
                # リレーション名が空の辺は鎖に使わない
                continue
            new_chain = f"{chain}.{rel_name}" if chain else rel_name
            visited[child] = new_chain
            visited_depth[child] = depth + 1
            queue.append((child, new_chain, depth + 1))

    # 子→ candidate のリストに展開 （ 1 件だけ。 浅い経路のみ採用 ）
    result: dict[str, list[_ChainCandidate]] = {}
    for child, chain in visited.items():
        if child == main_object:
            continue
        depth = visited_depth[child]
        result[child] = [_ChainCandidate(chain=chain, object_name=child, depth=depth)]

    # 主オブジェクト自身は含めない
    result.pop(main_object, None)

    return result


def _build_field_index(describe: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Object Describe の ``fields`` を ``{正規化表示名: [field, ...]}`` に組み立てる。

    同じ表示名を持つフィールドが複数ある場合は最初に見つかった 1 件だけを
    選ばず **リストのまま** 残す。 呼び出し側で件数を判定し、 1 件なら
    採用、 2 件以上なら「複数候補あり」として注記する。 名前か表示名が空の
    項目は照合に使えないので入れない。
    """
    index: dict[str, list[dict[str, Any]]] = {}
    for field in describe.get("fields", []):
        name = field.get("name")
        label = field.get("label")
        if not name or not label:
            continue
        index.setdefault(_normalize_label(label), []).append(
            {
                "name": name,
                "label": label,
                "type": field.get("type") or "",
                "referenceTo": field.get("referenceTo") or [],
                "relationshipName": field.get("relationshipName") or "",
            }
        )
    return index


def _filter_only_columns(report_filters: list[dict[str, Any]]) -> list[str]:
    """``reportFilters`` にだけ現れる列キーを返す（SELECT には出ない列）。"""
    return [
        report_filter["column"] for report_filter in report_filters if report_filter.get("column")
    ]


def _grouping_columns(report_metadata: dict[str, Any]) -> list[str]:
    """``groupingsDown`` / ``groupingsAcross`` （ ``SUMMARY`` / ``MATRIX`` ）の列キー。"""
    return [
        grouping["name"]
        for grouping_key in ("groupingsDown", "groupingsAcross")
        for grouping in report_metadata.get(grouping_key, [])
        if grouping.get("name")
    ]


def _aggregate_field_columns(aggregates: list[str]) -> list[str]:
    """``aggregates`` の集計対象列キーを返す。

    集計キーは ``"s!Amount"`` のように「関数!列キー」の形。 ``"!"`` が無い
    もの（ ``RowCount`` など ）は列キーとして扱えないため無視する。
    """
    return [key.split("!", 1)[-1] for key in aggregates if "!" in key]


def _date_filter_column(report_metadata: dict[str, Any]) -> str | None:
    """``standardDateFilter`` の列キーを 1 件だけ返す。 無ければ ``None`` 。

    レポート describe では日付条件の対象列は 1 つだけ。 SOQL 組立時に対応を
    取るために列対応表に含める必要がある。
    """
    date_filter = report_metadata.get("standardDateFilter") or {}
    return date_filter.get("column") or None


def _sort_by_columns(report_metadata: dict[str, Any]) -> list[str]:
    """``sortBy`` の ``sortColumn`` を取り出して順番通りに返す。

    SELECT には出ないが ``ORDER BY`` で使う列も列対応表に含める
    ( ``build_column_map()`` がラベルで解決するため) 。
    """
    return [
        entry["sortColumn"]
        for entry in report_metadata.get("sortBy", [])
        if entry.get("sortColumn")
    ]


def collect_describable_columns(report_metadata: dict[str, Any]) -> list[str]:
    """``build_column_map()`` が解決を試みる列キーの一覧を組み立てる（重複除去済み）。

    出力に出る列（ ``output_column_keys`` ）に加え、 SELECT には出ないが
    ``reportFilters`` だけで使う列、 ``standardDateFilter`` の列、 ``sortBy`` の
    列も含める（ WHERE / ORDER BY で使うため ）。
    """
    columns: list[str] = []
    for column_key in (
        report_metadata.get("detailColumns", [])
        + _filter_only_columns(report_metadata.get("reportFilters", []))
        + _grouping_columns(report_metadata)
        + _aggregate_field_columns(report_metadata.get("aggregates", []))
        + [column for column in [_date_filter_column(report_metadata)] if column]
        + _sort_by_columns(report_metadata)
    ):
        if column_key not in columns:
            columns.append(column_key)
    return columns


def select_column_keys(report_metadata: dict[str, Any]) -> list[str]:
    """SELECT に入れる列のキー（ ``detailColumns`` → グルーピング列 ）。重複は除かない。"""
    return report_metadata.get("detailColumns", []) + _grouping_columns(report_metadata)


def output_column_keys(report_metadata: dict[str, Any]) -> list[str]:
    """レポートの出力に出る列のキー（ SELECT の列 → 集計の対象列 ）。重複は除かない。

    個人情報の判定など「 出力に出る列 」 だけを見るときに使う。 絞り込みにだけ
    使う列は含まない。
    """
    return select_column_keys(report_metadata) + _aggregate_field_columns(
        report_metadata.get("aggregates", [])
    )


def field_name_from_soql(soql_name: str) -> str:
    """``"Account.Owner.Name"`` から末端の ``"Name"`` を取り出す。 ``(不明)`` は空文字。"""
    if not soql_name or soql_name == "(不明)":
        return ""
    return soql_name.rsplit(".", 1)[-1]


def _collect_column_info(metadata: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """``build_column_map()`` の表示名引き当てに使う列情報を組み立てる。

    グルーピング列・集計列の表示名は ``detailColumnInfo`` ではなく
    ``groupingColumnInfo`` / ``aggregateColumnInfo`` 側に入っているため、
    同じ列キー空間としてマージする（キーの重複は無い前提）。
    """
    extended_metadata: dict[str, dict[str, Any]] = metadata.get("reportExtendedMetadata", {})
    column_info: dict[str, dict[str, Any]] = {}
    for info_key in ("detailColumnInfo", "groupingColumnInfo", "aggregateColumnInfo"):
        column_info.update(extended_metadata.get(info_key, {}))
    return column_info


def _prefix_matches(chain_candidate: _ChainCandidate, prefix: str) -> bool:
    """列キーの最初の ``.`` の前の語が、 候補の鎖と一致するか。

    一致条件: 大文字小文字・``_`` を無視して、 次のいずれかと等しい

    - 鎖の最初のリレーション名 （ ``"A.Owner"`` → ``"A"`` ）
    - 鎖の末端オブジェクト名 （ ``"What.Owner"`` → ``"User"`` ）

    末端オブジェクト名は鎖の行き先そのものなので、 鎖が深くなっても
    ``prefix`` が末端オブジェクトを指している場合は一致する。
    """
    if not chain_candidate.chain:
        return False
    parts = chain_candidate.chain.split(".")
    first_rel = parts[0]
    normalized_prefix = _normalize_identifier(prefix)
    if _normalize_identifier(first_rel) == normalized_prefix:
        return True
    if _normalize_identifier(chain_candidate.object_name) == normalized_prefix:
        return True
    return False


def build_column_map(
    metadata: dict[str, Any],
    main_describe: dict[str, Any] | None,
    reason: str | None,
    *,
    related_describes: dict[str, dict[str, Any]] | None = None,
    relations: list[RelationRecord] | None = None,
    main_object: str | None = None,
    fetch_object: Callable[[str], dict[str, Any] | None] | None = None,
) -> list[dict[str, str]]:
    """渡された主オブジェクトの describe から列対応表を作る（純粋関数）。

    関連オブジェクトの describe （ 名前 → describe dict ） と relations を
    渡すと、 関連を跨ぐ表示名も解決する。

    ``main_describe`` が ``None`` のとき、 もしくは ``reason`` が ``None`` でない
    ときは、 全列を ``"(不明)"`` にして備考に ``reason`` を入れる。 一致する
    フィールドが無い列は ``"対応フィールドなし"`` 、 候補が 2 件以上の列は
    誤った候補を押し付けず「複数候補あり」にする。

    解決アルゴリズム （ 列キーごとに ）:

    0. **列キーからたどる** （ 2026-10-14 ）: ``column_key`` が
       ``<主オブジェクト名>.`` で始まる（ 大小文字無視 ） 列は、 列キーの
       残りから 1 段ずつ主オブジェクトの ``fields`` を ``relationshipName``
       → ``name`` の順で突き合わせる。 途中の段は ``relationshipName`` を
       SOQL セグメントにして次の参照先へ進む。 経路上のオブジェクトは
       ``fetch_object`` で取りに行く。 たどれれば SOQL 列名と最後の型を
       採用し、 備考は「 列キーから 」 。 5 段を超える、または途中で項目
        が見つからない・参照先が取れない場合は表示名の照合にフォールバック
    0b. **FK_ からたどる** （ 2026-10-14 ）: ``column_key`` が ``FK_``
       で始まり残りに ``.`` を含む列は、 主オブジェクトの参照項目のうち
       ``referenceTo`` に対象オブジェクト名（ 大小文字無視 ） を含み
       ``relationshipName`` が空でないものを全部集める。 1 件で、 残りを
       既存の ``_resolve_column_key_to_soql`` で解ければ
       ``<relationshipName>.<SOQL 片>`` を採用し 備考は「 FK_ から 」 。
       2 件以上で、 経路が解ける候補があれば 対応は ``"(不明)"`` のまま
       備考に「 複数候補あり: ... （ 最大 5 件 ） 」 を入れ表示名照合には
       進まない。 対象が主に無い / 対象 describe が取れない / 経路が解け
       ないときは失敗理由を ``path_failure_note`` に持って表示名照合に
       フォールバック
    1. 主オブジェクトで、 表示名（ 前後空白除去・小文字 ） が **一意に**
       一致すればそれを採用 （ 今までの挙動 ）
    2. 一致しない列は、 取れた関連オブジェクト（ 段の浅い順 ） の項目
       ラベルとも突き合わせる。 候補の SOQL 名は「 主オブジェクトから
       そのオブジェクトまでのリレーション名の鎖 + ``.`` + 項目 API 名 」
       （ 例: ``Account.Owner.Name`` ）。 鎖は ``relations`` から最も浅い
       1 本を選ぶ
    3. 候補が 1 つなら採用して備考に「 関連オブジェクト経由 」。 複数
       あれば、 列キーの最初の ``.`` の前の語が候補の鎖の最初のリレーション
       名またはオブジェクト名と一致する候補だけに絞り、 1 つになったら採用
       （ 備考「 列キーの接頭辞で絞り込み 」 ）。 それでも複数あれば 対応は
       ``"(不明)"`` のまま、 備考に「複数候補あり: 鎖+項目（ 最大 5 件、
       浅い順、 オブジェクト名 + 表示名も添える ）」。 無ければ
       「 対応フィールドなし 」、 0. で経路の補完に失敗していたときは
       その理由も添える

    列キーの実際の形は本物の組織で未確認。 この実装は推測の範囲で作る。
    結果は **必ず備考を確認** すること。

    Args:
        metadata: ``client.report.describe(report_id)`` の戻り値。
        main_describe: 主オブジェクトの Object Describe。 取れなかった /
            取らなかったときは ``None``。
        reason: 主オブジェクトを特定できなかった / Object Describe が失敗した
            理由。 ``None`` のときは特定・取得が成功している。
        related_describes: 関連オブジェクト名 → Object Describe の dict。
            渡された関連だけが解決対象になる。
        relations: ``RelationRecord`` のリスト。 鎖の計算に使う。
        main_object: 主オブジェクト名。 ``relations`` から鎖を引くために使う。
        fetch_object: 経路の補完 （ 列キーからのたどり ） で経路上の
            オブジェクトを取りに行く関数。 ``None`` のときは ``main_describe``
            だけから既知の経路で解決を試み、 無いものはたどれなくなる。

    Returns:
        列対応表の行のリスト。 各行は ``列キー`` / ``表示名`` /
        ``所属オブジェクト`` / ``対応フィールドAPI名`` / ``型`` /
        ``参照先オブジェクト`` / ``リレーション名`` / ``備考`` のキーを
        持つ dict。
    """
    related_describes = related_describes or {}
    relations = relations or []

    report_metadata = metadata.get("reportMetadata", {})
    columns = collect_describable_columns(report_metadata)
    column_info = _collect_column_info(metadata)
    field_index = _build_field_index(main_describe) if main_describe is not None else None

    # ``main_object`` が未指定なら ``main_describe.get("name")`` から取る
    if main_object is None and main_describe is not None:
        main_object = main_describe.get("name")
    main_object = main_object or ""

    # 鎖を事前計算
    chains_by_object = build_relation_chains(relations, main_object)

    # 関連オブジェクトの鎖を段の浅い順に並べたもの
    ordered_chains: list[_ChainCandidate] = []
    for obj_name in chains_by_object:
        for chain in chains_by_object[obj_name]:
            ordered_chains.append(chain)
    ordered_chains.sort(key=lambda c: (c.depth, c.object_name))

    # 関連オブジェクトの表示名引き当て用インデックス。
    # キー: ``(所属オブジェクト名, 正規化表示名)`` → ``[field, ...]``
    related_indices: dict[str, dict[str, list[dict[str, Any]]]] = {}

    rows: list[dict[str, str]] = []
    # オブジェクトの表示名 → API 名のマップ （ 「 タスク: 作業会社名 」 形式の照合用 ）
    object_label_to_api: dict[str, str] = {}
    if main_describe is not None:
        # 主オブジェクト自身の表示名と、 関連オブジェクトの表示名
        labeled = [(main_object, main_describe), *related_describes.items()]
        for object_name, describe in labeled:
            object_label = describe.get("label")
            if object_label:
                object_label_to_api[_normalize_label(object_label)] = object_name

    for column_key in columns:
        label = column_info.get(column_key, {}).get("label", column_key)
        # 表示名が 「 オブジェクト: 項目名 」 の形なら分解して、 コロン前の
        # オブジェクト表示名に一致するオブジェクト内の項目表示名で照合する
        # （ 2026-10-15 ） 。 コロン前が見つからないときは従来どおり後ろだけで照合
        prefix_object, body_label = _split_object_label(label)
        normalized_label = _normalize_label(body_label if prefix_object else label)
        target_object_hint: str | None = None
        if prefix_object is not None:
            target_object_hint = object_label_to_api.get(_normalize_label(prefix_object))
        row = {
            "列キー": column_key,
            "表示名": label,
            "所属オブジェクト": main_object or "",
            "対応フィールドAPI名": "(不明)",
            "型": "",
            "参照先オブジェクト": "",
            "リレーション名": "",
            "備考": "",
        }

        if main_describe is None or field_index is None or reason is not None:
            row["備考"] = reason or ""
            rows.append(row)
            continue

        # 0. 列キーからたどる （ 2026-10-14 ） 。
        # 主オブジェクトが分かっていて、 列キーが ``主オブジェクト.`` で
        # 始まり ``.`` を含むときだけ試す。 たどれが漏れたら表示名の照合
        # に進むので、 失敗の理由は保持しておく。
        path_failure_note: str | None = None
        if main_object and "." in column_key and _starts_with_main_object(column_key, main_object):
            (
                soql_name,
                field_type,
                failure_note,
                id_column,
            ) = _resolve_column_key_to_soql(
                main_object=main_object,
                column_key=column_key,
                main_describe=main_describe,
                fetch_object=fetch_object,
            )
            if soql_name:
                row["対応フィールドAPI名"] = soql_name
                row["型"] = field_type or ""
                if id_column is not None:
                    # 末端の参照項目を参照先の名前項目に置き換えた。 参照先 /
                    # リレーション名は埋めない （ 末端は名前項目で参照では
                    # ないため ）
                    row["備考"] = f"列キーから（参照先の名前。ID なら {id_column}）"
                else:
                    # 最後の項目の参照先 / リレーション名は表示名引き当てと
                    # 同じく埋める （ 表示の一貫性 ） 。 末端の項目は
                    # ``main_describe`` から同じ name を探して再利用する。
                    last_field = _find_last_field(main_describe, field_name_from_soql(soql_name))
                    if last_field is not None:
                        _fill_reference(row, last_field)
                    row["備考"] = "列キーから"
                rows.append(row)
                continue
            path_failure_note = failure_note

        # 0b. FK_ からたどる （ 2026-10-14 ） 。
        # 列キーが ``FK_`` で始まり残りに ``.`` を含む形
        # （ 例: ``FK_Contact.Email__c`` / ``FK_$Contact.Email__c`` ） は、
        # 主オブジェクトの参照項目から対象オブジェクトのリレーション名を
        # 取り、 残りは既存の ``_resolve_column_key_to_soql`` で解く。
        # 1 件で経路が解ければ採用、 2 件以上で経路が解ける候補があれば
        # 「 複数候補あり: ... 」 の形で備考だけ入れて表示名照合には
        # 進まない、 どちらも無ければ失敗理由を ``path_failure_note`` に
        # 持って表示名照合へフォールバック。
        if main_object and column_key.startswith("FK_"):
            fk_body = column_key[3:]
            if fk_body.startswith("$"):
                fk_body = fk_body[1:]
            if "." in fk_body:
                fk_target, _, fk_remainder = fk_body.partition(".")
                if fk_target and fk_remainder:
                    # main_describe の fields から対象オブジェクトを参照する
                    # 参照項目を全部集める （ ``referenceTo`` は大小無視 ）
                    fk_matches = [
                        mf
                        for mf in main_describe.get("fields", [])
                        if mf.get("type") == "reference"
                        and mf.get("relationshipName")
                        and mf.get("name")
                        and any(r.lower() == fk_target.lower() for r in mf.get("referenceTo") or [])
                    ]

                    if not fk_matches:
                        path_failure_note = (
                            f"FK_ で {fk_target} を参照する項目が {main_object} にありません"
                        )
                    else:
                        # 対象オブジェクトの describe を取る。 ``fetch_object``
                        # が優先、 無ければ ``related_describes`` を探す
                        target_describe = fetch_object(fk_target) if fetch_object else None
                        if target_describe is None:
                            target_describe = related_describes.get(fk_target)

                        if target_describe is None:
                            path_failure_note = f"FK_ で {fk_target} の項目定義を取得できません"
                        else:
                            fk_path_key = f"{fk_target}.{fk_remainder}"
                            (
                                fk_sub_soql,
                                fk_sub_type,
                                fk_sub_failure,
                                fk_sub_id_column,
                            ) = _resolve_column_key_to_soql(
                                main_object=fk_target,
                                column_key=fk_path_key,
                                main_describe=target_describe,
                                fetch_object=fetch_object,
                            )

                            if fk_sub_soql:
                                if len(fk_matches) == 1:
                                    fk_field = fk_matches[0]
                                    fk_rel = fk_field.get("relationshipName") or ""
                                    row["対応フィールドAPI名"] = f"{fk_rel}.{fk_sub_soql}"
                                    row["型"] = fk_sub_type or ""
                                    if fk_sub_id_column is not None:
                                        # 末端の参照項目を参照先の名前項目に
                                        # 置き換えた。 参照先 / リレーション名は
                                        # 埋めない （ 末端は名前項目で参照では
                                        # ないため ）
                                        row["備考"] = (
                                            "FK_ から（参照先の名前。"
                                            f"ID なら {fk_rel}.{fk_sub_id_column}）"
                                        )
                                    else:
                                        # 末端の項目の参照先 / リレーション名は
                                        # 0. と同じく埋める
                                        fk_last = _find_last_field(
                                            target_describe, field_name_from_soql(fk_sub_soql)
                                        )
                                        if fk_last is not None:
                                            _fill_reference(row, fk_last)
                                        row["備考"] = "FK_ から"
                                    rows.append(row)
                                    continue
                                # 2 件以上: 経路が解けたものだけ 「 複数候補あり 」
                                # の形式で備考に並べる
                                resolved = [
                                    (fk_field["relationshipName"], fk_field["name"])
                                    for fk_field in fk_matches
                                ]
                                row["備考"] = "複数候補あり: " + " / ".join(
                                    f"{rel}.{fk_sub_soql}（{field_name}）"
                                    for rel, field_name in resolved[:5]
                                )
                                rows.append(row)
                                continue
                            # 経路が解けない
                            path_failure_note = fk_sub_failure or (
                                f"FK_ で {fk_target} の経路 {fk_remainder} が解けません"
                            )

        # 候補を全部集める （ 主 → 関連、 関連は浅い順 ）
        # 各候補は (所属オブジェクト名, 鎖文字列, field_dict) の組
        all_candidates: list[tuple[str, str, dict[str, Any], bool]] = []
        # bool は "main に由来するか"
        # 「 オブジェクト: 項目名 」 形式なら、 候補を ``target_object_hint`` で
        # 絞り込む （ 2026-10-15 ） 。
        main_matches = field_index.get(normalized_label, [])
        for field in main_matches:
            if target_object_hint is not None and main_object != target_object_hint:
                continue
            all_candidates.append((main_object or "", "", field, True))

        # 関連から
        for chain in ordered_chains:
            related_describe = related_describes.get(chain.object_name)
            if related_describe is None:
                continue
            if target_object_hint is not None and chain.object_name != target_object_hint:
                continue
            if chain.object_name not in related_indices:
                related_indices[chain.object_name] = _build_field_index(related_describe)
            related_index = related_indices[chain.object_name]
            related_matches = related_index.get(normalized_label, [])
            for field in related_matches:
                soql_chain = chain.chain
                all_candidates.append((chain.object_name, soql_chain, field, False))

        if not all_candidates:
            row["備考"] = path_failure_note or "対応フィールドなし"
            rows.append(row)
            continue

        if len(all_candidates) == 1:
            owner, soql_chain, field, from_main = all_candidates[0]
            _apply_candidate(row, owner, soql_chain, field, from_main, "関連オブジェクト経由")
            rows.append(row)
            continue

        # 2 件以上: 列キーの最初の ``.`` の前でフィルタ
        prefix = column_key.split(".", 1)[0]
        filtered: list[tuple[str, str, dict[str, Any], bool, _ChainCandidate | None]] = []
        for owner, soql_chain, field, from_main in all_candidates:
            if from_main:
                # 主由来は鎖なし。 接頭辞フィルタは main_object と比較
                if main_object and _normalize_identifier(prefix) == _normalize_identifier(
                    main_object
                ):
                    filtered.append((owner, soql_chain, field, from_main, None))
                continue
            # 関連由来: 鎖から比較
            chain_obj = next(
                (c for c in ordered_chains if c.object_name == owner and c.chain == soql_chain),
                None,
            )
            if chain_obj is not None and _prefix_matches(chain_obj, prefix):
                filtered.append((owner, soql_chain, field, from_main, chain_obj))

        if len(filtered) == 1:
            owner, soql_chain, field, from_main, _ = filtered[0]
            _apply_candidate(row, owner, soql_chain, field, from_main, "列キーの接頭辞で絞り込み")
            rows.append(row)
            continue

        # それでも複数 （ またはフィルタで 0 件 ）
        if not filtered:
            # フィルタで 0 件なら、 全部 「(不明)」 扱いで複数の候補を並べる
            filtered = [
                (owner, soql_chain, field, from_main, chain_obj)
                for owner, soql_chain, field, from_main in all_candidates
                for chain_obj in (
                    [None]
                    if from_main
                    else [
                        c
                        for c in ordered_chains
                        if c.object_name == owner and c.chain == soql_chain
                    ]
                )
            ]
        # 候補を最大 5 件、 浅い順 （ 主→関連の順 ） で並べる
        sorted_filtered = sorted(
            filtered,
            key=lambda t: (0 if t[3] else 1, t[0], t[1]),
        )[:5]
        row["備考"] = "複数候補あり: " + " / ".join(
            _format_multiple_candidate(owner, soql_chain, field, from_main)
            for owner, soql_chain, field, from_main, _ in sorted_filtered
        )
        rows.append(row)
    return rows


def _fill_reference(row: dict[str, str], field: dict[str, Any]) -> None:
    """``row`` に項目の参照先（ ``|`` 区切り ）とリレーション名を書く。"""
    row["参照先オブジェクト"] = "|".join(field.get("referenceTo") or [])
    row["リレーション名"] = field.get("relationshipName") or ""


def _apply_candidate(
    row: dict[str, str],
    owner: str,
    soql_chain: str,
    field: dict[str, Any],
    from_main: bool,
    related_note: str,
) -> None:
    """1 つに決まった候補を ``row`` に書く。 関連由来なら鎖つきの SOQL 名と ``related_note``。"""
    row["所属オブジェクト"] = owner
    if from_main or not soql_chain:
        row["対応フィールドAPI名"] = field["name"]
    else:
        row["対応フィールドAPI名"] = f"{soql_chain}.{field['name']}"
    row["型"] = field["type"]
    _fill_reference(row, field)
    row["備考"] = "" if from_main else related_note


def _format_multiple_candidate(
    owner: str, soql_chain: str, field: dict[str, Any], from_main: bool
) -> str:
    """複数候補ありの 1 件分の文字列を作る。

    主オブジェクトの項目は ``Name（Opportunity 商談名）`` のように表示名と
    オブジェクト名だけで添え、 関連経由の項目は ``Account.Name（Account
    取引先名）`` のように鎖つきの SOQL セグメントに続けてオブジェクト名 +
    表示名を添える。 SOQL 列名として ``Account.Name`` のようにそのまま
    貼れる形にする。
    """
    name = field.get("name", "")
    soql_segment = f"{soql_chain}.{name}" if soql_chain and not from_main else name
    return f"{soql_segment}（{owner} {field.get('label', '')}）"


def _find_last_field(main_describe: dict[str, Any], last_field_name: str) -> dict[str, Any] | None:
    """``main_describe`` の ``fields`` から ``name == last_field_name`` の項目を返す。

    列キー経路の最後の項目は多くの場合 **主オブジェクトの** フィールド
    （ ``Contact.Name`` / ``Contact.Account.Name`` など ）。 主オブジェクト
    に同名の項目があればそれを、 無ければ ``None`` を返す（ 参照先 /
    リレーション名 は主オブジェクト上の項目に対してだけ埋める ） 。
    """
    return next(
        (
            field
            for field in main_describe.get("fields", [])
            if field.get("name") == last_field_name
        ),
        None,
    )


def column_source(row: dict[str, str]) -> str:
    """列の対応の 1 行が、 どこから来た値か（ 自動 / 人が直した / 取れず ）。

    人が直した値は ``column_fix_xlsx.apply_overrides`` が備考に印を残す。
    xlsx の列の対応シートと AI 用 Markdown が同じ判定を使う。
    """
    note = row["備考"]
    soql = row["対応フィールドAPI名"]
    if "人が直した（要確認: たどれない）" in note:
        return "人が直した（要確認: たどれない）"
    if "直したSOQL列名" in note:
        return "人が直した"
    if soql and soql != "(不明)":
        return "自動"
    return "取れず"


def column_status(row: dict[str, str]) -> str:
    """列の対応の 1 行の状態（ OK / 要対応 / 要確認 ）。

    取れず → 要対応（ 候補が複数あるときは 要確認 ）、 人が直した値が項目定義で
    たどれない → 要確認、 それ以外 → OK。
    """
    soql = row["対応フィールドAPI名"]
    note = row["備考"]
    if not soql or soql == "(不明)":
        return "要確認" if "複数候補あり" in note else "要対応"
    if "人が直した（要確認: たどれない）" in note:
        return "要確認"
    return "OK"


def column_candidates(row: dict[str, str]) -> str:
    """備考の 「 複数候補あり: … 」 から候補の部分だけを返す。 無ければ空文字。"""
    note = row["備考"]
    return note.replace("複数候補あり: ", "") if "複数候補あり" in note else ""
