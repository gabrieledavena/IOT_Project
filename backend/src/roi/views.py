"""Calcolo del ROI e preventivo stampabile, usati dal personale per le consulenze ai clienti."""
import hashlib
from datetime import date

import pandas as pd
from django.contrib.auth.mixins import UserPassesTestMixin
from django.shortcuts import redirect, render
from django.views import View

from forecast.predictor import ForecastError, estimate_past_year_yield

from .calculator import calculate_roi
from .forms import RoiForm

MONTHS = ["Gen", "Feb", "Mar", "Apr", "Mag", "Giu", "Lug", "Ago", "Set", "Ott", "Nov", "Dic"]


class StaffRequiredMixin(UserPassesTestMixin):
    """Solo staff e amministratori: gli altri utenti ricevono 403, i visitatori vanno al login."""

    def test_func(self):
        return self.request.user.is_staff or self.request.user.is_superuser


def simulate(form):
    """Stima la produzione con il meteo reale degli ultimi 365 giorni e calcola il ROI.

    Solleva ForecastError se il meteo o il modello di previsione non sono disponibili.
    """
    data = form.cleaned_data
    daily = estimate_past_year_yield(data["city"])
    # Se l'archivio meteo non ha ancora gli ultimi giorni, la resa viene riportata a un anno intero
    scale = 365 / len(daily)
    specific_yield = daily["specific_yield"].sum() * scale
    monthly_yield = daily.groupby(pd.to_datetime(daily["Date"]).dt.month)["specific_yield"].sum() * scale

    power = data["peak_power_kw"]
    result = calculate_roi(
        annual_production_kwh=specific_yield * power,
        annual_consumption_kwh=data["annual_consumption_kwh"],
        system_cost=data["system_cost"],
        energy_price=data["energy_price"],
        assumptions=form.assumptions(),
    )
    monthly_production = [(MONTHS[month - 1], monthly_yield.get(month, 0) * power) for month in range(1, 13)]
    coverage = result.consumption_coverage
    return {
        "result": result,
        "assumptions": form.assumptions(),
        "roi_percent": result.roi * 100,
        "coverage_percent": None if coverage is None else coverage * 100,
        "specific_yield": specific_yield,
        "monthly_production": monthly_production,
        "weather_first_day": daily["Date"].min(),
        "weather_last_day": daily["Date"].max(),
        "weather_days": len(daily),
        "chart_data": {
            "months": [month for month, _ in monthly_production],
            "production": [round(kwh, 1) for _, kwh in monthly_production],
            "years": [year.year for year in result.years],
            "cumulative": [round(year.cumulative_cash_flow, 2) for year in result.years],
        },
    }


class RoiView(StaffRequiredMixin, View):
    template_name = "roi/roi.html"

    def get(self, request):
        return calculator_page(request, RoiForm())

    def post(self, request):
        # I dati del cliente arrivano in POST: non finiscono nell'URL, nella cronologia o nei log
        return calculator_page(request, RoiForm(request.POST))


def calculator_page(request, form):
    """Pagina del calcolo: il form e, se i dati inviati sono validi, i risultati della simulazione."""
    context = {"form": form}
    if form.is_bound and form.is_valid():
        try:
            context.update(simulate(form))
        except ForecastError as error:
            context["error"] = str(error)
    return render(request, RoiView.template_name, context)


class QuoteView(StaffRequiredMixin, View):
    """Preventivo pronto da stampare (o da salvare in PDF dal browser) con gli stessi dati del calcolo.

    Riceve i dati in POST dal pulsante "Stampa preventivo" della pagina del calcolo.
    """

    template_name = "roi/quote.html"

    def get(self, request):
        return redirect("roi:calculator")

    def post(self, request):
        form = RoiForm(request.POST)
        if not form.is_valid():
            return calculator_page(request, form)
        try:
            simulation = simulate(form)
        except ForecastError:
            # La pagina del calcolo mostra il motivo
            return calculator_page(request, form)

        today = date.today()
        # Stesso numero per gli stessi dati nello stesso giorno
        submitted = "&".join(f"{name}={form.data.get(name, '')}" for name in form.fields)
        digest = hashlib.sha1(submitted.encode()).hexdigest()[:6].upper()
        context = {
            **simulation,
            "form": form,
            "data": form.cleaned_data,
            "today": today,
            "quote_number": f"SF-{today:%Y%m%d}-{digest}",
        }
        return render(request, self.template_name, context)
