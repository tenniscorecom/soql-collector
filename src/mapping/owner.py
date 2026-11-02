"""所有者を User とみなして解決する判定と、 その補助。

Salesforce の ``Owner`` 項目はポリモーフィックで、 ``referenceTo`` に
``Group`` （キュー） と ``User`` の両方が並ぶことがある。 レポートの表示は
所有者を User として見せるため、 所有者系の項目は **User を先に** 試す。

判定は ``is_owner_user_field()`` 1 か所にまとめ、 ``src/mapping/column_key.py``
と ``src/column_fix_xlsx.py`` の両方から使う。 どちらの経路でも同じ判定を
使わないと、 列キー経由と 「列の直し.xlsx」 経由で結果が食い違う。
"""

from __future__ import annotations

from typing import Any

# 所有者項目のリレーション名
OWNER_RELATIONSHIP_NAME = "Owner"

# 所有者を指すオブジェクト
OWNER_USER_OBJECT = "User"

# User だけが持っている共通項目。 ``Owner.<X>`` の X がここに無いと、
# User の項目を引いた SOQL は実行時に失敗する（キュー等を指せるため）。
OWNER_COMMON_FIELDS: frozenset[str] = frozenset(
    {
        "Alias",
        "Email",
        "FirstName",
        "Id",
        "IsActive",
        "LastName",
        "MiddleName",
        "Name",
        "Phone",
        "Profile",
        "ProfileId",
        "Suffix",
        "Title",
        "Type",
        "Username",
        "UserRole",
        "UserRoleId",
    }
)

# Owner がポリモーフィックなときの注意書き。 備考と checks に出す。
OWNER_POLYMORPHIC_NOTE = "Owner はキュー等も指せるため User だけの項目は SOQL で失敗する場合あり"

# 表示名の接頭辞が所有者を指すときの終わり方（正規化後）
OWNER_LABEL_SUFFIX_JA = "所有者"
OWNER_LABEL_SUFFIX_EN = "owner"

# 所有者の列キーのうち、 そのまま ``Owner.Name`` にするもの
OWNER_NAME_COLUMN_KEYS: frozenset[str] = frozenset(
    {
        "OWNER",
        "OWNER_NAME",
        "OWNER_FULL_NAME",
        "CUST_OWNER_NAME",
    }
)


def is_owner_user_field(field: dict[str, Any]) -> bool:
    """所有者を User とみなす項目か判定する。

    ``relationshipName`` が ``Owner`` で、 かつ ``referenceTo`` に ``User`` が
    含まれるときだけ True。 他のポリモーフィック参照は対象外なので、
    それらの挙動は変わらない。

    Args:
        field: Object Describe の ``fields`` の 1 件。

    Returns:
        所有者を User とみなす項目なら True。
    """
    if field.get("relationshipName") != OWNER_RELATIONSHIP_NAME:
        return False
    return any(
        str(target).lower() == OWNER_USER_OBJECT.lower()
        for target in (field.get("referenceTo") or [])
    )


def owner_first_reference_targets(field: dict[str, Any]) -> list[str]:
    """参照先の候補を並べる。 所有者を User とみなす項目は ``User`` が先頭。

    他の項目では ``referenceTo`` の並びをそのまま返す。

    Args:
        field: Object Describe の ``fields`` の 1 件。

    Returns:
        参照先オブジェクト名のリスト（重複除去はしない）。
    """
    reference_to = list(field.get("referenceTo") or [])
    if not is_owner_user_field(field):
        return reference_to
    user_index = next(
        (
            index
            for index, target in enumerate(reference_to)
            if str(target).lower() == OWNER_USER_OBJECT.lower()
        ),
        None,
    )
    if user_index is None:
        return reference_to
    user_target = reference_to[user_index]
    rest = [*reference_to[:user_index], *reference_to[user_index + 1 :]]
    return [user_target, *rest]


def is_owner_label_prefix(prefix: str) -> bool:
    """表示名の接頭辞が所有者を指すか判定する。

    正規化 （ 前後空白を落として小文字化 ） した後、 ``所有者`` で終わるか
    ``owner`` で終わるかを見る。 例: ``所有者`` / ``ケース所有者`` /
    ``Opportunity Owner`` 。

    Args:
        prefix: 表示名のコロン前の部分。

    Returns:
        所有者を指す接頭辞なら True。
    """
    normalized = prefix.strip().lower()
    return normalized.endswith(OWNER_LABEL_SUFFIX_JA) or normalized.endswith(OWNER_LABEL_SUFFIX_EN)
