"""個人情報（ PII ） の判定ロジック（ 純粋関数 ）。

レポートの **出力に出る列** （ detailColumns / groupingsDown / groupingsAcross /
集計対象列 ） だけを対象にし、 絞り込みにだけ使う列は対象外。 判定はあくまで
機械的な目安で、 最終判断は人が行う。 曖昧なものは 「 要確認 」 に倒す
（ 「 なし 」 に倒さない ）。

判定ルール（ 列単位 ）:

- ``あり``
  - 項目の型が ``email`` / ``phone`` / ``address`` / ``location``
  - 名前 / ラベルがキーワードに一致 **かつ** 所属オブジェクトが個人系
- ``要確認``
  - キーワードに一致するが所属オブジェクトが個人系でない
  - 対応表で解決できていない列
- ``空``
  - 上のどれでもない

キーワード一致のさせ方は **単純な部分一致ではなく、 3 段階** で判定する:

- **1 文字の日本語キーワード** （ ``姓`` / ``名`` ）: 表示名が **完全一致**
  するときだけ一致 （ 前後の空白は除く ）。 API 名には適用しない。
  ``商談名`` は ``名`` に **当たらない**
- **ASCII キーワード** （ ``email`` / ``phone`` / ``city`` / ``state`` /
  ``zip`` / ``fax`` / ``birth`` / ``gender`` / ``mobile`` / ``street`` /
  ``postal`` ）: API 名・表示名を **トークン** に分解 （ ``_`` / ``__c`` /
  空白 / camelCase の切れ目 `BillingCity` → `Billing` `City` /
  数字との切れ目 ） して、 トークンがキーワードに **一致** するとき一致する。
  ``birth`` だけはトークンの **先頭一致** も許す
- **2 文字以上の日本語キーワード** （ ``氏名`` / ``名前`` / ``メール`` など ）
  は従来どおり **部分一致** （ 大文字小文字を無視 ）

レポート単位の集約は あり > 要確認 > なし。 該当列（ 表示名(根拠) ） は最大
10 件。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

# 個人情報とみなす Salesforce 項目の型
PII_TYPES: frozenset[str] = frozenset({"email", "phone", "address", "location"})

# 既定のキーワード（ 1 文字日本語は表示名完全一致、 ASCII は API 名・表示名を
# トークン分解してトークン一致、 2 文字以上日本語は部分一致 ）
DEFAULT_KEYWORDS: tuple[str, ...] = (
    "氏名",
    "名前",
    "姓",
    "名",
    "フリガナ",
    "カナ",
    "メール",
    "email",
    "電話",
    "phone",
    "mobile",
    "fax",
    "住所",
    "street",
    "city",
    "state",
    "postal",
    "zip",
    "郵便",
    "生年月日",
    "birth",
    "性別",
    "gender",
    "マイナンバー",
    "個人番号",
    "口座",
    "免許",
    "パスポート",
    "年収",
    "給与",
)

# 既定の個人系オブジェクト
DEFAULT_PERSON_OBJECTS: tuple[str, ...] = (
    "Contact",
    "Lead",
    "User",
    "Individual",
    "PersonAccount",
)

# Account を個人系とみなす判定に使う項目名
_IS_PERSON_ACCOUNT_FIELD = "IsPersonAccount"

# 調査表の 該当列に出す件数の上限
MATCHING_COLUMNS_LIMIT = 10


@dataclass(frozen=True)
class PIIConfig:
    """PII 判定の設定。 ``settings.py`` から渡される。"""

    keywords: tuple[str, ...]
    person_objects: tuple[str, ...]


@dataclass(frozen=True)
class ColumnPIIResult:
    """列単位の PII 判定結果。"""

    status: str  # "あり" / "要確認" / ""
    basis: str  # "型 email" / "キーワード『電話』" / "未解決の列" など
    label: str  # 調査表の 該当列 にそのまま表示する


@dataclass(frozen=True)
class ReportPIIResult:
    """レポート単位の PII 集約結果。"""

    status: str  # "あり" / "要確認" / "なし"
    matching_columns: tuple[tuple[str, str], ...]  # (表示名, 根拠) のリスト


@dataclass(frozen=True)
class ColumnRef:
    """PII 判定の入力 1 件分。 レポートの出力に出る列を 1 本ずつ。"""

    column_key: str  # "Opp.Name" 等
    label: str  # 表示名
    field_name: str  # 解決できた API 名。 未解決なら ""
    field_type: str  # 解決できた項目の型。 未解決なら ""
    owner_object: str  # 解決できた所属オブジェクト。 未解決なら ""
    resolved: bool  # 対応表で対応フィールドが引けたか


def evaluate_column_pii(
    ref: ColumnRef,
    config: PIIConfig,
    objects: dict[str, Any],
) -> ColumnPIIResult:
    """1 列ぶんの PII 判定をする（ 純粋関数 ）。

    Args:
        ref: 判定対象の列。 解決済みなら ``resolved=True`` で ``field_name`` /
            ``field_type`` / ``owner_object`` が入る。 未解決なら ``resolved=False``
            で ``field_name`` 以降は空文字。
        config: キーワードと個人系オブジェクトの設定。
        objects: レポートが取れたオブジェクトの describe （ 個人系の動的判定に使う ）。
    """
    label = ref.label or ref.column_key

    if not ref.resolved:
        return ColumnPIIResult(status="要確認", basis="未解決の列", label=label)

    field_type_lower = (ref.field_type or "").lower()
    if field_type_lower in PII_TYPES:
        return ColumnPIIResult(
            status="あり",
            basis=f"型 {field_type_lower}",
            label=label,
        )

    matched_keyword = _match_keyword(ref.field_name, ref.label, config.keywords)
    if matched_keyword is not None:
        if _is_person_owner(ref.owner_object, objects, config.person_objects):
            return ColumnPIIResult(
                status="あり",
                basis=f"キーワード『{matched_keyword}』",
                label=label,
            )
        return ColumnPIIResult(
            status="要確認",
            basis=f"キーワード『{matched_keyword}』(個人系オブジェクトでない)",
            label=label,
        )

    return ColumnPIIResult(status="", basis="", label=label)


def evaluate_report_pii(
    refs: Iterable[ColumnRef],
    config: PIIConfig,
    objects: dict[str, Any],
) -> ReportPIIResult:
    """レポート単位の PII 集約。

    集約ルール:

    - 1 列でも ``あり`` なら レポート全体も ``あり``
    - ``あり`` が無く 1 列でも ``要確認`` なら ``要確認``
    - 全部 ``空`` なら ``なし``
    """
    statuses: list[str] = []
    matches: list[tuple[str, str]] = []
    for ref in refs:
        result = evaluate_column_pii(ref, config, objects)
        statuses.append(result.status)
        if result.status in ("あり", "要確認"):
            matches.append((result.label, result.basis))

    if any(s == "あり" for s in statuses):
        status = "あり"
    elif any(s == "要確認" for s in statuses):
        status = "要確認"
    else:
        status = "なし"

    return ReportPIIResult(
        status=status,
        matching_columns=tuple(matches[:MATCHING_COLUMNS_LIMIT]),
    )


# ── 内部ヘルパー ────────────────────────────────────────────────────────


def _match_keyword(field_name: str, label: str, keywords: Iterable[str]) -> str | None:
    """API 名と表示名のどちらかにキーワードが一致するかを返す。 一致した
    キーワードを返す（ 根拠用 ）。

    判定ルール:

    - 1 文字の日本語キーワード（ ``姓`` / ``名`` など ）: 表示名が
      **キーワードと完全一致** するときだけ一致とする （ 前後の空白は除く ）。
      API 名には適用しない
    - ASCII キーワード（ ``email`` / ``phone`` / ``city`` / ``state`` /
      ``zip`` / ``fax`` / ``birth`` / ``gender`` / ``mobile`` / ``street`` /
      ``postal`` など ）: API 名・表示名を **トークン** に分解し、 いずれかの
      トークンがキーワードに **一致** するとき一致とする。 ``birth`` だけは
      トークンの **先頭一致** も許す （ ``Birthdate`` / ``BirthDate`` /
      ``Birth_Date`` などにヒット ）
    - 上記以外のキーワード（ 2 文字以上の日本語 ）: 表示名または API 名に
      **部分一致** するかを従来どおり見る

    なぜ 単純な ``in`` での部分一致にしないか:
    - ``商談名`` がキーワード ``名`` に当たって 商談レポートが全部 「 要確認 」
      になるため
    - ``Capacity`` が ``city`` に、 ``Estate`` が ``state`` に当たるため
      （ いずれも短い英単語の部分一致 ）
    """
    field_tokens = _tokenize_identifier(field_name)
    label_tokens = _tokenize_identifier(label)
    label_stripped = (label or "").strip()
    for keyword in keywords:
        if not keyword:
            continue
        if keyword.isascii():
            # ASCII キーワード: トークンに一致したときだけ採用。 ``birth`` だけは
            # 先頭一致を許す （ Birthdate / Birth_Date にヒットさせるため ）
            keyword_lower = keyword.lower()
            if keyword_lower == "birth":
                if any(t.lower().startswith("birth") for t in field_tokens + label_tokens):
                    return keyword
                continue
            if any(t.lower() == keyword_lower for t in field_tokens + label_tokens):
                return keyword
            continue
        if len(keyword) == 1:
            # 日本語 1 文字: 表示名の完全一致だけ （ API 名は見ない ）
            if label_stripped == keyword:
                return keyword
            continue
        # 日本語 2 文字以上: 大文字小文字を無視した部分一致
        keyword_lower = keyword.lower()
        needles: tuple[str, ...] = (field_name or "", label or "")
        for needle in needles:
            if needle and keyword_lower in needle.lower():
                return keyword
    return None


# camelCase と数字の切れ目を入れるための正規表現。 ``BillingCity`` →
# ``Billing City`` 、 ``BirthDate`` → ``Birth Date`` 、 ``PersonEmail2`` →
# ``Person Email 2`` のようになる
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z])(?=[A-Z])")
_CAMEL_ACRONYM_BOUNDARY = re.compile(r"(?<=[A-Z])(?=[A-Z][a-z])")
_LETTER_DIGIT_BOUNDARY = re.compile(r"(?<=[a-zA-Z])(?=\d)")
_DIGIT_LETTER_BOUNDARY = re.compile(r"(?<=\d)(?=[a-zA-Z])")
_SPLIT_ON_UNDERSCORE_OR_SPACE = re.compile(r"[_\s]+")


def _tokenize_identifier(value: object) -> list[str]:
    """API 名の様な文字列を ASCII レターの **トークン** に分解する。

    分解ルール:

    - Salesforce のカスタム接尾辞 ``__c`` は落とす
    - ``_`` と空白で先に切る （ ``Mailing_State`` → ``Mailing`` / ``State`` ）
    - camelCase の切れ目で切る （ ``BillingCity`` → ``Billing`` / ``City`` ）
    - ASCII レターと数字の切れ目で切る （ ``PersonEmail2`` → ``Person`` /
      ``Email`` / ``2`` ）
    - 結果のトークン列から、 数字だけ ・ 非 ASCII のものを除く

    戻り値の例:

    - ``BillingCity`` → ``["Billing", "City"]``
    - ``City__c`` → ``["City"]``
    - ``Mailing State`` → ``["Mailing", "State"]``
    - ``Birth_Date`` → ``["Birth", "Date"]``
    - ``PersonEmail2__c`` → ``["Person", "Email"]``
    - ``""`` / ``None`` → ``[]``
    """
    if not isinstance(value, str) or not value:
        return []
    cleaned = value.replace("__c", "")
    tokens: list[str] = []
    for piece in _SPLIT_ON_UNDERSCORE_OR_SPACE.split(cleaned):
        if not piece:
            continue
        # camelCase と頭字語 （ ``URLPath`` → ``URL Path`` ） の切れ目
        piece = _CAMEL_ACRONYM_BOUNDARY.sub(" ", piece)
        piece = _CAMEL_BOUNDARY.sub(" ", piece)
        piece = _LETTER_DIGIT_BOUNDARY.sub(" ", piece)
        piece = _DIGIT_LETTER_BOUNDARY.sub(" ", piece)
        for token in piece.split():
            if token.isdigit():
                continue
            # ASCII レターを含むトークンだけ残す （ 日本語ラベルに混ざった
            # 英字だけ拾うイメージ ）。 非 ASCII の文字が含まれていたら捨てる。
            if not token.isascii():
                continue
            if any(ch.isalpha() for ch in token):
                tokens.append(token)
    return tokens


def _is_person_owner(
    owner_object: str,
    objects: dict[str, Any],
    person_objects: Iterable[str],
) -> bool:
    """所属オブジェクトが個人系かを判定する。

    - ``owner_object`` が ``person_objects`` のいずれか
    - ``owner_object`` が ``Account`` で ``IsPersonAccount`` 項目を持つ
    """
    if not owner_object:
        return False
    if owner_object in person_objects:
        return True
    if owner_object == "Account":
        describe = objects.get("Account")
        if isinstance(describe, dict):
            fields = describe.get("fields")
            if isinstance(fields, list):
                for field in fields:
                    if not isinstance(field, dict):
                        continue
                    name = field.get("name")
                    if isinstance(name, str) and name == _IS_PERSON_ACCOUNT_FIELD:
                        return True
    return False
