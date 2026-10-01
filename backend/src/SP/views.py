"""Pagine web: registrazione, confronto tra città, elenco impianti e dashboard di produzione."""
from datetime import date
from statistics import mean

from django.contrib.auth import login
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.paginator import Paginator
from django.db.models import Case, Count, IntegerField, Q, Value, When
from django.shortcuts import redirect, render
from django.views import View

from forecast.reference_data import REFERENCE_YEARS

from .access import NOT_A_CUSTOMER, AccessError, is_staff, visible_system
from .benchmark import measured_city_yields, reference_yields
from .forms import CustomerRegistrationForm
from .models import City, Customer, PanelData, PhotovoltaicSystem
from .production import energy_kwh, get_community_series, get_system_series
from .weather import get_day_weather

# Come mostrare l'esito del controllo automatico (SP/monitoring.py) nella pagina dell'impianto
SYSTEM_STATUSES = {
    PhotovoltaicSystem.Status.OK: {
        "color": "success", "icon": "fa-check-circle",
        "description": "Nei due giorni controllati l'impianto ha prodotto quanto previsto dal modello per il meteo di quei giorni.",
    },
    PhotovoltaicSystem.Status.DIRTY: {
        "color": "warning", "icon": "fa-broom",
        "description": "Ha prodotto oltre il 10% in meno del previsto, come gli impianti vicini: probabilmente i pannelli "
                       "sono sporchi (polvere, pollini). Si consiglia una pulizia.",
    },
    PhotovoltaicSystem.Status.FAULT: {
        "color": "danger", "icon": "fa-exclamation-triangle",
        "description": "Ha prodotto oltre il 10% in meno del previsto, mentre gli impianti vicini producono regolarmente: "
                       "probabile guasto di pannelli o inverter. Si consiglia un intervento tecnico.",
    },
}


def register_view(request):
    if request.method == "POST":
        form = CustomerRegistrationForm(request.POST)
        if form.is_valid():
            login(request, form.save())
            return redirect("SP:dashboard")
    else:
        form = CustomerRegistrationForm()
    return render(request, "SP/register.html", {"form": form})


class UserDashboardView(LoginRequiredMixin, View):
    """Pagina dopo il login: gli impianti con il loro stato e il link alla pagina di dettaglio.

    I clienti vedono quelli della propria community, lo staff quelli di tutte; prima quelli con un problema.
    """

    template_name = "SP/user_dashboard.html"
    per_page = 50
    Status = PhotovoltaicSystem.Status
    checked = Q(last_check__isnull=False)
    # Filtri per stato: mai controllati a parte, perché il loro stato è solo quello iniziale
    FILTERS = {
        "fault": checked & Q(status=Status.FAULT),
        "dirty": checked & Q(status=Status.DIRTY),
        "ok": checked & Q(status=Status.OK),
        "new": Q(last_check__isnull=True),
    }

    def get(self, request):
        staff = is_staff(request.user)
        community = None if staff else Customer.community_of(request.user)
        if not staff and community is None:
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
        priority = Case(
            *(When(self.FILTERS[name], then=Value(rank)) for rank, name in enumerate(self.FILTERS)),
            output_field=IntegerField(),
        )
        systems = systems.order_by(priority, "community__name", "name")

        page = Paginator(systems, self.per_page).get_page(request.GET.get("page"))
        context = {
            "staff": staff,
            "community": community,
            "is_owner": bool(community) and community.owner_id is not None and community.owner.user_id == request.user.id,
            "counts": counts,
            "selected": selected,
            "page": page,
            "rows": [(system, SYSTEM_STATUSES[system.status] if system.last_check else None) for system in page],
        }
        return render(request, self.template_name, context)


class CityBenchmarkView(View):
    """Pagina pubblica: resa media misurata (kWh per kW installato al giorno) di ogni città italiana."""

    template_name = "SP/city_benchmark.html"
    cities_per_page = 50

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
        # Prima le città con dati, poi le altre in ordine alfabetico
        has_data = Case(When(id__in=list(stats), then=Value(1)), default=Value(0), output_field=IntegerField())
        cities = cities.annotate(has_data=has_data if stats else Value(0)).order_by("-has_data", "name", "province")

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
            "filters": filters.urlencode(),
            "cities_with_data": len(stats),
            "average_yield": mean(s["specific_yield"] for s in stats.values()) if stats else None,
            "heatmap": self.heatmap_data(stats),
        }
        return render(request, self.template_name, context)

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


class ProductionDashboardView(LoginRequiredMixin, View):
    """Parte comune delle dashboard: giorno selezionato, energia prodotta, grafici, meteo e mappa.

    Le sottoclassi impostano self.community in load() e indicano quali misure (get_readings)
    e quale serie di produzione (get_series) mostrare.
    """

    template_name = None
    # Un punto ogni 2 minuti basta per i grafici e dimezza i dati inviati alla pagina
    chart_step = 2

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
        chart_data = series[:: self.chart_step]
        today = date.today().isoformat()
        context = {
            "community": self.community,
            "available_days": [{"value": day, "label": "Oggi" if day == today else day} for day in days],
            "selected_day": selected_day,
            "total_energy": round(energy_kwh(series), 2),
            "chart_data": chart_data,
            "weather": get_day_weather(self.community.city, selected_day) if chart_data else None,
            **self.get_extra_context(),
        }
        return render(request, self.template_name, context)


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
        return {
            "system": self.system,
            "status": SYSTEM_STATUSES[self.system.status],
            # Lo stato delle richieste di intervento, visibile anche al cliente
            "interventions": self.system.interventions.select_related("staff")[:5],
            "can_request_intervention": is_member and self.system.can_request_intervention,
        }
