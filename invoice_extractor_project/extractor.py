
import json, os
from pdf_reader import extract_text
from invoice_router import detect_invoice_type
from extractors import bosch_vietnam, generic

def process_pdf(path):
    text = extract_text(path)
    print(f"--- Extracted text ---")
    print(text)
    print("--- End of extracted text ---")
    inv_type = detect_invoice_type(text)

    if inv_type == "bosch_vietnam":
        data = bosch_vietnam.extract(text)
    else:
        data = generic.extract(text)

    data["file"] = os.path.basename(path)
    return data

def process_file(path):
    if path.lower().endswith(".pdf"):
        return [process_pdf(path)]
    raise ValueError("Only PDF supported")

if __name__ == "__main__":
    INPUT_PATH = r"C:/Users/HP/Downloads/fwdrequesting15cbforrbin_citibank/00000805.pdf"
    results = process_file(INPUT_PATH)
    print(json.dumps(results, indent=2))
