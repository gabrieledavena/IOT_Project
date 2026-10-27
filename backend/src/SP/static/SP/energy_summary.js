// Sommario dell'energia prodotta per mese o per anno (home della community e pagina dell'impianto).
// Il server restituisce in JSON le barre del grafico e le statistiche del periodo (SP/energy.py):
// l'indirizzo è nell'attributo data-energy-summary del riquadro.
(function () {
    const root = document.querySelector('[data-energy-summary]');
    if (!root) return;

    const url = root.dataset.energySummary;
    const monthInput = root.querySelector('[data-summary-month]');
    const yearInput = root.querySelector('[data-summary-year]');
    const previous = root.querySelector('[data-summary-previous]');
    const next = root.querySelector('[data-summary-next]');
    const canvas = root.querySelector('[data-summary-chart]');
    const empty = root.querySelector('[data-summary-empty]');

    const number = (value, digits) => value.toLocaleString('it-IT', {maximumFractionDigits: digits});
    const kwh = value => `${number(value, 1)} kWh`;
    const FORMATS = {
        min: (value, stats) => `${kwh(value)} · ${stats.min_day}`,
        max: (value, stats) => `${kwh(value)} · ${stats.max_day}`,
        mean: kwh,
        total: kwh,
        specific_yield: value => `${number(value, 2)} kWh/kWp`,
        daily_specific_yield: value => `${number(value, 2)} kWh/kWp`,
        days: value => number(value, 0),
        co2_kg: value => `${number(value, 0)} kg`,
        trees: value => number(value, 0),
        car_km: value => `${number(value, 0)} km`,
    };

    let summary = null;
    let chart = null;

    async function load(period, value) {
        const params = new URLSearchParams({period});
        if (value) params.set('date', value);
        const response = await fetch(`${url}?${params}`, {headers: {Accept: 'application/json'}});
        if (!response.ok) return;
        summary = await response.json();
        render();
    }

    function render() {
        const monthly = summary.period === 'month';
        root.querySelector(`input[name="summary-period"][value="${summary.period}"]`).checked = true;
        monthInput.classList.toggle('d-none', !monthly);
        yearInput.classList.toggle('d-none', monthly);
        (monthly ? monthInput : yearInput).value = summary.value;
        previous.disabled = !summary.previous;
        next.disabled = !summary.next;

        root.querySelectorAll('[data-stat]').forEach(cell => {
            const value = summary.stats[cell.dataset.stat];
            cell.textContent = value === null ? '-' : FORMATS[cell.dataset.stat](value, summary.stats);
        });
        empty.classList.toggle('d-none', summary.bars.some(bar => bar.energy !== null));
        draw();
    }

    function draw() {
        const data = {
            labels: summary.bars.map(bar => bar.label),
            datasets: [{
                label: 'Energia generata',
                data: summary.bars.map(bar => bar.energy),
                backgroundColor: '#f59f00',
                borderRadius: 4,
                maxBarThickness: 32,
            }],
        };
        if (chart) {
            chart.data = data;
            chart.update();
            return;
        }
        chart = new Chart(canvas.getContext('2d'), {
            type: 'bar',
            data: data,
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: {display: false},
                    tooltip: {
                        backgroundColor: 'rgba(0, 0, 0, 0.8)',
                        padding: 12,
                        callbacks: {label: context => kwh(context.parsed.y)},
                    },
                },
                scales: {
                    x: {grid: {display: false}, ticks: {color: '#6c757d', maxRotation: 45}},
                    y: {
                        beginAtZero: true,
                        grid: {color: '#e9ecef'},
                        ticks: {color: '#6c757d', callback: value => `${number(value, 0)} kWh`},
                    },
                },
            },
        });
    }

    // Passando all'anno si resta nell'anno del mese mostrato; passando al mese si va all'ultimo mese dell'anno
    // (il server mostra il mese in corso se è nel futuro)
    root.querySelectorAll('input[name="summary-period"]').forEach(input => {
        input.addEventListener('change', () => {
            const year = summary ? summary.value.slice(0, 4) : null;
            load(input.value, year && (input.value === 'year' ? year : `${year}-12`));
        });
    });
    previous.addEventListener('click', () => summary && summary.previous && load(summary.period, summary.previous));
    next.addEventListener('click', () => summary && summary.next && load(summary.period, summary.next));
    monthInput.addEventListener('change', () => monthInput.value && load('month', monthInput.value));
    yearInput.addEventListener('change', () => yearInput.value && load('year', yearInput.value));

    load('month');
})();
