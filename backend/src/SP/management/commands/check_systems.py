from collections import Counter
from itertools import groupby

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from forecast.predictor import ForecastError
from SP import mqtt
from SP.models import PhotovoltaicSystem
from SP.monitoring import ANOMALY_THRESHOLD, CHECK_DAYS, SystemChecker, systems_to_check

Status = PhotovoltaicSystem.Status


class Command(BaseCommand):
    help = (
        f"Checks the photovoltaic systems, community by community: the production of the last {CHECK_DAYS} days "
        f"is compared with the forecast model and, if more than {1 - ANOMALY_THRESHOLD:.0%} lower, with the nearby "
        f"systems. Only the systems not checked in the last {CHECK_DAYS} days are checked, unless --all is given; "
        "the scheduler service runs it every hour."
    )

    def add_arguments(self, parser):
        parser.add_argument('--community', type=int, action='append', help='Id of a community to check (repeatable)')
        parser.add_argument('--all', action='store_true', help=f'Also check the systems checked in the last {CHECK_DAYS} days')

    def handle(self, *args, **options):
        now = timezone.now()
        systems = PhotovoltaicSystem.objects.all() if options['all'] else systems_to_check(now)
        if options['community']:
            systems = systems.filter(community_id__in=options['community'])
        systems = list(systems.select_related('community__city').order_by('community__name', 'community_id', 'name'))
        if not systems:
            self.stdout.write('No system to check.')
            return

        changed = []
        try:
            checker = SystemChecker(today=now.date())
            counts = Counter()
            for community, community_systems in groupby(systems, key=lambda system: system.community):
                self.stdout.write(f'{community}:')
                for system in community_systems:
                    result = checker.check(system)
                    counts[result.status if result else None] += 1
                    self.stdout.write(f'  {system.name}: {self.describe(result)}')
                    if result and system.set_status(result.status, now):
                        changed.append(system)
        except ForecastError as error:
            raise CommandError(str(error)) from error

        # I dispositivi degli impianti il cui stato è cambiato lo ricevono via MQTT (con DRT parte il lavaggio)
        if mqtt.publish_status(changed):
            self.stdout.write(f'Status change sent to the devices of {len(mqtt.installed(changed))} systems.')

        summary = ', '.join(f'{counts[status]} {status.label}' for status in Status)
        self.stdout.write(self.style.SUCCESS(
            f'Checked {len(systems) - counts[None]} systems ({summary}) on {checker.first_day} - {checker.last_day}.'
        ))
        if counts[None]:
            self.stdout.write(self.style.WARNING(
                f'{counts[None]} systems not checked: missing readings or weather, they will be checked at the next run.'
            ))

    @staticmethod
    def describe(result):
        if result is None:
            return 'not checked (missing readings or weather)'
        text = f'{result.status.label}, {result.ratio:.0%} of the forecast'
        if result.status != Status.OK and result.neighbours:
            text += f'; {result.anomalous_neighbours} of {result.neighbours} nearby systems also below it'
        elif result.status != Status.OK:
            text += '; no nearby system to compare with'
        return text
