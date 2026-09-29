from django.contrib import admin

from .models import City, Community, Customer, Intervention, PanelData, PhotovoltaicSystem


@admin.register(City)
class CityAdmin(admin.ModelAdmin):
    list_display = ("name", "province", "region", "latitude", "longitude")
    list_filter = ("region",)
    search_fields = ("name", "province")


@admin.register(Community)
class CommunityAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "city")
    list_select_related = ("city",)
    # Le città sono migliaia: meglio la ricerca di un menu a tendina
    autocomplete_fields = ("city",)


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "surname", "community")
    list_select_related = ("community",)


@admin.register(PhotovoltaicSystem)
class PhotovoltaicSystemAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "max_power", "community")
    list_select_related = ("community",)


@admin.register(PanelData)
class PanelDataAdmin(admin.ModelAdmin):
    list_display = ("id", "system", "time_stamp")
    list_select_related = ("system",)


@admin.register(Intervention)
class InterventionAdmin(admin.ModelAdmin):
    list_display = ("id", "system", "date", "code")
    list_select_related = ("system",)
