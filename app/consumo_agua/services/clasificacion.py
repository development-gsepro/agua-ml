from datetime import date, timedelta

from ..models.consumo import ConsumoAgua, ConsumoDB, DiaConsumo
from ..repositories.postgres_repository import ActualizadorTags
from ..utils.normalizacion import clave_consumo, fecha_solo


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
    n = len(fechas)
    for i in range(n):
        if i + 2 >= n:
            break
        if not (
            fechas[i + 1] == fechas[i] + timedelta(days=1)
            and fechas[i + 2] == fechas[i + 1] + timedelta(days=1)
        ):
            continue
        d1, d2, d3 = dias[fechas[i]], dias[fechas[i + 1]], dias[fechas[i + 2]]
        if not (d1.supera_sp and d2.supera_sp and d3.supera_sp):
            continue
        if i < 3:
            continue
        prev1, prev2, prev3 = dias[fechas[i - 1]], dias[fechas[i - 2]], dias[fechas[i - 3]]
        if not (
            fechas[i - 1] == fechas[i] - timedelta(days=1)
            and fechas[i - 2] == fechas[i] - timedelta(days=2)
            and fechas[i - 3] == fechas[i] - timedelta(days=3)
        ):
            continue
        if prev1.supera_sp or prev2.supera_sp or prev3.supera_sp:
            continue
        tag = "FUGA ACTIVA" if (d1.consumo_24h and d2.consumo_24h and d3.consumo_24h) else "POSIBLE FUGA"
        actualizador.agregar(d1.registros + d2.registros + d3.registros, tag)


def evaluar_fuga_reparada(dias: dict[date, DiaConsumo], actualizador: ActualizadorTags):
    fechas = sorted(dias)
    n = len(fechas)
    for i in range(n - 6):
        if not all(
            fechas[j] == fechas[i] + timedelta(days=j - i)
            for j in range(i, i + 7)
        ):
            continue
        ventana = [dias[fechas[j]] for j in range(i, i + 7)]
        expected = [True] * 3 + [False] * 4
        fallos = 0
        for dia, exp in zip(ventana, expected):
            if dia.supera_sp != exp:
                fallos += 1
                if fallos > 1:
                    break
        if fallos <= 1:
            registros = []
            for dia in ventana[3:]:
                registros.extend(dia.registros)
            actualizador.agregar(registros, "FUGA REPARADA")


def evaluar_consumo_madrugada(dias: dict[date, DiaConsumo], actualizador: ActualizadorTags):
    for dia in dias.values():
        total = dia.consumo_total
        if total <= 0 or dia.supera_sp:
            continue
        madrugada = dia.consumo_madrugada
        if madrugada / total > 0.05:
            actualizador.agregar(dia.registros, "CONSUMO DE MADRUGADA")


def marcar_offline_y_sin_agua(
    periodo_por_medidor: dict,
    historico_por_medidor: dict,
    ultimo_dia_periodo: date,
    actualizador: ActualizadorTags,
    registros_db: dict,
):
    for clave, consumos in periodo_por_medidor.items():
        fechas_dict = {}
        for cons in consumos:
            dia = fecha_solo(cons.Fecha)
            fechas_dict.setdefault(dia, []).append(cons)
        if ultimo_dia_periodo not in fechas_dict:
            continue
        ultimo_consumo = fechas_dict[ultimo_dia_periodo][0]
        consumo_total = ultimo_consumo.consumoTotal
        registros_ultimo_dia = []
        for cons in fechas_dict[ultimo_dia_periodo]:
            ck = clave_consumo(cons)
            if ck in registros_db:
                registros_ultimo_dia.append(registros_db[ck])
        if not registros_ultimo_dia:
            continue
        hay_consumo_previo = False
        for dia, cons_list in fechas_dict.items():
            if dia >= ultimo_dia_periodo:
                continue
            if any(c.consumoTotal > 0 for c in cons_list):
                hay_consumo_previo = True
                break
        if not hay_consumo_previo:
            historico = historico_por_medidor.get(clave, [])
            for registro in historico:
                if registro.consumo.consumoTotal > 0:
                    hay_consumo_previo = True
                    break
        if not hay_consumo_previo:
            continue
        if consumo_total in (-1, -2):
            actualizador.agregar(registros_ultimo_dia, "MEDIDOR OFFLINE")
        elif consumo_total == 0:
            actualizador.agregar(registros_ultimo_dia, "SITIO SIN AGUA")


def preparar_sin_categorizar(consumos: list[ConsumoAgua], registros_db: dict) -> list[int]:
    ids = []
    for consumo in consumos:
        if consumo.tag:
            continue
        consumo.tag = "SIN CATEGORIZAR"
        registro = registros_db.get(clave_consumo(consumo))
        if registro and registro.id:
            ids.append(registro.id)
    return ids
