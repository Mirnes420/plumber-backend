import os
import sys

os.environ.setdefault('DATABASE_URL', 'sqlite:///billing_test.db')

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from main import normalize_incident_payload
import asyncio
from unittest.mock import AsyncMock, patch
import logic

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

async def test_incomplete_incident_does_not_dispatch():
    with patch.object(logic, 'analyze_triage', AsyncMock(return_value={
        'urgency': 'HIGH',
        'summary': 'Test summary',
        'ai_engine': 'unit-test',
        'gear': 'Flashlight'
    })), \
         patch.object(logic, 'log_incident'), \
         patch.object(logic, 'send_email_async', AsyncMock(return_value=True)) as send_email, \
         patch.object(logic, 'send_whatsapp_message', AsyncMock(return_value=True)) as send_whatsapp, \
         patch.object(logic.asyncio, 'create_task') as create_task:
        triage_result, notification_sent = await logic.process_incoming_incident(
            customer_phone='385919293138',
            body='Need help',
            location='',
            customer_name='',
            media_url=None,
            sender_override=None,
            image_bytes=None,
            contractor_override=None,
            demo=False,
            professional_type='contractor',
            dispatcher_email='ops@example.com',
            dispatcher_name='Ops',
            language='en',
        )

    assert triage_result['urgency'] == 'HIGH'
    assert notification_sent is False
    send_email.assert_not_called()
    send_whatsapp.assert_not_called()
    create_task.assert_not_called()

asyncio.run(test_incomplete_incident_does_not_dispatch())
print('incident parsing tests pass')
