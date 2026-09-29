from django.core.management.base import BaseCommand, CommandError

from forecast.predictor import MODEL_PATH, save_model
from forecast.reference_data import DATASET_PATH, download_reference_dataset, load_reference_dataset
from forecast.training import TARGET, feature_importances, train, validate_on_unseen_locations


class Command(BaseCommand):
    help = (
        "Trains the production forecast model (daily kWh per installed kW) on the reference dataset: "
        "real Open-Meteo weather and PVGIS production for 24 Italian locations"
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--refresh-data", action="store_true",
            help="Download the reference dataset again from PVGIS and Open-Meteo (a few minutes)",
        )

    def handle(self, *args, **options):
        if options["refresh_data"] or not DATASET_PATH.exists():
            self.stdout.write("--- 1. DOWNLOAD DEL DATASET DI RIFERIMENTO ---")
            dataset = download_reference_dataset(log=self.stdout.write)
        else:
            dataset = load_reference_dataset()
        if dataset.empty:
            raise CommandError("Il dataset di riferimento è vuoto.")

        locations = dataset["location"].nunique()
        self.stdout.write(
            f"Dataset: {len(dataset)} giornate di {locations} località "
            f"({dataset['Date'].min()} - {dataset['Date'].max()})."
        )

        self.stdout.write("\n--- 2. VALIDAZIONE SU LOCALITÀ ESCLUSE DAL TRAINING ---")
        daily_error, annual = validate_on_unseen_locations(dataset)
        self.stdout.write(f"Errore medio giornaliero: {daily_error:.2f} kWh per kW installato")
        self.stdout.write(
            f"Errore sulla produzione annua: medio {100 * annual['error'].abs().mean():.1f}%, "
            f"massimo {100 * annual['error'].abs().max():.1f}%"
        )
        by_location = annual.groupby(level="location")[["reference", "predicted"]].mean()
        for location, row in by_location.iterrows():
            self.stdout.write(
                f"  {location:<11} riferimento {row['reference']:>5.0f} kWh/kWp, previsto {row['predicted']:>5.0f} kWh/kWp"
            )

        self.stdout.write("\n--- 3. ADDESTRAMENTO SU TUTTE LE LOCALITÀ ---")
        model = train(dataset)
        self.stdout.write("Importanza delle variabili (top 5):")
        for feature, importance in feature_importances(model).head(5).items():
            self.stdout.write(f"  {feature:<18} {importance:.2f}")
        self.stdout.write(f"Resa media nel dataset: {dataset[TARGET].mean():.2f} kWh per kW installato al giorno")

        save_model(model)
        self.stdout.write(self.style.SUCCESS(f"Modello salvato con successo in {MODEL_PATH}"))
