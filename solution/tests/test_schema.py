
from sqlalchemy import inspect

from solution.app.database import engine, create_tables


def test_database_schema():
    create_tables()
    inspector = inspect(engine)

    # Verify the lead table uses a composite primary key.
    lead_pk = inspector.get_pk_constraint("leads")
    assert set(lead_pk["constrained_columns"]) == {
        "campaign_id",
        "lead_id",
    }

    # Verify provider request IDs are unique.
    call_indexes = inspector.get_indexes("calls")
    call_constraints = inspector.get_unique_constraints("calls")

    unique_columns = {
        column
        for index in call_indexes
        if index.get("unique")
        for column in index["column_names"]
    }

    unique_columns.update(
        column
        for constraint in call_constraints
        for column in constraint["column_names"]
    )

    assert "request_id" in unique_columns

    # Verify phone suppression uses phone_hash as its primary key.
    suppression_pk = inspector.get_pk_constraint("phone_suppressions")
    assert suppression_pk["constrained_columns"] == ["phone_hash"]
