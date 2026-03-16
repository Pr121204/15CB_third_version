"""
Extractor for Robert Bosch GmbH SAP invoices that go through OCR fallback.
Handles both:
  A) Spaced text:    "Robert Bosch GmbH / Robert-Bosch-Platz 1 / 70839 GERLINGEN"
  B) Compressed text: "RobertBoschGmbH / Robert-Bosch-Platz1 / 70839GERLINGEN"
"""
import re

_COUNTRY_MAP = {
    "DE": "Germany", "FR": "France", "GB": "UK", "NL": "Netherlands",
    "IT": "Italy", "ES": "Spain", "BE": "Belgium", "AT": "Austria",
    "SE": "Sweden", "PL": "Poland", "CZ": "Czech Republic",
    "HU": "Hungary", "RO": "Romania", "PT": "Portugal", "JP": "Japan",
}

_NAME_STRIP = re.compile(
    r"\s*[,\s]+"
    r"(?:GmbH|SAS|S\.A\.S\.|spol\.\s*s\s*r\.\s*o\.|Ltd\.|"
    r"SE|AG|NV|BV|S\.A\.|S\.p\.A\.|Oy|AB|AS|ApS|Inc\.|LLC|"
    r"France|Deutschland|Japan|Czech|Polska).*$",
    re.IGNORECASE,
)


def _normalize_name(raw):
    if re.search(r"Bosch\s+Corporation", raw, re.IGNORECASE):
        return "Bosch Corporation"
    name = re.sub(r"\s*\([^)]+\).*$", "", raw).strip()
    name = _NAME_STRIP.sub("", name).strip().rstrip(",").strip()
    return name if name else raw


def _decompress(s):
    """Insert spaces in compressed PDF text.
    'Robert-Bosch-Platz1' → 'Robert-Bosch-Platz 1'
    '70839GERLINGEN'      → '70839 GERLINGEN'
    'BoschLtd.'           → 'Bosch Ltd.'  (not needed here but safe)
    '3000,HosurRoadPostBoxNo' → '3000, Hosur Road Post Box No'
    """
    # Space before digit following a letter (but not after hyphen)
    s = re.sub(r"([A-Za-z])(\d)", r"\1 \2", s)
    # Space before uppercase following lowercase (CamelCase split)
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", s)
    # Space after digit following a letter (already done above for reverse)
    s = re.sub(r"(\d)([A-Za-z])", r"\1 \2", s)
    # Normalise multiple spaces
    s = re.sub(r"\s{2,}", " ", s)
    return s.strip()


def _normalize_amount(raw):
    """Handle both US (1,234.56) and European (1.234,56) number formats."""
    raw = raw.strip()
    # European: groups of 3 separated by dots, comma decimal → 1.234,56
    if re.match(r"^\d{1,3}(\.\d{3})*,\d{2}$", raw):
        integer = raw.rsplit(",", 1)[0].replace(".", "")
        decimal = raw.rsplit(",", 1)[1]
        return f"{int(integer):,}.{decimal}"
    # Already US format or simple number — return as-is
    return raw


def extract(text, words=None):
    data = {}
    lines = [l.strip() for l in text.splitlines() if l.strip()]

    # ── Beneficiary name ──────────────────────────────────────────────────────
    raw_name = lines[0] if lines else ""
    data["beneficiary_name"] = _normalize_name(raw_name)

    # ── Beneficiary country ───────────────────────────────────────────────────
    # 1) Bare VAT ID on letterhead e.g. "DE811128135"
    m_vat = re.search(r"(?<![A-Z])([A-Z]{2})\d{9,}(?!\d)", text)
    vat_prefix = m_vat.group(1).upper() if m_vat else ""
    if not vat_prefix:
        # 2) "GERMANY" keyword in header
        header = "\n".join(lines[:10])
        if re.search(r"\bGERMANY\b", header):
            vat_prefix = "DE"
    data["beneficiary_country"] = _COUNTRY_MAP.get(vat_prefix, vat_prefix)

    # ── Beneficiary address ───────────────────────────────────────────────────
    # Letterhead lines after entity name, before "BOSCH"/page marker
    addr_lines = []
    for l in lines[1:10]:
        if re.match(r"^(BOSCH|Page\s*\d|Page\d|Original|DE\d{9})", l, re.IGNORECASE):
            continue
        if re.match(r"^(Invoice|Billing)", l, re.IGNORECASE):
            break
        if re.match(r"^\d{5}|^[A-Z][a-z]|^GERMANY|^Robert-Bosch", l):
            clean = re.sub(r"\s*\.$", "", _decompress(l)).strip()
            addr_lines.append(clean)
        if re.match(r"^GERMANY", l):
            break
    data["beneficiary_address"] = ", ".join(addr_lines)

    # ── Remitter (Bill to Party) ───────────────────────────────────────────────
    data["remitter_country"] = "India"

    # "Bill to PartyAddress" or "Bill to Party Address" (space optional)
    m_block = re.search(
        r"Bill\s+to\s+Party\s*Address[^\n]*\n"
        r"(.*?)\n(\d{5,6}),?\s*(?:INDIEN|INDIA|INDIE)",
        text, re.IGNORECASE | re.DOTALL,
    )

    remitter_name = ""
    remitter_addr_parts = []

    if m_block:
        raw_block = m_block.group(1)
        pincode   = m_block.group(2).strip()
        block_lines = [l.strip() for l in raw_block.splitlines() if l.strip()]

        name_parts = []
        addr_collecting = False

        for l in block_lines:
            # Skip lines that ARE entirely right-column table data
            if re.match(r"^(?:Payer|BillingDate|Billing\s+Date)\b", l, re.IGNORECASE) and \
               re.search(r"\d{4,}", l):
                continue
            # Strip right-column noise: "BillingDate 26.11.2025", "Payer 4001020584"
            l_clean = re.sub(
                r"\s+(?:Billing\s*Date\s+\S+|Payer\s+\d+|\d{10})$", "", l
            ).strip()
            l_clean = re.sub(r"\s+Payer\s+\d+$", "", l_clean).strip()
            if not l_clean:
                continue

            if not addr_collecting and not name_parts:
                name_parts.append(_decompress(l_clean))
                continue

            # Legal-suffix continuation of name
            if not addr_collecting and re.match(
                r"^(Private\s*Limited|Limited|Ltd\.?|Pvt\.?\s*Ltd\.?|Private\s*Ltd\.?)$",
                _decompress(l_clean), re.IGNORECASE,
            ):
                name_parts.append(_decompress(l_clean))
                continue

            addr_collecting = True
            remitter_addr_parts.append(_decompress(l_clean))

        remitter_name = " ".join(name_parts)
        if remitter_addr_parts:
            remitter_addr_parts.append(pincode)

    data["remitter_name"] = remitter_name
    data["remitter_address"] = ", ".join(remitter_addr_parts)

    # ── Invoice number: alphanumeric "Billing Document AG00038371" ────────────
    m_inv = re.search(
        r"(?:Billing\s*Document|BillingDocument)\s+([A-Z0-9]+)",
        text, re.IGNORECASE,
    )
    data["invoice_number"] = m_inv.group(1).strip() if m_inv else ""

    # ── Invoice date ───────────────────────────────────────────────────────────
    m_date = re.search(
        r"(?:Billing\s*Date|BillingDate)\s+(\d{2}\.\d{2}\.\d{4})",
        text, re.IGNORECASE,
    )
    data["invoice_date"] = m_date.group(1).strip() if m_date else ""

    # ── Amount & currency ──────────────────────────────────────────────────────
    # "Gross value" repeats on every page header; rfind gets the LAST (summary page).
    # Text may be "Gross value 2,935.29 EUR" or "Grossvalue 2.589,31EUR" (compressed).
    m_gross = re.search(r"Gross\s*value", text, re.IGNORECASE)
    gross_pos = m_gross.start() if m_gross else -1
    # Use rfind for the LAST occurrence
    for mo in re.finditer(r"Gross\s*value", text, re.IGNORECASE):
        gross_pos = mo.start()

    if gross_pos != -1:
        after_gross = text[gross_pos:]
        # Match amount+currency with optional space between them
        m_amt = re.search(
            r"([\d.,]+\.\d{2}|[\d.]+,\d{2})\s*(EUR|USD|GBP|JPY|CZK)",
            after_gross, re.IGNORECASE,
        )
    else:
        m_amt = None

    if m_amt:
        data["currency"] = m_amt.group(2).upper()
        data["amount_foreign"] = _normalize_amount(m_amt.group(1))
    else:
        # Fallback: Net value
        m_net = re.search(
            r"Net\s*value\s*([\d.,]+\.\d{2}|[\d.]+,\d{2})\s*(EUR|USD|GBP|JPY|CZK)",
            text, re.IGNORECASE,
        )
        data["currency"] = m_net.group(2).upper() if m_net else ""
        data["amount_foreign"] = _normalize_amount(m_net.group(1)) if m_net else ""

    return data