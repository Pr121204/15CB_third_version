import re
from coordinate_utils import reconstruct_line_from_words

# ── Constants ──────────────────────────────────────────────────────────────────

_NAME_STRIP = re.compile(
    r"\s*[,\s]+"
    r"(?:GmbH|SAS|S\.A\.S\.|spol\.\s*s\s*r\.\s*o\.|Ltd\.|\bKFT\b|\bKft\b|"
    r"SE|AG|NV|BV|S\.A\.|S\.p\.A\.|Oy|AB|AS|ApS|Inc\.|LLC|"
    r"France|Deutschland|Japan|Czech|Polska|Zum\s+Eisengiefer).*$",
    re.IGNORECASE,
)

_COUNTRY_MAP = {
    "IT": "Italy",  "ES": "Spain",          "BE": "Belgium",
    "AT": "Austria","SE": "Sweden",          "PL": "Poland",
    "CZ": "Czech Republic",                  "HU": "Hungary", "RO": "Romania", "PT": "Portugal",
    "JP": "Japan",   "KR": "Korea",   "DE": "Germany", "US": "USA",
    "TH": "Thailand", "VN": "Vietnam", "BD": "Bangladesh",
    "AT": "Austria", "ATU": "Austria",
}

# Ordered: most specific / rarest first so they win over the generic "Germany" fallback
_COUNTRY_KEYWORDS = [
    ("Korea",   "Korea"),        ("KOREA",   "Korea"),
    ("Sejong",  "Korea"),        ("Bugang",  "Korea"),
    # Japan — include Kanagawa (prefecture that hosts several Bosch JP offices)
    ("Japan",   "Japan"),        ("JAPAN",   "Japan"),
    ("Kanagawa","Japan"),
    ("France",  "France"),       ("FRANCE",  "France"),
    ("CZECHIA", "Czech Republic"),("Czech",  "Czech Republic"),
    ("Hungary", "Hungary"),      ("Budapest","Hungary"),
    ("Thailand", "Thailand"),    ("THAILAND", "Thailand"),
    ("China",    "China"),       ("CHINA",    "China"),       ("Wuxi", "China"), ("Suzhou", "China"),
    ("Austria",  "Austria"),     ("AUSTRIA",  "Austria"),     ("Wien", "Austria"), ("Hallein", "Austria"),
    ("Bangladesh", "Bangladesh"), ("Dhaka", "Bangladesh"),
    ("USA",      "USA"),         ("LLC",      "USA"),
    ("Michigan", "USA"),         ("MI 48",    "USA"),
    ("Germany", "Germany"),      ("GERMANY", "Germany"),
    ("Stuttgart","Germany"),     ("Gerlingen","Germany"),
]

# Single-word name-wrap tokens that must NOT be treated as address lines
_NAME_WRAP_RE = re.compile(
    r"^(Limited|Private|Pvt\.?|Inc\.?|LLC|GmbH|KFT|S\.A\.S\.|Corporation)\s*$",
    re.IGNORECASE,
)

# Right-column noise patterns that appear merged onto address lines in two-column PDFs
_RIGHT_COL_NOISE_RE = re.compile(
    r"(?:\s+|^)(?:[©®]\s*)?(?:Ship\s+to|Customer\s+No|Contact\s+addr(?:esses)?|Sales\s*:|"
    r"Accounting\s*:|Sz[áa]mla|Vev[oő]sz[áa]m|[Áá]rufogad[oó]|"
    r"Payer|Invoice\s+No|Date\s+Invoice|Supplier|PO\s+Number|Order\s+date|Order\s+from|Customer\s+no|"
    r"licence\s+YEC|Manufacturing\s+licence|Royalty\s+calculation).*",
    re.IGNORECASE,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _normalize_name(raw):
    """Strip trailing standalone brand-logo token (e.g. 'Robert Bosch GmbH BOSCH').
    Keeps full legal name including suffix (GmbH, KFT, etc.).
    """
    # Strip leading artifacts like "k Robert Bosch" or "BOSCH Bosch.IO"
    name = raw.strip()
    name = re.sub(r"^[a-z]\s+", "", name)
    name = re.sub(r"^BOSCH\s+(?=Bosch)", "", name, flags=re.IGNORECASE)
    # Strip address leaks e.g. "Bosch Rexroth AG, Zum Eisengief/ßer..."
    name = re.sub(r",?\s+\bZum\s+Eisengie[sßf]er.*$", "", name, flags=re.IGNORECASE)
    # Strip logo artifacts like "@" or "BOSCH" logo text
    name = re.sub(r"\s+@\s*BOSCH.*$", "", name, flags=re.I)
    name = re.sub(r"\s+@\s*$", "", name).strip()
    name = re.sub(
        r"\s+(?!GmbH|SRL|KFT|NV|BV|SE|AG|AB|AS|LLC|INC|LTD)[A-Z]{2,8}\s*$",
        "", name
    ).strip()

    if name.upper() in ["COVERPAGE", "COVER FOR INVOICE"]:
        return ""

    return (name if name else raw).upper()


def _clean_dispatch_line(text):
    """Extract and clean the Dispatch/Services address line.
    Supports multi-line garbled content common in scanned Bosch invoices.
    """
    m = re.search(
        r"((?:D[\s_]*i[\s_]*s[\s_]*p[\s_]*a[\s_]*t[\s_]*c[\s_]*h"
        r"|S[\s_]*e[\s_]*r[\s_]*v[\s_]*i[\s_]*c[\s_]*e[\s_]*s)"
        r"(?:"
        r"[\s_]*[Aa][\s_]*d[\s_]*d[\s_]*r[\s_]*e[\s_]*s[\s_]*s"  # full: "address"
        r"|[\s_]*[Aa][\s_]*d[\s_]*\.?"                            # abbrev: "ad."
        r")"
        r"[^\n]+)",
        text, re.IGNORECASE,
    )
    if not m:
        return ""
    raw = m.group(1).strip()
    
    # Check for a second line (garbled address part)
    end_idx = m.end()
    rest = text[end_idx:].lstrip()
    if rest:
        next_line = rest.split("\n")[0]
        # Heuristic: if next line has many underscores or PIN code parts
        if re.search(r"_{8,}|[I_N\s-]{8,}\d", next_line, re.IGNORECASE):
            raw += " " + next_line

    cleaned = raw.replace("\n", " ")
    cleaned = re.sub(r"__", " ", cleaned)       # double underscore → word boundary
    cleaned = re.sub(r"_", "", cleaned)     # strip remaining underscores
    cleaned = re.sub(r"(IN[-\s]*\d{6})([A-Za-z])", r"\1 \2", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    return cleaned.strip()


def _is_garbled(text):
    """Return True if >30% of tokens are single characters (garbled OCR)."""
    tokens = text.split()
    if not tokens:
        return True
    return (sum(1 for t in tokens if len(t) == 1) / len(tokens)) > 0.30


def _normalize_pincode_city(addr):
    """Reformat Indian pincode+city tokens to 'City - 560030' style."""
    addr = re.sub(
        r",?\s*IN[-\s]+(\d{6})\s+([A-Za-z][\w\s]+?)(?:\s*,|$)",
        lambda mo: ", " + mo.group(2).strip() + " - " + mo.group(1),
        addr,
    )
    addr = re.sub(
        r"(?<!\d)(\d{6})\s+([A-Z][\w\s]*?)(?:\s*,|$|(?=\d))",
        lambda mo: mo.group(2).strip() + " - " + mo.group(1),
        addr,
    )
    return addr.strip().lstrip(",").strip()


def _clean_address_text(addr):
    """Deep clean a remitter address string: strip name-wrap fragments and OCR noise."""
    if not addr: return ""
    # 1. Strip artifacts like "7,. 0" or loose coords
    addr = re.sub(r"\s+[a-z]\s+\d\s+\d.*$", "", addr, flags=re.IGNORECASE).strip()
    addr = re.sub(r"\b[0\d]\s*[_,]\s*", "", addr).strip() # specific "0 _" or "7." noise
    addr = re.sub(r"_{2,}", " ", addr).strip()
    # 2. Strip name-wrap fragments (e.g. "Ltd., GAT N. 306..." -> "GAT N. 306...")
    # Longest match first.
    addr = re.sub(
        r"^(?:Mobility\s+Platform\s+and\s+Solutions\s+India\s+Private\s+Limited"
        r"|India\s+Private\s+Ltd\.?|Private\s+Limited|Private\s+Ltd\.?|Limited|Private|Pvt\.?|Ltd\.?),?\s*",
        "", addr, flags=re.IGNORECASE
    ).strip()
    return addr

def _extract_bill_to_block(text, name_pattern):
    """Collect address lines from the bill-to block identified by name_pattern.

    Handles:
    - Right-column metadata noise merged onto same line (stripped inline)
    - Multi-line name wraps ('Bosch Rexroth (India) Private' / 'Limited')
    - Stop tokens: INDIEN / INDIA / bare IN, or invoice field labels
    """
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if re.search(name_pattern, line, re.IGNORECASE):
            parts = []
            for j in range(i + 1, min(i + 15, len(lines))):
                l = lines[j].strip()
                # 1. Strip right-column noise & artifacts
                l = _RIGHT_COL_NOISE_RE.sub("", l).strip()
                # Unrecoverable garbled lines (many underscores but no usable text)
                if re.match(r"^[_ \d-]{10,}$", l):
                    continue
                l = re.sub(r"\s+[a-z\d]\s+\d\s+\d.*$", "", l, flags=re.IGNORECASE).strip()
                l = re.sub(r"\s+[a-z\d](\s+\d)+$", "", l, flags=re.IGNORECASE).strip()
                l = re.sub(r"\s+\d$", "", l).strip()

                # 2. Stop logic
                if re.search(r"(India|INDIA|IN\s*:?|INDIEN|Bangladesh)", l, re.IGNORECASE):
                    # If it's a name wrap (e.g. "India Private Ltd"), don't stop, just skip
                    if re.search(r"Private|Limited|Pvt|Ltd", l, re.IGNORECASE):
                        continue
                    # Else it's the final location line or bare country
                    if l and not re.match(r"^[a-z\d\._\s]{1,3}$", l, re.IGNORECASE):
                        parts.append(l)
                    break

                # 3. Skip noise & wrap lines
                if re.match(r"^[a-z\d\._\s]{1,3}$", l, re.IGNORECASE):
                    continue
                if _NAME_WRAP_RE.match(l):
                    continue
                if re.search(
                    r"^(Customer\s+No|Contact\s+addr|Sales\s*:|Accounting\s*:"
                    r"|Sz[áa]mla|Vev[oő]sz[áa]m|[Áá]rufogad[oó]|Payer|Supplier|Attn|Invoice\s+No)",
                    l, re.IGNORECASE
                ):
                    continue
                if l:
                    parts.append(l)
            return parts
    return []


def _extract_remitter_from_block(text):
    """Line-by-line collector for 'Bosch Ltd.' style names.

    Collects address lines after 'Bosch Ltd.' until a country marker.
    Skips English AND Hungarian metadata labels.
    """
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if re.search(r"Bosch\s+L[\s_I|l]*td[\s_I|l]*\.", line, re.IGNORECASE):
            parts = []
            for j in range(i + 1, min(i + 15, len(lines))):
                l = lines[j].strip()
                # 1. Strip artifacts like "c 2 0", "6.", "1", "2."
                l = re.sub(r"\s+[a-z\d]\s+\d\s+\d.*$", "", l, flags=re.IGNORECASE).strip()
                l = re.sub(r"\s+[a-z\d](\s+\d)+$", "", l, flags=re.IGNORECASE).strip()
                l = re.sub(r"\s+\d$", "", l).strip()
                l = _RIGHT_COL_NOISE_RE.sub("", l).strip()
                l = re.sub(r"\s+Ship\s+to.*", "", l, flags=re.IGNORECASE).strip()

                # 2. Stop logic (search is broad but allows city info on same line)
                if re.search(r"(India|INDIA|IN\s*:?|INDIEN|Bangladesh)", l, re.IGNORECASE):
                    if l and not re.match(r"^[a-z\d\._\s]{1,3}$", l, re.IGNORECASE):
                        parts.append(l)
                    break

                # 3. Skip noise lines (single chars or artifacts)
                if re.match(r"^[a-z\d\._\s]{1,3}$", l, re.IGNORECASE):
                    continue
                if re.search(
                    r"^(Customer\s+No|Contact\s+addr|Sales\s*:|Accounting\s*:"
                    r"|Vev[oő]sz[áa]m|[Áá]rufogad[oó]|Kapcsolattart[oó]"
                    r"|[Éé]rt[eé]kes[ií]t[eé]s|K[oö]nyvel[eé]s|A\s+mi\s+EU|Attn|Invoice\s+No)",
                    l, re.IGNORECASE
                ):
                    continue
                if l:
                    parts.append(l)
            return parts
    return []


# ── Main extractor ─────────────────────────────────────────────────────────────

def extract(text, words=None):
    data = {}

    _name_lines = []
    lines = text.splitlines()
    for i, _l in enumerate(lines):
        _l = _l.strip()
        if not _l:
            continue
            
        # 0. Skip cover page junk
        if re.search(r"^(coverpage|cover\s+for\s+invoice|invoice\s*no\.?)$", _l.rstrip(":"), re.I):
            if not _name_lines:
                continue

        # 1. Break on definitive invoice/page headers
        # Even if they have "Invoice No" or "Date Invoice" and are long
        if re.search(r"\b(Invoice|Sz[áa]mla|BillingDocument|Page)\b", _l, re.I):
            if len(_l) < 25 or re.search(r"Invoice\s*No|Date\s*Invoice", _l, re.I):
                if not re.search(r"Bosch", _l, re.I):
                    if _name_lines:
                        break
                    else:
                        continue

        # 2. Skip solo "Page" or "Invoice" noise lines (already matched by break if appropriate)
        if re.search(r"^(Page\s*[\d/ ]+|Invoice\s*[\d/ ]*)$", _l, re.IGNORECASE) and not re.search(r"Bosch", _l, re.I):
            continue
            
        _l_clean = re.sub(r"\s*/\s*\d.*$", "", _l).strip()   # strip "/ 2" page suffixes
        if not _l_clean:
            continue

        _name_lines.append(_l_clean)
        
        # 3. Break on legal suffix
        if re.search(
            r"\b(GmbH|SRL|KFT|Kft\.?|Ltd\.?|Limited|S\.A\.S\.|spol\.|"
            r"Corp\.|Inc\.|LLC|AG|NV|BV|SE|Company)\b",
            _l_clean, re.IGNORECASE
        ):
            # Peak ahead: if next line is a parenthetical branch note like "(Suzhou)", don't break yet
            if i + 1 < len(lines):
                next_l = lines[i+1].strip()
                if next_l.startswith("(") and next_l.endswith(")"):
                    continue
            break
            
    raw_name = " ".join(_name_lines) if _name_lines else ""
    raw_name = re.sub(r"\s*/\s*$", "", raw_name).strip()
    data["beneficiary_name"] = _normalize_name(raw_name)

    # ── Beneficiary country ───────────────────────────────────────────────────
    # Priority: (1) labeled VAT ID → (2) standalone VAT line → (3) Company address
    # line (focused, avoids false positives) → (4) broad first-25-line search

    m_vat = re.search(r"Our\s+VAT\s+ID\s+No\s*[:;\s]+\s*([A-Z]{2,3})\d+", text, re.IGNORECASE)
    if not m_vat:
        # Standalone VAT number on its own line e.g. "DE294064848", "ATU14719303"
        # Handles 2 or 3 letter prefixes (ATU)
        m_vat = re.search(r"(?m)^([A-Z]{2,3})\d{8,}\s*$", text)
    if m_vat:
        vat_prefix = m_vat.group(1).upper()
        data["beneficiary_country"] = _COUNTRY_MAP.get(vat_prefix, vat_prefix)
    else:
        # Use Company address line as the primary search zone — most reliable
        # because it explicitly names the beneficiary's location.
        # (From the attached code: smarter for entities like Bosch Japan / Kanagawa)
        m_ca_line = re.search(r"Company\s+(?:address|ad\.)\s*:?\s*(.+)", text, re.IGNORECASE)
        header = "\n".join(text.splitlines()[:25])
        search_zone = (m_ca_line.group(1) if m_ca_line else "") + "\n" + header
        country_found = ""
        for keyword, country in _COUNTRY_KEYWORDS:
            if keyword in search_zone:
                country_found = country
                break
        data["beneficiary_country"] = country_found or "DE"

    # ── Beneficiary address ───────────────────────────────────────────────────
    # Priority: Company address/ad. label → Headquarter (with coord reconstruction)
    # → Siège Social (French entities)
    # Pre-clean any garbled "C_o_mp_an_y _ad_d_re_ss_" line before searching.
    _ca_text = text
    for _line in text.splitlines():
        if re.match(r"C_o_mp_an_y", _line.strip(), re.IGNORECASE):
            _cl = re.sub(r"__", " ", _line)
            _cl = re.sub(r"_", "", _cl)
            _cl = re.sub(r"\s{2,}", " ", _cl).strip()
            _ca_text = text.replace(_line, _cl, 1)
            break

    m_ca = re.search(
        r"Company\s+(?:address|ad\.)\s*:?\s*([\s\n]*)(.+)",
        _ca_text, re.IGNORECASE
    )
    # Rexroth header pattern is highly specific and reliable
    m_rh = re.search(r"Bosch\s+Rexroth\s+AG,?\s*(Zum\s+Eisengie[sßf]er.*)", text, re.I)

    if m_ca:
        addr = m_ca.group(2).strip().rstrip(",").strip()
        # Collect subsequent lines if they look like address parts
        idx = _ca_text.find(m_ca.group(0))
        rest = _ca_text[idx + len(m_ca.group(0)):].lstrip()
        for cand_line in rest.splitlines()[:8]:
            cand_l = cand_line.strip()
            if not cand_l: continue
            if re.search(r"\b(ISHO|Timisoara|Judet|postal|Timis|Office|Building|Floor|Bulevardul|Hills|Drive|MI|USA|Legal|Street|St\.|Road|Avenue|Lane|P\.O\.?|Box|MI\s+\d|Farmington)\b", cand_l, re.I):
                addr += ", " + cand_l
            elif re.search(r"Robert\s+Bosch\s+LLC", cand_l, re.I):
                continue # name repeat in block
            elif re.match(r"^\d{5}(?:-\d{4})?$", cand_l): # USA zipcode alone on line
                addr += " " + cand_l
            elif re.search(r"^[A-Z][a-z]+,\s*[A-Z]{2}\s+\d{5}", cand_l): # Farmington Hills, MI 48331
                addr += " " + cand_l
            else:
                # If we already have some address, and this line doesn't match, stop.
                if len(addr.split()) > 4:
                    break
        
        addr = addr.replace("&e", "ße")
        addr = addr.replace("Sc8hillerhoehe", "Schillerhoehe")
        addr = addr.replace("Gerli 3n1gen", "Gerlingen")
        data["beneficiary_address"] = re.sub(r",\s*$", "", addr)
    elif m_rh:
        data["beneficiary_address"] = m_rh.group(1).split("\n")[0].strip()
    else:
        # Headquarter line — use coordinate reconstruction to fix split diacritic chars
        hq_line = reconstruct_line_from_words(words or [], "Headquarter") if words else ""
        # Filter out footers like "Firmensitz/Headquarters"
        if hq_line and re.search(r"Registration|HRB|Amtsgericht|Stuttgart", hq_line, re.I):
            hq_line = ""

        if not hq_line:
            # Anchor to start of line to avoid registration sections like "Firmensitz/Headquarters"
            m_hq = re.search(r"^Headquarter\s*:?\s*(.+)", text, re.I | re.M)
            if m_hq and not re.search(r"Registration|HRB|Amtsgericht|Stuttgart", m_hq.group(0), re.I):
                hq_line = m_hq.group(1).strip().rstrip(".")
            else:
                hq_line = ""

        if hq_line:
            hq_line = re.sub(r"^.*?Headquarter\s*:?\s*", "", hq_line, flags=re.I).strip()
            data["beneficiary_address"] = hq_line
        else:
            # Siège Social — French entities footer pattern
            m_ss = re.search(
                r"Si.ge\s+Social\s*:?\s*(.+?)(?:\s*[-\u2013]\s*(?:France|N°|TVA|N\b))",
                text, re.IGNORECASE | re.DOTALL,
            )
            if m_ss:
                addr = m_ss.group(1).strip().replace("\n", " ")
                addr = re.sub(r"([a-z])([A-Z])", r"\1 \2", addr)
                addr = re.sub(r"\s{2,}", " ", addr).strip()
                addr = addr.replace("&e", "ße")
                data["beneficiary_address"] = addr
            else:
                # Bangladesh specific address pattern (Building...House-...Bangladesh)
                # Handles OCR typos like "Hcouse"
                m_bd = re.search(r"((?:“[^”]+”|[\w\s]+)?[,\s]*H[co]*use-.*?Bangladesh)", text, re.IGNORECASE | re.S)
                if m_bd:
                    addr = m_bd.group(1).replace("\n", " ")
                    addr = re.sub(r"\s{2,}", " ", addr).strip()
                    data["beneficiary_address"] = addr
                else:
                    # Rexroth header pattern "Bosch Rexroth AG, Zum Eisengief/ßer 1, ..."
                    m_rh = re.search(r"Bosch\s+Rexroth\s+AG,?\s*(Zum\s+Eisengie[sßf]er.*)", text, re.I)
                    if m_rh:
                        data["beneficiary_address"] = m_rh.group(1).split("\n")[0].strip()
                    else:
                        data["beneficiary_address"] = ""

    # ── Remitter (India-side entity) ──────────────────────────────────────────
    data["remitter_country"] = "India"

    m_limited    = re.search(r"BOSCH\s+LIM:?ITED", text, re.IGNORECASE)
    m_rexroth    = re.search(r"Bosch\s+Rexroth\s*\(India\)", text, re.IGNORECASE)
    m_bgsw       = re.search(r"Bosch\s+Global\s+Software\s+Technologies", text, re.IGNORECASE)
    # Automotive: may split "Bosch Automotive Electronics" / "India Private Ltd." across lines
    m_automotive = re.search(
        r"Bosch\s+Automotive\s+Electronics"
        r"(?:\s+India\s+Private"                            # single-line form
        r"|[^\n]*\n(?:[^\n]*\n){0,3}?[^\n]*India\s+Private)",  # split across ≤3 lines
        text, re.IGNORECASE
    )
    # Chassis Systems: may split "Bosch Chassis Systems India" / "Private Ltd." across lines
    m_chassis = re.search(
        r"Bosch\s+Chassis\s+Systems\s+India"
        r"(?:\s+Private\s+Ltd"
        r"|[^\n]*\n(?:[^\n]*\n){0,3}?[^\n]*Private\s+Ltd)",
        text, re.IGNORECASE
    )
    m_mobility = re.search(r"Bosch\s+Mobility\s+Platform", text, re.I)
    m_etas     = re.search(r"ETAS\s+Automotive\s+India", text, re.I)
    m_ltd        = re.search(r"Bosch\s+L[\s_I|l]*td[\s_I|l]*\.", text, re.IGNORECASE)

    if m_mobility:
        data["remitter_name"] = "Bosch Mobility Platform and Solutions India Private Limited"
    elif m_etas:
        data["remitter_name"] = "ETAS Automotive India Private Ltd."
    elif m_limited:
        data["remitter_name"] = "BOSCH LIMITED"
    elif m_rexroth:
        data["remitter_name"] = "Bosch Rexroth (India) Private Limited"
    elif m_bgsw:
        data["remitter_name"] = "Bosch Global Software Technologies Private Limited"
    elif m_automotive:
        data["remitter_name"] = "Bosch Automotive Electronics India Private Ltd."
    elif m_chassis:
        data["remitter_name"] = "Bosch Chassis Systems India Private Ltd."
    elif m_ltd:
        data["remitter_name"] = "Bosch Ltd."
    else:
        data["remitter_name"] = ""

    # ── Remitter address — 7-strategy waterfall ───────────────────────────────
    matched = False

    # Strategy 1: Rexroth — bill-to block preferred (dispatch line omits intermediate lines)
    if not matched and m_rexroth:
        block_lines = _extract_bill_to_block(text, r"Bosch\s+Rexroth\s*\(India\)")
        if block_lines:
            addr = _normalize_pincode_city(", ".join(block_lines))
            data["remitter_address"] = _clean_address_text(addr)
            matched = True

    # Strategy 1.5: Automotive — bill-to block (cleaner than dispatch if available)
    if not matched and m_automotive:
        block_lines = _extract_bill_to_block(text, r"Bosch\s+Automotive\s+Electronics")
        if block_lines:
            addr = _normalize_pincode_city(", ".join(block_lines))
            data["remitter_address"] = _clean_address_text(addr)
            matched = True

    # Strategy 1.6: Chassis Systems — bill-to block
    if not matched and m_chassis:
        block_lines = _extract_bill_to_block(text, r"Bosch\s+Chassis\s+Systems\s+India")
        if block_lines:
            addr = _clean_address_text(", ".join(block_lines))
            data["remitter_address"] = _normalize_pincode_city(addr)
            matched = True

    # Strategy 1.7: Mobility Platform — bill-to block
    if not matched and m_mobility:
        block_lines = _extract_bill_to_block(text, r"Bosch\s+Mobility\s+Platform")
        if block_lines:
            addr = _clean_address_text(", ".join(block_lines))
            data["remitter_address"] = _normalize_pincode_city(addr)
            matched = True

    # Strategy 1.8: ETAS — bill-to block
    if not matched and m_etas:
        block_lines = _extract_bill_to_block(text, r"ETAS\s+Automotive\s+India")
        if block_lines:
            addr = _clean_address_text(", ".join(block_lines))
            data["remitter_address"] = _normalize_pincode_city(addr)
            matched = True

    # Strategy 2: Dispatch / Services address line (skipped if garbled OCR)
    if not matched:
        dispatch = _clean_dispatch_line(text)
        if dispatch and not _is_garbled(dispatch):
            m_d = re.search(
                r"(?:Dispatch|Services)\s*(?:address|ad\.)\s*:?\s*(?:Bosch|ETAS)[^,]+,\s*(.+)",
                dispatch, re.IGNORECASE,
            )
            if m_d:
                addr = m_d.group(1).strip()
                addr = _clean_address_text(addr)
                data["remitter_address"] = _normalize_pincode_city(addr)
                matched = True

    # Strategy 3: SIPCOT industrial park anchor (Tirunelveli-style)
    if not matched:
        m_a = re.search(
            r"(SIPCOT[^\n]+)\n(Plot[^\n]+)\n([^\n]+)\n(\d{6})[^\n]*",
            text, re.IGNORECASE,
        )
        if m_a:
            data["remitter_address"] = (
                f"{m_a.group(1).strip()}, {m_a.group(2).strip()}, "
                f"{m_a.group(3).strip()} - {m_a.group(4).strip()}"
            )
            matched = True

    # Strategy 4: POST BOX in dispatch line (KFT / BANGALORE style)
    if not matched:
        m_pb = re.search(
            r"POST\s+BOX\s*:?\s*(\d+)\s+([A-Z][A-Z\s]+),\s*IN[-\s]*(\d{6})\s+([A-Za-z]+)",
            text, re.IGNORECASE,
        )
        if m_pb:
            data["remitter_address"] = (
                f"POST BOX {m_pb.group(1).strip()} {m_pb.group(2).strip()}, "
                f"{m_pb.group(4).strip()} - {m_pb.group(3).strip()}"
            )
            matched = True

    # Strategy 5: Bill-to block for BOSCH LIMITED (garbled KFT invoice style)
    if not matched and m_limited:
        block_lines = _extract_bill_to_block(text, r"BOSCH\s+LIM:?ITED")
        if block_lines:
            data["remitter_address"] = ", ".join(block_lines)
            matched = True

    # Strategy 6: Line-by-line collector for "Bosch Ltd."
    if not matched:
        block_lines = _extract_remitter_from_block(text)
        if block_lines:
            data["remitter_address"] = ", ".join(block_lines)
            matched = True

    # Strategy 7: Street-before-Ship-to then pincode+CITY+INDIA (simple 2-line)
    if not matched:
        m_b = re.search(
            r"^([\w\s]+?)\s+Ship\s+to[^\n]*\n(\d{6})\s+([A-Z]{2,})\s*\n(?:INDIA|IN\b)",
            text, re.MULTILINE | re.IGNORECASE,
        )
        data["remitter_address"] = (
            f"{m_b.group(1).strip()}, {m_b.group(3).strip().title()} - {m_b.group(2).strip()}"
            if m_b else ""
        )

    # English "Invoice No." and Hungarian "Számla szám" (diacritic-tolerant)
    # Robust against OCR artifacts like "+" or " Doc"
    # Allows slashes and dashes for Bangladesh/complex numbers e.g. "301000002/23025"
    # Allows intermediate noise/digits e.g. "Invoice No. 2 7057441295"
    # Allows "Doc. no./date:" for Rexroth
    m_inv = re.search(
        r"(?:Invoice\s*(?:No\.?|Doc)|Sz[áa]mla\s+sz[áa]m|Doc\.\s*no\./date)\s*[:+>]*\s*(?:\b\d\b\s+)?([A-Z0-9/-]*[0-9][A-Z0-9/-]*)",
        text, re.IGNORECASE
    )
    inv_no = m_inv.group(1).strip() if m_inv else ""
    # Ensure it's likely a number (at least 5 chars or has digits)
    if len(inv_no) < 5 and not re.search(r"\d", inv_no):
        inv_no = ""
    data["invoice_number"] = inv_no

    # ── Invoice date ──────────────────────────────────────────────────────────
    # Allows up to 25 chars of OCR noise between label and date value.
    # Supports DD.MM.YYYY, DD/MM/YYYY, and DDMonth YYYY (e.g. 31December 2025)
    # Supports comma separators (e.g. 15,05, 2023)
    # Supports separator "|" after doc number label
    # Year part \d{4,5} can be garbled e.g. "20725" -> take first 4 "2025" (or similar)
    m_date = re.search(
        r"(?:Date\s+Invoice|Sz[áa]mla\s+kelte|Date|Doc\.\s*no\./date)\s*[:\.]?\s*[^\n]{0,25}?"
        r"(?:\d{5,}\s*[|/]\s*)?"
        r"(\d{1,2}(?:[./ ,]\s*\d{1,2}|[A-Z][a-z]+)[./ ,]\s*\d{4,5})",
        text, re.IGNORECASE
    )
    dt = m_date.group(1).strip() if m_date else ""
    if dt:
        # Normalize: replace commas with dots and strip internal spaces
        dt = re.sub(r",", ".", dt)
        dt = re.sub(r"\s+", "", dt)
        # Year part: handle garbled 5-6 digit strings specifically looking for 20XX
        # e.g. "27025" -> "2025", "32025" -> "2025"
        m_year = re.search(r"(202\d)", dt)
        if m_year:
            # Reconstruct with the found year if it was likely prefixed by noise
            dt = re.sub(r"\d{4,6}$", m_year.group(1), dt)
        else:
            # Fallback to existing 5th-digit truncate
            dt = re.sub(r"(\d{4})\d$", r"\1", dt)
    data["invoice_date"] = dt

    # ── Amount & currency ─────────────────────────────────────────────────────
    # Bosch Germany invoices may include a VAT breakdown in the form:
    #   Net amount:  CNY 5,330.81
    #   Value Added Tax (VAT) : 6.000 % CNY 319.85
    #   Invoice amount : CNY 5,650.66
    #
    # Strategy (each label searched independently, not via first-match):
    #   1. "Invoice amount"  → always the total payable (highest priority)
    #   2. "Net amount"      → base before VAT
    #   3. "Value Added Tax" → VAT component
    #   4. Fallback labels   → Total Invoice Value / Total amount / Value of goods
    _CURR_RE = r"(EUR|USD|GBP|CHF|JPY|CZK|HUF|THB|VND|CNY)"
    _AMT_RE  = r"([\d,.]+)"

    # Search for each of the three VAT-breakdown labels independently.
    m_inv_amt = re.search(
        rf"Invoice\s+amount\s*:?\s*{_CURR_RE}\s*{_AMT_RE}",
        text, re.IGNORECASE,
    )
    m_net_amt = re.search(
        rf"Net\s+amount\s*:?\s*{_CURR_RE}\s*{_AMT_RE}",
        text, re.IGNORECASE,
    )
    m_vat_amt = re.search(
        rf"Value\s+Added\s+Tax\s*(?:\(VAT\))?\s*:?\s*[\d.]+\s*%\s*{_CURR_RE}\s*{_AMT_RE}",
        text, re.IGNORECASE,
    )

    if m_inv_amt:
        # Explicit invoice total found — use it as amount_foreign
        data["currency"] = m_inv_amt.group(1).upper()
        data["amount_foreign"] = m_inv_amt.group(2).strip()
        # Only treat as VAT case when BOTH net_amount and vat_amount are present
        if m_net_amt and m_vat_amt:
            data["net_amount"] = m_net_amt.group(2).strip()
            data["vat_amount"] = m_vat_amt.group(2).strip()
        else:
            data["net_amount"] = ""
            data["vat_amount"] = ""

    elif m_net_amt and m_vat_amt:
        # No explicit invoice total, but net + VAT are present → compute total
        data["currency"] = m_net_amt.group(1).upper()
        data["net_amount"] = m_net_amt.group(2).strip()
        data["vat_amount"] = m_vat_amt.group(2).strip()
        try:
            from text_utils import parse_invoice_amount  # type: ignore
            net_val = parse_invoice_amount(data["net_amount"])
            vat_val = parse_invoice_amount(data["vat_amount"])
            if net_val is not None and vat_val is not None:
                data["amount_foreign"] = f"{net_val + vat_val:.2f}"
            else:
                # Parsing failed; fall through to fallback below
                data["net_amount"] = ""
                data["vat_amount"] = ""
                data["amount_foreign"] = ""
        except Exception:
            data["net_amount"] = ""
            data["vat_amount"] = ""
            data["amount_foreign"] = ""

    else:
        # No VAT breakdown — standard single-amount extraction
        data["net_amount"] = ""
        data["vat_amount"] = ""

    # Fallback when amount_foreign is still empty: try other total labels
    if not data.get("amount_foreign"):
        label_m = re.search(
            r"(?:Total\s+amount|Total\s+Invoice\s+Value)",
            text, re.IGNORECASE,
        )
        if label_m:
            zone = text[label_m.end():label_m.end()+60]
            zone = re.sub(r"\(VAT\)|VAT|%|0\.000", "", zone, flags=re.I)
            zone = re.sub(r"EU[\s\n\r]+R", "EUR", zone, flags=re.I)
            m_num = re.search(r"(?:([A-HJ-Z]{3})\s*)?([\d,. ]{4,})", zone, re.IGNORECASE)
            if m_num:
                cur_m = re.search(r"\b(EUR|USD|GBP|CHF|JPY|CZK|HUF|THB|VND|CNY)\b", zone, re.I)
                data["currency"] = cur_m.group(1).upper() if cur_m else ""
                amt_raw = m_num.group(2).strip()
                amt_tokens = [p for p in re.split(r"[\s\n\r]+", amt_raw) if re.search(r"\d", p)]
                if len(amt_tokens) > 1:
                    best = [t for t in amt_tokens if re.search(r"[,.]", t)]
                    data["amount_foreign"] = best[0] if best else amt_tokens[0]
                else:
                    data["amount_foreign"] = amt_tokens[0] if amt_tokens else ""

    if not data.get("amount_foreign"):
        # Final fallback: "Value of Services/goods" label
        m_vs = re.search(
            r"Value\s+of\s+(?:Services|goods)\s*:?\s*(EUR|USD|GBP|CHF|JPY|CZK|HUF|THB|VND|CNY)\s*([\d,. ]+)",
            text, re.IGNORECASE,
        )
        if m_vs:
            data["currency"] = m_vs.group(1).upper()
            data["amount_foreign"] = m_vs.group(2).strip().replace(" ", "")
        else:
            data["amount_foreign"] = ""
            data["currency"] = ""

    return data








# import re
# from coordinate_utils import reconstruct_line_from_words

# _NAME_STRIP = re.compile(
#     r"\s*[,\s]+"
#     r"(?:GmbH|SAS|S\.A\.S\.|spol\.\s*s\s*r\.\s*o\.|Ltd\.|\bKFT\b|\bKft\b|"
#     r"SE|AG|NV|BV|S\.A\.|S\.p\.A\.|Oy|AB|AS|ApS|Inc\.|LLC|"
#     r"France|Deutschland|Japan|Czech|Polska).*$",
#     re.IGNORECASE,
# )

# _COUNTRY_MAP = {
#     "IT": "Italy", "ES": "Spain", "BE": "Belgium", "AT": "Austria",
#     "SE": "Sweden", "PL": "Poland", "CZ": "Czech Republic",
#     "HU": "Hungary", "RO": "Romania", "PT": "Portugal", "JP": "Japan",
#     "KR": "Korea",
#     "DE": "Germany",
# }

# # Ordered: more specific / rarer keywords first so they win over "Germany"
# _COUNTRY_KEYWORDS = [
#     ("Korea", "Korea"), ("KOREA", "Korea"), ("Sejong", "Korea"), ("Bugang", "Korea"),
#     ("Japan", "Japan"), ("JAPAN", "Japan"), ("Kanagawa", "Japan"),
#     ("France", "France"), ("FRANCE", "France"),
#     ("CZECHIA", "Czech Republic"), ("Czech", "Czech Republic"),
#     ("Hungary", "Hungary"), ("Budapest", "Hungary"),
#     ("Germany", "Germany"), ("GERMANY", "Germany"), ("Stuttgart", "Germany"),
# ]

# # Legal-suffix / name-wrap tokens that must NOT be treated as address lines
# _NAME_WRAP_RE = re.compile(
#     r"^(Limited|Private|Pvt\.?|Inc\.?|LLC|GmbH|KFT|S\.A\.S\.|Corporation)\s*$",
#     re.IGNORECASE,
# )

# # Right-column noise patterns that appear on the same line as address content
# _RIGHT_COL_NOISE_RE = re.compile(
#     r"\s+(?:Ship\s+to|Customer\s+No|Contact\s+addr(?:esses)?|Sales\s*:|"
#     r"Accounting\s*:|Sz[áa]mla|Vev[oő]sz[áa]m|[Áá]rufogad[oó]|"
#     r"Payer|Invoice\s+No|Date\s+Invoice|Supplier).*",
#     re.IGNORECASE,
# )


# # ── Helpers ────────────────────────────────────────────────────────────────────

# def _normalize_name(raw):
#     """Keep full legal name but strip trailing brand logo word (e.g. 'BOSCH').
#     'Robert Bosch GmbH BOSCH' -> 'Robert Bosch GmbH'
#     """
#     if re.search(r"Bosch\s+Corporation", raw, re.IGNORECASE):
#         return "Bosch Corporation"

#     # Strip trailing standalone all-caps brand token (2-8 chars, not a known suffix)
#     # also handles preceding @ symbol artifacts (e.g. "@ BOSCH")
#     name = re.sub(
#         r"\s*[@#]*\s*(?!GmbH|KFT|NV|BV|SE|AG|AB|AS|LLC|INC|LTD)[A-Z]{2,8}\s*$",
#         "", raw
#     ).strip()
#     return name if name else raw


# def _clean_dispatch_line(text):
#     """Extract and clean the Dispatch/Services address line.
#     Matches full label (Dispatch address) and KFT abbreviated form (Dispatch ad.).
#     """
#     m = re.search(
#         r"((?:D[\s_]*i[\s_]*s[\s_]*p[\s_]*a[\s_]*t[\s_]*c[\s_]*h"
#         r"|S[\s_]*e[\s_]*r[\s_]*v[\s_]*i[\s_]*c[\s_]*e[\s_]*s)"
#         r"(?:[\s_]*[Aa][\s_]*d[\s_]*d[\s_]*r[\s_]*e[\s_]*s[\s_]*s"  # full: address
#         r"|[\s_]*[Aa][\s_]*d[\s_]*\.?)"
#         r"[^\n]+)",
#         text, re.IGNORECASE,
#     )
#     if not m:
#         return ""
#     raw = m.group(1)
#     cleaned = re.sub(r"__", " ", raw)
#     cleaned = re.sub(r"_", "", cleaned)
#     cleaned = re.sub(r"(IN[-\s]*\d{6})([A-Za-z])", r"\1 \2", cleaned)
#     cleaned = re.sub(r"\s{2,}", " ", cleaned)
#     return cleaned.strip()


# def _is_garbled(text):
#     """Return True if >30% of tokens are single characters (garbled OCR)."""
#     tokens = text.split()
#     if not tokens:
#         return True
#     single_char = sum(1 for t in tokens if len(t) == 1)
#     return (single_char / len(tokens)) > 0.30


# def _normalize_pincode_city(addr):
#     """Convert 'IN-382170 Ahmedabad' style into 'Ahmedabad - 382170'."""
#     addr = re.sub(
#         r",?\s*IN[-\s]+(\d{6})\s+([A-Za-z][\w\s]+?)(?:\s*,|$)",
#         lambda mo: ", " + mo.group(2).strip() + " - " + mo.group(1),
#         addr,
#     )
#     # Also handle bare pincode-city e.g. "382170 Ahmedabad" at start/end
#     addr = re.sub(
#         r"(?<!\d)(\d{6})\s+([A-Z][a-z]+)",
#         lambda mo: mo.group(2) + " - " + mo.group(1),
#         addr,
#     )
#     return addr.strip().lstrip(",").strip()


# def _extract_bill_to_block(text, name_pattern):
#     """
#     Extract address lines from the bill-to block.

#     Handles:
#     - Right-column metadata noise on same line (strips it off)
#     - Multi-line company name wrap (e.g. 'Bosch Rexroth (India) Private' / 'Limited')
#     - Stop tokens: INDIEN / INDIA / bare IN line, or invoice field labels

#     Returns a list of clean address lines (company name line excluded).
#     """
#     lines = text.splitlines()
#     for i, line in enumerate(lines):
#         if re.search(name_pattern, line, re.IGNORECASE):
#             parts = []
#             for j in range(i + 1, min(i + 12, len(lines))):
#                 raw = lines[j].strip()

#                 # Strip right-column noise (text to the right of the address)
#                 l = _RIGHT_COL_NOISE_RE.sub("", raw).strip()

#                 # Stop at country markers (INDIEN = German for India)
#                 if re.match(r"^(INDIEN|INDIA|IN)\s*$", l, re.IGNORECASE):
#                     break
#                 # Stop at pure right-column metadata lines
#                 if re.search(
#                     r"^(Customer\s+No|Contact\s+addr|Sales\s*:|Accounting\s*:"
#                     r"|Sz[áa]mla|Vev[oő]sz[áa]m|[Áá]rufogad[oó]|Payer|Supplier)",
#                     l, re.IGNORECASE
#                 ):
#                     continue
#                 # Skip name-wrap continuation lines ("Limited", "Private", etc.)
#                 if _NAME_WRAP_RE.match(l):
#                     continue
#                 if l:
#                     parts.append(l)
#             return parts
#     return []


# def _extract_remitter_from_block(text):
#     """
#     Line-by-line block collector for 'Bosch Ltd.' style names.
#     Collects address lines until a country marker.
#     """
#     lines = text.splitlines()
#     for i, line in enumerate(lines):
#         if re.search(r"Bosch\s+L[\s_I|l]*td[\s_I|l]*\.", line, re.IGNORECASE):
#             parts = []
#             for j in range(i + 1, min(i + 10, len(lines))):
#                 l = lines[j].strip()
#                 l = re.sub(r"\s+Ship\s+to.*", "", l, flags=re.IGNORECASE).strip()
#                 if re.match(r"^(India|INDIA|IN\s*:?)$", l):
#                     break
#                 if re.search(
#                     r"^(Customer\s+No|Contact\s+addr|Sales\s*:|Accounting\s*:"
#                     r"|Vev[oő]sz[áa]m|[Áá]rufogad[oó]|Kapcsolattart[oó]"
#                     r"|[Éé]rt[eé]kes[ií]t[eé]s|K[oö]nyvel[eé]s|A\s+mi\s+EU)",
#                     l, re.IGNORECASE
#                 ):
#                     continue
#                 if l:
#                     parts.append(l)
#             return parts
#     return []


# # ── Main extractor ─────────────────────────────────────────────────────────────

# def extract(text, words=None):
#     data = {}

#     # ── Beneficiary (the issuing Bosch entity) ────────────────────────────────

#     # FIX 2: collect multi-line company names e.g. "Bosch Technology Licensing" / "Administration GmbH"
#     _name_lines = []
#     for _l in text.splitlines():
#         _l = _l.strip()
#         if not _l:
#             continue
#         _l = re.sub(r"\s*/\s*\d.*$", "", _l).strip()  # strip "/ 2" page suffixes
#         if not _l:
#             continue
#         if re.search(r"\b(Invoice|Számla|BillingDocument|Billing\s+Document|Page)\b", _l, re.IGNORECASE):
#             break
#         _name_lines.append(_l)
#         if re.search(r"\b(GmbH|KFT|Kft\.|Ltd\.|Limited|S\.A\.S\.|spol\.|Corp\.|Inc\.|LLC|AG|NV|BV|SE|Company)\b", _l, re.IGNORECASE):
#             break
#     raw_name = " ".join(_name_lines) if _name_lines else ""
#     raw_name = re.sub(r"\s*/\s*$", "", raw_name).strip()
#     # FIX A: strip trailing brand logo token ("Robert Bosch GmbH BOSCH" → "Robert Bosch GmbH")
#     data["beneficiary_name"] = _normalize_name(raw_name)

#     # Country: 1) VAT ID prefix (most reliable)
#     m_vat = re.search(r"Our\s+VAT\s+ID\s+No\s*:?\s*([A-Z]{2})\d+", text, re.IGNORECASE)
#     if not m_vat:
#         # Standalone VAT number on its own line e.g. "DE294064848" (no label)
#         m_vat = re.search(r"(?m)^([A-Z]{2})\d{9,}\s*$", text)
#     if m_vat:
#         vat_prefix = m_vat.group(1).upper()
#         data["beneficiary_country"] = _COUNTRY_MAP.get(vat_prefix, vat_prefix)
#     else:
#         # 2) Company address block or first 25 lines
#         header = "\n".join(text.splitlines()[:25])
#         m_ca_block = re.search(
#             r"Company\s+(?:address|ad\.)\s*:?.+", header, re.IGNORECASE | re.DOTALL
#         )
#         search_zone = m_ca_block.group(0) if m_ca_block else header
#         country_found = ""
#         for keyword, country in _COUNTRY_KEYWORDS:
#             if keyword in search_zone:
#                 country_found = country
#                 break
#         data["beneficiary_country"] = country_found or "DE"

#     # Beneficiary address: "Company address/ad." label → "Headquarter" → Siège Social
#     # Pre-clean garbled "C_o_mp_an_y _ad_d_re_ss_" line if present
#     _ca_text = text
#     for _line in text.splitlines():
#         if re.match(r"C_o_mp_an_y", _line.strip(), re.IGNORECASE):
#             _cl = re.sub(r"__", " ", _line)
#             _cl = re.sub(r"_", "", _cl)
#             _cl = re.sub(r"\s{2,}", " ", _cl).strip()
#             _ca_text = text.replace(_line, _cl, 1)
#             break
#     m_ca = re.search(
#         r"Company\s+(?:address|ad\.)\s*:?\s*[^,\n]+,\s*(.+)",
#         _ca_text, re.IGNORECASE
#     )
#     if m_ca:
#         addr = m_ca.group(1).strip().rstrip(",").strip()
#         data["beneficiary_address"] = re.sub(r",\s*$", "", addr)
#     else:
#         # FIX: use word-coordinate reconstruction to reassemble split chars
#         # (e.g. "Gyömrő"+"i" and "ú"+"t" rendered at slightly different y positions)
#         hq_line = reconstruct_line_from_words(words or [], "Headquarter") if words else ""
#         if not hq_line:
#             m_hq = re.search(r"Headquarter\s*:?\s*(.+)", text, re.IGNORECASE)
#             hq_line = m_hq.group(1).strip().rstrip(".") if m_hq else ""
#         if hq_line:
#             # Strip leading label tokens up to and including the colon
#             hq_line = re.sub(
#                 r"^.*?Headquarter\s*:?\s*", "", hq_line, flags=re.IGNORECASE
#             ).strip()
#             data["beneficiary_address"] = hq_line
#         else:
#             m_ss = re.search(
#                 r"Si.ge\s+Social\s*:?\s*(.+?)(?:\s*[-\u2013]\s*(?:France|N°|TVA|N\b))",
#                 text, re.IGNORECASE | re.DOTALL,
#             )
#             if m_ss:
#                 addr = m_ss.group(1).strip().replace("\n", " ")
#                 addr = re.sub(r"([a-z])([A-Z])", r"\1 \2", addr)
#                 addr = re.sub(r"\s{2,}", " ", addr).strip()
#                 data["beneficiary_address"] = addr
#             else:
#                 data["beneficiary_address"] = ""

#     # ── Remitter (India side) ─────────────────────────────────────────────────

#     data["remitter_country"] = "India"

#     # FIX B: detect all India-side Bosch entity variants
#     m_limited    = re.search(r"BOSCH\s+LIM:?ITED", text, re.IGNORECASE)
#     m_rexroth    = re.search(r"Bosch\s+Rexroth\s*\(India\)", text, re.IGNORECASE)
#     # Also match when "Bosch Automotive Electronics" and "India Private Ltd." are on separate lines
#     m_automotive = re.search(
#         r"Bosch\s+Automotive\s+Electronics(?:\s+India\s+Private"  # single line
#         r"|[^\n]*\n(?:[^\n]*\n){0,3}?[^\n]*India\s+Private)",   # across lines (≤3 lines gap)
#         text, re.IGNORECASE
#     )
#     m_ltd        = re.search(r"Bosch\s+L[\s_I|l]*td[\s_I|l]*\.", text, re.IGNORECASE)

#     if m_limited:
#         data["remitter_name"] = "BOSCH LIMITED"
#     elif m_rexroth:
#         data["remitter_name"] = "Bosch Rexroth (India) Private Limited"
#     elif m_automotive:
#         data["remitter_name"] = "Bosch Automotive Electronics India Private Ltd."
#     elif m_ltd:
#         data["remitter_name"] = "Bosch Ltd."
#     else:
#         data["remitter_name"] = ""

#     # ── Remitter address — ordered strategies ─────────────────────────────────
#     matched = False

#     # Strategy 1: Bill-to block for Rexroth (dispatch omits intermediate address lines)
#     # FIX C: prioritise bill-to block for Rexroth so "Iyava Village" etc. are captured
#     if not matched and m_rexroth:
#         block_lines = _extract_bill_to_block(text, r"Bosch\s+Rexroth\s*\(India\)")
#         if block_lines:
#             addr = ", ".join(block_lines)
#             addr = _normalize_pincode_city(addr)
#             data["remitter_address"] = addr
#             matched = True

#     # Strategy 2: Dispatch / Services address line (skip if garbled)
#     if not matched:
#         dispatch = _clean_dispatch_line(text)
#         if dispatch and not _is_garbled(dispatch):
#             m_d = re.search(
#                 r"(?:Dispatch|Services)\s*(?:address|ad\.)\s*:?\s*Bosch[^,]+,\s*(.+)",
#                 dispatch, re.IGNORECASE,
#             )
#             if m_d:
#                 addr = m_d.group(1).strip()
#                 # Strip name-wrap fragments leaked at start
#                 # e.g. "India Private Ltd., ..." or "Limited, ..." or "Private, ..."
#                 addr = re.sub(
#                     r"^(?:India\s+Private\s+Ltd\.|Limited|Private|Pvt\.?),?\s*",
#                     "", addr, flags=re.IGNORECASE
#                 )
#                 addr = _normalize_pincode_city(addr)
#                 data["remitter_address"] = addr
#                 matched = True

#     # Strategy 3: SIPCOT industrial park anchor
#     if not matched:
#         m_a = re.search(
#             r"(SIPCOT[^\n]+)\n(Plot[^\n]+)\n([^\n]+)\n(\d{6})[^\n]*",
#             text, re.IGNORECASE,
#         )
#         if m_a:
#             data["remitter_address"] = (
#                 m_a.group(1).strip() + ", " + m_a.group(2).strip() +
#                 ", " + m_a.group(3).strip() + " - " + m_a.group(4).strip()
#             )
#             matched = True

#     # Strategy 4: POST BOX extraction (KFT/BANGALORE style)
#     if not matched:
#         m_pb = re.search(
#             r"POST\s+BOX\s*:?\s*(\d+)\s+([A-Z][A-Z\s]+),\s*IN[-\s]*(\d{6})\s+([A-Za-z]+)",
#             text, re.IGNORECASE,
#         )
#         if m_pb:
#             data["remitter_address"] = (
#                 f"POST BOX {m_pb.group(1).strip()} {m_pb.group(2).strip()}, "
#                 f"{m_pb.group(4).strip()} - {m_pb.group(3).strip()}"
#             )
#             matched = True

#     # Strategy 5: Bill-to block for "BOSCH LIMITED" (KFT invoice style)
#     if not matched and m_limited:
#         block_lines = _extract_bill_to_block(text, r"BOSCH\s+LIM:?ITED")
#         if block_lines:
#             data["remitter_address"] = ", ".join(block_lines)
#             matched = True

#     # Strategy 6: Line-by-line collector for "Bosch Ltd."
#     if not matched:
#         block_lines = _extract_remitter_from_block(text)
#         if block_lines:
#             data["remitter_address"] = ", ".join(block_lines)
#             matched = True

#     # Strategy 7: Street-before-Ship-to + pincode+CITY+INDIA
#     if not matched:
#         m_b = re.search(
#             r"^([\w\s]+?)\s+Ship\s+to[^\n]*\n(\d{6})\s+([A-Z]{2,})\s*\n(?:INDIA|IN\b)",
#             text, re.MULTILINE | re.IGNORECASE,
#         )
#         data["remitter_address"] = (
#             m_b.group(1).strip() + ", " +
#             m_b.group(3).strip().title() + " - " + m_b.group(2).strip()
#             if m_b else ""
#         )

#     # ── Invoice number ─────────────────────────────────────────────────────────
#     # Covers English "Invoice No." and Hungarian "Számla szám"
#     m_inv = re.search(
#         r"(?:Invoice\s+No\.?|Sz[áa]mla\s+sz[áa]m)\s*:?\s*([A-Z0-9]+)",
#         text, re.IGNORECASE
#     )
#     data["invoice_number"] = m_inv.group(1).strip() if m_inv else ""

#     # ── Invoice date ───────────────────────────────────────────────────────────
#     # FIX D: allow arbitrary OCR noise tokens between label and date
#     # "Date Invoice 1 24.02.2026" — the "1" is a page-number artefact
#     # Use [^\n]{0,20} to skip up to 20 chars of noise but stay on same line
#     m_date = re.search(
#         r"(?:Date\s+Invoice|Sz[áa]mla\s+kelte)[^\n]{0,20}?(\d{2}[./]\d{2}[./]\d{4})",
#         text, re.IGNORECASE
#     )
#     data["invoice_date"] = m_date.group(1).strip() if m_date else ""

#     # ── Amount & currency ──────────────────────────────────────────────────────
#     m_amt = re.search(
#         r"Invoice\s+amount\s*:?\s*(EUR|USD|GBP|CHF|JPY|CZK|HUF)\s*([\d,. ]+)",
#         text, re.IGNORECASE,
#     )
#     if m_amt:
#         data["currency"] = m_amt.group(1).upper()
#         data["amount_foreign"] = m_amt.group(2).strip().replace(" ", "")
#     else:
#         # FIX 7: Value of Services/goods is cleaner than "Amount carried" (less watermark noise)
#         m_vs = re.search(
#             r"Value\s+of\s+(?:Services|goods)\s*:?\s*(EUR|USD|GBP|CHF|JPY|CZK|HUF)\s*([\d,. ]+)",
#             text, re.IGNORECASE,
#         )
#         m_ac = re.search(r"Amount\s+carried\s*:?\s*([\d,. ]+)", text, re.IGNORECASE)
#         if m_vs:
#             data["currency"] = m_vs.group(1).upper()
#             data["amount_foreign"] = m_vs.group(2).strip().replace(" ", "")
#         elif m_ac and m_ac.group(1).strip():
#             data["amount_foreign"] = m_ac.group(1).strip().replace(" ", "")
#             mc = re.search(r"\b(EUR|USD|GBP|CHF|JPY|CZK|HUF)\b", text)
#             data["currency"] = mc.group(1) if mc else ""
#         else:
#             data["amount_foreign"] = ""
#             data["currency"] = ""

#     return data








# # import re
# # from coordinate_utils import reconstruct_line_from_words
# # from text_utils import detect_country

# # _NAME_STRIP = re.compile(
# #     r"\s*[,\s]+"
# #     r"(?:GmbH|SAS|S\.A\.S\.|spol\.\s*s\s*r\.\s*o\.|Ltd\.|\bKFT\b|\bKft\b|"
# #     r"SE|AG|NV|BV|S\.A\.|S\.p\.A\.|Oy|AB|AS|ApS|Inc\.|LLC|"
# #     r"France|Deutschland|Japan|Czech|Polska).*$",
# #     re.IGNORECASE,
# # )

# # _COUNTRY_MAP = {
# #     "IT": "Italy", "ES": "Spain", "BE": "Belgium", "AT": "Austria",
# #     "SE": "Sweden", "PL": "Poland", "CZ": "Czech Republic",
# #     "HU": "Hungary", "RO": "Romania", "PT": "Portugal", "JP": "Japan",
# #     "KR": "Korea",
# #     "DE": "Germany",
# # }

# # # Ordered: more specific / rarer keywords first so they win over "Germany"
# # _COUNTRY_KEYWORDS = [
# #     ("Korea", "Korea"), ("KOREA", "Korea"), ("Sejong", "Korea"), ("Bugang", "Korea"),
# #     ("Japan", "Japan"), ("JAPAN", "Japan"), ("Kanagawa", "Japan"),
# #     ("France", "France"), ("FRANCE", "France"),
# #     ("CZECHIA", "Czech Republic"), ("Czech", "Czech Republic"),
# #     ("Hungary", "Hungary"), ("Budapest", "Hungary"),
# #     ("Germany", "Germany"), ("GERMANY", "Germany"), ("Stuttgart", "Germany"),
# # ]

# # # Legal-suffix / name-wrap tokens that must NOT be treated as address lines
# # _NAME_WRAP_RE = re.compile(
# #     r"^(Limited|Private|Pvt\.?|Inc\.?|LLC|GmbH|KFT|S\.A\.S\.|Corporation)\s*$",
# #     re.IGNORECASE,
# # )

# # # Right-column noise patterns that appear on the same line as address content
# # _RIGHT_COL_NOISE_RE = re.compile(
# #     r"\s+(?:Ship\s+to|Customer\s+No|Contact\s+addr(?:esses)?|Sales\s*:|"
# #     r"Accounting\s*:|Sz[áa]mla|Vev[oő]sz[áa]m|[Áá]rufogad[oó]|"
# #     r"Payer|Invoice\s+No|Date\s+Invoice|Supplier).*",
# #     re.IGNORECASE,
# # )


# # # ── Helpers ────────────────────────────────────────────────────────────────────

# # def _normalize_name(raw):
# #     """Keep full legal name but strip trailing brand logo word (e.g. 'BOSCH').
# #     'Robert Bosch GmbH BOSCH' -> 'Robert Bosch GmbH'
# #     """
# #     # FIX A: strip trailing standalone all-caps brand token (2-8 chars, not a known suffix)
# #     name = re.sub(
# #         r"\s+(?!GmbH|KFT|NV|BV|SE|AG|AB|AS|LLC|INC|LTD)[A-Z]{2,8}\s*$",
# #         "", raw
# #     ).strip()
# #     return name if name else raw


# # def _clean_dispatch_line(text):
# #     """Extract and clean the Dispatch/Services address line.
# #     Matches full label (Dispatch address) and KFT abbreviated form (Dispatch ad.).
# #     """
# #     m = re.search(
# #         r"((?:D[\s_]*i[\s_]*s[\s_]*p[\s_]*a[\s_]*t[\s_]*c[\s_]*h"
# #         r"|S[\s_]*e[\s_]*r[\s_]*v[\s_]*i[\s_]*c[\s_]*e[\s_]*s)"
# #         r"(?:[\s_]*[Aa][\s_]*d[\s_]*d[\s_]*r[\s_]*e[\s_]*s[\s_]*s"  # full: address
# #         r"|[\s_]*[Aa][\s_]*d[\s_]*\.?)"
# #         r"[^\n]+)",
# #         text, re.IGNORECASE,
# #     )
# #     if not m:
# #         return ""
# #     raw = m.group(1)
# #     cleaned = re.sub(r"__", " ", raw)
# #     cleaned = re.sub(r"_", "", cleaned)
# #     cleaned = re.sub(r"(IN[-\s]*\d{6})([A-Za-z])", r"\1 \2", cleaned)
# #     cleaned = re.sub(r"\s{2,}", " ", cleaned)
# #     return cleaned.strip()


# # def _is_garbled(text):
# #     """Return True if >30% of tokens are single characters (garbled OCR)."""
# #     tokens = text.split()
# #     if not tokens:
# #         return True
# #     single_char = sum(1 for t in tokens if len(t) == 1)
# #     return (single_char / len(tokens)) > 0.30


# # def _normalize_pincode_city(addr):
# #     """Convert 'IN-382170 Ahmedabad' style into 'Ahmedabad - 382170'."""
# #     addr = re.sub(
# #         r",?\s*IN[-\s]+(\d{6})\s+([A-Za-z][\w\s]+?)(?:\s*,|$)",
# #         lambda mo: ", " + mo.group(2).strip() + " - " + mo.group(1),
# #         addr,
# #     )
# #     # Also handle bare pincode-city e.g. "382170 Ahmedabad" at start/end
# #     addr = re.sub(
# #         r"(?<!\d)(\d{6})\s+([A-Z][a-z]+)",
# #         lambda mo: mo.group(2) + " - " + mo.group(1),
# #         addr,
# #     )
# #     return addr.strip().lstrip(",").strip()


# # def _extract_bill_to_block(text, name_pattern):
# #     """
# #     Extract address lines from the bill-to block.

# #     Handles:
# #     - Right-column metadata noise on same line (strips it off)
# #     - Multi-line company name wrap (e.g. 'Bosch Rexroth (India) Private' / 'Limited')
# #     - Stop tokens: INDIEN / INDIA / bare IN line, or invoice field labels

# #     Returns a list of clean address lines (company name line excluded).
# #     """
# #     lines = text.splitlines()
# #     for i, line in enumerate(lines):
# #         if re.search(name_pattern, line, re.IGNORECASE):
# #             parts = []
# #             for j in range(i + 1, min(i + 12, len(lines))):
# #                 raw = lines[j].strip()

# #                 # Strip right-column noise (text to the right of the address)
# #                 l = _RIGHT_COL_NOISE_RE.sub("", raw).strip()

# #                 # Stop at country markers (INDIEN = German for India)
# #                 if re.match(r"^(INDIEN|INDIA|IN)\s*$", l, re.IGNORECASE):
# #                     break
# #                 # Stop at pure right-column metadata lines
# #                 if re.search(
# #                     r"^(Customer\s+No|Contact\s+addr|Sales\s*:|Accounting\s*:"
# #                     r"|Sz[áa]mla|Vev[oő]sz[áa]m|[Áá]rufogad[oó]|Payer|Supplier)",
# #                     l, re.IGNORECASE
# #                 ):
# #                     continue
# #                 # Skip name-wrap continuation lines ("Limited", "Private", etc.)
# #                 if _NAME_WRAP_RE.match(l):
# #                     continue
# #                 if l:
# #                     parts.append(l)
# #             return parts
# #     return []


# # def _extract_remitter_from_block(text):
# #     """
# #     Line-by-line block collector for 'Bosch Ltd.' style names.
# #     Collects address lines until a country marker.
# #     """
# #     lines = text.splitlines()
# #     for i, line in enumerate(lines):
# #         if re.search(r"Bosch\s+L[\s_I|l]*td[\s_I|l]*\.", line, re.IGNORECASE):
# #             parts = []
# #             for j in range(i + 1, min(i + 10, len(lines))):
# #                 l = lines[j].strip()
# #                 l = re.sub(r"\s+Ship\s+to.*", "", l, flags=re.IGNORECASE).strip()
# #                 if re.match(r"^(India|INDIA|IN\s*:?)$", l):
# #                     break
# #                 if re.search(
# #                     r"^(Customer\s+No|Contact\s+addr|Sales\s*:|Accounting\s*:"
# #                     r"|Vev[oő]sz[áa]m|[Áá]rufogad[oó]|Kapcsolattart[oó]"
# #                     r"|[Éé]rt[eé]kes[ií]t[eé]s|K[oö]nyvel[eé]s|A\s+mi\s+EU)",
# #                     l, re.IGNORECASE
# #                 ):
# #                     continue
# #                 if l:
# #                     parts.append(l)
# #             return parts
# #     return []


# # # ── Main extractor ─────────────────────────────────────────────────────────────

# # def extract(text, words=None):
# #     data = {}

# #     # ── Beneficiary (the issuing Bosch entity) ────────────────────────────────

# #     raw_name = next((l.strip() for l in text.splitlines() if l.strip()), "")
# #     raw_name = re.sub(r"\s*/\s*$", "", raw_name).strip()
# #     # FIX A: strip trailing brand logo token ("Robert Bosch GmbH BOSCH" → "Robert Bosch GmbH")
# #     data["beneficiary_name"] = _normalize_name(raw_name)

# #     # Country: 1) VAT ID prefix (most reliable)
# #     m_vat = re.search(r"Our\s+VAT\s+ID\s+No\s*:?\s*([A-Z]{2})\d+", text, re.IGNORECASE)
# #     if m_vat:
# #         vat_prefix = m_vat.group(1).upper()
# #         data["beneficiary_country"] = _COUNTRY_MAP.get(vat_prefix, vat_prefix)
# #     else:
# #         # 2) Company address block or first 25 lines
# #         header = "\n".join(text.splitlines()[:25])
# #         m_ca_block = re.search(
# #             r"Company\s+(?:address|ad\.)\s*:?.+", header, re.IGNORECASE | re.DOTALL
# #         )
# #         search_zone = m_ca_block.group(0) if m_ca_block else header
# #         country_found = ""
# #         for keyword, country in _COUNTRY_KEYWORDS:
# #             if keyword in search_zone:
# #                 country_found = country
# #                 break
# #         data["beneficiary_country"] = country_found or "DE"

# #     # Beneficiary address: "Company address/ad." label → "Headquarter" → Siège Social
# #     m_ca = re.search(
# #         r"Company\s+(?:address|ad\.)\s*:?\s*[^,\n]+,\s*(.+)",
# #         text, re.IGNORECASE
# #     )
# #     if m_ca:
# #         addr = m_ca.group(1).strip().rstrip(",").strip()
# #         data["beneficiary_address"] = re.sub(r",\s*$", "", addr)
# #     else:
# #         # FIX: use word-coordinate reconstruction to reassemble split chars
# #         # (e.g. "Gyömrő"+"i" and "ú"+"t" rendered at slightly different y positions)
# #         hq_line = reconstruct_line_from_words(words or [], "Headquarter") if words else ""
# #         if not hq_line:
# #             m_hq = re.search(r"Headquarter\s*:?\s*(.+)", text, re.IGNORECASE)
# #             hq_line = m_hq.group(1).strip().rstrip(".") if m_hq else ""
# #         if hq_line:
# #             # Strip leading label tokens up to and including the colon
# #             hq_line = re.sub(
# #                 r"^.*?Headquarter\s*:?\s*", "", hq_line, flags=re.IGNORECASE
# #             ).strip()
# #             data["beneficiary_address"] = hq_line
# #         else:
# #             m_ss = re.search(
# #                 r"Si.ge\s+Social\s*:?\s*(.+?)(?:\s*[-\u2013]\s*(?:France|N°|TVA|N\b))",
# #                 text, re.IGNORECASE | re.DOTALL,
# #             )
# #             if m_ss:
# #                 addr = m_ss.group(1).strip().replace("\n", " ")
# #                 addr = re.sub(r"([a-z])([A-Z])", r"\1 \2", addr)
# #                 addr = re.sub(r"\s{2,}", " ", addr).strip()
# #                 data["beneficiary_address"] = addr
# #             else:
# #                 data["beneficiary_address"] = ""

# #     # ── Remitter (India side) ─────────────────────────────────────────────────
# #     # data["remitter_country"] = "India"  # Replaced by dynamic detection

# #     # FIX B: detect all India-side Bosch entity variants
# #     m_limited  = re.search(r"BOSCH\s+LIM:?ITED", text, re.IGNORECASE)
# #     m_rexroth  = re.search(r"Bosch\s+Rexroth\s*\(India\)", text, re.IGNORECASE)
# #     m_ltd      = re.search(r"Bosch\s+L[\s_I|l]*td[\s_I|l]*\.", text, re.IGNORECASE)

# #     if m_limited:
# #         data["remitter_name"] = "BOSCH LIMITED"
# #     elif m_rexroth:
# #         data["remitter_name"] = "Bosch Rexroth (India) Private Limited"
# #     elif m_ltd:
# #         data["remitter_name"] = "Bosch Ltd."
# #     else:
# #         data["remitter_name"] = ""

# #     # ── Remitter address — ordered strategies ─────────────────────────────────
# #     matched = False

# #     # Strategy 1: Bill-to block for Rexroth (dispatch omits intermediate address lines)
# #     # FIX C: prioritise bill-to block for Rexroth so "Iyava Village" etc. are captured
# #     if not matched and m_rexroth:
# #         block_lines = _extract_bill_to_block(text, r"Bosch\s+Rexroth\s*\(India\)")
# #         if block_lines:
# #             addr = ", ".join(block_lines)
# #             addr = _normalize_pincode_city(addr)
# #             data["remitter_address"] = addr
# #             matched = True

# #     # Strategy 2: Dispatch / Services address line (skip if garbled)
# #     if not matched:
# #         dispatch = _clean_dispatch_line(text)
# #         if dispatch and not _is_garbled(dispatch):
# #             m_d = re.search(
# #                 r"(?:Dispatch|Services)\s*(?:address|ad\.)\s*:?\s*Bosch[^,]+,\s*(.+)",
# #                 dispatch, re.IGNORECASE,
# #             )
# #             if m_d:
# #                 addr = m_d.group(1).strip()
# #                 # Strip name-wrap fragment leaked as first token ("Limited, ...")
# #                 addr = re.sub(r"^(Limited|Private|Pvt\.?),?\s*", "", addr, flags=re.IGNORECASE)
# #                 addr = _normalize_pincode_city(addr)
# #                 data["remitter_address"] = addr
# #                 matched = True

# #     # Strategy 3: SIPCOT industrial park anchor
# #     if not matched:
# #         m_a = re.search(
# #             r"(SIPCOT[^\n]+)\n(Plot[^\n]+)\n([^\n]+)\n(\d{6})[^\n]*",
# #             text, re.IGNORECASE,
# #         )
# #         if m_a:
# #             data["remitter_address"] = (
# #                 m_a.group(1).strip() + ", " + m_a.group(2).strip() +
# #                 ", " + m_a.group(3).strip() + " - " + m_a.group(4).strip()
# #             )
# #             matched = True

# #     # Strategy 4: POST BOX extraction (KFT/BANGALORE style)
# #     if not matched:
# #         m_pb = re.search(
# #             r"POST\s+BOX\s*:?\s*(\d+)\s+([A-Z][A-Z\s]+),\s*IN[-\s]*(\d{6})\s+([A-Za-z]+)",
# #             text, re.IGNORECASE,
# #         )
# #         if m_pb:
# #             data["remitter_address"] = (
# #                 f"POST BOX {m_pb.group(1).strip()} {m_pb.group(2).strip()}, "
# #                 f"{m_pb.group(4).strip()} - {m_pb.group(3).strip()}"
# #             )
# #             matched = True

# #     # Strategy 5: Bill-to block for "BOSCH LIMITED" (KFT invoice style)
# #     if not matched and m_limited:
# #         block_lines = _extract_bill_to_block(text, r"BOSCH\s+LIM:?ITED")
# #         if block_lines:
# #             data["remitter_address"] = ", ".join(block_lines)
# #             matched = True

# #     # Strategy 6: Line-by-line collector for "Bosch Ltd."
# #     if not matched:
# #         block_lines = _extract_remitter_from_block(text)
# #         if block_lines:
# #             data["remitter_address"] = ", ".join(block_lines)
# #             matched = True

# #     # Strategy 7: Street-before-Ship-to + pincode+CITY+INDIA
# #     if not matched:
# #         m_b = re.search(
# #             r"^([\w\s]+?)\s+Ship\s+to[^\n]*\n(\d{6})\s+([A-Z]{2,})\s*\n(?:INDIA|IN\b)",
# #             text, re.MULTILINE | re.IGNORECASE,
# #         )
# #         data["remitter_address"] = (
# #             m_b.group(1).strip() + ", " +
# #             m_b.group(3).strip().title() + " - " + m_b.group(2).strip()
# #             if m_b else ""
# #         )

# #     # ── Invoice number ─────────────────────────────────────────────────────────
# #     # Covers English "Invoice No." and Hungarian "Számla szám"
# #     m_inv = re.search(
# #         r"(?:Invoice\s+No\.?|Sz[áa]mla\s+sz[áa]m)\s*:?\s*([A-Z0-9]+)",
# #         text, re.IGNORECASE
# #     )
# #     data["invoice_number"] = m_inv.group(1).strip() if m_inv else ""

# #     # ── Invoice date ───────────────────────────────────────────────────────────
# #     # FIX D: allow arbitrary OCR noise tokens between label and date
# #     # "Date Invoice 1 24.02.2026" — the "1" is a page-number artefact
# #     # Use [^\n]{0,20} to skip up to 20 chars of noise but stay on same line
# #     m_date = re.search(
# #         r"(?:Date\s+Invoice|Sz[áa]mla\s+kelte)[^\n]{0,20}?(\d{2}[./]\d{2}[./]\d{4})",
# #         text, re.IGNORECASE
# #     )
# #     data["invoice_date"] = m_date.group(1).strip() if m_date else ""

# #     # ── Amount & currency ──────────────────────────────────────────────────────
# #     m_amt = re.search(
# #         r"Invoice\s+amount\s*:?\s*(EUR|USD|GBP|CHF|JPY|CZK|HUF)\s*([\d,. ]+)",
# #         text, re.IGNORECASE,
# #     )
# #     if m_amt:
# #         data["currency"] = m_amt.group(1).upper()
# #         data["amount_foreign"] = m_amt.group(2).strip().replace(" ", "")
# #     else:
# #         m_ac = re.search(r"Amount\s+carried\s*:?\s*([\d,. ]+)", text, re.IGNORECASE)
# #         if m_ac and m_ac.group(1).strip():
# #             data["amount_foreign"] = m_ac.group(1).strip().replace(" ", "")
# #             mc = re.search(r"\b(EUR|USD|GBP|CHF|JPY|CZK|HUF)\b", text)
# #             data["currency"] = mc.group(1) if mc else ""
# #         else:
# #             m_vs = re.search(
# #                 r"Value\s+of\s+(?:Services|goods)\s*:?\s*(EUR|USD|GBP|CHF|JPY|CZK|HUF)\s*([\d,. ]+)",
# #                 text, re.IGNORECASE,
# #             )
# #             if m_vs:
# #                 data["currency"] = m_vs.group(1).upper()
# #                 data["amount_foreign"] = m_vs.group(2).strip().replace(" ", "")
# #             else:
# #                 data["amount_foreign"] = ""
# #                 data["currency"] = ""

# #     data["remitter_country"] = detect_country(data.get("remitter_address", "") + "\n" + text, default="")
# #     return data






# # import re

# # _NAME_STRIP = re.compile(
# #     r"\s*[,\s]+"
# #     r"(?:GmbH|SAS|S\.A\.S\.|spol\.\s*s\s*r\.\s*o\.|Ltd\.|\bKFT\b|\bKft\b|"
# #     r"SE|AG|NV|BV|S\.A\.|S\.p\.A\.|Oy|AB|AS|ApS|Inc\.|LLC|"
# #     r"France|Deutschland|Japan|Czech|Polska).*$",
# #     re.IGNORECASE,
# # )

# # _COUNTRY_MAP = {
# #     "IT": "Italy", "ES": "Spain", "BE": "Belgium", "AT": "Austria",
# #     "SE": "Sweden", "PL": "Poland", "CZ": "Czech Republic",
# #     "HU": "Hungary", "RO": "Romania", "PT": "Portugal", "JP": "Japan",
# #     "KR": "Korea",
# #     "DE": "Germany",
# # }

# # # Ordered: more specific / rarer keywords first so they win over "Germany"
# # _COUNTRY_KEYWORDS = [
# #     ("Korea", "Korea"), ("KOREA", "Korea"), ("Sejong", "Korea"), ("Bugang", "Korea"),
# #     ("Japan", "Japan"), ("JAPAN", "Japan"), ("Kanagawa", "Japan"),
# #     ("France", "France"), ("FRANCE", "France"),
# #     ("CZECHIA", "Czech Republic"), ("Czech", "Czech Republic"),
# #     ("Hungary", "Hungary"), ("Budapest", "Hungary"),
# #     ("Germany", "Germany"), ("GERMANY", "Germany"), ("Stuttgart", "Germany"),
# # ]

# # # Legal-suffix / name-wrap tokens that must NOT be treated as address lines
# # _NAME_WRAP_RE = re.compile(
# #     r"^(Limited|Private|Pvt\.?|Inc\.?|LLC|GmbH|KFT|S\.A\.S\.|Corporation)\s*$",
# #     re.IGNORECASE,
# # )

# # # Right-column noise patterns that appear on the same line as address content
# # _RIGHT_COL_NOISE_RE = re.compile(
# #     r"\s+(?:Ship\s+to|Customer\s+No|Contact\s+addr(?:esses)?|Sales\s*:|"
# #     r"Accounting\s*:|Sz[áa]mla|Vev[oő]sz[áa]m|[Áá]rufogad[oó]|"
# #     r"Payer|Invoice\s+No|Date\s+Invoice|Supplier).*",
# #     re.IGNORECASE,
# # )


# # # ── Helpers ────────────────────────────────────────────────────────────────────

# # def _normalize_name(raw):
# #     """Keep full legal name but strip trailing brand logo word (e.g. 'BOSCH').
# #     'Robert Bosch GmbH BOSCH' -> 'Robert Bosch GmbH'
# #     """
# #     # FIX A: strip trailing standalone all-caps brand token (2-8 chars, not a known suffix)
# #     name = re.sub(
# #         r"\s+(?!GmbH|KFT|NV|BV|SE|AG|AB|AS|LLC|INC|LTD)[A-Z]{2,8}\s*$",
# #         "", raw
# #     ).strip()
# #     return name if name else raw


# # def _clean_dispatch_line(text):
# #     """Extract and clean the Dispatch/Services address line.
# #     Matches full label (Dispatch address) and KFT abbreviated form (Dispatch ad.).
# #     """
# #     m = re.search(
# #         r"((?:D[\s_]*i[\s_]*s[\s_]*p[\s_]*a[\s_]*t[\s_]*c[\s_]*h"
# #         r"|S[\s_]*e[\s_]*r[\s_]*v[\s_]*i[\s_]*c[\s_]*e[\s_]*s)"
# #         r"(?:[\s_]*[Aa][\s_]*d[\s_]*d[\s_]*r[\s_]*e[\s_]*s[\s_]*s"  # full: address
# #         r"|[\s_]*[Aa][\s_]*d[\s_]*\.?)"
# #         r"[^\n]+)",
# #         text, re.IGNORECASE,
# #     )
# #     if not m:
# #         return ""
# #     raw = m.group(1)
# #     cleaned = re.sub(r"__", " ", raw)
# #     cleaned = re.sub(r"_", "", cleaned)
# #     cleaned = re.sub(r"(IN[-\s]*\d{6})([A-Za-z])", r"\1 \2", cleaned)
# #     cleaned = re.sub(r"\s{2,}", " ", cleaned)
# #     return cleaned.strip()


# # def _is_garbled(text):
# #     """Return True if >30% of tokens are single characters (garbled OCR)."""
# #     tokens = text.split()
# #     if not tokens:
# #         return True
# #     single_char = sum(1 for t in tokens if len(t) == 1)
# #     return (single_char / len(tokens)) > 0.30


# # def _normalize_pincode_city(addr):
# #     """Convert 'IN-382170 Ahmedabad' style into 'Ahmedabad - 382170'."""
# #     addr = re.sub(
# #         r",?\s*IN[-\s]+(\d{6})\s+([A-Za-z][\w\s]+?)(?:\s*,|$)",
# #         lambda mo: ", " + mo.group(2).strip() + " - " + mo.group(1),
# #         addr,
# #     )
# #     # Also handle bare pincode-city e.g. "382170 Ahmedabad" at start/end
# #     addr = re.sub(
# #         r"(?<!\d)(\d{6})\s+([A-Z][a-z]+)",
# #         lambda mo: mo.group(2) + " - " + mo.group(1),
# #         addr,
# #     )
# #     return addr.strip().lstrip(",").strip()


# # def _extract_bill_to_block(text, name_pattern):
# #     """
# #     Extract address lines from the bill-to block.

# #     Handles:
# #     - Right-column metadata noise on same line (strips it off)
# #     - Multi-line company name wrap (e.g. 'Bosch Rexroth (India) Private' / 'Limited')
# #     - Stop tokens: INDIEN / INDIA / bare IN line, or invoice field labels

# #     Returns a list of clean address lines (company name line excluded).
# #     """
# #     lines = text.splitlines()
# #     for i, line in enumerate(lines):
# #         if re.search(name_pattern, line, re.IGNORECASE):
# #             parts = []
# #             for j in range(i + 1, min(i + 12, len(lines))):
# #                 raw = lines[j].strip()

# #                 # Strip right-column noise (text to the right of the address)
# #                 l = _RIGHT_COL_NOISE_RE.sub("", raw).strip()

# #                 # Stop at country markers (INDIEN = German for India)
# #                 if re.match(r"^(INDIEN|INDIA|IN)\s*$", l, re.IGNORECASE):
# #                     break
# #                 # Stop at pure right-column metadata lines
# #                 if re.search(
# #                     r"^(Customer\s+No|Contact\s+addr|Sales\s*:|Accounting\s*:"
# #                     r"|Sz[áa]mla|Vev[oő]sz[áa]m|[Áá]rufogad[oó]|Payer|Supplier)",
# #                     l, re.IGNORECASE
# #                 ):
# #                     continue
# #                 # Skip name-wrap continuation lines ("Limited", "Private", etc.)
# #                 if _NAME_WRAP_RE.match(l):
# #                     continue
# #                 if l:
# #                     parts.append(l)
# #             return parts
# #     return []


# # def _extract_remitter_from_block(text):
# #     """
# #     Line-by-line block collector for 'Bosch Ltd.' style names.
# #     Collects address lines until a country marker.
# #     """
# #     lines = text.splitlines()
# #     for i, line in enumerate(lines):
# #         if re.search(r"Bosch\s+L[\s_I|l]*td[\s_I|l]*\.", line, re.IGNORECASE):
# #             parts = []
# #             for j in range(i + 1, min(i + 10, len(lines))):
# #                 l = lines[j].strip()
# #                 l = re.sub(r"\s+Ship\s+to.*", "", l, flags=re.IGNORECASE).strip()
# #                 if re.match(r"^(India|INDIA|IN\s*:?)$", l):
# #                     break
# #                 if re.search(
# #                     r"^(Customer\s+No|Contact\s+addr|Sales\s*:|Accounting\s*:"
# #                     r"|Vev[oő]sz[áa]m|[Áá]rufogad[oó]|Kapcsolattart[oó]"
# #                     r"|[Éé]rt[eé]kes[ií]t[eé]s|K[oö]nyvel[eé]s|A\s+mi\s+EU)",
# #                     l, re.IGNORECASE
# #                 ):
# #                     continue
# #                 if l:
# #                     parts.append(l)
# #             return parts
# #     return []


# # # ── Main extractor ─────────────────────────────────────────────────────────────

# # def extract(text, words=None):
# #     data = {}

# #     # ── Beneficiary (the issuing Bosch entity) ────────────────────────────────

# #     raw_name = next((l.strip() for l in text.splitlines() if l.strip()), "")
# #     raw_name = re.sub(r"\s*/\s*$", "", raw_name).strip()
# #     # FIX A: strip trailing brand logo token ("Robert Bosch GmbH BOSCH" → "Robert Bosch GmbH")
# #     data["beneficiary_name"] = _normalize_name(raw_name)

# #     # Country: 1) VAT ID prefix (most reliable)
# #     m_vat = re.search(r"Our\s+VAT\s+ID\s+No\s*:?\s*([A-Z]{2})\d+", text, re.IGNORECASE)
# #     if m_vat:
# #         vat_prefix = m_vat.group(1).upper()
# #         data["beneficiary_country"] = _COUNTRY_MAP.get(vat_prefix, vat_prefix)
# #     else:
# #         # 2) Company address block or first 25 lines
# #         header = "\n".join(text.splitlines()[:25])
# #         m_ca_block = re.search(
# #             r"Company\s+(?:address|ad\.)\s*:?.+", header, re.IGNORECASE | re.DOTALL
# #         )
# #         search_zone = m_ca_block.group(0) if m_ca_block else header
# #         country_found = ""
# #         for keyword, country in _COUNTRY_KEYWORDS:
# #             if keyword in search_zone:
# #                 country_found = country
# #                 break
# #         data["beneficiary_country"] = country_found or "DE"

# #     # Beneficiary address: "Company address/ad." label → "Headquarter" → Siège Social
# #     m_ca = re.search(
# #         r"Company\s+(?:address|ad\.)\s*:?\s*[^,\n]+,\s*(.+)",
# #         text, re.IGNORECASE
# #     )
# #     if m_ca:
# #         addr = m_ca.group(1).strip().rstrip(",").strip()
# #         data["beneficiary_address"] = re.sub(r",\s*$", "", addr)
# #     else:
# #         m_hq = re.search(r"Headquarter\s*:?\s*(.+)", text, re.IGNORECASE)
# #         if m_hq:
# #             data["beneficiary_address"] = m_hq.group(1).strip().rstrip(".")
# #         else:
# #             m_ss = re.search(
# #                 r"Si.ge\s+Social\s*:?\s*(.+?)(?:\s*[-\u2013]\s*(?:France|N°|TVA|N\b))",
# #                 text, re.IGNORECASE | re.DOTALL,
# #             )
# #             if m_ss:
# #                 addr = m_ss.group(1).strip().replace("\n", " ")
# #                 addr = re.sub(r"([a-z])([A-Z])", r"\1 \2", addr)
# #                 addr = re.sub(r"\s{2,}", " ", addr).strip()
# #                 data["beneficiary_address"] = addr
# #             else:
# #                 data["beneficiary_address"] = ""

# #     # ── Remitter (India side) ─────────────────────────────────────────────────

# #     data["remitter_country"] = "India"

# #     # FIX B: detect all India-side Bosch entity variants
# #     m_limited  = re.search(r"BOSCH\s+LIM:?ITED", text, re.IGNORECASE)
# #     m_rexroth  = re.search(r"Bosch\s+Rexroth\s*\(India\)", text, re.IGNORECASE)
# #     m_ltd      = re.search(r"Bosch\s+L[\s_I|l]*td[\s_I|l]*\.", text, re.IGNORECASE)

# #     if m_limited:
# #         data["remitter_name"] = "BOSCH LIMITED"
# #     elif m_rexroth:
# #         data["remitter_name"] = "Bosch Rexroth (India) Private Limited"
# #     elif m_ltd:
# #         data["remitter_name"] = "Bosch Ltd."
# #     else:
# #         data["remitter_name"] = ""

# #     # ── Remitter address — ordered strategies ─────────────────────────────────
# #     matched = False

# #     # Strategy 1: Bill-to block for Rexroth (dispatch omits intermediate address lines)
# #     # FIX C: prioritise bill-to block for Rexroth so "Iyava Village" etc. are captured
# #     if not matched and m_rexroth:
# #         block_lines = _extract_bill_to_block(text, r"Bosch\s+Rexroth\s*\(India\)")
# #         if block_lines:
# #             addr = ", ".join(block_lines)
# #             addr = _normalize_pincode_city(addr)
# #             data["remitter_address"] = addr
# #             matched = True

# #     # Strategy 2: Dispatch / Services address line (skip if garbled)
# #     if not matched:
# #         dispatch = _clean_dispatch_line(text)
# #         if dispatch and not _is_garbled(dispatch):
# #             m_d = re.search(
# #                 r"(?:Dispatch|Services)\s*(?:address|ad\.)\s*:?\s*Bosch[^,]+,\s*(.+)",
# #                 dispatch, re.IGNORECASE,
# #             )
# #             if m_d:
# #                 addr = m_d.group(1).strip()
# #                 # Strip name-wrap fragment leaked as first token ("Limited, ...")
# #                 addr = re.sub(r"^(Limited|Private|Pvt\.?),?\s*", "", addr, flags=re.IGNORECASE)
# #                 addr = _normalize_pincode_city(addr)
# #                 data["remitter_address"] = addr
# #                 matched = True

# #     # Strategy 3: SIPCOT industrial park anchor
# #     if not matched:
# #         m_a = re.search(
# #             r"(SIPCOT[^\n]+)\n(Plot[^\n]+)\n([^\n]+)\n(\d{6})[^\n]*",
# #             text, re.IGNORECASE,
# #         )
# #         if m_a:
# #             data["remitter_address"] = (
# #                 m_a.group(1).strip() + ", " + m_a.group(2).strip() +
# #                 ", " + m_a.group(3).strip() + " - " + m_a.group(4).strip()
# #             )
# #             matched = True

# #     # Strategy 4: POST BOX extraction (KFT/BANGALORE style)
# #     if not matched:
# #         m_pb = re.search(
# #             r"POST\s+BOX\s*:?\s*(\d+)\s+([A-Z][A-Z\s]+),\s*IN[-\s]*(\d{6})\s+([A-Za-z]+)",
# #             text, re.IGNORECASE,
# #         )
# #         if m_pb:
# #             data["remitter_address"] = (
# #                 f"POST BOX {m_pb.group(1).strip()} {m_pb.group(2).strip()}, "
# #                 f"{m_pb.group(4).strip()} - {m_pb.group(3).strip()}"
# #             )
# #             matched = True

# #     # Strategy 5: Bill-to block for "BOSCH LIMITED" (KFT invoice style)
# #     if not matched and m_limited:
# #         block_lines = _extract_bill_to_block(text, r"BOSCH\s+LIM:?ITED")
# #         if block_lines:
# #             data["remitter_address"] = ", ".join(block_lines)
# #             matched = True

# #     # Strategy 6: Line-by-line collector for "Bosch Ltd."
# #     if not matched:
# #         block_lines = _extract_remitter_from_block(text)
# #         if block_lines:
# #             data["remitter_address"] = ", ".join(block_lines)
# #             matched = True

# #     # Strategy 7: Street-before-Ship-to + pincode+CITY+INDIA
# #     if not matched:
# #         m_b = re.search(
# #             r"^([\w\s]+?)\s+Ship\s+to[^\n]*\n(\d{6})\s+([A-Z]{2,})\s*\n(?:INDIA|IN\b)",
# #             text, re.MULTILINE | re.IGNORECASE,
# #         )
# #         data["remitter_address"] = (
# #             m_b.group(1).strip() + ", " +
# #             m_b.group(3).strip().title() + " - " + m_b.group(2).strip()
# #             if m_b else ""
# #         )

# #     # ── Invoice number ─────────────────────────────────────────────────────────
# #     # Covers English "Invoice No." and Hungarian "Számla szám"
# #     m_inv = re.search(
# #         r"(?:Invoice\s+No\.?|Sz[áa]mla\s+sz[áa]m)\s*:?\s*([A-Z0-9]+)",
# #         text, re.IGNORECASE
# #     )
# #     data["invoice_number"] = m_inv.group(1).strip() if m_inv else ""

# #     # ── Invoice date ───────────────────────────────────────────────────────────
# #     # FIX D: allow arbitrary OCR noise tokens between label and date
# #     # "Date Invoice 1 24.02.2026" — the "1" is a page-number artefact
# #     # Use [^\n]{0,20} to skip up to 20 chars of noise but stay on same line
# #     m_date = re.search(
# #         r"(?:Date\s+Invoice|Sz[áa]mla\s+kelte)[^\n]{0,20}?(\d{2}[./]\d{2}[./]\d{4})",
# #         text, re.IGNORECASE
# #     )
# #     data["invoice_date"] = m_date.group(1).strip() if m_date else ""

# #     # ── Amount & currency ──────────────────────────────────────────────────────
# #     m_amt = re.search(
# #         r"Invoice\s+amount\s*:?\s*(EUR|USD|GBP|CHF|JPY|CZK|HUF)\s*([\d,. ]+)",
# #         text, re.IGNORECASE,
# #     )
# #     if m_amt:
# #         data["currency"] = m_amt.group(1).upper()
# #         data["amount_foreign"] = m_amt.group(2).strip().replace(" ", "")
# #     else:
# #         m_ac = re.search(r"Amount\s+carried\s*:?\s*([\d,. ]+)", text, re.IGNORECASE)
# #         if m_ac and m_ac.group(1).strip():
# #             data["amount_foreign"] = m_ac.group(1).strip().replace(" ", "")
# #             mc = re.search(r"\b(EUR|USD|GBP|CHF|JPY|CZK|HUF)\b", text)
# #             data["currency"] = mc.group(1) if mc else ""
# #         else:
# #             m_vs = re.search(
# #                 r"Value\s+of\s+(?:Services|goods)\s*:?\s*(EUR|USD|GBP|CHF|JPY|CZK|HUF)\s*([\d,. ]+)",
# #                 text, re.IGNORECASE,
# #             )
# #             if m_vs:
# #                 data["currency"] = m_vs.group(1).upper()
# #                 data["amount_foreign"] = m_vs.group(2).strip().replace(" ", "")
# #             else:
# #                 data["amount_foreign"] = ""
# #                 data["currency"] = ""

# #     return data






# # # import re

# # # _NAME_STRIP = re.compile(
# # #     r"\s*[,\s]+"
# # #     r"(?:GmbH|SAS|S\.A\.S\.|spol\.\s*s\s*r\.\s*o\.|Ltd\.|\bKFT\b|\bKft\b|"
# # #     r"SE|AG|NV|BV|S\.A\.|S\.p\.A\.|Oy|AB|AS|ApS|Inc\.|LLC|"
# # #     r"France|Deutschland|Japan|Czech|Polska).*$",
# # #     re.IGNORECASE,
# # # )

# # # _COUNTRY_MAP = {
# # #     "IT": "Italy", "ES": "Spain", "BE": "Belgium", "AT": "Austria",
# # #     "SE": "Sweden", "PL": "Poland", "CZ": "Czech Republic",
# # #     "HU": "Hungary", "RO": "Romania", "PT": "Portugal", "JP": "Japan",
# # #     "KR": "Korea",
# # #     "DE": "Germany",
# # # }

# # # # Ordered: more specific / rarer keywords first so they win over "Germany"
# # # _COUNTRY_KEYWORDS = [
# # #     ("Korea", "Korea"), ("KOREA", "Korea"), ("Sejong", "Korea"), ("Bugang", "Korea"),
# # #     ("Japan", "Japan"), ("JAPAN", "Japan"), ("Kanagawa", "Japan"),
# # #     ("France", "France"), ("FRANCE", "France"),
# # #     ("CZECHIA", "Czech Republic"), ("Czech", "Czech Republic"),
# # #     ("Hungary", "Hungary"), ("Budapest", "Hungary"),
# # #     ("Germany", "Germany"), ("GERMANY", "Germany"), ("Stuttgart", "Germany"),
# # # ]

# # # # Legal-suffix / name-wrap tokens that must NOT be treated as address lines
# # # _NAME_WRAP_RE = re.compile(
# # #     r"^(Limited|Private|Pvt\.?|Inc\.?|LLC|GmbH|KFT|S\.A\.S\.|Corporation)\s*$",
# # #     re.IGNORECASE,
# # # )

# # # # Right-column noise patterns that appear on the same line as address content
# # # _RIGHT_COL_NOISE_RE = re.compile(
# # #     r"\s+(?:Ship\s+to|Customer\s+No|Contact\s+addr(?:esses)?|Sales\s*:|"
# # #     r"Accounting\s*:|Sz[áa]mla|Vev[oő]sz[áa]m|[Áá]rufogad[oó]|"
# # #     r"Payer|Invoice\s+No|Date\s+Invoice|Supplier).*",
# # #     re.IGNORECASE,
# # # )


# # # # ── Helpers ────────────────────────────────────────────────────────────────────

# # # def _normalize_name(raw):
# # #     """Keep full legal name but strip trailing brand logo word (e.g. 'BOSCH').
# # #     'Robert Bosch GmbH BOSCH' -> 'Robert Bosch GmbH'
# # #     """
# # #     # FIX A: strip trailing standalone all-caps brand token (2-8 chars, not a known suffix)
# # #     name = re.sub(
# # #         r"\s+(?!GmbH|KFT|NV|BV|SE|AG|AB|AS|LLC|INC|LTD)[A-Z]{2,8}\s*$",
# # #         "", raw
# # #     ).strip()
# # #     return name if name else raw


# # # def _clean_dispatch_line(text):
# # #     """Extract and clean the Dispatch/Services address line."""
# # #     m = re.search(
# # #         r"((?:D[\s_]*i[\s_]*s[\s_]*p[\s_]*a[\s_]*t[\s_]*c[\s_]*h"
# # #         r"|S[\s_]*e[\s_]*r[\s_]*v[\s_]*i[\s_]*c[\s_]*e[\s_]*s)"
# # #         r"[\s_]*[Aa][\s_]*d[\s_]*d[\s_]*r[\s_]*e[\s_]*s[\s_]*s[^\n]+)",
# # #         text, re.IGNORECASE,
# # #     )
# # #     if not m:
# # #         return ""
# # #     raw = m.group(1)
# # #     cleaned = re.sub(r"__", " ", raw)
# # #     cleaned = re.sub(r"_", "", cleaned)
# # #     cleaned = re.sub(r"(IN[-\s]*\d{6})([A-Za-z])", r"\1 \2", cleaned)
# # #     cleaned = re.sub(r"\s{2,}", " ", cleaned)
# # #     return cleaned.strip()


# # # def _is_garbled(text):
# # #     """Return True if >30% of tokens are single characters (garbled OCR)."""
# # #     tokens = text.split()
# # #     if not tokens:
# # #         return True
# # #     single_char = sum(1 for t in tokens if len(t) == 1)
# # #     return (single_char / len(tokens)) > 0.30


# # # def _normalize_pincode_city(addr):
# # #     """Convert 'IN-382170 Ahmedabad' style into 'Ahmedabad - 382170'."""
# # #     addr = re.sub(
# # #         r",?\s*IN[-\s]+(\d{6})\s+([A-Za-z][\w\s]+?)(?:\s*,|$)",
# # #         lambda mo: ", " + mo.group(2).strip() + " - " + mo.group(1),
# # #         addr,
# # #     )
# # #     # Also handle bare pincode-city e.g. "382170 Ahmedabad" at start/end
# # #     addr = re.sub(
# # #         r"(?<!\d)(\d{6})\s+([A-Z][a-z]+)",
# # #         lambda mo: mo.group(2) + " - " + mo.group(1),
# # #         addr,
# # #     )
# # #     return addr.strip().lstrip(",").strip()


# # # def _extract_bill_to_block(text, name_pattern):
# # #     """
# # #     Extract address lines from the bill-to block.

# # #     Handles:
# # #     - Right-column metadata noise on same line (strips it off)
# # #     - Multi-line company name wrap (e.g. 'Bosch Rexroth (India) Private' / 'Limited')
# # #     - Stop tokens: INDIEN / INDIA / bare IN line, or invoice field labels

# # #     Returns a list of clean address lines (company name line excluded).
# # #     """
# # #     lines = text.splitlines()
# # #     for i, line in enumerate(lines):
# # #         if re.search(name_pattern, line, re.IGNORECASE):
# # #             parts = []
# # #             for j in range(i + 1, min(i + 12, len(lines))):
# # #                 raw = lines[j].strip()

# # #                 # Strip right-column noise (text to the right of the address)
# # #                 l = _RIGHT_COL_NOISE_RE.sub("", raw).strip()

# # #                 # Stop at country markers (INDIEN = German for India)
# # #                 if re.match(r"^(INDIEN|INDIA|IN)\s*$", l, re.IGNORECASE):
# # #                     break
# # #                 # Stop at pure right-column metadata lines
# # #                 if re.search(
# # #                     r"^(Customer\s+No|Contact\s+addr|Sales\s*:|Accounting\s*:"
# # #                     r"|Sz[áa]mla|Vev[oő]sz[áa]m|[Áá]rufogad[oó]|Payer|Supplier)",
# # #                     l, re.IGNORECASE
# # #                 ):
# # #                     continue
# # #                 # Skip name-wrap continuation lines ("Limited", "Private", etc.)
# # #                 if _NAME_WRAP_RE.match(l):
# # #                     continue
# # #                 if l:
# # #                     parts.append(l)
# # #             return parts
# # #     return []


# # # def _extract_remitter_from_block(text):
# # #     """
# # #     Line-by-line block collector for 'Bosch Ltd.' style names.
# # #     Collects address lines until a country marker.
# # #     """
# # #     lines = text.splitlines()
# # #     for i, line in enumerate(lines):
# # #         if re.search(r"Bosch\s+L[\s_I|l]*td[\s_I|l]*\.", line, re.IGNORECASE):
# # #             parts = []
# # #             for j in range(i + 1, min(i + 10, len(lines))):
# # #                 l = lines[j].strip()
# # #                 l = re.sub(r"\s+Ship\s+to.*", "", l, flags=re.IGNORECASE).strip()
# # #                 if re.match(r"^(India|INDIA|IN\s*:?)$", l):
# # #                     break
# # #                 if re.search(
# # #                     r"^(Customer\s+No|Contact\s+addr|Sales\s*:|Accounting\s*:)",
# # #                     l, re.IGNORECASE
# # #                 ):
# # #                     continue
# # #                 if l:
# # #                     parts.append(l)
# # #             return parts
# # #     return []


# # # # ── Main extractor ─────────────────────────────────────────────────────────────

# # # def extract(text, words=None):
# # #     data = {}

# # #     # ── Beneficiary (the issuing Bosch entity) ────────────────────────────────

# # #     raw_name = next((l.strip() for l in text.splitlines() if l.strip()), "")
# # #     raw_name = re.sub(r"\s*/\s*$", "", raw_name).strip()
# # #     # FIX A: strip trailing brand logo token ("Robert Bosch GmbH BOSCH" → "Robert Bosch GmbH")
# # #     data["beneficiary_name"] = _normalize_name(raw_name)

# # #     # Country: 1) VAT ID prefix (most reliable)
# # #     m_vat = re.search(r"Our\s+VAT\s+ID\s+No\s*:?\s*([A-Z]{2})\d+", text, re.IGNORECASE)
# # #     if m_vat:
# # #         vat_prefix = m_vat.group(1).upper()
# # #         data["beneficiary_country"] = _COUNTRY_MAP.get(vat_prefix, vat_prefix)
# # #     else:
# # #         # 2) Company address block or first 25 lines
# # #         header = "\n".join(text.splitlines()[:25])
# # #         m_ca_block = re.search(
# # #             r"Company\s+(?:address|ad\.)\s*:?.+", header, re.IGNORECASE | re.DOTALL
# # #         )
# # #         search_zone = m_ca_block.group(0) if m_ca_block else header
# # #         country_found = ""
# # #         for keyword, country in _COUNTRY_KEYWORDS:
# # #             if keyword in search_zone:
# # #                 country_found = country
# # #                 break
# # #         data["beneficiary_country"] = country_found or "DE"

# # #     # Beneficiary address: "Company address/ad." label → "Headquarter" → Siège Social
# # #     m_ca = re.search(
# # #         r"Company\s+(?:address|ad\.)\s*:?\s*[^,\n]+,\s*(.+)",
# # #         text, re.IGNORECASE
# # #     )
# # #     if m_ca:
# # #         addr = m_ca.group(1).strip().rstrip(",").strip()
# # #         data["beneficiary_address"] = re.sub(r",\s*$", "", addr)
# # #     else:
# # #         m_hq = re.search(r"Headquarter\s*:?\s*(.+)", text, re.IGNORECASE)
# # #         if m_hq:
# # #             data["beneficiary_address"] = m_hq.group(1).strip().rstrip(".")
# # #         else:
# # #             m_ss = re.search(
# # #                 r"Si.ge\s+Social\s*:?\s*(.+?)(?:\s*[-\u2013]\s*(?:France|N°|TVA|N\b))",
# # #                 text, re.IGNORECASE | re.DOTALL,
# # #             )
# # #             if m_ss:
# # #                 addr = m_ss.group(1).strip().replace("\n", " ")
# # #                 addr = re.sub(r"([a-z])([A-Z])", r"\1 \2", addr)
# # #                 addr = re.sub(r"\s{2,}", " ", addr).strip()
# # #                 data["beneficiary_address"] = addr
# # #             else:
# # #                 data["beneficiary_address"] = ""

# # #     # ── Remitter (India side) ─────────────────────────────────────────────────

# # #     data["remitter_country"] = "India"

# # #     # FIX B: detect all India-side Bosch entity variants
# # #     m_limited  = re.search(r"BOSCH\s+LIM:?ITED", text, re.IGNORECASE)
# # #     m_rexroth  = re.search(r"Bosch\s+Rexroth\s*\(India\)", text, re.IGNORECASE)
# # #     m_ltd      = re.search(r"Bosch\s+L[\s_I|l]*td[\s_I|l]*\.", text, re.IGNORECASE)

# # #     if m_limited:
# # #         data["remitter_name"] = "BOSCH LIMITED"
# # #     elif m_rexroth:
# # #         data["remitter_name"] = "Bosch Rexroth (India) Private Limited"
# # #     elif m_ltd:
# # #         data["remitter_name"] = "Bosch Ltd."
# # #     else:
# # #         data["remitter_name"] = ""

# # #     # ── Remitter address — ordered strategies ─────────────────────────────────
# # #     matched = False

# # #     # Strategy 1: Bill-to block for Rexroth (dispatch omits intermediate address lines)
# # #     # FIX C: prioritise bill-to block for Rexroth so "Iyava Village" etc. are captured
# # #     if not matched and m_rexroth:
# # #         block_lines = _extract_bill_to_block(text, r"Bosch\s+Rexroth\s*\(India\)")
# # #         if block_lines:
# # #             addr = ", ".join(block_lines)
# # #             addr = _normalize_pincode_city(addr)
# # #             data["remitter_address"] = addr
# # #             matched = True

# # #     # Strategy 2: Dispatch / Services address line (skip if garbled)
# # #     if not matched:
# # #         dispatch = _clean_dispatch_line(text)
# # #         if dispatch and not _is_garbled(dispatch):
# # #             m_d = re.search(
# # #                 r"(?:Dispatch|Services)\s*[Aa]ddress\s*:?\s*Bosch[^,]+,\s*(.+)",
# # #                 dispatch, re.IGNORECASE,
# # #             )
# # #             if m_d:
# # #                 addr = m_d.group(1).strip()
# # #                 # Strip name-wrap fragment leaked as first token ("Limited, ...")
# # #                 addr = re.sub(r"^(Limited|Private|Pvt\.?),?\s*", "", addr, flags=re.IGNORECASE)
# # #                 addr = _normalize_pincode_city(addr)
# # #                 data["remitter_address"] = addr
# # #                 matched = True

# # #     # Strategy 3: SIPCOT industrial park anchor
# # #     if not matched:
# # #         m_a = re.search(
# # #             r"(SIPCOT[^\n]+)\n(Plot[^\n]+)\n([^\n]+)\n(\d{6})[^\n]*",
# # #             text, re.IGNORECASE,
# # #         )
# # #         if m_a:
# # #             data["remitter_address"] = (
# # #                 m_a.group(1).strip() + ", " + m_a.group(2).strip() +
# # #                 ", " + m_a.group(3).strip() + " - " + m_a.group(4).strip()
# # #             )
# # #             matched = True

# # #     # Strategy 4: POST BOX extraction (KFT/BANGALORE style)
# # #     if not matched:
# # #         m_pb = re.search(
# # #             r"POST\s+BOX\s*:?\s*(\d+)\s+([A-Z][A-Z\s]+),\s*IN[-\s]*(\d{6})\s+([A-Za-z]+)",
# # #             text, re.IGNORECASE,
# # #         )
# # #         if m_pb:
# # #             data["remitter_address"] = (
# # #                 f"POST BOX {m_pb.group(1).strip()} {m_pb.group(2).strip()}, "
# # #                 f"{m_pb.group(4).strip()} - {m_pb.group(3).strip()}"
# # #             )
# # #             matched = True

# # #     # Strategy 5: Bill-to block for "BOSCH LIMITED" (KFT invoice style)
# # #     if not matched and m_limited:
# # #         block_lines = _extract_bill_to_block(text, r"BOSCH\s+LIM:?ITED")
# # #         if block_lines:
# # #             data["remitter_address"] = ", ".join(block_lines)
# # #             matched = True

# # #     # Strategy 6: Line-by-line collector for "Bosch Ltd."
# # #     if not matched:
# # #         block_lines = _extract_remitter_from_block(text)
# # #         if block_lines:
# # #             data["remitter_address"] = ", ".join(block_lines)
# # #             matched = True

# # #     # Strategy 7: Street-before-Ship-to + pincode+CITY+INDIA
# # #     if not matched:
# # #         m_b = re.search(
# # #             r"^([\w\s]+?)\s+Ship\s+to[^\n]*\n(\d{6})\s+([A-Z]{2,})\s*\n(?:INDIA|IN\b)",
# # #             text, re.MULTILINE | re.IGNORECASE,
# # #         )
# # #         data["remitter_address"] = (
# # #             m_b.group(1).strip() + ", " +
# # #             m_b.group(3).strip().title() + " - " + m_b.group(2).strip()
# # #             if m_b else ""
# # #         )

# # #     # ── Invoice number ─────────────────────────────────────────────────────────
# # #     # Covers English "Invoice No." and Hungarian "Számla szám"
# # #     m_inv = re.search(
# # #         r"(?:Invoice\s+No\.?|Sz[áa]mla\s+sz[áa]m)\s*:?\s*([A-Z0-9]+)",
# # #         text, re.IGNORECASE
# # #     )
# # #     data["invoice_number"] = m_inv.group(1).strip() if m_inv else ""

# # #     # ── Invoice date ───────────────────────────────────────────────────────────
# # #     # FIX D: allow arbitrary OCR noise tokens between label and date
# # #     # "Date Invoice 1 24.02.2026" — the "1" is a page-number artefact
# # #     # Use [^\n]{0,20} to skip up to 20 chars of noise but stay on same line
# # #     m_date = re.search(
# # #         r"(?:Date\s+Invoice|Sz[áa]mla\s+kelte)[^\n]{0,20}?(\d{2}[./]\d{2}[./]\d{4})",
# # #         text, re.IGNORECASE
# # #     )
# # #     data["invoice_date"] = m_date.group(1).strip() if m_date else ""

# # #     # ── Amount & currency ──────────────────────────────────────────────────────
# # #     m_amt = re.search(
# # #         r"Invoice\s+amount\s*:?\s*(EUR|USD|GBP|CHF|JPY|CZK)\s*([\d,. ]+)",
# # #         text, re.IGNORECASE,
# # #     )
# # #     if m_amt:
# # #         data["currency"] = m_amt.group(1).upper()
# # #         data["amount_foreign"] = m_amt.group(2).strip().replace(" ", "")
# # #     else:
# # #         m_ac = re.search(r"Amount\s+carried\s*:?\s*([\d,. ]+)", text, re.IGNORECASE)
# # #         if m_ac and m_ac.group(1).strip():
# # #             data["amount_foreign"] = m_ac.group(1).strip().replace(" ", "")
# # #             mc = re.search(r"\b(EUR|USD|GBP|CHF|JPY|CZK)\b", text)
# # #             data["currency"] = mc.group(1) if mc else ""
# # #         else:
# # #             m_vs = re.search(
# # #                 r"Value\s+of\s+(?:Services|goods)\s*:?\s*(EUR|USD|GBP|CHF|JPY|CZK)\s*([\d,. ]+)",
# # #                 text, re.IGNORECASE,
# # #             )
# # #             if m_vs:
# # #                 data["currency"] = m_vs.group(1).upper()
# # #                 data["amount_foreign"] = m_vs.group(2).strip().replace(" ", "")
# # #             else:
# # #                 data["amount_foreign"] = ""
# # #                 data["currency"] = ""

# # #     return data




# # # # import re

# # # # _NAME_STRIP = re.compile(
# # # #     r"\s*[,\s]+"
# # # #     r"(?:GmbH|SAS|S\.A\.S\.|spol\.\s*s\s*r\.\s*o\.|Ltd\.|\bKFT\b|\bKft\b|"
# # # #     r"SE|AG|NV|BV|S\.A\.|S\.p\.A\.|Oy|AB|AS|ApS|Inc\.|LLC|"
# # # #     r"France|Deutschland|Japan|Czech|Polska).*$",
# # # #     re.IGNORECASE,
# # # # )


# # # # def _normalize_name(raw):
# # # #     if re.search(r"Bosch\s+Corporation", raw, re.IGNORECASE):
# # # #         return "Bosch Corporation"
# # # #     name = re.sub(r"\s*\([^)]+\).*$", "", raw).strip()
# # # #     name = _NAME_STRIP.sub("", name).strip().rstrip(",").strip()
# # # #     return name if name else raw


# # # # _COUNTRY_MAP = {
# # # #     "IT": "Italy", "ES": "Spain", "BE": "Belgium", "AT": "Austria",
# # # #     "SE": "Sweden", "PL": "Poland", "CZ": "Czech Republic",
# # # #     "HU": "Hungary", "RO": "Romania", "PT": "Portugal", "JP": "Japan",
# # # #     "KR": "Korea",   # FIX 4a
# # # # }

# # # # _COUNTRY_KEYWORDS = [
# # # #     # FIX 4b: Korea keywords added before generic fallbacks
# # # #     ("Korea", "Korea"), ("KOREA", "Korea"),
# # # #     ("Sejong", "Korea"), ("Bugang", "Korea"),
# # # #     ("Japan", "Japan"), ("JAPAN", "Japan"), ("Kanagawa", "Japan"),
# # # #     ("Germany", "Germany"), ("GERMANY", "Germany"), ("Stuttgart", "Germany"),
# # # #     ("France", "France"), ("FRANCE", "France"),
# # # #     ("CZECHIA", "Czech Republic"), ("Czech", "Czech Republic"),
# # # #     ("Hungary", "Hungary"), ("Budapest", "Hungary"),
# # # # ]


# # # # def _clean_dispatch_line(text):
# # # #     m = re.search(
# # # #         r"((?:D[\s_]*i[\s_]*s[\s_]*p[\s_]*a[\s_]*t[\s_]*c[\s_]*h"
# # # #         r"|S[\s_]*e[\s_]*r[\s_]*v[\s_]*i[\s_]*c[\s_]*e[\s_]*s)"
# # # #         r"[\s_]*[Aa][\s_]*d[\s_]*d[\s_]*r[\s_]*e[\s_]*s[\s_]*s[^\n]+)",
# # # #         text, re.IGNORECASE,
# # # #     )
# # # #     if not m:
# # # #         return ""
# # # #     raw = m.group(1)
# # # #     cleaned = re.sub(r"__", " ", raw)
# # # #     cleaned = re.sub(r"_", "", cleaned)
# # # #     cleaned = re.sub(r"(IN[-\s]*\d{6})([A-Za-z])", r"\1 \2", cleaned)
# # # #     cleaned = re.sub(r"\s{2,}", " ", cleaned)
# # # #     return cleaned.strip()


# # # # def _is_garbled(text):
# # # #     """FIX 7: Detect garbled OCR — >30% single-char tokens means garbled."""
# # # #     tokens = text.split()
# # # #     if not tokens:
# # # #         return True
# # # #     single_char = sum(1 for t in tokens if len(t) == 1)
# # # #     return (single_char / len(tokens)) > 0.30


# # # # def _extract_remitter_from_block(text):
# # # #     lines = text.splitlines()
# # # #     for i, line in enumerate(lines):
# # # #         if re.search(r"Bosch\s+L[\s_I|l]*td[\s_I|l]*\.", line, re.IGNORECASE):
# # # #             parts = []
# # # #             for j in range(i + 1, min(i + 10, len(lines))):
# # # #                 l = lines[j].strip()
# # # #                 l = re.sub(r"\s+Ship\s+to.*", "", l, flags=re.IGNORECASE).strip()
# # # #                 if re.match(r"^(India|INDIA|IN\s*:?)$", l):
# # # #                     break
# # # #                 if re.search(
# # # #                     r"^(Customer\s+No|Contact\s+addr|Sales\s*:|Accounting\s*:)",
# # # #                     l, re.IGNORECASE
# # # #                 ):
# # # #                     continue
# # # #                 if l:
# # # #                     parts.append(l)
# # # #             return parts
# # # #     return []


# # # # def _extract_bill_to_block(text, name_pattern):
# # # #     """
# # # #     FIX 6: Extract address lines from the bill-to block.
# # # #     Searches for lines after `name_pattern`, stopping at country line
# # # #     (INDIEN / INDIA / IN) or invoice field labels.
# # # #     """
# # # #     lines = text.splitlines()
# # # #     for i, line in enumerate(lines):
# # # #         if re.search(name_pattern, line, re.IGNORECASE):
# # # #             parts = []
# # # #             for j in range(i + 1, min(i + 10, len(lines))):
# # # #                 l = lines[j].strip()
# # # #                 # INDIEN is German for India — stop here
# # # #                 if re.match(r"^(INDIEN|INDIA|IN)\s*$", l, re.IGNORECASE):
# # # #                     break
# # # #                 # Stop at right-column invoice metadata labels
# # # #                 if re.search(
# # # #                     r"(Sz[áa]mla|Vev[oő]sz[áa]m|[Áá]rufogad[oó]"
# # # #                     r"|Customer\s+No|Ship\s+to|Payer|Invoice\s+No"
# # # #                     r"|Date\s+Invoice|Supplier)",
# # # #                     l, re.IGNORECASE
# # # #                 ):
# # # #                     break
# # # #                 if l:
# # # #                     parts.append(l)
# # # #             return parts
# # # #     return []


# # # # def extract(text, words=None):
# # # #     data = {}

# # # #     # ── Beneficiary (issuing Bosch entity) ────────────────────────────────────

# # # #     raw_name = next((l.strip() for l in text.splitlines() if l.strip()), "")
# # # #     raw_name = re.sub(r"\s*/\s*$", "", raw_name).strip()
# # # #     # Keep full legal name as-is (e.g. "Robert Bosch KFT", "Robert Bosch Korea Limited Company")
# # # #     data["beneficiary_name"] = raw_name

# # # #     # Country: 1) VAT ID prefix (most reliable)
# # # #     m_vat = re.search(r"Our\s+VAT\s+ID\s+No\s*:?\s*([A-Z]{2})\d+", text, re.IGNORECASE)
# # # #     if m_vat:
# # # #         vat_prefix = m_vat.group(1).upper()
# # # #         data["beneficiary_country"] = _COUNTRY_MAP.get(vat_prefix, vat_prefix)
# # # #     else:
# # # #         # FIX 4b: Search company address block (expanded to 25 lines)
# # # #         header = "\n".join(text.splitlines()[:25])
# # # #         m_ca_block = re.search(
# # # #             r"Company\s+(?:address|ad\.)\s*:?.+", header, re.IGNORECASE | re.DOTALL
# # # #         )
# # # #         search_zone = m_ca_block.group(0) if m_ca_block else header
# # # #         country_found = ""
# # # #         for keyword, country in _COUNTRY_KEYWORDS:
# # # #             if keyword in search_zone:
# # # #                 country_found = country
# # # #                 break
# # # #         data["beneficiary_country"] = country_found or "DE"

# # # #     # FIX 3: Match "Company address", "Company ad.", and Hungarian "Székhely / Headquarter"
# # # #     m_ca = re.search(
# # # #         r"Company\s+(?:address|ad\.)\s*:?\s*[^,\n]+,\s*(.+)",
# # # #         text, re.IGNORECASE
# # # #     )
# # # #     if m_ca:
# # # #         addr = m_ca.group(1).strip().rstrip(",").strip()
# # # #         addr = re.sub(r",\s*$", "", addr)
# # # #         data["beneficiary_address"] = addr
# # # #     else:
# # # #         # FIX 3b: Hungarian footer label "Székhely / Headquarter: <address>"
# # # #         m_hq = re.search(r"Headquarter\s*:?\s*(.+)", text, re.IGNORECASE)
# # # #         if m_hq:
# # # #             data["beneficiary_address"] = m_hq.group(1).strip().rstrip(".")
# # # #         else:
# # # #             m_ss = re.search(
# # # #                 r"Si.ge\s+Social\s*:?\s*(.+?)(?:\s*[-\u2013]\s*(?:France|N°|TVA|N\b))",
# # # #                 text, re.IGNORECASE | re.DOTALL,
# # # #             )
# # # #             if m_ss:
# # # #                 addr = m_ss.group(1).strip().replace("\n", " ")
# # # #                 addr = re.sub(r"([a-z])([A-Z])", r"\1 \2", addr)
# # # #                 addr = re.sub(r"\s{2,}", " ", addr).strip()
# # # #                 data["beneficiary_address"] = addr
# # # #             else:
# # # #                 data["beneficiary_address"] = ""

# # # #     # ── Remitter (Bosch Ltd. / BOSCH LIMITED — India side) ───────────────────

# # # #     data["remitter_country"] = "India"

# # # #     # FIX 5: Match both "Bosch Ltd." and "BOSCH LIMITED" (including garbled "BOSCH LIM:ITED")
# # # #     m_ltd     = re.search(r"Bosch\s+L[\s_I|l]*td[\s_I|l]*\.", text, re.IGNORECASE)
# # # #     m_limited = re.search(r"BOSCH\s+LIM:?ITED", text, re.IGNORECASE)

# # # #     if m_limited:
# # # #         data["remitter_name"] = "BOSCH LIMITED"
# # # #     elif m_ltd:
# # # #         data["remitter_name"] = "Bosch Ltd."
# # # #     else:
# # # #         data["remitter_name"] = ""

# # # #     # Address strategy 1: Dispatch line — skip if garbled (FIX 7)
# # # #     dispatch = _clean_dispatch_line(text)
# # # #     matched = False
# # # #     if dispatch and not _is_garbled(dispatch):
# # # #         m_d = re.search(
# # # #             r"(?:Dispatch|Services)\s*[Aa]ddress\s*:?\s*Bosch[^,]+,\s*(.+)",
# # # #             dispatch, re.IGNORECASE,
# # # #         )
# # # #         if m_d:
# # # #             addr = m_d.group(1).strip()
# # # #             addr = re.sub(
# # # #                 r",?\s*IN[-\s]+(\d{6})\s+([A-Za-z]+)",
# # # #                 lambda mo: ", " + mo.group(2) + " - " + mo.group(1),
# # # #                 addr,
# # # #             )
# # # #             data["remitter_address"] = addr.strip().lstrip(",").strip()
# # # #             matched = True

# # # #     if not matched:
# # # #         # Strategy 2: SIPCOT anchor (Tirunelveli-style)
# # # #         m_a = re.search(
# # # #             r"(SIPCOT[^\n]+)\n(Plot[^\n]+)\n([^\n]+)\n(\d{6})[^\n]*",
# # # #             text, re.IGNORECASE,
# # # #         )
# # # #         if m_a:
# # # #             data["remitter_address"] = (
# # # #                 m_a.group(1).strip() + ", " + m_a.group(2).strip() +
# # # #                 ", " + m_a.group(3).strip() + " - " + m_a.group(4).strip()
# # # #             )
# # # #             matched = True

# # # #     if not matched:
# # # #         # FIX 6: Strategy 2b — POST BOX extraction from dispatch line (KFT/BANGALORE style)
# # # #         # Catches "POST BOX : 3000 ADUGODI HOSUR ROAD, IN- 560030 BANGALORE"
# # # #         m_pb = re.search(
# # # #             r"POST\s+BOX\s*:?\s*(\d+)\s+([A-Z][A-Z\s]+),\s*IN[-\s]*(\d{6})\s+([A-Za-z]+)",
# # # #             text, re.IGNORECASE,
# # # #         )
# # # #         if m_pb:
# # # #             data["remitter_address"] = (
# # # #                 f"POST BOX {m_pb.group(1).strip()} {m_pb.group(2).strip()}, "
# # # #                 f"{m_pb.group(4).strip()} - {m_pb.group(3).strip()}"
# # # #             )
# # # #             matched = True

# # # #     if not matched:
# # # #         # Strategy 2c — bill-to block for "BOSCH LIM:ITED" / "BOSCH LIMITED"
# # # #         if m_limited:
# # # #             block_lines = _extract_bill_to_block(text, r"BOSCH\s+LIM:?ITED")
# # # #             if block_lines:
# # # #                 data["remitter_address"] = ", ".join(block_lines)
# # # #                 matched = True

# # # #     if not matched:
# # # #         # Strategy 3: line-by-line block for "Bosch Ltd."
# # # #         block_lines = _extract_remitter_from_block(text)
# # # #         if block_lines:
# # # #             data["remitter_address"] = ", ".join(block_lines)
# # # #             matched = True

# # # #     if not matched:
# # # #         # Strategy 4: street-before-Ship-to, then pincode+CITY+INDIA
# # # #         m_b = re.search(
# # # #             r"^([\w\s]+?)\s+Ship\s+to[^\n]*\n(\d{6})\s+([A-Z]{2,})\s*\n(?:INDIA|IN\b)",
# # # #             text, re.MULTILINE | re.IGNORECASE,
# # # #         )
# # # #         data["remitter_address"] = (
# # # #             m_b.group(1).strip() + ", " +
# # # #             m_b.group(3).strip().title() + " - " + m_b.group(2).strip()
# # # #             if m_b else ""
# # # #         )

# # # #     # ── Invoice number ─────────────────────────────────────────────────────────
# # # #     # FIX 1: Add Hungarian label "Számla szám" (regex handles diacritics too)
# # # #     m_inv = re.search(
# # # #         r"(?:Invoice\s+No\.?|Sz[áa]mla\s+sz[áa]m)\s*:?\s*([A-Z0-9]+)",
# # # #         text, re.IGNORECASE
# # # #     )
# # # #     data["invoice_number"] = m_inv.group(1).strip() if m_inv else ""

# # # #     # ── Invoice date ───────────────────────────────────────────────────────────
# # # #     # FIX 2: Add Hungarian label "Számla kelte"
# # # #     m_date = re.search(
# # # #         r"(?:Date\s+Invoice|Sz[áa]mla\s+kelte)\s*:?\s*(\d{2}[./]\d{2}[./]\d{4})",
# # # #         text, re.IGNORECASE
# # # #     )
# # # #     data["invoice_date"] = m_date.group(1).strip() if m_date else ""

# # # #     # ── Amount & currency ──────────────────────────────────────────────────────
# # # #     m_amt = re.search(
# # # #         r"Invoice\s+amount\s*:?\s*(EUR|USD|GBP|CHF|JPY|CZK)\s*([\d,. ]+)",
# # # #         text, re.IGNORECASE,
# # # #     )
# # # #     if m_amt:
# # # #         data["currency"] = m_amt.group(1).upper()
# # # #         data["amount_foreign"] = m_amt.group(2).strip().replace(" ", "")
# # # #     else:
# # # #         m_ac = re.search(r"Amount\s+carried\s*:?\s*([\d,. ]+)", text, re.IGNORECASE)
# # # #         if m_ac and m_ac.group(1).strip():
# # # #             data["amount_foreign"] = m_ac.group(1).strip().replace(" ", "")
# # # #             mc = re.search(r"\b(EUR|USD|GBP|CHF|JPY|CZK)\b", text)
# # # #             data["currency"] = mc.group(1) if mc else ""
# # # #         else:
# # # #             m_vs = re.search(
# # # #                 r"Value\s+of\s+(?:Services|goods)\s*:?\s*(EUR|USD|GBP|CHF|JPY|CZK)\s*([\d,. ]+)",
# # # #                 text, re.IGNORECASE,
# # # #             )
# # # #             if m_vs:
# # # #                 data["currency"] = m_vs.group(1).upper()
# # # #                 data["amount_foreign"] = m_vs.group(2).strip().replace(" ", "")
# # # #             else:
# # # #                 data["amount_foreign"] = ""
# # # #                 data["currency"] = ""

# # # #     return data




# # # # # import re

# # # # # _NAME_STRIP = re.compile(
# # # # #     r"\s*[,\s]+"
# # # # #     r"(?:GmbH|SAS|S\.A\.S\.|spol\.\s*s\s*r\.\s*o\.|Ltd\.|"
# # # # #     r"SE|AG|NV|BV|S\.A\.|S\.p\.A\.|Oy|AB|AS|ApS|Inc\.|LLC|"
# # # # #     r"France|Deutschland|Japan|Czech|Polska).*$",
# # # # #     re.IGNORECASE,
# # # # # )


# # # # # def _normalize_name(raw):
# # # # #     """Strip legal-form suffix: 'Robert Bosch GmbH' -> 'Robert Bosch'.
# # # # #     Preserves 'Bosch Corporation' as-is (Corporation is part of the trading name).
# # # # #     """
# # # # #     if re.search(r"Bosch\s+Corporation", raw, re.IGNORECASE):
# # # # #         return "Bosch Corporation"
# # # # #     # Strip parenthesised country qualifier e.g. "(France)" in "Robert Bosch (France) S.A.S."
# # # # #     name = re.sub(r"\s*\([^)]+\).*$", "", raw).strip()
# # # # #     # Also strip remaining legal suffix if paren-strip left one
# # # # #     name = _NAME_STRIP.sub("", name).strip().rstrip(",").strip()
# # # # #     return name if name else raw


# # # # # _COUNTRY_MAP = {
# # # # #     "IT": "Italy", "ES": "Spain", "BE": "Belgium", "AT": "Austria",
# # # # #     "SE": "Sweden", "PL": "Poland", "CZ": "Czech Republic",
# # # # #     "HU": "Hungary", "RO": "Romania", "PT": "Portugal", "JP": "Japan",
# # # # # }

# # # # # # Country keywords for fallback when no VAT ID is present
# # # # # _COUNTRY_KEYWORDS = [
# # # # #     ("Japan", "Japan"), ("JAPAN", "Japan"), ("Kanagawa", "Japan"),
# # # # #     ("Germany", "Germany"), ("GERMANY", "Germany"), ("Stuttgart", "Germany"),
# # # # #     ("France", "France"), ("FRANCE", "France"),
# # # # #     ("CZECHIA", "Czech Republic"), ("Czech", "Czech Republic"),
# # # # # ]


# # # # # def _clean_dispatch_line(text):
# # # # #     """
# # # # #     Extract and clean the Dispatch/Services address line.
# # # # #     Two garbling patterns:
# # # # #       A) D_i_sp_at_ch__ad_dr_es_s_ -> double-underscore = word boundary
# # # # #       B) D _ i _ sp _ a ... (space-separated single chars - unrecoverable)
# # # # #     Pattern A is cleanable; Pattern B falls through to other strategies.
# # # # #     The "Ltd." within the line may also be garbled as "Lt _ d _." or "Lt d .".
# # # # #     """
# # # # #     m = re.search(
# # # # #         r"((?:D[\s_]*i[\s_]*s[\s_]*p[\s_]*a[\s_]*t[\s_]*c[\s_]*h"
# # # # #         r"|S[\s_]*e[\s_]*r[\s_]*v[\s_]*i[\s_]*c[\s_]*e[\s_]*s)"
# # # # #         r"[\s_]*[Aa][\s_]*d[\s_]*d[\s_]*r[\s_]*e[\s_]*s[\s_]*s[^\n]+)",
# # # # #         text, re.IGNORECASE,
# # # # #     )
# # # # #     if not m:
# # # # #         return ""
# # # # #     raw = m.group(1)
# # # # #     cleaned = re.sub(r"__", " ", raw)       # double underscore = word boundary
# # # # #     cleaned = re.sub(r"_", "", cleaned)     # strip remaining single underscores
# # # # #     # Fix pincode glued to city e.g. "IN-562109Bidadi" -> "IN-562109 Bidadi"
# # # # #     cleaned = re.sub(r"(IN[-\s]*\d{6})([A-Za-z])", r"\1 \2", cleaned)
# # # # #     cleaned = re.sub(r"\s{2,}", " ", cleaned)
# # # # #     return cleaned.strip()


# # # # # def _extract_remitter_from_block(text):
# # # # #     """
# # # # #     Strategy C: scan line-by-line for 'Bosch Ltd.' then collect address lines
# # # # #     until a country marker (India/INDIA/IN). Handles multi-line blocks where
# # # # #     P.O.Box or other intermediate lines appear between street and pincode.
# # # # #     """
# # # # #     lines = text.splitlines()
# # # # #     for i, line in enumerate(lines):
# # # # #         if re.search(r"Bosch\s+L[\s_I|l]*td[\s_I|l]*\.", line, re.IGNORECASE):
# # # # #             parts = []
# # # # #             for j in range(i + 1, min(i + 10, len(lines))):
# # # # #                 l = lines[j].strip()
# # # # #                 # Strip trailing "Ship to : XXXXXX" noise
# # # # #                 l = re.sub(r"\s+Ship\s+to.*", "", l, flags=re.IGNORECASE).strip()
# # # # #                 # Stop at country markers
# # # # #                 if re.match(r"^(India|INDIA|IN\s*:?)\s*$", l):
# # # # #                     break
# # # # #                 # Skip pure noise lines
# # # # #                 if re.search(
# # # # #                     r"^(Customer\s+No|Contact\s+addr|Sales\s*:|Accounting\s*:)",
# # # # #                     l, re.IGNORECASE
# # # # #                 ):
# # # # #                     continue
# # # # #                 if l:
# # # # #                     parts.append(l)
# # # # #             return parts
# # # # #     return []


# # # # # def extract(text, words=None):
# # # # #     data = {}

# # # # #     # ── Beneficiary (the issuing Bosch entity) ────────────────────────────────

# # # # #     # Name: first non-empty line, then strip legal suffix
# # # # #     raw_name = next(
# # # # #         (l.strip() for l in text.splitlines() if l.strip()), ""
# # # # #     )
# # # # #     raw_name = re.sub(r"\s*/\s*$", "", raw_name).strip()
# # # # #     data["beneficiary_name"] = _normalize_name(raw_name)

# # # # #     # Country: 1) VAT ID prefix
# # # # #     m_vat = re.search(r"Our\s+VAT\s+ID\s+No\s*:?\s*([A-Z]{2})\d+", text, re.IGNORECASE)
# # # # #     if m_vat:
# # # # #         vat_prefix = m_vat.group(1).upper()
# # # # #         data["beneficiary_country"] = _COUNTRY_MAP.get(vat_prefix, vat_prefix)
# # # # #     else:
# # # # #         # 2) Country keyword in Company address or first 15 lines of text
# # # # #         header = "\n".join(text.splitlines()[:15])
# # # # #         m_ca_block = re.search(
# # # # #             r"Company\s+address\s*:?.+", header, re.IGNORECASE | re.DOTALL
# # # # #         )
# # # # #         search_zone = m_ca_block.group(0) if m_ca_block else header
# # # # #         country_found = ""
# # # # #         for keyword, country in _COUNTRY_KEYWORDS:
# # # # #             if keyword in search_zone:
# # # # #                 country_found = country
# # # # #                 break
# # # # #         data["beneficiary_country"] = country_found or "DE"

# # # # #     # Address: labeled "Company address" (works when it includes a street)
# # # # #     m_ca = re.search(r"Company\s+address\s*:?\s*[^,\n]+,\s*(.+)", text, re.IGNORECASE)
# # # # #     if m_ca:
# # # # #         addr = m_ca.group(1).strip().rstrip(",").strip()
# # # # #         # Remove garbled country suffix on next line (e.g. "J_a_pa_n")
# # # # #         addr = re.sub(r",\s*$", "", addr)
# # # # #         data["beneficiary_address"] = addr
# # # # #     else:
# # # # #         # Fallback: Siège Social in footer (French entities) — needs DOTALL
# # # # #         m_ss = re.search(
# # # # #             r"Si.ge\s+Social\s*:?\s*(.+?)(?:\s*[-\u2013]\s*(?:France|N°|TVA|N\b))",
# # # # #             text, re.IGNORECASE | re.DOTALL,
# # # # #         )
# # # # #         if m_ss:
# # # # #             addr = m_ss.group(1).strip().replace("\n", " ")
# # # # #             addr = re.sub(r"([a-z])([A-Z])", r"\1 \2", addr)  # fix CamelCase merges
# # # # #             addr = re.sub(r"\s{2,}", " ", addr).strip()
# # # # #             data["beneficiary_address"] = addr
# # # # #         else:
# # # # #             data["beneficiary_address"] = ""

# # # # #     # ── Remitter (Bosch Ltd., India side) ─────────────────────────────────────

# # # # #     data["remitter_country"] = "India"

# # # # #     # Name: PDF sometimes mangles "Bosch Ltd." as "Bosch L I td I ."
# # # # #     # More flexible for Bosch Rexroth or other variants
# # # # #     m_name = re.search(r"Bosch\s+(?:L[\s_I|l]*td[\s_I|l]*\.|Rexroth|Corporation)", text, re.IGNORECASE)
# # # # #     data["remitter_name"] = m_name.group(0).strip() if m_name else ""
# # # # #     if "td" in data["remitter_name"].lower():
# # # # #         data["remitter_name"] = "Bosch Ltd."
# # # # #     elif "rexroth" in data["remitter_name"].lower():
# # # # #         data["remitter_name"] = "Bosch Rexroth"

# # # # #     # Address strategy 1: Dispatch / Services address line
# # # # #     dispatch = _clean_dispatch_line(text)
# # # # #     matched = False
# # # # #     if dispatch:
# # # # #         # Use Bosch[^,]+, to skip garbled "Ltd." variants
# # # # #         m_d = re.search(
# # # # #             r"(?:Dispatch|Services)\s*[Aa]ddress\s*:?\s*Bosch[^,]+,\s*(.+)",
# # # # #             dispatch, re.IGNORECASE,
# # # # #         )
# # # # #         if m_d:
# # # # #             addr = m_d.group(1).strip()
# # # # #             # Normalise "IN- 422007 Nashik" -> ", Nashik - 422007"
# # # # #             addr = re.sub(
# # # # #                 r",?\s*IN[-\s]+(\d{6})\s+([A-Za-z]+)",
# # # # #                 lambda mo: ", " + mo.group(2) + " - " + mo.group(1),
# # # # #                 addr,
# # # # #             )
# # # # #             data["remitter_address"] = addr.strip().lstrip(",").strip()
# # # # #             matched = True

# # # # #     if not matched:
# # # # #         # Strategy 2: SIPCOT anchor (Tirunelveli-style industrial park)
# # # # #         m_a = re.search(
# # # # #             r"(SIPCOT[^\n]+)\n(Plot[^\n]+)\n([^\n]+)\n(\d{6})[^\n]*",
# # # # #             text, re.IGNORECASE,
# # # # #         )
# # # # #         if m_a:
# # # # #             data["remitter_address"] = (
# # # # #                 m_a.group(1).strip() + ", " + m_a.group(2).strip() +
# # # # #                 ", " + m_a.group(3).strip() + " - " + m_a.group(4).strip()
# # # # #             )
# # # # #             matched = True
            
# # # # #     if not matched:
# # # # #         # Strategy 2.5: Plain city-pincode fallback for OCR (e.g. "382170 Ahmedabad")
# # # # #         m_ocr_a = re.search(r"(\d{6})\s+([A-Z][a-z]+)", text)
# # # # #         if m_ocr_a:
# # # # #             lines = text.splitlines()
# # # # #             for idx, line in enumerate(lines):
# # # # #                 if m_ocr_a.group(1) in line:
# # # # #                     # Take up to 5 previous lines to ensure "Iyava Village" etc. are captured
# # # # #                     addr_parts = []
# # # # #                     for k in range(max(0, idx-5), idx):
# # # # #                         l = lines[k].strip()
# # # # #                         # Strictly skip remitter name lines and fragments
# # # # #                         if re.search(r"\b(Robert|Bosch|Rexroth|Limited|Private|India|Ltd)\b", l, re.IGNORECASE):
# # # # #                             continue
# # # # #                         if l:
# # # # #                             addr_parts.append(l)
                    
# # # # #                     city = m_ocr_a.group(2)
# # # # #                     pincode = m_ocr_a.group(1)
# # # # #                     street = ", ".join(addr_parts).strip(", ")
# # # # #                     # Final safety: remove trailing name fragments if they leaked
# # # # #                     street = re.sub(r"^(?:Limited|Private|India|Ltd),?\s*", "", street, flags=re.IGNORECASE)
                    
# # # # #                     data["remitter_address"] = f"{street}, {city} - {pincode}".strip(", ").strip()
# # # # #                     matched = True
# # # # #                     break

# # # # #     if not matched:
# # # # #         # Strategy 3: collect address block line-by-line (handles P.O.Box, KA etc.)
# # # # #         block_lines = _extract_remitter_from_block(text)
# # # # #         if block_lines:
# # # # #             # Combine — last line may have "pincode city" or "city STATE pincode"
# # # # #             addr = ", ".join(block_lines)
# # # # #             data["remitter_address"] = addr
# # # # #             matched = True

# # # # #     if not matched:
# # # # #         # Strategy 4: street-before-Ship-to, then pincode+CITY+INDIA (simple 2-line)
# # # # #         m_b = re.search(
# # # # #             r"^([\w\s]+?)\s+Ship\s+to[^\n]*\n(\d{6})\s+([A-Z]{2,})\s*\n(?:INDIA|IN\b)",
# # # # #             text, re.MULTILINE | re.IGNORECASE,
# # # # #         )
# # # # #         data["remitter_address"] = (
# # # # #             m_b.group(1).strip() + ", " +
# # # # #             m_b.group(3).strip().title() + " - " + m_b.group(2).strip()
# # # # #             if m_b else ""
# # # # #         )

# # # # #     # ── Invoice number ─────────────────────────────────────────────────────────
# # # # #     m_inv = re.search(r"Invoice\s*(?:No\.?|Doc)\s*:?\s*([A-Z0-9]+)", text, re.IGNORECASE)
# # # # #     data["invoice_number"] = m_inv.group(1).strip() if m_inv else ""

# # # # #     # ── Invoice date ───────────────────────────────────────────────────────────
# # # # #     # More flexible for OCR tokens between label and value
# # # # #     m_date = re.search(r"Date\s+Invoice\s*.*?\s*(\d{2}[./]\d{2}[./]\d{4})", text, re.IGNORECASE)
# # # # #     data["invoice_date"] = m_date.group(1).strip() if m_date else ""

# # # # #     # ── Amount & currency ──────────────────────────────────────────────────────
# # # # #     m_amt = re.search(
# # # # #         r"Invoice\s+amount\s*:?\s*(EUR|USD|GBP|CHF|JPY|CZK)\s*([\d,. ]+)",
# # # # #         text, re.IGNORECASE,
# # # # #     )
# # # # #     if m_amt:
# # # # #         data["currency"] = m_amt.group(1).upper()
# # # # #         data["amount_foreign"] = m_amt.group(2).strip().replace(" ", "")
# # # # #     else:
# # # # #         m_ac = re.search(
# # # # #             r"Amount\s+carried\s*:?\s*([\d,. ]+)", text, re.IGNORECASE
# # # # #         )
# # # # #         if m_ac and m_ac.group(1).strip():
# # # # #             data["amount_foreign"] = m_ac.group(1).strip().replace(" ", "")
# # # # #             mc = re.search(r"\b(EUR|USD|GBP|CHF|JPY|CZK)\b", text)
# # # # #             data["currency"] = mc.group(1) if mc else ""
# # # # #         else:
# # # # #             # "Value of Services: JPY 78,000" fallback for Japan
# # # # #             m_vs = re.search(
# # # # #                 r"Value\s+of\s+(?:Services|goods)\s*:?\s*(EUR|USD|GBP|CHF|JPY|CZK)\s*([\d,. ]+)",
# # # # #                 text, re.IGNORECASE,
# # # # #             )
# # # # #             if m_vs:
# # # # #                 data["currency"] = m_vs.group(1).upper()
# # # # #                 data["amount_foreign"] = m_vs.group(2).strip().replace(" ", "")
# # # # #             else:
# # # # #                 data["amount_foreign"] = ""
# # # # #                 data["currency"] = ""

# # # # #     return data