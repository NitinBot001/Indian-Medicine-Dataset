"""
Fallback starter catalog for SQLite.

Automatically seeds popular Indian medicines if SQLite database is empty,
ensuring search and health check endpoints return valid responses immediately.
"""

import uuid
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.normalization import (
    normalize_medicine_name,
    normalize_manufacturer,
    normalize_composition,
)
from app.importers.csv_importer import compute_product_identity_hash

STARTER_MEDICINES = [
    {
        "source_id": "1",
        "name": "Augmentin 625 Duo Tablet",
        "manufacturer": "GlaxoSmithKline Pharmaceuticals Ltd",
        "pack_size": "strip of 10 tablets",
        "price": 223.42,
        "is_discontinued": False,
        "comp1": "Amoxycillin (500mg)",
        "comp2": "Clavulanic Acid (125mg)",
        "type": "allopathy",
        "aliases": ["augmentin", "augmentin 625", "augmentin duo"],
    },
    {
        "source_id": "2",
        "name": "Augmentin 375 Tablet",
        "manufacturer": "GlaxoSmithKline Pharmaceuticals Ltd",
        "pack_size": "strip of 10 tablets",
        "price": 145.00,
        "is_discontinued": False,
        "comp1": "Amoxycillin (250mg)",
        "comp2": "Clavulanic Acid (125mg)",
        "type": "allopathy",
        "aliases": ["augmentin 375"],
    },
    {
        "source_id": "3",
        "name": "Augmentin 1000 Duo Tablet",
        "manufacturer": "GlaxoSmithKline Pharmaceuticals Ltd",
        "pack_size": "strip of 10 tablets",
        "price": 310.00,
        "is_discontinued": False,
        "comp1": "Amoxycillin (875mg)",
        "comp2": "Clavulanic Acid (125mg)",
        "type": "allopathy",
        "aliases": ["augmentin 1000", "augmentin 1g"],
    },
    {
        "source_id": "4",
        "name": "Dolo 650 Tablet",
        "manufacturer": "Micro Labs Ltd",
        "pack_size": "strip of 15 tablets",
        "price": 30.91,
        "is_discontinued": False,
        "comp1": "Paracetamol (650mg)",
        "comp2": None,
        "type": "allopathy",
        "aliases": ["dolo", "dolo 650", "paracetamol 650"],
    },
    {
        "source_id": "5",
        "name": "Azithral 500 Tablet",
        "manufacturer": "Alembic Pharmaceuticals Ltd",
        "pack_size": "strip of 5 tablets",
        "price": 119.50,
        "is_discontinued": False,
        "comp1": "Azithromycin (500mg)",
        "comp2": None,
        "type": "allopathy",
        "aliases": ["azithral", "azithral 500", "azithromycin 500"],
    },
    {
        "source_id": "6",
        "name": "Azithral 250 Tablet",
        "manufacturer": "Alembic Pharmaceuticals Ltd",
        "pack_size": "strip of 6 tablets",
        "price": 72.00,
        "is_discontinued": False,
        "comp1": "Azithromycin (250mg)",
        "comp2": None,
        "type": "allopathy",
        "aliases": ["azithral 250"],
    },
    {
        "source_id": "7",
        "name": "Crocin Advance Tablet",
        "manufacturer": "GlaxoSmithKline Pharmaceuticals Ltd",
        "pack_size": "strip of 20 tablets",
        "price": 42.00,
        "is_discontinued": False,
        "comp1": "Paracetamol (500mg)",
        "comp2": None,
        "type": "allopathy",
        "aliases": ["crocin", "crocin advance", "crocin 500"],
    },
    {
        "source_id": "8",
        "name": "Crocin 650 Tablet",
        "manufacturer": "GlaxoSmithKline Pharmaceuticals Ltd",
        "pack_size": "strip of 15 tablets",
        "price": 33.50,
        "is_discontinued": False,
        "comp1": "Paracetamol (650mg)",
        "comp2": None,
        "type": "allopathy",
        "aliases": ["crocin 650"],
    },
    {
        "source_id": "9",
        "name": "Pan 40 Tablet",
        "manufacturer": "Alkem Laboratories Ltd",
        "pack_size": "strip of 15 tablets",
        "price": 155.00,
        "is_discontinued": False,
        "comp1": "Pantoprazole (40mg)",
        "comp2": None,
        "type": "allopathy",
        "aliases": ["pan 40", "pantoprazole 40"],
    },
    {
        "source_id": "10",
        "name": "Pan D Capsule",
        "manufacturer": "Alkem Laboratories Ltd",
        "pack_size": "strip of 15 capsules",
        "price": 199.00,
        "is_discontinued": False,
        "comp1": "Pantoprazole (40mg)",
        "comp2": "Domperidone (30mg)",
        "type": "allopathy",
        "aliases": ["pan d", "pan dsr"],
    },
    {
        "source_id": "11",
        "name": "Pantocid 40 Tablet",
        "manufacturer": "Sun Pharma Laboratories Ltd",
        "pack_size": "strip of 15 tablets",
        "price": 162.00,
        "is_discontinued": False,
        "comp1": "Pantoprazole (40mg)",
        "comp2": None,
        "type": "allopathy",
        "aliases": ["pantocid", "pantocid 40"],
    },
    {
        "source_id": "12",
        "name": "Cetrizine Tablet",
        "manufacturer": "Cipla Ltd",
        "pack_size": "strip of 10 tablets",
        "price": 18.20,
        "is_discontinued": False,
        "comp1": "Cetirizine (10mg)",
        "comp2": None,
        "type": "allopathy",
        "aliases": ["cetrizine", "cetzine"],
    },
    {
        "source_id": "13",
        "name": "Metrogyl Gel",
        "manufacturer": "J.B. Chemicals & Pharmaceuticals Ltd",
        "pack_size": "tube of 30 gm",
        "price": 48.00,
        "is_discontinued": False,
        "comp1": "Metronidazole (1% w/w)",
        "comp2": None,
        "type": "allopathy",
        "aliases": ["metrogyl", "metrogyl ointment"],
    },
    {
        "source_id": "14",
        "name": "Cipron Drop",
        "manufacturer": "Cipla Ltd",
        "pack_size": "bottle of 10 ml",
        "price": 28.50,
        "is_discontinued": False,
        "comp1": "Ciprofloxacin (0.3% w/v)",
        "comp2": None,
        "type": "allopathy",
        "aliases": ["cipron", "cipro"],
    },
    {
        "source_id": "15",
        "name": "Omed Capsule",
        "manufacturer": "Intas Pharmaceuticals Ltd",
        "pack_size": "strip of 20 capsules",
        "price": 60.00,
        "is_discontinued": False,
        "comp1": "Omeprazole (20mg)",
        "comp2": None,
        "type": "allopathy",
        "aliases": ["omed", "omeprazole"],
    },
    {
        "source_id": "16",
        "name": "Augmexin Gel",
        "manufacturer": "GlaxoSmithKline Pharmaceuticals Ltd",
        "pack_size": "tube of 15 gm",
        "price": 85.00,
        "is_discontinued": False,
        "comp1": "Amoxycillin",
        "comp2": None,
        "type": "allopathy",
        "aliases": ["augmexin"],
    },
    {
        "source_id": "17",
        "name": "Curemite Soap",
        "manufacturer": "Kivi",
        "pack_size": "packet of 75 gm Soap",
        "price": 85.00,
        "is_discontinued": False,
        "comp1": "Permethrin (NA)",
        "comp2": None,
        "type": "allopathy",
        "aliases": ["curemite"],
    },
    {
        "source_id": "18",
        "name": "Discontinued Sample Tablet",
        "manufacturer": "Sample Pharma Ltd",
        "pack_size": "strip of 10 tablets",
        "price": 50.00,
        "is_discontinued": True,
        "comp1": "Sample Active (100mg)",
        "comp2": None,
        "type": "allopathy",
        "aliases": [],
    },
]


async def seed_fallback_sqlite_if_empty(session: AsyncSession) -> int:
    """
    Seed popular Indian medicines if table is empty.
    Returns number of seeded medicines.
    """
    count_res = await session.execute(text("SELECT count(*) FROM medicines"))
    current_count = count_res.scalar_one_or_none() or 0
    if current_count > 0:
        return current_count

    # 1. Create a catalog version
    version_id = str(uuid.uuid4())
    import_run_id = str(uuid.uuid4())
    source_id = str(uuid.uuid4())

    await session.execute(
        text("""
            INSERT OR IGNORE INTO catalog_sources (id, name, repository_url)
            VALUES (:id, 'sqlite_fallback_catalog', 'https://github.com/NitinBot001/Indian-Medicine-Dataset')
        """),
        {"id": source_id},
    )

    await session.execute(
        text("""
            INSERT OR IGNORE INTO catalog_import_runs (id, source_id, status)
            VALUES (:id, :source_id, 'completed')
        """),
        {"id": import_run_id, "source_id": source_id},
    )

    await session.execute(
        text("""
            INSERT OR IGNORE INTO catalog_versions (id, version_label, import_run_id, is_active)
            VALUES (:id, 'v1.0-sqlite-fallback', :import_run_id, 1)
        """),
        {"id": version_id, "import_run_id": import_run_id},
    )

    # 2. Insert medicines
    inserted_count = 0
    for item in STARTER_MEDICINES:
        med_id = str(uuid.uuid4())
        name = item["name"]
        mfr = item["manufacturer"]
        pack = item["pack_size"]
        comp1 = item["comp1"]
        comp2 = item["comp2"]

        name_norm = normalize_medicine_name(name)
        mfr_norm = normalize_manufacturer(mfr)
        comp_norm = normalize_composition(comp1, comp2)
        prod_hash = compute_product_identity_hash(name_norm, mfr_norm, pack, comp_norm)

        await session.execute(
            text("""
                INSERT OR IGNORE INTO medicines (
                    id, source_id, name, name_normalized, manufacturer_name,
                    manufacturer_normalized, pack_size_label, short_composition1,
                    short_composition2, composition_normalized, price,
                    is_discontinued, type, catalog_version_id, product_identity_hash
                )
                VALUES (
                    :id, :source_id, :name, :name_norm, :mfr, :mfr_norm, :pack,
                    :comp1, :comp2, :comp_norm, :price, :is_disc, :type,
                    :version_id, :prod_hash
                )
            """),
            {
                "id": med_id,
                "source_id": item["source_id"],
                "name": name,
                "name_norm": name_norm,
                "mfr": mfr,
                "mfr_norm": mfr_norm,
                "pack": pack,
                "comp1": comp1,
                "comp2": comp2,
                "comp_norm": comp_norm,
                "price": item["price"],
                "is_disc": 1 if item["is_discontinued"] else 0,
                "type": item["type"],
                "version_id": version_id,
                "prod_hash": prod_hash,
            },
        )
        inserted_count += 1

        # Add aliases
        for alias in item.get("aliases", []):
            await session.execute(
                text("""
                    INSERT OR IGNORE INTO medicine_aliases (
                        id, medicine_id, alias, alias_normalized, alias_type
                    )
                    VALUES (
                        :id, :med_id, :alias, :alias_norm, 'brand_alias'
                    )
                """),
                {
                    "id": str(uuid.uuid4()),
                    "med_id": med_id,
                    "alias": alias,
                    "alias_norm": normalize_medicine_name(alias),
                },
            )

    await session.commit()
    return inserted_count
