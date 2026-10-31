from datetime import date, timedelta

from ..models.consumo import (
    ConsumoAgua, ConsumoDB, DiaConsumo, es_hora_madrugada, es_medidor_virtual,
)
from ..repositories.postgres_repository import ActualizadorTags
from ..utils.normalizacion import clave_consumo, clave_fila, fecha_solo


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
    """Levanta FUGA ACTIVA/POSIBLE FUGA al cuarto día consecutivo arriba del SP.

    La bandera se aplica únicamente al cuarto día, no retroactivamente a los
    primeros tres días. Una racha mayor a cuatro días tampoco vuelve a generar
    la bandera en los días siguientes.
    """
    fechas = sorted(dias)
    for i in range(3, len(fechas)):
        ventana_fechas = fechas[i - 3:i + 1]
        if not all(
            ventana_fechas[j] == ventana_fechas[0] + timedelta(days=j)
            for j in range(4)
        ):
            continue

        ventana = [dias[f] for f in ventana_fechas]
        if not all(dia.supera_sp for dia in ventana):
            continue

        # Solo levantar la bandera cuando la racha alcanza cuatro días.
        # Si el día anterior a la ventana también superaba el SP, ya se había
        # alcanzado el cuarto día en una fecha previa.
        if i >= 4:
            fecha_previa = fechas[i - 4]
            if (
                fecha_previa == ventana_fechas[0] - timedelta(days=1)
                and dias[fecha_previa].supera_sp
            ):
                continue

        dia_bandera = ventana[-1]
        tag = (
            "FUGA ACTIVA"
            if all(dia.consumo_24h for dia in ventana)
            else "POSIBLE FUGA"
        )
        actualizador.agregar(dia_bandera.registros, tag)

def evaluar_fuga_reparada(
    dias: dict[date, DiaConsumo],
    actualizador: ActualizadorTags
):
    """Marca FUGA REPARADA al cuarto día consecutivo debajo del SP.

    El día inmediatamente anterior a esos cuatro días debe ser un día
    que superó o igualó el SP.

    Un consumo igual al SP NO se considera debajo del SP.
    """

    fechas = sorted(dias)

    for i in range(4, len(fechas)):
        fecha_anterior = fechas[i - 4]
        posteriores = fechas[i - 3:i + 1]

        # Los cuatro días deben ser consecutivos
        if not all(
            posteriores[j] == fecha_anterior + timedelta(days=j + 1)
            for j in range(4)
        ):
            continue

        # El día anterior debe haber cumplido o superado el SP
        if not dias[fecha_anterior].supera_sp:
            continue

        ventana_reparacion = [dias[f] for f in posteriores]

        if any(dia.consumo_total <= 0 for dia in ventana_reparacion):
            continue

        # Los cuatro días deben estar estrictamente debajo del SP.
        # Si alguno cumple o supera el SP, se rompe la secuencia.
        if any(dia.supera_sp for dia in ventana_reparacion):
            continue

        # La etiqueta solamente se aplica al cuarto día
        actualizador.agregar(
            ventana_reparacion[-1].registros,
            "FUGA REPARADA"
        )
        
def evaluar_consumo_madrugada(dias: dict[date, DiaConsumo], actualizador: ActualizadorTags):
    """Detecta consumo de madrugada sin etiquetar registros fuera de la ventana.

    La fuente actual guarda ``hora`` como entero. Por ello, la ventana solicitada
    00:05--06:00 se representa con los buckets horarios 0, 1, 2, 3, 4 y 5.
    """
    for dia in dias.values():
        total = dia.consumo_total
        if total <= 0 or dia.supera_sp:
            continue

        registros_madrugada = [
            r for r in dia.registros
            if es_hora_madrugada(r.consumo.hora)
        ]
        if not registros_madrugada:
            continue

        madrugada = sum(
            float(r.consumo.ConsumoHora or 0)
            for r in registros_madrugada
        )
        if madrugada / total > 0.05:
            actualizador.agregar(registros_madrugada, "CONSUMO DE MADRUGADA")

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
            ck = clave_fila(cons)
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
        elif consumo_total == 0 and all(
            float(c.consumoTotal or 0) == 0
            for c in fechas_dict[ultimo_dia_periodo]
        ):
            actualizador.agregar(registros_ultimo_dia, "MEDIDOR SIN AGUA")


def preparar_sin_categorizar(consumos: list[ConsumoAgua], registros_db: dict) -> list[int]:
    ids = []
    for consumo in consumos:
        if es_medidor_virtual(consumo.Area):
            continue
        if consumo.tag:
            continue
        consumo.tag = "SIN CATEGORIZAR"
        registro = registros_db.get(clave_fila(consumo))
        if registro and registro.id:
            ids.append(registro.id)
    return ids
