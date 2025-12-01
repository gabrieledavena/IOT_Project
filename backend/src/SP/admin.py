from django.contrib import admin

# Register your models here.
from django.contrib import admin
from .models import (
    Community,
    User,
    PhotovoltaicSystem,
    PanelData,
    Intervention,
)  # importa i tuoi modelli


class CommunityAdmin(admin.ModelAdmin):
    list_display = ("id", "name")


class UserAdmin(admin.ModelAdmin):
    list_display = ("id", "name")


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
admin.site.register(User, UserAdmin)
admin.site.register(PhotovoltaicSystem, PhotovoltaicSystemAdmin)
admin.site.register(PanelData, PanelDataAdmin)
admin.site.register(Intervention, InterventionAdmin)
