// Grafici delle dashboard di produzione: potenza e, se presenti nella pagina, luminosità e temperatura.
// I dati arrivano dal tag <script id="chart-data"> generato dal template (un punto al minuto).
// Colori e font vengono da SP/chart_theme.js: menta sulla banda verde, verde e nero sul fondo chiaro.
(function () {
    const rawData = JSON.parse(document.getElementById('chart-data').textContent);
    const theme = window.chartTheme;

    const SERIES = [
        {canvasId: 'productionChart', field: 'value', unit: 'kW', digits: 2, production: true},
        {canvasId: 'lightnessChart', field: 'lightness', unit: 'lx', digits: 0, beginAtZero: true},
        {canvasId: 'temperatureChart', field: 'temperature', unit: '°C', digits: 1},
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

    // Asse dei tempi: dalle 6 alle 18, allargato se gli impianti producono prima o dopo (d'estate)
    const producing = rawData.filter(point => point.value > 0);
    const dayStart = moment(rawData[0].timestamp).startOf('day');
    let xMin = dayStart.clone().add(6, 'hours');
    let xMax = dayStart.clone().add(18, 'hours');
    if (producing.length) {
        const first = moment(producing[0].timestamp).startOf('hour');
        const last = moment(producing[producing.length - 1].timestamp).add(1, 'hour').startOf('hour');
        if (first.isBefore(xMin)) xMin = first;
        if (last.isAfter(xMax)) xMax = last;
    }

    // Nel giorno in corso una linea tratteggiata segna l'ultima misura
    const live = Boolean(document.querySelector('#productionChart[data-live]'));
    const lastTime = moment(rawData[rawData.length - 1].timestamp);
    const lastReading = {
        id: 'lastReading',
        afterDatasetsDraw(chart, args, options) {
            if (!options.show) return;
            const {ctx, chartArea: area, scales} = chart;
            const x = scales.x.getPixelForValue(lastTime.valueOf());
            if (x < area.left || x > area.right) return;
            const label = `Ultima misura ${lastTime.format('HH:mm')}`;
            ctx.save();
            ctx.strokeStyle = options.color;
            ctx.fillStyle = options.color;
            ctx.setLineDash([3, 4]);
            ctx.beginPath();
            ctx.moveTo(x, area.top);
            ctx.lineTo(x, area.bottom);
            ctx.stroke();
            ctx.setLineDash([]);
            ctx.font = `500 13px ${Chart.defaults.font.family}`;
            const right = x + 8 + ctx.measureText(label).width < area.right;
            ctx.textAlign = right ? 'left' : 'right';
            ctx.fillText(label, right ? x + 8 : x - 8, area.top + 12);
            ctx.restore();
        },
    };

    function chartOptions(series, palette) {
        return {
            responsive: true,
            maintainAspectRatio: false,
            interaction: {mode: 'index', intersect: false},
            plugins: {
                legend: {display: false},
                tooltip: {
                    ...theme.tooltip(palette),
                    callbacks: {label: context => `${theme.number(context.parsed.y, series.digits)} ${series.unit}`},
                },
                lastReading: {show: live && series.production, color: palette.now},
            },
            scales: {
                x: {
                    type: 'time',
                    min: xMin.valueOf(),
                    max: xMax.valueOf(),
                    offset: false,
                    time: {unit: 'hour', displayFormats: {hour: 'HH:mm'}, tooltipFormat: 'HH:mm'},
                    grid: {display: false},
                    border: {display: false},
                    ticks: {color: palette.text, maxRotation: 0, autoSkipPadding: 24},
                },
                y: {
                    beginAtZero: Boolean(series.production || series.beginAtZero),
                    grid: {color: palette.grid},
                    border: {display: false},
                    ticks: theme.ticks(palette, series.unit),
                },
            },
        };
    }

    // La potenza è una linea al minuto e un istogramma a 15 minuti e 1 ora; luce e temperatura sono linee
    function dataset(series, canvas, palette, values, minutes) {
        if (series.production && minutes > 1) {
            return {type: 'bar', data: values, backgroundColor: palette.bar, borderRadius: 4, maxBarThickness: 40, borderWidth: 0};
        }
        return {
            type: 'line',
            data: values,
            borderColor: palette.line,
            borderWidth: series.production ? 1.75 : 2,
            backgroundColor: series.production ? theme.gradient(canvas, palette.fill, 0.5) : 'transparent',
            fill: Boolean(series.production),
            pointRadius: 0,
            pointHoverRadius: 4,
            pointHoverBackgroundColor: palette.line,
            tension: 0,
        };
    }

    function updateCharts(minutes) {
        const data = downsample(rawData, minutes);
        const labels = data.map(point => point.timestamp);

        SERIES.forEach(series => {
            const canvas = document.getElementById(series.canvasId);
            if (!canvas) return;
            const palette = theme.colors(canvas);
            const values = data.map(point => point[series.field]);
            if (charts[series.canvasId]) charts[series.canvasId].destroy();
            const set = dataset(series, canvas, palette, values, minutes);
            charts[series.canvasId] = new Chart(canvas.getContext('2d'), {
                type: set.type,
                data: {labels: labels, datasets: [set]},
                options: chartOptions(series, palette),
                plugins: [lastReading],
            });
        });
    }

    // Sopra i grafici dei sensori: luce massima e temperatura minima e massima della giornata
    function showSensors() {
        const lightMax = document.querySelector('[data-sensor="light-max"]');
        const tempRange = document.querySelector('[data-sensor="temp-range"]');
        const values = field => rawData.map(point => point[field]).filter(value => typeof value === 'number');
        const light = values('lightness');
        const temperature = values('temperature');
        if (lightMax && light.length) {
            lightMax.innerHTML = `${theme.number(Math.max(...light), 0)} <small>lx</small>`;
        }
        if (tempRange && temperature.length) {
            const min = theme.number(Math.min(...temperature), 1);
            const max = theme.number(Math.max(...temperature), 1);
            tempRange.innerHTML = `${min}–${max} <small>°C</small>`;
        }
    }

    document.querySelectorAll('[data-resolution]').forEach(input => {
        input.addEventListener('change', () => updateCharts(Number(input.dataset.resolution)));
    });
    const checked = document.querySelector('[data-resolution]:checked');
    updateCharts(checked ? Number(checked.dataset.resolution) : 1);
    showSensors();
})();
