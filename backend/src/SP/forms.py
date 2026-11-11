from datetime import timedelta

from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User
from django.utils import timezone

from .models import City, Community, Customer, Intervention, PhotovoltaicSystem

NOT_SPECIFIED = "Non indicata"


def cities_with_coordinates():
    # Solo i comuni con coordinate: senza, non si può scaricare il meteo per stime, previsioni e dashboard
    return City.objects.filter(latitude__isnull=False, longitude__isnull=False).order_by("name", "province")


class CityChoiceForm(forms.Form):
    """Scelta del comune con i menu a cascata regione, provincia e comune (SP/static/SP/city_select.js).

    Il template passa city_options allo script con json_script "city-options".
    """

    # Regione e provincia servono solo a filtrare l'elenco dei comuni
    region = forms.ChoiceField(label="Regione", required=False)
    province = forms.ChoiceField(label="Provincia", required=False)
    city = forms.ModelChoiceField(
        label="Comune",
        queryset=cities_with_coordinates(),
        empty_label="Scegli un comune",
        error_messages={"invalid_choice": "Scegli un comune dall'elenco."},
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Comuni selezionabili, con regione e provincia, per i menu a cascata
        self.city_options = [
            {"id": pk, "name": name, "province": province or NOT_SPECIFIED, "region": region or NOT_SPECIFIED}
            for pk, name, province, region in cities_with_coordinates().values_list("id", "name", "province", "region")
        ]
        regions = sorted({city["region"] for city in self.city_options})
        provinces = sorted({city["province"] for city in self.city_options})
        self.fields["region"].choices = [("", "Scegli una regione")] + [(name, name) for name in regions]
        self.fields["province"].choices = [("", "Scegli una provincia")] + [(name, name) for name in provinces]

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


class CommunityForm(CityChoiceForm):
    """Community di un'azienda o di una famiglia, nuova o da modificare (community=...): solo i tecnici
    (SP/customers.py). Modificandola si sceglie anche il titolare, tra i suoi utenti."""

    name = forms.CharField(label="Nome", max_length=Community._meta.get_field("name").max_length,
                           help_text="Es. il nome dell'azienda o \"Famiglia Bianchi\".")

    field_order = ("name", "region", "province", "city")

    def __init__(self, *args, community=None, **kwargs):
        self.community = community
        if community is not None:
            kwargs.setdefault("initial", {"name": community.name, "city": community.city_id,
                                          "owner": community.owner_id})
        super().__init__(*args, **kwargs)
        if community is not None:
            members = community.customers.select_related("user").order_by("surname", "name")
            if members.exists():
                # Una community con utenti ha sempre un titolare (Community.clean)
                self.fields["owner"] = forms.ModelChoiceField(
                    label="Titolare", queryset=members, empty_label=None,
                    help_text="Uno degli utenti della community.",
                )
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-select" if isinstance(field.widget, forms.Select) else "form-control"

    def clean_name(self):
        name = " ".join(self.cleaned_data["name"].split())
        others = Community.objects.exclude(pk=self.community.pk) if self.community else Community.objects.all()
        if others.filter(name__iexact=name).exists():
            raise forms.ValidationError("Esiste già una community con questo nome.")
        return name


class CustomerRegistrationForm(UserCreationForm):
    """Registrazione di un cliente, privato o di un'azienda. Non si sceglie la community: la assegna un tecnico."""

    name = forms.CharField(label="Nome", max_length=100, required=True)
    surname = forms.CharField(label="Cognome", max_length=100, required=True)
    email = forms.EmailField(label="Email (facoltativa)", required=False)
    phone = forms.CharField(
        label="Telefono (facoltativo)", max_length=Customer._meta.get_field("phone").max_length, required=False,
        validators=Customer._meta.get_field("phone").validators,
    )

    class Meta(UserCreationForm.Meta):
        model = User
        fields = UserCreationForm.Meta.fields + ('name', 'surname', 'email', 'phone')

    def clean_phone(self):
        return " ".join(self.cleaned_data["phone"].split())

    def save(self, commit=True):
        user = super().save(commit=False)
        if commit:
            user.save()
            Customer.objects.create(
                user=user,
                name=self.cleaned_data.get('name'),
                surname=self.cleaned_data.get('surname'),
                phone=self.cleaned_data.get('phone'),
            )
        return user


class CommunityAssignmentForm(forms.Form):
    """Il tecnico sceglie la community a cui associare un utente appena registrato."""

    community = forms.ModelChoiceField(
        queryset=Community.objects.select_related("city").order_by("name"), label="Community",
        empty_label="Scegli la community", widget=forms.Select(attrs={"class": "form-select form-select-sm"}),
    )


# Un intervento si può chiedere da domani ai prossimi due mesi
INTERVENTION_MAX_DAYS_AHEAD = 60


def intervention_date_range():
    """Primo e ultimo giorno che il cliente può scegliere per l'intervento."""
    tomorrow = timezone.localdate() + timedelta(days=1)
    return tomorrow, tomorrow + timedelta(days=INTERVENTION_MAX_DAYS_AHEAD - 1)


class InterventionRequestForm(forms.ModelForm):
    """Richiesta del cliente: il giorno in cui vorrebbe l'intervento e, se vuole, una descrizione del problema."""

    class Meta:
        model = Intervention
        fields = ("preferred_date", "customer_notes")
        labels = {"preferred_date": "Giorno dell'intervento", "customer_notes": "Note per il tecnico (facoltative)"}
        widgets = {
            "preferred_date": forms.DateInput(attrs={"type": "date", "class": "form-control"}, format="%Y-%m-%d"),
            "customer_notes": forms.Textarea(attrs={
                "rows": 4, "class": "form-control",
                "placeholder": "Es. orari in cui il tecnico può accedere al tetto, problemi notati sull'inverter...",
            }),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        first, last = intervention_date_range()
        self.fields["preferred_date"].widget.attrs.update(min=first.isoformat(), max=last.isoformat())

    def clean_preferred_date(self):
        day = self.cleaned_data["preferred_date"]
        first, last = intervention_date_range()
        if not first <= day <= last:
            raise forms.ValidationError(f"Scegli un giorno tra il {first:%d/%m/%Y} e il {last:%d/%m/%Y}.")
        return day


class InterventionAcceptForm(forms.ModelForm):
    """Prima di accettare una richiesta lo staff stabilisce di che intervento si tratta."""

    class Meta:
        model = Intervention
        fields = ("code",)
        widgets = {"code": forms.Select(attrs={"class": "form-select"})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["code"].required = True


class InterventionCompletionForm(forms.ModelForm):
    """Dettagli dell'intervento eseguito, che inserisce il membro dello staff che lo ha in carico."""

    class Meta:
        model = Intervention
        fields = ("code", "executed_on", "notes", "cost")
        widgets = {
            "code": forms.Select(attrs={"class": "form-select"}),
            "executed_on": forms.DateInput(attrs={"type": "date", "class": "form-control"}, format="%Y-%m-%d"),
            "notes": forms.Textarea(attrs={"rows": 5, "class": "form-control",
                                           "placeholder": "Cosa è stato verificato, sostituito o riparato"}),
            "cost": forms.NumberInput(attrs={"class": "form-control", "step": "0.01", "min": "0"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in ("code", "executed_on", "notes"):
            self.fields[name].required = True
        self.fields["executed_on"].widget.attrs["max"] = timezone.localdate().isoformat()
        self.fields["cost"].min_value = 0

    def clean_executed_on(self):
        day = self.cleaned_data["executed_on"]
        if day > timezone.localdate():
            raise forms.ValidationError("L'intervento non può essere eseguito in una data futura.")
        if day < timezone.localdate(self.instance.requested_at):
            raise forms.ValidationError("L'intervento non può essere precedente alla richiesta.")
        return day


class InstallationForm(forms.ModelForm):
    """Nuova installazione: l'impianto viene creato nella community scelta, con il suo dispositivo."""

    class Meta:
        model = PhotovoltaicSystem
        fields = ("community", "name", "max_power", "area", "brand", "inclination",
                  "selling_rate_per_kwh", "buying_rate_per_kwh")
        labels = {
            "community": "Community", "name": "Nome dell'impianto", "max_power": "Potenza di picco (kW)",
            "area": "Superficie (m²)", "brand": "Marca dei pannelli", "inclination": "Inclinazione (°)",
            "selling_rate_per_kwh": "Prezzo di vendita (€/kWh)", "buying_rate_per_kwh": "Prezzo di acquisto (€/kWh)",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["community"].queryset = Community.objects.select_related("city").order_by("name")
        self.fields["max_power"].min_value = 0.1
        self.fields["max_power"].widget.attrs.update(min="0.1", step="0.01")
        for name, field in self.fields.items():
            field.widget.attrs["class"] = "form-select" if name == "community" else "form-control"

    def clean_max_power(self):
        max_power = self.cleaned_data["max_power"]
        if max_power <= 0:
            raise forms.ValidationError("La potenza di picco deve essere maggiore di zero.")
        return max_power


class ForceStatusForm(forms.Form):
    """Simulazione: lo staff imposta lo stato dell'impianto come se l'avesse trovato il controllo automatico."""

    status = forms.ChoiceField(
        choices=PhotovoltaicSystem.Status.choices, label="Nuovo stato",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
