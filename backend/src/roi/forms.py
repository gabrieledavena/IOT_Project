from django import forms

from SP.models import City

from .calculator import Assumptions


NOT_SPECIFIED = "Non indicata"


def cities_with_coordinates():
    # Solo i comuni con coordinate: senza, non si può scaricare il meteo per stimare la produzione
    return City.objects.filter(latitude__isnull=False, longitude__isnull=False).order_by("name", "province")


def number_field(label, min_value, max_value=None, required=True):
    # localize: il consulente può scrivere i decimali con la virgola
    return forms.FloatField(
        label=label, min_value=min_value, max_value=max_value, required=required, localize=True,
        widget=forms.TextInput(attrs={"inputmode": "decimal"}),
    )


class RoiForm(forms.Form):
    client = forms.CharField(label="Cliente", max_length=100, required=False)
    # Regione e provincia servono a filtrare l'elenco dei comuni (vedi roi/city_select.js)
    region = forms.ChoiceField(label="Regione", required=False)
    province = forms.ChoiceField(label="Provincia", required=False)
    city = forms.ModelChoiceField(
        label="Comune",
        queryset=cities_with_coordinates(),
        empty_label="Scegli un comune",
        error_messages={"invalid_choice": "Scegli un comune dall'elenco."},
    )
    peak_power_kw = number_field("Potenza dell'impianto (kWp)", 0.5, 1000)
    annual_consumption_kwh = number_field("Consumi annuali (kWh)", 0)
    system_cost = number_field("Costo impianto + installazione (€)", 1)
    energy_price = number_field("Costo attuale dell'energia (€/kWh)", 0.01, 2)

    # Ipotesi: se lasciate vuote valgono quelle di Assumptions
    self_consumption_percent = number_field("Quota di autoconsumo (%)", 0, 100, required=False)
    export_price = number_field("Prezzo dell'energia immessa in rete (€/kWh)", 0, 2, required=False)
    degradation_percent = number_field("Degrado annuo dei pannelli (%)", 0, 5, required=False)
    lifetime_years = forms.IntegerField(label="Vita utile (anni)", min_value=1, max_value=40, required=False)

    ASSUMPTION_FIELDS = ("self_consumption_percent", "export_price", "degradation_percent", "lifetime_years")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        defaults = Assumptions()
        self.fields["self_consumption_percent"].initial = defaults.self_consumption_share * 100
        self.fields["export_price"].initial = defaults.export_price
        self.fields["degradation_percent"].initial = defaults.annual_degradation * 100
        self.fields["lifetime_years"].initial = defaults.lifetime_years

        # Comuni selezionabili, con regione e provincia, per i menu a cascata
        self.city_options = [
            {"id": pk, "name": name, "province": province or NOT_SPECIFIED, "region": region or NOT_SPECIFIED}
            for pk, name, province, region in cities_with_coordinates().values_list("id", "name", "province", "region")
        ]
        regions = sorted({city["region"] for city in self.city_options})
        provinces = sorted({city["province"] for city in self.city_options})
        self.fields["region"].choices = [("", "Scegli una regione")] + [(name, name) for name in regions]
        self.fields["province"].choices = [("", "Scegli una provincia")] + [(name, name) for name in provinces]

        for field in self.fields.values():
            is_select = isinstance(field.widget, forms.Select)
            field.widget.attrs["class"] = "form-select" if is_select else "form-control"

    def clean(self):
        cleaned_data = super().clean()
        city = cleaned_data.get("city")
        if city:
            for field, value in (("region", city.region), ("province", city.province)):
                chosen = cleaned_data.get(field)
                if chosen and chosen != (value or NOT_SPECIFIED):
                    self.add_error("city", "Il comune non appartiene alla regione o alla provincia scelta.")
                    break
        return cleaned_data

    def assumptions(self):
        data, defaults = self.cleaned_data, Assumptions()

        def value(field, default):
            return default if data.get(field) is None else data[field]

        return Assumptions(
            self_consumption_share=value("self_consumption_percent", defaults.self_consumption_share * 100) / 100,
            export_price=value("export_price", defaults.export_price),
            annual_degradation=value("degradation_percent", defaults.annual_degradation * 100) / 100,
            lifetime_years=value("lifetime_years", defaults.lifetime_years),
        )

    @property
    def main_fields(self):
        return [field for field in self if field.name not in self.ASSUMPTION_FIELDS]

    @property
    def assumption_fields(self):
        return [self[name] for name in self.ASSUMPTION_FIELDS]
