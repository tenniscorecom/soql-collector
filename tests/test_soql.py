from soql_collector.soql import (
    _build_select_clause,
    _expand_boolean_filter,
    _filter_to_condition,
    _format_raw_aggregation,
    _format_raw_cross_filters,
    _format_raw_filters,
    compose_soql,
)


def test_boolean_filter_expands_with_precedence() -> None:
    assert _expand_boolean_filter("1 OR (2 AND 3)", {1: "A", 2: "B", 3: "C"}) == "(A OR (B AND C))"


def test_format_raw_filters() -> None:
    filters = [
        {"column": "A.NAME", "operator": "equals", "value": "Acme"},
        {"column": "A.TYPE", "operator": "notEqual", "value": None},
    ]
    assert _format_raw_filters(filters) == "A.NAME=equals:Acme; A.TYPE=notEqual:"


def test_format_raw_filters_handles_non_dict() -> None:
    assert _format_raw_filters(["broken"]) == "(不正な要素)"


def test_format_raw_aggregation_with_groupings() -> None:
    metadata = {
        "aggregates": ["RowCount", "s!Amount"],
        "groupingsDown": [{"name": "Owner", "sortOrder": "Desc"}, {"name": "Type"}],
    }
    assert (
        _format_raw_aggregation(metadata)
        == "aggregates: RowCount, s!Amount | groupingsDown: Owner(Desc), Type"
    )


def test_format_raw_cross_filters() -> None:
    cross_filters = [
        {
            "relatedEntity": "Contact",
            "operation": "with",
            "criteria": [{"column": "C.NAME", "operator": "contains", "value": "O'Hara"}],
        }
    ]
    assert _format_raw_cross_filters(cross_filters) == (
        "relatedEntity=Contact, operation=with, criteria=[C.NAME=contains:O'Hara]"
    )


def test_build_select_clause_skips_unknown_columns() -> None:
    notes: list[str] = []
    result = _build_select_clause(["A.NAME", "A.UNKNOWN"], {"A.NAME": ("Name", "string")}, notes)
    assert result == "Name"
    assert notes == ["列キー未解決: A.UNKNOWN"]


def test_filter_to_condition_like() -> None:
    assert _filter_to_condition("Name", "string", "contains", "O'Hara") == (
        "Name LIKE '%O\\'Hara%'"
    )


def test_filter_to_condition_starts_with() -> None:
    assert _filter_to_condition("Name", "string", "startsWith", "Acme") == "Name LIKE 'Acme%'"


def test_filter_to_condition_not_equal() -> None:
    assert _filter_to_condition("Name", "string", "notEqual", "Acme") == "Name != 'Acme'"


def test_filter_to_condition_unknown_operator_returns_none() -> None:
    assert _filter_to_condition("Name", "string", "includes", "Acme") is None


def test_compose_tabular_soql() -> None:
    metadata = {
        "reportMetadata": {
            "reportFormat": "TABULAR",
            "reportType": {"type": "Account"},
            "detailColumns": ["A.NAME"],
            "reportFilters": [{"column": "A.NAME", "operator": "contains", "value": "O'Hara"}],
        }
    }
    draft = compose_soql(metadata, {"A.NAME": ("Name", "string")})
    assert draft.soql == "SELECT Name FROM Account WHERE Name LIKE '%O\\'Hara%'"
    assert draft.is_complete


def test_summary_requires_manual_group_by() -> None:
    draft = compose_soql(
        {"reportMetadata": {"reportFormat": "SUMMARY", "reportType": {"type": "Account"}}}, {}
    )
    assert not draft.is_complete
    assert "GROUP BY 手動組立要" in draft.notes


def test_compose_soql_with_cross_filters_blocked() -> None:
    draft = compose_soql(
        {
            "reportMetadata": {
                "reportFormat": "TABULAR",
                "reportType": {"type": "Account"},
                "crossFilters": [{"relatedEntity": "Contact"}],
            }
        },
        {},
    )
    assert not draft.is_complete
    assert "crossFiltersは個別対応要" in draft.notes


def test_compose_soql_with_summary_blocked() -> None:
    draft = compose_soql(
        {"reportMetadata": {"reportFormat": "SUMMARY", "reportType": {"type": "Account"}}},
        {},
    )
    assert not draft.is_complete
    assert "GROUP BY 手動組立要" in draft.notes


def test_compose_soql_boolean_filter_expanded() -> None:
    metadata = {
        "reportMetadata": {
            "reportFormat": "TABULAR",
            "reportType": {"type": "Account"},
            "reportFilters": [
                {"column": "A.NAME", "operator": "equals", "value": "Acme"},
                {"column": "A.TYPE", "operator": "equals", "value": "Customer"},
            ],
            "reportBooleanFilter": "1 OR 2",
        }
    }
    mappings = {"A.NAME": ("Name", "string"), "A.TYPE": ("Type", "string")}
    draft = compose_soql(metadata, mappings)
    assert draft.soql == "SELECT Id FROM Account WHERE (Name = 'Acme' OR Type = 'Customer')"
    assert draft.is_complete
