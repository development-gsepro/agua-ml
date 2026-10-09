from datetime import datetime, timedelta
import time
import gc

from .models.consumo import ConsumoAgua, es_medidor_virtual
from .repositories.postgres_repository import (
    ActualizadorTags,
    actualizar_sin_categorizar,
    insertar_consumos,
    obtener_historico_postgres,
    limpiar_tags_periodo,
)
from .repositories.atipicos_repository import actualizar_tags_atipicos_positivos
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
    TAG_ATIPICO_POSITIVO_HORA,
)
from .utils.normalizacion import clave_consumo, clave_medidor, normalizar_medidor

DIAS_HISTORICO = 10
EXCEPCION_MEDIDORES = {
    normalizar_medidor(x)
    for x in (
        "AGUA PRINCIPAL", "AGUA PRINCIPAL*", "CONSUMO DE AGUA",
        "CONSUMO DE AGUA GRAL", "CONSUMO DE AGUA GRAL.",
        "AGUA POTABLE", "AGUA FILTRADA",
    )
}


def clasificador(fec_ini: str, fec_fin: str) -> list[ConsumoAgua]:
    inicio_total = time.perf_counter()
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

    print("\n[POSTGRES] LIMPIANDO TAGS ANTERIORES DEL PERIODO")
    limpiar_tags_periodo(fec_ini, fec_fin)

    print("\n[2/7] CLASIFICANDO ATÍPICO Y OFFLINE")
    historicos = []
    atipicos = 0
    offline = 0
    for consumo in consumos:
        if es_medidor_virtual(consumo.Area):
            continue
        total = float(consumo.consumoTotal or 0)
        medidor = normalizar_medidor(consumo.Area)
        if total in (-1, -2):
            consumo.tag = "MEDIDOR OFFLINE"
            consumo.categoria = f"{total}"
            offline += 1
        elif ((total == 0 and medidor in EXCEPCION_MEDIDORES) or total < -2):
            consumo.tag = "ATÍPICO"
            consumo.categoria = f"{total}"
            atipicos += 1
            historicos.append(consumo)
        else:
            historicos.append(consumo)
    print(f"[2/7] OFFLINE: {offline}")
    print(f"[2/7] ATÍPICO: {atipicos}")
    print(f"[2/7] Históricos (para clasificación): {len(historicos)}")

    print("\n[3/7] INSERTANDO EN POSTGRESQL")
    registros_db = insertar_consumos(consumos)
    print(f"[3/7] Registros insertados: {len(registros_db)}")

    print("\n[4/7] PREPARANDO MEDIDORES")
    grupos = {}
    for consumo in historicos:
        clave = clave_medidor(consumo.idVBox, consumo.Area)
        grupos.setdefault(clave, []).append(consumo)
    claves_medidores = set(grupos.keys())
    print(f"[4/7] Medidores a clasificar: {len(claves_medidores)}")

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
        actualizador = ActualizadorTags()
        for numero, (clave, consumos_medidor) in enumerate(grupos.items(), start=1):
            todos = {}
            for registro in historico_por_medidor.get(clave, []):
                todos[clave_consumo(registro.consumo)] = registro
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

        grupos_todos = {}
        for consumo in consumos:
            if es_medidor_virtual(consumo.Area):
                continue
            clave = clave_medidor(consumo.idVBox, consumo.Area)
            grupos_todos.setdefault(clave, []).append(consumo)
        marcar_offline_y_sin_agua(
            grupos_todos, historico_por_medidor, fecha_fin,
            actualizador, registros_db,
        )

        print("\n[POSTGRES] APLICANDO TAGS")
        print(f"[POSTGRES] Registros afectados: {actualizador.cantidad()}")
        actualizador.ejecutar()

    print("\n[POSTGRES] MARCANDO SIN CATEGORIZAR")
    ids = preparar_sin_categorizar(consumos, registros_db)
    actualizar_sin_categorizar(ids)
    del ids

    # Estas estructuras ya no se necesitan para atípicos positivos y pueden
    # ocupar varios GB durante una ejecución mensual.
    if claves_medidores:
        del historico
        del historico_por_medidor
        del grupos_todos
        del actualizador
    del grupos
    gc.collect()
    print("[MEMORIA] Estructuras del clasificador normal liberadas.")

    print("\n[7/7] EVALUANDO ATÍPICOS POSITIVOS")
    tags_positivos, ids_vacios, resultados_atipicos = (
        aplicar_modelo_atipicos_positivos(
            consumos, registros_db, modelo_atipicos
        )
    )
    actualizar_tags_atipicos_positivos(tags_positivos, ids_vacios)
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
        f"{sum(tag == TAG_ATIPICO_POSITIVO_DIA for tag in tags_positivos.values())} | "
        f"HORA: "
        f"{sum(tag == TAG_ATIPICO_POSITIVO_HORA for tag in tags_positivos.values())}"
    )

    verificar_sincronizacion(consumos, registros_db)

    tiempo_total = time.perf_counter() - inicio_total
    consumos_unicos = {
        clave_consumo(consumo): consumo
        for consumo in consumos
    }

    for clave, registro in registros_db.items():
        tag_positivo = tags_positivos.get(registro.id)
        if tag_positivo and clave in consumos_unicos:
            consumos_unicos[clave].tag = tag_positivo

    conteo_tags = {}
    for consumo in consumos_unicos.values():
        tag = consumo.tag or "VACÍO"
        conteo_tags[tag] = conteo_tags.get(tag, 0) + 1
    print("\n" + "#" * 70)
    print("CLASIFICACIÓN FINALIZADA")
    print("#" * 70)
    print(f"Total MSSQL recibidos: {total_recibidos}")
    print(f"Total MSSQL (sin contar duplicados): {len({clave_consumo(c) for c in consumos})}")
    print(f"PostgreSQL sincronizados: {len(registros_db)}")
    print()
    for tag, cantidad in sorted(conteo_tags.items()):
        print(f"{tag}: {cantidad}")
    print()
    print(f"Tiempo total: {tiempo_total:.2f} segundos")
    print("#" * 70)
    return consumos

