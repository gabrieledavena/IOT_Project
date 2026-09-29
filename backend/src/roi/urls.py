from django.urls import path

from .views import QuoteView, RoiView

app_name = "roi"

urlpatterns = [
    path("", RoiView.as_view(), name="calculator"),
    path("quote/", QuoteView.as_view(), name="quote"),
]
