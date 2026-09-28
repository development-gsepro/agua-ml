from datetime import datetime, timedelta
import time

from .models.consumo import ConsumoAgua
from .repositories.postgres_repository import (
    ActualizadorTags,
    actualizar_sin_categorizar,
    insertar_consumos,
    obtener_historico_postgres,
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

    print("\n[1/6] DESCARGANDO MSSQL")
    consumos = ejecutar_por_dia(fec_ini, fec_fin, consumoTotalHora)
    if not consumos:
        print("[ERROR] MSSQL no devolvió datos.")
        return []
    total_recibidos = len(consumos)

    print("\n[SETPOINT] Aplicando setpoints...")
    aplicar_setpoints(consumos, setpoints)

    print("\n[2/6] CLASIFICANDO ATÍPICO Y OFFLINE")
    historicos = []
    atipicos = 0
    offline = 0
    for consumo in consumos:
        total = float(consumo.consumoTotal or 0)
        medidor = normalizar_medidor(consumo.Area)
        if total in (-1, -2):
            consumo.tag = "OFFLINE"
            consumo.categoria = f"{total}"
            offline += 1
        elif ((total == 0 and medidor in EXCEPCION_MEDIDORES) or total < -2):
            consumo.tag = "ATÍPICO"
            consumo.categoria = f"{total}"
            atipicos += 1
            historicos.append(consumo)
        else:
            historicos.append(consumo)
    print(f"[2/6] OFFLINE: {offline}")
    print(f"[2/6] ATÍPICO: {atipicos}")
    print(f"[2/6] Históricos (para clasificación): {len(historicos)}")

    print("\n[3/6] INSERTANDO EN POSTGRESQL")
    registros_db = insertar_consumos(consumos)
    print(f"[3/6] Registros insertados: {len(registros_db)}")

    print("\n[4/6] PREPARANDO MEDIDORES")
    grupos = {}
    for consumo in historicos:
        clave = clave_medidor(consumo.idVBox, consumo.Area)
        grupos.setdefault(clave, []).append(consumo)
    claves_medidores = set(grupos.keys())
    print(f"[4/6] Medidores a clasificar: {len(claves_medidores)}")

    if claves_medidores:
        print("\n[5/6] CARGANDO HISTÓRICO")
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

        print("\n[6/6] EJECUTANDO CLASIFICACIÓN")
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

    verificar_sincronizacion(consumos, registros_db)

    tiempo_total = time.perf_counter() - inicio_total
    conteo_tags = {}
    for consumo in consumos:
        tag = consumo.tag or "SIN CATEGORIZAR"
        conteo_tags[tag] = conteo_tags.get(tag, 0) + 1
    print("\n" + "#" * 70)
    print("CLASIFICACIÓN FINALIZADA")
    print("#" * 70)
    print(f"Total MSSQL recibidos: {total_recibidos}")
    print(f"Total MSSQL (sin contar duplicados): {len(consumos)}")
    print(f"PostgreSQL sincronizados: {len(registros_db)}")
    print()
    for tag, cantidad in sorted(conteo_tags.items()):
        print(f"{tag}: {cantidad}")
    print()
    print(f"Tiempo total: {tiempo_total:.2f} segundos")
    print("#" * 70)
    return consumos
