from datetime import datetime, date, time
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re

from ..models.consumo import ConsumoAgua, SetPointAgua

FORMATOS_M3 = {
    "BA", "MB", "SAM'S CLUB", "SAMS CLUB", "WALMART", "WMEX",
}


def normalizar_medidor(medidor: str) -> str:
    texto = str(medidor or "").strip().upper()
    texto = re.sub(r"\s+", " ", texto)
    return texto.strip(" .")


def formato_es_m3(formato) -> bool:
    # Se conserva por compatibilidad. La regla vigente de conversión de
    # setpoints ya no usa Formato; usa Cliente == GSE - GRUPO WALMART.
    return str(formato or "").strip().upper() in FORMATOS_M3


def fecha_solo(fecha) -> date:
    if isinstance(fecha, datetime):
        return fecha.date()
    if isinstance(fecha, date):
        return fecha
    return datetime.strptime(str(fecha), "%Y-%m-%d").date()


def clave_medidor(id_vbox: int, medidor: str) -> tuple:
    return (id_vbox, normalizar_medidor(medidor))


def clave_consumo(consumo: ConsumoAgua) -> tuple:
    return (
        consumo.idVBox,
        normalizar_medidor(consumo.Area),
        fecha_solo(consumo.Fecha),
        consumo.hora,
    )



def _decimal_pg(valor):
    """Normaliza un número con la precisión NUMERIC(...,2) de PostgreSQL."""
    if valor is None:
        return None
    try:
        numero = Decimal(str(valor))
        if not numero.is_finite():
            return None
        return numero.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError):
        return None


def _fecha_pg(valor):
    """Fecha/hora almacenada en PG; no convierte fecha de consumo a hora de ejecución."""
    if isinstance(valor, datetime):
        return valor.replace(tzinfo=None)
    if isinstance(valor, date):
        return datetime.combine(valor, time.min)
    return datetime.fromisoformat(str(valor))


def clave_fila(consumo: ConsumoAgua) -> tuple:
    """Identidad de una fila ORIGINAL de MSSQL (16 campos), sin clasificaciones.

    A diferencia de clave_consumo, conserva NombreMedidor, consumos horarios y
    demás campos de origen. tag, categoria, setpoint y métricas NO son parte de
    la identidad: se actualizan sobre la misma fila en PostgreSQL.
    """
    return (
        int(consumo.cc), int(consumo.idVBox),
        str(consumo.Sucursal or ""), str(consumo.NombreMedidor or ""),
        str(consumo.Formato or ""), str(consumo.Mercado or ""),
        str(consumo.Campo or ""), str(consumo.tipo or ""),
        _decimal_pg(consumo.M2), _fecha_pg(consumo.Fecha),
        _decimal_pg(consumo.consumoTotal), int(consumo.hora),
        _decimal_pg(consumo.ConsumoHora), str(consumo.Cliente or ""),
        str(consumo.Area or ""), str(consumo.Ciudad or ""),
    )

def normalizar_nombre_columna(nombre) -> str:
    return re.sub(r"[\s_]+", "", str(nombre or "").strip().lower())


def indice_columnas(descripcion) -> dict:
    return {
        normalizar_nombre_columna(col[0]): idx
        for idx, col in enumerate(descripcion or [])
    }


def fila_a_setpoint(row, columnas: dict) -> SetPointAgua:
    def buscar(*alias):
        for a in alias:
            key = normalizar_nombre_columna(a)
            if key in columnas:
                return row[columnas[key]]
        raise KeyError(
            f"No se encontró ninguna columna entre {alias}. "
            f"Columnas disponibles: {list(columnas.keys())}"
        )

    return SetPointAgua(
        idVBox=buscar("idVBox", "id_vbox", "id"),
        Medidor=buscar("Medidor", "NombreMedidor", "Nombre_Medidor", "Area"),
        weekdaysValue=buscar(
            "weekdaysValue", "weekdays_value", "weekdayValue",
            "weekday_value", "weekdays", "weekday",
        ),
        weekendValue=buscar(
            "weekendValue", "weekend_value", "weekend", "weekends",
        ),
    )
