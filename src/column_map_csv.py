"""人が目で見比べて ``対応フィールドAPI名`` を直したり書き足したりするための
``列の対応.csv`` を読み、 マージし、 書く。

自動の列対応 (``src.mapping.build_column_map``) では対応が取れない列が
多く、 自動の対応が間違っていることもある。 そこで **全レポート分の列を 1 つ
の CSV に並べ、 人が Excel で開いて直せる** ようにする。 ルール:

- 手で入れるのは **自動で取れなかった（ ``対応フィールドAPI名`` が ``(不明)``
  か空の ） 列だけ** 。 自動で取れた列は CSV の値を信用しず、 自動の
  結果を優先する （ 人が入れた値は自動の結果で置き換えられ、 WARNING が出る ）
- 自動で取れなかった列は、 CSV の ``SOQL列名`` があれば使い、 ``型`` は
  項目定義から引く。 引けなければ 「 打ち間違い？ 」 WARNING だけ出し値は
  そのまま使う
- 表示名は毎回、 レポートの表示名で自動になる
- ファイルが無ければ新規作成。 既存ファイルが見出し違いなら ``CSVError``
- 書き込みが ``PermissionError`` （ Excel で開いている等 ） なら実行は止めず
  WARNING だけ出す。 他の出力はそのまま作る
- 主オブジェクト名は大小文字を区別しない。 並びは 主オブジェクト → 列キー
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from comken.core.table import Table
from comken.exceptions import CSVError
from comken.toolbox.csv import CSV

logger = logging.getLogger(__name__)


# CSV の列名 （ この順 ）
COLUMN_MAP_CSV_COLUMNS: tuple[str, ...] = (
    "主オブジェクト",
    "列キー",
    "表示名",
    "SOQL列名",
    "メモ",
    "使っているレポート数",
    "更新日",
)


# ``apply_overrides`` が 「 CSV の手で入れた値を当てはめた 」 列の ``備考``
# に付ける印。 ``_merge_and_write_after_run`` がこの印を見て、 その列を
# 「 自動で取れなかった 」 列として ``merge_and_write`` に渡す。 これにより
# ``merge_and_write`` 側の既存ルール （ 自動で取れず CSV に手入力が入って
# いる行のメモを 「 手入力 」 に統一する ） がそのまま機能する。 値としては
# 残るので上書きしない
OVERRIDE_MARKER: str = "列の対応.csv の値"


# ── 行型 ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ColumnMapRow:
    """``列の対応.csv`` の 1 行分。 人が編集するセル (``SOQL列名`` /
    ``表示名`` / ``メモ``) と自動が毎回更新するセル (``使っているレポート数`` /
    ``更新日``) を同じ dataclass で持つ。

    空欄かどうかは ``str == ""`` で判定する （ CSV は文字列なので ）。
    """

    main_object: str
    column_key: str
    display_name: str
    soql_name: str
    note: str
    report_count: int
    updated_at: str  # YYYY-MM-DD

    @classmethod
    def from_dict(cls, row: dict[str, str]) -> ColumnMapRow:
        return cls(
            main_object=_coerce_str(row.get("主オブジェクト")),
            column_key=_coerce_str(row.get("列キー")),
            display_name=_coerce_str(row.get("表示名")),
            soql_name=_coerce_str(row.get("SOQL列名")),
            note=_coerce_str(row.get("メモ")),
            report_count=_parse_int(_coerce_str(row.get("使っているレポート数"))),
            updated_at=_coerce_str(row.get("更新日")),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "主オブジェクト": self.main_object,
            "列キー": self.column_key,
            "表示名": self.display_name,
            "SOQL列名": self.soql_name,
            "メモ": self.note,
            "使っているレポート数": str(self.report_count) if self.report_count else "",
            "更新日": self.updated_at,
        }


@dataclass(frozen=True)
class ObservedColumnRow:
    """1 レポート分の ``column_map`` の 1 行を、 ``列の対応.csv`` と同じ
    軸 (``主オブジェクト`` 、 ``列キー``) で表した中間形式。

    ``building.build_column_map`` の戻り値 （ ``対応フィールドAPI名`` 等 ） を
    ``column_map_csv`` が扱いやすい形に直すため、 ``mapping`` を直接読まない
    ようにしている （ ``src.column_map_csv`` が ``src.mapping`` に依存しない
    ほうが、 テストでも組み立てやすい ）。
    """

    main_object: str  # 表示名そのまま。 比較は呼び出し側で大小文字無視
    column_key: str
    display_name: str
    soql_name: str  # 自動で取れた値。 空のこともある
    note: str  # 自動で取れなかった理由。 取れたときは空


# ── 読み込み ────────────────────────────────────────────────────────────────


def load_column_map(path: Path) -> dict[tuple[str, str], ColumnMapRow]:
    """CSV を読み ``{(主オブジェクト小文字, 列キー): ColumnMapRow}`` を返す。

    - ファイルが存在しないときは空 dict を返す （ 新規扱い ）
    - 見出しが ``COLUMN_MAP_CSV_COLUMNS`` と違うときは ``CSVError`` を上げる。
      ファイルは変更しない
    - 主オブジェクトが空の行・ 列キーが空の行は読み飛ばす
    """
    if not path.exists():
        return {}
    try:
        with CSV(path) as csv:
            table = csv.read()
    except CSVError:
        raise
    if list(table.columns) != list(COLUMN_MAP_CSV_COLUMNS):
        raise CSVError(
            f"既存の {path.name} の見出しが想定と違います: {path}\n"
            f"期待: {list(COLUMN_MAP_CSV_COLUMNS)}\n"
            f"実際: {list(table.columns)}\n"
            "対処: ファイルを別名に退避してから再実行してください。"
        )
    result: dict[tuple[str, str], ColumnMapRow] = {}
    for raw in table.to_rows():
        if not isinstance(raw, dict):
            continue
        row = ColumnMapRow.from_dict(raw)
        if not row.main_object or not row.column_key:
            continue
        result[_normalize_key(row.main_object, row.column_key)] = row
    return result


# ── column_map への上書き適用 ──────────────────────────────────────────


def apply_overrides(
    column_map_rows: list[dict[str, str]],
    existing: dict[tuple[str, str], ColumnMapRow],
    main_object: str,
    objects: dict[str, dict[str, Any]],
    warnings: list[str],
) -> list[dict[str, str]]:
    """``column_map`` の各行に ``列の対応.csv`` の上書きを適用する。

    ルール:
    - ``対応フィールドAPI名`` が空でも ``(不明)`` でもない （ 自動が解決
      できた ） 行は、 **CSV を見ずに自動の結果をそのまま使う** 。 CSV の
      値は信用しない
    - ``対応フィールドAPI名`` が空か ``(不明)`` （ 自動が取れなかった ）
      行に限り、 CSV に ``(主オブジェクト小文字, 列キー)`` が有り かつ
      ``SOQL列名`` が空でないとき → CSV の値で ``対応フィールドAPI名`` を
      上書きし、 ``型`` は ``objects`` から ``Account.Name`` のようなドット
      記法をたどって引く
    - 自動が解決できたのに ``objects`` から型が引けないときは WARNING を
      出す （ CSV は見ていないので 「 打ち間違い？ 」 の警告は出さない ）
    - 自動が解決できず CSV の値で上書きしたが型が引けないときは 「 打ち
      間違い？ 」 WARNING を出す。 値はそのまま使う

    鍵は **``column_map`` 行の ``所属オブジェクト`` ではなく** ``main_object``
    （ レポートの主オブジェクト ） を使う。 同じ ``Opp.Account.Name`` 列でも、
    ``所属オブジェクト`` は ``Account`` （ 自動解決先 ） と ``Opportunity``
    （ 主由来 ） で値が変わる。 一方で SOQL 列名 ``Account.Name`` は主
    オブジェクト ``Opportunity`` から見たパスなので、 鍵は ``Opportunity``
    でないと人手の値が拾えない・行が二重になる。

    Args:
        column_map_rows: ``build_column_map`` の戻り値。 1 行が dict の list。
            変更した dict のコピーを返すが、 元の list は触らない。
        existing: ``load_column_map()`` の戻り値。 空 dict でも可。
        main_object: レポートの主オブジェクト名。 鍵と ``_resolve_soql_type``
            の起点に使う。 ``column_map`` 行の ``所属オブジェクト`` は使わない。
        objects: ``record.objects`` 互換 （ ``name`` / ``fields`` を持つ dict
            の dict ）。 CSV には無い型引き当てに使う。 キーはオブジェクト名。
        warnings: 呼び出し側の warnings list。 そのまま追加する。

    Returns:
        新しい list （ 各 dict はコピー ）。 呼び出し側はこれを ``column_map``
        として使う。
    """
    new_rows: list[dict[str, str]] = []
    for row in column_map_rows:
        if not isinstance(row, dict):
            new_rows.append(dict(row))
            continue
        column_key = _coerce_str(row.get("列キー"))
        auto_api_name = _coerce_str(row.get("対応フィールドAPI名"))
        if not main_object or not column_key:
            new_rows.append(dict(row))
            continue
        # 自動で取れた列は CSV を見ない
        if _is_auto_resolved(auto_api_name):
            new_rows.append(dict(row))
            continue
        # 自動で取れなかった列だけ CSV を当てる
        override = existing.get(_normalize_key(main_object, column_key))
        if override is None or not override.soql_name:
            new_rows.append(dict(row))
            continue
        new_row = dict(row)
        new_row["対応フィールドAPI名"] = override.soql_name
        # 型を describe から引く。 引けなければ空のまま（ 列の対応.csv の値を
        # 消すのは禁止。 無い型情報は付けない ）
        resolved_type = _resolve_soql_type(
            main_object=main_object,
            soql_name=override.soql_name,
            objects=objects,
        )
        if resolved_type:
            new_row["型"] = resolved_type
        else:
            warnings.append(
                "列の対応.csv の SOQL列名が項目定義に見つかりません"
                f"（打ち間違い？）: {main_object} / {column_key} / {override.soql_name}"
            )
        new_row["備考"] = OVERRIDE_MARKER
        new_rows.append(new_row)
    return new_rows


def _is_auto_resolved(auto_api_name: str) -> bool:
    """``対応フィールドAPI名`` が 「 自動で取れた 」 値かどうかを返す。

    空文字と ``(不明)`` は 「 自動で取れなかった 」 とみなす。 それ以外の
    値は 「 自動で取れた 」 とする。 自動が取れた行は ``apply_overrides``
    でも ``merge_and_write`` でも CSV を信用しない。
    """
    return bool(auto_api_name) and auto_api_name != "(不明)"


# ── 書き出し ────────────────────────────────────────────────────────────


def merge_and_write(
    path: Path,
    existing: dict[tuple[str, str], ColumnMapRow],
    observed: list[ObservedColumnRow],
    today: date,
    *,
    writer: Callable[[Path, list[ColumnMapRow]], None] | None = None,
) -> None:
    """``observed`` の情報を ``existing`` にマージし、 CSV を書き出す。

    ルール:
    - ``observed`` に無いキー → 触らない
    - ``observed`` に有るキーで ``observed.soql_name`` が空でも ``(不明)``
      でもない （ 自動で取れた ） 行 → ``SOQL列名`` を ``observed.soql_name``
      で **必ず書き換える** 。 既に値が入っていて （ 人が入れた値でも ）
      自動の結果と違うときは WARNING を出して置き換えられたことを知らせる。
      ``メモ`` は空にする
    - ``observed`` に有るキーで自動が取れなかった行 → ``SOQL列名`` は
      既存の値をそのまま残す。 ``メモ`` は、 既存 ``SOQL列名`` が空なら今回
      の取れなかった理由、 既に手入力で埋まっていれば 「 手入力 」 にする
    - ``表示名`` は毎回 ``observed.display_name`` で書き換える
    - ``使っているレポート数`` と ``更新日`` は今回用いた行で毎回書き換える
    - 並びは 主オブジェクト → 列キー
    - 書き込みが ``PermissionError`` なら実行は止めず WARNING だけ出す。
      ファイルの中身は触らない （ ``atomic_write`` 系の挙動と同じく ）

    ``writer`` はテスト用に差し替えられる。 ``None`` のとき ``_write_column_map_csv``
    を使う。 ``PermissionError`` を投げる ``writer`` を入れれば WARNING 経路を
    検証できる。

    見出し違いの既存ファイルは ``existing`` が既に空 dict で来ているので、
    ここで ``CSVError`` を上げる。 ``_write_column_map_csv`` で同じチェック
    をする。
    """
    new_rows: dict[tuple[str, str], ColumnMapRow] = {}
    report_counts: dict[tuple[str, str], int] = {}
    iso_date = today.isoformat()

    observed_keys: set[tuple[str, str]] = set()
    for obs in observed:
        if not obs.main_object or not obs.column_key:
            continue
        key = _normalize_key(obs.main_object, obs.column_key)
        observed_keys.add(key)
        report_counts[key] = report_counts.get(key, 0) + 1
        # observed の内容を 「 今回の値で埋める候補 」 として覚える
        new_rows[key] = ColumnMapRow(
            main_object=obs.main_object,
            column_key=obs.column_key,
            display_name=obs.display_name,
            soql_name=obs.soql_name,
            note=obs.note,
            report_count=0,
            updated_at=iso_date,
        )

    # 全行を組み立て直す: 既存 + 新規/上書き分
    merged: dict[tuple[str, str], ColumnMapRow] = dict(existing)
    for key, row in new_rows.items():
        existing_row = existing.get(key)
        auto_resolved = _is_auto_resolved(row.soql_name)
        # 自動で取れた列は自動の結果で必ず上書きする
        if auto_resolved:
            soql_name = row.soql_name
            note = ""
            previous = existing_row.soql_name if existing_row else ""
            if previous and previous != soql_name:
                # 人が入れた値を自動の結果で置き換えた
                logger.warning(
                    "列の対応.csv の手入力を自動の結果で置き換えました: "
                    "%s / %s / %s → %s",
                    row.main_object,
                    row.column_key,
                    previous,
                    soql_name,
                )
        else:
            # 自動で取れなかった列は CSV の値を壊さない
            soql_name = existing_row.soql_name if existing_row else ""
            if soql_name:
                note = "手入力"
            else:
                note = row.note
        # 表示名は毎回、 レポートの表示名で上書きする
        display_name = row.display_name
        merged[key] = ColumnMapRow(
            main_object=row.main_object,
            column_key=row.column_key,
            display_name=display_name,
            soql_name=soql_name,
            note=note,
            report_count=report_counts.get(key, 0),
            updated_at=iso_date,
        )

    # 並びは 主オブジェクト → 列キー
    sorted_rows = sorted(
        merged.values(),
        key=lambda r: (r.main_object.lower(), r.column_key),
    )
    write_csv = writer if writer is not None else _write_column_map_csv
    try:
        write_csv(path, sorted_rows)
    except PermissionError as exc:
        logger.warning(
            "列の対応.csv を書けませんでした（ Excel などで開いていないか確認 ）: %s",
            exc,
        )


# ── 内部ヘルパー ────────────────────────────────────────────────────────


def _write_column_map_csv(path: Path, rows: list[ColumnMapRow]) -> None:
    """CSV を書く。 ``PermissionError`` は呼び出し側 (``merge_and_write``) で
    WARNING に縮退される。 comken の ``CSV._write`` は途中で失敗したら
    ``.part`` 一時ファイルが片付けられ、 既存の同名ファイルは無傷で残る。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [row.to_dict() for row in rows]
    with CSV(path, columns=list(COLUMN_MAP_CSV_COLUMNS)) as csv:
        csv.replace(Table(list(COLUMN_MAP_CSV_COLUMNS), payload))


def _resolve_soql_type(
    *,
    main_object: str,
    soql_name: str,
    objects: dict[str, dict[str, Any]],
) -> str:
    """``soql_name`` （ ``Amount`` や ``Account.Name`` のようなドット記法 ） を、
    ``main_object`` の describe から順にたどって最後の項目の ``型`` を返す。

    ドット記法は関連項目。 中間の ``Account`` 段は次の優先順で探す:

    1. ``relationshipName`` が ``part`` と一致する項目
    2. ``name`` が ``part`` と一致する項目
    3. ``referenceTo`` の最初の要素が ``objects`` のキーと一致するならそれ

    合わなければ空文字を返す。
    """
    if not soql_name or not main_object:
        return ""
    parts = soql_name.split(".")
    current_object = main_object
    for index, part in enumerate(parts):
        describe = objects.get(current_object)
        if not isinstance(describe, dict):
            return ""
        fields = describe.get("fields")
        if not isinstance(fields, list):
            return ""
        matched: dict[str, Any] | None = None
        # 優先順: relationshipName → name
        for field in fields:
            if not isinstance(field, dict):
                continue
            relationship = field.get("relationshipName")
            if isinstance(relationship, str) and relationship == part:
                matched = field
                break
        if matched is None:
            for field in fields:
                if not isinstance(field, dict):
                    continue
                name = field.get("name")
                if isinstance(name, str) and name == part:
                    matched = field
                    break
        if matched is None:
            return ""
        if index == len(parts) - 1:
            field_type = matched.get("type")
            return field_type if isinstance(field_type, str) else ""
        # 次のオブジェクト名を決める。 参照項目の ``relationshipName`` が
        # ``objects`` のキーと一致するならそれ。 合わなければ ``referenceTo``
        # の最初の要素
        reference_to = matched.get("referenceTo")
        next_object = ""
        relationship = matched.get("relationshipName")
        if isinstance(relationship, str) and relationship in objects:
            next_object = relationship
        elif isinstance(reference_to, list) and reference_to:
            first = reference_to[0]
            if isinstance(first, str):
                next_object = first
        if not next_object or next_object not in objects:
            return ""
        current_object = next_object
    return ""


def _normalize_key(main_object: str, column_key: str) -> tuple[str, str]:
    """比較用のキー。 主オブジェクトの大小文字無視。"""
    return (main_object.strip().lower(), column_key)


def _parse_int(value: str) -> int:
    """``str`` を ``int`` にする。 変換失敗時は ``0`` を返す。"""
    if not value:
        return 0
    try:
        return int(value)
    except ValueError:
        return 0


def _coerce_str(value: object) -> str:
    """``None`` を空文字に、 他は ``str`` にする。"""
    return "" if value is None else str(value)


def _merge_and_write_after_run(
    *,
    path: Path,
    existing: dict[tuple[str, str], ColumnMapRow],
    records: list[Any],
    today: date,
) -> None:
    """``run`` の最後に ``列の対応.csv`` をマージして書く薄いラッパー。

    ``records`` の ``column_map`` を ``ObservedColumnRow`` に直し、
    ``merge_and_write`` に渡す。 ``record.column_map`` が無い ``record``
    （ 失敗した ID など ） は無視する。
    """
    observed: list[ObservedColumnRow] = []
    for record in records:
        column_map_rows = getattr(record, "column_map", None)
        if not isinstance(column_map_rows, list):
            continue
        main_object = getattr(record, "main_object", None)
        main_object_text = main_object if isinstance(main_object, str) and main_object else ""
        if not main_object_text:
            # 主オブジェクト不明な record はスキップ（ 鍵が作れない ）
            continue
        for row in column_map_rows:
            if not isinstance(row, dict):
                continue
            column_key = _coerce_str(row.get("列キー"))
            if not column_key:
                continue
            # 鍵は **常に** ``record.main_object`` 。 ``row.get("所属オブジェクト")``
            # は使わない: 自動解決で ``Account`` 等に振られていることがあり、
            # CSV の手入力行と別行になる・二重化の元になる
            #
            # ``備考`` に ``OVERRIDE_MARKER`` が付いていれば ``apply_overrides``
            # が CSV の手で入れた値を当てはめた列。 ``対応フィールドAPI名`` は
            # 既に上書き済みで 「 自動で取れた 」 ように見えるが、 元は自動が
            # 取れなかった列。 ここで ``soql_name`` を空に戻しておかないと、
            # ``merge_and_write`` が 「 自動で取れた 」 と誤判定してメモを空に
            # 戻してしまう。 空に戻せば ``merge_and_write`` 側の 「 自動で取れ
            # なかった列で既存 ``SOQL列名`` が入っていればメモ=手入力 」 の
            # 既存ルールで 「 手入力 」 に統一される
            note_text = _coerce_str(row.get("備考"))
            if note_text == OVERRIDE_MARKER:
                soql_name_text = ""
                note_text = ""
            else:
                soql_name_text = _coerce_str(row.get("対応フィールドAPI名"))
            observed.append(
                ObservedColumnRow(
                    main_object=main_object_text,
                    column_key=column_key,
                    display_name=_coerce_str(row.get("表示名")),
                    soql_name=soql_name_text,
                    note=note_text,
                )
            )
    merge_and_write(
        path=path,
        existing=existing,
        observed=observed,
        today=today,
    )


def today_local() -> date:
    """「 今日の日付 」 をこの PC のローカル時間で返す。

    ``datetime.date.today()`` でも同じ結果だが、 ``datetime.now(UTC).astimezone()``
    経由にして ``_today_local`` （ ``tables.py`` ） と挙動を揃える。
    """
    return datetime.now(tz=UTC).astimezone().date()
