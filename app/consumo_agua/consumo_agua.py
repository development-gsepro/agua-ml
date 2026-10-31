from datetime import datetime, timedelta
from collections import Counter
import time
import gc

from .models.consumo import ConsumoAgua, es_medidor_virtual
from .repositories.postgres_repository import (
    get_conn,
    ActualizadorTags,
    actualizar_sin_categorizar,
    insertar_consumos,
    obtener_historico_postgres,
    limpiar_tags_ids,
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
    aplicar_modelo_atipicos_positivos,
    entrenar_y_guardar_modelo,
    TAG_ATIPICO_POSITIVO_DIA,
)
from .utils.normalizacion import clave_consumo, clave_fila, clave_medidor

DIAS_HISTORICO = 10


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
        and c.Fecha.date().isoformat() >= '2026-09-01'
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
    print("\n" + "#" * 70)
    print("CLASIFICADOR DE CONSUMO DE AGUA")
    print("#" * 70)
    print(f"Periodo: {fec_ini} -> {fec_fin}")
    print("#" * 70)

    setpoints = cargar_setpoints()

    print("\n[1/7] OBTENIENDO CONSUMOS (PARQUET / MSSQL)")
    consumos = ejecutar_por_dia(fec_ini, fec_fin, consumoTotalHora)
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
    modelo_atipicos = entrenar_y_guardar_modelo(df_entrenamiento)
    del df_entrenamiento
    gc.collect()
    print(
        f"[ENTRENAMIENTO] PKL actualizado: {MODEL_PATH} | "
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

    print("\n[3/7] INSERTANDO EN POSTGRESQL")
    registros_db = insertar_consumos(consumos)
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
        print("\n[5/7] CARGANDO HISTÓRICO")
        fecha_inicio = datetime.strptime(fec_ini, "%Y-%m-%d").date()
        fecha_fin = datetime.strptime(fec_fin, "%Y-%m-%d").date()
        historico_inicio = fecha_inicio - timedelta(days=DIAS_HISTORICO)
        historico = obtener_historico_postgres(
            claves_medidores, historico_inicio, fecha_fin
        )
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

    print("\n[7/7] EVALUANDO ATÍPICOS POSITIVOS")
    tags_positivos, ids_vacios, resultados_atipicos = (
        aplicar_modelo_atipicos_positivos(
            consumos_clasificables, registros_db, modelo_atipicos
        )
    )
    print("[ATÍPICO POSITIVO] Etiquetas y métricas calculadas (sin escribir todavía)")
    con_perfil = sum(r.tiene_perfil for r in resultados_atipicos)
    diarios = sum(r.atipico_diario for r in resultados_atipicos)
    horarios = sum(r.atipico_horario for r in resultados_atipicos)
    ambos = sum(
        r.atipico_diario and r.atipico_horario
        for r in resultados_atipicos
    )
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

