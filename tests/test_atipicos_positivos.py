from datetime import datetime, timedelta

import pandas as pd

from app.consumo_agua.models.consumo import ConsumoAgua, ConsumoDB
from app.consumo_agua.services import atipicos_positivos as servicio_atipicos
from app.consumo_agua.services.atipicos_positivos import (
    ModeloAtipicosPositivos,
    aplicar_modelo_atipicos_positivos,
)
from app.consumo_agua.utils.normalizacion import clave_consumo


def _consumo(total, hora_valor, fecha, hora=8, tag="SIN CATEGORIZAR"):
    return ConsumoAgua(
        idVBox=100,
        Sucursal="Sucursal prueba",
        Cliente="CLIENTE PRUEBA",
        Area="Agua principal",
        NombreMedidor="Medidor",
        Ciudad="CIUDAD PRUEBA",
        Formato="",
        Mercado="",
        Campo="",
        tipo="Invasivo",
        M2=100.0,
        Fecha=fecha,
        consumoTotal=total,
        hora=hora,
        ConsumoHora=hora_valor,
        tag=tag,
        categoria="",
    )


def _dataset(dia_objetivo, total_objetivo=100.0, hora_objetivo=10.0):
    filas = []
    base = datetime(2026, 9, 1)
    for d in range(8):
        fecha = base + timedelta(days=d)
        total = total_objetivo if fecha.date() == dia_objetivo.date() else 100.0
        for hora in range(10):
            valor = 10.0
            if fecha.date() == dia_objetivo.date() and hora == 8:
                valor = hora_objetivo
            filas.append({
                "idVBox": 100,
                "Sucursal": "Sucursal prueba",
                "Cliente": "CLIENTE PRUEBA",
                "Ciudad": "CIUDAD PRUEBA",
                "Area": "Agua principal",
                "Fecha": fecha,
                "consumoTotal": total,
                "hora": hora,
                "ConsumoHora": valor,
                "tag": "",
                "categoria": "",
            })
    return pd.DataFrame(filas)


def test_caso_a_dia_normal_hora_atipica():
    fecha = datetime(2026, 9, 8)
    modelo = ModeloAtipicosPositivos(7).entrenar(
        _dataset(fecha, total_objetivo=100, hora_objetivo=100)
    )
    r = modelo.evaluar(_consumo(100, 100, fecha, hora=8))
    assert r.atipico_diario is False
    assert r.atipico_horario is True
    assert r.es_atipico is True


def test_caso_b_dia_atipico_hora_normal():
    fecha = datetime(2026, 9, 8)
    modelo = ModeloAtipicosPositivos(7).entrenar(
        _dataset(fecha, total_objetivo=500, hora_objetivo=10)
    )
    r = modelo.evaluar(_consumo(500, 10, fecha, hora=8))
    assert r.atipico_diario is True
    assert r.atipico_horario is False
    assert r.es_atipico is True


def test_caso_c_dia_y_hora_atipicos():
    fecha = datetime(2026, 9, 8)
    modelo = ModeloAtipicosPositivos(7).entrenar(
        _dataset(fecha, total_objetivo=500, hora_objetivo=100)
    )
    r = modelo.evaluar(_consumo(500, 100, fecha, hora=8))
    assert r.atipico_diario is True
    assert r.atipico_horario is True


def test_caso_d_ninguno_atipico():
    fecha = datetime(2026, 9, 8)
    modelo = ModeloAtipicosPositivos(7).entrenar(
        _dataset(fecha, total_objetivo=100, hora_objetivo=10)
    )
    r = modelo.evaluar(_consumo(100, 10, fecha, hora=8))
    assert r.atipico_diario is False
    assert r.atipico_horario is False
    assert r.es_atipico is False


def test_tipo_dia_no_forma_parte_del_modelo():
    fecha = datetime(2026, 9, 8)
    df = _dataset(fecha, total_objetivo=100, hora_objetivo=10)
    df["tipo"] = ["ENTRE_SEMANA" if i % 2 else "FIN_SEMANA" for i in range(len(df))]
    modelo = ModeloAtipicosPositivos(7).entrenar(df)
    assert len(modelo.perfiles_diarios) == 1
    assert len(modelo.perfiles_horarios) == 10


def test_aplicacion_respeta_candidatos(tmp_path, monkeypatch):
    monkeypatch.setattr(servicio_atipicos, "REPORT_DIR", tmp_path / "reporte")
    fecha = datetime(2026, 9, 8)
    modelo = ModeloAtipicosPositivos(7).entrenar(
        _dataset(fecha, total_objetivo=100, hora_objetivo=100)
    )

    positivo = _consumo(100, 100, fecha, hora=8)
    normal = _consumo(100, 10, fecha, hora=7)
    protegido = _consumo(100, 100, fecha, hora=9, tag="FUGA ACTIVA")
    
    registros = {}
    for i, consumo in enumerate([positivo, normal, protegido], start=1):
        registros[clave_consumo(consumo)] = ConsumoDB(i, consumo)

    ids_pos, ids_vacios, resultados = aplicar_modelo_atipicos_positivos(
        [positivo, normal, protegido], registros, modelo
    )

    assert ids_pos == {1: "ATÍPICO POSITIVO HORA"}
    assert ids_vacios == [2]
    assert positivo.tag == "ATÍPICO POSITIVO HORA"
    assert normal.tag == ""
    assert protegido.tag == "FUGA ACTIVA"
    assert len(resultados) == 3


def test_modelo_pkl_roundtrip(tmp_path):
    fecha = datetime(2026, 9, 8)
    modelo = ModeloAtipicosPositivos(7).entrenar(_dataset(fecha))
    ruta = tmp_path / "modelo.pkl"
    modelo.guardar(ruta)
    cargado = ModeloAtipicosPositivos.cargar(ruta)
    assert cargado.version == modelo.version
    assert cargado.min_muestras == modelo.min_muestras
    assert cargado.perfiles_diarios.keys() == modelo.perfiles_diarios.keys()
    assert cargado.perfiles_horarios.keys() == modelo.perfiles_horarios.keys()


def _consumo_area(area, total, hora_valor, fecha, hora=8):
    return ConsumoAgua(
        idVBox=100,
        Sucursal="Sucursal prueba",
        Cliente="CLIENTE PRUEBA",
        Area=area,
        NombreMedidor="Medidor",
        Ciudad="CIUDAD PRUEBA",
        Formato="",
        Mercado="",
        Campo="",
        tipo="Invasivo",
        M2=100.0,
        Fecha=fecha,
        consumoTotal=total,
        hora=hora,
        ConsumoHora=hora_valor,
        tag="SIN CATEGORIZAR",
        categoria="",
    )


def _dataset_sucursal_diaria(fecha_objetivo):
    """
    Cada Area tiene bastante variación histórica, por lo que 140 no es
    atípico individualmente.

    Sin embargo, históricamente ambas áreas suman 200 por día.
    El día objetivo suman 280, por lo que la sucursal completa sí debe marcar.
    """
    filas = []
    base = datetime(2026, 9, 1)

    valores_a = [50, 70, 90, 110, 130, 150, 170]
    valores_b = [150, 130, 110, 90, 70, 50, 30]

    for d in range(8):
        fecha = base + timedelta(days=d)

        if fecha.date() == fecha_objetivo.date():
            total_a = 140.0
            total_b = 140.0
        else:
            total_a = float(valores_a[d])
            total_b = float(valores_b[d])

        for hora in range(10):
            for area, total in (
                ("Agua principal", total_a),
                ("Agua filtrada", total_b),
            ):
                filas.append({
                    "idVBox": 100,
                    "Sucursal": "Sucursal prueba",
                    "Cliente": "CLIENTE PRUEBA",
                    "Ciudad": "CIUDAD PRUEBA",
                    "Area": area,
                    "Fecha": fecha,
                    "consumoTotal": total,
                    "hora": hora,
                    "ConsumoHora": 10.0,
                    "tag": "",
                    "categoria": "",
                })

    return pd.DataFrame(filas)


def _dataset_sucursal_horaria(fecha_objetivo):
    """
    El consumo diario agregado de la sucursal se mantiene normal.

    Para el día objetivo, cada Area tiene un comportamiento horario con
    suficiente variación para que 80 no resulte atípico individualmente,
    pero en la hora 8 ambas áreas consumen 80 y la sucursal suma 160.

    El resto de las horas suma aproximadamente 110.
    """
    filas = []
    base = datetime(2026, 9, 1)

    valores_diarios_a = [50, 70, 90, 110, 130, 150, 170, 100]
    valores_diarios_b = [150, 130, 110, 90, 70, 50, 30, 100]

    for d in range(8):
        fecha = base + timedelta(days=d)

        for hora in range(10):
            if fecha.date() == fecha_objetivo.date():
                if hora == 8:
                    valor_a = 80.0
                    valor_b = 80.0
                else:
                    valor_a = float((hora + 1) * 10)
                    valor_b = float(110 - valor_a)
            else:
                if hora == 8:
                    valores_hora_8_a = [30, 40, 50, 60, 70, 80, 90]
                    valores_hora_8_b = [80, 70, 60, 50, 40, 30, 20]
                    valor_a = float(valores_hora_8_a[d])
                    valor_b = float(valores_hora_8_b[d])
                else:
                    valor_a = 50.0
                    valor_b = 60.0

            for area, total, valor_hora in (
                (
                    "Agua principal",
                    float(valores_diarios_a[d]),
                    valor_a,
                ),
                (
                    "Agua filtrada",
                    float(valores_diarios_b[d]),
                    valor_b,
                ),
            ):
                filas.append({
                    "idVBox": 100,
                    "Sucursal": "Sucursal prueba",
                    "Cliente": "CLIENTE PRUEBA",
                    "Ciudad": "CIUDAD PRUEBA",
                    "Area": area,
                    "Fecha": fecha,
                    "consumoTotal": total,
                    "hora": hora,
                    "ConsumoHora": valor_hora,
                    "tag": "",
                    "categoria": "",
                })

    return pd.DataFrame(filas)


def test_sucursal_diaria_detecta_atipico_aunque_grupos_no_marcan(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        servicio_atipicos,
        "REPORT_DIR",
        tmp_path / "reporte",
    )

    fecha = datetime(2026, 9, 8)

    modelo = ModeloAtipicosPositivos(7).entrenar(
        _dataset_sucursal_diaria(fecha)
    )

    principal = _consumo_area(
        "Agua principal", 140, 10, fecha, hora=8
    )
    filtrada = _consumo_area(
        "Agua filtrada", 140, 10, fecha, hora=8
    )

    registros = {
        clave_consumo(principal): ConsumoDB(1, principal),
        clave_consumo(filtrada): ConsumoDB(2, filtrada),
    }

    ids_pos, ids_vacios, resultados = aplicar_modelo_atipicos_positivos(
        [principal, filtrada],
        registros,
        modelo,
    )

    assert not ids_pos
    assert set(ids_vacios) == {1, 2}

    assert all(r.atipico_grupo_diario is False for r in resultados)
    assert all(r.atipico_sucursal_diario is True for r in resultados)
    assert all(r.atipico_diario is False for r in resultados)

    assert principal.tag == ""
    assert filtrada.tag == ""


def test_sucursal_horaria_no_etiqueta_si_grupos_no_marcan(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        servicio_atipicos,
        "REPORT_DIR",
        tmp_path / "reporte",
    )

    fecha = datetime(2026, 9, 8)

    modelo = ModeloAtipicosPositivos(7).entrenar(
        _dataset_sucursal_horaria(fecha)
    )

    principal = _consumo_area(
        "Agua principal", 100, 80, fecha, hora=8
    )
    filtrada = _consumo_area(
        "Agua filtrada", 100, 80, fecha, hora=8
    )

    registros = {
        clave_consumo(principal): ConsumoDB(1, principal),
        clave_consumo(filtrada): ConsumoDB(2, filtrada),
    }

    ids_pos, ids_vacios, resultados = aplicar_modelo_atipicos_positivos(
        [principal, filtrada],
        registros,
        modelo,
    )

    assert not ids_pos
    assert set(ids_vacios) == {1, 2}

    assert all(r.atipico_grupo_diario is False for r in resultados)
    assert all(r.atipico_sucursal_diario is False for r in resultados)

    assert all(r.atipico_grupo_horario is False for r in resultados)
    assert all(r.atipico_sucursal_horario is True for r in resultados)
    assert all(r.atipico_horario is False for r in resultados)

    assert principal.tag == ""
    assert filtrada.tag == ""


def test_dia_atipico_bajo():
    fecha = datetime(2026, 9, 8)

    modelo = ModeloAtipicosPositivos(7).entrenar(
        _dataset(
            fecha,
            total_objetivo=20,
            hora_objetivo=10,
        )
    )

    r = modelo.evaluar(
        _consumo(20, 10, fecha, hora=8)
    )

    assert r.atipico_diario is True
    assert r.direccion_diaria == "BAJO"
    assert r.limite_inferior_diario == 100.0
    assert r.limite_diario == 100.0


def test_dia_atipico_alto_indica_direccion():
    fecha = datetime(2026, 9, 8)

    modelo = ModeloAtipicosPositivos(7).entrenar(
        _dataset(
            fecha,
            total_objetivo=500,
            hora_objetivo=10,
        )
    )

    r = modelo.evaluar(
        _consumo(500, 10, fecha, hora=8)
    )

    assert r.atipico_diario is True
    assert r.direccion_diaria == "ALTO"
    assert r.limite_inferior_diario == 100.0
    assert r.limite_diario == 100.0


def test_consumo_cero_no_se_clasifica_como_atipico_bajo():
    fecha = datetime(2026, 9, 8)

    modelo = ModeloAtipicosPositivos(7).entrenar(
        _dataset(
            fecha,
            total_objetivo=100,
            hora_objetivo=10,
        )
    )

    r = modelo.evaluar(
        _consumo(0, 10, fecha, hora=8)
    )

    assert r.atipico_diario is False
    assert r.direccion_diaria is None
    assert r.es_atipico is False


def test_aplicacion_asigna_tag_atipico_bajo_dia(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        servicio_atipicos,
        "REPORT_DIR",
        tmp_path / "reporte",
    )

    fecha = datetime(2026, 9, 8)

    modelo = ModeloAtipicosPositivos(7).entrenar(
        _dataset(
            fecha,
            total_objetivo=20,
            hora_objetivo=10,
        )
    )

    consumo = _consumo(
        20,
        10,
        fecha,
        hora=8,
    )

    registros = {
        clave_consumo(consumo): ConsumoDB(
            1,
            consumo,
        )
    }

    tags, ids_vacios, resultados = (
        aplicar_modelo_atipicos_positivos(
            [consumo],
            registros,
            modelo,
        )
    )

    assert tags == {
        1: "ATÍPICO BAJO DÍA"
    }
    assert ids_vacios == []
    assert consumo.tag == "ATÍPICO BAJO DÍA"
    assert resultados[0].direccion_diaria == "BAJO"


def test_bodega_san_miguel_29_normal_30_alto():
    filas = []

    valores = {
        8: 1613.85,
        9: 5567.88,
        10: 5658.91,
        11: 5658.91,
        12: 5658.91,
        13: 5186.11,
        14: 5595.63,
        15: 5952.74,
        16: 5748.80,
        17: 6548.55,
        18: 8463.79,
        19: 8063.98,
        20: 7082.14,
        24: 5719.97,
        25: 5719.97,
        26: 5719.97,
        27: 5719.96,
        28: 5721.20,
        29: 6200.00,
        30: 9745.92,
    }

    for dia, total in valores.items():
        fecha = datetime(2026, 9, dia)

        for hora in range(10):
            filas.append({
                "idVBox": 5700,
                "Sucursal": "3628 BODEGA SAN MIGUEL",
                "Cliente": "CLIENTE PRUEBA",
                "Ciudad": "CIUDAD PRUEBA",
                "Area": "Suministro",
                "Fecha": fecha,
                "consumoTotal": total,
                "hora": hora,
                "ConsumoHora": 10.0,
                "tag": "",
                "categoria": "",
            })

    modelo = ModeloAtipicosPositivos(7).entrenar(
        pd.DataFrame(filas)
    )

    consumo_29 = ConsumoAgua(
        idVBox=5700,
        Sucursal="3628 BODEGA SAN MIGUEL",
        Cliente="CLIENTE PRUEBA",
        Area="Suministro",
        NombreMedidor="Medidor",
        Ciudad="CIUDAD PRUEBA",
        Formato="",
        Mercado="",
        Campo="",
        tipo="Invasivo",
        M2=100.0,
        Fecha=datetime(2026, 9, 29),
        consumoTotal=6200.0,
        hora=8,
        ConsumoHora=10.0,
        tag="SIN CATEGORIZAR",
        categoria="",
    )

    consumo_30 = ConsumoAgua(
        idVBox=5700,
        Sucursal="3628 BODEGA SAN MIGUEL",
        Cliente="CLIENTE PRUEBA",
        Area="Suministro",
        NombreMedidor="Medidor",
        Ciudad="CIUDAD PRUEBA",
        Formato="",
        Mercado="",
        Campo="",
        tipo="Invasivo",
        M2=100.0,
        Fecha=datetime(2026, 9, 30),
        consumoTotal=9745.92,
        hora=8,
        ConsumoHora=10.0,
        tag="SIN CATEGORIZAR",
        categoria="",
    )

    r29 = modelo.evaluar(consumo_29)
    r30 = modelo.evaluar(consumo_30)

    assert r29.atipico_diario is False
    assert r29.direccion_diaria is None

    assert r30.atipico_diario is True
    assert r30.direccion_diaria == "ALTO"
