from django.conf import settings
from django.contrib.auth.models import Permission, User
from django.core.management.base import BaseCommand
from rest_framework.authtoken.models import Token


class Command(BaseCommand):
    help = 'Creates the user the Python bridge uses to send PanelData to the API and prints its token'

    def add_arguments(self, parser):
        parser.add_argument(
            '--reset',
            action='store_true',
            help='Generate a new token (the old one stops working)'
        )

    def handle(self, *args, **options):
        user, created = User.objects.get_or_create(username=settings.BRIDGE_USERNAME)
        if created:
            # The bridge authenticates only with its token, never with a password
            user.set_unusable_password()
            user.save()

        # The bridge can only add new measurements: no updates, no deletions
        add_panel_data = Permission.objects.get(content_type__app_label='SP', codename='add_paneldata')
        user.user_permissions.add(add_panel_data)

        if options['reset']:
            Token.objects.filter(user=user).delete()
        token, _ = Token.objects.get_or_create(user=user)

        self.stdout.write(self.style.SUCCESS(f"Bridge user '{user.username}' ready. Token:"))
        self.stdout.write(token.key)
        self.stdout.write("Set it in the environment of SensorsBridge.py as SOLAR_BRIDGE_TOKEN.")
