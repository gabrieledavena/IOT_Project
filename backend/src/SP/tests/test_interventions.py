from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from SP.installations import force_status

from SP.forms import OWNER_WITHOUT_CONTACTS
from SP.models import Customer, Intervention, PhotovoltaicSystem
from SP.tests.helpers import create_community, create_consultant, create_customer, create_system, create_technician

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
        self.technician = create_technician("tecnico", first_name="Paolo", last_name="Neri")
        self.other_technician = create_technician("tecnico2", first_name="Sara", last_name="Gialli")
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

        # Anche quando il guasto è confermato dopo il lavaggio
        self.system.status = PhotovoltaicSystem.Status.BROKEN
        self.system.save()
        self.assertContains(self.client.get(url, follow=True), "Richiedi intervento")

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

    def test_a_new_request_is_possible_when_the_fault_comes_back_after_an_intervention(self):
        intervention = self.request_intervention(status=Status.ACCEPTED, staff=self.technician, code="INV")
        self.client.force_login(self.technician)
        self.client.post(f"/sp/interventions/{intervention.pk}/", {
            "code": "INV", "executed_on": timezone.localdate().isoformat(), "notes": "Sostituito l'inverter",
        })
        self.system.refresh_from_db()
        self.assertFalse(self.system.can_request_intervention)  # è tornato OK

        # Il guasto torna lo stesso giorno (qui impostato a mano, come dalla pagina dell'installazione)
        force_status(self.system, PhotovoltaicSystem.Status.BROKEN)

        self.assertTrue(self.system.can_request_intervention)
        self.client.force_login(self.customer)
        self.assertContains(self.client.get(f"/sp/system/{self.system.id}/", follow=True), "Richiedi intervento")
        self.client.post(f"/sp/system/{self.system.id}/intervention/", {"preferred_date": self.tomorrow.isoformat()})
        self.assertEqual(Intervention.objects.filter(status=Status.REQUESTED).count(), 1)

    def test_a_fault_still_there_after_an_executed_intervention_can_have_a_new_request(self):
        # Intervento registrato prima che riportasse l'impianto a OK: il guasto c'è ancora
        self.request_intervention(status=Status.DONE, staff=self.technician, code="INV",
                                  executed_on=timezone.localdate())

        self.assertTrue(self.system.can_request_intervention)

    def test_an_open_request_waits_for_the_technician(self):
        for status in (Status.REQUESTED, Status.ACCEPTED, Status.PROPOSED):
            with self.subTest(status=status):
                Intervention.objects.all().delete()
                self.request_intervention(status=status, staff=self.technician, code="INV",
                                          proposed_date=self.tomorrow + timedelta(days=1))
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

    def test_the_executed_intervention_brings_the_system_back_to_ok(self):
        self.intervention.status, self.intervention.staff, self.intervention.code = Status.ACCEPTED, self.technician, "INV"
        self.intervention.save()
        self.client.force_login(self.technician)

        with mock.patch("SP.interventions.mqtt.publish_status") as publish, \
                self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(self.url, {"code": "INV", "executed_on": timezone.localdate().isoformat(),
                                                   "notes": "Sostituito l'inverter"})

        self.system.refresh_from_db()
        self.assertEqual((self.system.status, self.system.previous_status), (PhotovoltaicSystem.Status.OK, FAULT))
        # Vale come un controllo: il prossimo automatico arriva dopo due giorni, sulla produzione riparata
        self.assertLess(timezone.now() - self.system.last_check, timedelta(minutes=1))
        self.assertEqual(self.system.status_changed_at, self.system.last_check)
        publish.assert_called_once_with([self.system])
        self.assertContains(self.client.get(response.url), "l&#x27;impianto è tornato OK")
        self.client.force_login(self.customer)
        page = self.client.get(f"/sp/system/{self.system.id}/", follow=True)
        self.assertContains(page, "Intervento eseguito")
        self.assertNotContains(page, "Richiedi intervento")

    def test_also_when_the_execution_is_recorded_in_the_admin(self):
        admin_user = User.objects.create_superuser("admin", password="test-password")
        self.client.force_login(admin_user)
        url = f"/admin/SP/intervention/{self.intervention.pk}/change/"
        data = {"system": self.system.id, "status": "DON", "requested_by": self.customer.id,
                "requested_at_0": "2026-11-17", "requested_at_1": "10:00:00",
                "preferred_date": self.tomorrow.isoformat(), "scheduled_date": self.tomorrow.isoformat(),
                "customer_notes": "", "staff": self.technician.id, "code": "CLN",
                "executed_on": timezone.localdate().isoformat(), "notes": "Pulizia", "cost": ""}

        self.assertEqual(self.client.post(url, data).status_code, 302)

        self.system.refresh_from_db()
        self.assertEqual(self.system.status, PhotovoltaicSystem.Status.OK)
        # Altre modifiche a un intervento già eseguito non cambiano lo stato
        force_status(self.system, PhotovoltaicSystem.Status.BROKEN)
        self.client.post(url, {**data, "notes": "Pulizia dei pannelli"})
        self.system.refresh_from_db()
        self.assertEqual(self.system.status, PhotovoltaicSystem.Status.BROKEN)

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

        intervention.code = "INV"
        # Né un cliente né un consulente: solo i tecnici
        intervention.staff = create_consultant()
        with self.assertRaisesMessage(ValidationError, "Gli interventi sono gestiti dai tecnici"):
            intervention.full_clean()
        intervention.staff = Customer.objects.get().user
        with self.assertRaisesMessage(ValidationError, "Gli interventi sono gestiti dai tecnici"):
            intervention.full_clean()

    def test_executed_interventions_need_the_execution_date(self):
        intervention = self.request_intervention(status=Status.DONE, staff=self.technician, code="INV")
        with self.assertRaisesMessage(ValidationError, "eseguito"):
            intervention.full_clean()

    def test_the_day_of_the_intervention_is_the_requested_one(self):
        self.assertEqual(self.request_intervention().scheduled_date, self.tomorrow)

    def test_a_proposal_needs_the_proposed_day(self):
        intervention = self.request_intervention(status=Status.PROPOSED, staff=self.technician, code="INV")
        with self.assertRaisesMessage(ValidationError, "Indica il giorno proposto al titolare"):
            intervention.full_clean()


class OwnerContactsTests(InterventionTestCase):
    """Il tecnico contatta il titolare dalla sua pagina, per accordarsi sul giorno dell'intervento."""

    def setUp(self):
        super().setUp()
        self.intervention = self.request_intervention()
        self.url = f"/sp/interventions/{self.intervention.pk}/"
        self.client.force_login(self.technician)

    def test_the_technician_calls_or_writes_to_the_owner(self):
        Customer.objects.filter(user=self.customer).update(phone="+39 333 1234567")

        response = self.client.get(self.url)

        self.assertContains(response, "Contatta il titolare")
        self.assertContains(response, 'href="tel:+393331234567"')
        self.assertContains(response, 'href="mailto:mario@example.com?subject=Intervento%20INT-')
        self.assertContains(response, "Chiama +39 333 1234567")

    def test_also_the_member_who_made_the_request(self):
        member = create_customer(self.community, username="luigi", phone="+39 347 7654321")
        self.intervention.requested_by = member
        self.intervention.save()

        response = self.client.get(self.url)

        self.assertContains(response, 'href="tel:+393331234567"')  # il titolare
        self.assertContains(response, 'href="tel:+393477654321"')  # chi ha fatto la richiesta
        self.assertContains(response, "luigi@example.com")

    def test_an_owner_from_before_without_contacts(self):
        Customer.objects.filter(user=self.customer).update(phone="")

        response = self.client.get(self.url)

        self.assertContains(response, "Il titolare non ha ancora indicato telefono ed email")
        self.assertContains(response, "mailto:mario@example.com")


class AcceptWithAnotherDayTests(InterventionTestCase):
    """Accettando, il tecnico indica il giorno: quello richiesto, o uno concordato con il titolare."""

    def setUp(self):
        super().setUp()
        self.intervention = self.request_intervention()
        self.url = f"/sp/interventions/{self.intervention.pk}/"
        self.client.force_login(self.technician)

    def test_the_form_starts_from_the_requested_day(self):
        form = self.client.get(self.url).context["form"]

        self.assertEqual(form["day"].value(), self.tomorrow)

    def test_on_the_requested_day_the_request_is_accepted(self):
        self.client.post(self.url, {"code": "INV", "day": self.tomorrow.isoformat()})

        self.intervention.refresh_from_db()
        self.assertEqual((self.intervention.status, self.intervention.scheduled_date), (Status.ACCEPTED, self.tomorrow))
        self.assertIsNone(self.intervention.proposed_date)

    def test_another_day_waits_for_the_owner(self):
        day = self.tomorrow + timedelta(days=6)

        response = self.client.post(self.url, {"code": "INV", "day": day.isoformat()})

        self.assertRedirects(response, self.url, fetch_redirect_response=False)
        self.intervention.refresh_from_db()
        self.assertEqual((self.intervention.status, self.intervention.staff, self.intervention.code),
                         (Status.PROPOSED, self.technician, "INV"))
        self.assertEqual((self.intervention.proposed_date, self.intervention.scheduled_date), (day, self.tomorrow))
        self.assertLess(timezone.now() - self.intervention.proposed_at, timedelta(minutes=1))
        page = self.client.get(self.url)
        self.assertContains(page, "In attesa del titolare")
        self.assertContains(page, "Il titolare deve approvare il nuovo giorno")
        # Finché il titolare non risponde non si registra l'esecuzione
        self.assertIsNone(page.context["form"])
        self.assertIsNotNone(page.context["proposal_form"])

    def test_the_new_day_is_between_tomorrow_and_two_months(self):
        today = timezone.localdate()
        for day in (today, today + timedelta(days=61)):
            with self.subTest(day=day):
                self.assertContains(self.client.post(self.url, {"code": "INV", "day": day.isoformat()}),
                                    "Proponi un giorno tra il")
        self.intervention.refresh_from_db()
        self.assertEqual(self.intervention.status, Status.REQUESTED)


class DateProposalTests(InterventionTestCase):
    """Il giorno cambia dopo l'accordo: il tecnico lo invia al titolare, che lo approva o lo rifiuta dal sito."""

    def setUp(self):
        super().setUp()
        self.intervention = self.request_intervention(status=Status.ACCEPTED, staff=self.technician, code="INV")
        self.url = f"/sp/interventions/{self.intervention.pk}/"
        self.proposal_url = f"{self.url}proposal/"
        self.new_day = self.tomorrow + timedelta(days=3)

    def propose(self, day=None, user=None):
        self.client.force_login(user or self.technician)
        return self.client.post(self.url, {"action": "propose", "day": (day or self.new_day).isoformat()})

    def answer(self, answer, day=None, user=None):
        self.client.force_login(user or self.customer)
        return self.client.post(self.proposal_url, {"answer": answer, "day": (day or self.new_day).isoformat()})

    def test_the_technician_in_charge_sends_a_new_day(self):
        self.client.force_login(self.technician)
        self.assertContains(self.client.get(self.url), "Invia il nuovo giorno al titolare")

        response = self.propose()

        self.assertRedirects(response, self.url, fetch_redirect_response=False)
        self.intervention.refresh_from_db()
        self.assertEqual((self.intervention.status, self.intervention.proposed_date), (Status.PROPOSED, self.new_day))
        self.assertEqual(self.intervention.scheduled_date, self.tomorrow)
        # Può ancora cambiarlo, prima che il titolare risponda
        later = self.new_day + timedelta(days=1)
        self.propose(later)
        self.intervention.refresh_from_db()
        self.assertEqual(self.intervention.proposed_date, later)

    def test_only_the_technician_in_charge(self):
        self.client.force_login(self.other_technician)
        self.assertNotContains(self.client.get(self.url), "Invia il nuovo giorno al titolare")

        self.assertEqual(self.propose(user=self.other_technician).status_code, 403)
        self.intervention.refresh_from_db()
        self.assertEqual(self.intervention.status, Status.ACCEPTED)

    def test_the_new_day_must_change_the_day(self):
        self.assertContains(self.propose(self.tomorrow), "È già il giorno dell&#x27;intervento.")
        self.propose()
        self.assertContains(self.propose(), "Hai già proposto questo giorno")
        self.assertContains(self.propose(timezone.localdate()), "Proponi un giorno tra il")

    def test_the_owner_finds_the_proposal_on_the_site(self):
        self.propose()
        self.client.force_login(self.customer)

        # In cima a tutte le sue pagine, e nella pagina dell'impianto con il bottone per rispondere
        for url in ("/", f"/sp/system/{self.system.id}/"):
            with self.subTest(url=url):
                page = self.client.get(url, follow=True)
                self.assertContains(page, f"Nuova data per l'intervento su {self.system.name}")
                self.assertContains(page, f'href="{self.proposal_url}"')
        page = self.client.get(self.proposal_url)
        self.assertContains(page, "Approva la nuova data")
        self.assertContains(page, "Rifiuta e annulla la richiesta")
        self.assertContains(page, f'value="{self.new_day.isoformat()}"')

    def test_other_users_of_the_community_see_it_but_do_not_answer(self):
        self.propose()
        member = create_customer(self.community, username="luigi")
        self.client.force_login(member)

        page = self.client.get(f"/sp/system/{self.system.id}/", follow=True)
        self.assertContains(page, "in attesa della risposta del titolare")
        self.assertNotContains(page, "Nuova data per l'intervento su")
        self.assertEqual(self.client.get(self.proposal_url).status_code, 403)
        self.assertEqual(self.answer("approve", user=member).status_code, 403)
        for user in (create_customer(create_community("Other"), username="anna"), self.technician):
            with self.subTest(user=user.username):
                self.assertEqual(self.answer("approve", user=user).status_code, 403)
        self.intervention.refresh_from_db()
        self.assertEqual(self.intervention.status, Status.PROPOSED)

    def test_approved_the_intervention_stays_with_the_new_day(self):
        self.propose()

        response = self.answer("approve")

        self.assertRedirects(response, f"/sp/system/{self.system.id}/", fetch_redirect_response=False)
        self.intervention.refresh_from_db()
        self.assertEqual((self.intervention.status, self.intervention.scheduled_date), (Status.ACCEPTED, self.new_day))
        self.assertEqual((self.intervention.staff, self.intervention.preferred_date), (self.technician, self.tomorrow))
        self.assertIsNone(self.intervention.proposed_date)
        self.assertIsNotNone(self.intervention.answered_at)
        self.assertNotContains(self.client.get("/"), "Nuova data per l'intervento su")
        # Il tecnico ritrova l'intervento in corso, con il nuovo giorno, e può registrarne l'esecuzione
        self.client.force_login(self.technician)
        page = self.client.get(self.url)
        self.assertContains(page, "Registra intervento eseguito")
        self.assertContains(page, "(approvato dal titolare)")

    def test_rejected_the_request_is_cancelled_and_a_new_one_can_be_opened(self):
        self.propose()

        self.answer("reject")

        self.intervention.refresh_from_db()
        self.assertEqual(self.intervention.status, Status.CANCELLED)
        self.assertEqual(self.intervention.proposed_date, self.new_day)
        self.system.refresh_from_db()
        self.assertTrue(self.system.can_request_intervention)
        page = self.client.get(f"/sp/system/{self.system.id}/", follow=True)
        self.assertContains(page, "Richiesta annullata")
        self.assertContains(page, "Richiedi intervento")
        self.client.post(f"/sp/system/{self.system.id}/intervention/", {"preferred_date": self.tomorrow.isoformat()})
        self.assertEqual(Intervention.objects.filter(status=Status.REQUESTED).count(), 1)
        # Per il tecnico non è più in corso: l'intervento non si esegue
        self.client.force_login(self.technician)
        self.assertEqual(self.client.post(self.url, {"code": "INV", "executed_on": timezone.localdate().isoformat(),
                                                     "notes": "x"}).status_code, 403)

    def test_an_answer_counts_only_for_the_day_still_proposed(self):
        self.propose()
        self.propose(self.new_day + timedelta(days=1))

        # Il titolare risponde dalla pagina aperta prima della seconda proposta
        response = self.answer("approve")

        self.assertRedirects(response, self.proposal_url, fetch_redirect_response=False)
        self.intervention.refresh_from_db()
        self.assertEqual(self.intervention.status, Status.PROPOSED)
        # E una volta risposto la risposta non cambia
        self.answer("reject", self.new_day + timedelta(days=1))
        self.answer("approve", self.new_day + timedelta(days=1))
        self.intervention.refresh_from_db()
        self.assertEqual(self.intervention.status, Status.CANCELLED)
        self.assertContains(self.client.get(self.proposal_url), "Hai rifiutato il giorno proposto")

    def test_tabs_and_counters_of_the_staff(self):
        self.propose()
        cancelled = self.request_intervention(status=Status.CANCELLED, staff=self.technician, code="CLN",
                                              proposed_date=self.new_day)
        self.client.force_login(self.technician)

        response = self.client.get("/sp/interventions/?tab=accepted")
        self.assertEqual(list(response.context["page"]), [self.intervention])
        self.assertEqual((response.context["my_open_count"], response.context["proposed_count"]), (1, 1))
        self.assertContains(response, "Proposto il")
        self.assertEqual(list(self.client.get("/sp/interventions/?tab=cancelled").context["page"]), [cancelled])
        tabs = {key: count for key, _, count in response.context["tabs"]}
        self.assertEqual(tabs, {"pending": 0, "accepted": 1, "done": 0, "cancelled": 1})


class OwnerWithoutContactsTests(InterventionTestCase):
    """Un titolare di prima che telefono ed email fossero obbligatori li aggiunge prima di chiedere un intervento."""

    def setUp(self):
        super().setUp()
        Customer.objects.filter(user=self.customer).update(phone="")
        self.url = f"/sp/system/{self.system.id}/intervention/"

    def test_the_owner_adds_them_from_the_profile_first(self):
        self.client.force_login(self.customer)
        self.assertContains(self.client.get("/"), "Aggiungi telefono ed email")

        response = self.client.post(self.url, {"preferred_date": self.tomorrow.isoformat()})

        self.assertRedirects(response, "/sp/profile/", fetch_redirect_response=False)
        self.assertContains(self.client.get("/sp/profile/"), OWNER_WITHOUT_CONTACTS)
        self.assertFalse(Intervention.objects.exists())
        self.client.post("/sp/profile/", {"email": "mario@example.com", "phone": "+39 333 1234567"})
        self.client.post(self.url, {"preferred_date": self.tomorrow.isoformat()})
        self.assertTrue(Intervention.objects.exists())
        self.assertNotContains(self.client.get("/"), "Aggiungi telefono ed email")

    def test_the_other_users_wait_for_the_owner(self):
        self.client.force_login(create_customer(self.community, username="luigi"))

        response = self.client.post(self.url, {"preferred_date": self.tomorrow.isoformat()})

        self.assertRedirects(response, f"/sp/system/{self.system.id}/", fetch_redirect_response=False)
        self.assertFalse(Intervention.objects.exists())
        self.assertNotContains(self.client.get("/"), "Aggiungi telefono ed email")
