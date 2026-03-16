import re, json

text = open('test_chassis_text.txt', encoding='utf-8').read()

print(f"--- DEBUG START ---")

label_m = re.search(
    r"(?:Invoice\s+amount|Total\s+amount|Total\s+Invoice\s+Value|Value\s+of\s+goods|Invoice amount)",
    text, re.IGNORECASE
)

if label_m:
    print(f"Label match: {label_m.group(0)} at {label_m.start()}-{label_m.end()}")
    zone = text[label_m.end():label_m.end()+60]
    print(f"Zone raw: {repr(zone)}")
    zone_clean = re.sub(r"\(VAT\)|VAT|%|0\.000", "", zone, flags=re.I)
    print(f"Zone clean: {repr(zone_clean)}")
    
    # Try the num regex
    m_num = re.search(
        r"(?:([A-H J-Z]{3})\s*)?([\d,. ]{4,})",
        zone_clean, re.IGNORECASE
    )
    if m_num:
        print(f"Num match G1: {m_num.group(1)}, G2: {m_num.group(2)}")
    else:
        print("No num match in zone")
else:
    print("No label match in text")

print(f"--- DEBUG END ---")
