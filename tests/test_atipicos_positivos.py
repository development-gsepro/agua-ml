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
    assert len(modelo.perfiles_horarios) == 8


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

    assert ids_pos == [1]
    assert ids_vacios == [2]
    assert positivo.tag == "ATÍPICO POSITIVO"
    assert normal.tag == ""
    assert protegido.tag == "FUGA ACTIVA"
    assert len(resultados) == 2


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

    assert set(ids_pos) == {1, 2}
    assert ids_vacios == []

    assert all(r.atipico_grupo_diario is False for r in resultados)
    assert all(r.atipico_sucursal_diario is True for r in resultados)

    assert principal.tag == "ATÍPICO POSITIVO"
    assert filtrada.tag == "ATÍPICO POSITIVO"


def test_sucursal_horaria_detecta_atipico_aunque_grupos_no_marcan(
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

    assert set(ids_pos) == {1, 2}
    assert ids_vacios == []

    assert all(r.atipico_grupo_diario is False for r in resultados)
    assert all(r.atipico_sucursal_diario is False for r in resultados)

    assert all(r.atipico_grupo_horario is False for r in resultados)
    assert all(r.atipico_sucursal_horario is True for r in resultados)

    assert principal.tag == "ATÍPICO POSITIVO"
    assert filtrada.tag == "ATÍPICO POSITIVO"
