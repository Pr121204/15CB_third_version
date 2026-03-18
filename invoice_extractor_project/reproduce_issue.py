from extractors.bosch_sap import extract

text = """Robert Bosch, spol. s r. o.
Roberta Bosche 2678
370 04 CESKE BUDEJOVICE
CZECHIA
Original
Our VAT ID: CZ46678735
Our Business ID: 46678735 Page 1 of 2
Invoice
Billing Document 5000562983
"""

data = extract(text)
print(f"Beneficiary Address: {data.get('beneficiary_address')}")

expected = "Roberta Bosche 2678, 370 04 CESKE BUDEJOVICE, CZECHIA"
actual = data.get('beneficiary_address')

if "Original" in actual:
    print("ISSUE REPRODUCED: 'Original' found in address.")
else:
    print("ISSUE NOT REPRODUCED.")
