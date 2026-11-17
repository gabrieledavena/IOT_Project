"""Interventi di manutenzione: richiesta del cliente, gestione da parte dello staff e report stampabile.

Ciclo di vita: il cliente chiede un intervento per un impianto con un guasto, probabile o confermato (richiesta inoltrata);
un membro dello staff ne stabilisce il tipo e la accetta, prendendola in carico (richiesta accettata); solo lui
può poi registrarne l'esecuzione (intervento eseguito).
"""
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Count, F
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views import View

from .access import AccessError, TechnicianRequiredMixin, visible_system
from .forms import InterventionAcceptForm, InterventionCompletionForm, InterventionRequestForm, intervention_date_range
from .models import Customer, Intervention
from .sorting import Ordering, person_name

Status = Intervention.Status


def interventions_with_details():
    return Intervention.objects.select_related("system__community__city", "system__community__owner", "staff", "requested_by")


class InterventionRequestView(LoginRequiredMixin, View):
    """Il cliente chiede un intervento per un impianto della sua community con un guasto, probabile o confermato."""

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
            if system.has_fault:
                messages.info(request, "C'è già un intervento per il guasto di questo impianto.")
            else:
                messages.info(request, "L'impianto non ha un guasto: non serve richiedere un intervento.")
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


class InterventionDashboardView(TechnicianRequiredMixin, View):
    """Dashboard dello staff: richieste da accettare, interventi in corso ed eseguiti."""

    template_name = "SP/intervention_dashboard.html"
    per_page = 20
    # Ordinamenti delle richieste e degli interventi in corso: prima il giorno richiesto più vicino
    OPEN_ORDERINGS = {
        "date": ("Giorno richiesto più vicino", ("preferred_date", "requested_at")),
        "date_desc": ("Giorno richiesto più lontano", ("-preferred_date", "-requested_at")),
        "requested_desc": ("Richiesta più recente", ("-requested_at",)),
        "requested": ("Richiesta meno recente", ("requested_at",)),
        "system": ("Impianto (A-Z)", ("system__name", "preferred_date")),
        "community": ("Community (A-Z)", ("system__community__name", "system__name", "preferred_date")),
    }
    REQUESTER_ORDERING = {"requested_by": ("Richiesto da (A-Z)", (person_name("requested_by"), "preferred_date"))}
    STAFF_ORDERING = {"staff": ("Tecnico (A-Z)", (person_name("staff"), "preferred_date"))}
    TABS = {
        "pending": (Status.REQUESTED, "Da accettare", {**OPEN_ORDERINGS, **REQUESTER_ORDERING}),
        "accepted": (Status.ACCEPTED, "In corso", {**OPEN_ORDERINGS, **STAFF_ORDERING}),
        "done": (Status.DONE, "Eseguiti", {
            "executed_desc": ("Eseguito più di recente", ("-executed_on", "-requested_at")),
            "executed": ("Eseguito meno di recente", ("executed_on", "requested_at")),
            "cost_desc": ("Costo più alto", (F("cost").desc(nulls_last=True), "-executed_on")),
            "cost": ("Costo più basso", (F("cost").asc(nulls_last=True), "-executed_on")),
            "system": ("Impianto (A-Z)", ("system__name", "-executed_on")),
            "community": ("Community (A-Z)", ("system__community__name", "system__name", "-executed_on")),
            "staff": ("Tecnico (A-Z)", (person_name("staff"), "-executed_on")),
        }),
    }

    def get(self, request):
        tab = request.GET.get("tab") if request.GET.get("tab") in self.TABS else "pending"
        status, _, orderings = self.TABS[tab]
        ordering = Ordering(request, orderings)
        # Le richieste da accettare non hanno ancora un responsabile
        mine = request.GET.get("mine") == "1" and status != Status.REQUESTED
        interventions = ordering.apply(interventions_with_details().filter(status=status))
        if mine:
            interventions = interventions.filter(staff=request.user)

        today = timezone.localdate()
        counts = dict(Intervention.objects.values_list("status").annotate(Count("id")))
        open_interventions = Intervention.objects.exclude(status=Status.DONE)
        context = {
            "tab": tab,
            "mine": mine,
            "ordering": ordering,
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


class InterventionDetailView(TechnicianRequiredMixin, View):
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
