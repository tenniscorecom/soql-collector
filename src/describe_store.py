"""src/describe_store.py — オブジェクトの describe の原本を保存する。

原本は ``元データ/オブジェクト/{組織}__{オブジェクト名の小文字}.json`` に、 API の返り値の
まま保存する（形は ``src/raw_store.py``）。 組織を分けるので、 本番と Sandbox が同じ実行に
混ざっても別物として扱われる。

- 保存にあるものは API を呼ばない。 ``force=True`` で作ると実行の最初に保存を見ない
- 失敗（HTTP エラー / ``ValueError``）は保存しない。 同じ実行の中では 1 回だけ試すよう、
  メモリにだけ覚える（401 / 403 は呼び出し側がそのまま上位へ送出する）
- AI チャットに渡す ``項目定義/{オブジェクト}.csv`` の行は ``describe_to_csv_rows`` で作る
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from comken.toolbox.salesforce.hosts import host_of

from src.raw_store import RAW_DIRNAME, now_iso, raw_file_path, read_original, write_original
from src.text_utils import coerce_str as _coerce_str

logger = logging.getLogger(__name__)


# 保存先のフォルダ名（``元データ/`` の下）
OBJECT_STORE_DIRNAME = "オブジェクト"


# 項目定義の CSV の列（AI チャット用の ``項目定義/{オブジェクト}.csv``）
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


def _key(org: str, object_name: str) -> tuple[str, str]:
    """メモリ辞書と保存ファイル名の共通キー ``(org_lower, object_lower)`` を組み立てる。"""
    return (org.strip().lower(), object_name.strip().lower())


def _join_pipe(values: list[str]) -> str:
    """空要素を除いて ``|`` 区切りにする。"""
    return "|".join(v for v in values if v)


# ── describe の原本 → CSV 行 ───────────────────────────────────────────


def active_picklist_values(picklist_values: object) -> list[str]:
    """API の ``picklistValues`` から active な ``value`` だけを取り出す（境界）。

    ``fetch._slim_field``（最大 30 件に切る）と、 AI チャット用の項目定義 CSV の
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
                "絞り込み可": TRUE_MARK if raw_field.get("filterable") is True else "",
                "並べ替え可": TRUE_MARK if raw_field.get("sortable") is True else "",
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


# ── 保存とメモリのストア ────────────────────────────────────────────────


@dataclass
class DescribeStore:
    """メモリ辞書と保存した原本を束ねたストア。

    1 回の実行の中で ``fetch_object_cached`` から何度も参照される。 メモリにヒットすれば
    即返し、 無ければ保存を見てヒットすればメモリに載せて返す。 どちらも無ければ API を
    呼ぶ（呼び元が行う）。

    属性:
        folder: 原本を置くフォルダ。 ``None`` なら保存せずメモリだけで動く
        force: True なら実行の最初に保存を見ない（全部取り直す）
        memory_cache: 実行中の ``(org, object_lower)`` -> ``{"describe": dict|None,
            "warning": str|None, "source": "saved"|"api"|"memory", "fetched_at": str}``
        saved_hit_count: メモリに無く保存から読んだ回数
        api_count: API を呼んで成功した回数
        failure_count: API を呼んで失敗した回数（保存に無くて諦めた回数も数える）
    """

    folder: Path | None = None
    force: bool = False
    memory_cache: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    saved_hit_count: int = 0
    api_count: int = 0
    failure_count: int = 0

    @classmethod
    def empty(cls) -> DescribeStore:
        """何も保存しないストアを作る（テストや単発の実行用）。"""
        return cls()

    @classmethod
    def load(cls, describe_csv_path: Path, *, force: bool = False) -> DescribeStore:
        """``describe_csv_path`` と同じフォルダの ``元データ/`` を保存場所にする。"""
        return cls(
            folder=describe_csv_path.parent / RAW_DIRNAME / OBJECT_STORE_DIRNAME,
            force=force,
        )

    def lookup(self, org: str, object_name: str) -> dict[str, Any] | None:
        """``(org, object)`` の値を返す。 メモリ → 保存の順。 ヒットしなければ ``None``。

        ``describe`` は原本のまま。 呼び出し側は ``make_slim_object`` で整えてから使う。
        """
        key = _key(org, object_name)
        cached = self.memory_cache.get(key)
        if cached is not None:
            return cached
        if self.folder is None or self.force:
            return None
        saved = read_original(raw_file_path(self.folder, key[0], key[1]))
        if saved is None:
            return None
        promoted: dict[str, Any] = {
            "describe": saved.original,
            "warning": None,
            "source": "saved",
            "fetched_at": saved.fetched_at,
        }
        self.memory_cache[key] = promoted
        self.saved_hit_count += 1
        return promoted

    def record_api_success(self, org: str, object_name: str, describe: dict[str, Any]) -> None:
        """API で取得した describe をメモリに載せ、 原本を保存する。

        保存に失敗してもメモリには載せる（1 回の実行の中で同じオブジェクトを取り直さないため）。
        """
        key = _key(org, object_name)
        fetched_at = now_iso()
        self.memory_cache[key] = {
            "describe": describe,
            "warning": None,
            "source": "api",
            "fetched_at": fetched_at,
        }
        self.api_count += 1
        if self.folder is not None:
            write_original(raw_file_path(self.folder, key[0], key[1]), key[0], describe, fetched_at)

    def record_api_failure(self, org: str, object_name: str, warning: str) -> None:
        """失敗をメモリにだけ覚える。 保存しないので、 次の取得でまた試す。"""
        self.memory_cache[_key(org, object_name)] = {
            "describe": None,
            "warning": warning,
            "source": "memory",
            "fetched_at": "",
        }
        self.failure_count += 1

    def fetched_at(self, org: str, object_name: str) -> str:
        """原本を取った日時。 分からなければ空文字。"""
        cached = self.memory_cache.get(_key(org, object_name)) or {}
        return str(cached.get("fetched_at") or "")
