"""Utenti appena registrati, in attesa di una community, e la loro associazione (SP/customers.py)."""
from django.contrib.auth.models import User
from django.test import TestCase

from SP.access import NOT_A_CUSTOMER
from SP.models import Customer
from SP.tests.helpers import (
    create_community, create_consultant, create_customer, create_pending_customer, create_system, create_technician,
)

PASSWORD = "Una-password-sicura-42"


def registration(**fields):
    return {"username": "nuovo", "password1": PASSWORD, "password2": PASSWORD, "name": "Anna", "surname": "Bianchi",
            **fields}


class RegistrationTests(TestCase):
    def test_registration_saves_email_and_phone_without_a_community(self):
        community = create_community()

        # Anche se la richiesta indica una community, non si sceglie registrandosi
        response = self.client.post("/sp/register/", registration(
            email="anna@example.com", phone="+39 333  1234567", community=community.id,
        ))

        self.assertRedirects(response, "/", fetch_redirect_response=False)
        customer = Customer.objects.get(user__username="nuovo")
        self.assertIsNone(customer.community)
        self.assertFalse(customer.is_owner)
        self.assertEqual(customer.user.email, "anna@example.com")
        self.assertEqual(customer.phone, "+39 333 1234567")
        self.assertIsNone(Customer.community_of(customer.user))

    def test_email_and_phone_are_optional(self):
        self.client.post("/sp/register/", registration())

        customer = Customer.objects.get(user__username="nuovo")
        self.assertEqual((customer.user.email, customer.phone), ("", ""))

    def test_invalid_email_or_phone_are_rejected(self):
        response = self.client.post("/sp/register/", registration(email="non-una-email", phone="chiamami"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.context["form"].errors), {"email", "phone"})
        self.assertFalse(User.objects.filter(username="nuovo").exists())

    def test_the_form_has_no_community_field(self):
        response = self.client.get("/sp/register/")

        self.assertNotIn("community", response.context["form"].fields)
        self.assertContains(response, 'name="email"')
        self.assertContains(response, 'name="phone"')


class AwaitingCommunityTests(TestCase):
    def setUp(self):
        self.customer = create_pending_customer(email="anna@example.com", phone="+39 333 1234567")
        self.client.force_login(self.customer.user)

    def test_home_says_the_registration_succeeded_and_waits_for_a_technician(self):
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "SP/awaiting_community.html")
        self.assertContains(response, "Registrazione completata")
        self.assertContains(response, "In attesa dell'assegnazione a una community")
        self.assertContains(response, "anna@example.com")
        # Nel menu nessuna pagina degli impianti, che non ha ancora
        self.assertNotContains(response, 'href="/sp/community/"')
        self.assertNotContains(response, 'href="/forecast/"')

    def test_pages_of_the_systems_are_forbidden(self):
        for url in ("/sp/community/", "/sp/system/", "/forecast/"):
            with self.subTest(url=url):
                self.assertContains(self.client.get(url), NOT_A_CUSTOMER, status_code=403)

    def test_once_assigned_the_home_is_the_dashboard_of_the_community(self):
        community = create_community("Famiglia Bianchi")
        create_system(community, "Tetto")
        self.customer.community = community
        self.customer.save()

        response = self.client.get("/")

        self.assertTemplateUsed(response, "SP/user_dashboard.html")
        self.assertContains(response, "Tetto")


class CommunityAssignmentTests(TestCase):
    URL = "/sp/customers/"

    def setUp(self):
        self.community = create_community("Famiglia Bianchi")
        self.anna = create_pending_customer(email="anna@example.com", phone="+39 333 1234567")
        create_customer(create_community("Rossi Trasporti"))  # già associato: non è in elenco
        self.client.force_login(create_technician())

    def assign(self, customer, community_id, **extra):
        return self.client.post(f"{self.URL}{customer.id}/assign/", {"community": community_id, **extra})

    def test_lists_only_the_users_without_a_community_with_their_contacts(self):
        response = self.client.get(self.URL)

        self.assertEqual(list(response.context["page"]), [self.anna])
        self.assertEqual(response.context["total"], 1)
        self.assertContains(response, "anna@example.com")
        self.assertContains(response, "+39 333 1234567")
        self.assertContains(response, f'<option value="{self.community.id}">')
        # Il contatore nel menu dei tecnici
        self.assertContains(response, 'title="Utenti da associare a una community">1</span>')

    def test_communities_created_later_are_offered_too(self):
        self.client.get(self.URL)
        later = create_community("Famiglia Verdi")

        response = self.client.get(self.URL)

        self.assertContains(response, f'<option value="{later.id}">')

    def test_search_by_name_email_or_phone(self):
        create_pending_customer("carlo", name="Carlo")

        for query, expected in (("anna@", [self.anna]), ("333", [self.anna]), ("carlo", ["Carlo"]), ("zzz", [])):
            with self.subTest(query=query):
                page = self.client.get(self.URL, {"q": query}).context["page"]
                self.assertEqual([c if c == self.anna else c.name for c in page], expected)

    def test_assigning_a_community_makes_the_first_user_its_owner(self):
        response = self.assign(self.anna, self.community.id, q="anna")

        self.assertRedirects(response, f"{self.URL}?q=anna", fetch_redirect_response=False)
        self.anna.refresh_from_db()
        self.assertEqual(self.anna.community, self.community)
        self.assertTrue(self.anna.is_owner)
        self.assertEqual(list(self.client.get(self.URL).context["page"]), [])

    def test_who_becomes_the_owner_without_phone_and_email_enters_them_first(self):
        carlo = create_pending_customer("carlo", name="Carlo")

        response = self.assign(carlo, self.community.id, q="carlo")

        self.assertTemplateUsed(response, "SP/owner_contacts.html")
        self.assertContains(response, "è il primo utente di Famiglia Bianchi e ne diventa il titolare")
        self.assertNotContains(response, "Obbligatorio per il titolare")
        carlo.refresh_from_db()
        self.assertIsNone(carlo.community)

        contacts = {"owner-customer": carlo.id, "owner-email": "carlo@example.com", "owner-phone": ""}
        self.assertContains(self.assign(carlo, self.community.id, **contacts), "Obbligatorio per il titolare")
        response = self.assign(carlo, self.community.id, q="carlo", **{**contacts, "owner-phone": "+39 347 7654321"})

        self.assertRedirects(response, f"{self.URL}?q=carlo", fetch_redirect_response=False)
        carlo.refresh_from_db()
        self.assertTrue(carlo.is_owner)
        self.assertEqual((carlo.user.email, carlo.phone), ("carlo@example.com", "+39 347 7654321"))

    def test_the_other_users_of_a_community_need_no_contacts(self):
        self.assign(self.anna, self.community.id)
        carlo = create_pending_customer("carlo", name="Carlo")

        self.assign(carlo, self.community.id)

        carlo.refresh_from_db()
        self.assertEqual(carlo.community, self.community)
        self.assertFalse(carlo.is_owner)

    def test_a_user_already_in_a_community_is_not_moved(self):
        self.assign(self.anna, self.community.id)
        other = create_community("Other")

        response = self.assign(self.anna, other.id)

        self.anna.refresh_from_db()
        self.assertEqual(self.anna.community, self.community)
        self.assertContains(self.client.get(response.url), "è già nella community Famiglia Bianchi")

    def test_a_community_must_be_chosen(self):
        response = self.assign(self.anna, "")

        self.anna.refresh_from_db()
        self.assertIsNone(self.anna.community)
        self.assertContains(self.client.get(response.url), "Scegli la community")

    def test_only_technicians_assign_communities(self):
        for user in (self.anna.user, create_consultant(), User.objects.get(username="mario")):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                self.assertEqual(self.client.get(self.URL).status_code, 403)
                self.assertEqual(self.assign(self.anna, self.community.id).status_code, 403)
        self.anna.refresh_from_db()
        self.assertIsNone(self.anna.community)

        self.client.logout()
        self.assertRedirects(self.client.get(self.URL), f"/sp/login/?next={self.URL}", fetch_redirect_response=False)


class OwnerContactsInTheAdminTests(TestCase):
    """Anche nell'admin il titolare di una community ha telefono ed email."""

    def setUp(self):
        self.client.force_login(User.objects.create_superuser("admin", password="test-password"))
        self.community = create_community("Famiglia Bianchi")
        self.anna = create_pending_customer(email="", phone="")

    def save_customer(self, customer, **fields):
        data = {"user": customer.user_id, "name": customer.name, "surname": customer.surname,
                "email": customer.user.email, "phone": customer.phone, "community": self.community.id,
                "telegram_chat_id": "", **fields}
        return self.client.post(f"/admin/SP/customer/{customer.id}/change/", data)

    def test_who_becomes_the_owner_needs_phone_and_email(self):
        response = self.save_customer(self.anna)

        self.assertContains(response, "il titolare deve averli tutti e due")
        self.anna.refresh_from_db()
        self.assertIsNone(self.anna.community)

        response = self.save_customer(self.anna, email="anna@example.com", phone="+39 333 1234567")

        self.assertEqual(response.status_code, 302)
        self.anna.refresh_from_db()
        self.assertTrue(self.anna.is_owner)
        self.assertEqual((self.anna.user.email, self.anna.phone), ("anna@example.com", "+39 333 1234567"))

    def test_the_owner_keeps_them(self):
        owner = Customer.objects.get(user=create_customer(self.community))

        self.assertContains(self.save_customer(owner, phone=""), "il titolare deve averli tutti e due")
        response = self.client.post(f"/admin/auth/user/{owner.user_id}/change/", {
            "username": owner.user.username, "first_name": "", "last_name": "", "email": "", "is_active": "on",
            "date_joined_0": "2026-11-01", "date_joined_1": "10:00:00",
        })
        self.assertContains(response, "l&#x27;email è obbligatoria")
        owner.refresh_from_db()
        self.assertEqual((owner.phone, owner.user.email), ("+39 333 1234567", "mario@example.com"))

    def test_the_owner_chosen_in_the_admin_needs_them(self):
        create_customer(self.community)
        self.anna.community = self.community
        self.anna.save()

        response = self.client.post(f"/admin/SP/community/{self.community.id}/change/", {
            "name": "Famiglia Bianchi", "city": self.community.city_id, "owner": self.anna.id,
        })

        self.assertContains(response, "non ha telefono ed email")
        self.community.refresh_from_db()
        self.assertNotEqual(self.community.owner, self.anna)
