from django.contrib import admin
from .models import (
    Community,
    Customer,
    PhotovoltaicSystem,
    PanelData,
    Intervention,
    City
)

class CommunityAdmin(admin.ModelAdmin):
    list_display = ("id", "name")

class CustomerAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "surname", "community")

class CityAdmin(admin.ModelAdmin):
    list_display = ("name"
    ,"province"
    ,"region"
    ,"latitude"
    ,"longitude")

class PhotovoltaicSystemAdmin(admin.ModelAdmin):
    list_display = ("id", "name")

class PanelDataAdmin(admin.ModelAdmin):
    list_display = ("id", "system_name", "time_stamp")

    def system_name(self, obj):
        return obj.system.name

    system_name.short_description = "System"

class InterventionAdmin(admin.ModelAdmin):
    list_display = ("id", "system_name", "date", "code")

    def system_name(self, obj):
        return obj.system.name

    system_name.short_description = "System"

admin.site.register(Community, CommunityAdmin)
admin.site.register(Customer, CustomerAdmin)
admin.site.register(PhotovoltaicSystem, PhotovoltaicSystemAdmin)
admin.site.register(PanelData, PanelDataAdmin)
admin.site.register(Intervention, InterventionAdmin)
admin.site.register(City, CityAdmin)
