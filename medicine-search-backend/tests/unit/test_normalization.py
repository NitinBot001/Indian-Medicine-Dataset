"""
Unit tests for text normalization (plan.md Section 6 and Section 3.1).
"""

from app.services.normalization import (
    escape_like,
    extract_strength,
    normalize_composition,
    normalize_manufacturer,
    normalize_medicine_name,
    normalize_query,
    normalize_text,
    parse_ingredient,
)


def test_normalize_text_basic():
    assert normalize_text("  Augmentin  625  ") == "augmentin 625"
    assert normalize_text("DOLO 650") == "dolo 650"
    assert normalize_text(None) == ""


def test_normalize_text_unicode_nfkc():
    # Full-width characters
    assert normalize_text("Ａｕｇｍｅｎｔｉｎ") == "augmentin"


def test_normalize_text_unit_spacing():
    # Unit normalization: 500mg -> 500 mg
    assert normalize_text("Azithral 500mg") == "azithral 500 mg"
    assert normalize_text("Ascoril 100ml") == "ascoril 100 ml"


def test_normalize_manufacturer_removes_suffixes():
    assert normalize_manufacturer("Glaxo SmithKline Pharmaceuticals Ltd") == "glaxo smithkline"
    assert normalize_manufacturer("Alembic Pharmaceuticals Ltd.") == "alembic"
    assert normalize_manufacturer("Sun Pharma Pvt Ltd") == "sun"
    assert normalize_manufacturer("Sanofi India Limited") == "sanofi india"


def test_normalize_composition():
    comp1 = "Amoxycillin (500mg)"
    comp2 = "Clavulanic Acid (125mg)"
    result = normalize_composition(comp1, comp2)
    assert "amoxycillin" in result
    assert "clavulanic acid" in result
    assert "+" in result


def test_extract_strength():
    amount, unit = extract_strength("Amoxycillin (500mg)")
    assert amount == "500"
    assert unit == "mg"

    amount, unit = extract_strength("Ambroxol (30mg/5ml)")
    assert amount == "30"
    assert unit == "mg/5ml"

    amount, unit = extract_strength("No Strength")
    assert amount is None
    assert unit is None


def test_parse_ingredient():
    parsed = parse_ingredient("Amoxycillin (500mg) + Clavulanic Acid (125mg)")
    assert len(parsed) == 2
    assert parsed[0]["ingredient_normalized"] == "amoxycillin"
    assert parsed[0]["strength_amount"] == "500"
    assert parsed[0]["strength_unit"] == "mg"
    assert parsed[1]["ingredient_normalized"] == "clavulanic acid"
    assert parsed[1]["strength_amount"] == "125"
    assert parsed[1]["strength_unit"] == "mg"


def test_normalize_query_tokenizer():
    norm_str, tokens = normalize_query("dolo650")
    assert norm_str == "dolo 650"
    assert tokens == ["dolo", "650"]

    norm_str, tokens = normalize_query("augmentin 625mg")
    assert norm_str == "augmentin 625"
    assert tokens == ["augmentin", "625"]

    norm_str, tokens = normalize_query("p_n")
    assert norm_str == "p n"
    assert tokens == ["p", "n"]

    norm_str, tokens = normalize_query("%")
    assert norm_str == ""
    assert tokens == []

    norm_str, tokens = normalize_query("tab paracetamol")
    assert norm_str == "tablet paracetamol"
    assert tokens == ["tablet", "paracetamol"]


def test_escape_like():
    assert escape_like("p_n") == "p\\_n"
    assert escape_like("100%") == "100\\%"
    assert escape_like("normal") == "normal"
