from datetime import datetime, timedelta

from app.consumo_agua.models.consumo import ConsumoAgua, ConsumoDB
from app.consumo_agua.services.clasificacion import (
    agrupar_por_dia,
    evaluar_consumo_madrugada,
    evaluar_fuga_reparada,
    evaluar_fugas,
    preparar_sin_categorizar,
)
from app.consumo_agua.services.atipicos_positivos import (
    ModeloAtipicosPositivos,
    es_candidato,
)


class ActualizadorFake:
    def __init__(self):
        self.actualizaciones = {}

    def agregar(self, registros, tag):
        for r in registros:
            if r.id is not None:
                self.actualizaciones[r.id] = tag


def consumo(fecha, hora, total, hora_valor, area="Agua principal", sp=100):
    c = ConsumoAgua(
        idVBox=100,
        Sucursal="Sucursal prueba",
        Cliente="Cliente prueba",
        Area=area,
        NombreMedidor="Medidor",
        Ciudad="Ciudad",
        Formato="",
        Mercado="",
        Campo="",
        tipo="Invasivo",
        M2=100.0,
        Fecha=fecha,
        consumoTotal=total,
        hora=hora,
        ConsumoHora=hora_valor,
    )
    c.setpoint_agua = sp
    return c


def dias_con_totales(totales, horas_positivas=True):
    inicio = datetime(2026, 9, 1)
    registros = []
    rid = 1
    for d, total in enumerate(totales):
        fecha = inicio + timedelta(days=d)
        for hora in range(24):
            valor = 1.0 if horas_positivas else 0.0
            registros.append(ConsumoDB(rid, consumo(fecha, hora, total, valor)))
            rid += 1
    return agrupar_por_dia(registros)


def test_fuga_se_levanta_en_cuarto_dia_y_solo_ese_dia():
    dias = dias_con_totales([50, 150, 150, 150, 150, 150])
    act = ActualizadorFake()
    evaluar_fugas(dias, act)

    # Primer día de la racha = 2 de septiembre; el cuarto = 5 de septiembre.
    ids_dia_5 = set(range(24 * 4 + 1, 24 * 5 + 1))
    assert set(act.actualizaciones) == ids_dia_5
    assert set(act.actualizaciones.values()) == {"FUGA ACTIVA"}


def test_posible_fuga_ejemplo_se_marca_hasta_cuarto_dia():
    inicio = datetime(2026, 9, 23)
    registros = []
    rid = 1
    for d, total in enumerate([10, 15, 15, 15, 15]):
        fecha = inicio + timedelta(days=d)
        # SP 12. En los cuatro días altos dejamos una hora en cero para que
        # no cumpla consumo_24h y corresponda a POSIBLE FUGA.
        for hora in range(24):
            valor = 0.0 if (d > 0 and hora == 3) else 1.0
            registros.append(ConsumoDB(rid, consumo(fecha, hora, total, valor, sp=12)))
            rid += 1
    dias = agrupar_por_dia(registros)
    act = ActualizadorFake()
    evaluar_fugas(dias, act)
    ids_27 = set(range(24 * 4 + 1, 24 * 5 + 1))
    assert set(act.actualizaciones) == ids_27
    assert set(act.actualizaciones.values()) == {"POSIBLE FUGA"}


def test_fuga_reparada_se_marca_cuatro_dias_despues_de_ultima_fuga():
    dias = dias_con_totales([150, 50, 50, 50, 50, 50])
    act = ActualizadorFake()
    evaluar_fuga_reparada(dias, act)
    ids_dia_5 = set(range(24 * 4 + 1, 24 * 5 + 1))
    assert set(act.actualizaciones) == ids_dia_5
    assert set(act.actualizaciones.values()) == {"FUGA REPARADA"}


def test_madrugada_solo_etiqueta_horas_0_a_5():
    fecha = datetime(2026, 9, 1)
    registros = []
    for hora in range(24):
        # 6 unidades de madrugada / total 100 = 6% > 5%.
        valor = 1.0 if hora < 6 else 0.0
        registros.append(ConsumoDB(hora + 1, consumo(fecha, hora, 100, valor, sp=200)))
    dias = agrupar_por_dia(registros)
    act = ActualizadorFake()
    evaluar_consumo_madrugada(dias, act)
    assert set(act.actualizaciones) == set(range(1, 7))
    assert set(act.actualizaciones.values()) == {"CONSUMO DE MADRUGADA"}


def test_madrugada_no_se_marca_si_total_supera_sp():
    fecha = datetime(2026, 9, 1)
    registros = [
        ConsumoDB(h + 1, consumo(fecha, h, 250, 5 if h < 6 else 0, sp=200))
        for h in range(24)
    ]
    act = ActualizadorFake()
    evaluar_consumo_madrugada(agrupar_por_dia(registros), act)
    assert act.actualizaciones == {}


def test_medidor_virtual_no_entra_sin_categorizar_ni_atipicos():
    fecha = datetime(2026, 9, 1)
    c = consumo(fecha, 8, 100, 100, area="Agua principal*")
    registro = ConsumoDB(1, c)
    assert preparar_sin_categorizar([c], {}) == []
    assert c.tag == ""
    assert es_candidato(c) is False


def test_modelo_atipicos_excluye_areas_virtuales():
    import pandas as pd

    filas = []
    inicio = datetime(2026, 9, 1)
    for d in range(8):
        for hora in range(10):
            filas.append({
                "idVBox": 100,
                "Sucursal": "Sucursal",
                "Area": "Agua principal*",
                "Fecha": inicio + timedelta(days=d),
                "consumoTotal": 100,
                "hora": hora,
                "ConsumoHora": 10,
            })
    modelo = ModeloAtipicosPositivos(7).entrenar(pd.DataFrame(filas))
    assert modelo.perfiles_diarios == {}
    assert modelo.perfiles_horarios == {}
