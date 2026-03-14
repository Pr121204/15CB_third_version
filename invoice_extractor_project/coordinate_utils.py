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


def reconstruct_line_from_words(words, anchor_text, y_tolerance=8, x_merge_gap=3.0, x_max_right=600):
    """
    Reconstruct a clean text line from word-coordinate data, merging split characters.

    pdfplumber sometimes splits ligature/diacritic characters across slightly different
    y-positions (e.g. 'Gyömrő' at top=766 and 'i' at top=758 but x0=219.6 ≈ x1=219.4).
    This function:
      1. Finds the anchor word (e.g. 'Headquarter') by text match.
      2. Collects all words within y_tolerance of the anchor's top.
      3. Also absorbs single-char tokens that immediately follow (x-gap ≤ x_merge_gap)
         ANY collected word, even if they're at a slightly different y.
      4. Sorts all collected tokens left-to-right and joins them.

    Returns the reconstructed line as a string, or "" if anchor not found.
    """
    anchor_word = None
    for w in words:
        if anchor_text.lower() in w["text"].lower():
            anchor_word = w
            break
    if not anchor_word:
        return ""

    anchor_top = anchor_word["top"]

    # Pass 1: collect words on same line as anchor
    line_words = []
    for w in words:
        if abs(w["top"] - anchor_top) <= y_tolerance and w["x0"] <= x_max_right:
            line_words.append(w)

    # Pass 2: absorb single-char stragglers that are x-adjacent to any line word,
    # within a loose y window (±15px of anchor)
    line_x1s = {id(w): w["x1"] for w in line_words}
    for w in words:
        if w in line_words:
            continue
        if len(w["text"].strip()) != 1:
            continue
        if abs(w["top"] - anchor_top) > 15:
            continue
        # Check if this char immediately follows any word already on the line
        for lw in line_words:
            gap = w["x0"] - lw["x1"]
            if 0 <= gap <= x_merge_gap:
                line_words.append(w)
                break

    # Sort left-to-right, then merge adjacent single chars into their predecessor
    line_words.sort(key=lambda w: w["x0"])

    tokens = []
    for w in line_words:
        ch = w["text"].strip()
        if not ch:
            continue
        # If single char and immediately follows last token (gap ≤ x_merge_gap), fuse
        if len(ch) == 1 and tokens:
            last = tokens[-1]
            gap = w["x0"] - last["x1"]
            if gap <= x_merge_gap:
                tokens[-1] = {**last, "text": last["text"] + ch, "x1": w["x1"]}
                continue
        tokens.append({**w})

    return " ".join(t["text"] for t in tokens)




# import re

# STOP_LABELS = [
#     "TaxCode",
#     "MST",
#     "BillingNo",
#     "Attention",
#     "Contact",
#     "InvoiceDate",
#     "Sôhoáđơn",
#     "Sốhoáđơn",
#     "SốHĐ",
#     "LiênHệ",
#     "Sốhợpđồng",
#     "Contractno",
#     "Mãsốthuế",
#     "Mãcủacơ",
#     "quanthuế",
#     "Ngânhàng",
#     "BeneficiaryBankname",
# ]

# def is_noise_token(text):
#     # Purely numeric or date-like tokens are likely values (invoice numbers, years, dates)
#     if text.isdigit() or re.match(r"^\d{1,2}[./-]\d{1,2}[./-]\d{2,4}$", text):
#         return False
#     # Filter SWIFT codes (8-11 uppercase chars)
#     if re.match(r"^[A-Z0-9]{8,11}$", text):
#         # Don't filter common currency/unit codes
#         if text in ["USD", "EUR", "VND", "PCS", "UNT"]:
#             return False
#         return True
#     return False

# def find_label(words, label):
#     for w in words:
#         if label.lower() in w["text"].lower():
#             return w
#     return None

# def value_right(words, label_word, max_dx=300, dy=5):
#     if not label_word:
#         return ""
    
#     candidates = []
#     for w in words:
#         same_line = abs(w["top"] - label_word["top"]) < dy
#         right_side = 0 < (w["x0"] - label_word["x1"]) < max_dx
#         if same_line and right_side:
#             candidates.append(w)
            
#     # Sort by horizontal position to process in reading order
#     candidates.sort(key=lambda x: x["x0"])
    
#     values = []
#     for w in candidates:
#         token = w["text"].strip()
#         if token in [":", "-", "|"]:
#             continue
            
#         # Proximity-aware stop labels: skip if close (synonym), break if far (next field)
#         is_stop = any(label.lower() in token.lower() for label in STOP_LABELS)
#         if is_stop:
#             dx = w["x0"] - label_word["x1"]
#             if dx < 120:
#                 continue
#             else:
#                 break
                
#         if is_noise_token(token):
#             continue
#         values.append(token)
#     return " ".join(values)

# def value_below(words, label_word, dy=15):
#     if not label_word:
#         return ""
    
#     # 1. Find all words below within threshold
#     candidates = []
#     for w in words:
#         v_dist = w["top"] - label_word["bottom"]
#         if 0 < v_dist < dy:
#             candidates.append(w)
    
#     if not candidates:
#         return ""
        
#     # 2. Pick the closest line (group by top coordinate roughly)
#     candidates.sort(key=lambda x: x["top"])
#     closest_top = candidates[0]["top"]
    
#     # Filter to words on that same line (within 3 pixels)
#     line_words = [w for w in candidates if abs(w["top"] - closest_top) < 3]
    
#     # 3. Sort by horizontal position
#     line_words.sort(key=lambda x: x["x0"])
    
#     values = []
#     for w in line_words:
#         token = w["text"].strip()
#         if any(s.lower() in token.lower() for s in STOP_LABELS):
#             break
#         if is_noise_token(token):
#             continue
#         values.append(token)
#     return " ".join(values)

# def extract_block(words, label_word):
#     if not label_word:
#         return ""
#     block = []
#     for w in words:
#         dy = w["top"] - label_word["bottom"]
#         if 0 < dy < 40:
#             token = w["text"].strip()
#             if any(stop.lower() in token.lower() for stop in STOP_LABELS):
#                 break
#             block.append(token)
#     return " ".join(block)

# def extract_currency(words):
#     for w in words:
#         if re.match(r"USD|EUR|GBP|JPY", w["text"]):
#             return w["text"]
#     return ""
