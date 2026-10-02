from datetime import datetime, timedelta
from typing import Optional
import time

from ...shared.database.databaseMSSQL import get_connection
from ..models.consumo import ConsumoAgua, SetPointAgua
from ..utils.normalizacion import (
    clave_consumo, clave_medidor, fecha_solo, fila_a_setpoint, indice_columnas,
)

MAX_RETRIES = 3
RETRY_DELAY = 2
CLIENTE_WALMART = "GSE - GRUPO WALMART"


def ejecutar_por_dia(fec_ini: str, fec_fin: str, funcion_dia) -> list[ConsumoAgua]:
    fecha_inicio = datetime.strptime(fec_ini, "%Y-%m-%d").date()
    fecha_fin = datetime.strptime(fec_fin, "%Y-%m-%d").date()
    total_dias = (fecha_fin - fecha_inicio).days + 1
    resultados = []
    print("\n" + "=" * 70)
    print("MSSQL - INICIO DE DESCARGA")
    print("=" * 70)
    print(f"Rango: {fecha_inicio} -> {fecha_fin}")
    print(f"Días: {total_dias}")
    print("=" * 70)
    fecha_actual = fecha_inicio
    dia = 0
    while fecha_actual <= fecha_fin:
        dia += 1
        fecha_str = fecha_actual.strftime("%Y-%m-%d")
        print(f"[MSSQL {dia}/{total_dias}] Consultando {fecha_str}...")
        resultado_dia = []
        for intento in range(1, MAX_RETRIES + 1):
            try:
                inicio = time.perf_counter()
                resultado_dia = funcion_dia(fecha_str)
                segundos = time.perf_counter() - inicio
                print(
                    f"[MSSQL {fecha_str}] OK - {len(resultado_dia)} registros "
                    f"({segundos:.2f}s)"
                )
                break
            except Exception as e:
                print(f"[MSSQL {fecha_str}] ERROR {intento}/{MAX_RETRIES}: {e}")
                if intento < MAX_RETRIES:
                    time.sleep(RETRY_DELAY)
        resultados.extend(resultado_dia)
        fecha_actual += timedelta(days=1)
    print(f"\nMSSQL - FIN: {len(resultados)} registros")
    print("=" * 70)
    return resultados


def _valor_numerico(valor) -> float:
    if valor is None or isinstance(valor, bool):
        return 0.0
    try:
        return float(valor)
    except (TypeError, ValueError, OverflowError):
        return 0.0


def cargar_setpoints() -> dict:
    query = "EXEC SP_ConsultaSetpointAgua"
    print("\n[SETPOINT] Consultando set points...")
    for intento in range(1, MAX_RETRIES + 1):
        try:
            with get_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(query)
                    while cursor.description is None:
                        if not cursor.nextset():
                            return {}
                    columnas = indice_columnas(cursor.description)
                    rows = cursor.fetchall()
            resultado = {}
            filas_ignoradas = 0
            for row in rows:
                try:
                    sp = fila_a_setpoint(row, columnas)
                except KeyError as e:
                    filas_ignoradas += 1
                    if filas_ignoradas <= 5:
                        print(f"[SETPOINT] Fila ignorada: {e}")
                    continue
                clave = clave_medidor(sp.idVBox, sp.Medidor)
                sp.weekdaysValue = _valor_numerico(sp.weekdaysValue)
                sp.weekendValue = _valor_numerico(sp.weekendValue)
                if clave in resultado:
                    existente = resultado[clave]
                    existente.weekdaysValue += sp.weekdaysValue
                    existente.weekendValue += sp.weekendValue
                else:
                    resultado[clave] = sp
            mensaje = f"[SETPOINT] OK - {len(resultado)} set points indexados"
            if filas_ignoradas:
                mensaje += f" ({filas_ignoradas} filas ignoradas)"
            print(mensaje)
            if resultado:
                con_valor = sum(
                    1 for sp in resultado.values()
                    if sp.weekdaysValue > 0 or sp.weekendValue > 0
                )
                print(f"[SETPOINT] Con valor > 0: {con_valor}/{len(resultado)}")
                for _, sp in list(resultado.items())[:5]:
                    print(
                        f"[SETPOINT] Ejemplo: idVBox={sp.idVBox} "
                        f"Medidor={sp.Medidor!r} weekdays={sp.weekdaysValue} "
                        f"weekend={sp.weekendValue}"
                    )
                if con_valor == 0:
                    print("[SETPOINT] ADVERTENCIA: todos los setpoints cargados tienen valor 0.")
            return resultado
        except Exception as e:
            print(f"[SETPOINT] ERROR {intento}/{MAX_RETRIES}: {e}")
            if intento < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
    return {}


def consumoTotalHora(fecha) -> list[ConsumoAgua]:
    query = "EXEC dbo.SP_ReporteConsumoAguaDiario_PowerBI @fecIni=?, @fecFin=?"
    for intento in range(1, MAX_RETRIES + 1):
        try:
            with get_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(query, (fecha, fecha))
                    while cursor.description is None:
                        if not cursor.nextset():
                            return []
                    rows = cursor.fetchall()
            return [ConsumoAgua(*row) for row in rows]
        except Exception as e:
            print(f"[MSSQL {fecha}] ERROR {intento}/{MAX_RETRIES}: {e}")
            if intento < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
    return []


def buscar_setpoint(setpoints: dict, consumo: ConsumoAgua) -> Optional[SetPointAgua]:
    return setpoints.get(clave_medidor(consumo.idVBox, consumo.Area))


def aplicar_setpoints(consumos: list[ConsumoAgua], setpoints: dict) -> None:
    convertidos = 0
    asignados = 0
    sin_sp = 0
    con_sp_positivo = 0
    medidores_sin_sp: dict[tuple, int] = {}
    for consumo in consumos:
        sp = buscar_setpoint(setpoints, consumo)
        if not sp:
            sin_sp += 1
            clave = clave_medidor(consumo.idVBox, consumo.Area)
            medidores_sin_sp[clave] = medidores_sin_sp.get(clave, 0) + 1
            continue
        weekdays = float(sp.weekdaysValue or 0)
        weekend = float(sp.weekendValue or 0)
        if str(consumo.Cliente or "").strip().upper() == CLIENTE_WALMART:
            if weekdays > 0:
                weekdays *= 1000
            if weekend > 0:
                weekend *= 1000
            convertidos += 1
        consumo.setPointSemana = weekdays
        consumo.setPointFinSemana = weekend
        fecha = fecha_solo(consumo.Fecha)
        consumo.setpoint_agua = weekend if fecha.weekday() >= 5 else weekdays
        if consumo.setpoint_agua > 0:
            con_sp_positivo += 1
        asignados += 1
    # Se conserva el texto del monolito para equivalencia de salida.
    print(
        f"[SETPOINT] Aplicados: {asignados} "
        f"(convertidos M3 -> L: {convertidos})"
    )
    print(f"[SETPOINT] Con setpoint_agua > 0: {con_sp_positivo}/{asignados}")
    if asignados > 0 and con_sp_positivo == 0:
        print("[SETPOINT] ADVERTENCIA: todos los setpoints aplicados quedaron en 0.")
    if sin_sp:
        print(
            f"[SETPOINT] ADVERTENCIA: {sin_sp} consumos sin set point "
            f"coincidente ({len(medidores_sin_sp)} medidores únicos)."
        )
        for (id_vbox, medidor), cantidad in list(medidores_sin_sp.items())[:20]:
            print(f"    - idVBox={id_vbox} Medidor={medidor!r} ({cantidad} registros)")
        if len(medidores_sin_sp) > 20:
            print(f"    ... y {len(medidores_sin_sp) - 20} medidores más.")


def verificar_sincronizacion(consumos_recibidos: list[ConsumoAgua], registros_db: dict) -> bool:
    claves_mssql = {clave_consumo(c) for c in consumos_recibidos}
    claves_postgres = set(registros_db.keys())
    print("\n" + "=" * 70)
    print("VERIFICACIÓN FINAL")
    print("=" * 70)
    print(f"MSSQL recibidos (incluye duplicados): {len(consumos_recibidos)}")
    print(f"Claves únicas en MSSQL: {len(claves_mssql)}")
    print(f"PostgreSQL encontrados: {len(claves_postgres)}")
    print(f"Faltantes: {len(claves_mssql - claves_postgres)}")
    print(f"Adicionales: {len(claves_postgres - claves_mssql)}")
    if claves_mssql - claves_postgres:
        print("\n[ERROR] REGISTROS FALTANTES:")
        for clave in list(claves_mssql - claves_postgres)[:20]:
            print(f"  {clave}")
        return False
    print("\n[OK] TODAS LAS CLAVES ÚNICAS DE MSSQL ESTÁN EN POSTGRESQL.")
    print("=" * 70)
    return True
