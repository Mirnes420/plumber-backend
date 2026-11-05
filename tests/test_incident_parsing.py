import os
import sys

os.environ.setdefault('DATABASE_URL', 'sqlite:///billing_test.db')

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from main import normalize_incident_payload

sample = {
    'phone': '',
    'description': 'Customer requested a voice callback for an emergency service inquiry. Please call me at 555-123-4567.',
    'location': '123 Main St, Toronto',
    'customer_name': '',
    'contractor_id': '2',
    'professional_type': 'contractor',
    'language': 'en',
    'demo': 'false',
}

parsed = normalize_incident_payload(sample)
assert parsed['phone'] == '5551234567', parsed
assert parsed['customer_name'] == '', parsed
assert parsed['location'] == '123 Main St, Toronto', parsed

print('incident parsing tests pass')
