"""Il campo "Ordina per" delle tabelle (SP/sorting.py, templates/partials/sort_select.html)."""
from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.test import RequestFactory, TestCase
from django.utils import timezone

from SP.communities import CommunityListView
from SP.gateways import install_gateway
from SP.models import Intervention, LoRaNode, PhotovoltaicSystem
from SP.sorting import Ordering
from SP.tests.helpers import (
    add_readings, create_city, create_community, create_customer, create_pending_customer, create_system,
    create_technician, install_device, utc,
)

SystemStatus = PhotovoltaicSystem.Status


def names(rows):
    return [row.name for row in rows]


def checked(system, status):
    system.status, system.last_check = status, timezone.now() - timedelta(hours=1)
    system.save()
    return system


class OrderingTests(TestCase):
    OPTIONS = {"name": ("Nome (A-Z)", ("name",)), "power": ("Potenza", ("-max_power",))}

    def ordering(self, query=""):
        return Ordering(RequestFactory().get(f"/{query}"), self.OPTIONS)

    def test_the_first_option_is_the_default_and_an_unknown_one_is_ignored(self):
        self.assertEqual(self.ordering().key, "name")
        self.assertEqual(self.ordering("?sort=power").key, "power")
        self.assertEqual(self.ordering("?sort=-name").key, "name")
        self.assertEqual(self.ordering().choices, [("name", "Nome (A-Z)"), ("power", "Potenza")])


class CommunityListSortingTests(TestCase):
    URL = "/sp/communities/"

    def setUp(self):
        self.rossi = create_community("Rossi Trasporti")
        self.bianchi = create_community("Bianchi Srl")
        self.verdi = create_community("Verdi Spa")
        for name in ("Sede", "Magazzino", "Uffici"):
            create_system(self.rossi, name)
        install_device(create_system(self.verdi, "Tetto"))  # sensore installato ma scollegato
        create_customer(self.bianchi)
        self.client.force_login(create_technician())

    def page(self, **params):
        return names(self.client.get(self.URL, params).context["page"])

    def test_orderings(self):
        self.assertEqual(self.page(), ["Bianchi Srl", "Rossi Trasporti", "Verdi Spa"])
        self.assertEqual(self.page(sort="name_desc"), ["Verdi Spa", "Rossi Trasporti", "Bianchi Srl"])
        self.assertEqual(self.page(sort="systems"), ["Rossi Trasporti", "Verdi Spa", "Bianchi Srl"])
        self.assertEqual(self.page(sort="users"), ["Bianchi Srl", "Rossi Trasporti", "Verdi Spa"])
        self.assertEqual(self.page(sort="offline"), ["Verdi Spa", "Bianchi Srl", "Rossi Trasporti"])
        self.assertEqual(self.page(sort="without_sensor"), ["Rossi Trasporti", "Bianchi Srl", "Verdi Spa"])

    def test_the_sort_field_keeps_the_search_and_the_search_keeps_the_sort(self):
        response = self.client.get(self.URL, {"q": "s", "sort": "systems", "page": "1"})

        self.assertContains(response, '<label for="sort"', html=False)
        self.assertContains(response, '<option value="systems" selected>Più impianti</option>', html=True)
        # Il campo ricarica la pagina con la stessa ricerca, dalla prima pagina
        self.assertContains(response, '<input type="hidden" name="q" value="s">', html=True)
        self.assertNotContains(response, 'name="page"')
        # La ricerca tiene l'ordinamento
        self.assertContains(response, '<input type="hidden" name="sort" value="systems">', html=True)

    def test_pagination_keeps_the_sort(self):
        with mock.patch.object(CommunityListView, "per_page", 1):
            response = self.client.get(self.URL, {"sort": "systems"})

        self.assertEqual(names(response.context["page"]), ["Rossi Trasporti"])
        self.assertContains(response, "?q=&amp;sort=systems&amp;page=2")


class CommunityDetailSortingTests(TestCase):
    def setUp(self):
        self.community = create_community("Rossi Trasporti")
        self.small = checked(create_system(self.community, "Uffici", max_power=3.0), SystemStatus.OK)
        self.big = checked(create_system(self.community, "Capannone", max_power=20.0), SystemStatus.FAULT)
        self.medium = create_system(self.community, "Magazzino", max_power=8.0)
        device, _ = install_device(self.small)
        device.online = True
        device.save()
        install_device(self.medium)
        self.url = f"/sp/communities/{self.community.id}/"
        self.client.force_login(create_technician())

    def systems(self, sort):
        return names(self.client.get(self.url, {"sort": sort}).context["systems"])

    def test_orderings_of_the_systems(self):
        self.assertEqual(self.systems(""), ["Capannone", "Magazzino", "Uffici"])
        self.assertEqual(self.systems("name_desc"), ["Uffici", "Magazzino", "Capannone"])
        self.assertEqual(self.systems("id"), ["Uffici", "Capannone", "Magazzino"])
        self.assertEqual(self.systems("power_desc"), ["Capannone", "Magazzino", "Uffici"])
        self.assertEqual(self.systems("power"), ["Uffici", "Magazzino", "Capannone"])
        # Probabile guasto, poi a posto, poi mai controllato
        self.assertEqual(self.systems("status"), ["Capannone", "Uffici", "Magazzino"])
        # Scollegato, poi senza sensore, poi collegato
        self.assertEqual(self.systems("sensor"), ["Magazzino", "Capannone", "Uffici"])

    def test_the_choice_of_the_system_of_a_node_stays_by_name_and_actions_keep_the_sort(self):
        with mock.patch("SP.mqtt.publish"), self.captureOnCommitCallbacks(execute=True):
            gateway, _ = install_gateway(self.community)
        LoRaNode.objects.create(gateway=gateway, node_id="A3F2C1D0", key="0" * 32)

        response = self.client.get(self.url, {"sort": "power"})
        content = response.content.decode()
        choice = content[content.index("Scegli l'impianto"):]
        self.assertLess(choice.index("Capannone"), choice.index("Magazzino"))
        self.assertLess(choice.index("Magazzino"), choice.index("Uffici"))

        with mock.patch("SP.mqtt.publish"), self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(f"{self.url}?sort=power", {"action": "identify", "node": "A3F2C1D0"})
        self.assertRedirects(response, f"{self.url}?sort=power", fetch_redirect_response=False)

    def test_a_single_system_has_nothing_to_sort(self):
        lonely = create_community("Bianchi Srl")
        create_system(lonely, "Tetto")

        self.assertNotContains(self.client.get(f"/sp/communities/{lonely.id}/"), 'name="sort"')


class PendingCustomerSortingTests(TestCase):
    URL = "/sp/customers/"

    def setUp(self):
        self.anna = create_pending_customer("anna", name="Anna")  # Anna Bianchi, la prima a registrarsi
        self.carlo = create_pending_customer("carlo", name="Carlo")
        self.carlo.surname = "Alberti"
        self.carlo.save()
        self.bruno = create_pending_customer("bruno", name="Bruno")
        self.client.force_login(create_technician())

    def page(self, sort):
        return [customer.name for customer in self.client.get(self.URL, {"sort": sort}).context["page"]]

    def test_orderings(self):
        self.assertEqual(self.page(""), ["Anna", "Carlo", "Bruno"])
        self.assertEqual(self.page("newest"), ["Bruno", "Carlo", "Anna"])
        self.assertEqual(self.page("surname"), ["Carlo", "Anna", "Bruno"])
        self.assertEqual(self.page("name"), ["Anna", "Bruno", "Carlo"])

    def test_assigning_goes_back_with_the_same_search_and_sort(self):
        community = create_community("Famiglia Bianchi")

        response = self.client.post(f"{self.URL}{self.anna.id}/assign/",
                                    {"community": community.id, "q": "a", "sort": "newest"})

        self.assertRedirects(response, f"{self.URL}?q=a&sort=newest", fetch_redirect_response=False)


class InterventionSortingTests(TestCase):
    URL = "/sp/interventions/"

    def setUp(self):
        community = create_community("Rossi Trasporti")
        self.customer = create_customer(community)
        self.sede = create_system(community, "Sede")
        self.capannone = create_system(create_community("Bianchi Srl"), "Capannone")
        self.paolo = create_technician("tecnico", first_name="Paolo", last_name="Neri")
        self.zeno = create_technician("zeno")  # senza nome: si ordina per username
        self.today = timezone.localdate()
        self.client.force_login(self.paolo)

    def intervention(self, system, days, **fields):
        return Intervention.objects.create(system=system, requested_by=self.customer,
                                           preferred_date=self.today + timedelta(days=days), **fields)

    def page(self, **params):
        return list(self.client.get(self.URL, params).context["page"])

    def test_requests_by_requested_day_system_or_community(self):
        later = self.intervention(self.sede, 5)
        sooner = self.intervention(self.capannone, 2)

        self.assertEqual(self.page(), [sooner, later])
        self.assertEqual(self.page(sort="date_desc"), [later, sooner])
        self.assertEqual(self.page(sort="requested_desc"), [sooner, later])
        self.assertEqual(self.page(sort="system"), [sooner, later])
        self.assertEqual(self.page(sort="community"), [sooner, later])  # Bianchi Srl prima di Rossi Trasporti

    def test_in_progress_by_technician(self):
        by_zeno = self.intervention(self.sede, 1, status=Intervention.Status.ACCEPTED, staff=self.zeno, code="INV")
        by_paolo = self.intervention(self.sede, 3, status=Intervention.Status.ACCEPTED, staff=self.paolo, code="INV")

        self.assertEqual(self.page(tab="accepted"), [by_zeno, by_paolo])
        self.assertEqual(self.page(tab="accepted", sort="staff"), [by_paolo, by_zeno])

    def test_done_by_execution_day_or_cost(self):
        done = {"status": Intervention.Status.DONE, "staff": self.paolo, "code": "CLN"}
        cheap = self.intervention(self.sede, -9, executed_on=self.today - timedelta(days=9), cost=50, **done)
        expensive = self.intervention(self.sede, -5, executed_on=self.today - timedelta(days=5), cost=300, **done)
        free = self.intervention(self.sede, -1, executed_on=self.today - timedelta(days=1), **done)

        self.assertEqual(self.page(tab="done"), [free, expensive, cheap])
        self.assertEqual(self.page(tab="done", sort="executed"), [cheap, expensive, free])
        # Senza costo in fondo, in tutti e due i versi
        self.assertEqual(self.page(tab="done", sort="cost_desc"), [expensive, cheap, free])
        self.assertEqual(self.page(tab="done", sort="cost"), [cheap, expensive, free])

    def test_each_tab_has_its_own_orderings(self):
        response = self.client.get(self.URL, {"sort": "cost_desc"})

        # Il costo è solo degli interventi eseguiti: tra le richieste vale l'ordinamento predefinito
        self.assertEqual(response.context["ordering"].key, "date")
        self.assertNotContains(response, 'value="cost_desc"')
        self.assertContains(self.client.get(self.URL, {"tab": "done"}), 'value="cost_desc"')
        # "Tutti" e "Solo i miei" tengono l'ordinamento
        self.assertContains(self.client.get(self.URL, {"tab": "accepted", "sort": "staff"}),
                            "?tab=accepted&amp;mine=1&amp;sort=staff")


class HomeSortingTests(TestCase):
    def setUp(self):
        self.community = create_community("Rossi Trasporti")
        self.user = create_customer(self.community)
        sede = checked(create_system(self.community, "Sede", max_power=6.0), SystemStatus.OK)
        sede.last_check = timezone.now() - timedelta(days=3)
        sede.save()
        checked(create_system(self.community, "Capannone", max_power=20.0), SystemStatus.FAULT)
        create_system(self.community, "Uffici", max_power=3.0)  # mai controllato

    def rows(self, **params):
        return [system.name for system, _ in self.client.get("/", params).context["rows"]]

    def test_orderings_of_the_customer(self):
        self.client.force_login(self.user)

        self.assertEqual(self.rows(), ["Capannone", "Sede", "Uffici"])
        self.assertEqual(self.rows(sort="name"), ["Capannone", "Sede", "Uffici"])
        self.assertEqual(self.rows(sort="power"), ["Uffici", "Sede", "Capannone"])
        self.assertEqual(self.rows(sort="power_desc"), ["Capannone", "Sede", "Uffici"])
        self.assertEqual(self.rows(sort="checked_desc"), ["Capannone", "Sede", "Uffici"])
        self.assertEqual(self.rows(sort="checked"), ["Uffici", "Sede", "Capannone"])
        # Il filtro per stato tiene l'ordinamento; la community non è tra le scelte del cliente
        response = self.client.get("/", {"sort": "power"})
        self.assertContains(response, 'href="?status=fault&amp;sort=power"')
        self.assertNotContains(response, 'value="community"')

    def test_technicians_also_sort_by_community(self):
        create_system(create_community("Bianchi Srl"), "Tetto")
        self.client.force_login(create_technician())

        self.assertEqual(self.rows(sort="community"), ["Tetto", "Capannone", "Sede", "Uffici"])


class CityBenchmarkSortingTests(TestCase):
    URL = "/sp/cities/"

    def setUp(self):
        cache.clear()
        yesterday = timezone.now().date() - timedelta(days=1)
        noon = utc(yesterday.year, yesterday.month, yesterday.day, 12, 0)
        self.modena = create_city("Modena", province="Modena", region="Emilia-Romagna")
        self.bari = create_city("Bari", province="Bari", region="Puglia")
        self.ancona = create_city("Ancona", province="Ancona", region="Marche")  # senza dati
        add_readings(create_system(create_community("A", city=self.modena), max_power=2.0), noon, [3.0] * 61)
        bari = create_community("B", city=self.bari)
        add_readings(create_system(bari, max_power=1.0), noon, [3.0] * 61)
        add_readings(create_system(bari, "Altro", max_power=1.0), noon, [3.0] * 61)
        patcher = mock.patch("SP.views.reference_yields", return_value=[])
        patcher.start()
        self.addCleanup(patcher.stop)

    def cities(self, **params):
        """Le città della prova, nell'ordine della tabella (il database ha anche la città predefinita)."""
        mine = {self.modena, self.bari, self.ancona}
        return [city.name for city, _ in self.client.get(self.URL, params).context["rows"] if city in mine]

    def test_orderings(self):
        self.assertEqual(self.cities(), ["Bari", "Modena", "Ancona"])
        self.assertEqual(self.cities(sort="name"), ["Ancona", "Bari", "Modena"])
        self.assertEqual(self.cities(sort="region"), ["Modena", "Ancona", "Bari"])
        # Resa di Bari doppia di quella di Modena; le città senza dati in fondo
        self.assertEqual(self.cities(sort="yield_desc"), ["Bari", "Modena", "Ancona"])
        self.assertEqual(self.cities(sort="yield"), ["Modena", "Bari", "Ancona"])
        self.assertEqual(self.cities(sort="systems"), ["Bari", "Modena", "Ancona"])

    def test_the_filters_keep_the_sort(self):
        response = self.client.get(self.URL, {"sort": "yield", "region": "Puglia"})

        self.assertContains(response, '<input type="hidden" name="sort" value="yield">', html=True)
        self.assertContains(response, '<input type="hidden" name="region" value="Puglia">', html=True)
