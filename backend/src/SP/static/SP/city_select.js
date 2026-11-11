// Menu a cascata del comune: la regione filtra le province, la provincia filtra i comuni.
// I comuni arrivano dal tag <script id="city-options"> generato dal template.
(function () {
    const cities = JSON.parse(document.getElementById('city-options').textContent);
    const region = document.getElementById('id_region');
    const province = document.getElementById('id_province');
    const city = document.getElementById('id_city');
    const placeholders = {province: province.options[0].text, city: city.options[0].text};
    const byName = (a, b) => a.localeCompare(b, 'it');

    function fill(select, placeholder, items, selected) {
        const options = items.map(([value, label]) => new Option(label, value, false, String(value) === String(selected)));
        select.replaceChildren(new Option(placeholder, ''), ...options);
        select.disabled = items.length === 0;
    }

    function showProvinces(selected) {
        const names = [...new Set(cities.filter(c => c.region === region.value).map(c => c.province))].sort(byName);
        fill(province, placeholders.province, names.map(name => [name, name]), selected);
    }

    function showCities(selected) {
        const items = cities.filter(c => c.province === province.value).sort((a, b) => byName(a.name, b.name));
        fill(city, placeholders.city, items.map(c => [c.id, c.name]), selected);
    }

    region.addEventListener('change', () => {
        showProvinces('');
        showCities('');
    });
    province.addEventListener('change', () => showCities(''));

    // Stato iniziale, anche dopo un invio: se un comune è già scelto, regione e provincia sono le sue
    const current = cities.find(c => String(c.id) === city.value);
    if (current) region.value = current.region;
    showProvinces(current ? current.province : province.value);
    showCities(current ? current.id : city.value);
})();
