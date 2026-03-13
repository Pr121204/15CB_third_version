import re
from pathlib import Path

ui_path = Path('modules/batch_form_ui.py')
xml_path = Path('templates/form15cb_template.xml')

ui = ui_path.read_text(encoding='utf8')
xml = xml_path.read_text(encoding='utf8')

ui_keys = set(re.findall(r'form\[\"([A-Za-z0-9_]+)\"\]', ui))
xml_keys = set(re.findall(r'\{\{([A-Za-z0-9_]+)\}\}', xml))

from modules.invoice_calculator import invoice_state_to_xml_fields
state = {'meta': {'mode': 'TDS'}, 'extracted': {}, 'form': {}, 'resolved': {}}
calc_keys = set(invoice_state_to_xml_fields(state).keys())

missing = sorted([k for k in ui_keys if k not in calc_keys])
extra = sorted([k for k in calc_keys if k not in xml_keys])
missing_from_calc = sorted([k for k in xml_keys if k not in calc_keys])

print('UI keys not in invoice_state_to_xml_fields (count):', len(missing))
print(missing[:60])
print('invoice_state_to_xml_fields keys not in template (count):', len(extra))
print(extra[:60])
report = []
report.append(f'UI keys not in invoice_state_to_xml_fields (count): {len(missing)}')
report.extend(missing[:100])
report.append(f'invoice_state_to_xml_fields keys not in template (count): {len(extra)}')
report.extend(extra[:100])
report.append(f'template keys missing from invoice_state_to_xml_fields (count): {len(missing_from_calc)}')
report.extend(missing_from_calc[:100])

out_dir = Path('tmp')
out_dir.mkdir(exist_ok=True)
out_path = out_dir / 'ui_xml_sync_report.txt'
out_path.write_text("\n".join(report), encoding='utf8')
print(f'Report written to {out_path}')
