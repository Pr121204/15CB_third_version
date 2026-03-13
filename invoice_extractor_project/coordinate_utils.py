import re

STOP_LABELS = [
    "TaxCode",
    "MST",
    "BillingNo",
    "Attention",
    "Contact",
    "InvoiceDate",
    "Sôhoáđơn",
    "Sốhoáđơn",
    "SốHĐ",
    "LiênHệ",
    "Sốhợpđồng",
    "Contractno",
    "Mãsốthuế",
    "Mãcủacơ",
    "quanthuế",
    "Ngânhàng",
    "BeneficiaryBankname",
]

def is_noise_token(text):
    # Purely numeric or date-like tokens are likely values (invoice numbers, years, dates)
    if text.isdigit() or re.match(r"^\d{1,2}[./-]\d{1,2}[./-]\d{2,4}$", text):
        return False
    # Filter SWIFT codes (8-11 uppercase chars)
    if re.match(r"^[A-Z0-9]{8,11}$", text):
        # Don't filter common currency/unit codes
        if text in ["USD", "EUR", "VND", "PCS", "UNT"]:
            return False
        return True
    return False

def find_label(words, label):
    for w in words:
        if label.lower() in w["text"].lower():
            return w
    return None

def value_right(words, label_word, max_dx=300, dy=5):
    if not label_word:
        return ""
    
    candidates = []
    for w in words:
        same_line = abs(w["top"] - label_word["top"]) < dy
        right_side = 0 < (w["x0"] - label_word["x1"]) < max_dx
        if same_line and right_side:
            candidates.append(w)
            
    # Sort by horizontal position to process in reading order
    candidates.sort(key=lambda x: x["x0"])
    
    values = []
    for w in candidates:
        token = w["text"].strip()
        if token in [":", "-", "|"]:
            continue
            
        # Proximity-aware stop labels: skip if close (synonym), break if far (next field)
        is_stop = any(label.lower() in token.lower() for label in STOP_LABELS)
        if is_stop:
            dx = w["x0"] - label_word["x1"]
            if dx < 120:
                continue
            else:
                break
                
        if is_noise_token(token):
            continue
        values.append(token)
    return " ".join(values)

def value_below(words, label_word, dy=15):
    if not label_word:
        return ""
    
    # 1. Find all words below within threshold
    candidates = []
    for w in words:
        v_dist = w["top"] - label_word["bottom"]
        if 0 < v_dist < dy:
            candidates.append(w)
    
    if not candidates:
        return ""
        
    # 2. Pick the closest line (group by top coordinate roughly)
    candidates.sort(key=lambda x: x["top"])
    closest_top = candidates[0]["top"]
    
    # Filter to words on that same line (within 3 pixels)
    line_words = [w for w in candidates if abs(w["top"] - closest_top) < 3]
    
    # 3. Sort by horizontal position
    line_words.sort(key=lambda x: x["x0"])
    
    values = []
    for w in line_words:
        token = w["text"].strip()
        if any(s.lower() in token.lower() for s in STOP_LABELS):
            break
        if is_noise_token(token):
            continue
        values.append(token)
    return " ".join(values)

def extract_block(words, label_word):
    if not label_word:
        return ""
    block = []
    for w in words:
        dy = w["top"] - label_word["bottom"]
        if 0 < dy < 40:
            token = w["text"].strip()
            if any(stop.lower() in token.lower() for stop in STOP_LABELS):
                break
            block.append(token)
    return " ".join(block)

def extract_currency(words):
    for w in words:
        if re.match(r"USD|EUR|GBP|JPY", w["text"]):
            return w["text"]
    return ""
