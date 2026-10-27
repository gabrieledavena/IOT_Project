from django.contrib.auth.models import Group, User
from django.test import TestCase

from SP.roles import CONSULTANTS, TECHNICIANS
from SP.tests.helpers import create_community, create_consultant, create_customer, create_system, create_technician

# Pagine dei tecnici (impianti, dispositivi, interventi) e dei consulenti (preventivi)
TECHNICIAN_PAGES = ["/sp/installations/", "/sp/installations/new/", "/sp/interventions/"]
CONSULTANT_PAGES = ["/roi/"]


class RoleAccessTests(TestCase):
    def setUp(self):
        self.community = create_community("Rossi Trasporti")
        self.system = create_system(self.community, "Sede")
        create_customer(self.community)

    def assert_status(self, urls, status):
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, status)

    def test_consultants_only_prepare_quotes(self):
        self.client.force_login(create_consultant())

        self.assert_status(CONSULTANT_PAGES, 200)
        self.assert_status(TECHNICIAN_PAGES, 403)
        # Nessun impianto: né la sua pagina né quella dell'installazione
        self.assert_status([f"/sp/system/{self.system.id}/", f"/sp/installations/{self.system.id}/"], 403)
        # La home è il calcolo del ROI, e nel menu c'è solo quello
        self.assertRedirects(self.client.get("/"), "/roi/")
        menu = self.client.get("/roi/")
        self.assertContains(menu, 'href="/roi/"')
        for link in ('href="/sp/installations/"', 'href="/sp/interventions/"', 'href="/admin/"', 'href="/sp/community/"'):
            self.assertNotContains(menu, link)

    def test_technicians_install_and_manage_the_systems(self):
        self.client.force_login(create_technician())

        self.assert_status(TECHNICIAN_PAGES + [f"/sp/system/{self.system.id}/", f"/sp/installations/{self.system.id}/"], 200)
        self.assert_status(CONSULTANT_PAGES, 403)
        home = self.client.get("/")
        self.assertContains(home, "Sede")  # la dashboard di tutti gli impianti
        for link in ('href="/sp/installations/"', 'href="/sp/interventions/"', 'href="/admin/"'):
            self.assertContains(home, link)
        self.assertNotContains(home, 'href="/roi/"')

    def test_admins_have_every_role(self):
        self.client.force_login(User.objects.create_superuser("admin", password="test-password"))

        self.assert_status(TECHNICIAN_PAGES + CONSULTANT_PAGES, 200)
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_staff_without_a_role_has_no_staff_page(self):
        self.client.force_login(User.objects.create_user("vecchio", is_staff=True))

        self.assert_status(TECHNICIAN_PAGES + CONSULTANT_PAGES, 403)


class AdminTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser("admin", password="test-password")

    def add_user(self, username, role):
        self.client.force_login(self.admin)
        response = self.client.post("/admin/auth/user/add/", {
            "username": username, "usable_password": "true",
            "password1": "Una-password-sicura-42", "password2": "Una-password-sicura-42", "role": role,
        })
        self.assertEqual(response.status_code, 302)
        return User.objects.get(username=username)

    def test_admins_create_technicians_with_access_to_the_admin(self):
        technician = self.add_user("tecnico", TECHNICIANS)

        self.assertEqual(list(technician.groups.values_list("name", flat=True)), [TECHNICIANS])
        self.assertTrue(technician.is_staff)

    def test_admins_create_consultants_without_access_to_the_admin(self):
        consultant = self.add_user("consulente", CONSULTANTS)

        self.assertEqual(list(consultant.groups.values_list("name", flat=True)), [CONSULTANTS])
        self.assertFalse(consultant.is_staff)
        self.client.force_login(consultant)
        self.assertRedirects(self.client.get("/admin/"), "/admin/login/?next=/admin/")

    def test_changing_the_role_changes_the_access_to_the_admin(self):
        user = self.add_user("mario", "")
        self.assertFalse(user.is_staff)

        response = self.client.post(f"/admin/auth/user/{user.id}/change/", {
            "username": "mario", "is_active": "on", "groups": [Group.objects.get(name=TECHNICIANS).id],
            "date_joined_0": "2026-10-27", "date_joined_1": "10:00:00",
        })

        self.assertEqual(response.status_code, 302)
        user.refresh_from_db()
        self.assertTrue(user.is_staff)

    def test_technicians_manage_systems_but_not_users_in_the_admin(self):
        self.client.force_login(create_technician())

        self.assertEqual(self.client.get("/admin/SP/photovoltaicsystem/add/").status_code, 200)
        self.assertEqual(self.client.get("/admin/SP/intervention/").status_code, 200)
        self.assertEqual(self.client.get("/admin/SP/community/").status_code, 200)  # solo consultazione
        self.assertEqual(self.client.get("/admin/SP/community/add/").status_code, 403)
        self.assertEqual(self.client.get("/admin/auth/user/").status_code, 403)
        self.assertEqual(self.client.get("/admin/auth/user/add/").status_code, 403)
        self.assertEqual(self.client.get("/admin/auth/group/").status_code, 403)
