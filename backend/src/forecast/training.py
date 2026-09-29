"""Addestramento e validazione del modello di previsione (resa giornaliera per kW installato)."""
import pandas as pd
from sklearn.ensemble import RandomForestRegressor
from sklearn.model_selection import GroupKFold

from .predictor import FEATURE_COLUMNS, build_features

TARGET = "specific_yield"
VALIDATION_FOLDS = 6


def make_model():
    # Foglie di almeno 50 giornate: la precisione resta la stessa e il file del modello resta piccolo
    return RandomForestRegressor(n_estimators=100, min_samples_leaf=50, n_jobs=-1, random_state=42)


def features_and_target(dataset):
    return build_features(dataset, dataset["latitude"]), dataset[TARGET]


def train(dataset):
    features, target = features_and_target(dataset)
    return make_model().fit(features, target)


def validate_on_unseen_locations(dataset):
    """Errore del modello su località escluse dal training, come per una città qualsiasi del ROI.

    Le località sono divise in VALIDATION_FOLDS gruppi: ogni gruppo viene previsto da un modello
    addestrato sugli altri. Restituisce (errore medio giornaliero in kWh/kWp, tabella annuale per
    località e anno con produzione di riferimento, prevista ed errore relativo).
    """
    features, target = features_and_target(dataset)
    predicted = pd.Series(index=dataset.index, dtype=float)
    for train_rows, test_rows in GroupKFold(n_splits=VALIDATION_FOLDS).split(features, groups=dataset["location"]):
        model = make_model().fit(features.iloc[train_rows], target.iloc[train_rows])
        predicted.iloc[test_rows] = model.predict(features.iloc[test_rows])

    years = pd.to_datetime(dataset["Date"]).dt.year.rename("year")
    annual = pd.DataFrame({"reference": target, "predicted": predicted}).groupby([dataset["location"], years]).sum()
    annual["error"] = (annual["predicted"] - annual["reference"]) / annual["reference"]
    return (predicted - target).abs().mean(), annual


def feature_importances(model):
    return pd.Series(model.feature_importances_, index=FEATURE_COLUMNS).sort_values(ascending=False)
