// Heatmap dell'Italia nella pagina di confronto tra città: il colore è la resa, cioè i kWh prodotti
// in un giorno per ogni kW installato (kWh/kWp). I punti arrivano dal tag <script id="heatmap-data">
// generato dal template; i confini delle regioni (file in data-regions-url) servono a colorare solo
// la terraferma. La resa tra un punto e l'altro è interpolata nel browser.
(function () {
    const data = JSON.parse(document.getElementById('heatmap-data').textContent);
    const container = document.getElementById('city-heatmap');

    // Rettangolo che contiene tutta l'Italia, isole comprese
    const BOUNDS = L.latLngBounds([35.3, 6.4], [47.2, 18.8]);
    // Larghezza (px) dell'immagine sovrapposta alla mappa e passo (px) della griglia di calcolo
    const IMAGE_WIDTH = 1200;
    const GRID_STEP = 4;
    // Le città misurate sono poche: si colora solo il loro intorno, perché estendere il valore
    // di una città a tutta l'Italia sarebbe un dato inventato
    const MEASURED_RADIUS_KM = 60;
    // Scala sequenziale: dal giallo chiaro (resa bassa) al bruno (resa alta)
    const RAMP = ['#fff3bf', '#fee391', '#fec44f', '#fe9929', '#ec7014', '#cc4c02', '#8c2d04'];
    const HEAT_OPACITY = 0.8;

    const formatYield = (value, digits = 2) =>
        value.toLocaleString('it-IT', {minimumFractionDigits: digits, maximumFractionDigits: digits});
    const escapeHtml = text => String(text).replace(/[&<>"']/g, c => `&#${c.charCodeAt(0)};`);
    const [firstYear, lastYear] = data.reference_years;

    const LAYERS = {
        measured: {
            points: data.measured,
            radiusKm: MEASURED_RADIUS_KM,
            markerRadius: 7,
            tooltip: p => `<strong>${escapeHtml(p.name)}</strong>${p.province ? ` (${escapeHtml(p.province)})` : ''}<br>`
                + `${formatYield(p.specific_yield)} kWh/kWp al giorno<br>`
                + `<span class="text-muted">${p.systems} impianti, ${p.days} giorni: ${p.first_day} - ${p.last_day}</span>`,
        },
        reference: {
            points: data.reference,
            radiusKm: null,  // tutta l'Italia
            markerRadius: 5,
            tooltip: p => `<strong>${escapeHtml(p.name)}</strong><br>`
                + `${formatYield(p.specific_yield)} kWh/kWp al giorno<br>`
                + `<span class="text-muted">stima PVGIS, media ${firstYear}-${lastYear}</span>`,
        },
    };

    // --- Scala dei colori: una per livello, perché i due livelli coprono periodi diversi ---

    function colorDomain(values) {
        const min = Math.min(...values);
        const max = Math.max(...values);
        const step = max - min <= 1.25 ? 0.25 : max - min <= 3 ? 0.5 : 1;
        let low = Math.floor(min / step) * step;
        let high = Math.ceil(max / step) * step;
        // Almeno 1 kWh/kWp tra gli estremi: differenze minime tra città non devono sembrare grandi
        while (high - low < 1) {
            low -= step;
            if (high - low < 1) high += step;
        }
        return {low: Math.max(0, low), high, step};
    }

    const hexToRgb = hex => [1, 3, 5].map(i => parseInt(hex.slice(i, i + 2), 16));
    // 256 colori precalcolati lungo la scala
    const PALETTE = Array.from({length: 256}, (_, i) => {
        const position = i / 255 * (RAMP.length - 1);
        const index = Math.min(Math.floor(position), RAMP.length - 2);
        const [from, to] = [hexToRgb(RAMP[index]), hexToRgb(RAMP[index + 1])];
        return from.map((channel, c) => Math.round(channel + (to[c] - channel) * (position - index)));
    });

    function rgbOf(value, domain) {
        const share = (value - domain.low) / (domain.high - domain.low);
        return PALETTE[Math.round(Math.min(1, Math.max(0, share)) * 255)];
    }

    Object.values(LAYERS).filter(layer => layer.points.length).forEach(layer => {
        layer.domain = colorDomain(layer.points.map(p => p.specific_yield));
    });

    // --- Proiezione: l'immagine è in Web Mercator, come le tile, così combacia a ogni zoom ---

    const mercatorY = lat => Math.log(Math.tan(Math.PI / 4 + lat * Math.PI / 360));
    const latFromMercatorY = y => (2 * Math.atan(Math.exp(y)) - Math.PI / 2) * 180 / Math.PI;
    const west = BOUNDS.getWest();
    const east = BOUNDS.getEast();
    const top = mercatorY(BOUNDS.getNorth());
    const bottom = mercatorY(BOUNDS.getSouth());
    const width = IMAGE_WIDTH;
    const height = Math.round(width * (top - bottom) / ((east - west) * Math.PI / 180));

    const toPixel = (lat, lng) => [(lng - west) / (east - west) * width, (top - mercatorY(lat)) / (top - bottom) * height];
    const toLatLng = (x, y) => [latFromMercatorY(top - y / height * (top - bottom)), west + x / width * (east - west)];

    // Distanza approssimata (km), più che precisa alla scala di una regione
    function distanceKm(lat1, lng1, lat2, lng2) {
        const x = (lng2 - lng1) * Math.cos((lat1 + lat2) * Math.PI / 360) * 111.32;
        const y = (lat2 - lat1) * 110.57;
        return Math.hypot(x, y);
    }

    // Resa in un punto: media dei punti del livello pesata con l'inverso del quadrato della distanza.
    // Nei livelli con un raggio i pesi si annullano al bordo, così la mappa non ha salti, e fuori dal
    // raggio di tutti i punti non c'è una stima (null). "coverage" sfuma il colore verso il bordo.
    function estimate(layer, lat, lng) {
        let weights = 0;
        let sum = 0;
        let nearest = Infinity;
        for (const point of layer.points) {
            const distance = Math.max(distanceKm(lat, lng, point.latitude, point.longitude), 0.1);
            if (layer.radiusKm && distance >= layer.radiusKm) continue;
            const weight = layer.radiusKm
                ? ((layer.radiusKm - distance) / (layer.radiusKm * distance)) ** 2
                : 1 / distance ** 2;
            weights += weight;
            sum += weight * point.specific_yield;
            nearest = Math.min(nearest, distance);
        }
        if (weights === 0) return null;
        const coverage = layer.radiusKm ? Math.min(1, (layer.radiusKm - nearest) / (layer.radiusKm * 0.4)) : 1;
        return {value: sum / weights, coverage};
    }

    // --- Disegno ---

    function drawLandMask(regions) {
        const canvas = document.createElement('canvas');
        canvas.width = width;
        canvas.height = height;
        const context = canvas.getContext('2d');
        context.beginPath();
        regions.features.forEach(feature => feature.geometry.coordinates.forEach(polygon => polygon.forEach(ring => {
            ring.forEach(([lng, lat], i) => {
                const [x, y] = toPixel(lat, lng);
                if (i === 0) context.moveTo(x, y);
                else context.lineTo(x, y);
            });
            context.closePath();
        })));
        context.fill();
        // Il bordo copre le piccole fessure tra regioni confinanti lasciate dalla semplificazione
        context.lineWidth = 1.5;
        context.stroke();
        return canvas;
    }

    function drawHeat(layer, landMask) {
        const columns = Math.ceil(width / GRID_STEP);
        const rows = Math.ceil(height / GRID_STEP);
        const grid = document.createElement('canvas');
        grid.width = columns;
        grid.height = rows;
        const gridContext = grid.getContext('2d');
        const pixels = gridContext.createImageData(columns, rows);
        for (let row = 0; row < rows; row++) {
            for (let column = 0; column < columns; column++) {
                const [lat, lng] = toLatLng((column + 0.5) * GRID_STEP, (row + 0.5) * GRID_STEP);
                const result = estimate(layer, lat, lng);
                if (result === null) continue;
                pixels.data.set([...rgbOf(result.value, layer.domain), Math.round(255 * result.coverage)], (row * columns + column) * 4);
            }
        }
        gridContext.putImageData(pixels, 0, 0);

        // La griglia ingrandita con lo smussamento del browser dà sfumature continue
        const canvas = document.createElement('canvas');
        canvas.width = width;
        canvas.height = height;
        const context = canvas.getContext('2d');
        context.imageSmoothingQuality = 'high';
        context.drawImage(grid, 0, 0, columns * GRID_STEP, rows * GRID_STEP);
        if (landMask) {
            context.globalCompositeOperation = 'destination-in';
            context.drawImage(landMask, 0, 0);
        }
        return canvas.toDataURL();
    }

    function drawLegend(domain) {
        const ticks = document.getElementById('heatmap-legend-ticks');
        const digits = domain.step < 0.5 ? 2 : 1;
        const count = Math.round((domain.high - domain.low) / domain.step);
        ticks.replaceChildren(...Array.from({length: count + 1}, (_, i) => {
            const tick = document.createElement('span');
            tick.style.left = `${i / count * 100}%`;
            tick.textContent = formatYield(domain.low + i * domain.step, digits);
            return tick;
        }));
    }

    // --- Mappa ---

    const map = L.map(container, {
        zoomSnap: 0.25,
        minZoom: 5,
        maxZoom: 10,
        maxBounds: BOUNDS.pad(0.25),
        // La rotella scorre la pagina: si zooma con i pulsanti o con doppio clic
        scrollWheelZoom: false,
    }).fitBounds(BOUNDS);

    // Colori sotto le etichette delle città, punti sopra a tutto
    map.createPane('heat').style.zIndex = 350;
    map.createPane('labels').style.zIndex = 450;
    map.getPane('labels').style.pointerEvents = 'none';
    map.createPane('points').style.zIndex = 500;

    // Con lo zoom frazionario il browser lascia sottili fessure tra le tile: le si allarga di 1 px
    const SeamlessTileLayer = L.TileLayer.extend({
        _initTile(tile) {
            L.TileLayer.prototype._initTile.call(this, tile);
            const size = this.getTileSize();
            tile.style.width = `${size.x + 1}px`;
            tile.style.height = `${size.y + 1}px`;
        },
    });

    // Mappa di base grigio chiaro di Esri, senza chiave: fondo e nomi dei luoghi sono due livelli separati
    const esriTiles = 'https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_{name}/MapServer/tile/{z}/{y}/{x}';
    new SeamlessTileLayer(esriTiles, {
        name: 'Base', maxNativeZoom: 16, attribution: 'Tiles &copy; Esri, HERE, Garmin, &copy; OpenStreetMap',
    }).addTo(map);
    L.tileLayer(esriTiles, {name: 'Reference', maxNativeZoom: 16, pane: 'labels'}).addTo(map);

    const readout = document.getElementById('heatmap-readout');

    let landPixels = null;  // canale alfa della maschera, per sapere se il cursore è sulla terraferma
    let heatOverlay = null;
    let points = null;
    let current = null;

    function show(name, landMask) {
        current = LAYERS[name];
        document.querySelectorAll('[data-heatmap-caption]').forEach(caption =>
            caption.classList.toggle('d-none', caption.dataset.heatmapCaption !== name));
        document.getElementById(`heatmap-${name}`).checked = true;
        drawLegend(current.domain);

        if (heatOverlay) heatOverlay.remove();
        heatOverlay = L.imageOverlay(drawHeat(current, landMask), BOUNDS, {
            pane: 'heat', opacity: HEAT_OPACITY, interactive: false,
        }).addTo(map);

        if (points) points.remove();
        points = L.layerGroup(current.points.map(point =>
            L.circleMarker([point.latitude, point.longitude], {
                pane: 'points',
                radius: current.markerRadius,
                color: '#343a40',
                weight: 1.5,
                fillColor: `rgb(${rgbOf(point.specific_yield, current.domain).join(',')})`,
                fillOpacity: 1,
            }).bindTooltip(current.tooltip(point), {direction: 'top', offset: [0, -current.markerRadius]})
        )).addTo(map);
    }

    // Sui telefoni non c'è il passaggio del mouse: vale il tocco
    map.on('mousemove click', event => {
        const {lat, lng} = event.latlng;
        const [x, y] = toPixel(lat, lng).map(Math.floor);
        const onLand = !landPixels || (x >= 0 && y >= 0 && x < width && y < height && landPixels[(y * width + x) * 4 + 3] > 0);
        const result = onLand && current ? estimate(current, lat, lng) : null;
        readout.textContent = result && result.coverage > 0
            ? `${formatYield(result.value)} kWh/kWp al giorno`
            : 'Nessun dato in questo punto';
    });
    map.on('mouseout', () => { readout.textContent = '-'; });

    document.getElementById('heatmap-legend-bar').style.background = `linear-gradient(to right, ${RAMP.join(', ')})`;

    // Senza i confini (file non raggiungibile) la mappa funziona lo stesso, colorando anche il mare
    fetch(container.dataset.regionsUrl)
        .then(response => response.ok ? response.json() : Promise.reject(response.status))
        .then(regions => {
            L.geoJSON(regions, {
                interactive: false,
                style: {color: '#6c757d', weight: 0.8, opacity: 0.5, fill: false},
            }).addTo(map);
            map.attributionControl.addAttribution('Confini: ISTAT (openpolis)');
            return drawLandMask(regions);
        })
        .catch(() => null)
        .then(landMask => {
            if (landMask) landPixels = landMask.getContext('2d').getImageData(0, 0, width, height).data;
            document.querySelectorAll('input[name="heatmap-layer"]').forEach(input =>
                input.addEventListener('change', () => show(input.value, landMask)));
            show(data.measured.length ? 'measured' : 'reference', landMask);
        });
})();
