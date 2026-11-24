// Grafico della pagina delle previsioni: produzione prevista per giorno, nei prossimi 7 o 30 giorni.
// I dati arrivano dal tag <script id="forecast-data"> generato dal template. I giorni oltre le previsioni meteo
// sono stimati dallo storico: barre più scure e una linea sottile dal minimo al massimo degli anni passati.
// Colori e font da SP/chart_theme.js: menta sulla banda verde.
(function () {
    const days = JSON.parse(document.getElementById('forecast-data').textContent);
    const canvas = document.getElementById('forecastChart');
    const theme = window.chartTheme;
    const palette = theme.colors(canvas);
    const kwh = value => `${theme.number(value, 1)} kWh`;
    let chart;

    // Minimo e massimo degli anni passati: una linea verticale con due tacche su ogni giorno stimato dallo storico
    const range = {
        id: 'historyRange',
        afterDatasetsDraw(current) {
            const {ctx, scales} = current;
            const bars = current.getDatasetMeta(0).data;
            ctx.save();
            ctx.strokeStyle = palette.whisker;
            ctx.lineWidth = 1.5;
            current.data.days.forEach((day, i) => {
                if (!day.history || day.low === null || day.high === null || !bars[i]) return;
                const x = bars[i].x;
                const top = scales.y.getPixelForValue(day.high);
                const bottom = scales.y.getPixelForValue(day.low);
                const cap = Math.min(10, bars[i].width);
                ctx.beginPath();
                ctx.moveTo(x, top);
                ctx.lineTo(x, bottom);
                ctx.moveTo(x - cap / 2, top);
                ctx.lineTo(x + cap / 2, top);
                ctx.moveTo(x - cap / 2, bottom);
                ctx.lineTo(x + cap / 2, bottom);
                ctx.stroke();
            });
            ctx.restore();
        },
    };

    function chartData(count) {
        const shown = days.slice(0, count);
        return {
            days: shown,
            labels: shown.map(day => day.label),
            datasets: [{
                label: 'Produzione prevista',
                data: shown.map(day => day.production),
                backgroundColor: shown.map(day => day.history ? palette.soft : palette.bar),
                borderRadius: 4,
                maxBarThickness: 48,
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
            plugins: [range],
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: {display: false},
                    tooltip: {
                        ...theme.tooltip(palette),
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
                    x: {grid: {display: false}, border: {display: false}, ticks: {color: palette.text, maxRotation: 0, autoSkipPadding: 16}},
                    y: {
                        beginAtZero: true,
                        grid: {color: palette.grid},
                        border: {display: false},
                        ticks: theme.ticks(palette, 'kWh', 0),
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
