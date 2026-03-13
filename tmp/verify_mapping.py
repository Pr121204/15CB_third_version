
import sys
import os
from unittest.mock import MagicMock

# Add project root to path
sys.path.append(r'c:\Users\HP\Desktop\15CB_third_version-main')

# Mock streamlit before importing modules that use it
sys.modules['streamlit'] = MagicMock()

from modules.invoice_calculator import recompute_invoice
from modules.form15cb_constants import MODE_NON_TDS, MODE_TDS
from modules.xml_generator import generate_xml_content, build_xml_fields_by_mode

def test_calculator_non_tds():
    print("Testing calculator in non-tds mode...")
    state = {
        "meta": {"mode": MODE_NON_TDS},
        "form": {
            "DednDateTds": "15/03/2026",
            "AmtPayForgnRem": "1000",
            "RateTdsSecB": "10"
        }
    }
    
    # In non-tds mode, DednDateTds should NOT be cleared
    recompute_invoice(state)
    updated_form = state["form"]
    
    print(f"DednDateTds after recompute: '{updated_form.get('DednDateTds')}'")
    if updated_form.get("DednDateTds") == "15/03/2026":
        print("PASS: DednDateTds preserved in non-tds mode.")
    else:
        print("FAIL: DednDateTds cleared in non-tds mode.")

def test_xml_generator_non_tds():
    print("\nTesting XML generator in non-tds mode...")
    # Add mandatory fields to avoid ValueError
    state = {
        "meta": {"mode": MODE_NON_TDS},
        "form": {
            "DednDateTds": "15/03/2026",
            "NatureRem": "TEST",
            "RemitterName": "SENDER",
            "RemitteeName": "RECEIVER",
            "AmtPayForgnRem": "1000",
            "SWVersionNo": "1.0",
            "FormName": "15CB",
            "AssessmentYear": "2024-25",
            "RemitterPAN": "ABCDE1234F",
            "NameRemitter": "JOHN DOE",
            "CurrencySecbCode": "USD",
            "RelArtDetlDDtaa": "YES",
            "NatureRemDtaa": "ROYALTY"
        }
    }
    
    # 1. Test build_xml_fields_by_mode
    xml_fields = build_xml_fields_by_mode(state)
    print(f"DednDateTds in xml_fields: '{xml_fields.get('DednDateTds')}'")
    # print(f"All xml_fields: {xml_fields}")
    
    # 2. Test generate_xml_content
    # Create a dummy template
    template_content = "<root><DednDateTds>{{DednDateTds}}</DednDateTds><Other>{{Other}}</Other></root>"
    template_path = r"c:\Users\HP\Desktop\15CB_third_version-main\tmp\dummy_template.xml"
    with open(template_path, "w") as f:
        f.write(template_content)
        
    xml_output = generate_xml_content(xml_fields, mode=MODE_NON_TDS, template_path=template_path)
    print(f"XML Output: {xml_output}")
    
    # Check for either original or ISO format
    if "15/03/2026" in xml_output or "2026-03-15" in xml_output:
        print("PASS: DednDateTds included in XML in non-tds mode.")
    else:
        print("FAIL: DednDateTds missing from XML in non-tds mode.")

if __name__ == "__main__":
    try:
        test_calculator_non_tds()
        test_xml_generator_non_tds()
    except Exception as e:
        print(f"Error during verification: {e}")
        import traceback
        traceback.print_exc()
