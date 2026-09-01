from .utils.databaseMSSQL import get_connection
from .utils.databasePostgress import get_connection as get_conn

from dataclasses import dataclass
from datetime import datetime, timedelta, date
from typing import Optional
from psycopg2.extras import execute_values
import time


# ============================================================
# CONSTANTES
# ============================================================

EXCEPCION_MEDIDORES = {
    "AGUA PRINCIPAL",
    "AGUA PRINCIPAL*",
    # =+Vision
    #! Cambiar las etiquetas por agua principal
    "CONSUMO DE AGUA",
    "CONSUMO DE AGUA GRAL",
    "CONSUMO DE AGUA GRAL.", 
    #! Cambiar las etiquetas por agua principal
    "AGUA POTABLE",
    "AGUA FILTRADA",
}

MAX_RETRIES = 3
RETRY_DELAY = 2
HORAS_MADRUGADA = set(range(0, 6))
DIAS_HISTORICO = 30
BATCH_SIZE = 10000


# ============================================================
# CLASES
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
    motivo: str = ""
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
        return float(self.registros[0].consumo.consumoTotal or 0)

    @property
    def set_point(self) -> float:
        if not self.registros:
            return 0.0
        consumo = self.registros[0].consumo
        if self.fecha.weekday() >= 5:
            return float(consumo.setPointFinSemana or 0)
        return float(consumo.setPointSemana or 0)

    @property
    def supera_sp(self) -> bool:
        return self.consumo_total > self.set_point

    @property
    def tiene_consumo_horario(self) -> bool:
        return any(float(registro.consumo.ConsumoHora or 0) > 0
                   for registro in self.registros)


# ============================================================
# UTILIDADES
# ============================================================

def normalizar_medidor(medidor: str) -> str:
    return str(medidor or "").strip().upper()


def fecha_solo(fecha) -> date:
    if isinstance(fecha, datetime):
        return fecha.date()
    if isinstance(fecha, date):
        return fecha
    return datetime.strptime(str(fecha), "%Y-%m-%d").date()


def clave_medidor(id_vbox: int, medidor: str) -> tuple:
    return (id_vbox, normalizar_medidor(medidor))


def clave_consumo(consumo: ConsumoAgua) -> tuple:
    return (consumo.idVBox,
            normalizar_medidor(consumo.Medidor),
            fecha_solo(consumo.Fecha),
            consumo.hora)


# ============================================================
# MSSQL
# ============================================================

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

        for intento in range(1, MAX_RETRIES + 1):
            try:
                inicio = time.perf_counter()
                resultado_dia = funcion_dia(fecha_str)
                segundos = time.perf_counter() - inicio
                print(f"[MSSQL {fecha_str}] OK - {len(resultado_dia)} registros ({segundos:.2f}s)")
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


# ============================================================
# SETPOINTS
# ============================================================

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
                    rows = cursor.fetchall()

            resultado = {clave_medidor(row[0], row[1]): SetPointAgua(*row) for row in rows}
            print(f"[SETPOINT] OK - {len(resultado)} set points indexados")
            return resultado
        except Exception as e:
            print(f"[SETPOINT] ERROR {intento}/{MAX_RETRIES}: {e}")
            if intento < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
    return {}


# ============================================================
# CONSUMO MSSQL
# ============================================================

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


# ============================================================
# CONVERSIÓN POSTGRES
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
    return ConsumoDB(id=row[0], consumo=consumo)


# ============================================================
# INSERTAR TODOS LOS CONSUMOS (SIN DEDUPLICAR)
# ============================================================

def insertar_consumos(consumos: list[ConsumoAgua]) -> dict:
    """Inserta todos los consumos recibidos y devuelve un diccionario {clave: ConsumoDB} con sus IDs."""
    if not consumos:
        return {}

    print(f"[POSTGRES] Insertando {len(consumos)} registros...")

    query = """
        INSERT INTO public.consumo_agua (
            "idVBox", "Sucursal", "Medidor", "Formato", "Mercado",
            "Campo", tipo, "M2", "Fecha", "consumoTotal",
            hora, "ConsumoHora", tag, categoria
        )
        VALUES %s
        RETURNING id
    """

    resultado = {}
    insertados = 0
    inicio = time.perf_counter()

    with get_conn() as conn:
        with conn.cursor() as cursor:
            for posicion in range(0, len(consumos), BATCH_SIZE):
                lote = consumos[posicion:posicion + BATCH_SIZE]
                valores = [
                    (
                        c.idVBox, c.Sucursal, c.Medidor, c.Formato, c.Mercado,
                        c.Campo, c.tipo, c.M2, c.Fecha, c.consumoTotal,
                        c.hora, c.ConsumoHora, c.tag or "", c.categoria or ""
                    )
                    for c in lote
                ]

                rows = execute_values(cursor, query, valores, page_size=BATCH_SIZE, fetch=True)

                if len(rows) != len(lote):
                    raise RuntimeError(f"INSERT inconsistente: {len(lote)} enviados / {len(rows)} IDs")

                for consumo, row in zip(lote, rows):
                    resultado[clave_consumo(consumo)] = ConsumoDB(id=row[0], consumo=consumo)

                insertados += len(lote)
                print(f"[POSTGRES] {insertados}/{len(consumos)} insertados")

            conn.commit()

    segundos = time.perf_counter() - inicio
    print(f"[POSTGRES] Insertados: {insertados} ({segundos:.2f}s)")
    return resultado


# ============================================================
# HISTÓRICO
# ============================================================

def obtener_historico_postgres(grupos: set, fecha_inicio: date, fecha_fin: date) -> dict:
    if not grupos:
        return {}

    valores = list(grupos)
    placeholders = ", ".join(["(%s,%s)"] * len(valores))

    query = f"""
        WITH grupos(id_vbox, medidor) AS (
            VALUES {placeholders}
        )
        SELECT
            c.id, c."idVBox", c."Sucursal", c."Medidor", c."Formato",
            c."Mercado", c."Campo", c.tipo, c."M2", c."Fecha",
            c."consumoTotal", c.hora, c."ConsumoHora", c.tag, c.categoria
        FROM public.consumo_agua c
        INNER JOIN grupos g
            ON c."idVBox" = g.id_vbox
            AND UPPER(TRIM(c."Medidor")) = g.medidor
        WHERE c."Fecha" >= %s AND c."Fecha" < %s
    """

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


# ============================================================
# AGRUPAR POR DÍA
# ============================================================

def agrupar_por_dia(registros: list[ConsumoDB]) -> dict[date, DiaConsumo]:
    grupos = {}
    for registro in registros:
        fecha = fecha_solo(registro.consumo.Fecha)
        if fecha not in grupos:
            grupos[fecha] = DiaConsumo(fecha=fecha, registros=[])
        grupos[fecha].registros.append(registro)

    for dia in grupos.values():
        dia.registros.sort(key=lambda x: x.consumo.hora)
    return grupos


# ============================================================
# ACTUALIZADOR DE TAGS
# ============================================================

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

        query = """
            UPDATE public.consumo_agua AS c
            SET tag = v.tag
            FROM (VALUES %s) AS v(id, tag)
            WHERE c.id = v.id
        """

        inicio = time.perf_counter()
        with get_conn() as conn:
            with conn.cursor() as cursor:
                for posicion in range(0, len(valores), BATCH_SIZE):
                    lote = valores[posicion:posicion + BATCH_SIZE]
                    execute_values(cursor, query, lote, page_size=BATCH_SIZE)
            conn.commit()

        segundos = time.perf_counter() - inicio
        print(f"[POSTGRES] Tags actualizados: {len(valores)} ({segundos:.2f}s)")
        self.actualizaciones.clear()


# ============================================================
# CLASIFICADORES
# ============================================================

def evaluar_consumo_elevado(dias: dict[date, DiaConsumo], actualizador: ActualizadorTags):
    fechas = sorted(dias)
    for fecha_actual in fechas:
        inicio = fecha_actual - timedelta(days=6)
        ventana = [dias[f] for f in fechas if inicio <= f <= fecha_actual]
        if len(ventana) < 7:
            continue
        if sum(dia.supera_sp for dia in ventana) > 3:
            registros = []
            for dia in ventana:
                if dia.supera_sp:
                    registros.extend(dia.registros)
            actualizador.agregar(registros, "CONSUMO ELEVADO")


def evaluar_fugas(dias: dict[date, DiaConsumo], actualizador: ActualizadorTags):
    fechas = sorted(dias)
    for i in range(len(fechas) - 2):
        f1, f2, f3 = fechas[i], fechas[i+1], fechas[i+2]
        if f2 != f1 + timedelta(days=1) or f3 != f2 + timedelta(days=1):
            continue
        d1, d2, d3 = dias[f1], dias[f2], dias[f3]
        if not (d1.supera_sp and d2.supera_sp and d3.supera_sp):
            continue
        registros = d1.registros + d2.registros + d3.registros
        tiene_consumo = any(dia.tiene_consumo_horario for dia in (d1, d2, d3))
        actualizador.agregar(registros, "FUGA ACTIVA" if tiene_consumo else "POSIBLE FUGA")


def evaluar_fuga_reparada(dias: dict[date, DiaConsumo], actualizador: ActualizadorTags):
    fechas = sorted(dias)
    for i in range(len(fechas) - 3):
        f1, f2, f3, f4 = fechas[i], fechas[i+1], fechas[i+2], fechas[i+3]
        if (f2 != f1 + timedelta(days=1) or f3 != f2 + timedelta(days=1) or f4 != f3 + timedelta(days=1)):
            continue
        d1, d2, d3, d4 = dias[f1], dias[f2], dias[f3], dias[f4]
        anteriores = (d1, d2, d3)
        if not all(any(reg.consumo.tag in {"FUGA ACTIVA", "POSIBLE FUGA"} for reg in dia.registros) for dia in anteriores):
            continue
        if d4.supera_sp:
            continue
        actualizador.agregar(d4.registros, "FUGA REPARADA")


def evaluar_consumo_madrugada(dias: dict[date, DiaConsumo], actualizador: ActualizadorTags):
    for dia in dias.values():
        total = dia.consumo_total
        if total <= 0 or dia.supera_sp:
            continue
        madrugada = sum(float(reg.consumo.ConsumoHora or 0) for reg in dia.registros if reg.consumo.hora in HORAS_MADRUGADA)
        if madrugada / total > 0.05:
            actualizador.agregar(dia.registros, "CONSUMO ATÍPICO EN MADRUGADA")


# ============================================================
# MARCAR SIN CATEGORIZAR
# ============================================================

def marcar_sin_categorizar(consumos: list[ConsumoAgua], registros_db: dict):
    ids = []
    for consumo in consumos:
        if consumo.tag:
            continue
        consumo.tag = "SIN CATEGORIZAR"
        registro = registros_db.get(clave_consumo(consumo))
        if registro and registro.id:
            ids.append(registro.id)

    if not ids:
        return

    query = "UPDATE public.consumo_agua SET tag = 'SIN CATEGORIZAR' WHERE id = ANY(%s)"
    with get_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute(query, (ids,))
        conn.commit()
    print(f"[POSTGRES] {len(ids)} registros -> SIN CATEGORIZAR")


# ============================================================
# VERIFICACIÓN FINAL
# ============================================================

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


# ============================================================
# CLASIFICADOR PRINCIPAL
# ============================================================

def clasificador(fec_ini: str, fec_fin: str) -> list[ConsumoAgua]:
    inicio_total = time.perf_counter()

    print("\n" + "#" * 70)
    print("CLASIFICADOR DE CONSUMO DE AGUA")
    print("#" * 70)
    print(f"Periodo: {fec_ini} -> {fec_fin}")
    print("#" * 70)

    # --------------------------------------------------------
    # SETPOINTS (una sola vez)
    # --------------------------------------------------------
    setpoints = cargar_setpoints()

    # --------------------------------------------------------
    # 1. MSSQL
    # --------------------------------------------------------
    print("\n[1/6] DESCARGANDO MSSQL")
    consumos = ejecutar_por_dia(fec_ini, fec_fin, consumoTotalHora)
    if not consumos:
        print("[ERROR] MSSQL no devolvió datos.")
        return []

    total_recibidos = len(consumos)

    # Aplicar setpoints
    for consumo in consumos:
        sp = setpoints.get(clave_medidor(consumo.idVBox, consumo.Medidor))
        if sp:
            consumo.setPointSemana = float(sp.weekdaysValue or 0)
            consumo.setPointFinSemana = float(sp.weekendValue or 0)

    # --------------------------------------------------------
    # 2. CLASIFICACIÓN DIRECTA (OFFLINE / ATÍPICO)
    # --------------------------------------------------------
    print("\n[2/6] CLASIFICANDO OFFLINE / ATÍPICOS")
    directos = []
    historicos = []
    offline = 0
    atipicos = 0

    for consumo in consumos:
        total = float(consumo.consumoTotal or 0)
        medidor = normalizar_medidor(consumo.Medidor)

        if total in (-1, -2):
            consumo.tag = "OFFLINE"
            consumo.categoria = f"{total}"
            offline += 1
            directos.append(consumo)
        elif (total == 0 and medidor in EXCEPCION_MEDIDORES) or total < -2:
            consumo.tag = "ATÍPICO"
            consumo.categoria = f"{total}"
            atipicos += 1
            directos.append(consumo)
            historicos.append(consumo)
        else:
            historicos.append(consumo)

    print(f"[2/6] OFFLINE: {offline}")
    print(f"[2/6] ATÍPICO: {atipicos}")
    print(f"[2/6] Históricos: {len(historicos)}")

    # --------------------------------------------------------
    # 3. INSERTAR TODOS LOS CONSUMOS EN POSTGRES
    # --------------------------------------------------------
    print("\n[3/6] INSERTANDO EN POSTGRESQL")
    registros_db = insertar_consumos(consumos)   # Inserta todos, sin deduplicar
    print(f"[3/6] Registros insertados: {len(registros_db)}")

    # --------------------------------------------------------
    # 4. MEDIDORES
    # --------------------------------------------------------
    print("\n[4/6] PREPARANDO MEDIDORES")
    grupos = {}
    for consumo in historicos:
        clave = clave_medidor(consumo.idVBox, consumo.Medidor)
        grupos.setdefault(clave, []).append(consumo)
    print(f"[4/6] Medidores: {len(grupos)}")

    if grupos:
        # --------------------------------------------------------
        # 5. HISTÓRICO
        # --------------------------------------------------------
        print("\n[5/6] CARGANDO HISTÓRICO")
        fecha_inicio = datetime.strptime(fec_ini, "%Y-%m-%d").date()
        fecha_fin = datetime.strptime(fec_fin, "%Y-%m-%d").date()
        historico_inicio = fecha_inicio - timedelta(days=DIAS_HISTORICO)

        historico = obtener_historico_postgres(
            grupos.keys(),
            historico_inicio,
            fecha_fin
        )

        # Indexar histórico por grupo
        historico_por_grupo = {}
        for registro in historico.values():
            clave = clave_medidor(registro.consumo.idVBox, registro.consumo.Medidor)
            historico_por_grupo.setdefault(clave, []).append(registro)

        # --------------------------------------------------------
        # 6. CLASIFICACIÓN
        # --------------------------------------------------------
        print("\n[6/6] EJECUTANDO CLASIFICACIÓN")
        actualizador = ActualizadorTags()

        for numero, (clave, consumos_medidor) in enumerate(grupos.items(), start=1):
            todos = {}

            # Histórico
            for registro in historico_por_grupo.get(clave, []):
                todos[clave_consumo(registro.consumo)] = registro

            # Nuevos
            for consumo in consumos_medidor:
                clave_hora = clave_consumo(consumo)
                registro = registros_db.get(clave_hora)
                if registro:
                    todos[clave_hora] = registro

            dias = agrupar_por_dia(list(todos.values()))
            evaluar_consumo_elevado(dias, actualizador)
            evaluar_fugas(dias, actualizador)
            evaluar_fuga_reparada(dias, actualizador)
            evaluar_consumo_madrugada(dias, actualizador)

            if numero % 100 == 0:
                print(f"[CLASIFICADOR] {numero}/{len(grupos)} medidores")

        # Aplicar tags
        print("\n[POSTGRES] APLICANDO TAGS")
        print(f"[POSTGRES] Registros afectados: {actualizador.cantidad()}")
        actualizador.ejecutar()

    # --------------------------------------------------------
    # SIN CATEGORIZAR (para todos los consumos, incluyendo directos)
    # --------------------------------------------------------
    print("\n[POSTGRES] MARCANDO SIN CATEGORIZAR")
    marcar_sin_categorizar(consumos, registros_db)

    # --------------------------------------------------------
    # VERIFICACIÓN FINAL
    # --------------------------------------------------------
    verificar_sincronizacion(consumos, registros_db)

    # --------------------------------------------------------
    # RESUMEN
    # --------------------------------------------------------
    tiempo_total = time.perf_counter() - inicio_total

    conteo_tags = {}
    for consumo in consumos:
        tag = consumo.tag or "SIN CATEGORIZAR"
        conteo_tags[tag] = conteo_tags.get(tag, 0) + 1

    print("\n" + "#" * 70)
    print("CLASIFICACIÓN FINALIZADA")
    print("#" * 70)
    print(f"Total MSSQL recibidos: {total_recibidos}")
    print(f"Total MSSQL (sin contar duplicados): {len(consumos)}")   # Nota: ahora no hay deduplicación, pero puede haber duplicados
    print(f"PostgreSQL sincronizados: {len(registros_db)}")
    print()
    for tag, cantidad in sorted(conteo_tags.items()):
        print(f"{tag}: {cantidad}")
    print()
    print(f"Tiempo total: {tiempo_total:.2f} segundos")
    print("#" * 70)

    return consumos