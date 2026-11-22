"""
Monitor de consumo de la nevera - monolito en FastAPI.

- POST /api/lecturas        -> la ESP32 envía UNA lectura (protegido con X-API-Key)
- POST /api/lecturas/lote   -> la ESP32 envía VARIAS lecturas juntas (las que guardó
                               mientras no había internet). Protegido con X-API-Key.
- GET  /api/lecturas        -> últimas lecturas (para cargar las gráficas)
- GET  /api/export          -> descarga las lecturas en CSV (para el ML)
                               opcional: ?desde=2026-11-21&hasta=2026-11-28
- POST /api/puerta          -> la ESP32 envía las aperturas de la puerta (inicio, fin y
                               duración exactos). Protegido con X-API-Key.
- GET  /api/puerta          -> últimas aperturas (para la página)
- GET  /api/puerta/export   -> descarga las aperturas en CSV (mismos filtros de fecha)
- WS   /ws                  -> envía cada lectura nueva a la página en tiempo real
- GET  /                    -> página web con el dashboard
- GET  /health              -> para comprobar que el servidor está vivo

Fechas: la ESP32 manda la hora de su reloj DS3231 (hora de Colombia). Si no la
manda, se usa la hora en que llegó al servidor. Si la misma lectura llega dos
veces (por un reintento), solo se guarda una vez.
"""

import csv
import io
import os
import secrets
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import Boolean, DateTime, Float, Integer, create_engine, select
from sqlalchemy.dialects.postgresql import insert as insert_postgres
from sqlalchemy.dialects.sqlite import insert as insert_sqlite
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

# ---------------------------------------------------------------------------
# Configuración (variables de entorno)
# ---------------------------------------------------------------------------
# En tu PC usa SQLite. En Render, DATABASE_URL la pone render.yaml automáticamente.
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./lecturas.db")
# Render entrega "postgres://..." o "postgresql://..."; SQLAlchemy necesita el driver.
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)
ES_SQLITE = DATABASE_URL.startswith("sqlite")

API_KEY = os.getenv("API_KEY", "clave-de-prueba")
if os.getenv("RENDER") and API_KEY == "clave-de-prueba":
    # En Render nunca debe quedar la clave de prueba
    raise RuntimeError("Configura la variable API_KEY en Render")

# Colombia no tiene horario de verano: siempre UTC-5
HORA_COLOMBIA = timezone(timedelta(hours=-5))

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    connect_args={"check_same_thread": False} if ES_SQLITE else {},
)


# ---------------------------------------------------------------------------
# Base de datos
# ---------------------------------------------------------------------------
class Base(DeclarativeBase):
    pass


class Lectura(Base):
    __tablename__ = "lecturas"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    fecha: Mapped[datetime] = mapped_column(DateTime(timezone=True), unique=True, index=True)
    recibido: Mapped[datetime] = mapped_column(DateTime(timezone=True))   # hora del servidor
    # Energía (PZEM-004T)
    voltaje: Mapped[Optional[float]] = mapped_column(Float)         # V
    corriente: Mapped[Optional[float]] = mapped_column(Float)       # A
    potencia: Mapped[Optional[float]] = mapped_column(Float)        # W
    energia: Mapped[Optional[float]] = mapped_column(Float)         # kWh acumulada
    fp: Mapped[Optional[float]] = mapped_column(Float)              # factor de potencia
    frecuencia: Mapped[Optional[float]] = mapped_column(Float)      # Hz
    # Temperaturas
    temp_interna: Mapped[Optional[float]] = mapped_column(Float)    # °C (DS18B20)
    temp_ambiente: Mapped[Optional[float]] = mapped_column(Float)   # °C (DHT11)
    humedad: Mapped[Optional[float]] = mapped_column(Float)         # % (DHT11)
    # Puerta (reed switch)
    puerta_abierta: Mapped[Optional[bool]] = mapped_column(Boolean)       # estado en ese instante
    segundos_abierta: Mapped[Optional[float]] = mapped_column(Float)      # s abierta desde la lectura anterior
    aperturas: Mapped[Optional[int]] = mapped_column(Integer)


class EventoPuerta(Base):
    """Una fila por cada vez que se abrió la puerta."""
    __tablename__ = "eventos_puerta"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    inicio: Mapped[datetime] = mapped_column(DateTime(timezone=True), unique=True, index=True)
    fin: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    duracion_s: Mapped[float] = mapped_column(Float)
    recibido: Mapped[datetime] = mapped_column(DateTime(timezone=True))


Base.metadata.create_all(engine)

COLUMNAS = ["voltaje", "corriente", "potencia", "energia", "fp", "frecuencia",
            "temp_interna", "temp_ambiente", "humedad", "puerta_abierta", "segundos_abierta",
            "aperturas"]


def a_hora_colombia(fecha: datetime) -> datetime:
    if fecha.tzinfo is None:          # SQLite no guarda la zona horaria (se guarda en UTC)
        fecha = fecha.replace(tzinfo=timezone.utc)
    return fecha.astimezone(HORA_COLOMBIA)


def a_dict(f: Lectura) -> dict:
    datos = {"id": f.id, "fecha": a_hora_colombia(f.fecha).isoformat()}
    datos.update({c: getattr(f, c) for c in COLUMNAS})
    return datos


def evento_a_dict(e: EventoPuerta) -> dict:
    return {"tipo": "puerta", "id": e.id,
            "inicio": a_hora_colombia(e.inicio).isoformat(),
            "fin": a_hora_colombia(e.fin).isoformat(),
            "duracion_s": e.duracion_s}


def a_utc(fecha: datetime) -> datetime:
    """Fechas sin zona (las del RTC) son hora de Colombia. Se guardan en UTC."""
    if fecha.tzinfo is None:
        fecha = fecha.replace(tzinfo=HORA_COLOMBIA)
    return fecha.astimezone(timezone.utc)


def rango_utc(desde: Optional[date], hasta: Optional[date]):
    """Convierte días de Colombia en límites UTC (incluye el día 'hasta' completo)."""
    inicio = fin = None
    if desde:
        inicio = datetime.combine(desde, datetime.min.time(), HORA_COLOMBIA).astimezone(timezone.utc)
    if hasta:
        fin = datetime.combine(hasta + timedelta(days=1), datetime.min.time(),
                               HORA_COLOMBIA).astimezone(timezone.utc)
    return inicio, fin


# ---------------------------------------------------------------------------
# Lo que envía la ESP32 (FastAPI valida los datos automáticamente).
# Todos los campos son opcionales: si un sensor falla, la ESP32 manda null.
# ---------------------------------------------------------------------------
class LecturaEntrada(BaseModel):
    fecha: Optional[datetime] = None      # hora del RTC, ej. "2026-11-21T10:30:00"
    voltaje: Optional[float] = Field(None, ge=0, le=400)
    corriente: Optional[float] = Field(None, ge=0, le=100)
    potencia: Optional[float] = Field(None, ge=0, le=25000)
    energia: Optional[float] = Field(None, ge=0)
    fp: Optional[float] = Field(None, ge=0, le=1)
    frecuencia: Optional[float] = Field(None, ge=40, le=70)
    temp_interna: Optional[float] = Field(None, ge=-40, le=80)
    temp_ambiente: Optional[float] = Field(None, ge=-10, le=60)
    humedad: Optional[float] = Field(None, ge=0, le=100)
    puerta_abierta: Optional[bool] = None
    segundos_abierta: Optional[float] = Field(None, ge=0, le=3600)
    aperturas: Optional[int] = Field(None, ge=0)


class EventoEntrada(BaseModel):
    inicio: datetime                      # cuándo se abrió (hora del RTC)
    fin: datetime                         # cuándo se cerró
    duracion_s: Optional[float] = Field(None, ge=0, le=86400)   # si no viene, se calcula


def preparar_fila(datos: LecturaEntrada, ahora: datetime) -> dict:
    fila = datos.model_dump()
    fila["fecha"] = a_utc(fila["fecha"] or ahora)
    fila["recibido"] = ahora
    return fila


def guardar(lecturas: list[LecturaEntrada]) -> list[dict]:
    """Guarda las lecturas ignorando las que ya existían (misma fecha). Devuelve las nuevas."""
    ahora = datetime.now(timezone.utc)
    filas = [preparar_fila(l, ahora) for l in lecturas]
    insertar = insert_sqlite if ES_SQLITE else insert_postgres
    with Session(engine) as s:
        consulta = (insertar(Lectura).values(filas)
                    .on_conflict_do_nothing(index_elements=["fecha"])
                    .returning(Lectura))
        nuevas = s.scalars(consulta).all()
        resultado = sorted((a_dict(f) for f in nuevas), key=lambda d: d["fecha"])
        s.commit()
        return resultado


def guardar_eventos(eventos: list[EventoEntrada]) -> list[dict]:
    """Guarda aperturas de puerta ignorando las repetidas (mismo inicio)."""
    ahora = datetime.now(timezone.utc)
    filas = []
    for e in eventos:
        inicio, fin = a_utc(e.inicio), a_utc(e.fin)
        if fin < inicio:
            raise HTTPException(status_code=422, detail="'fin' no puede ser antes de 'inicio'")
        duracion = e.duracion_s if e.duracion_s is not None else (fin - inicio).total_seconds()
        filas.append({"inicio": inicio, "fin": fin, "duracion_s": duracion, "recibido": ahora})
    insertar = insert_sqlite if ES_SQLITE else insert_postgres
    with Session(engine) as s:
        consulta = (insertar(EventoPuerta).values(filas)
                    .on_conflict_do_nothing(index_elements=["inicio"])
                    .returning(EventoPuerta))
        nuevos = s.scalars(consulta).all()
        resultado = sorted((evento_a_dict(e) for e in nuevos), key=lambda d: d["inicio"])
        s.commit()
        return resultado


def leer_eventos(limite: int) -> list[dict]:
    with Session(engine) as s:
        filas = s.scalars(select(EventoPuerta).order_by(EventoPuerta.inicio.desc()).limit(limite)).all()
        return [evento_a_dict(e) for e in filas]          # el más reciente primero


def leer_ultimas(limite: int) -> list[dict]:
    with Session(engine) as s:
        filas = s.scalars(select(Lectura).order_by(Lectura.fecha.desc()).limit(limite)).all()
        return [a_dict(f) for f in reversed(filas)]


# ---------------------------------------------------------------------------
# Conexiones WebSocket abiertas (las páginas que están mirando)
# ---------------------------------------------------------------------------
class GestorConexiones:
    def __init__(self):
        self.activas: list[WebSocket] = []

    async def conectar(self, ws: WebSocket):
        await ws.accept()
        self.activas.append(ws)

    def desconectar(self, ws: WebSocket):
        if ws in self.activas:
            self.activas.remove(ws)

    async def enviar_a_todos(self, mensaje: dict):
        for ws in list(self.activas):
            try:
                await ws.send_json(mensaje)
            except Exception:
                self.desconectar(ws)


gestor = GestorConexiones()


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="Monitor Nevera")
app.mount("/static", StaticFiles(directory="static"), name="static")


def revisar_clave(x_api_key: str):
    if not secrets.compare_digest(x_api_key.encode(), API_KEY.encode()):
        raise HTTPException(status_code=401, detail="API key incorrecta")


@app.post("/api/lecturas", status_code=201)
async def recibir_lectura(datos: LecturaEntrada, x_api_key: str = Header(default="")):
    revisar_clave(x_api_key)
    nuevas = await run_in_threadpool(guardar, [datos])
    for l in nuevas:
        await gestor.enviar_a_todos(l)  # tiempo real
    return {"guardadas": len(nuevas)}


@app.post("/api/lecturas/lote", status_code=201)
async def recibir_lote(datos: list[LecturaEntrada], x_api_key: str = Header(default="")):
    revisar_clave(x_api_key)
    if not 1 <= len(datos) <= 500:
        raise HTTPException(status_code=422, detail="Envía entre 1 y 500 lecturas por lote")
    nuevas = await run_in_threadpool(guardar, datos)
    if nuevas:
        await gestor.enviar_a_todos(nuevas[-1])  # a la página solo le interesa la más reciente
    return {"recibidas": len(datos), "guardadas": len(nuevas)}


@app.get("/api/lecturas")
async def ultimas_lecturas(limite: int = 300):
    limite = max(1, min(limite, 5000))
    return await run_in_threadpool(leer_ultimas, limite)


@app.get("/api/export")
def exportar_csv(desde: Optional[date] = None, hasta: Optional[date] = None):
    consulta = select(Lectura).order_by(Lectura.fecha)
    inicio, fin = rango_utc(desde, hasta)     # días en hora de Colombia
    if inicio:
        consulta = consulta.where(Lectura.fecha >= inicio)
    if fin:
        consulta = consulta.where(Lectura.fecha < fin)

    def generar():
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["fecha"] + COLUMNAS)
        yield buffer.getvalue()
        with Session(engine) as s:
            for f in s.scalars(consulta).yield_per(1000):
                buffer.seek(0)
                buffer.truncate(0)
                fila = [a_hora_colombia(f.fecha).strftime("%Y-%m-%d %H:%M:%S")]
                for c in COLUMNAS:
                    v = getattr(f, c)
                    fila.append("" if v is None else int(v) if isinstance(v, bool) else v)
                writer.writerow(fila)
                yield buffer.getvalue()

    return StreamingResponse(
        generar(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=lecturas_nevera.csv"},
    )


@app.post("/api/puerta", status_code=201)
async def recibir_eventos(datos: list[EventoEntrada], x_api_key: str = Header(default="")):
    revisar_clave(x_api_key)
    if not 1 <= len(datos) <= 500:
        raise HTTPException(status_code=422, detail="Envía entre 1 y 500 aperturas por petición")
    nuevos = await run_in_threadpool(guardar_eventos, datos)
    for e in nuevos:
        await gestor.enviar_a_todos(e)
    return {"recibidas": len(datos), "guardadas": len(nuevos)}


@app.get("/api/puerta")
async def ultimas_aperturas(limite: int = 10):
    limite = max(1, min(limite, 500))
    return await run_in_threadpool(leer_eventos, limite)


@app.get("/api/puerta/export")
def exportar_puerta_csv(desde: Optional[date] = None, hasta: Optional[date] = None):
    consulta = select(EventoPuerta).order_by(EventoPuerta.inicio)
    inicio, fin = rango_utc(desde, hasta)
    if inicio:
        consulta = consulta.where(EventoPuerta.inicio >= inicio)
    if fin:
        consulta = consulta.where(EventoPuerta.inicio < fin)

    def generar():
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["inicio", "fin", "duracion_s"])
        with Session(engine) as s:
            for e in s.scalars(consulta):
                writer.writerow([a_hora_colombia(e.inicio).strftime("%Y-%m-%d %H:%M:%S"),
                                 a_hora_colombia(e.fin).strftime("%Y-%m-%d %H:%M:%S"),
                                 round(e.duracion_s, 1)])
        yield buffer.getvalue()

    return StreamingResponse(
        generar(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=aperturas_puerta.csv"},
    )


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await gestor.conectar(ws)
    try:
        while True:
            await ws.receive_text()  # mantiene la conexión abierta
    except WebSocketDisconnect:
        gestor.desconectar(ws)


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/")
def inicio():
    return FileResponse("static/index.html")
