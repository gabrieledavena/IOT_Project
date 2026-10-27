"""Pagina delle previsioni di produzione della community: oggi, domani, prossimi 7 e 30 giorni."""
import math

from django.contrib.auth.decorators import login_required
from django.db.models import Sum
from django.shortcuts import render
from django.utils import dateformat

from SP.access import NOT_A_CUSTOMER
from SP.models import Customer

from .predictor import MONTH_DAYS, SOURCE_HISTORY, TYPICAL_YEARS, WEEK_DAYS, ForecastError, forecast_community_days

TEMPLATE = "forecast/forecast.html"


def weather_icon(day):
    """Icona (Font Awesome) del meteo previsto in un giorno."""
    if day["snowfall"] > 0:
        return "fa-snowflake text-info"
    if day["precipitation"] >= 1:
        return "fa-cloud-rain text-primary"
    if day["cloud_cover"] < 25:
        return "fa-sun text-warning"
    if day["cloud_cover"] < 70:
        return "fa-cloud-sun text-warning"
    return "fa-cloud text-secondary"


def rounded(value):
    """Numero per il grafico: i valori mancanti (NaN) non sono JSON valido e diventano null."""
    return None if math.isnan(value) else round(value, 2)


@login_required
def forecast_view(request):
    community = Customer.community_of(request.user)
    if community is None:
        return render(request, TEMPLATE, {"error": NOT_A_CUSTOMER}, status=403)

    context = {
        "community": community,
        "total_power": community.photovoltaic_systems.aggregate(total=Sum("max_power"))["total"],
    }
    try:
        forecast = forecast_community_days(community, MONTH_DAYS)
    except ForecastError as error:
        context["error"] = str(error)
        return render(request, TEMPLATE, context)

    days = forecast.to_dict("records")
    history = [day for day in days if day["source"] == SOURCE_HISTORY]
    week = days[:WEEK_DAYS]
    context.update(
        today=days[0],
        tomorrow=days[1],
        week_total=sum(day["production"] for day in week),
        month_total=sum(day["production"] for day in days),
        history_total=sum(day["production"] for day in history),
        history_start=history[0]["Date"] if history else None,
        week=[{**day, "icon": weather_icon(day)} for day in week],
        last_day=days[-1]["Date"],
        typical_years=TYPICAL_YEARS,
        chart_data=[
            {
                "label": dateformat.format(day["Date"], "D d/m"),
                "production": rounded(day["production"]),
                "low": rounded(day["production_low"]),
                "high": rounded(day["production_high"]),
                "history": day["source"] == SOURCE_HISTORY,
            }
            for day in days
        ],
    )
    return render(request, TEMPLATE, context)
