from datetime import date, timedelta
import math
import time

from psycopg2.extras import execute_values

from ...shared.database.databasePostgress import get_connection as get_conn
from ..models.consumo import ConsumoAgua, ConsumoDB
from ..utils.normalizacion import clave_consumo

BATCH_SIZE = 10000
NUMERIC_10_2_MAX = 99_999_999.99
NUMERIC_20_2_MAX = 99_999_999_999_999_999.99


def valor_numeric_postgres(valor, campo: str, limite_max: float = NUMERIC_20_2_MAX):
    if valor is None:
        return None
    try:
        numero = float(valor)
    except (TypeError, ValueError, OverflowError):
        print(f"[POSTGRES] {campo} inválido ({valor!r}) -> NULL")
        return None
    if not math.isfinite(numero):
        print(f"[POSTGRES] {campo} no finito ({valor!r}) -> NULL")
        return None
    if abs(numero) > limite_max:
        print(
            f"[POSTGRES] {campo} fuera de rango "
            f"(>{limite_max}): {numero!r} -> NULL"
        )
        return None
    return numero


def row_a_consumo_db(row) -> ConsumoDB:
    consumo = ConsumoAgua(
        idVBox=row[1], Sucursal=row[2], NombreMedidor=row[3],
        Formato=row[4], Mercado=row[5], Campo=row[6], tipo=row[7],
        M2=row[8], Fecha=row[9], consumoTotal=row[10], hora=row[11],
        ConsumoHora=row[12], tag=row[13] or "", categoria=row[14] or "",
        setpoint_agua=row[15] or 0.0, Cliente=row[16] or "",
        Area=row[17] or "", Ciudad=row[18] or "",
    )
    return ConsumoDB(id=row[0], consumo=consumo)


def insertar_consumos(consumos: list[ConsumoAgua]) -> dict:
    if not consumos:
        return {}
    print(f"[POSTGRES] Insertando {len(consumos)} registros...")
    query = '''
        INSERT INTO public.consumo_agua (
            "idVBox", "Sucursal", "NombreMedidor", "Formato", "Mercado",
            "Campo", tipo, "M2", "Fecha", "consumoTotal", hora,
            "ConsumoHora", tag, categoria, setpoint_agua, "Cliente",
            "Area", "Ciudad"
        ) VALUES %s RETURNING id
    '''
    resultado = {}
    insertados = 0
    inicio = time.perf_counter()
    with get_conn() as conn:
        with conn.cursor() as cursor:
            for posicion in range(0, len(consumos), BATCH_SIZE):
                lote = consumos[posicion: posicion + BATCH_SIZE]
                valores = [(
                    c.idVBox, c.Sucursal, c.NombreMedidor, c.Formato,
                    c.Mercado, c.Campo, c.tipo,
                    valor_numeric_postgres(c.M2, "M2", NUMERIC_10_2_MAX),
                    c.Fecha,
                    valor_numeric_postgres(c.consumoTotal, "consumoTotal", NUMERIC_20_2_MAX),
                    c.hora,
                    valor_numeric_postgres(c.ConsumoHora, "ConsumoHora", NUMERIC_20_2_MAX),
                    c.tag or "", c.categoria or "",
                    valor_numeric_postgres(c.setpoint_agua, "setpoint_agua", NUMERIC_20_2_MAX),
                    c.Cliente, c.Area, c.Ciudad,
                ) for c in lote]
                rows = execute_values(
                    cursor, query, valores, page_size=BATCH_SIZE, fetch=True
                )
                if len(rows) != len(lote):
                    raise RuntimeError(
                        "INSERT inconsistente: "
                        f"{len(lote)} enviados / {len(rows)} IDs"
                    )
                for consumo, row in zip(lote, rows):
                    resultado[clave_consumo(consumo)] = ConsumoDB(
                        id=row[0], consumo=consumo
                    )
                insertados += len(lote)
                print(f"[POSTGRES] {insertados}/{len(consumos)} insertados")
            conn.commit()
    segundos = time.perf_counter() - inicio
    print(f"[POSTGRES] Insertados: {insertados} ({segundos:.2f}s)")
    return resultado


def obtener_historico_postgres(grupos: set, fecha_inicio: date, fecha_fin: date) -> dict:
    if not grupos:
        return {}
    valores = list(grupos)
    placeholders = ", ".join(["(%s,%s)"] * len(valores))
    query = f'''
        WITH grupos(id_vbox, medidor) AS (VALUES {placeholders})
        SELECT c.id, c."idVBox", c."Sucursal", c."NombreMedidor",
               c."Formato", c."Mercado", c."Campo", c.tipo, c."M2",
               c."Fecha", c."consumoTotal", c.hora, c."ConsumoHora",
               c.tag, c.categoria, c.setpoint_agua, c."Cliente", c."Area",
               c."Ciudad"
        FROM public.consumo_agua c
        INNER JOIN grupos g
            ON c."idVBox" = g.id_vbox
            AND (
                regexp_replace(regexp_replace(UPPER(TRIM(COALESCE(c."Area", ''))), '\\s+', ' ', 'g'), '^[ .]+|[ .]+$', '', 'g') = g.medidor
                OR
                regexp_replace(regexp_replace(UPPER(TRIM(COALESCE(c."NombreMedidor", ''))), '\\s+', ' ', 'g'), '^[ .]+|[ .]+$', '', 'g') = g.medidor
            )
        WHERE c."Fecha" >= %s AND c."Fecha" < %s
    '''
    parametros = []
    for id_vbox, medidor in valores:
        parametros.extend([id_vbox, medidor])
    parametros.extend([fecha_inicio, fecha_fin + timedelta(days=1)])
    print(f"[POSTGRES] Cargando histórico {fecha_inicio} -> {fecha_fin}...")
    inicio = time.perf_counter()
    with get_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute(query, parametros)
            rows = cursor.fetchall()
    resultado = {}
    for row in rows:
        registro = row_a_consumo_db(row)
        resultado[clave_consumo(registro.consumo)] = registro
    segundos = time.perf_counter() - inicio
    print(f"[POSTGRES] Histórico: {len(resultado)} registros ({segundos:.2f}s)")
    return resultado


class ActualizadorTags:
    def __init__(self):
        self.actualizaciones = {}

    def agregar(self, registros: list[ConsumoDB], tag: str):
        for registro in registros:
            if registro.id is None:
                continue
            self.actualizaciones[registro.id] = tag
            registro.consumo.tag = tag

    def cantidad(self) -> int:
        return len(self.actualizaciones)

    def ejecutar(self):
        if not self.actualizaciones:
            return
        valores = list(self.actualizaciones.items())
        print(f"[POSTGRES] Actualizando {len(valores)} tags...")
        query = '''
            UPDATE public.consumo_agua AS c
            SET tag = v.tag
            FROM (VALUES %s) AS v(id, tag)
            WHERE c.id = v.id
        '''
        inicio = time.perf_counter()
        with get_conn() as conn:
            with conn.cursor() as cursor:
                for posicion in range(0, len(valores), BATCH_SIZE):
                    lote = valores[posicion: posicion + BATCH_SIZE]
                    execute_values(cursor, query, lote, page_size=BATCH_SIZE)
            conn.commit()
        segundos = time.perf_counter() - inicio
        print(f"[POSTGRES] Tags actualizados: {len(valores)} ({segundos:.2f}s)")
        self.actualizaciones.clear()


def actualizar_sin_categorizar(ids: list[int]) -> None:
    if not ids:
        return
    query = '''
        UPDATE public.consumo_agua
        SET tag = 'SIN CATEGORIZAR'
        WHERE id = ANY(%s)
    '''
    with get_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute(query, (ids,))
        conn.commit()
    print(f"[POSTGRES] {len(ids)} registros -> SIN CATEGORIZAR")

def limpiar_tags_periodo(fecha_inicio, fecha_fin) -> None:
    query = '''
        UPDATE public.consumo_agua
        SET tag = ''
        WHERE "Fecha" >= %s
          AND "Fecha" <= %s
    '''

    with get_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute(query, (fecha_inicio, fecha_fin))
            afectados = cursor.rowcount
        conn.commit()

    print(f"[POSTGRES] Tags limpiados para el periodo: {afectados}")

