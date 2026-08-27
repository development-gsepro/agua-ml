from .utils.databaseMSSQL import get_connection
from .utils.databasePostgress import get_connection as get_conn
from dataclasses import dataclass
from datetime import datetime, timedelta, date
from typing import Optional
import time
from psycopg2.extras import execute_values


# ============================================================
# MODELOS
# ============================================================

@dataclass(slots=True)
class ConsumoAgua:
    idVBox: int
    Sucursal: str
    Medidor: str
    Formato: str
    Mercado: str
    Campo: str
    tipo: str
    M2: float
    Fecha: datetime
    consumoTotal: float
    hora: int
    ConsumoHora: float
    tag: str = ""
    categoria: str = ""
    setPointSemana: float = 0.0
    setPointFinSemana: float = 0.0


@dataclass(slots=True)
class SetPointAgua:
    idVBox: int
    Medidor: str
    weekdaysValue: float
    weekendValue: float


@dataclass(slots=True)
class ConsumoDB:
    id: Optional[int]
    consumo: ConsumoAgua


@dataclass(slots=True)
class DiaConsumo:
    fecha: date
    registros: list[ConsumoDB]

    @property
    def consumo_total(self) -> float:
        if not self.registros:
            return 0.0

        return float(
            self.registros[0].consumo.consumoTotal or 0
        )

    @property
    def set_point(self) -> float:
        if not self.registros:
            return 0.0

        consumo = self.registros[0].consumo

        if self.fecha.weekday() >= 5:
            return float(
                consumo.setPointFinSemana or 0
            )

        return float(
            consumo.setPointSemana or 0
        )

    @property
    def supera_sp(self) -> bool:
        return self.consumo_total > self.set_point

    @property
    def tiene_consumo_horario(self) -> bool:
        return any(
            float(
                registro.consumo.ConsumoHora or 0
            ) > 0
            for registro in self.registros
        )


# ============================================================
# CONSTANTES
# ============================================================

EXCEPCION_MEDIDORES = (
    "AGUA PRINCIPAL",
    "AGUA PRINCIPAL*",
    "AGUA POTABLE",
    "AGUA FILTRADA",
)

MAX_RETRIES = 3
RETRY_DELAY = 2

HORAS_MADRUGADA = set(range(0, 6))

DIAS_HISTORICO = 30

BATCH_SIZE_POSTGRES = 5000


# ============================================================
# UTILIDADES
# ============================================================

def normalizar_medidor(medidor: str) -> str:
    return str(
        medidor or ""
    ).strip().upper()


def fecha_solo(fecha) -> date:
    if isinstance(fecha, datetime):
        return fecha.date()

    if isinstance(fecha, date):
        return fecha

    return datetime.strptime(
        str(fecha),
        "%Y-%m-%d"
    ).date()


def clave_medidor(
    id_vbox: int,
    medidor: str
):
    return (
        id_vbox,
        normalizar_medidor(medidor)
    )


def clave_consumo(
    consumo: ConsumoAgua
):
    return (
        consumo.idVBox,
        normalizar_medidor(consumo.Medidor),
        fecha_solo(consumo.Fecha),
        consumo.hora,
    )


# ============================================================
# EJECUTAR MSSQL POR DÍA
# ============================================================

def ejecutar_por_dia(
    fec_ini: str,
    fec_fin: str,
    funcion_dia,
    max_retries: int = MAX_RETRIES,
    retry_delay: int = RETRY_DELAY
) -> list:

    fecha_inicio = datetime.strptime(
        fec_ini,
        "%Y-%m-%d"
    ).date()

    fecha_fin = datetime.strptime(
        fec_fin,
        "%Y-%m-%d"
    ).date()

    resultados = []

    fecha_actual = fecha_inicio

    total_dias = (
        fecha_fin - fecha_inicio
    ).days + 1

    dia_numero = 0

    print()
    print("=" * 70)
    print("MSSQL - INICIO DE DESCARGA")
    print(
        f"Rango: {fecha_inicio} -> {fecha_fin}"
    )
    print(
        f"Días a consultar: {total_dias}"
    )
    print("=" * 70)

    while fecha_actual <= fecha_fin:

        dia_numero += 1

        fecha_dia = fecha_actual.strftime(
            "%Y-%m-%d"
        )

        print(
            f"[MSSQL {dia_numero}/{total_dias}] "
            f"Consultando {fecha_dia}..."
        )

        resultado_dia = []

        for intento in range(
            1,
            max_retries + 1
        ):

            try:

                inicio = time.perf_counter()

                resultado_dia = funcion_dia(
                    fecha_dia
                )

                segundos = (
                    time.perf_counter() - inicio
                )

                print(
                    f"[MSSQL {fecha_dia}] "
                    f"OK - {len(resultado_dia)} registros "
                    f"({segundos:.2f}s)"
                )

                break

            except Exception as e:

                print(
                    f"[MSSQL {fecha_dia}] "
                    f"ERROR "
                    f"(intento {intento}/{max_retries}): "
                    f"{e}"
                )

                if intento < max_retries:

                    print(
                        f"[MSSQL {fecha_dia}] "
                        f"Reintentando en "
                        f"{retry_delay}s..."
                    )

                    time.sleep(
                        retry_delay
                    )

                else:

                    print(
                        f"[MSSQL {fecha_dia}] "
                        f"Se agotaron los reintentos."
                    )

        if resultado_dia:
            resultados.extend(
                resultado_dia
            )

        fecha_actual += timedelta(
            days=1
        )

    print()
    print(
        f"MSSQL - FIN DE DESCARGA: "
        f"{len(resultados)} registros"
    )
    print("=" * 70)

    return resultados


# ============================================================
# SET POINTS - MSSQL
# ============================================================

def setPointAgua() -> list:

    query = """
        EXEC SP_ConsultaSetpointAgua
    """

    print(
        "[SETPOINT] Consultando set points en MSSQL..."
    )

    for intento in range(
        1,
        MAX_RETRIES + 1
    ):

        try:

            with get_connection() as conn:

                with conn.cursor() as cursor:

                    cursor.execute(query)

                    while cursor.description is None:

                        if not cursor.nextset():
                            return []

                    rows = cursor.fetchall()

            resultado = [
                SetPointAgua(*row)
                for row in rows
            ]

            print(
                f"[SETPOINT] OK - "
                f"{len(resultado)} set points obtenidos"
            )

            return resultado

        except Exception as e:

            print(
                f"[SETPOINT] ERROR "
                f"(intento {intento}/{MAX_RETRIES}): "
                f"{e}"
            )

            if intento < MAX_RETRIES:

                time.sleep(
                    RETRY_DELAY
                )

    print(
        "[SETPOINT] No fue posible obtener "
        "los set points."
    )

    return []


# ============================================================
# SETPOINTS COMO DICCIONARIO
# ============================================================

print()
print("[INIT] Cargando set points...")

SETPOINTSAGUA = setPointAgua()

SETPOINTS_MAP = {
    clave_medidor(
        sp.idVBox,
        sp.Medidor
    ): sp
    for sp in SETPOINTSAGUA
}

print(
    f"[INIT] Set points indexados: "
    f"{len(SETPOINTS_MAP)}"
)


# ============================================================
# CONSUMO POR HORA - MSSQL
# ============================================================

def consumoTotalHora(
    fecha
) -> list:

    query = """
        EXEC dbo.SP_ReporteConsumoAguaDiario_PowerBI
            @fecIni=?,
            @fecFin=?
    """

    for intento in range(
        1,
        MAX_RETRIES + 1
    ):

        try:

            with get_connection() as conn:

                with conn.cursor() as cursor:

                    cursor.execute(
                        query,
                        (fecha, fecha)
                    )

                    while cursor.description is None:

                        if not cursor.nextset():
                            return []

                    rows = cursor.fetchall()

            consumos = [
                ConsumoAgua(*row)
                for row in rows
            ]

            for consumo in consumos:

                setpoint = SETPOINTS_MAP.get(
                    clave_medidor(
                        consumo.idVBox,
                        consumo.Medidor
                    )
                )

                if setpoint is None:
                    continue

                consumo.setPointSemana = float(
                    setpoint.weekdaysValue or 0
                )

                consumo.setPointFinSemana = float(
                    setpoint.weekendValue or 0
                )

            return consumos

        except Exception as e:

            print(
                f"[MSSQL {fecha}] Error en "
                f"consumoTotalHora "
                f"(intento {intento}/{MAX_RETRIES}): "
                f"{e}"
            )

            if intento < MAX_RETRIES:

                time.sleep(
                    RETRY_DELAY
                )

    return []


# ============================================================
# POSTGRESQL - CONVERTIR ROW
# ============================================================

def row_a_consumo_db(row) -> ConsumoDB:

    consumo = ConsumoAgua(
        idVBox=row[1],
        Sucursal=row[2],
        Medidor=row[3],
        Formato=row[4],
        Mercado=row[5],
        Campo=row[6],
        tipo=row[7],
        M2=row[8],
        Fecha=row[9],
        consumoTotal=row[10],
        hora=row[11],
        ConsumoHora=row[12],
        tag=row[13] or "",
        categoria=row[14] or "",
    )

    return ConsumoDB(
        id=row[0],
        consumo=consumo
    )


# ============================================================
# POSTGRESQL - BUSCAR EXISTENTES
# ============================================================

def obtener_existentes_postgres(
    consumos: list[ConsumoAgua]
) -> dict:

    if not consumos:
        return {}

    fechas = [
        fecha_solo(consumo.Fecha)
        for consumo in consumos
    ]

    fecha_inicio = min(fechas)
    fecha_fin = max(fechas)

    grupos = {
        clave_medidor(
            consumo.idVBox,
            consumo.Medidor
        )
        for consumo in consumos
    }

    print()
    print(
        "[POSTGRES] Buscando registros existentes..."
    )
    print(
        f"[POSTGRES] Medidores involucrados: "
        f"{len(grupos)}"
    )
    print(
        f"[POSTGRES] Rango: "
        f"{fecha_inicio} -> {fecha_fin}"
    )

    inicio = time.perf_counter()

    grupos_values = list(grupos)

    values_sql = ", ".join(
        ["(%s, %s)"] * len(grupos_values)
    )

    query = f"""
        WITH grupos(id_vbox, medidor) AS (
            VALUES {values_sql}
        )
        SELECT
            c.id,
            c."idVBox",
            c."Sucursal",
            c."Medidor",
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
            c.categoria
        FROM public.consumo_agua AS c
        INNER JOIN grupos AS g
            ON c."idVBox" = g.id_vbox
           AND UPPER(TRIM(c."Medidor")) = g.medidor
        WHERE c."Fecha" >= %s
          AND c."Fecha" < %s
    """

    parametros = []

    for id_vbox, medidor in grupos_values:
        parametros.extend(
            [
                id_vbox,
                medidor
            ]
        )

    parametros.extend(
        [
            fecha_inicio,
            fecha_fin + timedelta(days=1)
        ]
    )

    try:

        with get_conn() as conn:

            with conn.cursor() as cursor:

                cursor.execute(
                    query,
                    parametros
                )

                rows = cursor.fetchall()

    except Exception as e:

        print(
            "[POSTGRES] ERROR buscando existentes:"
        )
        print(e)

        raise

    resultado = {}

    for row in rows:

        consumo_db = row_a_consumo_db(
            row
        )

        resultado[
            clave_consumo(
                consumo_db.consumo
            )
        ] = consumo_db

    segundos = (
        time.perf_counter() - inicio
    )

    print(
        f"[POSTGRES] Encontrados: "
        f"{len(resultado)} registros "
        f"({segundos:.2f}s)"
    )

    return resultado


# ============================================================
# INSERTAR OFFLINE / ATÍPICOS
#
# IMPORTANTE:
# Estos registros NO consultan PostgreSQL para validar
# existencia. Se insertan directamente.
# ============================================================

def insertar_directos_postgres(
    consumos: list[ConsumoAgua]
) -> dict:

    if not consumos:
        return {}

    print()
    print(
        f"[POSTGRES] Insertando directamente "
        f"{len(consumos)} OFFLINE / ATÍPICOS..."
    )

    query = """
        INSERT INTO public.consumo_agua (
            "idVBox",
            "Sucursal",
            "Medidor",
            "Formato",
            "Mercado",
            "Campo",
            tipo,
            "M2",
            "Fecha",
            "consumoTotal",
            hora,
            "ConsumoHora",
            tag,
            categoria
        )
        VALUES %s
        RETURNING id
    """

    inicio = time.perf_counter()
    resultado = {}
    total_insertados = 0

    with get_conn() as conn:

        with conn.cursor() as cursor:

            for inicio_lote in range(
                0,
                len(consumos),
                BATCH_SIZE_POSTGRES
            ):

                lote = consumos[
                    inicio_lote:
                    inicio_lote + BATCH_SIZE_POSTGRES
                ]

                parametros = [
                    (
                        consumo.idVBox,
                        consumo.Sucursal,
                        consumo.Medidor,
                        consumo.Formato,
                        consumo.Mercado,
                        consumo.Campo,
                        consumo.tipo,
                        consumo.M2,
                        consumo.Fecha,
                        consumo.consumoTotal,
                        consumo.hora,
                        consumo.ConsumoHora,
                        consumo.tag or "",
                        consumo.categoria or "",
                    )
                    for consumo in lote
                ]

                rows = execute_values(
                    cursor,
                    query,
                    parametros,
                    page_size=BATCH_SIZE_POSTGRES,
                    fetch=True
                )

                for consumo, row in zip(
                    lote,
                    rows
                ):

                    resultado[
                        clave_consumo(consumo)
                    ] = ConsumoDB(
                        id=row[0],
                        consumo=consumo
                    )

                total_insertados += len(
                    rows
                )

        conn.commit()

    segundos = (
        time.perf_counter() - inicio
    )

    print(
        f"[POSTGRES] Directos insertados: "
        f"{total_insertados} registros "
        f"({segundos:.2f}s)"
    )

    return resultado


# ============================================================
# INSERTAR NUEVOS REGISTROS HISTÓRICOS
# ============================================================

def insertar_consumos_postgres(
    consumos: list[ConsumoAgua]
) -> dict:

    if not consumos:
        return {}

    print()
    print(
        "[POSTGRES] Verificando existencia "
        "de registros históricos..."
    )

    existentes = obtener_existentes_postgres(
        consumos
    )

    resultado = dict(
        existentes
    )

    nuevos = [
        consumo
        for consumo in consumos
        if clave_consumo(consumo)
        not in existentes
    ]

    print(
        f"[POSTGRES] Históricos existentes: "
        f"{len(existentes)}"
    )

    print(
        f"[POSTGRES] Históricos nuevos: "
        f"{len(nuevos)}"
    )

    if not nuevos:

        print(
            "[POSTGRES] No hay registros "
            "históricos nuevos."
        )

        return resultado

    query = """
        INSERT INTO public.consumo_agua (
            "idVBox",
            "Sucursal",
            "Medidor",
            "Formato",
            "Mercado",
            "Campo",
            tipo,
            "M2",
            "Fecha",
            "consumoTotal",
            hora,
            "ConsumoHora",
            tag,
            categoria
        )
        VALUES %s
        RETURNING id
    """

    inicio = time.perf_counter()
    total_insertados = 0

    with get_conn() as conn:

        with conn.cursor() as cursor:

            for inicio_lote in range(
                0,
                len(nuevos),
                BATCH_SIZE_POSTGRES
            ):

                lote = nuevos[
                    inicio_lote:
                    inicio_lote + BATCH_SIZE_POSTGRES
                ]

                parametros = [
                    (
                        consumo.idVBox,
                        consumo.Sucursal,
                        consumo.Medidor,
                        consumo.Formato,
                        consumo.Mercado,
                        consumo.Campo,
                        consumo.tipo,
                        consumo.M2,
                        consumo.Fecha,
                        consumo.consumoTotal,
                        consumo.hora,
                        consumo.ConsumoHora,
                        consumo.tag or "",
                        consumo.categoria or "",
                    )
                    for consumo in lote
                ]

                rows = execute_values(
                    cursor,
                    query,
                    parametros,
                    page_size=BATCH_SIZE_POSTGRES,
                    fetch=True
                )

                for consumo, row in zip(
                    lote,
                    rows
                ):

                    registro_db = ConsumoDB(
                        id=row[0],
                        consumo=consumo
                    )

                    resultado[
                        clave_consumo(consumo)
                    ] = registro_db

                total_insertados += len(
                    rows
                )

        conn.commit()

    segundos = (
        time.perf_counter() - inicio
    )

    print(
        f"[POSTGRES] Históricos insertados: "
        f"{total_insertados} registros "
        f"({segundos:.2f}s)"
    )

    return resultado


# ============================================================
# HISTÓRICO POSTGRES
# ============================================================

def obtener_historico_postgres(
    grupos: set,
    fecha_inicio: date,
    fecha_fin: date
) -> dict:

    if not grupos:
        return {}

    print()
    print(
        "[POSTGRES] Cargando histórico..."
    )
    print(
        f"[POSTGRES] Medidores: {len(grupos)}"
    )
    print(
        f"[POSTGRES] Desde: {fecha_inicio}"
    )
    print(
        f"[POSTGRES] Hasta: {fecha_fin}"
    )

    inicio = time.perf_counter()

    grupos_values = list(grupos)

    values_sql = ", ".join(
        ["(%s, %s)"] * len(grupos_values)
    )

    query = f"""
        WITH grupos(id_vbox, medidor) AS (
            VALUES {values_sql}
        )
        SELECT
            c.id,
            c."idVBox",
            c."Sucursal",
            c."Medidor",
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
            c.categoria
        FROM public.consumo_agua AS c
        INNER JOIN grupos AS g
            ON c."idVBox" = g.id_vbox
           AND UPPER(TRIM(c."Medidor")) = g.medidor
        WHERE c."Fecha" >= %s
          AND c."Fecha" < %s
    """

    parametros = []

    for id_vbox, medidor in grupos_values:
        parametros.extend(
            [
                id_vbox,
                medidor
            ]
        )

    parametros.extend(
        [
            fecha_inicio,
            fecha_fin + timedelta(days=1)
        ]
    )

    with get_conn() as conn:

        with conn.cursor() as cursor:

            cursor.execute(
                query,
                parametros
            )

            rows = cursor.fetchall()

    resultado = {}

    for row in rows:

        registro = row_a_consumo_db(
            row
        )

        resultado[
            clave_consumo(
                registro.consumo
            )
        ] = registro

    segundos = (
        time.perf_counter() - inicio
    )

    print(
        f"[POSTGRES] Histórico cargado: "
        f"{len(resultado)} registros "
        f"({segundos:.2f}s)"
    )

    return resultado


# ============================================================
# AGRUPAR POR DÍA
# ============================================================

def agrupar_por_dia(
    registros: list[ConsumoDB]
) -> dict[date, DiaConsumo]:

    grupos = {}

    for registro in registros:

        fecha = fecha_solo(
            registro.consumo.Fecha
        )

        if fecha not in grupos:

            grupos[fecha] = DiaConsumo(
                fecha=fecha,
                registros=[]
            )

        grupos[fecha].registros.append(
            registro
        )

    for dia in grupos.values():

        dia.registros.sort(
            key=lambda x: x.consumo.hora
        )

    return grupos


# ============================================================
# ACTUALIZACIONES PENDIENTES
# ============================================================

class ActualizadorTags:

    def __init__(self):
        self.actualizaciones = {}

    def agregar(
        self,
        registros: list[ConsumoDB],
        tag: str
    ):

        for registro in registros:

            if registro.id is None:
                continue

            self.actualizaciones[
                registro.id
            ] = tag

            registro.consumo.tag = tag

    def cantidad(self) -> int:
        return len(
            self.actualizaciones
        )

    def ejecutar(self):

        if not self.actualizaciones:

            print(
                "[POSTGRES] No hay tags "
                "para actualizar."
            )

            return

        print()
        print(
            "[POSTGRES] Actualizando tags..."
        )

        inicio = time.perf_counter()

        valores = list(
            self.actualizaciones.items()
        )

        placeholders = ", ".join(
            ["(%s, %s)"] * len(valores)
        )

        query = f"""
            UPDATE public.consumo_agua AS c
            SET tag = v.tag
            FROM (
                VALUES {placeholders}
            ) AS v(id, tag)
            WHERE c.id = v.id
        """

        parametros = []

        for id_registro, tag in valores:

            parametros.extend(
                [
                    id_registro,
                    tag
                ]
            )

        with get_conn() as conn:

            with conn.cursor() as cursor:

                cursor.execute(
                    query,
                    parametros
                )

            conn.commit()

        segundos = (
            time.perf_counter() - inicio
        )

        print(
            f"[POSTGRES] Tags actualizados: "
            f"{len(valores)} "
            f"({segundos:.2f}s)"
        )

        self.actualizaciones.clear()


# ============================================================
# CONSUMO ELEVADO
# ============================================================

def evaluar_consumo_elevado(
    dias: dict[date, DiaConsumo],
    actualizador: ActualizadorTags
):

    fechas = sorted(
        dias.keys()
    )

    if not fechas:
        return

    for fecha_actual in fechas:

        inicio = (
            fecha_actual
            - timedelta(days=6)
        )

        ventana = [
            dias[fecha]
            for fecha in fechas
            if inicio <= fecha <= fecha_actual
        ]

        if len(ventana) < 7:
            continue

        dias_supera_sp = [
            dia
            for dia in ventana
            if dia.supera_sp
        ]

        if len(dias_supera_sp) > 3:

            registros = []

            for dia in dias_supera_sp:

                registros.extend(
                    dia.registros
                )

            actualizador.agregar(
                registros,
                "CONSUMO ELEVADO"
            )


# ============================================================
# FUGAS
# ============================================================

def evaluar_fugas(
    dias: dict[date, DiaConsumo],
    actualizador: ActualizadorTags
):

    fechas = sorted(
        dias.keys()
    )

    if len(fechas) < 3:
        return

    for i in range(
        len(fechas) - 2
    ):

        fecha1 = fechas[i]
        fecha2 = fechas[i + 1]
        fecha3 = fechas[i + 2]

        if fecha2 != fecha1 + timedelta(days=1):
            continue

        if fecha3 != fecha2 + timedelta(days=1):
            continue

        dia1 = dias[fecha1]
        dia2 = dias[fecha2]
        dia3 = dias[fecha3]

        if not (
            dia1.supera_sp
            and dia2.supera_sp
            and dia3.supera_sp
        ):
            continue

        dias_fuga = [
            dia1,
            dia2,
            dia3
        ]

        registros = []

        for dia in dias_fuga:

            registros.extend(
                dia.registros
            )

        tiene_consumo = any(
            dia.tiene_consumo_horario
            for dia in dias_fuga
        )

        if tiene_consumo:

            actualizador.agregar(
                registros,
                "FUGA ACTIVA"
            )

        else:

            actualizador.agregar(
                registros,
                "POSIBLE FUGA"
            )


# ============================================================
# FUGA REPARADA
# ============================================================

def evaluar_fuga_reparada(
    dias: dict[date, DiaConsumo],
    actualizador: ActualizadorTags
):

    fechas = sorted(
        dias.keys()
    )

    if len(fechas) < 4:
        return

    for i in range(
        len(fechas) - 3
    ):

        fecha1 = fechas[i]
        fecha2 = fechas[i + 1]
        fecha3 = fechas[i + 2]
        fecha4 = fechas[i + 3]

        if fecha2 != fecha1 + timedelta(days=1):
            continue

        if fecha3 != fecha2 + timedelta(days=1):
            continue

        if fecha4 != fecha3 + timedelta(days=1):
            continue

        dia1 = dias[fecha1]
        dia2 = dias[fecha2]
        dia3 = dias[fecha3]
        dia4 = dias[fecha4]

        anomalia_anterior = all(
            any(
                registro.consumo.tag in (
                    "FUGA ACTIVA",
                    "POSIBLE FUGA"
                )
                for registro in dia.registros
            )
            for dia in (
                dia1,
                dia2,
                dia3
            )
        )

        if not anomalia_anterior:
            continue

        if dia4.supera_sp:
            continue

        actualizador.agregar(
            dia4.registros,
            "FUGA REPARADA"
        )


# ============================================================
# CONSUMO ATÍPICO EN MADRUGADA
# ============================================================

def evaluar_consumo_madrugada(
    dias: dict[date, DiaConsumo],
    actualizador: ActualizadorTags
):

    for dia in dias.values():

        consumo_total = (
            dia.consumo_total
        )

        if consumo_total <= 0:
            continue

        if dia.supera_sp:
            continue

        consumo_madrugada = sum(
            float(
                registro.consumo.ConsumoHora or 0
            )
            for registro in dia.registros
            if registro.consumo.hora
            in HORAS_MADRUGADA
        )

        porcentaje = (
            consumo_madrugada
            / consumo_total
        )

        if porcentaje > 0.05:

            actualizador.agregar(
                dia.registros,
                "CONSUMO ATÍPICO EN MADRUGADA"
            )


# ============================================================
# CLASIFICADOR
# ============================================================

def clasificador(
    fec_ini: str,
    fec_fin: str
) -> list:

    inicio_total = time.perf_counter()

    print()
    print("#" * 70)
    print("CLASIFICADOR DE CONSUMO DE AGUA")
    print("#" * 70)
    print(
        f"Periodo: {fec_ini} -> {fec_fin}"
    )
    print("#" * 70)

    # ========================================================
    # 1. MSSQL
    # ========================================================

    print()
    print("[1/6] DESCARGANDO DATOS DE MSSQL")

    consumos = ejecutar_por_dia(
        fec_ini,
        fec_fin,
        consumoTotalHora
    )

    if not consumos:

        print(
            "[CLASIFICADOR] MSSQL no regresó "
            "datos."
        )

        return []

    print(
        f"[1/6] MSSQL terminado: "
        f"{len(consumos)} registros"
    )

    # ========================================================
    # 2. CLASIFICACIÓN DIRECTA
    # ========================================================

    print()
    print(
        "[2/6] CLASIFICANDO OFFLINE / ATÍPICOS"
    )

    directos = []
    historicos = []

    contador_offline = 0
    contador_atipico = 0

    for consumo in consumos:

        consumo_total = (
            consumo.consumoTotal or 0
        )

        # ----------------------------------------------------
        # OFFLINE
        # ----------------------------------------------------
        
        if consumo_total == -1 or consumo_total == -2:

            consumo.tag = "OFFLINE"

            contador_offline += 1

            # Se guarda directamente, pero NO participa
            # en el análisis histórico.
            directos.append(
                consumo
            )

            continue

        medidor = normalizar_medidor(
            consumo.Medidor
        )

        # ----------------------------------------------------
        # ATÍPICO
        # ----------------------------------------------------

        if (
            (
                consumo_total == 0
                and medidor in EXCEPCION_MEDIDORES
            )
            or 
            (
                consumo_total < -2
            )
        ):

            consumo.tag = "ATÍPICO"

            contador_atipico += 1

            # Se inserta directamente porque la categoría
            # ya está determinada.
            directos.append(
                consumo
            )

            # PERO sí participa en el análisis histórico.
            historicos.append(
                consumo
            )

            continue

        # ----------------------------------------------------
        # REQUIERE ANÁLISIS HISTÓRICO
        # ----------------------------------------------------

        historicos.append(
            consumo
        )

    print(
        f"[2/6] OFFLINE: "
        f"{contador_offline}"
    )

    print(
        f"[2/6] ATÍPICO: "
        f"{contador_atipico}"
    )

    print(
        f"[2/6] Para análisis histórico: "
        f"{len(historicos)}"
    )

    # ========================================================
    # 3. POSTGRES - INSERTAR DIRECTOS / HISTÓRICOS
    # ========================================================

    print()
    print(
        "[3/6] SINCRONIZANDO CON POSTGRESQL"
    )

    registros_db = {}

    # --------------------------------------------------------
    # OFFLINE / ATÍPICOS
    #
    # NO CONSULTAN POSTGRES PARA VALIDAR EXISTENCIA.
    # SE INSERTAN DIRECTAMENTE.
    # --------------------------------------------------------

    if directos:

        registros_directos = (
            insertar_directos_postgres(
                directos
            )
        )

        registros_db.update(
            registros_directos
        )

    # --------------------------------------------------------
    # HISTÓRICOS
    #
    # ESTOS SÍ NECESITAN VALIDAR EXISTENCIA.
    # --------------------------------------------------------

    if historicos:

        registros_historicos = (
            insertar_consumos_postgres(
                historicos
            )
        )

        registros_db.update(
            registros_historicos
        )

    print(
        "[3/6] PostgreSQL sincronizado."
    )

    # ========================================================
    # 4. AGRUPAR MEDIDORES
    # ========================================================

    print()
    print(
        "[4/6] AGRUPANDO MEDIDORES"
    )

    grupos = {}

    for consumo in historicos:

        clave = clave_medidor(
            consumo.idVBox,
            consumo.Medidor
        )

        grupos.setdefault(
            clave,
            []
        ).append(consumo)

    print(
        f"[4/6] Medidores para analizar: "
        f"{len(grupos)}"
    )

    # --------------------------------------------------------
    # Si no hay históricos, ya terminamos.
    # --------------------------------------------------------

    if not grupos:

        print(
            "[CLASIFICADOR] No existen "
            "medidores para análisis histórico."
        )

        tiempo_total = (
            time.perf_counter()
            - inicio_total
        )

        conteo_tags = {}

        for consumo in consumos:

            tag = (
                consumo.tag
                or "SIN CATEGORIZAR"
            )

            conteo_tags[tag] = (
                conteo_tags.get(tag, 0)
                + 1
            )

        print()
        print("#" * 70)
        print("CLASIFICACIÓN FINALIZADA")
        print("#" * 70)
        print(
            f"Total registros: "
            f"{len(consumos)}"
        )

        for tag, cantidad in sorted(
            conteo_tags.items()
        ):

            print(
                f"{tag}: {cantidad}"
            )

        print(
            f"Tiempo total: "
            f"{tiempo_total:.2f} segundos"
        )

        print("#" * 70)

        return consumos

    # ========================================================
    # 5. HISTÓRICO POSTGRES
    # ========================================================

    print()
    print(
        "[5/6] CARGANDO HISTÓRICO DE POSTGRESQL"
    )

    fecha_inicio = datetime.strptime(
        fec_ini,
        "%Y-%m-%d"
    ).date()

    fecha_fin = datetime.strptime(
        fec_fin,
        "%Y-%m-%d"
    ).date()

    fecha_historico_inicio = (
        fecha_inicio
        - timedelta(
            days=DIAS_HISTORICO
        )
    )

    historico = obtener_historico_postgres(
        grupos.keys(),
        fecha_historico_inicio,
        fecha_fin
    )

    print(
        f"[5/6] Histórico listo."
    )

    # ========================================================
    # 6. CLASIFICACIÓN
    # ========================================================

    print()
    print(
        "[6/6] EJECUTANDO REGLAS DE CLASIFICACIÓN"
    )

    actualizador = ActualizadorTags()

    total_grupos = len(
        grupos
    )

    # --------------------------------------------------------
    # INDEXAR HISTÓRICO UNA SOLA VEZ
    #
    # Antes se recorría TODO el histórico para cada medidor.
    # Ahora cada registro se procesa una sola vez.
    # --------------------------------------------------------

    historico_por_grupo = {}

    for registro in historico.values():

        consumo = registro.consumo

        clave = clave_medidor(
            consumo.idVBox,
            consumo.Medidor
        )

        historico_por_grupo.setdefault(
            clave,
            []
        ).append(registro)

    print(
        f"[6/6] Histórico indexado en "
        f"{len(historico_por_grupo)} medidores."
    )

    # --------------------------------------------------------
    # PROCESAR CADA MEDIDOR
    # --------------------------------------------------------

    for numero, (
        clave,
        consumos_medidor
    ) in enumerate(
        grupos.items(),
        start=1
    ):

        id_vbox, medidor = clave

        print(
            f"[CLASIFICADOR "
            f"{numero}/{total_grupos}] "
            f"idVBox={id_vbox} "
            f"Medidor={medidor}"
        )

        # ----------------------------------------------------
        # Combinar histórico + nuevos
        # ----------------------------------------------------

        todos = {}

        # ----------------------------------------------------
        # Histórico
        # ----------------------------------------------------

        for registro in historico_por_grupo.get(
            clave,
            []
        ):

            todos[
                clave_consumo(
                    registro.consumo
                )
            ] = registro

        # ----------------------------------------------------
        # Nuevos
        # ----------------------------------------------------

        for consumo in consumos_medidor:

            clave_hora = clave_consumo(
                consumo
            )

            if clave_hora in registros_db:

                todos[
                    clave_hora
                ] = registros_db[
                    clave_hora
                ]

        # ----------------------------------------------------
        # Agrupar por día
        # ----------------------------------------------------

        dias = agrupar_por_dia(
            list(
                todos.values()
            )
        )

        # ----------------------------------------------------
        # Ejecutar reglas
        # ----------------------------------------------------

        evaluar_consumo_elevado(
            dias,
            actualizador
        )

        evaluar_fugas(
            dias,
            actualizador
        )

        evaluar_fuga_reparada(
            dias,
            actualizador
        )

        evaluar_consumo_madrugada(
            dias,
            actualizador
        )

    # ========================================================
    # ACTUALIZAR TAGS EN POSTGRES
    # ========================================================

    print()
    print(
        "[POSTGRES] Aplicando clasificación..."
    )

    print(
        f"[POSTGRES] Registros afectados: "
        f"{actualizador.cantidad()}"
    )

    actualizador.ejecutar()

    # ========================================================
    # SIN CATEGORIZAR
    # ========================================================

    print()
    print(
        "[CLASIFICADOR] Buscando registros "
        "sin clasificación..."
    )

    ids_sin_categoria = []

    for consumo in historicos:

        if consumo.tag:
            continue

        consumo.tag = (
            "SIN CATEGORIZAR"
        )

        registro = registros_db.get(
            clave_consumo(consumo)
        )

        if (
            registro is not None
            and registro.id is not None
        ):

            ids_sin_categoria.append(
                registro.id
            )

    # --------------------------------------------------------
    # Actualizar SIN CATEGORIZAR masivamente
    # --------------------------------------------------------

    if ids_sin_categoria:

        query = """
            UPDATE public.consumo_agua
            SET tag = 'SIN CATEGORIZAR'
            WHERE id = ANY(%s)
        """

        with get_conn() as conn:

            with conn.cursor() as cursor:

                cursor.execute(
                    query,
                    (
                        ids_sin_categoria,
                    )
                )

            conn.commit()

        print(
            f"[POSTGRES] "
            f"{len(ids_sin_categoria)} registros "
            f"marcados como SIN CATEGORIZAR."
        )

    # ========================================================
    # RESUMEN
    # ========================================================

    tiempo_total = (
        time.perf_counter()
        - inicio_total
    )

    conteo_tags = {}

    for consumo in consumos:

        tag = (
            consumo.tag
            or "SIN CATEGORIZAR"
        )

        conteo_tags[tag] = (
            conteo_tags.get(tag, 0)
            + 1
        )

    print()
    print("#" * 70)
    print("CLASIFICACIÓN FINALIZADA")
    print("#" * 70)

    print(
        f"Total registros: "
        f"{len(consumos)}"
    )

    for tag, cantidad in sorted(
        conteo_tags.items()
    ):

        print(
            f"{tag}: {cantidad}"
        )

    print(
        f"Tiempo total: "
        f"{tiempo_total:.2f} segundos"
    )

    print("#" * 70)

    return consumos