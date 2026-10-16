"""
Text normalization utilities for medicine search.
Implements conservative normalization per plan.md Section 6 and Section 3.1.
"""

import re
import unicodedata

# Common pharmaceutical form synonyms (expanded per Section 3.1)
FORM_SYNONYMS: dict[str, str] = {
    "tab": "tablet",
    "cap": "capsule",
    "syp": "syrup",
    "inj": "injection",
    "susp": "suspension",
    "oint": "ointment",
}

# Bare unit tokens to drop from search queries (Section 3.1)
UNIT_TOKENS: set[str] = {
    "mg",
    "ml",
    "mcg",
    "gm",
    "gms",
    "g",
    "iu",
    "mm",
    "cm",
    "l",
    "kg",
}


def escape_like(text: str) -> str:
    """
    Escape SQL LIKE wildcard characters ('%' and '_') to prevent wildcard injection (F9).
    """
    if not text:
        return ""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def normalize_query(query: str | None) -> tuple[str, list[str]]:
    """
    Query understanding and tokenizer per Section 3.1:
    - Unicode NFKC normalization and lowercase.
    - Tokens only [a-z0-9] and decimal numbers (removes LIKE wildcards % and _).
    - Split glued letters and digits (e.g. 'dolo650' -> 'dolo 650').
    - Drop bare unit tokens (mg, ml, mcg, gm, iu, etc.).
    - Expand form synonyms (tab -> tablet, cap -> capsule, etc.).
    - Maximum 8 tokens and 64 characters.

    Returns:
        tuple of (normalized_query_string, token_list)
    """
    if not query:
        return "", []

    # 1. Unicode NFKC normalization + lowercase
    text = unicodedata.normalize("NFKC", str(query)).lower()

    # 2. Split glued letters and digits: "dolo650" -> "dolo 650", "625duo" -> "625 duo"
    text = re.sub(r"([a-z])(\d)", r"\1 \2", text)
    text = re.sub(r"(\d)([a-z])", r"\1 \2", text)

    # 3. Strip LIKE wildcards and non-alphanumeric chars (keep spaces and decimal dots between digits)
    text = re.sub(r"[^a-z0-9.]", " ", text)
    # Remove standalone dots or non-decimal dots
    text = re.sub(r"(?<!\d)\.|\.(?!\d)", " ", text)

    # 4. Tokenize and clean
    raw_tokens = text.split()
    clean_tokens: list[str] = []

    for t in raw_tokens:
        # Drop bare unit tokens
        if t in UNIT_TOKENS:
            continue
        # Expand form synonyms
        if t in FORM_SYNONYMS:
            t = FORM_SYNONYMS[t]
        clean_tokens.append(t)

    # 5. Cap at 8 tokens
    clean_tokens = clean_tokens[:8]

    # 6. Build normalized string and cap at 64 characters
    norm_str = " ".join(clean_tokens)
    if len(norm_str) > 64:
        norm_str = norm_str[:64].rstrip()
        clean_tokens = norm_str.split()

    return norm_str, clean_tokens


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
