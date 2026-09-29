// Grafici delle dashboard di produzione: potenza e, se presenti nella pagina, luminosità e temperatura.
// I dati arrivano dal tag <script id="chart-data"> generato dal template (un punto ogni 2 minuti).
(function () {
    const rawData = JSON.parse(document.getElementById('chart-data').textContent);

    const SERIES = [
        {canvasId: 'productionChart', field: 'value', label: 'Potenza Generata (kW)', unit: 'kW',
         rgb: '25, 135, 84', color: '#198754', opacity: 0.8, beginAtZero: true, production: true},
        {canvasId: 'lightnessChart', field: 'lightness', label: 'Luminosità', unit: 'lx',
         rgb: '255, 193, 7', color: '#ffc107', opacity: 0.6, beginAtZero: false},
        {canvasId: 'temperatureChart', field: 'temperature', label: 'Temperatura (°C)', unit: '°C',
         rgb: '220, 53, 69', color: '#dc3545', opacity: 0.6, beginAtZero: false},
    ];
    const charts = {};

    // Media dei punti su finestre di `minutes` minuti (1 = dati originali)
    function downsample(data, minutes) {
        if (data.length === 0 || minutes === 1) return data;

        const sampled = [];
        let windowStart = moment(data[0].timestamp);
        let sums = {value: 0, temperature: 0, lightness: 0};
        let count = 0;
        const pushWindow = () => sampled.push({
            timestamp: windowStart.toISOString(),
            value: sums.value / count,
            temperature: sums.temperature / count,
            lightness: sums.lightness / count,
        });

        data.forEach(point => {
            const time = moment(point.timestamp);
            if (time.diff(windowStart, 'minutes') >= minutes) {
                if (count > 0) pushWindow();
                windowStart = time;
                sums = {value: 0, temperature: 0, lightness: 0};
                count = 0;
            }
            sums.value += point.value;
            sums.temperature += point.temperature || 0;
            sums.lightness += point.lightness || 0;
            count++;
        });
        if (count > 0) pushWindow();
        return sampled;
    }

    function gradient(canvas, series) {
        const fill = canvas.getContext('2d').createLinearGradient(0, 0, 0, 400);
        fill.addColorStop(0, `rgba(${series.rgb}, ${series.opacity})`);
        fill.addColorStop(1, `rgba(${series.rgb}, 0.1)`);
        return fill;
    }

    function chartOptions(series) {
        return {
            responsive: true,
            maintainAspectRatio: false,
            interaction: {mode: 'index', intersect: false},
            plugins: {
                legend: {display: false},
                tooltip: {
                    backgroundColor: 'rgba(0, 0, 0, 0.8)',
                    padding: 12,
                    titleFont: {size: 14},
                    bodyFont: {size: 14, weight: 'bold'},
                    callbacks: {
                        label: context => context.parsed.y.toFixed(2) + ' ' + series.unit,
                    },
                },
            },
            scales: {
                x: {
                    type: 'time',
                    time: {unit: 'hour', displayFormats: {hour: 'HH:mm'}, tooltipFormat: 'DD MMM YYYY, HH:mm'},
                    grid: {display: false},
                    ticks: {color: '#6c757d', font: {size: 12}},
                },
                y: {
                    beginAtZero: series.beginAtZero,
                    grid: {color: '#e9ecef', drawBorder: false},
                    ticks: {color: '#6c757d', font: {size: 12}, callback: value => value + ' ' + series.unit},
                },
            },
        };
    }

    // La potenza è una linea al minuto e un istogramma a 15 minuti e 1 ora; gli altri sono linee
    function datasetStyle(series, minutes) {
        if (series.production) {
            const line = minutes === 1;
            return {type: line ? 'line' : 'bar', borderWidth: line ? 2 : 0, pointHoverRadius: 5, borderRadius: 4};
        }
        return {borderWidth: 2, pointHoverRadius: 4, tension: 0.4};
    }

    function updateCharts(minutes) {
        const data = downsample(rawData, minutes);
        const labels = data.map(point => point.timestamp);

        SERIES.forEach(series => {
            const canvas = document.getElementById(series.canvasId);
            if (!canvas) return;
            const values = data.map(point => point[series.field]);
            const chart = charts[series.canvasId];

            if (chart) {
                chart.data.labels = labels;
                Object.assign(chart.data.datasets[0], datasetStyle(series, minutes), {data: values});
                if (series.production) chart.data.datasets[0].fill = minutes === 1;
                chart.update();
            } else {
                const style = datasetStyle(series, minutes);
                charts[series.canvasId] = new Chart(canvas.getContext('2d'), {
                    type: style.type || 'line',
                    data: {
                        labels: labels,
                        datasets: [{
                            label: series.label,
                            data: values,
                            backgroundColor: gradient(canvas, series),
                            borderColor: series.color,
                            fill: true,
                            pointRadius: 0,
                            ...style,
                        }],
                    },
                    options: chartOptions(series),
                });
            }
        });
    }

    document.querySelectorAll('[data-resolution]').forEach(input => {
        input.addEventListener('change', () => updateCharts(Number(input.dataset.resolution)));
    });
    updateCharts(15);
})();
