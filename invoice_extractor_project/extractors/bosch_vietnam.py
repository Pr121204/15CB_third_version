
import re
from text_utils import normalize_company, normalize_address, clean_value, remove_hex, validate_amount, detect_country
from coordinate_utils import find_label, value_right, value_below, extract_block, extract_currency

def extract(text, words):
    data = {}

    # 1. Labels for coordinate extraction
    invoice_no_label = find_label(words, "InvoiceNo") or find_label(words, "Sốhoáđơn")
    invoice_date_label = find_label(words, "InvoiceDate") or find_label(words, "Ngàyhóađơn")
    customer_label = find_label(words, "CustomerName")
    address_label = find_label(words, "Address")
    total_label = find_label(words, "TotalAmount") or find_label(words, "Thànhtiền")
    bank_addr_label = find_label(words, "Bankaddress")

    # 2. Extraction with Coordinate-first, Regex-fallback logic

    # --- Invoice Number ---
    num = clean_value(value_right(words, invoice_no_label))
    if not num:
        # Use [^0-9A-Z]* to skip tokens like "):" before the actual number
        m = re.search(r"(?:Invoice\s+No\.?|Số\s+h[oóò]a\s+đơn)[^0-9A-Z]*\s*([A-Z0-9]+)", text, re.I)
        num = m.group(1) if m else ""
    data["invoice_number"] = num

    # --- Invoice Date ---
    dt = clean_value(value_right(words, invoice_date_label))
    if not dt:
        # Use [^0-9]* to skip tokens like "):" before the date
        m = re.search(r"(?:Invoice\s+Date|Ng[àa]y\s+h[oóò]a\s+đơn)[^0-9]*\s*(\d{2}[,./ ]\d{2}[,./ ]\d{4})", text, re.I)
        dt = m.group(1) if m else ""
    
    # Second fallback: look for "Ky ngay: 26/02/2026"
    if not dt:
        m = re.search(r"K[ýy]\s+ng[àa]y\s*[:\.]*\s*(\d{2}[,./]\d{2}[,./]\d{4})", text, re.I)
        dt = m.group(1) if m else ""

    # Normalize date separators to .
    if dt:
        dt = re.sub(r"[,/ ]", ".", dt)
    data["invoice_date"] = dt

    # --- Remitter Name ---
    remitter = value_below(words, customer_label)
    if not remitter:
        # Skip labels and artifact "INVOICE"
        m = re.search(r"(?:Customer\s+Name|Tên\s+khách\s+hàng)[^:]*[:\.]*\s*(?:INVOICE\s*)?\n?([^\n]+)", text, re.I)
        remitter = m.group(1).strip() if m else ""
        # Remove any leading garbage like "): "
        remitter = re.sub(r"^[\s):]*", "", remitter).strip()
    data["remitter_name"] = normalize_company(remitter)

    # --- Remitter Address ---
    remitter_addr = extract_block(words, address_label)
    if not remitter_addr:
        # Search after Address label up to MST/Billing No or similar
        m = re.search(r"(?:Address|[ĐĐ]ịa\s+ch[ỉi])[^:]*[:\.]*\s*(.*?)\n(?:MST|Mã\s+số\s+thuế|\$6\s+HD|Billing\s+No)", text, re.I | re.S)
        remitter_addr = m.group(1).strip() if m else ""
        # Clean up leading garbage
        remitter_addr = re.sub(r"^[\s):]*M[éê]\s+cua\s+co\u2019\s+quan\s+thu[eé]\s*:", "", remitter_addr, flags=re.I).strip()
    
    remitter_addr = remove_hex(remitter_addr)
    # Fix common OCR failures for "Hosur Road"
    remitter_addr = re.sub(r"\bron\b\s+Road", "Hosur Road", remitter_addr, flags=re.I)
    remitter_addr = re.sub(r"\bHosu\b\s+Road", "Hosur Road", remitter_addr, flags=re.I)
    data["remitter_address"] = normalize_address(remitter_addr)
    data["remitter_country"] = detect_country(remitter_addr, default="India")

    # --- Beneficiary ---
    data["beneficiary_name"] = "Bosch Global Software Technologies Company Limited"
    
    beneficiary_addr = value_right(words, bank_addr_label, max_dx=300)
    if not beneficiary_addr:
        # Specific search for HCM/Vietnam address
        m = re.search(r"Beneficiary\s+Bank\s+name\s*:.*?\n.*?address\s*:\s*(.*?)\n", text, re.I | re.S)
        if not m:
            m = re.search(r"33\s+Le\s+Duan\s+St\..*?Vietnam", text, re.I | re.S)
        beneficiary_addr = m.group(1).strip() if m and m.groups() else (m.group(0).strip() if m else "")
    
    if not beneficiary_addr:
        # Fallback to hardcoded if it's the standard Bosch Vietnam office
        beneficiary_addr = "33 Le Duan St., Dist. 1, HCMC, Vietnam"
        
    beneficiary_addr = remove_hex(beneficiary_addr)
    data["beneficiary_address"] = normalize_address(beneficiary_addr)
    data["beneficiary_country"] = "Vietnam"

    # --- Amount & Currency ---
    amount = value_right(words, total_label)
    if not amount or not validate_amount(amount):
        # Target the final total line specifically
        # "Tong tien thanh toan (Total Amount):"
        m = re.search(r"thanh\s*to[aáà]n[^\d]*([\d,.]+)", text, re.I)
        if m:
            amount = m.group(1).strip()
        else:
            # Fallback: find the VERY LAST occurrence of Total Amount + digits
            all_matches = re.findall(r"Total\s*Amount[^\d]*([\d,.]+)", text, re.I)
            if all_matches:
                amount = all_matches[-1].strip()

    amount = clean_value(amount)
    data["amount_foreign"] = validate_amount(amount)

    curr = extract_currency(words)
    if not curr:
        m = re.search(r"\b(USD|EUR|VND)\b", text)
        curr = m.group(1) if m else ""
    data["currency"] = curr

    return data
