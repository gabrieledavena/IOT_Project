from collections import Counter, defaultdict

import geonamescache
import pandas as pd
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from SP.models import City

DEFAULT_FILE = settings.BASE_DIR / "cities" / "Elenco-comuni-italiani.xlsx"

# Colonne del file ISTAT "Elenco comuni italiani"
NAME_COLUMN = "Denominazione in italiano"
PROVINCE_COLUMN = "Denominazione dell'Unità territoriale sovracomunale \n(valida a fini statistici)"
REGION_COLUMN = "Denominazione Regione"

# With 500 inhabitants as the threshold, geonames contains almost every Italian municipality
GEONAMES_MIN_POPULATION = 500


class Command(BaseCommand):
    help = (
        "Imports the Italian municipalities from the ISTAT list, with coordinates from geonames. "
        "Existing cities are updated, never deleted, so it is safe to run it again."
    )

    def add_arguments(self, parser):
        parser.add_argument("--file", default=str(DEFAULT_FILE), help="ISTAT Excel file with the municipalities")

    def handle(self, *args, **options):
        municipalities = self.read_municipalities(options["file"])
        self.stdout.write(f"Read {len(municipalities)} municipalities, matching them with geonames...")
        locate = build_locator(municipalities)

        existing = {(city.name, city.province): city for city in City.objects.all()}
        to_create, to_update, without_coordinates = [], [], 0
        for name, province, region in municipalities:
            place = locate(name, region)
            latitude, longitude = (place["latitude"], place["longitude"]) if place else (None, None)
            without_coordinates += place is None

            city = existing.get((name, province))
            if city is None:
                to_create.append(City(name=name, province=province, region=region, latitude=latitude, longitude=longitude))
            elif (city.region, city.latitude, city.longitude) != (region, latitude, longitude):
                city.region, city.latitude, city.longitude = region, latitude, longitude
                to_update.append(city)

        City.objects.bulk_create(to_create, batch_size=500)
        City.objects.bulk_update(to_update, ["region", "latitude", "longitude"], batch_size=500)

        self.stdout.write(self.style.SUCCESS(
            f"Cities: {len(to_create)} created, {len(to_update)} updated, "
            f"{len(municipalities) - len(to_create) - len(to_update)} unchanged."
        ))
        if without_coordinates:
            self.stdout.write(self.style.WARNING(
                f"{without_coordinates} municipalities not found in geonames: saved without coordinates."
            ))

    def read_municipalities(self, path):
        """(name, province, region) of every municipality in the ISTAT file."""
        try:
            # keep_default_na=False: strings like "NA" must stay strings, not become missing values
            df = pd.read_excel(
                path, usecols=[NAME_COLUMN, PROVINCE_COLUMN, REGION_COLUMN], keep_default_na=False, na_values=[""]
            ).fillna("")
        except FileNotFoundError:
            raise CommandError(f"File not found: {path}")
        except ValueError as error:
            raise CommandError(f"Unexpected columns in {path}: {error}")

        rows = zip(df[NAME_COLUMN], df[PROVINCE_COLUMN], df[REGION_COLUMN])
        return [(str(n).strip(), str(p).strip(), str(r).strip()) for n, p, r in rows if str(n).strip()]


def build_locator(municipalities, min_population=GEONAMES_MIN_POPULATION):
    """Function (name, region) -> geonames place of the municipality, or None if not found.

    Municipalities are looked up by name. To tell apart places with the same name, the region is
    compared with the geonames region code (admin1code): the region -> code mapping is learned from
    the municipalities whose name is unique, and among several candidates in the same region the most
    populated one is chosen.
    """
    places = defaultdict(list)
    for place in geonamescache.GeonamesCache(min_city_population=min_population).get_cities().values():
        if place["countrycode"] == "IT":
            places[place["name"].lower()].append(place)

    votes = defaultdict(Counter)
    for name, _, region in municipalities:
        candidates = places.get(name.lower(), [])
        if len(candidates) == 1:
            votes[region][candidates[0]["admin1code"]] += 1
    region_codes = {region: counter.most_common(1)[0][0] for region, counter in votes.items()}

    def locate(name, region):
        candidates = [p for p in places.get(name.lower(), []) if p["admin1code"] == region_codes.get(region)]
        return max(candidates, key=lambda p: p["population"], default=None)

    return locate
