from datetime import datetime, timedelta
from pathlib import Path
import os
from collections import Counter
import time
import gc

from .models.consumo import ConsumoAgua, ConsumoDB, es_medidor_virtual
from .repositories.postgres_repository import (
    get_conn,
    ActualizadorTags,
    actualizar_sin_categorizar,
    insertar_consumos,
    obtener_historico_postgres,
    limpiar_tags_ids,
    _existentes_por_fila_dia,
)
from .repositories.atipicos_repository import (
    actualizar_tags_atipicos_positivos,
    actualizar_metricas_atipicos,
)
from .services.clasificacion import (
    agrupar_por_dia,
    evaluar_consumo_elevado,
    evaluar_fuga_reparada,
    evaluar_fugas,
    evaluar_consumo_madrugada,
    marcar_offline_y_sin_agua,
    preparar_sin_categorizar,
)
from .services.consumo_service import (
    aplicar_setpoints,
    cargar_setpoints,
    consumoTotalHora,
    ejecutar_por_dia,
    verificar_sincronizacion,
)
from .services.historico_parquet import obtener_consumos_con_cache, leer_historico
from .services.atipicos_positivos import (
    MODEL_PATH,
    ModeloAtipicosPositivos,
    aplicar_modelo_atipicos_positivos,
    entrenar_y_guardar_modelo,
    TAG_ATIPICO_POSITIVO_DIA,
)
from .utils.normalizacion import clave_consumo, clave_fila, clave_medidor, fecha_solo

DIAS_HISTORICO = 1


def _filas_existentes_simulacion(consumos: list[ConsumoAgua]) -> dict:
    """Resuelve IDs físicos sin ejecutar un solo INSERT/UPDATE."""
    diferentes = {clave_fila(c): c for c in consumos}
    por_dia = {}
    for firma, consumo in diferentes.items():
        dia = fecha_solo(consumo.Fecha)
        por_dia.setdefault(dia, []).append((firma, consumo))

    resultado = {}
    with get_conn() as conexion:
        with conexion.cursor() as cursor:
            for dia, filas in sorted(por_dia.items()):
                existentes = _existentes_por_fila_dia(cursor, dia)
                for clave, consumo in filas:
                    candidatos = set()
                    for cc in (clave[0], None, 0):
                        anterior = (cc, *clave[1:])
                        if anterior in existentes:
                            candidatos.add(int(existentes[anterior]))
                    if (
                        not candidatos and int(consumo.idVBox) == 7369
                        and clave[14] == 'Demasías'
                        and datetime(2026, 9, 1).date() <= dia <= datetime(2026, 9, 3).date()
                    ):
                        for cc in (clave[0], None, 0):
                            anterior = (cc, *clave[1:14], 'Agua principal', clave[15])
                            if anterior in existentes:
                                candidatos.add(int(existentes[anterior]))
                    if len(candidatos) != 1:
                        raise RuntimeError(
                            f'SIMULACIÓN BLOQUEADA: {dia} idVBox={consumo.idVBox} '
                            f'hora={consumo.hora}: {len(candidatos)} IDs físicos. '
                            'Sin insertar ni reclasificar.'
                        )
                    resultado[clave] = ConsumoDB(id=candidatos.pop(), consumo=consumo)
    if len({r.id for r in resultado.values()}) != len(resultado):
        raise RuntimeError('SIMULACIÓN BLOQUEADA: dos lecturas comparten un ID')
    return resultado


def _preservar_tags_previos(anteriores, futuros):
    """Conserva tag anterior si la clasificación no propone uno nuevo.

    Una etiqueta nueva no vacía tiene prioridad sobre la antigua.
    El dato previo se conserva tal cual, sin presumir que está actualizado.
    Modifica futuros y devuelve los IDs protegidos.
    """
    conservados = {}
    for rid, anterior in anteriores.items():
        if str(anterior or '').strip() and not str(futuros.get(rid) or '').strip():
            futuros[rid] = anterior
            conservados[rid] = anterior
    return conservados


def _previsualizar_etiquetas_sin_escribir(
    consumos_clasificables, registros_db, actualizador,
    ids_sin_categoria, tags_positivos, ids_vacios
):
    """Reproduce la precedencia de los UPDATE sin ejecutar SQL de escritura."""
    from collections import Counter as _Counter

    por_id = {
        registros_db[clave_fila(c)].id: c
        for c in consumos_clasificables
        if clave_fila(c) in registros_db
    }
    futuros = {}
    for rid, c in por_id.items():
        total = float(c.consumoTotal or 0)
        futuros[rid] = (
            'MEDIDOR OFFLINE' if total == -1 else
            'ATÍPICO' if total == 0 else ''
        )
    futuros.update({int(k): v for k, v in actualizador.actualizaciones.items()})
    futuros.update({int(k): 'SIN CATEGORIZAR' for k in ids_sin_categoria})
    futuros.update({int(k): v for k, v in tags_positivos.items()})
    futuros.update({int(k): '' for k in ids_vacios if k not in tags_positivos})

    anteriores = {}
    with get_conn() as conexion:
        with conexion.cursor() as cursor:
            ids = list(futuros)
            for inicio in range(0, len(ids), 5000):
                cursor.execute(
                    'SELECT id, tag FROM public.consumo_agua WHERE id=ANY(%s)',
                    (ids[inicio:inicio + 5000],)
                )
                anteriores.update({int(rid): str(tag or '').strip() for rid, tag in cursor})
    if len(anteriores) != len(futuros):
        raise RuntimeError('SIMULACIÓN BLOQUEADA: faltan IDs físicos')
    conservadas = _preservar_tags_previos(anteriores, futuros)

    transiciones = _Counter()
    perdidas = 0
    diferencias = 0
    for rid, futuro in futuros.items():
        anterior = anteriores[rid]
        nuevo = str(futuro or '').strip()
        if anterior != nuevo:
            diferencias += 1
            transiciones[(anterior or 'VACÍO', nuevo or 'VACÍO')] += 1
            if anterior and not nuevo:
                perdidas += 1

    print('\n' + '=' * 65)
    print('SIMULACIÓN REAL: PostgreSQL NO MODIFICADO')
    print('IDs contemplados:', len(futuros))
    print('Etiquetas que cambiarían:', diferencias)
    print('Etiquetas antiguas conservadas (sin regla nueva):', len(conservadas))
    print('Etiquetas no vacías que quedarían vacías:', perdidas)
    for (antes, despues), n in transiciones.most_common(12):
        print(f'  {antes} -> {despues}: {n}')
    print('=' * 65)
    if perdidas:
        raise RuntimeError(
            f'SIMULACIÓN BLOQUEADA: {perdidas} etiquetas se perderían. '
            'No se escribió en PostgreSQL ni se guardó modelo.'
        )



def separar_medidores_protegidos(consumos: list[ConsumoAgua]):
    """No tocar etiquetas de medidores con lecturas diferentes para una hora.

    Excepción documentada: 7369 / Demasías / 2026-09-01 ya existe
    con otro nombre de área en PostgreSQL; conservar sus tags.
    """
    conteos = Counter(clave_consumo(c) for c in consumos)
    protegidos = {
        clave_medidor(c.idVBox, c.Area)
        for c in consumos
        if conteos[clave_consumo(c)] > 1
    }
    if any(
        int(c.idVBox) == 7369
        and str(c.Area or '').strip() == 'Demasías'
        and '2026-09-01' <= c.Fecha.isoformat()[:10] <= '2026-09-03'
        for c in consumos
    ):
        protegidos.add(clave_medidor(7369, 'Demasías'))

    clasificables = [
        c for c in consumos
        if clave_medidor(c.idVBox, c.Area) not in protegidos
    ]
    return clasificables, protegidos


def clasificar_consumo_inicial(consumo: ConsumoAgua) -> str | None:
    if es_medidor_virtual(consumo.Area):
        return None

    total = float(consumo.consumoTotal or 0)

    if total == -1:
        consumo.tag = "MEDIDOR OFFLINE"
        consumo.categoria = f"{total}"
        return "offline"

    if total == 0:
        consumo.tag = "ATÍPICO"
        consumo.categoria = f"{total}"
        return "atipico"

    if total < 0:
        return "invalido"

    return "valido"



def validar_reglas_finales(consumos: list[ConsumoAgua]) -> dict[str, int]:
    errores = {
        "ATÍPICO": 0,
        "MEDIDOR OFFLINE": 0,
        "MEDIDOR SIN AGUA": 0,
        "FUGA REPARADA": 0,
        "ATÍPICO POSITIVO DÍA": 0,
        "VACÍO": 0,
        "CONSUMO HORA -1": 0,
    }

    for consumo in consumos:
        if es_medidor_virtual(consumo.Area):
            continue

        total = float(consumo.consumoTotal or 0)
        hora = float(consumo.ConsumoHora or 0)
        tag = str(consumo.tag or "").strip()

        if hora == -1:
            errores["CONSUMO HORA -1"] += 1

        if tag == "ATÍPICO" and total != 0:
            errores["ATÍPICO"] += 1
        elif tag == "MEDIDOR OFFLINE" and total != -1:
            errores["MEDIDOR OFFLINE"] += 1
        elif tag == "MEDIDOR SIN AGUA" and total != 0:
            errores["MEDIDOR SIN AGUA"] += 1
        elif tag == "FUGA REPARADA" and total <= 0:
            errores["FUGA REPARADA"] += 1
        elif tag == TAG_ATIPICO_POSITIVO_DIA and total <= 0:
            errores["ATÍPICO POSITIVO DÍA"] += 1
        elif tag == "" and total <= 0:
            errores["VACÍO"] += 1

    return errores

def clasificador(fec_ini: str, fec_fin: str) -> list[ConsumoAgua]:
    inicio_total = time.perf_counter()
    solo_simular = os.environ.get('AGUA_MODO_SIMULACION') == '1'
    print("\n" + "#" * 70)
    print("CLASIFICADOR DE CONSUMO DE AGUA")
    print("#" * 70)
    print(f"Periodo: {fec_ini} -> {fec_fin}")
    print("#" * 70)

    setpoints = cargar_setpoints()

    print("\n[1/7] OBTENIENDO CONSUMOS (PARQUET / MSSQL)")
    consumos = obtener_consumos_con_cache(fec_ini, fec_fin, consumoTotalHora)
    if not consumos:
        print("[ERROR] MSSQL no devolvió datos.")
        return []
    total_recibidos = len(consumos)
    # Una fila totalmente idéntica de MSSQL no necesita clasificación doble.
    # Las lecturas diferentes (incluido ConsumoHora y NombreMedidor) sí pasan.
    consumos = list({clave_fila(c): c for c in consumos}.values())
    print(f"[MSSQL] Filas de origen distintas: {len(consumos)} "
          f"(copias idénticas: {total_recibidos - len(consumos)})")
    consumos_clasificables, grupos_protegidos = separar_medidores_protegidos(consumos)
    print(f'[PROTECCIÓN TAGS] Medidores protegidos: {len(grupos_protegidos)}; '
          f'filas sin reclasificar: {len(consumos) - len(consumos_clasificables)}')
    print("\n[ENTRENAMIENTO] ENTRENANDO IA DE ATÍPICOS POSITIVOS")
    df_entrenamiento = leer_historico()
    if df_entrenamiento.empty:
        print("[ENTRENAMIENTO] Sin histórico disponible; se crea modelo vacío.")
    # Entrenar en memoria; NO sobrescribir el PKL hasta que el COMMIT funcione.
    modelo_atipicos = ModeloAtipicosPositivos().entrenar(df_entrenamiento)
    del df_entrenamiento
    gc.collect()
    print(
        f"[ENTRENAMIENTO] Modelo calculado en memoria; PKL sin cambios | "
        f"perfiles diarios: {len(modelo_atipicos.perfiles_diarios)} | "
        f"perfiles horarios: {len(modelo_atipicos.perfiles_horarios)}"
    )

    print("\n[SETPOINT] Aplicando setpoints...")
    aplicar_setpoints(consumos, setpoints)


    print("\n[2/7] CLASIFICANDO ATÍPICO Y OFFLINE")
    historicos = []
    atipicos = 0
    offline = 0
    for consumo in consumos_clasificables:
        estado = clasificar_consumo_inicial(consumo)

        if estado is None:
            continue
        if estado == "offline":
            offline += 1
        elif estado == "atipico":
            atipicos += 1
        elif estado == "valido":
            historicos.append(consumo)
    print(f"[2/7] OFFLINE: {offline}")
    print(f"[2/7] ATÍPICO: {atipicos}")
    print(f"[2/7] Históricos (para clasificación): {len(historicos)}")

    input("Presione ENTER para continuar | Insertar Postgresql...")

    print("\n[3/7] INSERTANDO EN POSTGRESQL")
    registros_db = (
        _filas_existentes_simulacion(consumos)
        if solo_simular else insertar_consumos(consumos)
    )
    print(f"[3/7] Filas de origen distintas sincronizadas: {len(registros_db)}")
    # Guardar los IDs permitidos; la actualización se hará al final, de forma atómica.
    ids_clasificables = {
        registros_db[clave_fila(c)].id
        for c in consumos_clasificables
        if clave_fila(c) in registros_db
        and registros_db[clave_fila(c)].id is not None
    }

    print("\n[4/7] PREPARANDO MEDIDORES")
    grupos = {}
    for consumo in historicos:
        clave = clave_medidor(consumo.idVBox, consumo.Area)
        grupos.setdefault(clave, []).append(consumo)
    claves_medidores = set(grupos.keys())
    print(f"[4/7] Medidores a clasificar: {len(claves_medidores)}")

    actualizador = ActualizadorTags()
    if claves_medidores:
        input("Presione ENTER para continuar | Cargar historico...")
        print("\n[5/7] CARGANDO HISTÓRICO")
        fecha_inicio = datetime.strptime(fec_ini, "%Y-%m-%d").date()
        fecha_fin = datetime.strptime(fec_fin, "%Y-%m-%d").date()
        historico_inicio = fecha_inicio - timedelta(days=DIAS_HISTORICO)
        historico = obtener_historico_postgres(
            claves_medidores, historico_inicio, fecha_fin
        )
        # IMPORTANTE: los registros antiguos almacenan setpoint_agua=0;
        # calcular su umbral usando los setpoints MSSQL y la fecha de cada día.
        aplicar_setpoints([r.consumo for r in historico.values()], setpoints)
        historico_por_medidor = {}
        for registro in historico.values():
            clave = clave_medidor(registro.consumo.idVBox, registro.consumo.Area)
            historico_por_medidor.setdefault(clave, []).append(registro)

        print("\n[6/7] EJECUTANDO CLASIFICACIÓN")
        grupos_ambiguos = 0
        for numero, (clave, consumos_medidor) in enumerate(grupos.items(), start=1):
            # El histórico usa un representante por hora. Si estamos
            # reprocesando esa hora, usamos las filas del MSSQL actual.
            claves_periodo = {clave_consumo(c) for c in consumos_medidor}
            todos = {}
            for registro in historico_por_medidor.get(clave, []):
                if clave_consumo(registro.consumo) not in claves_periodo:
                    todos[clave_fila(registro.consumo)] = registro
            for consumo in consumos_medidor:
                registro = registros_db.get(clave_fila(consumo))
                if registro:
                    todos[clave_fila(consumo)] = registro

            # El clasificador diario asume una lectura por hora. Si MSSQL
            # contiene dos versiones para la MISMA hora, no hay criterio
            # válido para escoger cuál es el consumo del día. No inventarlo.
            conteos_horas = Counter(clave_consumo(c) for c in consumos_medidor)
            if any(n > 1 for n in conteos_horas.values()):
                grupos_ambiguos += 1
                print(f'[ADVERTENCIA] Medidor {clave}: múltiples lecturas '
                      'distintas por hora. Se almacenan todas; se omiten '
                      'reglas diarias ambiguas para este medidor.')
                continue
            dias = agrupar_por_dia(list(todos.values()))
            evaluar_consumo_elevado(dias, actualizador)
            evaluar_fugas(dias, actualizador)
            evaluar_fuga_reparada(dias, actualizador)
            evaluar_consumo_madrugada(dias, actualizador)
            if numero % 100 == 0:
                print(f"[CLASIFICADOR] {numero}/{len(grupos)} medidores")

        if grupos_ambiguos:
            print(f'[ADVERTENCIA] {grupos_ambiguos} medidores requieren '
                  'criterio de negocio para resolver lecturas por hora diferentes.')

        grupos_todos = {}
        for consumo in consumos_clasificables:
            if es_medidor_virtual(consumo.Area):
                continue
            clave = clave_medidor(consumo.idVBox, consumo.Area)
            grupos_todos.setdefault(clave, []).append(consumo)
        marcar_offline_y_sin_agua(
            grupos_todos, historico_por_medidor, fecha_fin,
            actualizador, registros_db,
        )

        print(f"[CLASIFICADOR] Tags calculados: {actualizador.cantidad()} (sin escribir todavía)")

    print("\n[POSTGRES] MARCANDO SIN CATEGORIZAR")
    ids = preparar_sin_categorizar(consumos_clasificables, registros_db)
    # Esta lista se escribirá en la misma transacción que las demás etiquetas.

    # Estas estructuras ya no se necesitan para atípicos positivos y pueden
    # ocupar varios GB durante una ejecución mensual.
    if claves_medidores:
        del historico
        del historico_por_medidor
        del grupos_todos
    del grupos
    gc.collect()
    print("[MEMORIA] Estructuras del clasificador normal liberadas.")

    input("Presione ENTER para continuar | Evaluar atípicos...")
    print("\n[7/7] EVALUANDO ATÍPICOS POSITIVOS")
    tags_positivos, ids_vacios, resultados_atipicos = (
        aplicar_modelo_atipicos_positivos(
            consumos_clasificables, registros_db, modelo_atipicos
        )
    )
    print("[ATÍPICO POSITIVO] Etiquetas y métricas calculadas (sin escribir todavía)")
    con_perfil = sum(r.tiene_perfil for r in resultados_atipicos)
    # diarios = sum(r.atipico_diario for r in resultados_atipicos)
    # horarios = sum(r.atipico_horario for r in resultados_atipicos)
    # ambos = sum(
    #     r.atipico_diario and r.atipico_horario
    #     for r in resultados_atipicos
    # )
    print(
        f"[ATÍPICO POSITIVO] Candidatos: {len(resultados_atipicos)} | "
        f"con perfil: {con_perfil} | positivos únicos: {len(tags_positivos)} | "
        f"vacíos: {len(ids_vacios)}"
    )
    print(
        f"[ATÍPICO POSITIVO] DÍA: "
        f"{sum(tag == TAG_ATIPICO_POSITIVO_DIA for tag in tags_positivos.values())}"
    )

    if verificar_sincronizacion(consumos, registros_db) is not True:
        raise RuntimeError('Verificación de sincronización fallida; no se escribirán etiquetas')

    # No permitir que reglas calculadas con histórico modifiquen registros
    # fuera del período o de medidores protegidos.
    fuera = set(actualizador.actualizaciones) - ids_clasificables
    if fuera:
        print(f'[PROTECCIÓN] Se excluyen {len(fuera)} tags de IDs históricos/protegidos.')
        actualizador.actualizaciones = {
            id_: tag for id_, tag in actualizador.actualizaciones.items()
            if id_ in ids_clasificables
        }
    if set(ids) - ids_clasificables:
        raise RuntimeError('SIN CATEGORIZAR contiene IDs fuera del período permitido')
    if (set(tags_positivos) | set(ids_vacios)) - ids_clasificables:
        raise RuntimeError('Atípicos incluyen IDs de medidores protegidos u otros días')

    # Validar ANTES del COMMIT, no después.
    consumos_prevalidacion = {
        clave: registro.consumo
        for clave, registro in registros_db.items()
    }
    for clave, registro in registros_db.items():
        tag_positivo = tags_positivos.get(registro.id)
        if tag_positivo:
            consumos_prevalidacion[clave].tag = tag_positivo

    errores_previos = validar_reglas_finales([
        c for c in consumos_prevalidacion.values()
        if clave_medidor(c.idVBox, c.Area) not in grupos_protegidos
    ])
    if any(errores_previos.values()):
        raise RuntimeError(
            f'Validación previa fallida: {errores_previos}. '
            'No se actualizaron etiquetas.'
        )

    if solo_simular:
        _previsualizar_etiquetas_sin_escribir(
            consumos_clasificables, registros_db, actualizador,
            ids, tags_positivos, ids_vacios,
        )
        print('[SIMULACIÓN] Sin INSERT, UPDATE, COMMIT ni guardado de PKL.')
        return consumos

    # El INSERT de lecturas ya pasó su propia transacción. Aquí SOLO se
    # actualizan etiquetas/métricas: o se confirma todo o se revierte todo.
    print('\n[POSTGRES] INICIANDO TRANSACCIÓN ATÓMICA DE ETIQUETAS')
    with get_conn() as conexion_tags:
        with conexion_tags.cursor() as cursor:
            cursor.execute("SET LOCAL lock_timeout = '5s'")
            cursor.execute('SELECT pg_try_advisory_xact_lock(193681, 20261031)')
            if not cursor.fetchone()[0]:
                raise RuntimeError('Ya existe otra reclasificación de etiquetas activa')
        # Conservar las etiquetas existentes para detectar pérdidas.
        with conexion_tags.cursor() as cursor:
            cursor.execute(
                '''
                SELECT id FROM public.consumo_agua
                WHERE id = ANY(%s)
                  AND NULLIF(BTRIM(tag), '') IS NOT NULL
                ''',
                (sorted(ids_clasificables),)
            )
            ids_con_tag_anterior = [fila[0] for fila in cursor]

        # Bloquear y capturar tags previos a cualquier limpieza.
        # La transacción garantiza que nadie los cambia entre lectura y escritura.
        with conexion_tags.cursor() as cursor:
            cursor.execute(
                """SELECT id, tag FROM public.consumo_agua
                   WHERE id = ANY(%s) AND NULLIF(BTRIM(tag), '') IS NOT NULL
                   FOR UPDATE""",
                (sorted(ids_clasificables),),
            )
            tags_anteriores = dict(cursor.fetchall())
        limpiar_tags_ids(sorted(ids_clasificables), conn=conexion_tags)
        actualizador.ejecutar(conn=conexion_tags)
        actualizar_sin_categorizar(ids, conn=conexion_tags)
        actualizar_tags_atipicos_positivos(
            tags_positivos, ids_vacios, conn=conexion_tags
        )
        actualizar_metricas_atipicos(
            resultados_atipicos, registros_db, modelo_atipicos,
            conn=conexion_tags,
        )
        # Política conservadora: si ninguna regla nueva produjo una etiqueta,
        # no convertir una etiqueta histórica no vacía en cadena vacía.
        if tags_anteriores:
            with conexion_tags.cursor() as cursor:
                cursor.execute(
                    "SELECT id, tag FROM public.consumo_agua WHERE id = ANY(%s)",
                    (list(tags_anteriores),),
                )
                tags_posteriores = dict(cursor.fetchall())
                if set(tags_posteriores) != set(tags_anteriores):
                    raise RuntimeError('Cambió el conjunto de IDs durante la transacción')
                conservadas = _preservar_tags_previos(
                    tags_anteriores, tags_posteriores,
                )
                if conservadas:
                    from psycopg2.extras import execute_values
                    pares = list(conservadas.items())
                    for ini in range(0, len(pares), 1000):
                        execute_values(
                            cursor,
                            """UPDATE public.consumo_agua AS c
                               SET tag = v.tag
                               FROM (VALUES %s) AS v(id, tag)
                               WHERE c.id = v.id""",
                            pares[ini:ini + 1000],
                            page_size=1000,
                        )
                cursor.execute(
                    """SELECT COUNT(*) FROM public.consumo_agua
                       WHERE id = ANY(%s) AND NULLIF(BTRIM(tag), '') IS NULL""",
                    (list(tags_anteriores),),
                )
                if cursor.fetchone()[0]:
                    raise RuntimeError('ROLLBACK: etiquetas antiguas quedaron vacías')
            print(
                f'[PROTECCIÓN] Conservadas {len(conservadas)} etiquetas previas '
                'sin nueva clasificación (no se consideran recalculadas).'
            )

        # No permitir que una etiqueta válida desaparezca silenciosamente.
        if ids_con_tag_anterior:
            with conexion_tags.cursor() as cursor:
                cursor.execute(
                    '''
                    SELECT COUNT(*)
                    FROM public.consumo_agua
                    WHERE id = ANY(%s)
                      AND NULLIF(BTRIM(tag), '') IS NULL
                    ''',
                    (ids_con_tag_anterior,)
                )
                etiquetas_perdidas = cursor.fetchone()[0]

            if etiquetas_perdidas:
                raise RuntimeError(
                    f'RECLASIFICACIÓN CANCELADA: '
                    f'{etiquetas_perdidas} etiquetas anteriores '
                    'quedarían vacías. ROLLBACK automático.'
                )

        # __exit__ confirma si no hubo errores; ante error revierte TODO.
    print('[POSTGRES] COMMIT ÚNICO: etiquetas y métricas confirmadas')
    # Publicar modelo solo después de que PostgreSQL confirme las etiquetas.
    from tempfile import mkstemp
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_nombre = mkstemp(prefix='.atipico_provisional_', dir=MODEL_PATH.parent)
    os.close(fd)
    temporal = Path(temp_nombre)
    try:
        modelo_atipicos.guardar(temporal)
        os.replace(temporal, MODEL_PATH)
    finally:
        temporal.unlink(missing_ok=True)
    print(f'[ENTRENAMIENTO] PKL publicado: {MODEL_PATH}')
    del actualizador, ids

    tiempo_total = time.perf_counter() - inicio_total
    consumos_unicos = {
        clave: registro.consumo
        for clave, registro in registros_db.items()
    }

    for clave, registro in registros_db.items():
        tag_positivo = tags_positivos.get(registro.id)
        if tag_positivo and clave in consumos_unicos:
            consumos_unicos[clave].tag = tag_positivo

    conteo_tags = {}
    for consumo in consumos_unicos.values():
        tag = consumo.tag or "VACÍO"
        conteo_tags[tag] = conteo_tags.get(tag, 0) + 1
    # Los medidores protegidos conservaron sus etiquetas históricas.
    # No considerarlos errores de una clasificación que no se ejecutó.
    consumos_validados = [
        c for c in consumos_unicos.values()
        if clave_medidor(c.idVBox, c.Area) not in grupos_protegidos
    ]
    errores_reglas = validar_reglas_finales(consumos_validados)
    print(
        f"[VALIDACIÓN] Medidores protegidos excluidos: "
        f"{len(grupos_protegidos)}"
    )

    print("\n" + "=" * 70)
    print("VALIDACIÓN DE REGLAS")
    print("=" * 70)
    for regla, errores in errores_reglas.items():
        print(f"{regla}: {errores} inválidos")

    print("\n" + "#" * 70)
    print("CLASIFICACIÓN FINALIZADA")
    print("#" * 70)
    print(f"Total MSSQL recibidos: {total_recibidos}")
    print(f"Total MSSQL (filas crudas distintas): {len({clave_fila(c) for c in consumos})}")
    print(f"PostgreSQL sincronizados: {len(registros_db)}")
    print()
    for tag, cantidad in sorted(conteo_tags.items()):
        print(f"{tag}: {cantidad}")
    print()
    print(f"Tiempo total: {tiempo_total:.2f} segundos")
    print("#" * 70)
    return consumos