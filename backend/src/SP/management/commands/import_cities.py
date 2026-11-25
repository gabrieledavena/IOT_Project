import csv

import pandas as pd
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from SP.models import City

DEFAULT_FILE = settings.BASE_DIR / "cities" / "Elenco-comuni-italiani.xlsx"
# Coordinate di ogni comune per codice ISTAT, ricavate dal dump di GeoNames (download.geonames.org/export/dump/IT.zip,
# licenza CC-BY 4.0): i comuni sono le unità ADM3, il cui codice admin3 è il codice ISTAT. Castegnero Nanto, nato nel
# 2025 e non ancora in GeoNames, ha il punto a metà tra Castegnero e Nanto.
DEFAULT_COORDINATES = settings.BASE_DIR / "cities" / "coordinate-comuni.csv"

# Colonne del file ISTAT "Elenco comuni italiani"
CODE_COLUMN = "Codice Comune formato alfanumerico"
NAME_COLUMN = "Denominazione in italiano"
PROVINCE_COLUMN = "Denominazione dell'Unità territoriale sovracomunale \n(valida a fini statistici)"
REGION_COLUMN = "Denominazione Regione"


class Command(BaseCommand):
    help = (
        "Imports all the Italian municipalities from the ISTAT list, with the coordinates of the file "
        "cities/coordinate-comuni.csv. Existing cities are updated, never deleted, so it is safe to run it again."
    )

    def add_arguments(self, parser):
        parser.add_argument("--file", default=str(DEFAULT_FILE), help="ISTAT Excel file with the municipalities")
        parser.add_argument("--coordinates", default=str(DEFAULT_COORDINATES),
                            help="CSV with ISTAT code, name, latitude and longitude of every municipality")

    def handle(self, *args, **options):
        municipalities = self.read_municipalities(options["file"])
        coordinates = self.read_coordinates(options["coordinates"])
        self.stdout.write(f"Read {len(municipalities)} municipalities and {len(coordinates)} coordinates.")

        existing = {(city.name, city.province): city for city in City.objects.all()}
        to_create, to_update, without_coordinates = [], [], 0
        for code, name, province, region in municipalities:
            latitude, longitude = coordinates.get(code, (None, None))
            without_coordinates += latitude is None

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
                f"{without_coordinates} municipalities without coordinates in the CSV: saved without coordinates."
            ))

    def read_municipalities(self, path):
        """(ISTAT code, name, province, region) of every municipality in the ISTAT file."""
        try:
            # dtype=str: the codes keep their leading zeros; keep_default_na=False: strings like "NA" stay strings
            df = pd.read_excel(
                path, usecols=[CODE_COLUMN, NAME_COLUMN, PROVINCE_COLUMN, REGION_COLUMN], dtype=str,
                keep_default_na=False, na_values=[""],
            ).fillna("")
        except FileNotFoundError:
            raise CommandError(f"File not found: {path}")
        except ValueError as error:
            raise CommandError(f"Unexpected columns in {path}: {error}")

        rows = zip(df[CODE_COLUMN], df[NAME_COLUMN], df[PROVINCE_COLUMN], df[REGION_COLUMN])
        return [(str(c).strip().zfill(6), str(n).strip(), str(p).strip(), str(r).strip())
                for c, n, p, r in rows if str(n).strip()]

    def read_coordinates(self, path):
        """ISTAT code -> (latitude, longitude) of the coordinates file."""
        try:
            with open(path, newline="", encoding="utf-8") as file:
                return {row["codice_istat"].zfill(6): (float(row["latitudine"]), float(row["longitudine"]))
                        for row in csv.DictReader(file)}
        except FileNotFoundError:
            raise CommandError(f"File not found: {path}")
        except (KeyError, ValueError) as error:
            raise CommandError(f"Unexpected content in {path}: {error}")
