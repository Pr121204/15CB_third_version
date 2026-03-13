import re, json, os
from pdf_reader import extract_pdf_data
from invoice_router import detect_invoice_type
from text_utils import remove_hex_strings, normalize_address
from extractors import bosch_vietnam, bosch_germany, bosch_sap, generic


def detect_template(text):
    if "HÓA ĐƠN GIÁ TRỊ GIA TĂNG" in text:
        return "bosch_vietnam"
    # SAP/Billing Document format (may be compressed "BillingDocument" or spaced)
    if re.search(r"Billing\s*Document", text, re.IGNORECASE):
        return "bosch_sap"
    # All Robert Bosch entity variants (GmbH, France SAS, spol. s r.o., etc.)
    if re.search(r"Robert\s+Bosch", text, re.IGNORECASE):
        return "bosch_germany"
    # Non-Robert-Bosch Bosch entities (e.g. Bosch Corporation, Japan)
    if re.search(r"Bosch\s+Corporation", text, re.IGNORECASE):
        return "bosch_germany"
    return "generic"


def process_pdf(path):
    text, words = extract_pdf_data(path)
    text = remove_hex_strings(text)

    print(f"--- Extracted text ---")
    print(text)
    print("--- End of extracted text ---")

    inv_type = detect_template(text)

    if inv_type == "bosch_vietnam":
        data = bosch_vietnam.extract(text, words)
    elif inv_type == "bosch_germany":
        data = bosch_germany.extract(text, words)
    elif inv_type == "bosch_sap":
        data = bosch_sap.extract(text, words)
    else:
        try:
            data = generic.extract(text, words)
        except TypeError:
            data = generic.extract(text)

    data["file"] = os.path.basename(path)
    return data


def process_file(path):
    if path.lower().endswith(".pdf"):
        return [process_pdf(path)]
    raise ValueError("Only PDF supported")


if __name__ == "__main__":
    INPUT_PATH = r"C:/Users/HP/Downloads/70616222.pdf"
    results = process_file(INPUT_PATH)
    print(json.dumps(results, indent=2))
    with open("result.json", "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)