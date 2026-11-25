"""Il proprio profilo, dal nome nel menu (SP/views.py, ProfileView)."""
from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from SP.models import Customer, Intervention, StaffProfile
from SP.tests.helpers import (
    create_community, create_consultant, create_customer, create_pending_customer, create_system, create_technician,
)

URL = "/sp/profile/"


class ProfileTests(TestCase):
    def test_the_name_in_the_menu_leads_to_the_profile(self):
        self.client.force_login(create_technician())

        response = self.client.get("/")

        self.assertContains(response, f'href="{URL}"')
        self.assertContains(self.client.get(URL), "tecnico")

    def test_only_for_logged_in_users(self):
        self.assertRedirects(self.client.get(URL), f"/sp/login/?next={URL}", fetch_redirect_response=False)

    def test_an_owner_sees_account_and_community(self):
        community = create_community("Famiglia Bianchi")
        create_system(community, "Tetto", max_power=4.5)
        mario = create_customer(community)
        Customer.objects.filter(user=mario).update(phone="+39 333 1234567")
        mario.email = "mario@example.com"
        mario.save()
        create_customer(community, username="luigi")
        self.client.force_login(mario)

        response = self.client.get(URL)

        self.assertEqual(response.context["roles"], ["Titolare della community"])
        for text in ("Mario Rossi", "mario@example.com", "+39 333 1234567", "Famiglia Bianchi",
                     "1 impianto · 4,50 kWp installati", "(tu)", "Titolare"):
            self.assertContains(response, text)
        self.assertEqual(len(response.context["members"]), 2)
        self.assertNotContains(response, "I tuoi interventi")

    def test_a_user_waiting_for_a_community(self):
        anna = create_pending_customer()
        self.client.force_login(anna.user)

        response = self.client.get(URL)

        self.assertEqual(response.context["roles"], ["Cliente"])
        self.assertContains(response, "Non fai ancora parte di una community")

    def test_a_technician_sees_the_interventions_in_charge(self):
        technician = create_technician(first_name="Paolo", last_name="Neri")
        system = create_system(create_community())
        today = timezone.localdate()
        Intervention.objects.create(system=system, preferred_date=today, status=Intervention.Status.ACCEPTED,
                                    staff=technician, code="INV")
        Intervention.objects.create(system=system, preferred_date=today, status=Intervention.Status.DONE,
                                    staff=technician, code="CLN", executed_on=today)
        Intervention.objects.create(system=system, preferred_date=today)  # da accettare: di nessuno
        self.client.force_login(technician)

        response = self.client.get(URL)

        self.assertEqual(response.context["roles"], ["Tecnico"])
        self.assertEqual(response.context["interventions"], {"open": 1, "done": 1})
        self.assertContains(response, "Paolo Neri")
        self.assertContains(response, "?tab=accepted&amp;mine=1")
        self.assertNotContains(response, "La tua community")

    def test_consultants_and_admins(self):
        self.client.force_login(create_consultant())
        response = self.client.get(URL)
        self.assertEqual(response.context["roles"], ["Consulente"])
        self.assertContains(response, 'href="/roi/"')

        self.client.force_login(User.objects.create_superuser("admin", password="test-password"))
        response = self.client.get(URL)
        self.assertEqual(response.context["roles"], ["Amministratore"])
        self.assertContains(response, "I tuoi interventi")  # gli amministratori sono anche tecnici


class ContactTests(TestCase):
    def setUp(self):
        self.anna = create_pending_customer(email="anna@example.com", phone="+39 333 1234567")
        self.client.force_login(self.anna.user)

    def test_the_form_starts_from_the_current_contacts(self):
        form = self.client.get(URL).context["form"]

        self.assertEqual((form["email"].value(), form["phone"].value()), ("anna@example.com", "+39 333 1234567"))

    def test_a_customer_changes_email_and_phone(self):
        response = self.client.post(URL, {"email": " anna.bianchi@example.com ", "phone": "+39  347 7654321"})

        self.assertRedirects(response, URL, fetch_redirect_response=False)
        self.anna.refresh_from_db()
        self.anna.user.refresh_from_db()
        self.assertEqual((self.anna.user.email, self.anna.phone), ("anna.bianchi@example.com", "+39 347 7654321"))
        self.assertContains(self.client.get(URL), "Recapiti aggiornati.")

    def test_empty_fields_remove_the_contacts(self):
        self.client.post(URL, {"email": "", "phone": ""})

        self.anna.refresh_from_db()
        self.anna.user.refresh_from_db()
        self.assertEqual((self.anna.user.email, self.anna.phone), ("", ""))

    def test_invalid_contacts_are_rejected(self):
        response = self.client.post(URL, {"email": "non-una-email", "phone": "chiamami"})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(set(response.context["form"].errors), {"email", "phone"})
        self.anna.refresh_from_db()
        self.anna.user.refresh_from_db()
        self.assertEqual((self.anna.user.email, self.anna.phone), ("anna@example.com", "+39 333 1234567"))

    def test_customers_keep_the_phone_in_their_customer_data(self):
        self.client.post(URL, {"email": "anna@example.com", "phone": "+39 347 7654321"})

        self.assertFalse(StaffProfile.objects.exists())

    def test_staff_read_their_contacts_but_do_not_change_them(self):
        for staff in (create_technician(email="tecnico@example.com"), create_consultant(email="consulente@example.com")):
            with self.subTest(user=staff.username):
                StaffProfile.objects.create(user=staff, phone="+39 051 123456")
                self.client.force_login(staff)

                response = self.client.get(URL)

                self.assertIsNone(response.context["form"])
                self.assertNotContains(response, 'name="email"')
                self.assertNotContains(response, "Salva i recapiti")
                self.assertContains(response, staff.email)
                self.assertContains(response, "+39 051 123456")
                self.assertContains(response, "li aggiorna un amministratore")
                self.assertNotContains(response, "Modificali nell'admin")

                response = self.client.post(URL, {"email": "altra@example.com", "phone": "+39 333 0000000"})

                self.assertEqual(response.status_code, 403)
                staff.refresh_from_db()
                self.assertEqual((staff.email, staff.staff_profile.phone),
                                 (f"{staff.username}@example.com", "+39 051 123456"))

    def test_staff_without_contacts(self):
        self.client.force_login(create_technician())

        response = self.client.get(URL)

        self.assertContains(response, "Non indicata")
        self.assertContains(response, "Non indicato")

    def test_admins_change_their_contacts_in_the_admin(self):
        admin_user = User.objects.create_superuser("admin", password="test-password")
        self.client.force_login(admin_user)

        response = self.client.get(URL)

        self.assertIsNone(response.context["form"])
        self.assertContains(response, f'href="/admin/auth/user/{admin_user.id}/change/"')
        self.assertEqual(self.client.post(URL, {"email": "x@example.com"}).status_code, 403)

    def test_only_logged_in_users_change_contacts(self):
        self.client.logout()

        response = self.client.post(URL, {"email": "x@example.com"})

        self.assertRedirects(response, f"/sp/login/?next={URL}", fetch_redirect_response=False)


class OwnerContactTests(TestCase):
    """Il titolare della community deve avere telefono ed email: servono ai tecnici per contattarlo."""

    def setUp(self):
        self.community = create_community("Famiglia Bianchi")
        self.owner = create_customer(self.community)
        self.client.force_login(self.owner)

    def test_the_owner_cannot_remove_them(self):
        response = self.client.get(URL)
        self.assertTrue(response.context["form"].required)
        self.assertContains(response, "Come titolare della community sono obbligatori tutti e due.")

        for contacts in ({"email": "", "phone": "+39 333 1234567"}, {"email": "mario@example.com", "phone": ""}):
            with self.subTest(contacts=contacts):
                response = self.client.post(URL, contacts)
                self.assertEqual(response.status_code, 400)
                self.assertContains(response, "Obbligatorio per il titolare della community.", status_code=400)
        customer = Customer.objects.select_related("user").get(user=self.owner)
        self.assertEqual((customer.user.email, customer.phone), ("mario@example.com", "+39 333 1234567"))

    def test_the_other_users_can(self):
        luigi = create_customer(self.community, username="luigi")
        self.client.force_login(luigi)

        self.client.post(URL, {"email": "", "phone": ""})

        customer = Customer.objects.select_related("user").get(user=luigi)
        self.assertEqual((customer.user.email, customer.phone), ("", ""))

    def test_an_owner_from_before_without_them_is_asked_on_every_page(self):
        Customer.objects.filter(user=self.owner).update(phone="")

        self.assertContains(self.client.get("/"), "Aggiungi telefono ed email")
        # Non nel profilo, dove li aggiunge
        self.assertNotContains(self.client.get(URL), "Aggiungi telefono ed email")
        self.client.post(URL, {"email": "mario@example.com", "phone": "+39 333 1234567"})
        self.assertNotContains(self.client.get("/"), "Aggiungi telefono ed email")
