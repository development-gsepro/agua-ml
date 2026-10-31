from datetime import date, timedelta
from contextlib import nullcontext
from collections import Counter, defaultdict
import math
import time

from psycopg2.extras import execute_values

from ...shared.database.databasePostgress import get_connection as get_conn
from ..models.consumo import ConsumoAgua, ConsumoDB
from ..utils.normalizacion import clave_consumo, clave_fila, fecha_solo, _decimal_pg, _fecha_pg

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
        cc=int(row[1] or 0),
        idVBox=row[2],
        Sucursal=row[3] or "",
        NombreMedidor=row[4] or "",
        Formato=row[5] or "",
        Mercado=row[6] or "",
        Campo=row[7] or "",
        tipo=row[8] or "",
        M2=row[9],
        Fecha=row[10],
        consumoTotal=row[11],
        hora=int(row[12] or 0),
        ConsumoHora=row[13],
        tag=row[14] or "",
        categoria=row[15] or "",
        setpoint_agua=row[16] or 0.0,
        Cliente=row[17] or "",
        Area=row[18] or "",
        Ciudad=row[19] or "",
    )

    return ConsumoDB(id=row[0], consumo=consumo)


def seleccionar_registro_representativo(
    actual: ConsumoDB | None,
    nuevo: ConsumoDB,
) -> ConsumoDB:
    if actual is None:
        return nuevo

    total_actual = float(actual.consumo.consumoTotal or 0)
    total_nuevo = float(nuevo.consumo.consumoTotal or 0)

    if total_nuevo > 0 and total_actual <= 0:
        return nuevo

    if total_actual > 0 and total_nuevo <= 0:
        return actual

    return nuevo

# Debe coincidir con normalizar_medidor(): espacios, mayúsculas y bordes.
_AREA_SQL = r"""regexp_replace(
    regexp_replace(UPPER(TRIM(COALESCE("Area", ''))), '\\s+', ' ', 'g'),
    '^[ .]+|[ .]+$', '', 'g'
)"""


def _conteos_existentes_dia(cursor, dia: date) -> dict:
    """Cuenta las multiplicidades existentes por clave; no confunde duplicados legítimos.

    Consulta agrupada por día: evita descargar millones de filas individuales
    de una base que ya contiene inserciones repetidas.
    """
    cursor.execute(f"""
        SELECT "idVBox", {_AREA_SQL}, "Fecha"::date, hora,
               COUNT(*)::bigint, MAX(id)
        FROM public.consumo_agua
        WHERE "Fecha" >= %s AND "Fecha" < %s
        GROUP BY 1, 2, 3, 4
    """, (dia, dia + timedelta(days=1)))
    return {
        (int(vbox), area, fecha, int(hora)): (int(cantidad), int(id_max))
        for vbox, area, fecha, hora, cantidad, id_max in cursor
    }


# Columnas crudas de MSSQL: intencionalmente NO incluyen tags, setpoints
# ni métricas derivadas. Si cambia una clasificación, se reutiliza el ID.
_COLUMNAS_ORIGEN = (
    'cc', '"idVBox"', '"Sucursal"', '"NombreMedidor"', '"Formato"',
    '"Mercado"', '"Campo"', 'tipo', '"M2"', '"Fecha"',
    '"consumoTotal"', 'hora', '"ConsumoHora"', '"Cliente"',
    '"Area"', '"Ciudad"',
)


def _validar_esquema(cursor) -> None:
    """Evita cambios parciales cuando falta cc en la base local."""
    cursor.execute("""
        SELECT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'consumo_agua'
              AND column_name = 'cc'
        )
    """)
    if not cursor.fetchone()[0]:
        raise RuntimeError(
            "La tabla public.consumo_agua no tiene 'cc'. "
            "No se insertó nada: aplicar migración autorizada primero."
        )


def _existentes_por_fila_dia(cursor, dia: date) -> dict:
    """Un ID por fila original distinta, aun si hay millones de copias antiguas.

    Se agrupa por las 16 columnas de origen y NO por tag o setpoint.
    Por eso una reclasificación no provoca una nueva inserción.
    """
    columnas = ', '.join(_COLUMNAS_ORIGEN)
    cursor.execute(f"""
        SELECT MAX(id), {columnas}
        FROM public.consumo_agua
        WHERE "Fecha" >= %s AND "Fecha" < %s
        GROUP BY {columnas}
    """, (dia, dia + timedelta(days=1)))
    existentes = {}
    for row in cursor:
        rid, cc, vbox, sucursal, nombre, formato, mercado, campo, tipo, m2, fecha, total, hora, consumo_hora, cliente, area, ciudad = row
        firma = (
            (None if cc is None else int(cc)), int(vbox), sucursal or '', nombre or '',
            formato or '', mercado or '', campo or '', tipo or '',
            _decimal_pg(m2), _fecha_pg(fecha), _decimal_pg(total),
            int(hora), _decimal_pg(consumo_hora), cliente or '',
            area or '', ciudad or '',
        )
        existentes[firma] = int(rid)
    return existentes


def insertar_consumos(consumos: list[ConsumoAgua]) -> dict:
    """Sincroniza filas crudas distintas y devuelve una referencia POR FILA.

    Se reciben/procesan TODOS los objetos de entrada. Si MSSQL repite una fila
    idéntica, esa copia comparte ID en PG en vez de generar un duplicado.
    Distintos consumos o NombreMedidor nunca se sustituyen mutuamente.
    """
    if not consumos:
        return {}
    print(f"[POSTGRES] Verificando {len(consumos)} filas de MSSQL...")
    columnas_insert = (
        'cc, "idVBox", "Sucursal", "NombreMedidor", "Formato", "Mercado", '
        '"Campo", tipo, "M2", "Fecha", "consumoTotal", hora, '
        '"ConsumoHora", tag, categoria, setpoint_agua, "Cliente", "Area", "Ciudad"'
    )
    query = f'INSERT INTO public.consumo_agua ({columnas_insert}) VALUES %s RETURNING id'
    por_dia = defaultdict(list)
    for consumo in consumos:
        por_dia[fecha_solo(consumo.Fecha)].append(consumo)

    resultado = {}
    nuevos = reutilizados = colapsados = 0
    inicio = time.perf_counter()
    with get_conn() as conn:
        with conn.cursor() as cursor:
            # Protección solo entre escritores que usan esta misma función.
            cursor.execute("SELECT pg_advisory_xact_lock(193681, 20261030)")
            _validar_esquema(cursor)
            for dia, consumos_dia in sorted(por_dia.items()):
                existentes = _existentes_por_fila_dia(cursor, dia)
                pendientes = []
                vistos = set()
                for c in consumos_dia:
                    clave = clave_fila(c)
                    if clave in vistos:
                        colapsados += 1
                        continue
                    vistos.add(clave)
                    if clave in existentes:
                        resultado[clave] = ConsumoDB(id=existentes[clave], consumo=c)
                        reutilizados += 1
                    else:
                        # Si cc se agregó después, las filas históricas podrían
                        # tener NULL o el valor por defecto 0. Nunca insertar
                        # silenciosamente otra copia de la misma lectura.
                        id_legacy = None
                        for cc_legacy in (None, 0):
                            legacy = (cc_legacy, *clave[1:])
                            if legacy != clave and legacy in existentes:
                                id_legacy = existentes[legacy]
                                break

                        if id_legacy is not None:
                            resultado[clave] = ConsumoDB(
                                id=id_legacy, consumo=c
                            )
                            reutilizados += 1
                        else:
                            # Excepción verificada: cambio de área en MSSQL.
                            # No modificar el registro histórico ni insertar copia.
                            if (
                                int(c.idVBox) == 7369
                                and date(2026, 9, 1) <= fecha_solo(c.Fecha) <= date(2026, 9, 3)
                                and str(c.Area or '').strip() == 'Demasías'
                            ):
                                candidatos = [
                                    existentes[k]
                                    for cc_anterior in (None, 0)
                                    for k in [(
                                        cc_anterior,
                                        *clave[1:14],
                                        'Agua principal',
                                        clave[15],
                                    )]
                                    if k in existentes
                                ]
                                if len(set(candidatos)) != 1:
                                    raise RuntimeError(
                                        'Equipo 7369: no existe una '
                                        'coincidencia histórica única. '
                                        'Sincronización cancelada.'
                                    )
                                resultado[clave] = ConsumoDB(
                                    id=candidatos[0], consumo=c
                                )
                                reutilizados += 1
                            else:
                                pendientes.append(c)

                for posicion in range(0, len(pendientes), BATCH_SIZE):
                    lote = pendientes[posicion:posicion + BATCH_SIZE]
                    valores = [(
                        c.cc, c.idVBox, c.Sucursal, c.NombreMedidor,
                        c.Formato, c.Mercado, c.Campo, c.tipo,
                        valor_numeric_postgres(c.M2, 'M2', NUMERIC_10_2_MAX),
                        c.Fecha,
                        valor_numeric_postgres(c.consumoTotal, 'consumoTotal', NUMERIC_20_2_MAX),
                        c.hora,
                        valor_numeric_postgres(c.ConsumoHora, 'ConsumoHora', NUMERIC_20_2_MAX),
                        c.tag or '', c.categoria or '',
                        valor_numeric_postgres(c.setpoint_agua, 'setpoint_agua', NUMERIC_20_2_MAX),
                        c.Cliente, c.Area, c.Ciudad,
                    ) for c in lote]
                    rows = execute_values(cursor, query, valores,
                                          page_size=BATCH_SIZE, fetch=True)
                    if len(rows) != len(lote):
                        raise RuntimeError(f'INSERT inconsistente: {len(lote)} filas / {len(rows)} IDs')
                    for c, (id_nuevo,) in zip(lote, rows):
                        resultado[clave_fila(c)] = ConsumoDB(id=id_nuevo, consumo=c)
                    nuevos += len(lote)
                print(f'[POSTGRES {dia}] recibidos={len(consumos_dia)} '
                      f'únicos={len(vistos)} nuevos={len(pendientes)}')
        conn.commit()
    print(f'[POSTGRES] Insertados: {nuevos}; reutilizados: {reutilizados}; '
          f'copias idénticas en origen: {colapsados} '
          f'({time.perf_counter() - inicio:.2f}s)')
    return resultado


def obtener_historico_postgres(
    grupos: set,
    fecha_inicio: date,
    fecha_fin: date,
) -> dict:
    if not grupos:
        return {}

    valores = list(grupos)
    placeholders = ", ".join(["(%s,%s)"] * len(valores))

    query = f"""
        WITH grupos(id_vbox, medidor) AS (VALUES {placeholders})
        SELECT DISTINCT ON (
            c."idVBox",
            regexp_replace(
                regexp_replace(
                    UPPER(TRIM(COALESCE(c."Area", ''))),
                    '\\s+',
                    ' ',
                    'g'
                ),
                '^[ .]+|[ .]+$',
                '',
                'g'
            ),
            c."Fecha"::date,
            c.hora
        )
            c.id,
            c.cc,
            c."idVBox",
            c."Sucursal",
            c."NombreMedidor",
            c."Formato",
            c."Mercado",
            c."Campo",
            c.tipo,
            c."M2",
            c."Fecha",
            c."consumoTotal",
            c.hora,
            c."ConsumoHora",
            c.tag,
            c.categoria,
            c.setpoint_agua,
            c."Cliente",
            c."Area",
            c."Ciudad"
        FROM public.consumo_agua c
        INNER JOIN grupos g
            ON c."idVBox" = g.id_vbox
            AND (
                regexp_replace(
                    regexp_replace(
                        UPPER(TRIM(COALESCE(c."Area", ''))),
                        '\\s+',
                        ' ',
                        'g'
                    ),
                    '^[ .]+|[ .]+$',
                    '',
                    'g'
                ) = g.medidor
                OR
                regexp_replace(
                    regexp_replace(
                        UPPER(TRIM(COALESCE(c."NombreMedidor", ''))),
                        '\\s+',
                        ' ',
                        'g'
                    ),
                    '^[ .]+|[ .]+$',
                    '',
                    'g'
                ) = g.medidor
            )
        WHERE c."Fecha" >= %s
          AND c."Fecha" < %s
        ORDER BY
            c."idVBox",
            regexp_replace(
                regexp_replace(
                    UPPER(TRIM(COALESCE(c."Area", ''))),
                    '\\s+',
                    ' ',
                    'g'
                ),
                '^[ .]+|[ .]+$',
                '',
                'g'
            ),
            c."Fecha"::date,
            c.hora,
            c.id DESC
    """

    parametros = []
    for id_vbox, medidor in valores:
        parametros.extend([id_vbox, medidor])

    parametros.extend([
        fecha_inicio,
        fecha_fin + timedelta(days=1),
    ])

    print(
        f"[POSTGRES] Cargando histórico "
        f"{fecha_inicio} -> {fecha_fin}..."
    )

    inicio = time.perf_counter()
    resultado = {}

    with get_conn() as conn:
        with conn.cursor(name="historico_consumo_agua") as cursor:
            cursor.itersize = BATCH_SIZE
            cursor.execute(query, parametros)

            procesados = 0

            for row in cursor:
                registro = row_a_consumo_db(row)
                resultado[clave_consumo(registro.consumo)] = registro
                procesados += 1

                if procesados % 100000 == 0:
                    print(
                        f"[POSTGRES] Histórico procesado: "
                        f"{procesados}"
                    )

    segundos = time.perf_counter() - inicio

    print(
        f"[POSTGRES] Histórico: "
        f"{len(resultado)} registros ({segundos:.2f}s)"
    )

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

    def ejecutar(self, conn=None):
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
        with (nullcontext(conn) if conn is not None else get_conn()) as conexion:
            with conexion.cursor() as cursor:
                for posicion in range(0, len(valores), BATCH_SIZE):
                    lote = valores[posicion: posicion + BATCH_SIZE]
                    execute_values(cursor, query, lote, page_size=BATCH_SIZE)
            if conn is None:
                conexion.commit()
        segundos = time.perf_counter() - inicio
        print(f"[POSTGRES] Tags actualizados: {len(valores)} ({segundos:.2f}s)")
        self.actualizaciones.clear()


def actualizar_sin_categorizar(ids: list[int], conn=None) -> None:
    if not ids:
        return
    query = '''
        UPDATE public.consumo_agua
        SET tag = 'SIN CATEGORIZAR'
        WHERE id = ANY(%s)
    '''
    with (nullcontext(conn) if conn is not None else get_conn()) as conexion:
        with conexion.cursor() as cursor:
            cursor.execute(query, (ids,))
        if conn is None:
            conexion.commit()
    print(f"[POSTGRES] {len(ids)} registros -> SIN CATEGORIZAR")

def limpiar_tags_ids(ids: list[int], conn=None) -> None:
    """Reinicia SOLO filas presentes en la extracción, nunca todo el histórico."""
    if not ids:
        return
    query = '''
        UPDATE public.consumo_agua
        SET tag = CASE
            WHEN "consumoTotal" = -1 THEN 'MEDIDOR OFFLINE'
            WHEN "consumoTotal" = 0 THEN 'ATÍPICO'
            ELSE ''
        END
        WHERE id = ANY(%s)
    '''
    with (nullcontext(conn) if conn is not None else get_conn()) as conexion:
        with conexion.cursor() as cursor:
            cursor.execute(query, (list(set(ids)),))
            cantidad = cursor.rowcount
        if conn is None:
            conexion.commit()
    print(f'[POSTGRES] Tags reiniciados solo para {cantidad} IDs recibidos')


def limpiar_tags_periodo(fecha_inicio, fecha_fin) -> None:
    query = '''
        UPDATE public.consumo_agua
        SET tag = CASE
            WHEN "consumoTotal" = -1 THEN 'MEDIDOR OFFLINE'
            WHEN "consumoTotal" = 0 THEN 'ATÍPICO'
            ELSE ''
        END
        WHERE "Fecha" >= %s
          AND "Fecha" < (%s::date + INTERVAL '1 day')
    '''

    with get_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute(query, (fecha_inicio, fecha_fin))
            afectados = cursor.rowcount
        conn.commit()

    print(f"[POSTGRES] Tags reiniciados para el periodo: {afectados}")

