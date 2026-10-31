from datetime import datetime, timedelta
from collections import Counter
from typing import Optional
import time

from ...shared.database.databaseMSSQL import get_connection
from ...shared.database.databasePostgress import get_connection as get_pg_connection
from ..models.consumo import ConsumoAgua, SetPointAgua
from ..utils.normalizacion import (
    clave_consumo, clave_fila, clave_medidor, fecha_solo, fila_a_setpoint, indice_columnas,
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
    query = """
        EXEC dbo.SP_ReporteConsumoAguaDiario_PowerBI
            @fecIni=?, @fecFin=?
    """

    for intento in range(1, MAX_RETRIES + 1):
        try:
            with get_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(query, (fecha, fecha))

                    while cursor.description is None:
                        if not cursor.nextset():
                            return []

                    columnas = [
                        columna[0] for columna in cursor.description
                    ]
                    rows = cursor.fetchall()

            resultados = []

            for row in rows:
                fila = dict(zip(columnas, row))

                resultados.append(
                    ConsumoAgua(
                        cc=int(fila.get("CC") or 0),
                        idVBox=int(fila.get("idVBox") or 0),
                        Sucursal=str(fila.get("Sucursal") or ""),
                        Cliente=str(fila.get("cliente") or ""),
                        Area=str(fila.get("Area") or ""),
                        NombreMedidor=str(fila.get("NombreMedidor") or ""),
                        Ciudad=str(fila.get("ciudad") or ""),
                        Formato=str(fila.get("Formato") or ""),
                        Mercado=str(fila.get("Mercado") or ""),
                        Campo=str(fila.get("Campo") or ""),
                        tipo=str(fila.get("tipo") or ""),
                        M2=fila.get("M2"),
                        Fecha=fila.get("Fecha"),
                        consumoTotal=fila.get("consumoTotal"),
                        hora=int(fila.get("hora") or 0),
                        ConsumoHora=fila.get("ConsumoHora"),
                    )
                )

            return resultados

        except Exception as e:
            print(
                f"[MSSQL {fecha}] ERROR "
                f"{intento}/{MAX_RETRIES}: {e}"
            )

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
    """Comprueba que CADA lectura se asoció a una fila física correcta en PG.

    Tolera cc NULL/0 de tablas históricas, y un único cambio de área
    documentado (7369, 2026-09-01); no ignora diferencias en otros equipos.
    Lanza error ANTES de reclasificar cuando detecta inconsistencias.
    """
    from ..repositories.postgres_repository import _COLUMNAS_ORIGEN
    from ..utils.normalizacion import _decimal_pg, _fecha_pg

    origen = {clave_fila(c) for c in consumos_recibidos}
    if len(registros_db) != len(origen) or set(registros_db) != origen:
        raise RuntimeError(
            f'Sincronización incompleta: {len(origen)} firmas origen; '
            f'{len(registros_db)} firmas devueltas')

    ids_por_firma = {}
    for firma, registro in registros_db.items():
        id_ = registro.id
        if id_ is None:
            raise RuntimeError('Sincronización: registro sin ID de PostgreSQL')
        ids_por_firma[firma] = int(id_)
    if len(set(ids_por_firma.values())) != len(origen):
        raise RuntimeError('Sincronización: dos lecturas distintas usan el mismo ID')

    originales_por_id = {v: k for k, v in ids_por_firma.items()}
    encontrados = set()
    inconsistencias = []
    ids = sorted(originales_por_id)
    cols = ', '.join(_COLUMNAS_ORIGEN)

    with get_pg_connection() as conexion:
        with conexion.cursor() as cursor:
            for start in range(0, len(ids), 5000):
                lote = ids[start:start + 5000]
                cursor.execute(
                    f'SELECT id, {cols} FROM public.consumo_agua WHERE id = ANY(%s)',
                    (lote,),
                )
                for r in cursor.fetchall():
                    id_ = int(r[0])
                    encontrados.add(id_)
                    firma_pg = (
                        None if r[1] is None else int(r[1]), int(r[2]),
                        r[3] or '', r[4] or '', r[5] or '', r[6] or '',
                        r[7] or '', r[8] or '', _decimal_pg(r[9]),
                        _fecha_pg(r[10]), _decimal_pg(r[11]), int(r[12]),
                        _decimal_pg(r[13]), r[14] or '', r[15] or '', r[16] or '',
                    )
                    esperada = originales_por_id[id_]
                    # Los históricos anteriores a la columna cc tienen NULL;
                    # cuando su valor era 0 también se permite reutilizarlo.
                    if firma_pg[0] in (None, 0):
                        firma_pg = (esperada[0], *firma_pg[1:])
                    if (
                        esperada[1] == 7369
                        and fecha_solo(esperada[9]).isoformat() >= '2026-09-01'
                        and esperada[14] == 'Demasías'
                        and firma_pg[14] == 'Agua principal'
                    ):
                        firma_pg = (*firma_pg[:14], 'Demasías', firma_pg[15])
                    if firma_pg != esperada:
                        inconsistencias.append((id_, esperada[1], esperada[11]))

    faltan_ids = set(ids) - encontrados
    print('\n' + '=' * 65)
    print('VERIFICACIÓN REAL — MSSQL / IDs FÍSICOS EN POSTGRESQL')
    print('=' * 65)
    print(f'Filas distintas de origen: {len(origen)}')
    print(f'IDs físicos encontrados: {len(encontrados)}')
    print(f'IDs ausentes: {len(faltan_ids)}')
    print(f'Filas con campos diferentes: {len(inconsistencias)}')
    if faltan_ids or inconsistencias:
        raise RuntimeError(
            f'Sincronización NO válida: {len(faltan_ids)} IDs inexistentes; '
            f'{len(inconsistencias)} lecturas con diferencias. '
            f'Ejemplos: {inconsistencias[:5]}')
    print('[OK] Todas las mediciones de origen corresponden a filas físicas')
    return True
