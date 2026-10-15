"""
Text normalization utilities for medicine search.
Implements conservative normalization per plan.md Section 6.
"""

import re
import unicodedata


def normalize_text(text: str | None) -> str:
    """
    Normalize text for search matching.

    Steps:
    1. Unicode NFKC normalization
    2. Lowercase
    3. Trim whitespace
    4. Collapse multiple whitespace to single space
    5. Normalize common punctuation/hyphens
    6. Normalize unit formatting (500mg -> 500 mg)
    """
    if not text:
        return ""

    # Unicode NFKC normalization
    result = unicodedata.normalize("NFKC", text)

    # Lowercase
    result = result.lower()

    # Normalize various dash/hyphen characters to standard hyphen
    result = re.sub(r"[\u2010\u2011\u2012\u2013\u2014\u2015\uFE58\uFE63\uFF0D]", "-", result)

    # Remove extra punctuation but keep meaningful ones (hyphens, periods, parentheses)
    # Keep: alphanumeric, spaces, hyphens, periods, parentheses, plus, percent, slash
    result = re.sub(r"[^\w\s\-.()+%/]", " ", result)

    # Normalize unit formatting: "500mg" -> "500 mg", "500ml" -> "500 ml"
    result = re.sub(r"(\d)(mg|ml|mcg|g|l|iu|mm|cm)\b", r"\1 \2", result)

    # Also handle: "mg500" is unlikely but "5ml" is common
    result = re.sub(r"(\d)\s*(mg|ml|mcg|gm|gms)\b", r"\1 \2", result)

    # Collapse whitespace
    result = re.sub(r"\s+", " ", result)

    # Trim
    result = result.strip()

    return result


def normalize_medicine_name(name: str | None) -> str:
    """Normalize a medicine name for search indexing."""
    return normalize_text(name)


def normalize_manufacturer(manufacturer: str | None) -> str:
    """
    Normalize manufacturer name.
    Remove common suffixes like Ltd, Limited, Pvt, Private, Inc.
    """
    if not manufacturer:
        return ""

    result = normalize_text(manufacturer)
    # Remove dots and commas so abbreviations like Ltd. become ltd
    result = re.sub(r"[.,]", "", result)

    # Remove common company suffixes for matching
    suffixes = [
        r"\bpvt\s*ltd\b",
        r"\bprivate\s+limited\b",
        r"\blimited\b",
        r"\bltd\b",
        r"\binc\b",
        r"\bcorp\b",
        r"\bcorporation\b",
        r"\bpharmaceuticals?\b",
        r"\bpharma\b",
        r"\blaboratories\b",
        r"\blabs?\b",
    ]
    for suffix in suffixes:
        result = re.sub(suffix, "", result)

    # Collapse whitespace again after suffix removal
    result = re.sub(r"\s+", " ", result).strip()

    return result


def normalize_composition(comp1: str | None, comp2: str | None = None) -> str:
    """Normalize composition fields for search indexing."""
    parts = []
    if comp1:
        parts.append(normalize_text(comp1))
    if comp2:
        parts.append(normalize_text(comp2))
    return " + ".join(parts) if parts else ""


def extract_strength(text: str | None) -> tuple[str | None, str | None]:
    """
    Extract strength amount and unit from a composition string.
    E.g., "Amoxycillin (500mg)" -> ("500", "mg")
    """
    if not text:
        return None, None

    # Put longer/compound units first so they aren't prematurely matched by 'mg' or 'ml'
    match = re.search(r"(\d+(?:\.\d+)?)\s*(mg/5ml|mg/ml|mcg/ml|mcg|mg|ml|iu|g|%)\b", text, re.IGNORECASE)
    if match:
        return match.group(1), match.group(2).lower()
    return None, None


def parse_ingredient(composition_text: str) -> list[dict]:
    """
    Parse a composition field like 'Amoxycillin (500mg)' into structured ingredients.
    Returns list of dicts with keys: ingredient_text, ingredient_normalized, strength_amount, strength_unit
    """
    if not composition_text or not composition_text.strip():
        return []

    results = []
    # Split by + for combined compositions
    parts = re.split(r"\s*\+\s*", composition_text)

    for part in parts:
        part = part.strip()
        if not part:
            continue

        # Extract ingredient name (everything before parenthesis)
        name_match = re.match(r"^([^(]+)", part)
        ingredient_name = name_match.group(1).strip() if name_match else part

        amount, unit = extract_strength(part)

        results.append({
            "ingredient_text": part,
            "ingredient_normalized": normalize_text(ingredient_name),
            "strength_amount": amount,
            "strength_unit": unit,
        })

    return results
