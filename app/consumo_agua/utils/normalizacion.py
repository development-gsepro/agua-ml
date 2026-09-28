from datetime import datetime, date
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
