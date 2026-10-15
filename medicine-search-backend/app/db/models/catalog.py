import uuid
from datetime import datetime

from sqlalchemy import (
    String,
    Text,
    Boolean,
    Numeric,
    Integer,
    DateTime,
    ForeignKey,
    Index,
    text,
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class CatalogSource(Base):
    __tablename__ = "catalog_sources"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    repository_url: Mapped[str | None] = mapped_column(String(512))
    license_attribution: Mapped[str | None] = mapped_column(Text)
    update_policy: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class CatalogImportRun(Base):
    __tablename__ = "catalog_import_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("catalog_sources.id"), nullable=False
    )
    source_commit: Mapped[str | None] = mapped_column(String(64))
    file_path: Mapped[str | None] = mapped_column(String(512))
    file_hash_sha256: Mapped[str | None] = mapped_column(String(64))
    importer_version: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(
        String(32), default="pending"
    )  # pending, running, completed, failed
    total_rows: Mapped[int | None] = mapped_column(Integer)
    accepted_rows: Mapped[int | None] = mapped_column(Integer)
    rejected_rows: Mapped[int | None] = mapped_column(Integer)
    error_report: Mapped[dict | None] = mapped_column(JSONB)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class CatalogVersion(Base):
    __tablename__ = "catalog_versions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    version_label: Mapped[str] = mapped_column(
        String(128), unique=True, nullable=False
    )
    import_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("catalog_import_runs.id"), nullable=False
    )
    published_by: Mapped[str | None] = mapped_column(String(255))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class Medicine(Base):
    __tablename__ = "medicines"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    source_id: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True
    )  # original ID from dataset
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    name_normalized: Mapped[str] = mapped_column(
        String(512), nullable=False, index=True
    )
    price: Mapped[float | None] = mapped_column(Numeric(10, 2))
    is_discontinued: Mapped[bool] = mapped_column(Boolean, default=False)
    manufacturer_name: Mapped[str | None] = mapped_column(String(512))
    manufacturer_normalized: Mapped[str | None] = mapped_column(String(512))
    type: Mapped[str | None] = mapped_column(String(64))
    pack_size_label: Mapped[str | None] = mapped_column(String(255))
    short_composition1: Mapped[str | None] = mapped_column(Text)
    short_composition2: Mapped[str | None] = mapped_column(Text)
    composition_normalized: Mapped[str | None] = mapped_column(Text)
    salt_composition: Mapped[str | None] = mapped_column(Text)
    catalog_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("catalog_versions.id")
    )
    source_hash: Mapped[str | None] = mapped_column(String(64))
    product_identity_hash: Mapped[str | None] = mapped_column(
        String(64), unique=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
        onupdate=datetime.utcnow,
    )

    # Indexes for search - defined in __table_args__
    __table_args__ = (
        Index(
            "idx_medicine_name_trgm",
            "name_normalized",
            postgresql_using="gin",
            postgresql_ops={"name_normalized": "gin_trgm_ops"},
        ),
        Index(
            "idx_medicine_manufacturer_trgm",
            "manufacturer_normalized",
            postgresql_using="gin",
            postgresql_ops={"manufacturer_normalized": "gin_trgm_ops"},
        ),
        Index(
            "idx_medicine_composition_trgm",
            "composition_normalized",
            postgresql_using="gin",
            postgresql_ops={"composition_normalized": "gin_trgm_ops"},
        ),
        Index("idx_medicine_discontinued", "is_discontinued"),
    )


class MedicineIngredient(Base):
    __tablename__ = "medicine_ingredients"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    medicine_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("medicines.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ingredient_text: Mapped[str] = mapped_column(Text, nullable=False)
    ingredient_normalized: Mapped[str | None] = mapped_column(String(512))
    strength_amount: Mapped[str | None] = mapped_column(String(64))
    strength_unit: Mapped[str | None] = mapped_column(String(32))
    source_field: Mapped[str | None] = mapped_column(
        String(32)
    )  # short_composition1, short_composition2
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class MedicineAlias(Base):
    __tablename__ = "medicine_aliases"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    medicine_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("medicines.id", ondelete="CASCADE"), nullable=False, index=True
    )
    alias: Mapped[str] = mapped_column(String(512), nullable=False)
    alias_normalized: Mapped[str] = mapped_column(String(512), nullable=False)
    alias_type: Mapped[str] = mapped_column(
        String(32), default="source_name"
    )  # source_name, approved_spelling, brand_alias, shop_alias
    provenance: Mapped[str | None] = mapped_column(Text)
    review_status: Mapped[str] = mapped_column(String(32), default="pending")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )

    __table_args__ = (
        Index(
            "idx_alias_normalized_trgm",
            "alias_normalized",
            postgresql_using="gin",
            postgresql_ops={"alias_normalized": "gin_trgm_ops"},
        ),
    )
