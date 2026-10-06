"""Controllo degli accessi al broker MQTT: mosquitto-go-auth chiede qui chi può collegarsi e a quali topic.

Il broker invia una POST con un JSON per ogni richiesta e guarda solo lo status della risposta:
200 autorizzato, 403 rifiutato (auth_opt_http_response_mode status, vedi mosquitto/mosquitto.conf).

    user/       {"username", "password", "clientid"}           può collegarsi?
    superuser/  {"username"}                                   può accedere a ogni topic?
    acl/        {"username", "clientid", "topic", "acc"}       può leggere (1), scrivere (2), iscriversi (4)?
"""
import json

from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import mqtt


def _answer(allowed):
    return HttpResponse(status=200 if allowed else 403)


def _request_data(request):
    try:
        data = json.loads(request.body)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


@csrf_exempt
@require_POST
def user(request):
    data = _request_data(request)
    username, password = str(data.get("username", "")), str(data.get("password", ""))
    # Il client id deve essere lo username: due bridge non possono collegarsi come lo stesso impianto
    # senza scollegarsi a vicenda, e nessuno può usare il client id di un altro
    client_id = str(data.get("clientid", ""))
    if not mqtt.is_server(username) and client_id != username:
        return _answer(False)
    return _answer(mqtt.authenticate(username, password))


@csrf_exempt
@require_POST
def superuser(request):
    return _answer(mqtt.is_server(str(_request_data(request).get("username", ""))))


@csrf_exempt
@require_POST
def acl(request):
    data = _request_data(request)
    try:
        acc = int(data.get("acc"))
    except (TypeError, ValueError):
        return _answer(False)
    return _answer(mqtt.may_access(str(data.get("username", "")), str(data.get("topic", "")), acc))
