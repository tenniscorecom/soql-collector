"""OUTPUT_DIR の ``{管理番号}.json`` を読み、SOQL 組み立て支援の CSV を作る。

AI にチャットで SOQL を組み立ててもらうための小さな表を、``fetch`` が書いた
JSON と ``OUTPUT_DIR/objects/{オブジェクト名}.json`` から組み立てる。
**入力は OUTPUT_DIR の JSON だけ**で、Salesforce には接続しない。
出力も同じ ``OUTPUT_DIR``。

書き出す CSV:

- ``対応表_{管理番号}.csv`` … 1 ID 分のレポート1列（ID ごと）
- ``対応表.csv`` … ``OUTPUT_DIR`` の全 JSON を連結（管理番号昇順）
- ``項目表.csv`` … 全 JSON と ``OUTPUT_DIR/objects/*.json`` を集めた
  「オブジェクト×項目」1 枚。 同じ名前がどちらにもあれば ``取得日時`` が
  新しい方が勝つ

すべて UTF-8 BOM つき + CRLF。一時ファイル経由で書く（``atomic_write``）。
comken の ``CSV`` クラスで書き出す（``comken.toolbox.csv.CSV``）。
"""

from __future__ import annotations

import csv
import json
import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from comken.core.files import atomic_write

logger = logging.getLogger(__name__)

# CSV の文字コード（UTF-8 BOM つきで Excel でも開ける）
CSV_ENCODING = "utf-8-sig"

# 1 つの CSV ファイルあたりの選択肢の最大値数。超えたら末尾に ``…`` を付ける
PICKLIST_LIMIT = 30

# ``[OBJECTS] NAMES`` 由来のオブジェクト JSON を入れるサブフォルダ
OBJECT_SUBDIR = "objects"


# 対応表の列名
CORRESPONDENCE_COLUMNS: tuple[str, ...] = (
    "管理番号",
    "概要",
    "レポートID",
    "レポートタイプ",
    "主オブジェクト",
    "列キー",
    "表示名",
    "対応フィールドAPI名",
    "型",
    "参照先オブジェクト",
    "リレーション名",
    "備考",
)

# 項目表の列名
FIELD_TABLE_COLUMNS: tuple[str, ...] = (
    "オブジェクト",
    "段",
    "オブジェクト表示名",
    "項目API名",
    "項目表示名",
    "型",
    "参照先オブジェクト",
    "リレーション名",
    "カスタム",
    "選択肢",
)


@dataclass(frozen=True)
class TableOutcome:
    """tables 実行の結果。"""

    wrote: tuple[Path, ...]
    skipped: tuple[Path, ...]


def run_tables(
    output_dir: Path,
    *,
    only_keys: Iterable[str] | None = None,
) -> TableOutcome:
    """OUTPUT_DIR の ``*.json`` を読み、CSV を作る。

    Args:
        output_dir: JSON が入っているフォルダ。
        only_keys: per-ID CSV ``対応表_{管理番号}.csv`` を作る対象の管理番号。
            ``None`` なら ``OUTPUT_DIR`` の全 JSON を対象にする。``fetch`` 直後に
            呼ぶときは、取れた ID（``status="ok"`` の ``FetchOutcome.entry.key``）
            を指定して渡し、**失敗した ID の CSV は作らず既存も消さない**。

    Returns:
        ``TableOutcome``: 書いたファイル一覧と、読み込めずに飛ばした JSON 一覧。

    Raises:
        FileNotFoundError: ``OUTPUT_DIR`` が存在しない場合は何もしない（成功扱い）。
            ``tables`` サブコマンドの側で「先に fetch してください」と扱う。
    """
    payloads, skipped = _read_all_jsons(output_dir)
    object_payloads, object_skipped = _read_object_jsons(output_dir)
    skipped = tuple(skipped) + tuple(object_skipped)

    # per-ID CSV は ``only_keys`` で絞った payloads だけ対象にする
    if only_keys is not None:
        only_set = set(only_keys)
        per_id_payloads = [(key, payload) for key, payload in payloads if key in only_set]
    else:
        per_id_payloads = payloads

    wrote: list[Path] = []

    for key, payload in per_id_payloads:
        path = output_dir / f"対応表_{key}.csv"
        rows = _build_correspondence_rows(payload)
        _write_csv(path, CORRESPONDENCE_COLUMNS, rows)
        wrote.append(path)

    # 全体の対応表・項目表は ``OUTPUT_DIR`` の **全 JSON** から作り直す
    # （今回の実行で取った分だけでなく、前に取った分も含む）
    sorted_payloads = sorted(payloads, key=lambda item: item[0])
    all_correspondence_rows: list[dict[str, str]] = []
    for _, payload in sorted_payloads:
        all_correspondence_rows.extend(_build_correspondence_rows(payload))
    correspondence_path = output_dir / "対応表.csv"
    _write_csv(correspondence_path, CORRESPONDENCE_COLUMNS, all_correspondence_rows)
    wrote.append(correspondence_path)

    field_rows = _build_field_table_rows(sorted_payloads, object_payloads)
    field_path = output_dir / "項目表.csv"
    _write_csv(field_path, FIELD_TABLE_COLUMNS, field_rows)
    wrote.append(field_path)

    for path in wrote:
        logger.info(
            "CSV を書き出し: %s（%d 行）",
            path,
            _count_data_rows(path),
        )
    for path in skipped:
        logger.warning("壊れた JSON を飛ばしました: %s", path)

    return TableOutcome(wrote=tuple(wrote), skipped=tuple(skipped))


def _read_all_jsons(output_dir: Path) -> tuple[list[tuple[str, dict]], list[Path]]:
    """``OUTPUT_DIR`` の ``*.json`` を全部読み、 ``(管理番号, payload)`` と
    読み込めなかったファイル一覧を返す。

    壊れた JSON・トップレベルが dict でないもの・ ``管理番号`` が無いものは
    そのファイルだけ飛ばして警告ログを出す（他の ID は止めない）。
    ``OUTPUT_DIR/objects/`` 配下は読まない（トップレベルの ``*.json`` だけ）。
    """
    payloads: list[tuple[str, dict]] = []
    skipped: list[Path] = []
    if not output_dir.exists():
        return payloads, skipped
    for path in sorted(output_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("JSON を読み込めないので飛ばします: %s (%s)", path, exc)
            skipped.append(path)
            continue
        if not isinstance(data, dict):
            logger.warning("JSON のトップレベルが dict ではないので飛ばします: %s", path)
            skipped.append(path)
            continue
        key = data.get("管理番号")
        if not isinstance(key, str) or not key:
            logger.warning("JSON に 管理番号 が無いので飛ばします: %s", path)
            skipped.append(path)
            continue
        payloads.append((key, data))
    return payloads, skipped


def _read_object_jsons(output_dir: Path) -> tuple[list[tuple[str, dict]], list[Path]]:
    """``OUTPUT_DIR/objects/*.json`` を全部読み、 ``(オブジェクト名, payload)`` と
    読み込めなかったファイル一覧を返す。

    存在しないときは空タプルを返す（エラーにしない）。 壊れた JSON・
    トップレベルが dict でないもの・ ``オブジェクト`` が無いものは
    そのファイルだけ飛ばして警告ログを出す（他の名前は止めない）。
    """
    payloads: list[tuple[str, dict]] = []
    skipped: list[Path] = []
    objects_dir = output_dir / OBJECT_SUBDIR
    if not objects_dir.exists():
        return payloads, skipped
    for path in sorted(objects_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("JSON を読み込めないので飛ばします: %s (%s)", path, exc)
            skipped.append(path)
            continue
        if not isinstance(data, dict):
            logger.warning("JSON のトップレベルが dict ではないので飛ばします: %s", path)
            skipped.append(path)
            continue
        name = data.get("オブジェクト")
        if not isinstance(name, str) or not name:
            logger.warning("JSON に オブジェクト が無いので飛ばします: %s", path)
            skipped.append(path)
            continue
        payloads.append((name, data))
    return payloads, skipped


def _build_correspondence_rows(payload: dict) -> list[dict[str, str]]:
    """1 JSON から対応表の行を作る（1 レポート列 = 1 行）。"""
    key = _coerce_str(payload.get("管理番号"))
    summary = _coerce_str(payload.get("概要"))
    report_id = _coerce_str(payload.get("レポートID"))
    main_object_raw = payload.get("主オブジェクト")
    main_object = main_object_raw if isinstance(main_object_raw, str) else ""

    report_type = _extract_report_type(payload.get("report"))

    objects = payload.get("objects")
    objects_dict = objects if isinstance(objects, dict) else {}
    main_describe = objects_dict.get(main_object) if main_object else None
    field_lookup = _build_field_lookup(main_describe)

    column_map = payload.get("column_map")
    column_map_list = column_map if isinstance(column_map, list) else []

    rows: list[dict[str, str]] = []
    for col in column_map_list:
        if not isinstance(col, dict):
            continue
        field_api = _coerce_str(col.get("対応フィールドAPI名"))
        lookup = field_lookup.get(field_api, {}) if field_api else {}
        row = {
            "管理番号": key,
            "概要": summary,
            "レポートID": report_id,
            "レポートタイプ": report_type,
            "主オブジェクト": main_object,
            "列キー": _coerce_str(col.get("列キー")),
            "表示名": _coerce_str(col.get("表示名")),
            "対応フィールドAPI名": field_api,
            "型": _coerce_str(col.get("型")),
            "参照先オブジェクト": lookup.get("referenceTo", ""),
            "リレーション名": lookup.get("relationshipName", ""),
            "備考": _coerce_str(col.get("備考")),
        }
        rows.append(row)
    return rows


def _extract_report_type(metadata: object) -> str:
    """``report.reportMetadata.reportType.type`` を取り出す。無ければ空文字。"""
    if not isinstance(metadata, dict):
        return ""
    report_metadata = metadata.get("reportMetadata")
    if not isinstance(report_metadata, dict):
        return ""
    report_type = report_metadata.get("reportType")
    if not isinstance(report_type, dict):
        return ""
    name = report_type.get("type")
    return name if isinstance(name, str) else ""


def _build_field_lookup(describe: object) -> dict[str, dict[str, str]]:
    """主オブジェクト describe の ``fields`` から、 ``name → (referenceTo, relationshipName)``
    の辞書を作る。

    ``referenceTo`` がリストなら ``|`` 区切り、 ``relationshipName`` が無ければ空。
    名前は ``describe`` 内に複数出てきても先勝ち（上書きしない）。
    """
    if not isinstance(describe, dict):
        return {}
    fields = describe.get("fields")
    if not isinstance(fields, list):
        return {}
    result: dict[str, dict[str, str]] = {}
    for field in fields:
        if not isinstance(field, dict):
            continue
        name = field.get("name")
        if not isinstance(name, str) or not name:
            continue
        reference_to = field.get("referenceTo")
        reference_text = ""
        if isinstance(reference_to, list) and reference_to:
            reference_text = "|".join(
                str(item) for item in reference_to if isinstance(item, str) and item
            )
        relationship_name = field.get("relationshipName")
        relationship_text = relationship_name if isinstance(relationship_name, str) else ""
        result[name] = {
            "referenceTo": reference_text,
            "relationshipName": relationship_text,
        }
    return result


def _build_field_table_rows(
    payloads: Sequence[tuple[str, dict]],
    object_payloads: Sequence[tuple[str, dict]] = (),
) -> list[dict[str, str]]:
    """全 JSON から項目表の行を作る（``オブジェクト × 項目`` のフラット表）。

    ``OUTPUT_DIR/objects/*.json`` もここで一緒に集約する。 同じオブジェクトが
    レポートの ``objects`` と ``objects/*.json`` の両方にあれば
    **``取得日時`` が新しい方** の describe を使う （レポート側・個別側を
    区別しない）。並びは「``objects`` のキー名の昇順 → describe の
    ``fields`` の並び順」のまま。

    ``段`` 列: 主オブジェクトは ``0`` 。 関連オブジェクトは JSON の
    ``関連オブジェクト`` リストに入った段の数字 （ 1 / 2 / … ）。 ``objects/*.json``
    だけの個別オブジェクトは空。
    """
    # ``obj_name -> 段`` のマッピング。 レポート JSON 側で ``関連オブジェクト`` から作る。
    # 同じ名前が複数回出てきたら ``段`` が小さい方を優先 （ 浅い方から到達した形 ） 。
    depth_by_name: dict[str, int] = {}

    # レポート JSON の ``objects`` 側
    merged: dict[str, tuple[str, dict]] = {}
    for _, payload in payloads:
        # 段マップ: 関連オブジェクトを集める
        main_object_raw = payload.get("主オブジェクト")
        main_object = main_object_raw if isinstance(main_object_raw, str) else ""
        if main_object:
            # 主オブジェクトは段 0 （ まだ登録されていなければ登録 ）
            if main_object not in depth_by_name:
                depth_by_name[main_object] = 0

        related = payload.get("関連オブジェクト")
        if isinstance(related, list):
            for entry in related:
                if not isinstance(entry, dict):
                    continue
                name = entry.get("名前")
                depth = entry.get("段")
                if not isinstance(name, str) or not name:
                    continue
                if not isinstance(depth, int):
                    continue
                if name not in depth_by_name or depth_by_name[name] > depth:
                    depth_by_name[name] = depth

        objects = payload.get("objects")
        if not isinstance(objects, dict):
            continue
        timestamp = payload.get("取得日時")
        timestamp_str = timestamp if isinstance(timestamp, str) else ""
        for obj_name, describe in objects.items():
            if not isinstance(obj_name, str) or not obj_name:
                continue
            if not isinstance(describe, dict):
                continue
            existing = merged.get(obj_name)
            if existing is None or timestamp_str > existing[0]:
                merged[obj_name] = (timestamp_str, describe)

    # ``OUTPUT_DIR/objects/*.json`` 側（ ``object`` キーを持つ）
    # 段マップは更新しない （ 個別オブジェクトは段が無い ）
    for _, payload in object_payloads:
        describe = payload.get("object")
        name = payload.get("オブジェクト")
        if not isinstance(name, str) or not name:
            continue
        if not isinstance(describe, dict):
            continue
        timestamp = payload.get("取得日時")
        timestamp_str = timestamp if isinstance(timestamp, str) else ""
        existing = merged.get(name)
        if existing is None or timestamp_str > existing[0]:
            merged[name] = (timestamp_str, describe)

    rows: list[dict[str, str]] = []
    for obj_name in sorted(merged.keys()):
        _, describe = merged[obj_name]
        obj_label = describe.get("label") if isinstance(describe, dict) else None
        obj_label_str = obj_label if isinstance(obj_label, str) else ""
        fields = describe.get("fields") if isinstance(describe, dict) else None
        if not isinstance(fields, list):
            continue
        depth = depth_by_name.get(obj_name)
        depth_str = "" if depth is None else str(depth)
        for field in fields:
            if not isinstance(field, dict):
                continue
            reference_to = field.get("referenceTo")
            reference_text = ""
            if isinstance(reference_to, list) and reference_to:
                reference_text = "|".join(
                    str(item) for item in reference_to if isinstance(item, str) and item
                )
            relationship_name = field.get("relationshipName")
            relationship_text = relationship_name if isinstance(relationship_name, str) else ""
            custom = field.get("custom")
            custom_mark = "○" if isinstance(custom, bool) and custom else ""
            rows.append(
                {
                    "オブジェクト": obj_name,
                    "段": depth_str,
                    "オブジェクト表示名": obj_label_str,
                    "項目API名": _coerce_str(field.get("name")),
                    "項目表示名": _coerce_str(field.get("label")),
                    "型": _coerce_str(field.get("type")),
                    "参照先オブジェクト": reference_text,
                    "リレーション名": relationship_text,
                    "カスタム": custom_mark,
                    "選択肢": _format_picklist(field.get("picklist"), field.get("picklistTotal")),
                }
            )
    return rows


def _format_picklist(picklist: object, picklist_total: object = 0) -> str:
    """``picklist`` （ active な ``value`` だけの配列）を ``|`` 区切りにする。

    ``picklistTotal`` が ``PICKLIST_LIMIT``（既定 30）を超えるとき末尾に
    ``…`` を 1 個足して切る（業務で実際に必要になるのは数件までなので、
    30 で十分。多すぎると CSV が読みづらくなる）。 ``picklist`` 自体が 30 件に
    絞られていても、 ``picklistTotal`` で「さらに後ろがある」ことを検知する。
    """
    if not isinstance(picklist, list) or not picklist:
        return ""
    values: list[str] = [v for v in picklist if isinstance(v, str) and v]
    if not values:
        return ""
    total = picklist_total if isinstance(picklist_total, int) else len(values)
    if total > PICKLIST_LIMIT:
        if len(values) > PICKLIST_LIMIT:
            values = values[:PICKLIST_LIMIT]
        values.append("…")
    return "|".join(values)


def _coerce_str(value: object) -> str:
    """値を ``str`` にする。 ``None`` は空文字。"""
    if value is None:
        return ""
    return str(value)


def _write_csv(path: Path, columns: Sequence[str], rows: list[dict[str, str]]) -> None:
    """UTF-8 BOM + CRLF で CSV を ``atomic_write`` 経由で書く。

    comken の ``CSV`` クラスの ``_write`` と同じく、 ``atomic_write`` の
    一時ファイルへ ``encoding="utf-8-sig"`` + ``newline=""`` で開き、
    ``csv.DictWriter`` のデフォルト（ ``\r\n``）で書く。新規ファイルなので
    文字コードを明示して BOM 付きで固定する（Excel で開ける形式）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with (
        atomic_write(path) as temporary_path,
        temporary_path.open("w", encoding=CSV_ENCODING, newline="") as file,
    ):
        writer = csv.DictWriter(file, fieldnames=list(columns), extrasaction="raise")
        writer.writeheader()
        writer.writerows(rows)


def _count_data_rows(path: Path) -> int:
    """CSV のデータ行数を返す（見出しを含まない）。ログ用に軽量に数える。"""
    count = 0
    with path.open("r", encoding=CSV_ENCODING, newline="") as file:
        reader = csv.reader(file)
        for _ in reader:
            count += 1
    return max(count - 1, 0)
