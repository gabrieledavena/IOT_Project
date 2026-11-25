"""Pagine web: home (vetrina o dashboard), registrazione, profilo, confronto tra città, elenco impianti e dashboard di
produzione."""
from datetime import date, timedelta
from statistics import mean

from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Case, Count, F, FloatField, IntegerField, Q, Sum, Value, When
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import dateformat
from django.views import View

from forecast.reference_data import REFERENCE_YEARS

from .access import NOT_A_CUSTOMER, AccessError, visible_system
from .benchmark import measured_city_yields, reference_yields
from .energy import daily_energy, period_summary, totalizer
from .forms import ContactForm, CustomerRegistrationForm
from .models import City, Community, Customer, Intervention, PanelData, PhotovoltaicSystem, StaffProfile
from .production import energy_kwh, get_community_series, get_system_series
from .roles import ROLES, is_consultant, is_technician
from .sorting import SYSTEM_STATES, Ordering, problems_first
from .telegram import enabled as telegram_enabled
from .weather import get_day_weather

# Come spiegare l'esito del controllo automatico (SP/monitoring.py) nella pagina dell'impianto; il colore del suo
# pallino lo dà il filtro status_class (SP/templatetags/ui.py)
SYSTEM_STATUSES = {
    PhotovoltaicSystem.Status.OK: {
        "description": "Nei due giorni controllati l'impianto ha prodotto quanto previsto dal modello per il meteo di quei giorni.",
    },
    PhotovoltaicSystem.Status.DIRTY: {
        "description": "Ha prodotto oltre il 10% in meno del previsto, come gli impianti vicini: probabilmente i pannelli "
                       "sono sporchi (polvere, pollini). Si consiglia una pulizia.",
    },
    PhotovoltaicSystem.Status.FAULT: {
        "description": "Ha prodotto oltre il 10% in meno del previsto, mentre gli impianti vicini producono regolarmente: "
                       "probabile guasto. Il dispositivo lava comunque i pannelli: se dopo un'ora di produzione l'impianto "
                       "non produce come i vicini, viene segnalato come guasto.",
    },
    PhotovoltaicSystem.Status.BROKEN: {
        "description": "Anche dopo il lavaggio dei pannelli produce meno degli impianti vicini: guasto di pannelli o "
                       "inverter. Si consiglia un intervento tecnico.",
    },
}


def register_view(request):
    if request.method == "POST":
        form = CustomerRegistrationForm(request.POST)
        if form.is_valid():
            login(request, form.save())
            return redirect("home")
    else:
        form = CustomerRegistrationForm()
    return render(request, "SP/register.html", {"form": form})


def home(request):
    """Home del sito: la vetrina dei servizi per i visitatori e per i consulenti (che la mostrano ai clienti), per
    gli altri utenti la dashboard degli impianti."""
    user = request.user
    if not user.is_authenticated or (is_consultant(user) and not is_technician(user)):
        return LandingView.as_view()(request)
    return UserDashboardView.as_view()(request)


class LandingView(View):
    """Vetrina dei servizi per i visitatori e i consulenti, con i numeri degli impianti seguiti e il link al
    confronto tra città."""

    template_name = "SP/landing.html"

    def get(self, request):
        systems = PhotovoltaicSystem.objects.aggregate(count=Count("id"), power=Sum("max_power"))
        context = {
            "systems": systems["count"],
            "installed_power": systems["power"] or 0,
            "communities": Community.objects.count(),
            "cities": Community.objects.values("city").distinct().count(),
        }
        return render(request, self.template_name, context)


class UserDashboardView(LoginRequiredMixin, View):
    """Home, la pagina dopo il login: gli impianti con il loro stato e il link alla pagina di dettaglio.

    I clienti vedono quelli della propria community, i tecnici quelli di tutte; prima quelli con un problema.
    """

    template_name = "SP/user_dashboard.html"
    per_page = 50
    # Filtri per stato: mai controllati a parte, perché il loro stato è solo quello iniziale
    FILTERS = SYSTEM_STATES
    ORDERINGS = {
        "status": ("Prima i problemi", (problems_first(), "community__name", "name")),
        "name": ("Nome (A-Z)", ("name", "community__name")),
        "community": ("Community (A-Z)", ("community__name", "name")),
        "power_desc": ("Potenza più alta", ("-max_power", "name")),
        "power": ("Potenza più bassa", ("max_power", "name")),
        "checked_desc": ("Controllo più recente", (F("last_check").desc(nulls_last=True), "name")),
        "checked": ("Controllo meno recente", (F("last_check").asc(nulls_first=True), "name")),
    }

    def get(self, request):
        staff = is_technician(request.user)
        community = None if staff else Customer.community_of(request.user)
        if not staff and community is None:
            # Appena registrato: la registrazione è riuscita, si aspetta il tecnico che lo associ a una community
            customer = Customer.objects.filter(user=request.user).first()
            if customer is not None:
                return render(request, "SP/awaiting_community.html", {"customer": customer})
            return render(request, self.template_name, {"error": NOT_A_CUSTOMER}, status=403)

        systems = PhotovoltaicSystem.objects.select_related("community__city")
        if community:
            systems = systems.filter(community=community)
        counts = systems.aggregate(total=Count("id"), **{
            name: Count("id", filter=condition) for name, condition in self.FILTERS.items()
        })
        selected = request.GET.get("status") if request.GET.get("status") in self.FILTERS else ""
        if selected:
            systems = systems.filter(self.FILTERS[selected])
        # La community solo per i tecnici, che vedono gli impianti di tutte
        orderings = self.ORDERINGS if staff else {k: v for k, v in self.ORDERINGS.items() if k != "community"}
        ordering = Ordering(request, orderings)
        systems = ordering.apply(systems)

        page = Paginator(systems, self.per_page).get_page(request.GET.get("page"))
        context = {
            "staff": staff,
            "community": community,
            "is_owner": bool(community) and community.owner_id is not None and community.owner.user_id == request.user.id,
            "counts": counts,
            "selected": selected,
            "ordering": ordering,
            "page": page,
            "rows": [(system, SYSTEM_STATUSES[system.status] if system.last_check else None) for system in page],
        }
        if community:
            context.update(self.community_energy(community))
        return render(request, self.template_name, context)

    def community_energy(self, community):
        """Energia della community: produzione di oggi, totalizzatore e indirizzo del sommario per mese e anno."""
        today = date.today()
        installed_power = community.photovoltaic_systems.aggregate(total=Sum("max_power"))["total"]
        return {
            **self.today_production(community, installed_power, today),
            "totals": totalizer(PanelData.objects.filter(system__community=community), installed_power, today),
            "installed_power": installed_power,
            "energy_summary_url": reverse("SP:community_energy"),
        }

    @staticmethod
    def today_production(community, installed_power, today):
        """Produzione di oggi della community: energia, picco di potenza, resa per kW installato, meteo e grafico."""
        series = get_community_series(community, today)
        if not series:
            return {"today": {"day": today}}
        energy = energy_kwh(series)
        return {
            "today": {
                "day": today,
                "energy": energy,
                "peak": max(point["value"] for point in series),
                "specific_yield": energy / installed_power if installed_power else None,
                "weather": get_day_weather(community.city, today),
            },
            "chart_data": series,
        }


class ProfileView(LoginRequiredMixin, View):
    """Il proprio account, dal nome nel menu: dati, ruoli e, per un cliente, la sua community; per un tecnico gli
    interventi che ha in carico. Un cliente vi cambia i suoi recapiti, email e telefono; quelli del personale li
    cambiano solo gli amministratori, dall'admin, e qui si leggono soltanto. Il titolare di una community vi trova
    le notifiche Telegram: se il suo account è collegato e il link alla pagina per collegarlo (SP/telegram.py)."""

    template_name = "SP/profile.html"

    def customer(self):
        return (Customer.objects.select_related("community__city", "community__owner")
                .filter(user=self.request.user).first())

    def get(self, request):
        customer = self.customer()
        return self.render(request, customer, ContactForm(customer=customer) if customer else None)

    def post(self, request):
        customer = self.customer()
        if customer is None:
            raise PermissionDenied("I recapiti del personale li cambiano gli amministratori.")
        form = ContactForm(request.POST, customer=customer)
        if not form.is_valid():
            return self.render(request, customer, form, status=400)
        form.save()
        messages.success(request, "Recapiti aggiornati.")
        return redirect("SP:profile")

    def render(self, request, customer, form, status=200):
        user = request.user
        community = customer.community if customer else None
        technician = is_technician(user)
        context = {
            "profile_user": user,
            "form": form,
            "customer": customer,
            # Il telefono del personale, che lo legge soltanto
            "staff_phone": None if customer else StaffProfile.objects.filter(user=user).values_list("phone", flat=True).first(),
            "community": community,
            "roles": self.roles(user, customer),
            "technician": technician,
        }
        if customer and customer.is_owner:
            context["telegram"] = {"enabled": telegram_enabled(), "linked": bool(customer.telegram_chat_id)}
        if community:
            context.update({
                "members": community.customers.select_related("user").order_by("surname", "name"),
                "systems": community.photovoltaic_systems.count(),
                "installed_power": community.photovoltaic_systems.aggregate(total=Sum("max_power"))["total"],
            })
        if technician:
            mine = Intervention.objects.filter(staff=user)
            context["interventions"] = {
                "open": mine.filter(status__in=(Intervention.Status.ACCEPTED, Intervention.Status.PROPOSED)).count(),
                "done": mine.filter(status=Intervention.Status.DONE).count(),
            }
        return render(request, self.template_name, context, status=status)

    @staticmethod
    def roles(user, customer):
        """I ruoli da mostrare: amministratore, i ruoli del personale (i gruppi) e cliente."""
        roles = ["Amministratore"] if user.is_superuser else []
        roles += [ROLES[name] for name in user.groups.values_list("name", flat=True).order_by("name") if name in ROLES]
        if customer:
            roles.append("Titolare della community" if customer.is_owner else "Cliente")
        return roles


class CityBenchmarkView(View):
    """Pagina pubblica: resa media misurata (kWh per kW installato al giorno) di ogni città italiana."""

    template_name = "SP/city_benchmark.html"
    cities_per_page = 50
    ORDERINGS = {
        "data": ("Prima le città con dati", ("-has_data", "name", "province")),
        "name": ("Nome (A-Z)", ("name", "province")),
        "name_desc": ("Nome (Z-A)", ("-name", "-province")),
        "province": ("Provincia (A-Z)", ("province", "name")),
        "region": ("Regione (A-Z)", ("region", "province", "name")),
        "yield_desc": ("Resa più alta", (F("specific_yield").desc(nulls_last=True), "name")),
        "yield": ("Resa più bassa", (F("specific_yield").asc(nulls_last=True), "name")),
        "systems": ("Più impianti", (F("measured_systems").desc(nulls_last=True), "name")),
    }

    def get(self, request):
        stats = measured_city_yields()
        query = request.GET.get("q", "").strip()
        region = request.GET.get("region", "")
        only_with_data = request.GET.get("only_with_data") == "1"

        cities = City.objects.all()
        if query:
            cities = cities.filter(Q(name__icontains=query) | Q(province__icontains=query))
        if region:
            cities = cities.filter(region=region)
        if only_with_data:
            cities = cities.filter(id__in=stats)
        # Resa e impianti misurati, calcolati fuori dal database: per ordinare la tabella diventano annotazioni
        has_data = Case(When(id__in=list(stats), then=Value(1)), default=Value(0), output_field=IntegerField())
        cities = cities.annotate(
            has_data=has_data if stats else Value(0),
            specific_yield=self.by_city(stats, "specific_yield", FloatField()),
            measured_systems=self.by_city(stats, "systems", IntegerField()),
        )
        ordering = Ordering(request, self.ORDERINGS)
        cities = ordering.apply(cities)

        page = Paginator(cities, self.cities_per_page).get_page(request.GET.get("page"))
        filters = request.GET.copy()
        filters.pop("page", None)
        context = {
            "page": page,
            "rows": [(city, stats.get(city.id)) for city in page],
            "regions": City.objects.exclude(region__isnull=True).exclude(region="")
                                   .values_list("region", flat=True).distinct().order_by("region"),
            "query": query,
            "region": region,
            "only_with_data": only_with_data,
            "ordering": ordering,
            "filters": filters.urlencode(),
            "cities_with_data": len(stats),
            "average_yield": mean(s["specific_yield"] for s in stats.values()) if stats else None,
            "heatmap": self.heatmap_data(stats),
        }
        return render(request, self.template_name, context)

    @staticmethod
    def by_city(stats, name, output_field):
        """Il valore delle statistiche di ogni città, o NULL per le città senza dati."""
        if not stats:
            return Value(None, output_field=output_field)
        return Case(*(When(id=city_id, then=Value(values[name])) for city_id, values in stats.items()),
                    default=None, output_field=output_field)

    @staticmethod
    def heatmap_data(stats):
        """Punti della mappa: resa misurata delle città con dati e resa attesa di riferimento (PVGIS).

        La mappa mostra sempre tutta l'Italia, qualunque siano i filtri della tabella.
        """
        cities = City.objects.filter(id__in=stats, latitude__isnull=False, longitude__isnull=False)
        measured = [
            {
                "name": city.name,
                "province": city.province,
                "latitude": city.latitude,
                "longitude": city.longitude,
                "specific_yield": round(stats[city.id]["specific_yield"], 3),
                "systems": stats[city.id]["systems"],
                "days": stats[city.id]["days"],
                "first_day": stats[city.id]["first_day"].strftime("%d/%m/%Y"),
                "last_day": stats[city.id]["last_day"].strftime("%d/%m/%Y"),
            }
            for city in cities
        ]
        reference = [{**point, "specific_yield": round(point["specific_yield"], 3)} for point in reference_yields()]
        return {"measured": measured, "reference": reference, "reference_years": REFERENCE_YEARS}


class PhotovoltaicSystemListView(LoginRequiredMixin, View):
    template_name = "SP/system_list.html"

    def get(self, request):
        community = Customer.community_of(request.user)
        if community is None:
            return render(request, self.template_name, {"error": NOT_A_CUSTOMER}, status=403)
        # Gli impianti sono della community: ogni suo utente li vede tutti
        context = {"community": community, "systems": community.photovoltaic_systems.order_by("name")}
        return render(request, self.template_name, context)


def day_label(day):
    """Giorno della scelta del giorno nelle dashboard, per esteso: "Lun 23 Nov 2026"."""
    return dateformat.format(date.fromisoformat(day), "D j M Y")


class ProductionDashboardView(LoginRequiredMixin, View):
    """Parte comune delle dashboard: giorno selezionato, energia prodotta, grafici, meteo e mappa.

    Le sottoclassi impostano self.community in load() e indicano quali misure (get_readings)
    e quale serie di produzione (get_series) mostrare.
    """

    template_name = None

    def load(self, request, **kwargs):
        raise NotImplementedError

    def get_readings(self):
        raise NotImplementedError

    def get_series(self, day):
        raise NotImplementedError

    def get_extra_context(self):
        return {}

    def get(self, request, **kwargs):
        try:
            self.load(request, **kwargs)
        except AccessError as error:
            return render(request, self.template_name, {"error": error.message}, status=error.status)

        days = [day.isoformat() for day in self.get_readings().dates("time_stamp", "day", order="DESC")]
        selected_day = request.GET.get("day")
        if days and selected_day not in days:
            # Giorno mancante, non valido o senza misure: si mostra il più recente
            return redirect(f"{request.path}?day={days[0]}")

        series = self.get_series(date.fromisoformat(selected_day)) if days else []
        chart_data = series  # un punto al minuto, la stessa serie del picco e dell'energia
        today = date.today().isoformat()
        context = {
            "community": self.community,
            "available_days": [{"value": day, "label": "Oggi" if day == today else day_label(day)} for day in days],
            "selected_day": selected_day,
            "total_energy": round(energy_kwh(series), 2),
            "chart_data": chart_data,
            "weather": get_day_weather(self.community.city, selected_day) if chart_data else None,
            **self.day_overview(days, selected_day, series),
            **self.get_extra_context(),
        }
        return render(request, self.template_name, context)

    def day_overview(self, days, selected_day, series):
        """Ciò che la banda della pagina mostra del giorno scelto: i 7 giorni con misure attorno a lui, ognuno con
        l'energia prodotta (come nel sommario per mese), i giorni prima e dopo, il picco e l'ultima misura."""
        if not days:
            return {}
        index = days.index(selected_day)
        start = min(max(index - 3, 0), max(len(days) - 7, 0))
        week = [date.fromisoformat(day) for day in reversed(days[start:start + 7])]
        energies = daily_energy(self.get_readings(), week[0], week[-1] + timedelta(days=1))
        today = date.today()
        peak = max(series, key=lambda point: point["value"]) if series else None
        return {
            "day": date.fromisoformat(selected_day),
            "week": [{"value": day.isoformat(), "day": day, "energy": energies.get(day), "today": day == today}
                     for day in week],
            "week_is_latest": start == 0,
            "previous_day": days[index + 1] if index + 1 < len(days) else None,
            "next_day": days[index - 1] if index > 0 else None,
            "is_today": selected_day == today.isoformat(),
            "peak": {"kw": peak["value"], "time": peak["timestamp"][11:16]} if peak else None,
            "last_reading": series[-1]["timestamp"][11:16] if series else None,
        }


class SolarCommunityView(ProductionDashboardView):
    template_name = "SP/community_dashboard.html"

    def load(self, request):
        self.community = Customer.community_of(request.user)
        if self.community is None:
            raise AccessError(NOT_A_CUSTOMER, status=403)

    def get_readings(self):
        return PanelData.objects.filter(system__community=self.community)

    def get_series(self, day):
        return get_community_series(self.community, day)


class PhotovoltaicSystemView(ProductionDashboardView):
    template_name = "SP/system_dashboard.html"

    def load(self, request, system_id):
        self.system = visible_system(request.user, system_id)
        self.community = self.system.community

    def get_readings(self):
        return PanelData.objects.filter(system=self.system)

    def get_series(self, day):
        return get_system_series(self.system, day)

    def get_extra_context(self):
        is_member = Customer.community_of(self.request.user) == self.community
        owner = self.community.owner
        return {
            "system": self.system,
            "status": SYSTEM_STATUSES[self.system.status],
            # Il titolare risponde ai giorni proposti dai tecnici
            "is_owner": is_member and owner is not None and owner.user_id == self.request.user.id,
            # Lo stato delle richieste di intervento, visibile anche al cliente
            "interventions": self.system.interventions.select_related("staff")[:5],
            "can_request_intervention": is_member and self.system.can_request_intervention,
            "energy_summary_url": reverse("SP:system_energy", args=[self.system.id]),
        }


class EnergySummaryView(LoginRequiredMixin, View):
    """Sommario dell'energia prodotta in un mese o in un anno, in JSON per il grafico della pagina.

    Della community dell'utente, o di un impianto che l'utente può vedere. Parametri: period (month o year)
    e date (2026-10 o 2026).
    """

    def get(self, request, system_id=None):
        try:
            if system_id is None:
                community = Customer.community_of(request.user)
                if community is None:
                    raise AccessError(NOT_A_CUSTOMER, status=403)
                readings = PanelData.objects.filter(system__community=community)
                installed_power = community.photovoltaic_systems.aggregate(total=Sum("max_power"))["total"]
            else:
                system = visible_system(request.user, system_id)
                readings, installed_power = PanelData.objects.filter(system=system), system.max_power
        except AccessError as error:
            return JsonResponse({"error": error.message}, status=error.status)
        summary = period_summary(readings, installed_power, request.GET.get("period"), request.GET.get("date"), date.today())
        return JsonResponse(summary)
