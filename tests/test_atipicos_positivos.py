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
