"""オブジェクトの describe をストア（メモリ → CSV）か API から取る。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from comken.exceptions import SalesforceRequestError

from src.describe_store import DescribeStore
from src.fetch.report_slim import make_slim_object

# ``describe_object`` が 401 / 403 を返したとき、 その ID 全体を失敗させる
# ステータスコード（comken の ``ReportAPI.describe`` と同じく）。
FATAL_STATUS_CODES: frozenset[int] = frozenset({401, 403})


def make_overrides_fetcher(
    *,
    client: Any,
    org: str,
    store: DescribeStore,
    objects: dict[str, dict[str, Any]],
    csv_used_keys: set[tuple[str, str]],
) -> Callable[[str], dict[str, Any] | None]:
    """列キーのたどりと ``apply_overrides`` に渡す fetcher を作る。

    引数 ``name`` のオブジェクトを取りに行くが、 自前の WARNING は出さない
    （``apply_overrides`` が 「たどれません」 文に整形して出す）。 401
    と 403 はそのまま上位へ送出 （``apply_overrides`` を経由して
    ``_fetch_one`` の ``except`` で拾われ、 レポート全体が失敗扱いになる） 。

    取得順序は ``fetch_object_cached`` と同じ:

    1. ``objects`` に有ればそれを返す （API を叩かない）
    2. ``DescribeStore`` （メモリ → CSV） を見る
    3. 無ければ API を呼ぶ。 成功時はメモリと CSV に保存

    取れた describe は ``make_slim_object`` で整えて ``objects[name]`` に入れる。
    """

    def fetcher(name: str) -> dict[str, Any] | None:
        # 1) 既に取っていればそれを返す
        if name in objects:
            return objects[name]
        # 2) ストアのキャッシュを引く
        cached = store.lookup(org, name)
        if cached is not None:
            describe = cached.get("describe")
            if describe is None:
                # 失敗記録だけのエントリは取りに行かない
                return None
            # CSV から読んだものは取り直し候補に入れる
            if cached.get("source") == "csv":
                csv_used_keys.add((org.lower(), name.strip().lower()))
            objects[name] = make_slim_object(describe)
            return objects[name]
        # 3) API 呼び出し
        try:
            describe = client.describe_object(name)
        except SalesforceRequestError as exc:
            if exc.status_code in FATAL_STATUS_CODES:
                # 401 / 403 は上位へ送出してレポート全体を失敗扱いにする
                raise
            # その他: 失敗をメモリにだけ覚えて None を返す。 警告は
            # ``apply_overrides`` 側で 「たどれません」 文に変換する
            store.record_api_failure(org, name, f"HTTP {exc.status_code}: {exc.detail}")
            return None
        except ValueError as exc:
            store.record_api_failure(org, name, str(exc))
            return None
        store.record_api_success(org, name, describe)
        objects[name] = make_slim_object(describe)
        return objects[name]

    return fetcher


def fetch_object_cached(
    client: Any,
    org: str,
    name: str,
    store: DescribeStore,
    warnings: list[str],
    csv_used_keys: set[tuple[str, str]] | None = None,
) -> dict[str, Any] | None:
    """オブジェクトの describe をストア（メモリ → CSV）か API から取り、 整えて返す。

    戻り値:
        成功時は ``make_slim_object`` で整えた describe。 失敗時（HTTP エラー /
        ``ValueError``）は ``None``。 401 / 403 は ``SalesforceRequestError`` を
        そのまま上位へ送出する（呼び出し側で ID 全体を失敗扱い）。 ストアには
        生の describe を書く（項目定義.csv の列のため）。

    ``csv_used_keys`` を渡すと、 CSV から読んだ （今回 API で取ったものではない）
    ``(org, object_lower)`` のキーをそこに追加する。 取り直しの判定に使う。
    """
    cached = store.lookup(org, name)
    if cached is not None:
        describe = cached.get("describe")
        warning = cached.get("warning")
        if warning:
            warnings.append(warning)
        if csv_used_keys is not None and cached.get("source") == "csv":
            csv_used_keys.add((org.lower(), name.strip().lower()))
        return None if describe is None else make_slim_object(describe)

    try:
        describe = client.describe_object(name)
    except SalesforceRequestError as exc:
        if exc.status_code in FATAL_STATUS_CODES:
            raise
        warning = f"オブジェクト {name} の describe に失敗（HTTP {exc.status_code}: {exc.detail}）"
        store.record_api_failure(org, name, warning)
        warnings.append(warning)
        return None
    except ValueError as exc:
        warning = f"オブジェクト {name} の describe に失敗: {exc}"
        store.record_api_failure(org, name, warning)
        warnings.append(warning)
        return None

    store.record_api_success(org, name, describe)
    return make_slim_object(describe)


def object_key(org: str, object_name: str) -> tuple[str, str]:
    """``(org, object)`` をストレージ用の正規化キーにする （``DescribeStore._key``
    と同じ正規化）。"""
    return (org.strip().lower(), object_name.strip().lower())


def api_name_from_store(store: DescribeStore, key: tuple[str, str]) -> str | None:
    """``(org, object_lower)`` に対応する describe がストアにあれば ``name`` を返す。
    取り直しの API 呼び出しで正しい API 名 （大文字小文字そのまま） を渡すために必要。
    """
    entry = store.memory_cache.get(key) or {}
    describe = entry.get("describe") or store.csv_index.get(key) or {}
    return describe.get("name") or None
