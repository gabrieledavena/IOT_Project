from datetime import timedelta

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from SP.models import Customer, Intervention, PhotovoltaicSystem
from SP.tests.helpers import create_community, create_customer, create_system

Status = Intervention.Status
FAULT = PhotovoltaicSystem.Status.FAULT


def faulty_system(community, name="Capannone"):
    system = create_system(community, name)
    system.status, system.last_check = FAULT, timezone.now() - timedelta(hours=3)
    system.save()
    return system


class InterventionTestCase(TestCase):
    def setUp(self):
        self.community = create_community("Rossi Trasporti")
        self.customer = create_customer(self.community)
        self.system = faulty_system(self.community)
        self.technician = User.objects.create_user("tecnico", first_name="Paolo", last_name="Neri", is_staff=True)
        self.other_technician = User.objects.create_user("tecnico2", first_name="Sara", last_name="Gialli", is_staff=True)
        self.tomorrow = timezone.localdate() + timedelta(days=1)

    def request_intervention(self, **fields):
        return Intervention.objects.create(
            system=self.system, requested_by=self.customer, preferred_date=self.tomorrow, **fields
        )


class InterventionRequestTests(InterventionTestCase):
    def test_button_appears_only_for_the_community_users_of_a_system_with_a_probable_fault(self):
        url = f"/sp/system/{self.system.id}/"
        self.client.force_login(self.customer)
        self.assertContains(self.client.get(url, follow=True), "Richiedi intervento")

        self.system.status = PhotovoltaicSystem.Status.DIRTY
        self.system.save()
        self.assertNotContains(self.client.get(url, follow=True), "Richiedi intervento")

        self.system.status = FAULT
        self.system.save()
        self.client.force_login(self.technician)
        self.assertNotContains(self.client.get(url, follow=True), "Richiedi intervento")

    def test_customer_requests_an_intervention_for_a_day_of_the_calendar(self):
        self.client.force_login(self.customer)
        url = f"/sp/system/{self.system.id}/intervention/"

        page = self.client.get(url)
        self.assertContains(page, 'type="date"')
        self.assertContains(page, f'min="{self.tomorrow.isoformat()}"')

        response = self.client.post(url, {"preferred_date": self.tomorrow.isoformat(), "customer_notes": "Inverter spento"})

        self.assertRedirects(response, f"/sp/system/{self.system.id}/", fetch_redirect_response=False)
        intervention = Intervention.objects.get()
        self.assertEqual(intervention.status, Status.REQUESTED)
        self.assertEqual(intervention.requested_by, self.customer)
        self.assertEqual(intervention.preferred_date, self.tomorrow)
        self.assertLess(timezone.now() - intervention.requested_at, timedelta(minutes=1))
        self.assertIsNone(intervention.staff)
        self.assertIsNone(intervention.executed_on)
        # Il cliente vede la richiesta e non può farne un'altra per lo stesso guasto
        page = self.client.get(f"/sp/system/{self.system.id}/", follow=True)
        self.assertContains(page, "Richiesta inoltrata")
        self.assertNotContains(page, "Richiedi intervento")
        self.assertRedirects(self.client.get(url), f"/sp/system/{self.system.id}/", fetch_redirect_response=False)

    def test_day_must_be_between_tomorrow_and_two_months(self):
        self.client.force_login(self.customer)
        url = f"/sp/system/{self.system.id}/intervention/"
        today = timezone.localdate()

        for day in (today, today + timedelta(days=61)):
            response = self.client.post(url, {"preferred_date": day.isoformat()})
            self.assertContains(response, "Scegli un giorno tra il")
        self.assertFalse(Intervention.objects.exists())

    def test_only_users_of_the_community_can_request(self):
        url = f"/sp/system/{self.system.id}/intervention/"
        self.client.force_login(create_customer(create_community("Other"), username="anna"))
        self.assertEqual(self.client.post(url, {"preferred_date": self.tomorrow.isoformat()}).status_code, 403)
        self.client.force_login(self.technician)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertFalse(Intervention.objects.exists())

    def test_a_new_request_is_possible_after_an_intervention_if_a_later_check_finds_the_fault_again(self):
        self.request_intervention(status=Status.DONE, staff=self.technician, code="INV",
                                  executed_on=timezone.localdate() - timedelta(days=5))
        self.system.refresh_from_db()
        self.assertTrue(self.system.can_request_intervention)

        Intervention.objects.update(executed_on=timezone.localdate())
        self.assertFalse(self.system.can_request_intervention)


class StaffDashboardTests(InterventionTestCase):
    def test_only_staff_can_access(self):
        for url in ("/sp/interventions/", f"/sp/interventions/{self.request_intervention().pk}/"):
            self.client.logout()
            self.assertRedirects(self.client.get(url), f"/sp/login/?next={url}", fetch_redirect_response=False)
            self.client.force_login(self.customer)
            self.assertEqual(self.client.get(url).status_code, 403)

    def test_tabs_show_pending_accepted_and_done_interventions(self):
        pending = self.request_intervention()
        accepted = self.request_intervention(status=Status.ACCEPTED, staff=self.technician, code="INV")
        done = self.request_intervention(status=Status.DONE, staff=self.other_technician, code="CLN",
                                         executed_on=timezone.localdate())
        self.client.force_login(self.technician)

        response = self.client.get("/sp/interventions/")
        self.assertEqual(list(response.context["page"]), [pending])
        self.assertEqual(response.context["pending_count"], 1)
        self.assertEqual(response.context["my_open_count"], 1)
        self.assertContains(response, 'title="Richieste da accettare">1</span>')  # contatore nel menu

        self.assertEqual(list(self.client.get("/sp/interventions/?tab=accepted").context["page"]), [accepted])
        self.assertEqual(list(self.client.get("/sp/interventions/?tab=done").context["page"]), [done])
        self.assertEqual(list(self.client.get("/sp/interventions/?tab=done&mine=1").context["page"]), [])


class StaffWorkflowTests(InterventionTestCase):
    def setUp(self):
        super().setUp()
        self.intervention = self.request_intervention()
        self.url = f"/sp/interventions/{self.intervention.pk}/"

    def test_staff_accepts_the_request_and_takes_charge_of_it(self):
        self.client.force_login(self.technician)
        self.assertContains(self.client.get(self.url), "Accetta richiesta")

        self.assertContains(self.client.post(self.url, {}), "Campo obbligatorio")
        self.client.post(self.url, {"code": "INV"})

        self.intervention.refresh_from_db()
        self.assertEqual(self.intervention.status, Status.ACCEPTED)
        self.assertEqual(self.intervention.staff, self.technician)
        self.assertEqual(self.intervention.code, "INV")
        # Il cliente vede che la richiesta è stata accettata
        self.client.force_login(self.customer)
        self.assertContains(self.client.get(f"/sp/system/{self.system.id}/", follow=True), "Richiesta accettata")

    def test_only_the_staff_member_in_charge_records_the_executed_intervention(self):
        self.intervention.status, self.intervention.staff, self.intervention.code = Status.ACCEPTED, self.technician, "INV"
        self.intervention.save()
        today = timezone.localdate()
        data = {"code": "INV", "executed_on": today.isoformat(), "notes": "Sostituito l'inverter", "cost": "850.00"}

        self.client.force_login(self.other_technician)
        self.assertNotContains(self.client.get(self.url), "Registra intervento eseguito")
        self.assertEqual(self.client.post(self.url, data).status_code, 403)

        self.client.force_login(self.technician)
        future = {**data, "executed_on": (today + timedelta(days=1)).isoformat()}
        self.assertContains(self.client.post(self.url, future), "data futura")
        self.client.post(self.url, data)

        self.intervention.refresh_from_db()
        self.assertEqual(self.intervention.status, Status.DONE)
        self.assertEqual(self.intervention.executed_on, today)
        self.assertEqual(str(self.intervention.cost), "850.00")
        self.assertEqual(self.client.post(self.url, data).status_code, 403)  # già eseguito

    def test_report_for_staff_and_community_users(self):
        report = f"/sp/interventions/{self.intervention.pk}/report/"
        self.client.force_login(self.technician)
        self.assertContains(self.client.get(report), f"Rapporto di intervento n. {self.intervention.number}")
        self.client.force_login(self.customer)
        self.assertEqual(self.client.get(report).status_code, 200)
        self.client.force_login(create_customer(create_community("Other"), username="anna"))
        self.assertEqual(self.client.get(report).status_code, 403)


class InterventionModelTests(InterventionTestCase):
    def test_accepted_interventions_need_a_staff_member_and_a_type(self):
        intervention = self.request_intervention(status=Status.ACCEPTED)
        with self.assertRaises(ValidationError) as error:
            intervention.full_clean()
        self.assertEqual(set(error.exception.message_dict), {"staff", "code"})

        intervention.staff = Customer.objects.get().user  # un cliente non è staff
        intervention.code = "INV"
        with self.assertRaisesMessage(ValidationError, "membri dello staff"):
            intervention.full_clean()

    def test_executed_interventions_need_the_execution_date(self):
        intervention = self.request_intervention(status=Status.DONE, staff=self.technician, code="INV")
        with self.assertRaisesMessage(ValidationError, "eseguito"):
            intervention.full_clean()
