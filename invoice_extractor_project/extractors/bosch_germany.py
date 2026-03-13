import re

_NAME_STRIP = re.compile(
    r"\s*[,\s]+"
    r"(?:GmbH|SAS|S\.A\.S\.|spol\.\s*s\s*r\.\s*o\.|Ltd\.|"
    r"SE|AG|NV|BV|S\.A\.|S\.p\.A\.|Oy|AB|AS|ApS|Inc\.|LLC|"
    r"France|Deutschland|Japan|Czech|Polska).*$",
    re.IGNORECASE,
)


def _normalize_name(raw):
    """Strip legal-form suffix: 'Robert Bosch GmbH' -> 'Robert Bosch'.
    Preserves 'Bosch Corporation' as-is (Corporation is part of the trading name).
    """
    if re.search(r"Bosch\s+Corporation", raw, re.IGNORECASE):
        return "Bosch Corporation"
    # Strip parenthesised country qualifier e.g. "(France)" in "Robert Bosch (France) S.A.S."
    name = re.sub(r"\s*\([^)]+\).*$", "", raw).strip()
    # Also strip remaining legal suffix if paren-strip left one
    name = _NAME_STRIP.sub("", name).strip().rstrip(",").strip()
    return name if name else raw


_COUNTRY_MAP = {
    "IT": "Italy", "ES": "Spain", "BE": "Belgium", "AT": "Austria",
    "SE": "Sweden", "PL": "Poland", "CZ": "Czech Republic",
    "HU": "Hungary", "RO": "Romania", "PT": "Portugal", "JP": "Japan",
}

# Country keywords for fallback when no VAT ID is present
_COUNTRY_KEYWORDS = [
    ("Japan", "Japan"), ("JAPAN", "Japan"), ("Kanagawa", "Japan"),
    ("Germany", "Germany"), ("GERMANY", "Germany"), ("Stuttgart", "Germany"),
    ("France", "France"), ("FRANCE", "France"),
    ("CZECHIA", "Czech Republic"), ("Czech", "Czech Republic"),
]


def _clean_dispatch_line(text):
    """
    Extract and clean the Dispatch/Services address line.
    Two garbling patterns:
      A) D_i_sp_at_ch__ad_dr_es_s_ -> double-underscore = word boundary
      B) D _ i _ sp _ a ... (space-separated single chars - unrecoverable)
    Pattern A is cleanable; Pattern B falls through to other strategies.
    The "Ltd." within the line may also be garbled as "Lt _ d _." or "Lt d .".
    """
    m = re.search(
        r"((?:D[\s_]*i[\s_]*s[\s_]*p[\s_]*a[\s_]*t[\s_]*c[\s_]*h"
        r"|S[\s_]*e[\s_]*r[\s_]*v[\s_]*i[\s_]*c[\s_]*e[\s_]*s)"
        r"[\s_]*[Aa][\s_]*d[\s_]*d[\s_]*r[\s_]*e[\s_]*s[\s_]*s[^\n]+)",
        text, re.IGNORECASE,
    )
    if not m:
        return ""
    raw = m.group(1)
    cleaned = re.sub(r"__", " ", raw)       # double underscore = word boundary
    cleaned = re.sub(r"_", "", cleaned)     # strip remaining single underscores
    # Fix pincode glued to city e.g. "IN-562109Bidadi" -> "IN-562109 Bidadi"
    cleaned = re.sub(r"(IN[-\s]*\d{6})([A-Za-z])", r"\1 \2", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    return cleaned.strip()


def _extract_remitter_from_block(text):
    """
    Strategy C: scan line-by-line for 'Bosch Ltd.' then collect address lines
    until a country marker (India/INDIA/IN). Handles multi-line blocks where
    P.O.Box or other intermediate lines appear between street and pincode.
    """
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if re.search(r"Bosch\s+L[\s_I|l]*td[\s_I|l]*\.", line, re.IGNORECASE):
            parts = []
            for j in range(i + 1, min(i + 10, len(lines))):
                l = lines[j].strip()
                # Strip trailing "Ship to : XXXXXX" noise
                l = re.sub(r"\s+Ship\s+to.*", "", l, flags=re.IGNORECASE).strip()
                # Stop at country markers
                if re.match(r"^(India|INDIA|IN\s*:?)\s*$", l):
                    break
                # Skip pure noise lines
                if re.search(
                    r"^(Customer\s+No|Contact\s+addr|Sales\s*:|Accounting\s*:)",
                    l, re.IGNORECASE
                ):
                    continue
                if l:
                    parts.append(l)
            return parts
    return []


def extract(text, words=None):
    data = {}

    # ── Beneficiary (the issuing Bosch entity) ────────────────────────────────

    # Name: first non-empty line, then strip legal suffix
    raw_name = next(
        (l.strip() for l in text.splitlines() if l.strip()), ""
    )
    raw_name = re.sub(r"\s*/\s*$", "", raw_name).strip()
    data["beneficiary_name"] = _normalize_name(raw_name)

    # Country: 1) VAT ID prefix
    m_vat = re.search(r"Our\s+VAT\s+ID\s+No\s*:?\s*([A-Z]{2})\d+", text, re.IGNORECASE)
    if m_vat:
        vat_prefix = m_vat.group(1).upper()
        data["beneficiary_country"] = _COUNTRY_MAP.get(vat_prefix, vat_prefix)
    else:
        # 2) Country keyword in Company address or first 15 lines of text
        header = "\n".join(text.splitlines()[:15])
        m_ca_block = re.search(
            r"Company\s+address\s*:?.+", header, re.IGNORECASE | re.DOTALL
        )
        search_zone = m_ca_block.group(0) if m_ca_block else header
        country_found = ""
        for keyword, country in _COUNTRY_KEYWORDS:
            if keyword in search_zone:
                country_found = country
                break
        data["beneficiary_country"] = country_found or "DE"

    # Address: labeled "Company address" (works when it includes a street)
    m_ca = re.search(r"Company\s+address\s*:?\s*[^,\n]+,\s*(.+)", text, re.IGNORECASE)
    if m_ca:
        addr = m_ca.group(1).strip().rstrip(",").strip()
        # Remove garbled country suffix on next line (e.g. "J_a_pa_n")
        addr = re.sub(r",\s*$", "", addr)
        data["beneficiary_address"] = addr
    else:
        # Fallback: Siège Social in footer (French entities) — needs DOTALL
        m_ss = re.search(
            r"Si.ge\s+Social\s*:?\s*(.+?)(?:\s*[-\u2013]\s*(?:France|N°|TVA|N\b))",
            text, re.IGNORECASE | re.DOTALL,
        )
        if m_ss:
            addr = m_ss.group(1).strip().replace("\n", " ")
            addr = re.sub(r"([a-z])([A-Z])", r"\1 \2", addr)  # fix CamelCase merges
            addr = re.sub(r"\s{2,}", " ", addr).strip()
            data["beneficiary_address"] = addr
        else:
            data["beneficiary_address"] = ""

    # ── Remitter (Bosch Ltd., India side) ─────────────────────────────────────

    data["remitter_country"] = "India"

    # Name: PDF sometimes mangles "Bosch Ltd." as "Bosch L I td I ."
    # More flexible for Bosch Rexroth or other variants
    m_name = re.search(r"Bosch\s+(?:L[\s_I|l]*td[\s_I|l]*\.|Rexroth|Corporation)", text, re.IGNORECASE)
    data["remitter_name"] = m_name.group(0).strip() if m_name else ""
    if "td" in data["remitter_name"].lower():
        data["remitter_name"] = "Bosch Ltd."
    elif "rexroth" in data["remitter_name"].lower():
        data["remitter_name"] = "Bosch Rexroth"

    # Address strategy 1: Dispatch / Services address line
    dispatch = _clean_dispatch_line(text)
    matched = False
    if dispatch:
        # Use Bosch[^,]+, to skip garbled "Ltd." variants
        m_d = re.search(
            r"(?:Dispatch|Services)\s*[Aa]ddress\s*:?\s*Bosch[^,]+,\s*(.+)",
            dispatch, re.IGNORECASE,
        )
        if m_d:
            addr = m_d.group(1).strip()
            # Normalise "IN- 422007 Nashik" -> ", Nashik - 422007"
            addr = re.sub(
                r",?\s*IN[-\s]+(\d{6})\s+([A-Za-z]+)",
                lambda mo: ", " + mo.group(2) + " - " + mo.group(1),
                addr,
            )
            data["remitter_address"] = addr.strip().lstrip(",").strip()
            matched = True

    if not matched:
        # Strategy 2: SIPCOT anchor (Tirunelveli-style industrial park)
        m_a = re.search(
            r"(SIPCOT[^\n]+)\n(Plot[^\n]+)\n([^\n]+)\n(\d{6})[^\n]*",
            text, re.IGNORECASE,
        )
        if m_a:
            data["remitter_address"] = (
                m_a.group(1).strip() + ", " + m_a.group(2).strip() +
                ", " + m_a.group(3).strip() + " - " + m_a.group(4).strip()
            )
            matched = True
            
    if not matched:
        # Strategy 2.5: Plain city-pincode fallback for OCR (e.g. "382170 Ahmedabad")
        m_ocr_a = re.search(r"(\d{6})\s+([A-Z][a-z]+)", text)
        if m_ocr_a:
            lines = text.splitlines()
            for idx, line in enumerate(lines):
                if m_ocr_a.group(1) in line:
                    # Take up to 5 previous lines to ensure "Iyava Village" etc. are captured
                    addr_parts = []
                    for k in range(max(0, idx-5), idx):
                        l = lines[k].strip()
                        # Strictly skip remitter name lines and fragments
                        if re.search(r"\b(Robert|Bosch|Rexroth|Limited|Private|India|Ltd)\b", l, re.IGNORECASE):
                            continue
                        if l:
                            addr_parts.append(l)
                    
                    city = m_ocr_a.group(2)
                    pincode = m_ocr_a.group(1)
                    street = ", ".join(addr_parts).strip(", ")
                    # Final safety: remove trailing name fragments if they leaked
                    street = re.sub(r"^(?:Limited|Private|India|Ltd),?\s*", "", street, flags=re.IGNORECASE)
                    
                    data["remitter_address"] = f"{street}, {city} - {pincode}".strip(", ").strip()
                    matched = True
                    break

    if not matched:
        # Strategy 3: collect address block line-by-line (handles P.O.Box, KA etc.)
        block_lines = _extract_remitter_from_block(text)
        if block_lines:
            # Combine — last line may have "pincode city" or "city STATE pincode"
            addr = ", ".join(block_lines)
            data["remitter_address"] = addr
            matched = True

    if not matched:
        # Strategy 4: street-before-Ship-to, then pincode+CITY+INDIA (simple 2-line)
        m_b = re.search(
            r"^([\w\s]+?)\s+Ship\s+to[^\n]*\n(\d{6})\s+([A-Z]{2,})\s*\n(?:INDIA|IN\b)",
            text, re.MULTILINE | re.IGNORECASE,
        )
        data["remitter_address"] = (
            m_b.group(1).strip() + ", " +
            m_b.group(3).strip().title() + " - " + m_b.group(2).strip()
            if m_b else ""
        )

    # ── Invoice number ─────────────────────────────────────────────────────────
    m_inv = re.search(r"Invoice\s*(?:No\.?|Doc)\s*:?\s*([A-Z0-9]+)", text, re.IGNORECASE)
    data["invoice_number"] = m_inv.group(1).strip() if m_inv else ""

    # ── Invoice date ───────────────────────────────────────────────────────────
    # More flexible for OCR tokens between label and value
    m_date = re.search(r"Date\s+Invoice\s*.*?\s*(\d{2}[./]\d{2}[./]\d{4})", text, re.IGNORECASE)
    data["invoice_date"] = m_date.group(1).strip() if m_date else ""

    # ── Amount & currency ──────────────────────────────────────────────────────
    m_amt = re.search(
        r"Invoice\s+amount\s*:?\s*(EUR|USD|GBP|CHF|JPY|CZK)\s*([\d,. ]+)",
        text, re.IGNORECASE,
    )
    if m_amt:
        data["currency"] = m_amt.group(1).upper()
        data["amount_foreign"] = m_amt.group(2).strip().replace(" ", "")
    else:
        m_ac = re.search(
            r"Amount\s+carried\s*:?\s*([\d,. ]+)", text, re.IGNORECASE
        )
        if m_ac and m_ac.group(1).strip():
            data["amount_foreign"] = m_ac.group(1).strip().replace(" ", "")
            mc = re.search(r"\b(EUR|USD|GBP|CHF|JPY|CZK)\b", text)
            data["currency"] = mc.group(1) if mc else ""
        else:
            # "Value of Services: JPY 78,000" fallback for Japan
            m_vs = re.search(
                r"Value\s+of\s+(?:Services|goods)\s*:?\s*(EUR|USD|GBP|CHF|JPY|CZK)\s*([\d,. ]+)",
                text, re.IGNORECASE,
            )
            if m_vs:
                data["currency"] = m_vs.group(1).upper()
                data["amount_foreign"] = m_vs.group(2).strip().replace(" ", "")
            else:
                data["amount_foreign"] = ""
                data["currency"] = ""

    return data