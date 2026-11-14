
from sqlalchemy import inspect

from solution.app.database import engine, create_tables


def test_database_tables_are_created():
    create_tables()

    inspector = inspect(engine)
    tables = inspector.get_table_names()

    expected_tables = {
        "campaigns",
        "leads",
        "calls",
        "webhook_events",
        "phone_suppressions",
    }

    assert expected_tables.issubset(set(tables))
