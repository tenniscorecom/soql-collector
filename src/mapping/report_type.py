"""``reportType.type`` から主オブジェクト候補を取り出す純粋関数。

カスタムレポートタイプで ``$`` や ``@`` を含む値が入ることがあるため、
``describe_object()`` にそのまま渡せない。 ここでは ``$`` / ``@`` で区切って
候補のリストにし、 先頭から ``describe_object()`` が通るものを採る前処理に
``report_type_candidates()`` を提供する。
"""

from __future__ import annotations

import re

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
      （ ``CustomEntity`` / ``CustomEntityCustomEntity`` など ） を除く
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
        token
        for token in tokens
        if token and not _CUSTOM_ENTITY_TOKEN_PATTERN.fullmatch(token)
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
    （ または候補が空だった ） ときの理由文言を組み立てる。

    元の ``reportType.type`` の値と試した候補の一覧を添え、 「 手動で確認 」
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
