
import re
from text_utils import normalize_company, normalize_address, clean_value, remove_hex, validate_amount
from coordinate_utils import find_label, value_right, value_below, extract_block, extract_currency

def extract(text, words):
    data = {}

    # 1. Labels
    invoice_no_label = find_label(words, "InvoiceNo") or find_label(words, "Sốhoáđơn")
    invoice_date_label = find_label(words, "InvoiceDate") or find_label(words, "Ngàyhóađơn")
    customer_label = find_label(words, "CustomerName")
    address_label = find_label(words, "Address")
    total_label = find_label(words, "TotalAmount") or find_label(words, "Thànhtiền")
    bank_addr_label = find_label(words, "Bankaddress")

    # 2. Extraction
    data["invoice_number"] = clean_value(value_right(words, invoice_no_label))
    data["invoice_date"] = clean_value(value_right(words, invoice_date_label))

    remitter = value_below(words, customer_label)
    data["remitter_name"] = normalize_company(remitter) if remitter else ""

    remitter_addr = extract_block(words, address_label)
    remitter_addr = remove_hex(remitter_addr)
    data["remitter_address"] = normalize_address(remitter_addr)
    data["remitter_country"] = "India"

    data["beneficiary_name"] = "Bosch Global Software Technologies Company Limited"
    
    beneficiary_addr = value_right(words, bank_addr_label, max_dx=300)
    if not beneficiary_addr:
        # Fallback to the previous multi-line regex if coordinates aren't clear
        m = re.search(
            r"Bosch\s*Global\s*Software\s*Technologies.*?\n(.*?)\nĐT",
            text,
            re.DOTALL | re.IGNORECASE
        )
        beneficiary_addr = m.group(1) if m else ""
    
    beneficiary_addr = remove_hex(beneficiary_addr)
    data["beneficiary_address"] = normalize_address(beneficiary_addr)
    data["beneficiary_country"] = "Vietnam"

    amount = value_right(words, total_label)
    amount = clean_value(amount)
    if not validate_amount(amount):
        # Regex safety fallback
        m = re.search(r"TotalAmount\)\s*:\s*([\d.,]+)", text, re.I) or \
            re.search(r"Total\s*Amount\s*:\s*([\d.,]+)", text, re.I)
        amount = m.group(1) if m else ""
    data["amount_foreign"] = validate_amount(amount)

    data["currency"] = extract_currency(words)

    return data
