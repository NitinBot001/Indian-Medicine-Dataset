# Indian Medicine Dataset & Search Engine

`TOTAL MEDICINES` `253973`

## Introduction
The Indian Medicine Dataset is a comprehensive collection of data about various medicines available in India. This dataset includes important details such as the medicine name, price, manufacturer, type, pack size, and composition. It is designed to be a valuable resource for researchers, healthcare professionals, pharmacies, and software developers.

Included in this repository is a **high-performance, production-ready autocomplete and search backend** ([`medicine-search-backend/`](file:///workspaces/Indian-Medicine-Dataset/medicine-search-backend)) built on FastAPI and PostgreSQL 16 delivering **warm p95 latency under 6ms** across all 254k products.

---

## High-Performance Medicine Search Backend

See the [`medicine-search-backend/README.md`](file:///workspaces/Indian-Medicine-Dataset/medicine-search-backend/README.md) for full architecture and documentation.

- **8-Tier Strict Ladder**: Exact match, prefix match, brand typo correction, abbreviation matching, word-prefix full-text search, composition/salt search, and manufacturer search.
- **Single DB Round Trip**: Executes via compiled PL/pgSQL function `search_medicines`.
- **Intra-Tier Popularity**: Usage feedback (`POST /api/v1/search/feedback`) bubbles frequently billed items to the top without breaking tier boundaries.
- **Shop Inventory Matching**: Real-time stock status and store-specific pricing.
- **Sub-10ms Autocomplete Latency**: Measured warm p95 of 5.58 ms.

### Quick Start (Search Backend)
```bash
cd medicine-search-backend
docker compose up -d --wait
pip install -e ".[dev]"
alembic upgrade head
python scripts/build_search_index.py
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

---

## Dataset Description
The dataset contains information on a range of medicines, with details that are crucial for understanding each product's specifics: product ID, name, price in Indian Rupees (₹), discontinuation status, manufacturer name, type (e.g., allopathy), pack size, and composition.

### Formats
The dataset is available in two formats:
1. **CSV (Comma-Separated Values)**: `DATA/indian_medicine_data.csv`
2. **JSON (JavaScript Object Notation)**: `DATA/indian_medicine_data.json`

### Data Fields
- **id**: Unique identifier for each medicine.
- **name**: Name of the medicine.
- **price(₹)**: Price of the medicine in Indian Rupees.
- **Is_discontinued**: Indicates whether the medicine is discontinued (FALSE = available).
- **manufacturer_name**: Name of the manufacturer.
- **type**: Type of medicine (e.g., allopathy).
- **pack_size_label**: Packaging size information (e.g. "strip of 10 tablets").
- **short_composition1**: Primary active ingredient of the medicine.
- **short_composition2**: Secondary active ingredient of the medicine (if applicable).

### CSV Format Example
```csv
id,name,price(₹),Is_discontinued,manufacturer_name,type,pack_size_label,short_composition1,short_composition2
1,Augmentin 625 Duo Tablet,223.42,FALSE,Glaxo SmithKline Pharmaceuticals Ltd,allopathy,strip of 10 tablets,Amoxycillin (500mg),Clavulanic Acid (125mg)
```

### JSON Format Example
```json
[
  {
    "id": "1",
    "name": "Augmentin 625 Duo Tablet",
    "price(₹)": "223.42",
    "Is_discontinued": "FALSE",
    "manufacturer_name": "Glaxo SmithKline Pharmaceuticals Ltd",
    "type": "allopathy",
    "pack_size_label": "strip of 10 tablets",
    "short_composition1": "Amoxycillin (500mg)",
    "short_composition2": "Clavulanic Acid (125mg)"
  }
]
```

## Usage and Contributions
This dataset and search backend are open source under the MIT License. Contributions are welcome for expanding the dataset, enhancing search ranking, and refining pharmaceutical metadata.
