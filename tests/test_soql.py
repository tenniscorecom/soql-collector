from soql_collector.soql import _expand_boolean_filter, compose_soql


def test_boolean_filter_expands_with_precedence() -> None:
    assert _expand_boolean_filter("1 OR (2 AND 3)", {1: "A", 2: "B", 3: "C"}) == "(A OR (B AND C))"


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
