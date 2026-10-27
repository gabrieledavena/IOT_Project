// Grafico della pagina delle previsioni: produzione prevista per giorno, nei prossimi 7 o 30 giorni.
// I dati arrivano dal tag <script id="forecast-data"> generato dal template. I giorni oltre le
// previsioni meteo sono stimati dallo storico: barre più chiare e, nel tooltip, minimo e massimo.
(function () {
    const days = JSON.parse(document.getElementById('forecast-data').textContent);
    const canvas = document.getElementById('forecastChart');
    const kwh = value => value.toLocaleString('it-IT', {maximumFractionDigits: 1}) + ' kWh';
    let chart;

    function chartData(count) {
        const shown = days.slice(0, count);
        return {
            labels: shown.map(day => day.label),
            datasets: [{
                label: 'Produzione prevista',
                data: shown.map(day => day.production),
                backgroundColor: shown.map(day => day.history ? 'rgba(25, 135, 84, 0.3)' : '#198754'),
                borderRadius: 4,
            }],
        };
    }

    function show(count) {
        if (chart) {
            chart.data = chartData(count);
            chart.update();
            return;
        }
        chart = new Chart(canvas.getContext('2d'), {
            type: 'bar',
            data: chartData(count),
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: {display: false},
                    tooltip: {
                        backgroundColor: 'rgba(0, 0, 0, 0.8)',
                        padding: 12,
                        bodyFont: {size: 14, weight: 'bold'},
                        callbacks: {
                            label: context => kwh(context.parsed.y),
                            afterLabel: context => {
                                const day = days[context.dataIndex];
                                return day.history ? `Stima storica: tra ${kwh(day.low)} e ${kwh(day.high)}` : 'Previsioni meteo';
                            },
                        },
                    },
                },
                scales: {
                    x: {grid: {display: false}, ticks: {color: '#6c757d'}},
                    y: {
                        beginAtZero: true,
                        grid: {color: '#e9ecef'},
                        ticks: {color: '#6c757d', callback: value => value + ' kWh'},
                    },
                },
            },
        });
    }

    document.querySelectorAll('[data-days]').forEach(input => {
        input.addEventListener('change', () => show(Number(input.dataset.days)));
    });
    show(Number(document.querySelector('[data-days]:checked').dataset.days));
})();
