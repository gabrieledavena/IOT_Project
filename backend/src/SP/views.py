"""Pagine web: registrazione, elenco impianti e dashboard di produzione di community e impianti."""
from datetime import date

from django.contrib.auth import login
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import redirect, render
from django.views import View

from .forms import CustomerRegistrationForm
from .models import Customer, PanelData, PhotovoltaicSystem
from .production import energy_kwh, get_community_series, get_system_series
from .weather import get_day_weather

NOT_A_CUSTOMER = "Utente non associato a un cliente."


def register_view(request):
    if request.method == "POST":
        form = CustomerRegistrationForm(request.POST)
        if form.is_valid():
            login(request, form.save())
            return redirect("home")
    else:
        form = CustomerRegistrationForm()
    return render(request, "SP/register.html", {"form": form})


class PhotovoltaicSystemListView(LoginRequiredMixin, View):
    template_name = "SP/system_list.html"

    def get(self, request):
        community = Customer.community_of(request.user)
        if community is None:
            return render(request, self.template_name, {"error": NOT_A_CUSTOMER}, status=403)
        context = {"community": community, "systems": community.photovoltaic_systems.all()}
        return render(request, self.template_name, context)


class DashboardError(Exception):
    """La dashboard non si può mostrare: il messaggio compare nella pagina con lo status HTTP indicato."""

    def __init__(self, message, status):
        super().__init__(message)
        self.message = message
        self.status = status


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
        except DashboardError as error:
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
            raise DashboardError(NOT_A_CUSTOMER, status=403)

    def get_readings(self):
        return PanelData.objects.filter(system__community=self.community)

    def get_series(self, day):
        return get_community_series(self.community, day)


class PhotovoltaicSystemView(ProductionDashboardView):
    template_name = "SP/system_dashboard.html"

    def load(self, request, system_id):
        self.system = PhotovoltaicSystem.objects.select_related("community__city").filter(pk=system_id).first()
        if self.system is None:
            raise DashboardError("Impianto non trovato.", status=404)
        self.community = self.system.community

        # Staff e superuser vedono tutti gli impianti, i clienti solo quelli della propria community
        if not (request.user.is_staff or request.user.is_superuser):
            customer_community = Customer.community_of(request.user)
            if customer_community is None:
                raise DashboardError(NOT_A_CUSTOMER, status=403)
            if customer_community != self.community:
                raise DashboardError("Non sei autorizzato a visualizzare questo impianto.", status=403)

    def get_readings(self):
        return PanelData.objects.filter(system=self.system)

    def get_series(self, day):
        return get_system_series(self.system, day)

    def get_extra_context(self):
        return {"system": self.system}
