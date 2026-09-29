"""Pagina di previsione della produzione della community, per oggi o per domani."""
from datetime import date, timedelta

from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from SP.models import Customer

from .predictor import ForecastError, forecast_community_production

DAY_LABELS = {0: "oggi", 1: "domani"}


@login_required
def forecast_view(request, days_ahead):
    day = date.today() + timedelta(days=days_ahead)
    context = {"day": day, "day_label": DAY_LABELS[days_ahead]}

    if request.method == "POST":
        community = Customer.community_of(request.user)
        if community is None:
            context["error"] = "Utente non associato a nessuna community."
        else:
            try:
                production, weather = forecast_community_production(community, day)
            except ForecastError as error:
                context["error"] = str(error)
            else:
                context.update(prediction=round(production, 3), weather=weather, community=community)

    return render(request, "forecast/forecast.html", context)
