// Grafici della pagina ROI: produzione stimata per mese e flusso di cassa cumulato anno per anno.
// I dati arrivano dal tag <script id="roi-chart-data"> generato dal template. Colori e font da SP/chart_theme.js;
// il flusso di cassa è rosso finché l'investimento non è ripagato e verde dopo.
(function () {
    const data = JSON.parse(document.getElementById('roi-chart-data').textContent);
    const theme = window.chartTheme;
    const euro = value => `${theme.number(value, 0)} €`;
    const kwh = value => `${theme.number(value, 0)} kWh`;
    const RED = '#c92a2a';

    function options(canvas, unit, format) {
        const palette = theme.colors(canvas);
        return {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: {display: false},
                tooltip: {...theme.tooltip(palette), callbacks: {label: context => format(context.parsed.y)}},
            },
            scales: {
                x: {grid: {display: false}, border: {display: false}, ticks: {color: palette.text, maxRotation: 0, autoSkipPadding: 12}},
                y: {grid: {color: palette.grid}, border: {display: false}, ticks: theme.ticks(palette, unit, 0)},
            },
        };
    }

    const monthly = document.getElementById('monthlyProductionChart');
    new Chart(monthly, {
        type: 'bar',
        data: {
            labels: data.months,
            datasets: [{data: data.production, backgroundColor: theme.colors(monthly).bar, borderRadius: 4, maxBarThickness: 40}],
        },
        options: options(monthly, 'kWh', kwh),
    });

    const cash = document.getElementById('cashFlowChart');
    const green = theme.colors(cash).line;
    new Chart(cash, {
        type: 'line',
        data: {
            labels: data.years.map(year => 'Anno ' + year),
            datasets: [{
                data: data.cumulative,
                borderColor: green,
                borderWidth: 2,
                pointRadius: 0,
                pointHoverRadius: 4,
                // Rosso finché l'investimento non è ripagato, verde dopo
                segment: {borderColor: context => context.p1.parsed.y < 0 ? RED : green},
            }],
        },
        options: options(cash, '€', euro),
    });
})();
