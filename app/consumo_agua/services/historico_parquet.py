from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta
import json
from pathlib import Path
from typing import Callable, Iterable

import pandas as pd

from ..models.consumo import ConsumoAgua


ROOT_DIR = Path(__file__).resolve().parents[3]
DATA_DIR = ROOT_DIR / "data"
PARQUET_PATH = DATA_DIR / "consumo_agua_historico.parquet"
META_PATH = DATA_DIR / "consumo_agua_historico.meta.json"

COLUMNAS_CONSUMO = [
    "cc","idVBox", "Sucursal", "Cliente", "Area", "NombreMedidor", "Ciudad",
    "Formato", "Mercado", "Campo", "tipo", "M2", "Fecha",
    "consumoTotal", "hora", "ConsumoHora", "tag", "categoria", "motivo",
    "setPointSemana", "setPointFinSemana", "setpoint_agua",
]

CLAVE_CACHE = ["idVBox", "Area", "Fecha", "hora"]


def _asegurar_directorio() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _leer_meta() -> dict:
    if not META_PATH.exists():
        return {"version": 1, "fechas_completas": {}}
    try:
        return json.loads(META_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"version": 1, "fechas_completas": {}}


def _guardar_meta(meta: dict) -> None:
    _asegurar_directorio()
    META_PATH.write_text(
        json.dumps(meta, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )


def _consumos_a_dataframe(consumos: Iterable[ConsumoAgua]) -> pd.DataFrame:
    filas = [asdict(c) for c in consumos]
    if not filas:
        return pd.DataFrame(columns=COLUMNAS_CONSUMO)
    df = pd.DataFrame(filas)
    for columna in COLUMNAS_CONSUMO:
        if columna not in df.columns:
            df[columna] = None
    df = df[COLUMNAS_CONSUMO]
    df["Fecha"] = pd.to_datetime(df["Fecha"], errors="coerce")
    return df



def _dataframe_a_consumos(df: pd.DataFrame) -> list[ConsumoAgua]:
    if df.empty:
        return []

    resultado: list[ConsumoAgua] = []

    for fila in df.to_dict(orient="records"):
        fecha = fila.get("Fecha")

        if pd.isna(fecha):
            continue

        if hasattr(fecha, "to_pydatetime"):
            fecha = fecha.to_pydatetime()

        resultado.append(
            ConsumoAgua(
                cc=int(fila.get("cc") or 0),
                idVBox=int(fila.get("idVBox") or 0),
                Sucursal=str(fila.get("Sucursal") or ""),
                Cliente=str(fila.get("Cliente") or ""),
                Area=str(fila.get("Area") or ""),
                NombreMedidor=str(fila.get("NombreMedidor") or ""),
                Ciudad=str(fila.get("Ciudad") or ""),
                Formato=str(fila.get("Formato") or ""),
                Mercado=str(fila.get("Mercado") or ""),
                Campo=str(fila.get("Campo") or ""),
                tipo=str(fila.get("tipo") or ""),
                M2=fila.get("M2"),
                Fecha=fecha,
                consumoTotal=fila.get("consumoTotal"),
                hora=int(fila.get("hora") or 0),
                ConsumoHora=fila.get("ConsumoHora"),
                tag=str(fila.get("tag") or ""),
                categoria=str(fila.get("categoria") or ""),
                motivo=str(fila.get("motivo") or ""),
                setPointSemana=float(fila.get("setPointSemana") or 0),
                setPointFinSemana=float(fila.get("setPointFinSemana") or 0),
                setpoint_agua=float(fila.get("setpoint_agua") or 0),
            )
        )

    return resultado

def leer_historico() -> pd.DataFrame:
    if not PARQUET_PATH.exists():
        return pd.DataFrame(columns=COLUMNAS_CONSUMO)
    df = pd.read_parquet(PARQUET_PATH)
    if "Fecha" in df.columns:
        df["Fecha"] = pd.to_datetime(df["Fecha"], errors="coerce")
    return df


def guardar_consultas(consumos: Iterable[ConsumoAgua], fechas_completas=None) -> None:
    nuevos = _consumos_a_dataframe(consumos)
    if nuevos.empty and not fechas_completas:
        return

    _asegurar_directorio()
    existente = leer_historico()

    # Si una fecha se volvió a consultar completa, sustituimos únicamente
    # ese día en el cache. Se conservan TODAS las filas devueltas por MSSQL,
    # incluidos duplicados legítimos.
    if fechas_completas and not existente.empty:
        fechas_reemplazar = pd.to_datetime(
            list(fechas_completas.keys())
        ).normalize()

        existente["Fecha"] = pd.to_datetime(
            existente["Fecha"],
            errors="coerce",
        )

        mascara_reemplazo = (
            existente["Fecha"]
            .dt.normalize()
            .isin(fechas_reemplazar)
        )

        existente = existente.loc[
            ~mascara_reemplazo
        ].copy()

    combinado = pd.concat(
        [existente, nuevos],
        ignore_index=True,
    )

    if not combinado.empty:
        combinado["Fecha"] = pd.to_datetime(
            combinado["Fecha"],
            errors="coerce",
        )

        combinado = combinado.dropna(
            subset=["Fecha"]
        )

        # Ordenamos solo por fecha y de forma estable para conservar el
        # orden original de las filas dentro de cada consulta diaria.
        combinado = combinado.sort_values(
            ["Fecha"],
            kind="stable",
        ).reset_index(drop=True)

        combinado.to_parquet(
            PARQUET_PATH,
            index=False,
        )

    if fechas_completas:
        meta = _leer_meta()
        completas = meta.setdefault("fechas_completas", {})
        for fecha, cantidad in fechas_completas.items():
            completas[str(fecha)] = int(cantidad)
        meta["actualizado_en"] = datetime.now().isoformat(timespec="seconds")
        _guardar_meta(meta)


def _rango_fechas(fec_ini: str, fec_fin: str) -> list[str]:
    inicio = datetime.strptime(fec_ini, "%Y-%m-%d").date()
    fin = datetime.strptime(fec_fin, "%Y-%m-%d").date()
    if fin < inicio:
        raise ValueError("fec_fin no puede ser anterior a fec_ini")
    fechas = []
    actual = inicio
    while actual <= fin:
        fechas.append(actual.isoformat())
        actual += timedelta(days=1)
    return fechas


def obtener_consumos_con_cache(
    fec_ini: str,
    fec_fin: str,
    consultar_dia: Callable[[str], list[ConsumoAgua]],
) -> list[ConsumoAgua]:
    """Obtiene un rango usando Parquet y consulta MSSQL solo para días faltantes.

    Una fecha se considera reutilizable únicamente si aparece en el archivo meta
    como consulta completada. Esto evita tratar un día parcial como cache válido.
    """
    fechas = _rango_fechas(fec_ini, fec_fin)
    meta = _leer_meta()
    completas = meta.get("fechas_completas", {})
    faltantes = [f for f in fechas if f not in completas]

    if faltantes:
        print(
            f"[CACHE] {len(faltantes)}/{len(fechas)} días no están en Parquet; "
            "se consultarán en MSSQL."
        )
        nuevos: list[ConsumoAgua] = []
        conteos = {}
        for indice, fecha in enumerate(faltantes, start=1):
            print(f"[CACHE MSSQL {indice}/{len(faltantes)}] Consultando {fecha}...")
            datos = consultar_dia(fecha)
            nuevos.extend(datos)
            if datos:
                conteos[fecha] = len(datos)
                print(f"[CACHE MSSQL {fecha}] OK - {len(datos)} registros")
            else:
                print(
                    f"[CACHE MSSQL {fecha}] ADVERTENCIA - 0 registros; "
                    "la fecha no se marcará como completa."
                )
        guardar_consultas(nuevos, conteos)
    else:
        print(f"[CACHE] {len(fechas)}/{len(fechas)} días disponibles en Parquet.")

    historico = leer_historico()
    if historico.empty:
        return []

    fechas_normalizadas = pd.to_datetime(fechas)
    mascara = historico["Fecha"].dt.normalize().isin(fechas_normalizadas)
    seleccionado = historico.loc[mascara].copy()

    seleccionado = seleccionado.sort_values(
        ["Fecha"],
        kind="stable",
    )

    resultado = _dataframe_a_consumos(seleccionado)
    print(f"[CACHE] Rango entregado: {len(resultado)} registros")
    return resultado
