"""SQLAlchemy declarative base for all ORM models."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Base class for all database models.

    All ORM models should inherit from this class so that Alembic
    migrations and table creation work correctly.
    """

    pass
