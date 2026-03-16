"""
Extractor for SAP SE invoices issued to Bosch India entities.

Document structure (may arrive as a composite PDF):
  - Optional pages: SRN Payment Request (Bosch internal approval wrapper)
  - Core pages:     SAP SE invoice (1 or 2 pages)
  - Optional pages: Approval email chain

Key label patterns:
  - "Payee Name: 97304493 : SAP SE"          (from SRN wrapper)
  - "Invoice No. XXXXXXXXXX"                  (from SAP invoice)
  - "Invoice Date:DD.MM.YYYY"
  - "Final Amount 225.00 EUR"
  - Remitter block: multi-line address after the India entity name
  - Beneficiary address: "Payee Address: SAP SE, ..." or SAP header block
"""
import re


_INDIA_ENTITIES = re.compile(
    r"Bosch\s+(?:"
    r"Global\s+Software\s+Technologies"
    r"|Limited"
    r"|Ltd\."
    r"|Automotive\s+Electronics"
    r"|Rexroth"
    r")",
    re.IGNORECASE,
)

_COUNTRY_MAP = {
    "DE": "Germany", "FR": "France", "GB": "UK", "US": "USA",
    "NL": "Netherlands", "CH": "Switzerland", "AT": "Austria",
    "SE": "Sweden",
}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _clean(s):
    return re.sub(r"\s+", " ", s).strip().strip(",").strip()


def _normalize_amount(s):
    """'225.00' stays; '1.939,31' (European) → '1,939.31'."""
    s = s.strip()
    if re.match(r"^\d{1,3}(\.\d{3})*,\d{2}$", s):
        integer = s.rsplit(",", 1)[0].replace(".", "")
        decimal = s.rsplit(",", 1)[1]
        return f"{int(integer):,}.{decimal}"
    return s


# ── Main extractor ─────────────────────────────────────────────────────────────

def extract(text, words=None):
    data = {}

    # ── Beneficiary (SAP SE — the foreign payee) ──────────────────────────────

    # Strategy 1: SRN wrapper "Payee Name: 97304493 : SAP SE"
    # Real OCR structure is two-column: labels appear first, then values separately.
    # e.g. "Payee Name:\nPayee Address:\n...\n97304493 : SAP SE\nSAP SE, Dietmar..."
    # Most reliable: match the vendor-code line "97304493 : SAP SE" directly.
    m = re.search(r"(?m)^\d{5,10}\s*:\s*(.+)$", text, re.IGNORECASE)
    if m:
        data["beneficiary_name"] = _clean(m.group(1))
    else:
        # Fallback: "Payee Name:" label followed closely by value on same/next line
        m2 = re.search(
            r"Payee\s+Name[^\S\n]*:[^\S\n]*(?:\n[^\S\n]*)?(?:\d+[^\S\n]*:[^\S\n]*)?([^\n:]+)",
            text, re.IGNORECASE
        )
        val = _clean(m2.group(1)) if m2 else ""
        # Reject if we captured a label name (contains colon or is empty)
        if val and ":" not in val:
            data["beneficiary_name"] = val
        else:
            # Last resort: standalone "SAP SE" line
            m3 = re.search(r"(?m)^SAP\s+SE\s*$", text, re.IGNORECASE)
            data["beneficiary_name"] = "SAP SE" if m3 else "SAP SE"

    # Beneficiary country: SAP SE is always Germany; verify via VAT if present
    m_vat = re.search(r"VAT\s+identification\s+number\s*:?\s*(DE)\d+", text, re.IGNORECASE)
    data["beneficiary_country"] = _COUNTRY_MAP.get(
        m_vat.group(1).upper() if m_vat else "DE", "Germany"
    )

    # Beneficiary address:
    # Strategy 1: "Payee Address: SAP SE, Dietmar-Hopp-Allee 16, 69190 Walldorf"
    m_pa = re.search(r"Payee\s+Address\s*:\s*SAP\s+SE\s*,\s*(.+)", text, re.IGNORECASE)
    if m_pa:
        data["beneficiary_address"] = _clean(m_pa.group(1))
    else:
        # Strategy 2: SAP SE header block — line after "SAP SE" containing the street
        m_hdr = re.search(
            r"(?m)^SAP\s+SE\s*\n(.+?)\n(\d{5}\s+\w+)",
            text, re.IGNORECASE
        )
        if m_hdr:
            data["beneficiary_address"] = _clean(
                f"{m_hdr.group(1).strip()}, {m_hdr.group(2).strip()}"
            )
        else:
            # Strategy 3: extract from footer reference line
            m_ftr = re.search(
                r"SAP\s+SE\s*,\s*(Dietmar-Hopp-Allee\s+\d+,\s*\d{5}\s+\w+)",
                text, re.IGNORECASE
            )
            data["beneficiary_address"] = _clean(m_ftr.group(1)) if m_ftr else ""

    # ── Remitter (Bosch India entity — the payer) ─────────────────────────────
    data["remitter_country"] = "India"

    # Remitter name: find the India-side Bosch entity
    # May span two lines: "Bosch Global Software Technologies\nPrivate Limited"
    m_ent = re.search(
        r"(Bosch\s+Global\s+Software\s+Technologies(?:\s*\nPrivate\s+Limited)?)",
        text, re.IGNORECASE
    )
    if m_ent:
        name = re.sub(r"\s+", " ", m_ent.group(1)).strip()
        # Ensure "Private Limited" is appended even if on next line
        if "private limited" not in name.lower():
            idx = m_ent.end()
            rest = text[idx:idx+40]
            if re.match(r"\s*Private\s+Limited", rest, re.IGNORECASE):
                name += " Private Limited"
        data["remitter_name"] = name
    else:
        # Fallback: any India-side Bosch entity
        m_fb = _INDIA_ENTITIES.search(text)
        data["remitter_name"] = _clean(m_fb.group(0)) if m_fb else ""

    # Remitter address: multi-line block after entity name, before country marker
    # Pattern: street \n CITY PINCODE \n INDIA
    addr = ""
    # Extract the entire block between "Private Limited" and "INDIA",
    # then pick out street and city-pincode lines, ignoring the SAP SE
    # header block that OCR sometimes injects between them.
    addr = ""
    m_block = re.search(
        r"Private\s+Limited(.*?)(?:\bINDIA\b|\bIndia\b)",
        text, re.IGNORECASE | re.DOTALL
    )
    if m_block:
        block_lines = [l.strip() for l in m_block.group(1).splitlines() if l.strip()]
        street, city_pin = "", ""
        for line in block_lines:
            # Skip: page markers, phone/fax/email, SAP header, foreign postcodes
            if re.match(
                r"(?:Page\s+\d+|T\s+\+|F\s+\+|SAP\s+SE"
                r"|\d{4,5}[,\s]|\w+@|werner\.|Dietmar|Walldorf)",
                line, re.IGNORECASE
            ):
                continue
            # City + 6-digit pincode (all-caps city name)
            if re.match(r"[A-Z]{3,}\s+\d{6}$", line):
                city_pin = line
                continue
            # Street: has comma and is not a metadata line
            if "," in line and not street:
                street = line
        if street and city_pin:
            mp = re.match(r"^(.+?)\s+(\d{6})$", city_pin)
            city = mp.group(1).strip() if mp else city_pin
            pin = mp.group(2) if mp else ""
            addr = f"{street}, {city} - {pin}" if pin else f"{street}, {city}"
        elif city_pin:
            addr = city_pin
    if not addr:
        # Fallback: "Sold-to-Party: 1242414, Bosch Global Software Technologies, Bangalore, India"
        m_sold = re.search(
            r"Sold-to-Party\s*:\s*\d+\s*,\s*Bosch[^,]+,\s*([^,]+),\s*India",
            text, re.IGNORECASE
        )
        if m_sold:
            addr = m_sold.group(1).strip() + ", India"
    data["remitter_address"] = addr

    # ── Invoice number ────────────────────────────────────────────────────────
    # "Invoice No. 6001551718" or "Invoice No. 6001551718 PO NUMBER ..."
    m_inv = re.search(
        r"Invoice\s+No\.?\s+(\d{7,12})",
        text, re.IGNORECASE
    )
    if not m_inv:
        # SRN wrapper: "Bill No. 6001551718"
        m_inv = re.search(r"Bill\s+No\.?\s+(\d{7,12})", text, re.IGNORECASE)
    data["invoice_number"] = m_inv.group(1).strip() if m_inv else ""

    # ── Invoice date ──────────────────────────────────────────────────────────
    # "Invoice Date:08.01.2026"
    m_date = re.search(
        r"Invoice\s+Date\s*:?\s*(\d{2}[./]\d{2}[./]\d{4})",
        text, re.IGNORECASE
    )
    if not m_date:
        # SRN wrapper: "Bill Date 08-Jan-2026"
        m_date = re.search(
            r"Bill\s+Date\s+(\d{2}-[A-Za-z]+-\d{4})",
            text, re.IGNORECASE
        )
    data["invoice_date"] = m_date.group(1).strip() if m_date else ""

    # ── Amount & currency ─────────────────────────────────────────────────────
    # Priority: "Final Amount 225.00 EUR" → header "225.00 EUR" → "Total net value"

    m_final = re.search(
        r"Final\s+Amount\s+([\d,. ]+)\s*(EUR|USD|GBP|CHF|JPY|INR)",
        text, re.IGNORECASE
    )
    if m_final:
        data["currency"] = m_final.group(2).upper()
        data["amount_foreign"] = _normalize_amount(m_final.group(1).strip())
    else:
        # "Invoice No. XXXXXXXXXX PO NUMBER : 225.00 EUR"
        m_hdr_amt = re.search(
            r"Invoice\s+No\.?\s+\d+\s+PO\s+NUMBER\s*:\s*([\d,.]+)\s+(EUR|USD|GBP|CHF)",
            text, re.IGNORECASE
        )
        if m_hdr_amt:
            data["currency"] = m_hdr_amt.group(2).upper()
            data["amount_foreign"] = _normalize_amount(m_hdr_amt.group(1).strip())
        else:
            # "Total net value 225.00 EUR"
            m_net = re.search(
                r"Total\s+net\s+value\s+([\d,. ]+)\s*(EUR|USD|GBP|CHF)",
                text, re.IGNORECASE
            )
            if m_net:
                data["currency"] = m_net.group(2).upper()
                data["amount_foreign"] = _normalize_amount(m_net.group(1).strip())
            else:
                # SRN wrapper: "Approved Amount: 225" + "Currency: Euro"
                m_srn_amt = re.search(r"Approved\s+Amount\s*:\s*([\d,.]+)", text, re.IGNORECASE)
                m_srn_cur = re.search(r"Currency\s*:\s*(Euro|USD|EUR|GBP|Dollar)", text, re.IGNORECASE)
                if m_srn_amt:
                    raw_cur = m_srn_cur.group(1) if m_srn_cur else ""
                    cur_map = {"euro": "EUR", "dollar": "USD", "eur": "EUR",
                               "usd": "USD", "gbp": "GBP"}
                    data["currency"] = cur_map.get(raw_cur.lower(), raw_cur.upper())
                    data["amount_foreign"] = _normalize_amount(m_srn_amt.group(1).strip())
                else:
                    data["amount_foreign"] = ""
                    data["currency"] = ""

    return data