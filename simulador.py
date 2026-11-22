"""
Simulador de la ESP32: envía lecturas falsas de una nevera para probar la app
sin hardware. El compresor se prende y se apaga en ciclos, como en una nevera real,
y de vez en cuando se abre la puerta.

Uso:
    python simulador.py                                        # contra tu PC
    python simulador.py https://tu-app.onrender.com mi-clave   # contra Render
"""

import json
import random
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

URL = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000").rstrip("/")
API_KEY = sys.argv[2] if len(sys.argv) > 2 else "clave-de-prueba"
INTERVALO = 2  # segundos entre lecturas
HORA_COLOMBIA = timezone(timedelta(hours=-5))


def enviar(ruta, datos):
    req = urllib.request.Request(
        URL + ruta, data=json.dumps(datos).encode(), method="POST",
        headers={"Content-Type": "application/json", "X-API-Key": API_KEY})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            print(r.status, ruta, datos)
    except Exception as e:
        print("Error:", e)


def hora_rtc(fecha):
    return fecha.strftime("%Y-%m-%dT%H:%M:%S")   # como la manda la ESP32: sin zona

energia = 0.0
temp_interna = 4.0
aperturas = 0
puerta_hasta = 0
abierta_desde = None
t = 0
while True:
    compresor = (t % 120) < 70  # 70 lecturas encendido, 50 apagado
    voltaje = random.gauss(120, 1.5)
    if compresor:
        potencia = random.gauss(110, 6)
        fp = random.uniform(0.80, 0.88)
        temp_interna -= 0.03
    else:
        potencia = random.gauss(4, 1)  # luz/electrónica en reposo
        fp = random.uniform(0.40, 0.55)
        temp_interna += 0.04
    if t >= puerta_hasta and random.random() < 0.03:   # a veces abren la puerta
        aperturas += 1
        puerta_hasta = t + random.randint(3, 10)
    puerta_abierta = t < puerta_hasta
    ahora = datetime.now(HORA_COLOMBIA)
    if puerta_abierta:
        temp_interna += 0.15
        if abierta_desde is None:
            abierta_desde = ahora
    elif abierta_desde is not None:
        # se acaba de cerrar: enviar la apertura completa
        enviar("/api/puerta", [{"inicio": hora_rtc(abierta_desde), "fin": hora_rtc(ahora)}])
        abierta_desde = None

    potencia = max(potencia, 0)
    corriente = potencia / (voltaje * fp)
    energia += potencia * INTERVALO / 3_600_000  # W*s -> kWh

    datos = {
        # misma forma que enviará la ESP32: hora del RTC sin zona (hora de Colombia)
        "fecha": hora_rtc(ahora),
        "voltaje": round(voltaje, 1), "corriente": round(corriente, 3),
        "potencia": round(potencia, 1), "energia": round(energia, 5), "fp": round(fp, 2),
        "frecuencia": round(random.gauss(60, 0.05), 1),
        "temp_interna": round(temp_interna, 2),
        "temp_ambiente": round(random.gauss(21, 0.3), 1),
        "humedad": round(random.gauss(55, 2)),
        "puerta_abierta": puerta_abierta,
        "segundos_abierta": INTERVALO if puerta_abierta else 0,
        "aperturas": aperturas,
    }
    enviar("/api/lecturas", datos)
    t += 1
    time.sleep(INTERVALO)
