from datetime import datetime, timedelta

import pandas as pd

from app.consumo_agua.models.consumo import ConsumoAgua, ConsumoDB
from app.consumo_agua.services.atipicos_positivos import (
    ModeloAtipicosPositivos,
    aplicar_modelo_atipicos_positivos,
)
from app.consumo_agua.utils.normalizacion import clave_consumo


def _consumo(valor, fecha=None, tag="SIN CATEGORIZAR", categoria=""):
    return ConsumoAgua(
        idVBox=100,
        Sucursal="Sucursal",
        Cliente="CLIENTE PRUEBA",
        Area="Agua principal",
        NombreMedidor="Medidor",
        Ciudad="CIUDAD PRUEBA",
        Formato="",
        Mercado="",
        Campo="",
        tipo="Invasivo",
        M2=100.0,
        Fecha=fecha or datetime(2026, 9, 1),
        consumoTotal=100.0,
        hora=8,
        ConsumoHora=valor,
        tag=tag,
        categoria=categoria,
    )


def test_modelo_detecta_solo_extremo_superior():
    filas = []
    base = datetime(2026, 8, 3)  # lunes
    for i, valor in enumerate([10, 11, 10, 12, 11, 10, 9, 11, 10, 12, 11, 10, 10, 11]):
        fecha = base + timedelta(days=i)
        filas.append({
            "idVBox": 100,
            "Sucursal": "Sucursal prueba",
            "Cliente": "CLIENTE PRUEBA",
            "Ciudad": "CIUDAD PRUEBA",
            "Area": "Agua principal",
            "Fecha": fecha,
            "hora": 8,
            "ConsumoHora": valor,
            "tag": "",
            "categoria": "",
        })
    modelo = ModeloAtipicosPositivos(min_muestras=7).entrenar(pd.DataFrame(filas))

    normal = _consumo(12, datetime(2026, 9, 1))
    extremo = _consumo(100, datetime(2026, 9, 1))

    assert modelo.evaluar(normal).es_atipico is False
    assert modelo.evaluar(extremo).es_atipico is True
    assert modelo.evaluar(extremo).limite is not None


def test_aplicacion_respeta_candidatos_y_deja_no_positivos_vacios():
    filas = []
    base = datetime(2026, 8, 3)
    for i in range(14):
        filas.append({
            "idVBox": 100,
            "Sucursal": "Sucursal prueba",
            "Cliente": "CLIENTE PRUEBA",
            "Ciudad": "CIUDAD PRUEBA",
            "Area": "Agua principal",
            "Fecha": base + timedelta(days=i),
            "hora": 8,
            "ConsumoHora": 10 + (i % 2),
            "tag": "",
            "categoria": "",
        })
    modelo = ModeloAtipicosPositivos(min_muestras=7).entrenar(pd.DataFrame(filas))

    positivo = _consumo(100, datetime(2026, 9, 1))
    normal = _consumo(10, datetime(2026, 9, 2))
    protegido = _consumo(100, datetime(2026, 9, 3), tag="FUGA ACTIVA")

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
    filas = []
    base = datetime(2026, 8, 3)
    for i in range(14):
        filas.append({
            "idVBox": 100,
            "Sucursal": "Sucursal prueba",
            "Cliente": "CLIENTE PRUEBA",
            "Ciudad": "CIUDAD PRUEBA",
            "Area": "Agua principal",
            "Fecha": base + timedelta(days=i),
            "hora": 8,
            "ConsumoHora": 10 + (i % 2),
            "tag": "",
            "categoria": "",
        })
    modelo = ModeloAtipicosPositivos(min_muestras=7).entrenar(pd.DataFrame(filas))
    ruta = tmp_path / "modelo.pkl"
    modelo.guardar(ruta)
    cargado = ModeloAtipicosPositivos.cargar(ruta)
    assert cargado.version == modelo.version
    assert cargado.min_muestras == modelo.min_muestras
    assert cargado.perfiles.keys() == modelo.perfiles.keys()
