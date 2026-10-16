"""comken から移した、 レポートの列 ⇔ Salesforce フィールド対応表の組み立て。

``report_type_candidates()`` がカスタムレポートタイプの ``$`` / ``@`` を
含む値から主オブジェクト候補を抽出し、 ``build_column_map()`` が渡された
describe から列対応表を作る。

``build_column_map()`` は主オブジェクトに加えて、 関連オブジェクトの
describe と relations を受け取り、 関連オブジェクトを跨ぐ列も解決する。

列キーが ``主オブジェクト.`` で始まる形 （ 例:
``Contact.Account.Name`` ） については、 表示名ではなく列キー自体から
主オブジェクトをたどって SOQL 列名を決める。 カスタムレポートタイプで
人が主オブジェクトを知らなくても SOQL 組立に必要な列名を引ける。

ファイルを ``src.mapping`` パッケージに分割 （ 手順ごとにサブモジュール ） 。
公開 API は再エクスポートしているので、
``from src.mapping import X`` の既存の import 先は全てそのまま動く。
"""

from __future__ import annotations

# 公開 API を再エクスポートする。 既存の ``from src.mapping import X`` を
# 壊さないことが最重要 （ ``test_mapping.py`` などで 直接 import される ）
from src.mapping.chains import _ChainCandidate, build_relation_chains
from src.mapping.column_key import (
    COLUMN_KEY_PATH_MAX_DEPTH,
    candidate_relationship_names,
    resolve_column_key_to_soql,
    starts_with_main_object,
)
from src.mapping.column_map import build_column_map
from src.mapping.columns import (
    collect_column_info,
    collect_describable_columns,
    field_name_from_soql,
    output_column_keys,
    select_column_keys,
)
from src.mapping.report_type import candidate_failure_reason, report_type_candidates
from src.mapping.row_status import column_candidates, column_source, column_status

__all__ = [
    # --- report_type ---
    "report_type_candidates",
    "candidate_failure_reason",
    # --- columns ---
    "collect_describable_columns",
    "select_column_keys",
    "output_column_keys",
    "collect_column_info",
    "field_name_from_soql",
    # --- column_key ---
    "resolve_column_key_to_soql",
    "starts_with_main_object",
    "candidate_relationship_names",
    "COLUMN_KEY_PATH_MAX_DEPTH",
    # --- chains ---
    "build_relation_chains",
    "_ChainCandidate",
    # --- column_map ---
    "build_column_map",
    # --- row_status ---
    "column_source",
    "column_status",
    "column_candidates",
    # --- 互換用エイリアス ---
    "_resolve_column_key_to_soql",
]

# 古い名前 （ テスト等で import されている可能性がある ） を `_` 接頭辞なしで
# 公開する互換用エイリアス。 既存挙動を変えないため、 リネームはしない。
_resolve_column_key_to_soql = resolve_column_key_to_soql
