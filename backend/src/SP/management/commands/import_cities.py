import os
import pandas as pd
import geonamescache
from django.core.management.base import BaseCommand
from SP.models import City
from config.settings import BASE_DIR


class Command(BaseCommand):
    help = 'Load cities data from Elenco-comuni-italiani.xlsx using pandas and geonamescache'

    def handle(self, *args, **kwargs):
        # The path is relative to the root of the project, where manage.py is
        file_path = os.path.join(BASE_DIR, 'cities', 'Elenco-comuni-italiani.xlsx')

        if not os.path.exists(file_path):
            self.stdout.write(self.style.ERROR(f'File not found: {file_path}'))
            return

        try:
            self.stdout.write("Reading Excel file with pandas...")
            # We must specify keep_default_na=False and na_values=None to prevent "NA" from being treated as NaN
            # This is crucial because "NA" in "Sigla automobilistica" means Napoli
            df = pd.read_excel(file_path, keep_default_na=False, na_values=[''])
            
            cities_to_create = []
            
            gc = geonamescache.GeonamesCache()
            
            # Fetch all cities from geonamescache and filter for Italy (country code 'IT')
            gc_cities = gc.get_cities()
            italy_cities = {city_data['name'].lower(): city_data for city_id, city_data in gc_cities.items() if city_data['countrycode'] == 'IT'}
            
            # We assume columns: Denominazione in italiano, Sigla automobilistica, Denominazione regione
            # Adjust these if the actual column names differ
            
            # Helper to find column names safely
            col_name = next((c for c in df.columns if 'comun' in c.lower() or 'denominazione in italiano' in c.lower()), None)
            col_prov = next((c for c in df.columns if 'provinc' in c.lower() or 'sigla automobilistica' in c.lower() or 'unità territoriale' in c.lower()), None)
            col_reg = next((c for c in df.columns if 'region' in c.lower()), None)
            
            if not col_name:
                self.stdout.write(self.style.ERROR("Could not identify the city name column."))
                return

            self.stdout.write(f"Identified columns: Name='{col_name}', Province='{col_prov}', Region='{col_reg}'")
            self.stdout.write("Matching cities to geonamescache data...")
            
            missing_coordinates_count = 0

            for index, row in df.iterrows():
                name = str(row[col_name]).strip() if pd.notna(row[col_name]) else ""
                
                # Careful not to treat string "NA" as missing
                province = str(row[col_prov]).strip() if pd.notna(row[col_prov]) and str(row[col_prov]) else ""
                region = str(row[col_reg]).strip() if pd.notna(row[col_reg]) and str(row[col_reg]) else ""

                if not name:
                    continue

                lat, lon = None, None
                
                # Match by city name (case-insensitive)
                name_lower = name.lower()
                
                if name_lower in italy_cities:
                    matched_city = italy_cities[name_lower]
                    lat = matched_city.get('latitude')
                    lon = matched_city.get('longitude')
                else:
                    missing_coordinates_count += 1
                
                cities_to_create.append(
                    City(name=name, province=province, region=region, latitude=lat, longitude=lon)
                )

                if len(cities_to_create) % 1000 == 0:
                    self.stdout.write(f"Processed {len(cities_to_create)} cities...")

            if missing_coordinates_count > 0:
                self.stdout.write(self.style.WARNING(f"{missing_coordinates_count} cities could not be matched for coordinates."))

            if cities_to_create:
                self.stdout.write("Clearing old data and inserting new records...")
                City.objects.all().delete()
                
                City.objects.bulk_create(cities_to_create, batch_size=500)
                self.stdout.write(self.style.SUCCESS(f'Successfully imported {len(cities_to_create)} cities!'))
            else:
                self.stdout.write(self.style.WARNING("No cities found in the file."))

        except ImportError as e:
             self.stdout.write(self.style.ERROR(f"Import error: {str(e)}. Ensure pandas, geonamescache and openpyxl are installed."))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'Error importing cities: {str(e)}'))
