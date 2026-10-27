from django import forms
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.forms import AdminUserCreationForm
from django.contrib.auth.models import Group, User
from django.db import transaction
from django.utils import timezone

from . import mqtt
from .models import City, Community, Customer, Device, Intervention, PanelData, PhotovoltaicSystem
from .roles import ROLES, TECHNICIANS


class StaffUserCreationForm(AdminUserCreationForm):
    role = forms.ChoiceField(
        label="Ruolo", required=False, choices=[("", "Nessuno"), *ROLES.items()],
        help_text="I tecnici installano e gestiscono gli impianti, anche nell'admin; i consulenti preparano i "
                  "preventivi. I clienti si registrano da soli dal sito.",
    )


admin.site.unregister(User)


@admin.register(User)
class StaffUserAdmin(UserAdmin):
    """Utenti: solo gli amministratori (superuser) li creano e assegnano i ruoli, cioè i gruppi Tecnici e Consulenti.

    L'accesso all'admin (is_staff) segue il ruolo: ce l'hanno gli amministratori e i tecnici, non i consulenti.
    """

    add_form = StaffUserCreationForm
    add_fieldsets = ((None, {"classes": ("wide",), "fields": (*UserAdmin.add_fieldsets[0][1]["fields"], "role")}),)
    fieldsets = (
        *UserAdmin.fieldsets[:2],
        ("Ruolo e permessi", {
            "description": "Il ruolo è il gruppo: Tecnici o Consulenti. L'accesso all'admin (staff) lo hanno gli "
                           "amministratori e i tecnici: si aggiorna da solo al salvataggio.",
            "fields": ("is_active", "is_superuser", "groups", "is_staff"),
        }),
        *UserAdmin.fieldsets[3:],
    )
    readonly_fields = ("is_staff",)
    list_display = ("username", "first_name", "last_name", "email", "role")
    list_filter = ("groups", "is_superuser", "is_active")

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("customer").prefetch_related("groups")

    @admin.display(description="Ruolo")
    def role(self, user):
        if user.is_superuser:
            return "Amministratore"
        roles = [ROLES[group.name] for group in user.groups.all() if group.name in ROLES]
        if roles:
            return ", ".join(roles)
        return "Cliente" if hasattr(user, "customer") else "-"

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        user = form.instance
        if form.cleaned_data.get("role"):
            user.groups.add(Group.objects.get(name=form.cleaned_data["role"]))
        is_staff = user.is_superuser or user.groups.filter(name=TECHNICIANS).exists()
        if user.is_staff != is_staff:
            user.is_staff = is_staff
            user.save(update_fields=["is_staff"])


@admin.register(City)
class CityAdmin(admin.ModelAdmin):
    list_display = ("name", "province", "region", "latitude", "longitude")
    list_filter = ("region",)
    search_fields = ("name", "province")


@admin.register(Community)
class CommunityAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "city", "owner")
    list_select_related = ("city", "owner")
    # Le città sono migliaia: meglio la ricerca di un menu a tendina
    autocomplete_fields = ("city",)

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        # Il titolare si sceglie tra gli utenti della community; una community nuova non ne ha ancora:
        # il primo utente aggiunto ne diventerà titolare
        form.base_fields["owner"].queryset = obj.customers.all() if obj else Customer.objects.none()
        return form


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "surname", "community")
    list_select_related = ("community",)
    search_fields = ("name", "surname", "user__username")


@admin.register(PhotovoltaicSystem)
class PhotovoltaicSystemAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "max_power", "community", "status", "last_check")
    list_select_related = ("community",)
    list_filter = ("status",)
    # Aggiornati da save_model quando cambia lo stato
    readonly_fields = ("previous_status", "status_changed_at")

    def save_model(self, request, obj, form, change):
        if change and "status" in form.changed_data:
            # Un cambiamento di stato come quelli del controllo automatico: il dispositivo lo riconosce
            # dalla data e reagisce (con DRT e FLT aziona la pompa)
            obj.previous_status, obj.status_changed_at = form.initial["status"], timezone.now()
        super().save_model(request, obj, form, change)
        # Il dispositivo dell'impianto riceve subito i dati aggiornati, come la potenza massima
        if change:
            transaction.on_commit(lambda: mqtt.publish_state([obj]))


@admin.register(Device)
class DeviceAdmin(admin.ModelAdmin):
    """Solo consultazione: installazione e credenziali si gestiscono dalla pagina Installazioni."""

    list_display = ("username", "system", "installed_by", "installed_at", "online", "last_seen", "last_cleaning")
    list_select_related = ("system", "installed_by")
    list_filter = ("online",)
    readonly_fields = ("system", "installed_by", "installed_at", "online", "last_seen", "pump_running", "last_cleaning")
    exclude = ("token_hash",)

    def has_add_permission(self, request):
        return False


@admin.register(PanelData)
class PanelDataAdmin(admin.ModelAdmin):
    list_display = ("id", "system", "time_stamp")
    list_select_related = ("system",)


@admin.register(Intervention)
class InterventionAdmin(admin.ModelAdmin):
    list_display = ("id", "system", "status", "requested_at", "preferred_date", "staff", "code", "executed_on")
    list_select_related = ("system", "staff")
    list_filter = ("status", "code")
