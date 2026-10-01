"""Interventi di manutenzione: richiesta del cliente, gestione da parte dello staff e report stampabile.

Ciclo di vita: il cliente chiede un intervento per un impianto con un probabile guasto (richiesta inoltrata);
un membro dello staff ne stabilisce il tipo e la accetta, prendendola in carico (richiesta accettata); solo lui
può poi registrarne l'esecuzione (intervento eseguito).
"""
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Count
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views import View

from .access import AccessError, StaffRequiredMixin, visible_system
from .forms import InterventionAcceptForm, InterventionCompletionForm, InterventionRequestForm, intervention_date_range
from .models import Customer, Intervention, PhotovoltaicSystem

Status = Intervention.Status


def interventions_with_details():
    return Intervention.objects.select_related("system__community__city", "system__community__owner", "staff", "requested_by")


class InterventionRequestView(LoginRequiredMixin, View):
    """Il cliente chiede un intervento per un impianto della sua community con un probabile guasto."""

    template_name = "SP/intervention_request.html"

    def get(self, request, system_id):
        return self.handle(request, system_id, InterventionRequestForm())

    def post(self, request, system_id):
        return self.handle(request, system_id, InterventionRequestForm(request.POST))

    def handle(self, request, system_id, form):
        try:
            system = visible_system(request.user, system_id)
            if Customer.community_of(request.user) != system.community:
                raise AccessError("Solo gli utenti della community possono richiedere un intervento per l'impianto.", 403)
        except AccessError as error:
            return render(request, self.template_name, {"error": error.message}, status=error.status)

        if not system.can_request_intervention:
            if system.status == PhotovoltaicSystem.Status.FAULT:
                messages.info(request, "C'è già un intervento per il guasto di questo impianto.")
            else:
                messages.info(request, "L'impianto non ha un probabile guasto: non serve richiedere un intervento.")
            return redirect("SP:photovoltaic_system", system_id=system.id)

        if form.is_bound and form.is_valid():
            intervention = form.save(commit=False)
            intervention.system, intervention.requested_by = system, request.user
            intervention.save()
            messages.success(
                request,
                f"Richiesta di intervento inoltrata per il {intervention.preferred_date:%d/%m/%Y}: "
                "la vedrai accettata qui quando lo staff la prenderà in carico.",
            )
            return redirect("SP:photovoltaic_system", system_id=system.id)

        first_day, last_day = intervention_date_range()
        context = {"system": system, "form": form, "first_day": first_day, "last_day": last_day}
        return render(request, self.template_name, context)


class InterventionDashboardView(StaffRequiredMixin, View):
    """Dashboard dello staff: richieste da accettare, interventi in corso ed eseguiti."""

    template_name = "SP/intervention_dashboard.html"
    per_page = 20
    TABS = {
        "pending": (Status.REQUESTED, "Da accettare", ("preferred_date", "requested_at")),
        "accepted": (Status.ACCEPTED, "In corso", ("preferred_date", "requested_at")),
        "done": (Status.DONE, "Eseguiti", ("-executed_on", "-requested_at")),
    }

    def get(self, request):
        tab = request.GET.get("tab") if request.GET.get("tab") in self.TABS else "pending"
        status, _, ordering = self.TABS[tab]
        # Le richieste da accettare non hanno ancora un responsabile
        mine = request.GET.get("mine") == "1" and status != Status.REQUESTED
        interventions = interventions_with_details().filter(status=status).order_by(*ordering)
        if mine:
            interventions = interventions.filter(staff=request.user)

        today = timezone.localdate()
        counts = dict(Intervention.objects.values_list("status").annotate(Count("id")))
        open_interventions = Intervention.objects.exclude(status=Status.DONE)
        context = {
            "tab": tab,
            "mine": mine,
            "tabs": [(key, label, counts.get(tab_status, 0)) for key, (tab_status, label, _) in self.TABS.items()],
            "page": Paginator(interventions, self.per_page).get_page(request.GET.get("page")),
            "today": today,
            "pending_count": counts.get(Status.REQUESTED, 0),
            "my_open_count": open_interventions.filter(staff=request.user).count(),
            "overdue_count": open_interventions.filter(preferred_date__lt=today).count(),
            "done_last_30_days": Intervention.objects.filter(
                status=Status.DONE, executed_on__gt=today - timedelta(days=30)
            ).count(),
        }
        return render(request, self.template_name, context)


class InterventionDetailView(StaffRequiredMixin, View):
    """Dettaglio per lo staff: si accetta la richiesta e, se la si ha in carico, si registra l'intervento eseguito."""

    template_name = "SP/intervention_detail.html"

    def get(self, request, pk):
        intervention = get_object_or_404(interventions_with_details(), pk=pk)
        return self.render(request, intervention, self.form_for(request, intervention))

    def post(self, request, pk):
        intervention = get_object_or_404(interventions_with_details(), pk=pk)
        form = self.form_for(request, intervention, request.POST)
        if form is None:
            # Intervento già eseguito, o in carico a un altro membro dello staff
            raise PermissionDenied
        if not form.is_valid():
            return self.render(request, intervention, form)

        if intervention.status == Status.REQUESTED:
            # Si accetta solo se nessun altro l'ha accettata nel frattempo
            accepted = Intervention.objects.filter(pk=pk, status=Status.REQUESTED).update(
                status=Status.ACCEPTED, staff=request.user, code=form.cleaned_data["code"]
            )
            if accepted:
                messages.success(request, "Richiesta accettata: l'intervento è in carico a te.")
            else:
                messages.warning(request, "Un altro membro dello staff ha appena accettato questa richiesta.")
        else:
            completed = form.save(commit=False)
            completed.status = Status.DONE
            completed.save()
            messages.success(request, "Intervento registrato come eseguito: ora puoi stamparne il report.")
        return redirect("SP:intervention_detail", pk=pk)

    @staticmethod
    def form_for(request, intervention, data=None):
        """Il form che l'utente può compilare in questo stato dell'intervento, o None."""
        if intervention.status == Status.REQUESTED:
            return InterventionAcceptForm(data, instance=intervention)
        if intervention.status == Status.ACCEPTED and intervention.staff_id == request.user.id:
            return InterventionCompletionForm(data, instance=intervention)
        return None

    def render(self, request, intervention, form):
        context = {
            "intervention": intervention,
            "system": intervention.system,
            "form": form,
            "history": intervention.system.interventions.exclude(pk=intervention.pk).select_related("staff")[:5],
        }
        return render(request, self.template_name, context)


class InterventionReportView(LoginRequiredMixin, View):
    """Report dell'intervento da stampare o salvare in PDF: per lo staff e per gli utenti della community."""

    template_name = "SP/intervention_report.html"

    def get(self, request, pk):
        intervention = get_object_or_404(interventions_with_details(), pk=pk)
        try:
            visible_system(request.user, intervention.system_id)
        except AccessError as error:
            raise (Http404 if error.status == 404 else PermissionDenied)(error.message) from error
        return render(request, self.template_name, {"intervention": intervention, "system": intervention.system,
                                                    "today": timezone.localdate()})
