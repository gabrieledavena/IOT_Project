"""Interventi di manutenzione: richiesta del cliente, gestione da parte dello staff e report stampabile.

Ciclo di vita: il cliente chiede un intervento per un impianto con un guasto, probabile o confermato (richiesta inoltrata);
un membro dello staff ne stabilisce il tipo e la accetta, prendendola in carico (richiesta accettata); solo lui
può poi registrarne l'esecuzione (intervento eseguito), e con l'intervento eseguito l'impianto torna OK.

Il tecnico contatta il titolare della community dalla sua pagina, per telefono o per email (recapiti obbligatori
per il titolare), e si accorda con lui se non è disponibile il giorno richiesto. Il nuovo giorno lo indica accettando
la richiesta, o più tardi se cambia ancora: il titolare lo riceve sul sito e su Telegram (nuova data proposta) e
lo approva, e l'intervento resta in carico al tecnico con il nuovo giorno, o lo rifiuta, e la richiesta è annullata:
per avere un tecnico ne apre una nuova.
"""
from datetime import date, timedelta

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, F
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views import View

from . import mqtt, telegram
from .access import AccessError, TechnicianRequiredMixin, visible_system
from .forms import (
    DateProposalForm, InterventionAcceptForm, InterventionCompletionForm, InterventionRequestForm,
    intervention_date_range, owner_contacts_missing, request_unavailable,
)
from .models import Customer, Intervention, PhotovoltaicSystem, StaffProfile
from .sorting import Ordering, person_name

Status = Intervention.Status


def interventions_with_details():
    return Intervention.objects.select_related(
        "system__community__city", "system__community__owner__user", "staff", "requested_by__customer",
    )


def system_repaired(system):
    """L'intervento eseguito ha riparato l'impianto: lo stato torna OK e lo riceve il dispositivo.

    Vale come un controllo (last_check), come lo stato impostato a mano (SP/installations.py): il prossimo
    controllo automatico arriva dopo due giorni, sulla produzione di dopo la riparazione. Se trova di nuovo un
    guasto, è un guasto nuovo e si può chiedere un altro intervento (PhotovoltaicSystem.can_request_intervention).
    """
    system.set_status(PhotovoltaicSystem.Status.OK, timezone.now())
    transaction.on_commit(lambda: mqtt.publish_status([system]))


def owned_intervention(user, pk):
    """Intervento di un impianto della community di cui l'utente è titolare: lui risponde al giorno proposto.

    Solleva AccessError se l'intervento non c'è o l'utente non è il titolare della sua community.
    """
    intervention = interventions_with_details().filter(pk=pk).first()
    if intervention is None:
        raise AccessError("Intervento non trovato.", 404)
    owner = intervention.system.community.owner
    if owner is None or owner.user_id != user.id:
        raise AccessError("Solo il titolare della community risponde al giorno proposto dal tecnico.", 403)
    return intervention


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

        unavailable = request_unavailable(system)
        if unavailable:
            messages.info(request, unavailable)
            return redirect("SP:photovoltaic_system", system_id=system.id)
        # Il tecnico deve poter contattare il titolare: chi lo è da prima che i recapiti fossero obbligatori li aggiunge
        missing = owner_contacts_missing(system.community, request.user)
        if missing:
            messages.warning(request, missing)
            if system.community.owner.user_id == request.user.id:
                return redirect("SP:profile")
            return redirect("SP:photovoltaic_system", system_id=system.id)

        if form.is_bound and form.is_valid():
            intervention = form.save_request(system, request.user)
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
    """Dashboard dello staff: richieste da accettare, interventi in corso (anche quelli con una nuova data in attesa
    del titolare), eseguiti e annullati (il titolare ha rifiutato la nuova data)."""

    template_name = "SP/intervention_dashboard.html"
    per_page = 20
    # Ordinamenti delle richieste e degli interventi in corso: prima il giorno dell'intervento più vicino
    OPEN_ORDERINGS = {
        "date": ("Giorno più vicino", ("scheduled_date", "requested_at")),
        "date_desc": ("Giorno più lontano", ("-scheduled_date", "-requested_at")),
        "requested_desc": ("Richiesta più recente", ("-requested_at",)),
        "requested": ("Richiesta meno recente", ("requested_at",)),
        "system": ("Impianto (A-Z)", ("system__name", "scheduled_date")),
        "community": ("Community (A-Z)", ("system__community__name", "system__name", "scheduled_date")),
    }
    REQUESTER_ORDERING = {"requested_by": ("Richiesto da (A-Z)", (person_name("requested_by"), "scheduled_date"))}
    STAFF_ORDERING = {"staff": ("Tecnico (A-Z)", (person_name("staff"), "scheduled_date"))}
    # Scheda: stati degli interventi, nome e ordinamenti
    TABS = {
        "pending": ((Status.REQUESTED,), "Da accettare", {**OPEN_ORDERINGS, **REQUESTER_ORDERING}),
        "accepted": ((Status.ACCEPTED, Status.PROPOSED), "In corso", {**OPEN_ORDERINGS, **STAFF_ORDERING}),
        "done": ((Status.DONE,), "Eseguiti", {
            "executed_desc": ("Eseguito più di recente", ("-executed_on", "-requested_at")),
            "executed": ("Eseguito meno di recente", ("executed_on", "requested_at")),
            "cost_desc": ("Costo più alto", (F("cost").desc(nulls_last=True), "-executed_on")),
            "cost": ("Costo più basso", (F("cost").asc(nulls_last=True), "-executed_on")),
            "system": ("Impianto (A-Z)", ("system__name", "-executed_on")),
            "community": ("Community (A-Z)", ("system__community__name", "system__name", "-executed_on")),
            "staff": ("Tecnico (A-Z)", (person_name("staff"), "-executed_on")),
        }),
        "cancelled": ((Status.CANCELLED,), "Annullati", {
            "answered_desc": ("Annullato più di recente", (F("answered_at").desc(nulls_last=True), "-requested_at")),
            "answered": ("Annullato meno di recente", (F("answered_at").asc(nulls_last=True), "requested_at")),
            "system": ("Impianto (A-Z)", ("system__name", "-answered_at")),
            "community": ("Community (A-Z)", ("system__community__name", "system__name", "-answered_at")),
            "staff": ("Tecnico (A-Z)", (person_name("staff"), "-answered_at")),
        }),
    }

    def get(self, request):
        tab = request.GET.get("tab") if request.GET.get("tab") in self.TABS else "pending"
        statuses, _, orderings = self.TABS[tab]
        ordering = Ordering(request, orderings)
        # Le richieste da accettare non hanno ancora un responsabile
        mine = request.GET.get("mine") == "1" and tab != "pending"
        interventions = ordering.apply(interventions_with_details().filter(status__in=statuses))
        if mine:
            interventions = interventions.filter(staff=request.user)

        today = timezone.localdate()
        counts = dict(Intervention.objects.values_list("status").annotate(Count("id")))
        open_interventions = Intervention.objects.filter(status__in=Intervention.OPEN_STATUSES)
        context = {
            "tab": tab,
            "mine": mine,
            "ordering": ordering,
            "tabs": [(key, label, sum(counts.get(status, 0) for status in tab_statuses))
                     for key, (tab_statuses, label, _) in self.TABS.items()],
            "page": Paginator(interventions, self.per_page).get_page(request.GET.get("page")),
            "today": today,
            "pending_count": counts.get(Status.REQUESTED, 0),
            "my_open_count": open_interventions.filter(staff=request.user).count(),
            "proposed_count": counts.get(Status.PROPOSED, 0),
            "overdue_count": open_interventions.filter(scheduled_date__lt=today).count(),
            "done_last_30_days": Intervention.objects.filter(
                status=Status.DONE, executed_on__gt=today - timedelta(days=30)
            ).count(),
        }
        return render(request, self.template_name, context)


class InterventionDetailView(TechnicianRequiredMixin, View):
    """Dettaglio per lo staff: i recapiti del titolare per accordarsi sul giorno, l'accettazione della richiesta e,
    per chi l'ha in carico, la proposta di un nuovo giorno e la registrazione dell'intervento eseguito.

    La pagina ha due form: quello principale (accettazione o esecuzione) e la proposta di un nuovo giorno, che si
    invia con action=propose.
    """

    template_name = "SP/intervention_detail.html"
    PROPOSE = "propose"

    def get(self, request, pk):
        intervention = get_object_or_404(interventions_with_details(), pk=pk)
        return self.render(request, intervention, *self.forms_for(request, intervention))

    def post(self, request, pk):
        intervention = get_object_or_404(interventions_with_details(), pk=pk)
        proposing = request.POST.get("action") == self.PROPOSE
        form, proposal_form = self.forms_for(request, intervention, request.POST, proposing)
        submitted = proposal_form if proposing else form
        if submitted is None:
            # Intervento già eseguito o annullato, in carico a un altro membro dello staff o in attesa del titolare
            raise PermissionDenied
        if not submitted.is_valid():
            return self.render(request, intervention, form, proposal_form)

        if proposing:
            self.propose(request, intervention, proposal_form.cleaned_data["day"])
        elif intervention.status == Status.REQUESTED:
            self.accept(request, intervention, form)
        else:
            completed = form.save(commit=False)
            completed.status = Status.DONE
            with transaction.atomic():
                completed.save()
                system_repaired(completed.system)
            messages.success(request, "Intervento registrato come eseguito: l'impianto è tornato OK e ora puoi "
                                      "stamparne il report.")
        return redirect("SP:intervention_detail", pk=pk)

    @staticmethod
    def accept(request, intervention, form):
        """Accetta la richiesta, nel giorno richiesto o in quello concordato con il titolare."""
        pk, code, day = intervention.pk, form.cleaned_data["code"], form.cleaned_data["day"]
        # Si accetta solo se nessun altro l'ha accettata nel frattempo
        if form.proposes_new_day:
            accepted = Intervention.propose(pk, request.user, day, code=code)
        else:
            accepted = Intervention.objects.filter(pk=pk, status=Status.REQUESTED).update(
                status=Status.ACCEPTED, staff=request.user, code=code
            )
        if not accepted:
            messages.warning(request, "Un altro membro dello staff ha appena accettato questa richiesta.")
        elif form.proposes_new_day:
            # Il titolare riceve la nuova data da approvare su Telegram; sul sito la trova nelle sue pagine
            transaction.on_commit(lambda: telegram.notify_proposal(pk))
            messages.success(request, f"Richiesta accettata: l'intervento è in carico a te. Il titolare deve approvare "
                                      f"il nuovo giorno, il {day:%d/%m/%Y}.")
        else:
            # Il titolare della community riceve su Telegram l'avviso che la richiesta è stata accettata
            transaction.on_commit(lambda: telegram.notify_accepted(pk))
            messages.success(request, "Richiesta accettata: l'intervento è in carico a te.")

    @staticmethod
    def propose(request, intervention, day):
        pk = intervention.pk
        if Intervention.propose(pk, request.user, day):
            transaction.on_commit(lambda: telegram.notify_proposal(pk))
            messages.success(request, f"Nuovo giorno inviato al titolare: il {day:%d/%m/%Y}, se lo approva.")
        else:
            messages.warning(request, "L'intervento è cambiato nel frattempo: il nuovo giorno non è stato inviato.")

    def forms_for(self, request, intervention, data=None, proposing=False):
        """I form che l'utente può compilare in questo stato dell'intervento, o None: (principale, proposta).

        Si lega ai dati inviati solo quello inviato, l'altro resta com'era.
        """
        main_data, proposal_data = (None, data) if proposing else (data, None)
        in_charge = intervention.staff_id == request.user.id
        form = proposal_form = None
        if intervention.status == Status.REQUESTED:
            form = InterventionAcceptForm(main_data, instance=intervention)
        elif intervention.status == Status.ACCEPTED and in_charge:
            form = InterventionCompletionForm(main_data, instance=intervention)
        if intervention.status in (Status.ACCEPTED, Status.PROPOSED) and in_charge:
            proposal_form = DateProposalForm(proposal_data, intervention=intervention)
        return form, proposal_form

    def render(self, request, intervention, form, proposal_form):
        owner = intervention.system.community.owner
        requester = getattr(intervention.requested_by, "customer", None) if intervention.requested_by else None
        context = {
            "intervention": intervention,
            "system": intervention.system,
            "form": form,
            "proposal_form": proposal_form,
            "owner": owner,
            # Chi ha fatto la richiesta, se è un altro utente della community
            "requester": requester if requester is not None and requester != owner else None,
            "history": intervention.system.interventions.exclude(pk=intervention.pk).select_related("staff")[:5],
        }
        return render(request, self.template_name, context)


class InterventionProposalView(LoginRequiredMixin, View):
    """Il titolare risponde al nuovo giorno proposto dal tecnico: lo approva o lo rifiuta, annullando la richiesta.

    La stessa risposta si dà dai bottoni del messaggio Telegram (SP/telegram.py). Il giorno arriva con il form: se
    nel frattempo il tecnico ne ha proposto un altro, la risposta non vale per quello nuovo.
    """

    template_name = "SP/intervention_proposal.html"

    def get(self, request, pk):
        try:
            intervention = owned_intervention(request.user, pk)
        except AccessError as error:
            return render(request, self.template_name, {"error": error.message}, status=error.status)
        staff_phone = StaffProfile.objects.filter(user_id=intervention.staff_id).values_list("phone", flat=True).first()
        context = {"intervention": intervention, "system": intervention.system, "staff_phone": staff_phone}
        return render(request, self.template_name, context)

    def post(self, request, pk):
        try:
            intervention = owned_intervention(request.user, pk)
        except AccessError as error:
            return render(request, self.template_name, {"error": error.message}, status=error.status)
        answer = request.POST.get("answer")
        try:
            day = date.fromisoformat(request.POST.get("day", ""))
        except ValueError:
            day = None
        if answer not in ("approve", "reject") or day is None:
            messages.error(request, "Scegli se approvare o rifiutare il nuovo giorno.")
            return redirect("SP:intervention_proposal", pk=pk)

        system = intervention.system
        if not Intervention.answer_proposal(pk, day, approved=answer == "approve"):
            messages.info(request, "Questa proposta non è più in attesa di risposta: ecco lo stato dell'intervento.")
            return redirect("SP:intervention_proposal", pk=pk)
        if answer == "approve":
            messages.success(request, f"Nuovo giorno approvato: l'intervento su {system.name} è il {day:%d/%m/%Y}.")
        else:
            messages.info(request, f"Nuovo giorno rifiutato: la richiesta di intervento su {system.name} è annullata. "
                                   "Se serve ancora un tecnico, aprine una nuova.")
        return redirect("SP:photovoltaic_system", system_id=system.id)


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
