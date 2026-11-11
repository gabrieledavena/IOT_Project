from django import forms

from SP.forms import CityChoiceForm

from .calculator import Assumptions


def number_field(label, min_value, max_value=None, required=True):
    # localize: il consulente può scrivere i decimali con la virgola
    return forms.FloatField(
        label=label, min_value=min_value, max_value=max_value, required=required, localize=True,
        widget=forms.TextInput(attrs={"inputmode": "decimal"}),
    )


class RoiForm(CityChoiceForm):
    """Dati del cliente e ipotesi; il comune si sceglie con i menu a cascata di CityChoiceForm."""

    client = forms.CharField(label="Cliente", max_length=100, required=False)
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
    # Il cliente prima del comune, come nel modulo
    field_order = ("client", "region", "province", "city")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        defaults = Assumptions()
        self.fields["self_consumption_percent"].initial = defaults.self_consumption_share * 100
        self.fields["export_price"].initial = defaults.export_price
        self.fields["degradation_percent"].initial = defaults.annual_degradation * 100
        self.fields["lifetime_years"].initial = defaults.lifetime_years

        for field in self.fields.values():
            is_select = isinstance(field.widget, forms.Select)
            field.widget.attrs["class"] = "form-select" if is_select else "form-control"

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
