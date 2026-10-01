// Calendario della richiesta di intervento: sostituisce il campo data del browser con un calendario sempre
// visibile, che rispetta gli stessi limiti (attributi min e max). Senza flatpickr resta il campo del browser.
(function () {
    const input = document.getElementById('id_preferred_date');
    const chosen = document.getElementById('chosen-day');
    if (!input || typeof flatpickr === 'undefined') return;

    function showChosen(day) {
        chosen.classList.remove('text-danger');
        chosen.textContent = day
            ? `Giorno scelto: ${day.toLocaleDateString('it-IT', {weekday: 'long', day: 'numeric', month: 'long', year: 'numeric'})}`
            : '';
    }

    const calendar = flatpickr(input, {
        inline: true,
        appendTo: document.getElementById('intervention-calendar'),
        locale: flatpickr.l10ns.it,
        dateFormat: 'Y-m-d',
        minDate: input.min,
        maxDate: input.max,
        disableMobile: true,
        onChange: dates => showChosen(dates[0]),
    });
    // flatpickr rende il campo di testo: resta nel form per inviare il giorno, ma si vede solo il calendario
    input.classList.add('d-none');
    showChosen(calendar.selectedDates[0]);

    // Il giorno è obbligatorio: senza, il form non parte e il messaggio lo dice
    input.form.addEventListener('submit', event => {
        if (!input.value) {
            event.preventDefault();
            chosen.textContent = 'Scegli un giorno sul calendario.';
            chosen.classList.add('text-danger');
        }
    });
})();
