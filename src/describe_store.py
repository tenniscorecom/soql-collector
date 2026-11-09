"""src/describe_store.py — オブジェクト describe を実行をまたいで CSV に貯める。

レポートの describe は毎回 Salesforce に聞くが、 オブジェクトの describe は
**このツールで標準的に入力とする管理番号が変わるまで** 再利用できる。 同じ組織
（URL の netloc が同じ） で過去に取り直した結果は **CSV 1 ファイル** に
1 行 1 項目で溜まり、 次回の実行からは CSV を見て API を呼ばない。

- 保存先: ``[FILES] DESCRIBE_CSV_PATH`` （既定 ``./data/項目定義.csv``）
- 子リレーション （ ``describe`` の ``childRelationships`` ） は別の CSV
  （同じフォルダの ``子リレーション.csv`` ） に保存する。 項目定義.csv の
  形式は変えない
- 子が 0 件のオブジェクトも、 子リレーション.csv に 1 行だけ残す（ 子オブジェクト・
  リレーション名・参照項目 は空文字 ）。 これがないと 「まだ取り直していない」
  と区別がつかなくなるため。 読み込んだときは キーは残して 要素は 0 個にする
- メモリ辞書（1 回の実行内） のキーは ``(組織, オブジェクト)`` のタプル。
  組織 = URL のホスト名 （小文字）。 これで本番と Sandbox のレポートが
  同じ実行に混ざっても別物として扱われる
- 列 （この順） : 組織 / オブジェクト / オブジェクト表示名 / カスタム
  オブジェクト / 項目 API 名 / 項目表示名 / 型 / 参照先 / リレーション名 /
  カスタム項目 / 絞り込み可 / 並べ替え可 / 選択肢 / 取得日時
- 1 オブジェクトの行は同じ取得日時
- 真偽値は ``○`` / 空文字、 参照先と選択肢は ``|`` 区切り
- CSV から読んだ describe は **そのまま** ``make_slim_object`` に渡せる形に
  組み立て直して返す （既存の mapping / pii / 関連の掘り込みは変えない）
- 失敗 （HTTP エラー / ``ValueError``） は CSV に書かない。 1 回の実行の
  メモリだけに留める （401 / 403 はそのまま上位へ送出）
- 見出しが違う既存 CSV は ``CSVError`` を上げ、 ファイルは変更しない
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from comken.core.table import Table
from comken.exceptions import CSVError
from comken.toolbox.csv import CSV
from comken.toolbox.salesforce.hosts import host_of

from src.text_utils import coerce_str as _coerce_str

logger = logging.getLogger(__name__)


# ── CSV の列定義 ────────────────────────────────────────────────────────────


DESCRIBE_CSV_COLUMNS: tuple[str, ...] = (
    "組織",
    "オブジェクト",
    "オブジェクト表示名",
    "カスタムオブジェクト",
    "項目API名",
    "項目表示名",
    "型",
    "参照先",
    "リレーション名",
    "カスタム項目",
    "絞り込み可",
    "並べ替え可",
    "選択肢",
    "取得日時",
)


# 子リレーションを保存する CSV のファイル名。 項目定義.csv と同じフォルダに
# 置く （ 保存場所の決め方は既存の保存に合わせる ）
CHILD_RELATIONSHIP_CSV_NAME = "子リレーション.csv"


# 子リレーションの CSV の列 （この順）
CHILD_RELATIONSHIP_CSV_COLUMNS: tuple[str, ...] = (
    "組織",
    "オブジェクト",
    "子オブジェクト",
    "リレーション名",
    "参照項目",
    "取得日時",
)


# 真偽値の書き出し
TRUE_MARK = "○"


# ── ヘルパー ────────────────────────────────────────────────────────────────


def org_from_url(url: str) -> str:
    """URL から、 組織を見分けるホスト名を取り出す（小文字）。

    comken の ``site_for`` と同じ ``host_of`` を使う。 大小文字の違いと、 ブラウザのアドレスバーの
    URL（``○○.lightning.force.com``）と My Domain（``○○.my.salesforce.com``）の違いを吸収する
    ので、 同じ組織が別の組織として保存されることはなく、 本番と Sandbox の取り違えも起きない。
    """
    return host_of(url)


def _normalize_object(name: str) -> str:
    """オブジェクト名を大小文字無視で比較用に正規化する。

    キー （``(org, object)`` のタプル） と、 CSV から読むときの引きにこれを使う。
    """
    return name.strip().lower()


def _key(org: str, object_name: str) -> tuple[str, str]:
    """メモリ辞書 / CSV の共通キー ``(org_lower, object_lower)`` を組み立てる。"""
    return (org.strip().lower(), _normalize_object(object_name))


def child_relationship_csv_path(describe_csv_path: Path) -> Path:
    """子リレーションの CSV のパスを返す。 項目定義.csv と同じフォルダに置く。"""
    return describe_csv_path.parent / CHILD_RELATIONSHIP_CSV_NAME


def _bool_mark(value: str) -> bool:
    """CSV から読んだ真偽値 （``"○"``） を ``bool`` にする。"""
    return value.strip() == TRUE_MARK


def _join_pipe(values: list[str]) -> str:
    """空要素を除いて ``|`` 区切りにする。"""
    return "|".join(v for v in values if v)


def _split_pipe(text: str) -> list[str]:
    """``|`` 区切りを空要素を除いて ``list[str]`` にする。"""
    return [v for v in (text or "").split("|") if v]


# ── describe dict ⇄ CSV 行 ──────────────────────────────────────────────


def active_picklist_values(picklist_values: object) -> list[str]:
    """API の ``picklistValues`` から active な ``value`` だけを取り出す（境界）。

    ``fetch._slim_field``（最大 30 件に切る）と、 項目定義.csv への書き出しの
    両方がこれを使う。
    """
    if not isinstance(picklist_values, list):
        return []
    return [
        entry["value"]
        for entry in picklist_values
        if isinstance(entry, dict)
        and entry.get("active")
        and isinstance(entry.get("value"), str)
        and entry["value"]
    ]


def _describe_to_csv_rows(
    org: str,
    object_name: str,
    object_label: str,
    object_custom: bool,
    fields: list[dict[str, Any]],
    fetched_at: str,
) -> list[dict[str, str]]:
    """describe dict から （1 オブジェクト） の CSV 行 dict のリストを作る。

    元の ``fields`` の **出現順** を保つ。 picklist は active な値だけ、
    参照先は ``referenceTo`` を ``|`` 区切りに潰す。
    """
    obj_custom_mark = TRUE_MARK if object_custom else ""
    rows: list[dict[str, str]] = []
    for raw_field in fields:
        if not isinstance(raw_field, dict):
            continue
        reference_to_raw = raw_field.get("referenceTo")
        reference_to_list = (
            [str(x) for x in reference_to_raw if isinstance(x, str) and x]
            if isinstance(reference_to_raw, list)
            else []
        )
        relationship_name = raw_field.get("relationshipName")
        relationship_text = (
            relationship_name if isinstance(relationship_name, str) and relationship_name else ""
        )
        custom_raw = raw_field.get("custom")
        custom_mark = TRUE_MARK if isinstance(custom_raw, bool) and custom_raw else ""
        active = active_picklist_values(raw_field.get("picklistValues"))
        rows.append(
            {
                "組織": org,
                "オブジェクト": object_name,
                "オブジェクト表示名": object_label,
                "カスタムオブジェクト": obj_custom_mark,
                "項目API名": _coerce_str(raw_field.get("name")),
                "項目表示名": _coerce_str(raw_field.get("label")),
                "型": _coerce_str(raw_field.get("type")),
                "参照先": _join_pipe(reference_to_list),
                "リレーション名": relationship_text,
                "カスタム項目": custom_mark,
                "絞り込み可": "",
                "並べ替え可": "",
                "選択肢": _join_pipe(active),
                "取得日時": fetched_at,
            }
        )
    return rows


def describe_to_csv_rows(
    org: str,
    object_name: str,
    describe: dict[str, Any],
    fetched_at: str,
) -> list[dict[str, str]]:
    """describe dict から CSV 行 dict のリストを作る。"""
    object_label = _coerce_str(describe.get("label"))
    object_custom_raw = describe.get("custom")
    object_custom = isinstance(object_custom_raw, bool) and object_custom_raw
    fields_raw = describe.get("fields")
    fields = fields_raw if isinstance(fields_raw, list) else []
    return _describe_to_csv_rows(org, object_name, object_label, object_custom, fields, fetched_at)


def csv_rows_to_describe(rows: list[dict[str, str]]) -> dict[str, Any]:
    """CSV の行 dict から ``describe_object`` 戻り値と同じ形の dict を組み立てる。

    ``make_slim_object`` が読むキー（``name`` / ``label`` / ``custom`` / ``fields`` と、
    各 field の ``name`` / ``label`` / ``type`` / ``custom`` / ``referenceTo`` /
    ``relationshipName`` / ``picklistValues``）を持たせる。 これで CSV から読んだ値を
    ``make_slim_object`` に通しても API 直呼びと同じ出力が得られる。 ``rows`` は comken の
    ``CSV`` で読んだ行（見出しを確かめてあり、 値はすべて str）。
    """
    if not rows:
        return {"name": "", "label": "", "custom": False, "fields": []}
    fields = [
        {
            "name": row["項目API名"],
            "label": row["項目表示名"],
            "type": row["型"],
            "custom": _bool_mark(row["カスタム項目"]),
            "referenceTo": _split_pipe(row["参照先"]),
            "relationshipName": row["リレーション名"].strip() or None,
            "picklistValues": [{"active": True, "value": v} for v in _split_pipe(row["選択肢"])],
        }
        for row in rows
    ]
    return {
        "name": rows[0]["オブジェクト"],
        "label": rows[0]["オブジェクト表示名"],
        "custom": _bool_mark(rows[0]["カスタムオブジェクト"]),
        "fields": fields,
    }


# ── 子リレーション（describe の childRelationships） ────────────────────


def describe_to_child_relationship_rows(
    org: str,
    object_name: str,
    describe: dict[str, Any],
    fetched_at: str,
) -> list[dict[str, str]]:
    """describe dict から 子リレーションの CSV 行 dict のリストを作る。

    ``relationshipName`` が空の要素は落とす（リレーションとして使えないため）。

    子が 1 つも無いオブジェクトは、 「取得済みだが 0 件」 を表す印の行を
    1 行書く（ 子オブジェクト・リレーション名・参照項目 は空文字 ）。 これ
    が無いと、 「まだ取り直していない」 と区別がつかなくなるため。
    """
    rows: list[dict[str, str]] = []
    relationships = describe.get("childRelationships")
    if isinstance(relationships, list):
        for item in relationships:
            if not isinstance(item, dict):
                continue
            relationship_name = _coerce_str(item.get("relationshipName"))
            if not relationship_name:
                continue
            rows.append(
                {
                    "組織": org,
                    "オブジェクト": object_name,
                    "子オブジェクト": _coerce_str(item.get("childSObject")),
                    "リレーション名": relationship_name,
                    "参照項目": _coerce_str(item.get("field")),
                    "取得日時": fetched_at,
                }
            )
    if rows:
        return rows
    return [
        {
            "組織": org,
            "オブジェクト": object_name,
            "子オブジェクト": "",
            "リレーション名": "",
            "参照項目": "",
            "取得日時": fetched_at,
        }
    ]


def csv_rows_to_child_relationships(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """CSV の行 dict から ``childRelationships`` の要素のリストを組み立てる。

    「取得済みだが 0 件」 の印の行（ リレーション名が空の行 ） は除かれる。
    キーの有無で 「取得済みか」 を表是这样的ので、 要素は 0 個でも
    キーは残す。
    """
    return [
        {
            "childSObject": row["子オブジェクト"],
            "field": row["参照項目"],
            "relationshipName": row["リレーション名"],
        }
        for row in rows
        if row["リレーション名"].strip()
    ]


def load_child_relationship_index(path: Path) -> dict[tuple[str, str], list[dict[str, str]]]:
    """子リレーションの CSV を読み ``(org, object_lower) -> 要素のリスト`` を返す。

    - ファイルが存在しないときは空 dict を返す（古い保存データでも落ちない）
    - 見出しが ``CHILD_RELATIONSHIP_CSV_COLUMNS`` と違うときは ``CSVError``
    - 「取得済みだが 0 件」 の印の行は、 キーは残して要素は 0 個にする
      （ キーの有無で 「取得済みか」 を表すため ）
    """
    if not path.exists():
        return {}
    with CSV(path) as csv:
        table = csv.read()
    if list(table.columns) != list(CHILD_RELATIONSHIP_CSV_COLUMNS):
        raise CSVError(
            f"既存の {path.name} の見出しが想定と違います: {path}\n"
            f"期待: {list(CHILD_RELATIONSHIP_CSV_COLUMNS)}\n"
            f"実際: {list(table.columns)}\n"
            "対処: ファイルを別名に退避してから再実行してください。"
        )
    index: dict[tuple[str, str], list[dict[str, str]]] = {}
    for row in table.to_rows():
        org_raw = row["組織"].strip().lower()
        obj_raw = row["オブジェクト"].strip()
        if not org_raw or not obj_raw:
            continue
        index.setdefault((org_raw, _normalize_object(obj_raw)), []).append(row)
    return {key: csv_rows_to_child_relationships(rows) for key, rows in index.items()}


def upsert_child_relationship_rows(
    path: Path,
    org: str,
    object_name: str,
    describe: dict[str, Any],
    *,
    fetched_at: str | None = None,
) -> None:
    """子リレーションの CSV の ``(org, object)`` の行を入れ替える。

    既存 CSV が無いときは新規作成。 既存 CSV が見出し違いのときは
    ``CSVError`` を上げる。
    """
    org_key = org.strip().lower()
    obj_key = _normalize_object(object_name)
    ts = fetched_at or now_iso()

    rows: list[dict[str, str]] = []
    if path.exists():
        with CSV(path) as csv:
            table = csv.read()
        if list(table.columns) != list(CHILD_RELATIONSHIP_CSV_COLUMNS):
            raise CSVError(
                f"既存の {path.name} の見出しが想定と違います: {path}\n"
                "対処: ファイルを別名に退避してから再実行してください。"
            )
        for row in table.to_rows():
            row_org = row["組織"].strip().lower()
            row_obj = row["オブジェクト"].strip()
            if row_org == org_key and _normalize_object(row_obj) == obj_key:
                continue
            rows.append(row)

    rows.extend(describe_to_child_relationship_rows(org_key, object_name, describe, ts))

    rows.sort(
        key=lambda r: (r["組織"], r["オブジェクト"], r["リレーション名"], r["子オブジェクト"])
    )

    path.parent.mkdir(parents=True, exist_ok=True)
    with CSV(path, columns=list(CHILD_RELATIONSHIP_CSV_COLUMNS)) as csv:
        csv.replace(Table(list(CHILD_RELATIONSHIP_CSV_COLUMNS), rows))


# ── 読み込み ────────────────────────────────────────────────────────────


def load_describe_index(path: Path) -> dict[tuple[str, str], dict[str, Any]]:
    """CSV を読み ``(org, object_lower) -> describe dict`` の dict を返す。

    - ファイルが存在しないときは空 dict を返す （新規扱い）
    - 見出しが ``DESCRIBE_CSV_COLUMNS`` と違うときは ``CSVError`` を上げる
    - 1 行でも列数が違うときも ``CSVError`` （``CSV.read`` が出す）
    """
    if not path.exists():
        return {}
    try:
        with CSV(path) as csv:
            table = csv.read()
    except CSVError:
        raise
    if list(table.columns) != list(DESCRIBE_CSV_COLUMNS):
        raise CSVError(
            f"既存の {path.name} の見出しが想定と違います: {path}\n"
            f"期待: {list(DESCRIBE_CSV_COLUMNS)}\n"
            f"実際: {list(table.columns)}\n"
            "対処: ファイルを別名に退避してから再実行してください。"
        )
    rows_by_object: dict[tuple[str, str], list[dict[str, str]]] = {}
    for row in table.to_rows():
        org_raw = row["組織"].strip().lower()
        obj_raw = row["オブジェクト"].strip()
        if not org_raw or not obj_raw:
            # 想定外の行は読み飛ばす
            continue
        rows_by_object.setdefault((org_raw, _normalize_object(obj_raw)), []).append(row)
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for key, rows in rows_by_object.items():
        # 同じ (org, object) の行は 1 つの describe に集約
        # （通常は 1 グループだけだが、 古いログ混入時の保険）
        object_rows = sorted(rows, key=lambda r: r["取得日時"])
        result[key] = csv_rows_to_describe(object_rows)
    return result


# ── 書き込み ────────────────────────────────────────────────────────────


def upsert_object_rows(
    path: Path,
    org: str,
    object_name: str,
    describe: dict[str, Any],
    *,
    fetched_at: str | None = None,
) -> None:
    """CSV の ``(org, object)`` の行を入れ替えて atomic に書き換える。

    既存 CSV が無いときは新規作成。 既存 CSV が見出し違いのときは
    ``CSVError`` を上げる （``merge_column_names_csv`` と同じやり方）。
    """
    org_key = org.strip().lower()
    obj_key = _normalize_object(object_name)
    ts = fetched_at or now_iso()

    rows: list[dict[str, str]] = []
    if path.exists():
        try:
            with CSV(path) as csv:
                table = csv.read()
        except CSVError:
            raise
        if list(table.columns) != list(DESCRIBE_CSV_COLUMNS):
            raise CSVError(
                f"既存の {path.name} の見出しが想定と違います: {path}\n"
                "対処: ファイルを別名に退避してから再実行してください。"
            )
        for row in table.to_rows():
            row_org = row["組織"].strip().lower()
            row_obj = row["オブジェクト"].strip()
            if row_org == org_key and _normalize_object(row_obj) == obj_key:
                continue
            rows.append(row)

    rows.extend(describe_to_csv_rows(org_key, object_name, describe, ts))

    rows.sort(key=lambda r: (r["組織"], r["オブジェクト"], r["取得日時"], r["項目API名"]))

    path.parent.mkdir(parents=True, exist_ok=True)
    with CSV(path, columns=list(DESCRIBE_CSV_COLUMNS)) as csv:
        csv.replace(Table(list(DESCRIBE_CSV_COLUMNS), rows))


def now_iso() -> str:
    """ISO 形式の取得日時を返す （秒精度）。

    CSV 上で人が読む時間表記と並べ替えのしやすさを両立するため ``T`` 区切り
    の ISO 8601 にする （``YYYY-MM-DDTHH:MM:SS+00:00``） 。
    """
    return datetime.now(UTC).isoformat(timespec="seconds")


def _merge_child_relationships(
    index: dict[tuple[str, str], dict[str, Any]],
    child_index: dict[tuple[str, str], list[dict[str, str]]],
) -> None:
    """読み込んだ describe に 子リレーションを足す （ in place ） 。

    子リレーションの CSV が無い（古い保存データ） ときは何もしない。
    """
    for key, relationships in child_index.items():
        describe = index.get(key)
        if describe is None:
            continue
        describe["childRelationships"] = relationships


# ── メモリ＋CSV のストア ────────────────────────────────────────────────


@dataclass
class DescribeStore:
    """メモリ辞書と CSV を束ねたストア。

    1 回の実行の中で ``fetch_object_cached`` から何度も参照される。 メモリ
    にヒットすれば即返し、 無ければ CSV を見てヒットすればメモリに載せて返す。
    どちらも無ければ API を呼ぶ （呼び元が行う） 。

    属性:
        path: CSV のパス。 親フォルダが無ければ作る。
        csv_index: 実行開始時に ``load_describe_index`` で読んだ ``(org,
            object_lower) -> describe dict`` のスナップショット。 CSV に書いた
            直後にメモリキャッシュも更新するため、 CSV 上の値とメモリの一致は
            実行中も保たれる
        memory_cache: 実行中の ``(org, object_lower)`` -> ``{"describe":
            dict|None, "warning": str|None, "source": "csv"|"api"|"memory"}``
            のキャッシュ。 CSV から引き出したエントリもここに載せる
        csv_hit_count: メモリに無く CSV から読んだ回数
        api_count: API を呼んで成功した回数
        refresh_count: ``refresh_from_api`` で取り直した回数
    """

    path: Path
    csv_index: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    memory_cache: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    csv_hit_count: int = 0
    api_count: int = 0
    refresh_count: int = 0

    @classmethod
    def empty(cls, path: Path) -> DescribeStore:
        """CSV を読まずに空のストアを作る （テストやパス指定が無いとき）"""
        return cls(path=path)

    @classmethod
    def load(cls, path: Path) -> DescribeStore:
        """CSV を読んでストアを作る。 ファイルが無ければ空ストアを返す。

        子リレーションの CSV も読み、 describe に ``childRelationships`` として
        持たせる。 子の CSV が無い（古い保存データ） ときは空のまま。
        """
        index = load_describe_index(path)
        child_index = load_child_relationship_index(child_relationship_csv_path(path))
        _merge_child_relationships(index, child_index)
        return cls(path=path, csv_index=index)

    def lookup(self, org: str, object_name: str) -> dict[str, Any] | None:
        """``(org, object)`` の値を返す。 メモリ → CSV の順。

        戻り値は ``{"describe": dict|None, "warning": str|None, "source":
        "csv"|"api"|"memory"}`` の dict。 ヒットしなければ ``None`` 。
        """
        key = _key(org, object_name)
        cached = self.memory_cache.get(key)
        if cached is not None:
            return cached
        describe = self.csv_index.get(key)
        if describe is not None:
            promoted: dict[str, Any] = {
                "describe": describe,
                "warning": None,
                "source": "csv",
            }
            self.memory_cache[key] = promoted
            self.csv_hit_count += 1
            return promoted
        return None

    def record_api_success(self, org: str, object_name: str, describe: dict[str, Any]) -> None:
        """API で取得した describe をメモリと CSV に書く。

        項目定義.csv と 子リレーション.csv の両方に書く。 CSV への書き出しが
        失敗してもメモリには載せる （1 回の実行の中では同じオブジェクトを
        取り直さないため） 。

        API から取った describe は、 子が 0 件でも 「子リレーションは取得済み」
        として扱う。 そのため  ``childRelationships`` キーが無い.describe は
        キーのみ足してからメモリと CSV に書く。
        """
        key = _key(org, object_name)
        stored = describe
        if isinstance(describe, dict) and "childRelationships" not in describe:
            stored = {**describe, "childRelationships": []}
        self.memory_cache[key] = {
            "describe": stored,
            "warning": None,
            "source": "api",
        }
        self.api_count += 1
        try:
            upsert_object_rows(self.path, org, object_name, stored)
            upsert_child_relationship_rows(
                child_relationship_csv_path(self.path),
                org,
                object_name,
                stored,
            )
        except (OSError, CSVError) as exc:
            # メモリには載っているので、 この実行中は API を再呼びしない
            logger.warning("CSV への書き出しに失敗: %s", exc)

    def has_child_relationships(self, org: str, object_name: str) -> bool:
        """子リレーションの情報を持っている（取得済み）なら True。

        件数が 0 でも True。 「まだ取り直していない」 ときは False。
        判定は ``childRelationships`` キーの有無で行う （ 0 件でもキーは
        残すので、 件数では判定できない ） 。
        """
        key = _key(org, object_name)
        cached = self.memory_cache.get(key)
        if cached is not None:
            describe = cached.get("describe")
            if isinstance(describe, dict):
                return "childRelationships" in describe
        describe = self.csv_index.get(key)
        if describe is None:
            return False
        return "childRelationships" in describe

    def record_api_failure(self, org: str, object_name: str, warning: str) -> None:
        """API の失敗をメモリにだけ覚える。 CSV には書かない。"""
        key = _key(org, object_name)
        self.memory_cache[key] = {
            "describe": None,
            "warning": warning,
            "source": "memory",
        }

    def restore_csv_entry(self, org: str, object_name: str) -> None:
        """失敗した取り直しのあと、 CSV の値をメモリへ読み戻す。

        ``refresh_from_api`` が失敗するとメモリには「失敗記録」だけが残る。
        そのままでは次のレポートが同じオブジェクトを引けなくなるので、 CSV の
        値へ戻す。
        """
        key = _key(org, object_name)
        describe = self.csv_index.get(key)
        if describe is None:
            return
        self.memory_cache[key] = {
            "describe": describe,
            "warning": None,
            "source": "csv",
        }

    def refresh_from_api(
        self,
        client: Any,
        org: str,
        object_name: str,
        warnings: list[str],
    ) -> dict[str, Any] | None:
        """``(org, object)`` を API から取り直し、 成功時に CSV の行を入れ替える。

        戻り値: 成功時は新しい describe dict、 失敗時は ``None``。 ``401`` /
        ``403`` はそのまま上位へ送出する （他の fetch と同じ挙動） 。
        """
        from comken.exceptions import SalesforceRequestError

        from src.fetch import FATAL_STATUS_CODES

        try:
            describe = client.describe_object(object_name)
        except SalesforceRequestError as exc:
            if exc.status_code in FATAL_STATUS_CODES:
                raise
            warning = (
                f"オブジェクト {object_name} の describe に失敗"
                f"（HTTP {exc.status_code}: {exc.detail}）"
            )
            self.record_api_failure(org, object_name, warning)
            return None
        except ValueError as exc:
            warning = f"オブジェクト {object_name} の describe に失敗: {exc}"
            self.record_api_failure(org, object_name, warning)
            return None

        self.record_api_success(org, object_name, describe)
        self.refresh_count += 1
        return describe
