"""Le community per i tecnici: lista, pagina di una community con master, nodi e impianti, e anagrafica
(SP/communities.py)."""
from unittest import mock

from django.test import TestCase

from SP import mqtt
from SP.gateways import install_gateway
from SP.models import Community, Customer, Gateway, LoRaNode
from SP.tests.helpers import (
    create_city, create_community, create_consultant, create_customer, create_pending_customer, create_system,
    create_technician, install_device,
)

LIST_URL = "/sp/communities/"


class CommunityTestCase(TestCase):
    def setUp(self):
        self.modena = create_city("Modena", province="Modena", region="Emilia-Romagna")
        self.community = create_community("Rossi Trasporti", city=self.modena)
        self.mario = Customer.objects.get(user=create_customer(self.community))
        create_customer(self.community, username="luigi")
        # Un impianto con il sensore collegato, uno scollegato e uno senza sensore
        self.online = create_system(self.community, "Magazzino")
        device, _ = install_device(self.online)
        device.online = True
        device.save()
        self.offline = create_system(self.community, "Sede")
        install_device(self.offline)
        self.without_sensor = create_system(self.community, "Capannone")
        self.page_url = f"/sp/communities/{self.community.id}/"
        self.client.force_login(create_technician())

    def install_master(self):
        with mock.patch("SP.mqtt.publish"), self.captureOnCommitCallbacks(execute=True):
            gateway, _ = install_gateway(self.community)
        Gateway.objects.filter(pk=gateway.pk).update(online=True)
        return gateway


class CommunityListTests(CommunityTestCase):
    def test_shows_owner_users_systems_with_their_sensor_master_and_nodes(self):
        gateway = self.install_master()
        LoRaNode.objects.create(gateway=gateway, node_id="A3F2C1D0", key="0" * 32)
        create_community("Bianchi Srl", city=self.modena)

        response = self.client.get(LIST_URL)

        rows = {c.name: (c.owner, c.users, c.systems, c.online, c.offline, c.without_sensor, c.node_count,
                         c.pending_nodes) for c in response.context["page"]}
        self.assertEqual(rows, {"Rossi Trasporti": (self.mario, 2, 3, 1, 1, 1, 1, 1),
                                "Bianchi Srl": (None, 0, 0, 0, 0, 0, 0, 0)})
        for text in ("1 collegato", "1 scollegato", "1 senza sensore", "1 da assegnare", "Collegato",
                     "Non installato", f'href="{self.page_url}"', 'href="/sp/communities/new/"',
                     'href="/sp/installations/new/"'):
            self.assertContains(response, text)

    def test_search_finds_a_community_also_by_the_name_of_a_system(self):
        create_community("Bianchi Srl", city=self.modena)

        for query, expected in (("rossi", ["Rossi Trasporti"]), ("modena", ["Rossi Trasporti", "Bianchi Srl"]),
                                ("magazz", ["Rossi Trasporti"]), ("zzz", [])):
            with self.subTest(query=query):
                page = self.client.get(LIST_URL, {"q": query}).context["page"]
                self.assertEqual(sorted(c.name for c in page), sorted(expected))
        # Trovata per un impianto, conta comunque tutti i suoi impianti
        self.assertEqual(self.client.get(LIST_URL, {"q": "magazz"}).context["page"][0].systems, 3)

    def test_is_the_page_community_of_the_menu_of_the_technicians(self):
        home = self.client.get("/")
        self.assertContains(home, f'href="{LIST_URL}"')
        self.assertNotContains(home, 'href="/sp/installations/"')
        self.assertEqual(self.client.get("/sp/installations/").status_code, 404)

    def test_new_users_page_no_longer_lists_the_communities(self):
        response = self.client.get("/sp/customers/")
        self.assertNotContains(response, "Tutte le community")
        self.assertContains(response, 'href="/sp/communities/new/"')

    def test_technicians_only(self):
        for user in (self.mario.user, create_consultant()):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                self.assertEqual(self.client.get(LIST_URL).status_code, 403)
                self.assertEqual(self.client.get(self.page_url).status_code, 403)


class CommunityDetailTests(CommunityTestCase):
    def test_lists_the_systems_with_their_sensor(self):
        gateway = self.install_master()
        LoRaNode.objects.create(gateway=gateway, node_id="A3F2C1D0", key="0" * 32, system=self.offline)

        response = self.client.get(self.page_url)

        self.assertEqual([s.name for s in response.context["systems"]], ["Capannone", "Magazzino", "Sede"])
        self.assertContains(response, "3 impianti · 2 utenti · titolare Mario Rossi")
        self.assertContains(response, f"pv-{self.online.id}")
        self.assertContains(response, 'nodo LoRa <span class="font-monospace">A3F2C1D0</span>')
        self.assertContains(response, "Non installato")
        for system in (self.online, self.offline, self.without_sensor):
            self.assertContains(response, f'href="/sp/installations/{system.id}/"')

    def test_links_to_a_new_installation_of_the_community_and_to_its_edit(self):
        response = self.client.get(self.page_url)

        self.assertContains(response, f'href="/sp/installations/new/?community={self.community.id}"')
        self.assertContains(response, f'href="/sp/communities/{self.community.id}/edit/"')

    def test_a_community_without_systems(self):
        empty = create_community("Bianchi Srl", city=self.modena)

        response = self.client.get(f"/sp/communities/{empty.id}/")

        self.assertContains(response, "Nessun impianto")
        self.assertContains(response, "Nessun master")

    def test_the_installation_page_leads_back_to_the_community(self):
        response = self.client.get(f"/sp/installations/{self.online.id}/")

        self.assertContains(response, f'href="{self.page_url}"')


class NewCommunityTests(TestCase):
    URL = "/sp/communities/new/"

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

        community = Community.objects.get()
        self.assertRedirects(response, f"/sp/communities/{community.id}/", fetch_redirect_response=False)
        self.assertEqual((community.name, community.city, community.owner), ("Famiglia Bianchi", self.modena, None))
        self.anna.refresh_from_db()
        self.assertIsNone(self.anna.community)

    def test_creates_the_community_of_a_waiting_user_who_becomes_its_owner(self):
        response = self.client.get(f"{self.URL}?customer={self.anna.id}")
        self.assertContains(response, "Alla nuova community sarà associato <strong>Anna Bianchi</strong>")

        response = self.create(f"{self.URL}?customer={self.anna.id}")

        self.assertRedirects(response, "/sp/customers/", fetch_redirect_response=False)
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
        self.url = f"/sp/communities/{self.community.id}/edit/"
        self.client.force_login(create_technician())

    def test_form_starts_from_the_community_and_offers_its_users_as_owner(self):
        response = self.client.get(self.url)

        form = response.context["form"]
        self.assertEqual((form["name"].value(), form["city"].value(), form["owner"].value()),
                         ("Famiglia Bianchi", self.modena.id, self.mario.id))
        self.assertEqual(set(form.fields["owner"].queryset), {self.mario, self.luigi})
        self.assertContains(response, "luigi")

    def test_changes_name_city_and_owner(self):
        response = self.client.post(self.url, {"name": "Famiglia Rossi", "city": self.bari.id, "owner": self.luigi.id})

        self.assertRedirects(response, f"/sp/communities/{self.community.id}/", fetch_redirect_response=False)
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

        response = self.client.post(f"/sp/communities/{empty.id}/edit/", {"name": "Vuota 2", "city": self.bari.id})

        self.assertEqual(response.status_code, 302)
        empty.refresh_from_db()
        self.assertEqual((empty.name, empty.owner), ("Vuota 2", None))

    def test_only_technicians_edit_communities(self):
        for user in (self.mario.user, create_consultant()):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                self.assertEqual(self.client.get(self.url).status_code, 403)
                self.assertEqual(self.client.post(self.url, {"name": "X", "city": self.bari.id}).status_code, 403)
        self.community.refresh_from_db()
        self.assertEqual(self.community.name, "Famiglia Bianchi")


class DeleteCommunityTests(TestCase):
    def setUp(self):
        self.community = create_community("Famiglia Bianchi")
        self.mario = Customer.objects.get(user=create_customer(self.community))
        self.url = f"/sp/communities/{self.community.id}/delete/"
        self.client.force_login(create_technician())

    def test_confirmation_says_the_users_go_back_to_waiting(self):
        response = self.client.get(self.url)

        self.assertContains(response, "tornano in attesa di una community")
        self.assertContains(response, "Elimina Famiglia Bianchi")

    def test_deleting_leaves_its_users_waiting_for_another_community(self):
        response = self.client.post(self.url)

        self.assertRedirects(response, "/sp/communities/", fetch_redirect_response=False)
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
