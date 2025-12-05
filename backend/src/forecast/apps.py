# mia_app/apps.py
import os
import joblib
from django.conf import settings
from django.apps import AppConfig


class ForecastConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "forecast"

    # Variabile per contenere il modello
    model = None

    def ready(self):
        # Il metodo ready viene eseguito all'avvio del server
        print(settings.BASE_DIR)
        model_path = os.path.join(settings.BASE_DIR, 'forecast','ml_models', 'modello_previsione_produzione.joblib')

        # Controllo se il modello esiste per evitare crash se il file manca
        if os.path.exists(model_path):
            print("Caricamento modello Random Forest in corso...")
            self.model = joblib.load(model_path)
            print("Modello caricato con successo!")
        else:
            print(f"Attenzione: File modello nomodello_previsione_produzione.joblibn trovato in {model_path}")