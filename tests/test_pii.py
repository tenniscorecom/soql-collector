"""``pii`` 判定のテスト。"""

from __future__ import annotations

from src import pii


def _ref(
    column_key: str = "Opp.Name",
    label: str = "氏名",
    field_name: str = "Name",
    field_type: str = "string",
    owner_object: str = "Contact",
    resolved: bool = True,
) -> pii.ColumnRef:
    return pii.ColumnRef(
        column_key=column_key,
        label=label,
        field_name=field_name,
        field_type=field_type,
        owner_object=owner_object,
        resolved=resolved,
    )


def _config() -> pii.PIIConfig:
    return pii.PIIConfig(
        keywords=pii.DEFAULT_KEYWORDS,
        person_objects=pii.DEFAULT_PERSON_OBJECTS,
    )


def _objects_with(name: str, fields: list[dict]) -> dict:
    return {name: {"name": name, "fields": fields}}


# ── 型ベースの判定 ────────────────────────────────────────────────────


def test_email_type_on_person_object_is_yes() -> None:
    """``email`` 型の列は 個人オブジェクトでなくても あり。"""
    objects = _objects_with("Opportunity", [{"name": "ContactEmail__c", "type": "email"}])
    result = pii.evaluate_column_pii(
        _ref(
            column_key="Opp.ContactEmail__c",
            label="メール",
            field_name="ContactEmail__c",
            field_type="email",
            owner_object="Opportunity",
        ),
        _config(),
        objects,
    )
    assert result.status == "あり"
    assert "email" in result.basis


def test_phone_type_is_yes() -> None:
    objects = _objects_with("Opportunity", [{"name": "Phone__c", "type": "phone"}])
    result = pii.evaluate_column_pii(
        _ref(
            column_key="Opp.Phone__c",
            label="電話",
            field_name="Phone__c",
            field_type="phone",
            owner_object="Opportunity",
        ),
        _config(),
        objects,
    )
    assert result.status == "あり"
    assert "phone" in result.basis


# ── キーワード＋個人オブジェクト ─────────────────────────────────────


def test_keyword_match_on_contact_object_is_yes() -> None:
    """``Contact.Name`` は キーワード一致 + 個人オブジェクトで あり。"""
    result = pii.evaluate_column_pii(
        _ref(
            column_key="Contact.Name",
            label="氏名",
            field_name="Name",
            field_type="string",
            owner_object="Contact",
        ),
        _config(),
        {},
    )
    assert result.status == "あり"
    assert "キーワード" in result.basis
    assert "氏名" in result.basis


def test_keyword_match_on_account_object_is_review() -> None:
    """``Account.Name`` は キーワード一致するが Account 個人系でないので 要確認。

    ラベルが 「 氏名 」 のように 2 文字以上の日本語キーワードに **部分一致**
    するケースを使う。 1 文字の ``名`` が ``商談名`` などに誤検知しないことを
    別テストで確かめている。
    """
    result = pii.evaluate_column_pii(
        _ref(
            column_key="Account.Name",
            label="氏名",
            field_name="Name",
            field_type="string",
            owner_object="Account",
        ),
        _config(),
        {},
    )
    assert result.status == "要確認"
    assert "個人系" in result.basis
    assert "氏名" in result.basis


def test_account_with_ispersonaccount_is_person() -> None:
    """``Account`` でも ``IsPersonAccount`` 項目があれば 個人系扱い。"""
    objects = _objects_with("Account", [{"name": "IsPersonAccount", "type": "boolean"}])
    result = pii.evaluate_column_pii(
        _ref(
            column_key="Account.Name",
            label="氏名",
            field_name="Name",
            field_type="string",
            owner_object="Account",
        ),
        _config(),
        objects,
    )
    assert result.status == "あり"


# ── 絞り込みだけの列は対応表では未評価 ──────────────────────────────


def test_unresolved_column_is_review() -> None:
    """対応表で対応フィールドが引けなかった列は 要確認。"""
    result = pii.evaluate_column_pii(
        _ref(
            column_key="Opp.Unknown",
            label="不明列",
            field_name="",
            field_type="",
            owner_object="",
            resolved=False,
        ),
        _config(),
        {},
    )
    assert result.status == "要確認"
    assert "未解決" in result.basis


def test_no_keyword_no_type_is_empty() -> None:
    """キーワードにも型にも該当しない列は 空。"""
    result = pii.evaluate_column_pii(
        _ref(
            column_key="Opp.Amount",
            label="金額",
            field_name="Amount",
            field_type="currency",
            owner_object="Opportunity",
        ),
        _config(),
        {},
    )
    assert result.status == ""


# ── 集約 ─────────────────────────────────────────────────────────────


def test_aggregate_yes_beats_review() -> None:
    """1 列でも あり があれば レポート全体は あり。"""
    refs = [
        _ref(
            column_key="Contact.Name",
            label="氏名",
            field_name="Name",
            field_type="string",
            owner_object="Contact",
        ),
        _ref(
            column_key="Account.Name",
            label="取引先名",
            field_name="Name",
            field_type="string",
            owner_object="Account",
        ),
    ]
    result = pii.evaluate_report_pii(refs, _config(), {})
    assert result.status == "あり"
    # 該当列は表示名(根拠) の形
    assert any("氏名" in label for label, _ in result.matching_columns)


def test_aggregate_review_when_no_yes() -> None:
    """あり が無く 要確認 があれば レポート全体は 要確認。"""
    refs = [
        _ref(
            column_key="Account.Name",
            label="氏名",
            field_name="Name",
            field_type="string",
            owner_object="Account",
        ),
    ]
    result = pii.evaluate_report_pii(refs, _config(), {})
    assert result.status == "要確認"


def test_aggregate_none_when_all_empty() -> None:
    refs = [
        _ref(
            column_key="Opp.Amount",
            label="金額",
            field_name="Amount",
            field_type="currency",
            owner_object="Opportunity",
        ),
    ]
    result = pii.evaluate_report_pii(refs, _config(), {})
    assert result.status == "なし"
    assert result.matching_columns == ()


def test_aggregate_matching_columns_capped_at_10() -> None:
    """該当列が 10 件を超えると 最大 10 件で切る。"""
    refs = [
        _ref(
            column_key=f"Account.Name{i}",
            label=f"氏名{i}",
            field_name="Name",
            field_type="string",
            owner_object="Account",
        )
        for i in range(20)
    ]
    result = pii.evaluate_report_pii(refs, _config(), {})
    assert len(result.matching_columns) == pii.MATCHING_COLUMNS_LIMIT


# ── 設定でキーワードを置き換える ─────────────────────────────────────


def test_keyword_override_marks_matching_column() -> None:
    """``config.keywords`` を独自にすると、 その言葉に一致した列が拾われる。"""
    config = pii.PIIConfig(keywords=("機密",), person_objects=("Contact",))
    result = pii.evaluate_column_pii(
        _ref(
            column_key="Custom.Secret__c",
            label="機密フラグ",
            field_name="Secret__c",
            field_type="string",
            owner_object="Contact",
        ),
        config,
        {},
    )
    assert result.status == "あり"


def test_keyword_override_ignores_unrelated() -> None:
    """既定のキーワードを消すと、 既定では一致していた ``Name`` が要確認/空になる。

    ``name`` 系のキーワードを消しても、 ``Contact.Name`` のように
    個人系オブジェクトで ``Name`` フィールドのときは 個人系 ``Name`` というより
    個人オブジェクトに紐づくもの。 キーワード一致が消えれば 判定は空。
    """
    config = pii.PIIConfig(keywords=("電話",), person_objects=("Contact",))
    result = pii.evaluate_column_pii(
        _ref(
            column_key="Contact.Name",
            label="氏名",
            field_name="Name",
            field_type="string",
            owner_object="Contact",
        ),
        config,
        {},
    )
    # キーワード 「 電話 」 には一致しないので 型も無しで 空
    assert result.status == ""


# ── キーワード一致ルール （ 1 文字日本語 / ASCII の区別 ） ──────────────


def test_opportunity_name_is_not_review_by_single_char_keyword() -> None:
    """``Opportunity.Name`` で表示名が ``商談名``: キーワード ``名`` （ 1 文字 ）
    に部分一致しないので、 商談レポートが全部 「 要確認 」 に倒れない。
    """
    result = pii.evaluate_column_pii(
        _ref(
            column_key="Opp.Name",
            label="商談名",
            field_name="Name",
            field_type="string",
            owner_object="Opportunity",
        ),
        _config(),
        {},
    )
    assert result.status == ""


def test_full_name_label_matches_japanese_two_char_keyword() -> None:
    """表示名 ``氏名`` は 2 文字の日本語キーワード ``氏名`` に部分一致して ``あり`` 。"""
    result = pii.evaluate_column_pii(
        _ref(
            column_key="Contact.Name",
            label="氏名",
            field_name="Name",
            field_type="string",
            owner_object="Contact",
        ),
        _config(),
        {},
    )
    assert result.status == "あり"
    assert "氏名" in result.basis


def test_single_char_japanese_label_matches_exactly() -> None:
    """表示名が ``名`` や ``姓`` そのもののときは 1 文字キーワードに **完全一致**
    するので ``あり`` （ Contact ） になる。 個人系オブジェクトでないときは
    ``要確認`` 。
    """
    name_result = pii.evaluate_column_pii(
        _ref(
            column_key="Contact.Mei",
            label="名",
            field_name="Mei",
            field_type="string",
            owner_object="Contact",
        ),
        _config(),
        {},
    )
    assert name_result.status == "あり"
    assert "キーワード『名』" in name_result.basis

    sei_result = pii.evaluate_column_pii(
        _ref(
            column_key="Contact.Sei",
            label="姓",
            field_name="Sei",
            field_type="string",
            owner_object="Contact",
        ),
        _config(),
        {},
    )
    assert sei_result.status == "あり"
    assert "キーワード『姓』" in sei_result.basis


def test_ascii_partial_match_in_field_name_does_not_false_positive() -> None:
    """``Capacity`` / ``Estate`` / ``Velocity``: 短い英単語が部分一致で
    ``city`` / ``state`` に誤検知しない。
    """
    capacity = pii.evaluate_column_pii(
        _ref(
            column_key="Opportunity.Capacity",
            label="容量",
            field_name="Capacity",
            field_type="string",
            owner_object="Opportunity",
        ),
        _config(),
        {},
    )
    assert capacity.status == ""

    estate = pii.evaluate_column_pii(
        _ref(
            column_key="Custom.Estate",
            label="不動産",
            field_name="Estate",
            field_type="string",
            owner_object="Custom__c",
        ),
        _config(),
        {},
    )
    assert estate.status == ""

    velocity = pii.evaluate_column_pii(
        _ref(
            column_key="Opportunity.Velocity",
            label="速度",
            field_name="Velocity",
            field_type="string",
            owner_object="Opportunity",
        ),
        _config(),
        {},
    )
    assert velocity.status == ""


def test_ascii_keywords_match_on_token_boundary() -> None:
    """ASCII キーワードは **トークン** で一致する。 ``BillingCity`` 、
    ``City__c`` 、 ``Mailing State``、 ``Birthdate`` などにヒットする。

    ここでは日本側のキーワードが先に当たらないよう、 表示名を英語寄りにして
    ASCII マッチの根拠を見えるようにしている。
    """
    # ``city`` キーワードは ``BillingCity`` / ``City__c`` にトークン一致
    billing_city = pii.evaluate_column_pii(
        _ref(
            column_key="Account.BillingCity",
            label="Billing City",
            field_name="BillingCity",
            field_type="string",
            owner_object="Account",
        ),
        _config(),
        {},
    )
    assert billing_city.status == "要確認"
    assert "city" in billing_city.basis.lower()

    city_custom = pii.evaluate_column_pii(
        _ref(
            column_key="Custom.City__c",
            label="City",
            field_name="City__c",
            field_type="string",
            owner_object="Custom__c",
        ),
        _config(),
        {},
    )
    assert city_custom.status == "要確認"
    assert "city" in city_custom.basis.lower()

    # ``state`` キーワードは ``Mailing State`` （ 表示名 ） にトークン一致
    mailing_state = pii.evaluate_column_pii(
        _ref(
            column_key="Contact.MailingState",
            label="Mailing State",
            field_name="MailingState",
            field_type="string",
            owner_object="Contact",
        ),
        _config(),
        {},
    )
    assert mailing_state.status == "あり"
    assert "state" in mailing_state.basis.lower()

    # ``birth`` は ``Birthdate`` のように **先頭一致** も許す
    birthdate = pii.evaluate_column_pii(
        _ref(
            column_key="Contact.Birthdate",
            label="Birthdate",
            field_name="Birthdate",
            field_type="string",
            owner_object="Contact",
        ),
        _config(),
        {},
    )
    assert birthdate.status == "あり"
    assert "birth" in birthdate.basis.lower()


def test_ascii_keywords_match_standard_salesforce_fields() -> None:
    """``Phone`` / ``MobilePhone`` / ``Email`` / ``PersonEmail`` のような
    標準的な Salesforce 項目は ASCII キーワードでヒットする。

    日本側のキーワードが先に当たらないよう ラベルを英語寄りにして ASCII
    マッチの根拠を見えるようにしている。
    """
    phone = pii.evaluate_column_pii(
        _ref(
            column_key="Contact.Phone",
            label="Phone Number",
            field_name="Phone",
            field_type="string",
            owner_object="Contact",
        ),
        _config(),
        {},
    )
    assert phone.status == "あり"
    assert "phone" in phone.basis.lower()

    mobile = pii.evaluate_column_pii(
        _ref(
            column_key="Contact.MobilePhone",
            label="Mobile Phone",
            field_name="MobilePhone",
            field_type="string",
            owner_object="Contact",
        ),
        _config(),
        {},
    )
    # ``MobilePhone`` は ``mobile`` と ``phone`` の両方にトークン一致する。 先に
    # 当たるのは ``DEFAULT_KEYWORDS`` の順で先に出ている ``phone`` の方
    assert mobile.status == "あり"
    assert "phone" in mobile.basis.lower()

    email = pii.evaluate_column_pii(
        _ref(
            column_key="Contact.Email",
            label="Email Address",
            field_name="Email",
            field_type="string",
            owner_object="Contact",
        ),
        _config(),
        {},
    )
    # 型は ``string`` にしてあるので、 キーワード一致で ``あり`` になる
    assert email.status == "あり"
    assert "email" in email.basis.lower()

    person_email = pii.evaluate_column_pii(
        _ref(
            column_key="Contact.PersonEmail",
            label="Person Email",
            field_name="PersonEmail",
            field_type="string",
            owner_object="Contact",
        ),
        _config(),
        {},
    )
    assert person_email.status == "あり"
    assert "email" in person_email.basis.lower()
