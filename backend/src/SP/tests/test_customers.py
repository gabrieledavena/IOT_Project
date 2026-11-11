from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase

from SP import mqtt
from SP.access import NOT_A_CUSTOMER
from SP.gateways import install_gateway
from SP.models import Community, Customer, Gateway
from SP.tests.helpers import (
    create_city, create_community, create_consultant, create_customer, create_system, create_technician,
)

PASSWORD = "Una-password-sicura-42"


def registration(**fields):
    return {"username": "nuovo", "password1": PASSWORD, "password2": PASSWORD, "name": "Anna", "surname": "Bianchi",
            **fields}


def create_pending_customer(username="anna", **fields):
    user = User.objects.create_user(username=username, password="test-password", email=fields.pop("email", ""))
    return Customer.objects.create(user=user, name=fields.pop("name", "Anna"), surname="Bianchi", **fields)


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


class NewCommunityTests(TestCase):
    URL = "/sp/customers/communities/new/"

    def setUp(self):
        self.modena = create_city("Modena", province="Modena", region="Emilia-Romagna")
        self.anna = create_pending_customer()
        self.client.force_login(create_technician())

    def create(self, url=URL, **fields):
        return self.client.post(url, {"name": "Famiglia Bianchi", "city": self.modena.id, **fields})

    def test_form_chooses_the_city_by_region_and_province(self):
        create_city("Senza coordinate", latitude=None, longitude=None)

        response = self.client.get(self.URL)

        for field in ("name", "region", "province", "city"):
            self.assertContains(response, f'name="{field}"')
        self.assertContains(response, '<script id="city-options" type="application/json">')
        # Solo i comuni con coordinate, che servono per meteo e previsioni
        self.assertEqual(list(response.context["form"].fields["city"].queryset), [self.modena])

    def test_creates_a_community_without_users(self):
        response = self.create(name="  Famiglia   Bianchi ", region="Emilia-Romagna", province="Modena")

        self.assertRedirects(response, "/sp/customers/", fetch_redirect_response=False)
        community = Community.objects.get()
        self.assertEqual((community.name, community.city, community.owner), ("Famiglia Bianchi", self.modena, None))
        self.anna.refresh_from_db()
        self.assertIsNone(self.anna.community)

    def test_creates_the_community_of_a_waiting_user_who_becomes_its_owner(self):
        response = self.client.get(f"{self.URL}?customer={self.anna.id}")
        self.assertContains(response, "Alla nuova community sarà associato <strong>Anna Bianchi</strong>")

        self.create(f"{self.URL}?customer={self.anna.id}")

        self.anna.refresh_from_db()
        self.assertEqual(self.anna.community.name, "Famiglia Bianchi")
        self.assertTrue(self.anna.is_owner)

    def test_a_user_already_in_a_community_is_not_moved(self):
        other = create_community("Rossi Trasporti", city=self.modena)
        self.anna.community = other
        self.anna.save()

        self.create(f"{self.URL}?customer={self.anna.id}")

        self.anna.refresh_from_db()
        self.assertEqual(self.anna.community, other)
        self.assertTrue(Community.objects.filter(name="Famiglia Bianchi", owner=None).exists())

    def test_name_must_be_new_and_city_valid(self):
        create_community("Famiglia Bianchi", city=self.modena)

        response = self.create(name="famiglia bianchi", city="")

        self.assertEqual(set(response.context["form"].errors), {"name", "city"})
        self.assertEqual(Community.objects.count(), 1)

    def test_city_must_belong_to_the_chosen_province(self):
        create_city("Bari", province="Bari", region="Puglia")

        response = self.create(province="Bari")

        self.assertIn("city", response.context["form"].errors)
        self.assertFalse(Community.objects.exists())

    def test_only_technicians_create_communities(self):
        for user in (self.anna.user, create_consultant()):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                self.assertEqual(self.create().status_code, 403)
        self.assertFalse(Community.objects.exists())


class EditCommunityTests(TestCase):
    def setUp(self):
        self.modena = create_city("Modena", province="Modena", region="Emilia-Romagna")
        self.bari = create_city("Bari", province="Bari", region="Puglia")
        self.community = create_community("Famiglia Bianchi", city=self.modena)
        self.mario = Customer.objects.get(user=create_customer(self.community))
        self.luigi = Customer.objects.get(user=create_customer(self.community, username="luigi"))
        self.url = f"/sp/customers/communities/{self.community.id}/"
        self.client.force_login(create_technician())

    def test_list_shows_every_community_with_owner_users_and_systems(self):
        create_system(self.community)
        create_community("Rossi Trasporti", city=self.bari)

        response = self.client.get("/sp/customers/communities/")

        rows = {c.name: (c.owner, c.users, c.systems) for c in response.context["page"]}
        self.assertEqual(rows, {"Famiglia Bianchi": (self.mario, 2, 1), "Rossi Trasporti": (None, 0, 0)})
        self.assertContains(response, f'href="{self.url}"')

    def test_form_starts_from_the_community_and_offers_its_users_as_owner(self):
        response = self.client.get(self.url)

        form = response.context["form"]
        self.assertEqual((form["name"].value(), form["city"].value(), form["owner"].value()),
                         ("Famiglia Bianchi", self.modena.id, self.mario.id))
        self.assertEqual(set(form.fields["owner"].queryset), {self.mario, self.luigi})
        self.assertContains(response, "luigi")

    def test_changes_name_city_and_owner(self):
        response = self.client.post(self.url, {"name": "Famiglia Rossi", "city": self.bari.id, "owner": self.luigi.id})

        self.assertRedirects(response, "/sp/customers/communities/", fetch_redirect_response=False)
        self.community.refresh_from_db()
        self.assertEqual((self.community.name, self.community.city, self.community.owner),
                         ("Famiglia Rossi", self.bari, self.luigi))

    def test_the_owner_is_one_of_its_users_and_the_name_is_unique(self):
        create_community("Rossi Trasporti", city=self.bari)
        outsider = Customer.objects.get(user=create_customer(create_community("Other"), username="anna"))

        response = self.client.post(self.url, {"name": "rossi trasporti", "city": self.modena.id, "owner": outsider.id})

        self.assertEqual(set(response.context["form"].errors), {"name", "owner"})
        # Il proprio nome resta valido
        self.client.post(self.url, {"name": "Famiglia Bianchi", "city": self.bari.id, "owner": self.mario.id})
        self.community.refresh_from_db()
        self.assertEqual(self.community.city, self.bari)

    def test_a_community_without_users_has_no_owner_to_choose(self):
        empty = create_community("Vuota", city=self.modena)

        response = self.client.post(f"/sp/customers/communities/{empty.id}/", {"name": "Vuota 2", "city": self.bari.id})

        self.assertEqual(response.status_code, 302)
        empty.refresh_from_db()
        self.assertEqual((empty.name, empty.owner), ("Vuota 2", None))

    def test_only_technicians_edit_communities(self):
        for user in (self.mario.user, create_consultant()):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                self.assertEqual(self.client.get("/sp/customers/communities/").status_code, 403)
                self.assertEqual(self.client.post(self.url, {"name": "X", "city": self.bari.id}).status_code, 403)
        self.community.refresh_from_db()
        self.assertEqual(self.community.name, "Famiglia Bianchi")


class DeleteCommunityTests(TestCase):
    def setUp(self):
        self.community = create_community("Famiglia Bianchi")
        self.mario = Customer.objects.get(user=create_customer(self.community))
        self.url = f"/sp/customers/communities/{self.community.id}/delete/"
        self.client.force_login(create_technician())

    def test_confirmation_says_the_users_go_back_to_waiting(self):
        response = self.client.get(self.url)

        self.assertContains(response, "tornano in attesa di una community")
        self.assertContains(response, "Elimina Famiglia Bianchi")

    def test_deleting_leaves_its_users_waiting_for_another_community(self):
        response = self.client.post(self.url)

        self.assertRedirects(response, "/sp/customers/communities/", fetch_redirect_response=False)
        self.assertFalse(Community.objects.exists())
        self.mario.refresh_from_db()
        self.assertIsNone(self.mario.community)
        self.assertEqual(list(self.client.get("/sp/customers/").context["page"]), [self.mario])

    def test_a_community_with_systems_is_not_deleted(self):
        create_system(self.community)

        self.assertContains(self.client.get(self.url), "Prima vanno eliminati o spostati")
        self.client.post(self.url)

        self.assertTrue(Community.objects.filter(pk=self.community.pk).exists())

    def test_its_master_is_uninstalled(self):
        with mock.patch("SP.mqtt.publish"), self.captureOnCommitCallbacks(execute=True):
            install_gateway(self.community)
        self.assertContains(self.client.get(self.url), "Il master della community viene disinstallato")

        with mock.patch("SP.mqtt.publish") as publish, self.captureOnCommitCallbacks(execute=True):
            self.client.post(self.url)

        self.assertFalse(Gateway.objects.exists())
        # I messaggi retained del master vengono cancellati
        cleared = [message["topic"] for call in publish.call_args_list for message in call.args[0]
                   if message["payload"] is None]
        self.assertIn(mqtt.gateway_topic(self.community.id, mqtt.CONFIG), cleared)

    def test_only_technicians_delete_communities(self):
        for user in (self.mario.user, create_consultant()):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                self.assertEqual(self.client.post(self.url).status_code, 403)
        self.assertTrue(Community.objects.exists())
