// Recapiti del titolare nella modifica della community: scegliendo un altro titolare, i campi mostrano telefono ed
// email di quell'utente, da completare se mancano. Il campo nascosto owner-customer dice al server di chi sono.
// I recapiti degli utenti arrivano dal tag <script id="member-contacts"> generato dal template.
(function () {
    const contacts = JSON.parse(document.getElementById('member-contacts').textContent);
    const owner = document.getElementById('id_owner');
    const customer = document.getElementById('id_owner-customer');
    const email = document.getElementById('id_owner-email');
    const phone = document.getElementById('id_owner-phone');
    if (!owner || !customer || !email || !phone) {
        return;
    }

    owner.addEventListener('change', () => {
        const chosen = contacts[owner.value] || {email: '', phone: ''};
        customer.value = owner.value;
        email.value = chosen.email;
        phone.value = chosen.phone;
        // Gli errori erano per i recapiti del titolare di prima
        document.querySelectorAll('[data-contacts-error]').forEach(error => error.remove());
    });
})();
