from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from SP.installations import bridge_commands, install_device
from SP.models import PhotovoltaicSystem


class Command(BaseCommand):
    help = (
        "Prints new MQTT credentials of the device of a photovoltaic system, installing it if needed, so that "
        "the bridge can connect as that system (for example one of the demo data). The previous token stops working."
    )

    def add_arguments(self, parser):
        parser.add_argument('system_id', type=int, help='Id of the photovoltaic system')

    def handle(self, *args, **options):
        system = PhotovoltaicSystem.objects.select_related('community__city', 'community__owner').filter(
            pk=options['system_id']
        ).first()
        if system is None:
            raise CommandError(f"Photovoltaic system {options['system_id']} not found.")

        with transaction.atomic():
            device, token = install_device(system)

        owner = f", owner {system.community.owner}" if system.community.owner_id else ""
        self.stdout.write(self.style.SUCCESS(
            f"Device of '{system.name}' ({system.max_power} kW, {system.community}{owner}):"
        ))
        self.stdout.write(f"  username: {device.username}")
        self.stdout.write(f"  token:    {token}")
        commands = bridge_commands(device, token)
        self.stdout.write("Start the bridge with Arduino (SimulIDE or USB: the bridge finds its serial port) "
                          "or with a simulated Arduino:")
        self.stdout.write(f"  {commands['serial']}")
        self.stdout.write(f"  {commands['simulate']}")
