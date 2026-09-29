from io import StringIO

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from SP.models import PanelData
from SP.tests.helpers import add_readings, create_community, create_customer, create_system, utc

LIST_URL = "/sp/panel-data/"


class PanelDataApiTests(TestCase):
    def setUp(self):
        own_community = create_community("Own")
        other_community = create_community("Other")
        self.system = create_system(own_community)
        add_readings(self.system, utc(2026, 9, 28, 12, 0), [1.0] * 3)
        add_readings(create_system(other_community, "Other system"), utc(2026, 9, 28, 12, 0), [1.0] * 2)
        self.own = PanelData.objects.filter(system__community=own_community).first()
        self.other = PanelData.objects.filter(system__community=other_community).first()
        self.customer = create_customer(own_community)

        call_command("create_bridge_user", stdout=StringIO())
        self.bridge = APIClient()
        self.bridge.credentials(HTTP_AUTHORIZATION=f"Token {Token.objects.get(user__username='bridge').key}")

    def payload(self):
        # Lo stesso formato inviato da SensorsBridge.py
        return {"time_stamp": "2026-09-28 13:30", "temperature": 21.5, "lightness": 812.0, "power": 3.2, "system": self.system.id}

    def test_anonymous_requests_are_rejected(self):
        client = APIClient()
        self.assertEqual(client.get(LIST_URL).status_code, 401)
        self.assertEqual(client.post(LIST_URL, self.payload(), format="json").status_code, 401)
        self.assertEqual(client.delete(f"{LIST_URL}{self.own.pk}/").status_code, 401)

    def test_bridge_can_only_add_readings(self):
        self.assertEqual(self.bridge.post(LIST_URL, self.payload(), format="json").status_code, 201)
        self.assertEqual(self.bridge.patch(f"{LIST_URL}{self.own.pk}/", {"power": 0}, format="json").status_code, 403)
        self.assertEqual(self.bridge.delete(f"{LIST_URL}{self.own.pk}/").status_code, 403)

    def test_invalid_token_is_rejected(self):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION="Token " + "0" * 40)
        self.assertEqual(client.post(LIST_URL, self.payload(), format="json").status_code, 401)

    def test_customer_reads_only_its_community(self):
        client = APIClient()
        client.force_authenticate(self.customer)

        response = client.get(LIST_URL)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 3)
        self.assertEqual(client.get(f"{LIST_URL}{self.other.pk}/").status_code, 404)
        self.assertEqual(client.post(LIST_URL, self.payload(), format="json").status_code, 403)
        self.assertEqual(client.delete(f"{LIST_URL}{self.own.pk}/").status_code, 403)

    def test_superuser_reads_everything(self):
        client = APIClient()
        client.force_authenticate(User.objects.create_superuser("admin", password=None))

        self.assertEqual(client.get(LIST_URL).json()["count"], 5)
        self.assertEqual(client.delete(f"{LIST_URL}{self.other.pk}/").status_code, 204)

    def test_list_is_paginated(self):
        add_readings(self.system, utc(2026, 9, 29, 0, 0), [1.0] * 150)
        client = APIClient()
        client.force_authenticate(self.customer)

        page = client.get(LIST_URL).json()

        self.assertEqual(len(page["results"]), 100)
        self.assertIsNotNone(page["next"])
