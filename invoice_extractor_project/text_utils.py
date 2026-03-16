
import re

def clean(text):
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"[,:;]+$", "", text)
    return text

# def normalize_company(name):
#     name = re.sub(r"BoschLtd", "Bosch Ltd.", name)
#     name = re.sub(r"([a-z])([A-Z])", r"\1 \2", name)
#     return name

def normalize_company(name):

    name = name.strip()

    # Fix BoschLtd
    name = re.sub(r"Bosch\s*Ltd\.?", "Bosch Ltd.", name, flags=re.I)

    # Remove duplicate punctuation
    name = re.sub(r"\.+$", ".", name)

    return name.upper()


# def normalize_address(text):

#     # Insert space between lowercase and uppercase
#     text = re.sub(r"([a-zà-ỹ])([A-ZÀ-Ỹ])", r"\1 \2", text)

#     # Insert space between letters and numbers
#     text = re.sub(r"([A-Za-zÀ-Ỹà-ỹ])(\d)", r"\1 \2", text)

#     # Insert space between numbers and letters
#     text = re.sub(r"(\d)([A-Za-zÀ-Ỹà-ỹ])", r"\1 \2", text)

#     # Fix Vietnamese common words
#     text = text.replace("Thànhphố", "Thành phố")
#     text = text.replace("ViệtNam", "Việt Nam")
#     text = text.replace("phốLiễu", "phố Liễu")

#     # Clean commas
#     text = re.sub(r"\s*,\s*", ", ", text)

#     return text.strip()

def normalize_address(text):
    if not text:
        return ""

    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    text = re.sub(r"([A-Za-z])(\d)", r"\1 \2", text)
    text = re.sub(r"(\d)([A-Za-z])", r"\1 \2", text)

    text = text.replace("LeDuan", "Le Duan")

    text = re.sub(r"\s*,\s*", ", ", text)
    text = re.sub(r"\s+", " ", text)

    return text.strip(", ")

def clean_value(v):
    if not v:
        return ""
    return v.strip().strip(":")

def remove_hex(text):
    if not text:
        return ""
    # Include 'O' as it's a common OCR error for '0' in hex IDs
    return re.sub(r"\b[A-F0-9O]{16,}\b", "", text)

def validate_amount(v):
    if not v:
        return ""
    # Ensure it looks like a number with optional dots/commas
    if not re.match(r"^[0-9.,]+$", v):
        return ""
    return v


def parse_invoice_amount(value) -> "float | None":
    """
    Parse invoice amounts from global OCR formats safely.

    Handles:
      EU format          1.234,56  →  1234.56
      US/India format    1,234.56  →  1234.56
      Plain decimal      538,25    →  538.25   (comma as decimal)
      Large EU           1.234.567,89 → 1234567.89
      Space separator    1 234,56  →  1234.56
      Currency symbols   €1.538,25 →  1538.25
      OCR noise          1.O38,25  →  1038.25  (letter-O → 0)
      Negative           -1.234,56 → -1234.56
    Returns None when the string cannot be converted to a number.
    """
    if value is None:
        return None

    text = str(value).strip()

    # --- OCR noise (only within numeric context) ---
    # Replace O/o → 0 and l → 1 only when the character is immediately
    # adjacent to a digit or numeric separator (., ,).
    # Avoids corrupting currency symbols / words like "EUR", "Total", etc.
    text = re.sub(r"(?<=[0-9.,\-])[Oo]|[Oo](?=[0-9.,\-])", "0", text)
    text = re.sub(r"(?<=[0-9.,\-])l|l(?=[0-9.,\-])", "1", text)

    # Strip currency symbols, letters, spaces — keep digits, dot, comma, minus
    text = re.sub(r"[^\d,.\-]", "", text)

    if not text or text in ("-", "."):
        return None

    comma_count = text.count(",")
    dot_count   = text.count(".")

    # --- Both separators present ---
    if comma_count > 0 and dot_count > 0:
        if text.rfind(",") > text.rfind("."):
            # EU:  1.234,56  → dot=thousands, comma=decimal
            text = text.replace(".", "").replace(",", ".")
        else:
            # US:  1,234.56  → comma=thousands, dot=decimal
            text = text.replace(",", "")

    # --- Only comma ---
    elif comma_count > 0:
        decimal_part = text.rsplit(",", 1)[-1]
        if len(decimal_part) <= 2:
            # Decimal comma: 538,25 → 538.25
            text = text.replace(",", ".")
        else:
            # Thousands comma: 1,234567 → 1234567
            text = text.replace(",", "")

    # --- Only dot ---
    elif dot_count > 1:
        # Multiple dots → thousands separators: 1.234.567 → 1234567
        text = text.replace(".", "")

    try:
        return float(text)
    except ValueError:
        return None

def remove_hex_strings(text):
    lines = []
    for l in text.splitlines():
        if re.match(r"[A-F0-9]{20,}", l):
            continue
        lines.append(l)
    return "\n".join(lines)

def detect_country(text, default="India"):
    if not text:
        return default
    
    # Keywords and mappings
    keywords = [
        (r"\bINDIA\b|\bINDIEN\b", "India"),
        (r"\bVIETNAM\b|\bVIET\s*NAM\b", "Vietnam"),
        (r"\bGERMANY\b|\bDEUTSCHLAND\b|\bSTUTTGART\b", "Germany"),
        (r"\bJAPAN\b|\bKANAGAWA\b", "Japan"),
        (r"\bFRANCE\b", "France"),
        (r"\bCZECH\b|\bČESKÁ\b", "Czech Republic"),
        (r"\bHUNGARY\b|\bMAGYARORSZÁG\b|\bBUDAPEST\b", "Hungary"),
        (r"\bKOREA\b", "Korea"),
        (r"\bTHAILAND\b", "Thailand"),
    ]
    
    for pattern, country in keywords:
        if re.search(pattern, text, re.IGNORECASE):
            return country
            
    return default
