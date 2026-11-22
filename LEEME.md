# Monitor de la nevera (FastAPI + Render)

## Probar en tu PC

```
pip install -r requirements.txt
uvicorn main:app --reload
python simulador.py            # en otra terminal: envía datos falsos
```

Abre `http://127.0.0.1:8000`. En tu PC usa SQLite (`lecturas.db`) y la clave `clave-de-prueba`.
Si tenías un `lecturas.db` de la versión anterior, bórralo: la tabla cambió.

## Desplegar en Render (plan gratis)

1. Sube esta carpeta a un repositorio de GitHub (con `render.yaml` en la raíz).
2. En Render: **New → Blueprint**, escoge el repositorio y acepta.
   Se crean el servidor web y la base de datos Postgres, ya conectados.
3. Cuando termine, entra al servicio **monitor-nevera → Environment** y copia el valor de
   **API_KEY** (Render lo generó al azar). Esa clave va en el código de la ESP32.
4. Prueba con el simulador: `python simulador.py https://TU-APP.onrender.com TU_API_KEY`

Límites del plan gratis que te afectan:

- El servidor se duerme tras 15 min sin peticiones y tarda ~1 min en despertar.
  La ESP32 enviando datos lo mantiene despierto.
- **La base de datos gratis vence a los 30 días** (luego hay 14 días de gracia y se borra).
  Descarga el CSV antes de que venza.
- Sin backups: descarga el CSV cada pocos días.

## Lo que debe enviar la ESP32

`POST /api/lecturas` con el encabezado `X-API-Key: TU_API_KEY` y este JSON:

```json
{
  "fecha": "2026-11-21T10:30:00",
  "voltaje": 120.5, "corriente": 1.10, "potencia": 110.0, "energia": 0.012,
  "fp": 0.83, "frecuencia": 60.0,
  "temp_interna": 4.2, "temp_ambiente": 21.0, "humedad": 55,
  "puerta_abierta": false, "segundos_abierta": 0, "aperturas": 3
}
```

- `puerta_abierta`: cómo estaba la puerta en el instante de la lectura.
- `segundos_abierta`: cuántos segundos estuvo abierta desde la lectura anterior (0 a 5).
  Así no se pierden aperturas cortas que pasan entre dos lecturas.

Además, **cada vez que la puerta se cierra**, la ESP32 envía la apertura completa a
`POST /api/puerta` (es una lista, aunque sea de una sola apertura):

```json
[{ "inicio": "2026-11-21T10:30:02", "fin": "2026-11-21T10:30:09" }]
```

La duración se calcula sola. Si la misma apertura llega dos veces, se guarda una vez.

- `fecha` es la hora del DS3231 (hora de Colombia, sin zona).
- Si un sensor falla, manda `null` en ese campo (nunca `NaN`, que no es JSON válido).
- Si no hubo internet, manda las lecturas guardadas juntas a `POST /api/lecturas/lote`
  (una lista de hasta 500). Las que ya existían se ignoran, así que reintentar no duplica datos.

## Descargar los datos para el ML

Botón **Descargar CSV** en la página, o directamente:
`https://TU-APP.onrender.com/api/export?desde=2026-11-21&hasta=2026-11-28`

Y las aperturas de la puerta: botón **Descargar aperturas (CSV)** o `/api/puerta/export`.

```python
import pandas as pd
df = pd.read_csv("lecturas_nevera.csv", parse_dates=["fecha"])
puerta = pd.read_csv("aperturas_puerta.csv", parse_dates=["inicio", "fin"])
```
