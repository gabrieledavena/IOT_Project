// Aspetto comune dei grafici Chart.js del sito: font, tooltip e colori. I colori vengono dalle variabili CSS del
// contenitore .chart (SP/app.css): menta sulla banda verde, verde sul fondo chiaro. Va caricato dopo Chart.js.
(function () {
    if (typeof Chart === 'undefined') return;

    Chart.defaults.font.family = 'Urbanist, system-ui, -apple-system, "Segoe UI", sans-serif';
    Chart.defaults.font.size = 13;
    Chart.defaults.font.weight = 500;

    // Colori del grafico disegnato in canvas
    function colors(canvas) {
        const style = getComputedStyle(canvas.closest('.chart') || canvas);
        const value = name => style.getPropertyValue(name).trim();
        return {
            bar: value('--ch-bar'), soft: value('--ch-soft'), line: value('--ch-line'), fill: value('--ch-fill'),
            grid: value('--ch-grid'), text: value('--ch-text'), now: value('--ch-now'), whisker: value('--ch-whisker'),
            tipBg: value('--ch-tip-bg'), tipInk: value('--ch-tip-ink'),
        };
    }

    // "#198754" e un'opacità -> "rgba(25, 135, 84, 0.5)"
    function alpha(hex, opacity) {
        const digits = hex.replace('#', '');
        const [r, g, b] = [0, 2, 4].map(i => parseInt(digits.slice(i, i + 2), 16));
        return `rgba(${r}, ${g}, ${b}, ${opacity})`;
    }

    // Riempimento sotto una linea: dal colore pieno in alto al trasparente in basso
    function gradient(canvas, hex, opacity) {
        const fill = canvas.getContext('2d').createLinearGradient(0, 0, 0, canvas.clientHeight || 300);
        fill.addColorStop(0, alpha(hex, opacity));
        fill.addColorStop(1, alpha(hex, 0));
        return fill;
    }

    function tooltip(palette) {
        return {
            backgroundColor: palette.tipBg,
            titleColor: palette.tipInk,
            bodyColor: palette.tipInk,
            footerColor: palette.tipInk,
            padding: 10,
            cornerRadius: 10,
            displayColors: false,
            titleFont: {weight: 500},
            bodyFont: {size: 15, weight: 700},
            footerFont: {weight: 500},
        };
    }

    // Numero all'italiana: 1.234,5
    function number(value, digits) {
        return value.toLocaleString('it-IT', {maximumFractionDigits: digits});
    }

    // Tacche dell'asse verticale: l'unità solo sulla più alta
    function ticks(palette, unit, digits = 1) {
        return {
            color: palette.text,
            maxTicksLimit: 6,
            callback: (value, index, all) => index === all.length - 1 ? `${number(value, digits)} ${unit}` : number(value, digits),
        };
    }

    window.chartTheme = {colors, alpha, gradient, tooltip, number, ticks};
})();
