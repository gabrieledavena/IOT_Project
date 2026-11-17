// Pagina Simulazione di una community (SP/simulation.py, SP/simulation_detail.html).
// I valori dei sensori simulati si inviano al server mentre si cambiano (cursore o casella), senza ricaricare la
// pagina: il server li manda subito al master. Gli scenari (Sole pieno, Nuvoloso, ...) riempiono i tre valori di un
// impianto, o di tutti. Ogni 5 secondi la pagina aggiorna master, collegamento, stato e ultima misura salvata.
(() => {
    const root = document.querySelector("[data-simulation]");
    if (!root) {
        return;
    }
    const presets = JSON.parse(document.getElementById("simulation-presets").textContent);
    const SAVE_DELAY = 400;       // ms dopo l'ultimo movimento del cursore
    const REFRESH = 5000;         // ms

    const number = (value, digits) => Number(value).toLocaleString("it-IT", {
        minimumFractionDigits: digits, maximumFractionDigits: digits,
    });
    const badge = (color, text, icon) =>
        `<span class="badge rounded-pill ${color}">${icon ? `<i class="fas ${icon} me-1"></i>` : ""}${text}</span>`;

    function showSaved(element, state, text) {
        const styles = {pending: "text-muted", ok: "text-success", error: "text-danger"};
        const icons = {pending: "fa-spinner fa-spin", ok: "fa-check", error: "fa-exclamation-triangle"};
        element.className = `text-nowrap ${styles[state]}`;
        element.innerHTML = `<i class="fas ${icons[state]} me-1"></i>`;
        element.append(text);
    }

    // --- Valori dei sensori simulati ---

    const forms = [...root.querySelectorAll("form[data-system]")];
    for (const form of forms) {
        const saved = form.querySelector("[data-field=saved]");
        const pairs = [...form.querySelectorAll("input[type=range]")].map(range =>
            ({range, input: form.querySelector(`input[name=${range.dataset.pair}]`)}));
        let timer = null;

        async function save() {
            clearTimeout(timer);
            const invalid = pairs.find(({input}) => !input.checkValidity());
            for (const {input} of pairs) {
                input.classList.toggle("is-invalid", !input.checkValidity());
            }
            if (invalid) {
                showSaved(saved, "error", `Valore non valido: ${invalid.input.validationMessage}`);
                return;
            }
            showSaved(saved, "pending", "Invio…");
            try {
                const response = await fetch(window.location.href, {
                    method: "POST", body: new FormData(form), headers: {Accept: "application/json"},
                });
                const data = await response.json().catch(() => ({}));
                if (!response.ok) {
                    throw new Error(Object.values(data.errors || {}).flat().join(" ") || `errore ${response.status}`);
                }
                showSaved(saved, "ok", `Inviato al master alle ${new Date().toLocaleTimeString("it-IT")}`);
            } catch (error) {
                showSaved(saved, "error", `Non inviato: ${error.message}`);
            }
        }

        for (const {range, input} of pairs) {
            range.addEventListener("input", () => {
                input.value = range.value;
                input.classList.remove("is-invalid");
                clearTimeout(timer);
                timer = setTimeout(save, SAVE_DELAY);
            });
            input.addEventListener("input", () => {
                range.value = input.value;
                clearTimeout(timer);
                timer = setTimeout(save, SAVE_DELAY * 2);   // si sta scrivendo: si aspetta un po' di più
            });
            input.addEventListener("change", save);
        }
        form.addEventListener("submit", event => {
            event.preventDefault();
            save();
        });

        // Scenario: produzione come frazione della potenza installata, temperatura e luce uguali per tutti
        form.applyPreset = key => {
            const preset = presets[key];
            const values = {
                power: Math.round(Number(form.dataset.maxPower) * preset.share * 100) / 100,
                temperature: preset.temperature,
                lightness: preset.lightness,
            };
            for (const {range, input} of pairs) {
                input.value = range.value = values[range.dataset.pair];
            }
            save();
        };
        for (const button of form.querySelectorAll("[data-preset]")) {
            button.addEventListener("click", () => form.applyPreset(button.dataset.preset));
        }
    }
    for (const button of root.querySelectorAll("[data-preset-all]")) {
        button.addEventListener("click", () => forms.forEach(form => form.applyPreset(button.dataset.presetAll)));
    }

    // --- Stato di master e sensori ---

    function showState(element, state, simulated) {
        const set = (field, html) => {
            const target = element.querySelector(`[data-field=${field}]`);
            if (target) {
                target.innerHTML = html;
            }
        };
        set("online", state.online
            ? badge("bg-success", simulated ? "Sentito dal master" : "Collegato", "fa-circle")
            : badge("bg-secondary", simulated ? "Non ancora sentito" : "Scollegato"));
        set("pump", state.pump_running ? badge("bg-info text-dark", "Lavaggio", "fa-tint") : "");
        const status = element.querySelector("[data-field=status]");
        if (status) {
            status.textContent = state.status_label;
        }
        const reading = element.querySelector("[data-field=reading]");
        if (reading) {
            const value = state.reading;
            reading.textContent = value
                ? `${value.time} · ${number(value.power, 2)} kW · ${number(value.temperature, 1)} °C · ${number(value.lightness, 0)} lux`
                : "nessuna";
        }
    }

    async function refresh() {
        const data = await fetch(root.dataset.statusUrl, {cache: "no-store", headers: {Accept: "application/json"}})
            .then(response => response.ok ? response.json() : null)
            .catch(() => null);
        if (!data) {
            return;
        }
        const gateway = root.querySelector("[data-gateway]");
        if (gateway) {
            gateway.innerHTML = data.gateway.online
                ? badge("bg-success", "Collegato", "fa-circle") : badge("bg-secondary", "Scollegato");
        }
        for (const state of data.systems) {
            for (const element of root.querySelectorAll(`[data-state="${state.id}"]`)) {
                showState(element, state, element.tagName === "FORM");
            }
        }
    }

    setInterval(refresh, REFRESH);
})();
