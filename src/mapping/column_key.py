"""列キーから SOQL 列名を取りに行く手順 （``Contact.Account.Name`` など） 。

``main_object`` から 1 段ずつ ``fields`` を ``relationshipName`` → ``name`` の順で
突き合わせ、 経路上の参照先を ``fetch_object`` で補完しながら SOQL 列名を
組み立てる。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

# 列キーから SOQL 列名を取りに行くときのパスの最大段数。 6 段以上なら
# 経路が長すぎると判断して表示名引き当てにフォールバックする
COLUMN_KEY_PATH_MAX_DEPTH: int = 5


def starts_with_main_object(column_key: str, main_object: str) -> bool:
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

    レポートの列表示は参照項目の名前項目 （ Account なら ``Name``、 Case なら
    ``CaseNumber`` ） と同じなので、 SOQL も ID ではなく名前項目で取らないと
    表示値と合わない。 決め方:

    - ``referenceTo`` がちょうど 1 つのときだけ、 参照先の describe を
      ``objects`` （既に ``_resolve_column_key_to_soql`` 内で集めたもの）
      → ``fetch_object`` の順で探し、 ``nameField`` が True の項目を採る
    - ``referenceTo`` が 2 つ以上 （ポリモーフィック） ・ describe が取れない
      ・ ``nameField`` 項目が無い、 のいずれかは ``("Name", "string")`` に
      フォールバック
    - ``fetch_object`` が 401 / 403 を投げたら **ここでは握りつぶさず**
      上位へ送出する （既存と同じ）

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


def resolve_column_key_to_soql(
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
    （ 「列キーで『 X 』が Y に見つかりません （リレーション名の候補: …）」 ）。
    呼び出し側で表示名の照合が決まったときはこの note は **使わない**
    （「決まったほうを優先」 のため） 。

    ``id_column`` は **末端の項目が参照項目でレポート表示と同じ名前項目に
    置き換えたとき** の元の ID 列の SOQL 名。 置き換えなかったとき / 失敗
    したときは ``None`` 。 例:

    - ``Task__c.Custom_Name__c`` （末端が参照） → soql_name
      ``Custom_Name__r.Name``、 id_column ``Custom_Name__c``
    - ``Contact.Account.Name`` （末端が ``Name``） → soql_name
      ``Account.Name``、 id_column ``None`` （置換しない）

    各段の項目を探す順は ``_walk_soql_path()`` と同じ:

    1. ``relationshipName`` が一致する参照項目
    2. ``name`` が一致する項目

    途中の段がマッチしたとき （最後の段以外） は、 **SOQL 上は** その項目
    の ``relationshipName`` （例: ``AccountId`` 参照 → ``Account``） に
    置き換える。 最後の段は項目の ``name`` （ ``Account.Owner.Name`` の
    末端 ``Name`` ） が基本だが、 **末端が ``type == "reference"`` かつ
    ``relationshipName`` が空でない参照項目のときは、 参照先の名前項目に
    置き換える** （レポート表示が名前項目で、 ID 列では値が違うため） 。

    経路上のオブジェクトが ``main_describe`` 起点の ``objects`` に無い
    ときは ``fetch_object`` で取りに行く。 取得は ``fetch_object`` の内部で
    既存のキャッシュ → API の順で行われる前提。 401 / 403 は
    ``fetch_object`` からそのまま上位へ送出される （ ``_make_overrides_fetcher``
    と同じ挙動 ） 。
    """
    if not starts_with_main_object(column_key, main_object):
        return None, None, None, None
    parts = _split_column_key_segments(main_object, column_key)
    if not parts:
        return None, None, None, None
    if len(parts) > max_depth:
        return (
            None,
            None,
            (f"列キーで {len(parts)} 段の経路は長すぎます（{max_depth} 段まで）"),
            None,
        )
    return _walk_soql_path(main_object, parts, main_describe, fetch_object)


def _split_column_key_segments(main_object: str, column_key: str) -> list[str]:
    """``column_key`` から ``main_object.`` 以降の経路セグメントを取り出す。

    空の経路 （``Contact.`` など） は空リストを返し、 呼び出し側で失敗扱い
    にしてもらう。 ``main_object`` の長さ + 1 は ``.`` の分を引いた残り。
    """
    suffix = column_key[len(main_object) + 1 :]
    parts = suffix.split(".")
    if not parts or any(not p for p in parts):
        return []
    return parts


def _walk_soql_path(
    main_object: str,
    parts: list[str],
    main_describe: dict[str, Any],
    fetch_object: Callable[[str], dict[str, Any] | None] | None,
) -> tuple[str | None, str | None, str | None, str | None]:
    """1 段ずつ参照経路を辿って SOQL 列名を組み立てる。

    最終段は ``_finalize_soql_segment()`` に、 中間段は
    ``_advance_soql_segment()`` に委譲し、 ここではループの枠組だけに専念する。
    """
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
        matched = _match_segment_field(describe.get("fields", []), part)
        if matched is None:
            return _segment_not_found_result(part, current_object, describe)

        if index == len(parts) - 1:
            return _finalize_soql_segment(matched, soql_segments, objects, fetch_object)

        advanced = _advance_soql_segment(
            matched, part, current_object, soql_segments, objects, fetch_object
        )
        if advanced is None:
            return (
                None,
                None,
                (
                    f"列キーで「{part}」は {current_object} の"
                    "参照項目ではないため経路を続けられません"
                ),
                None,
            )
        current_object = advanced

    return None, None, None, None


def _match_segment_field(
    fields: list[dict[str, Any]],
    part: str,
) -> dict[str, Any] | None:
    """経路の 1 段に当たるフィールドを ``relationshipName`` → ``name`` の順で探す。"""
    matched = next(
        (field for field in fields if field.get("relationshipName") == part),
        None,
    )
    if matched is not None:
        return matched
    return next(
        (field for field in fields if field.get("name") == part),
        None,
    )


def _segment_not_found_result(
    part: str,
    current_object: str,
    describe: dict[str, Any],
) -> tuple[str | None, str | None, str | None, str | None]:
    """対応するフィールドが見つからなかったときの失敗結果を作る。"""
    candidates = candidate_relationship_names(describe.get("fields", []))
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


def _finalize_soql_segment(
    matched: dict[str, Any],
    soql_segments: list[str],
    objects: dict[str, dict[str, Any]],
    fetch_object: Callable[[str], dict[str, Any] | None] | None,
) -> tuple[str | None, str | None, str | None, str | None]:
    """最終段の解決と、 必要なら参照先の名前項目への置換を行う。

    末端が ``type == "reference"`` かつ ``relationshipName`` が空でない
    参照項目のときは、 レポート表示と同じになるよう参照先の名前項目に
    置き換える。 そうでないときは項目の ``name`` をそのまま SOQL 末端にする。
    """
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
        # 元の ID 列の SOQL 名を保持 （備考で ID 列の代替手段を示す）
        id_column = ".".join([*soql_segments, field_name])
        soql_segments.append(relationship)
        soql_segments.append(name_field_name)
        return ".".join(soql_segments), name_field_type, None, id_column
    soql_segments.append(field_name)
    return ".".join(soql_segments), field_type, None, None


def _advance_soql_segment(
    matched: dict[str, Any],
    part: str,
    current_object: str,
    soql_segments: list[str],
    objects: dict[str, dict[str, Any]],
    fetch_object: Callable[[str], dict[str, Any] | None] | None,
) -> str | None:
    """中間段で ``soql_segments`` を進め、 次の ``current_object`` を返す。

    単一参照は ``referenceTo`` の先頭、 ``objects`` にある候補があれば
    それを優先する。 経路上のオブジェクトが無いときは ``fetch_object`` で
    取りに行く。 次のオブジェクトが決まらないときは ``None`` を返し、
    ``_walk_soql_path()`` が失敗結果を作る。
    """
    relationship = matched.get("relationshipName")
    if not relationship:
        return None
    soql_segments.append(relationship)

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
        return None
    _ = part
    _ = current_object
    return next_object
