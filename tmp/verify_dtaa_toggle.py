
import sys
import os
from unittest.mock import MagicMock

# Add project root to path
sys.path.append(r'c:\Users\HP\Desktop\15CB_third_version-main')

# Mock streamlit before importing modules that use it
sys.modules['streamlit'] = MagicMock()

from modules.invoice_calculator import recompute_invoice, invoice_state_to_xml_fields
from modules.form15cb_constants import MODE_TDS, RATE_TDS_SECB_FLG_IT_ACT
from modules.xml_generator import generate_xml_content

def test_dtaa_toggle_to_it_act():
    print("Testing 20.80% (IT Act) toggle logic...")
    
    # Simulate state with toggle ON
    state = {
        "meta": {"mode": MODE_TDS},
        "form": {
            "NonTdsBasisRateMode": "it_act_2080",
            "AmtPayForgnRem": "1000",
            "CurrencySecbCode": "USD",
            "TaxResidCert": "Y", # Even if TRC is Y
            "RateTdsSecbFlg": "2"  # Even if DTAA was selected before
        }
    }
    
    # 1. Run recompute_invoice
    recompute_invoice(state)
    form = state["form"]
    print(f"RateTdsSecbFlg after recompute: '{form.get('RateTdsSecbFlg')}'")
    
    if form.get("RateTdsSecbFlg") == RATE_TDS_SECB_FLG_IT_ACT:
        print("PASS: RateTdsSecbFlg forced to IT Act ('1').")
    else:
        print(f"FAIL: RateTdsSecbFlg is '{form.get('RateTdsSecbFlg')}', expected '1'.")
        return

    # 2. Run invoice_state_to_xml_fields
    xml_fields = invoice_state_to_xml_fields(state)
    print(f"RemForRoyFlg in xml_fields: '{xml_fields.get('RemForRoyFlg')}'")
    
    if xml_fields.get("RemForRoyFlg") == "N":
        print("PASS: RemForRoyFlg is 'N'.")
    else:
        print(f"FAIL: RemForRoyFlg is '{xml_fields.get('RemForRoyFlg')}', expected 'N'.")
        return

    # 3. Test handle in XML output
    template_content = "<root><RemForRoyFlg>{{RemForRoyFlg}}</RemForRoyFlg></root>"
    template_path = r"c:\Users\HP\Desktop\15CB_third_version-main\tmp\dummy_toggle_template.xml"
    with open(template_path, "w") as f:
        f.write(template_content)
        
    # We need enough fields to pass validation if we use the real generate_xml_content
    # or we can mock validate_required_fields
    
    xml_fields.update({
        "SWVersionNo": "1.0",
        "FormName": "15CB",
        "AssessmentYear": "2024-25",
        "RemitterPAN": "ABCDE1234F",
        "NameRemitter": "JOHN DOE",
        "CurrencySecbCode": "USD",
        "TaxLiablIt": "208",
        "BasisDeterTax": "TEST",
        "RateTdsSecB": "20.8",
        "AmtPayForgnTds": "0",
        "AmtPayIndianTds": "0",
        "ActlAmtTdsForgn": "1000"
    })
    
    xml_output = generate_xml_content(xml_fields, mode=MODE_TDS, template_path=template_path)
    print(f"XML Output Snippet: {xml_output}")
    
    if "<RemForRoyFlg>N</RemForRoyFlg>" in xml_output:
        print("PASS: XML correctly shows RemForRoyFlg as 'N'.")
    else:
        print("FAIL: XML missing or incorrect RemForRoyFlg.")

if __name__ == "__main__":
    try:
        test_dtaa_toggle_to_it_act()
    except Exception as e:
        print(f"Error during verification: {e}")
        import traceback
        traceback.print_exc()
