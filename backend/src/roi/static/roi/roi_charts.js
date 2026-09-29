// Grafici della pagina ROI: produzione stimata per mese e flusso di cassa cumulato anno per anno.
// I dati arrivano dal tag <script id="roi-chart-data"> generato dal template.
(function () {
    const data = JSON.parse(document.getElementById('roi-chart-data').textContent);
    const euro = value => value.toLocaleString('it-IT', {maximumFractionDigits: 0}) + ' €';
    const kwh = value => value.toLocaleString('it-IT', {maximumFractionDigits: 0}) + ' kWh';
    const baseOptions = unitFormatter => ({
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
            legend: {display: false},
            tooltip: {callbacks: {label: context => unitFormatter(context.parsed.y)}},
        },
        scales: {
            x: {grid: {display: false}},
            y: {ticks: {callback: unitFormatter}, grid: {color: '#e9ecef'}},
        },
    });

    new Chart(document.getElementById('monthlyProductionChart'), {
        type: 'bar',
        data: {
            labels: data.months,
            datasets: [{data: data.production, backgroundColor: 'rgba(25, 135, 84, 0.7)', borderRadius: 4}],
        },
        options: baseOptions(kwh),
    });

    new Chart(document.getElementById('cashFlowChart'), {
        type: 'line',
        data: {
            labels: data.years.map(year => 'Anno ' + year),
            datasets: [{
                data: data.cumulative,
                borderColor: '#0d6efd',
                borderWidth: 2,
                pointRadius: 0,
                // Rosso finché l'investimento non è ripagato, verde dopo
                segment: {borderColor: context => context.p1.parsed.y < 0 ? '#dc3545' : '#198754'},
            }],
        },
        options: baseOptions(euro),
    });
})();
