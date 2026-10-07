from logic import get_localized_incident_copy


def test_customer_confirmation_uses_spanish():
    lines = get_localized_incident_copy("customer_confirmation", "Spanish", notification_sent=True)
    assert "Gracias" in lines[0]
    assert "Hemos recibido" in lines[1]


def test_contractor_email_labels_are_localized():
    labels = get_localized_incident_copy("contractor_email_labels", "Spanish")
    assert "Ubicación" in labels["location"]
    assert "Urgencia" in labels["urgency"]
