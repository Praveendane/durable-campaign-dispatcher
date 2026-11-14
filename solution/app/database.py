
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker


# Find the project root directory.
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Store the SQLite database in the project root.
DATABASE_URL = f"sqlite:///{PROJECT_ROOT / 'dispatcher.db'}"


# Create the database engine.
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
)


# Enable foreign-key enforcement in SQLite.
@event.listens_for(engine, "connect")
def enable_foreign_keys(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


# Create a base class for all database models.
Base = declarative_base()


# Create a factory for database sessions.
SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
)


def create_tables():
    """Create all tables registered with Base."""
    from solution.app import models
    Base.metadata.create_all(bind=engine)


def get_db():
    """Provide a database session and close it after use."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
