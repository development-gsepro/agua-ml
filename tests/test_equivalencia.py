import importlib.util
import sys
import types
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path

# Stubs mínimos para permitir importar sin conexiones reales.
if "dotenv" not in sys.modules:
    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda *a, **k: None
    sys.modules["dotenv"] = dotenv

if "pyodbc" not in sys.modules:
    pyodbc = types.ModuleType("pyodbc")
    pyodbc.Error = Exception
    pyodbc.Connection = object
    pyodbc.connect = lambda *a, **k: None
    sys.modules["pyodbc"] = pyodbc

if "psycopg2" not in sys.modules:
    psycopg2 = types.ModuleType("psycopg2")
    psycopg2.connect = lambda *a, **k: None
    extras = types.ModuleType("psycopg2.extras")
    extras.execute_values = lambda *a, **k: []
    extensions = types.ModuleType("psycopg2.extensions")
    extensions.connection = object
    psycopg2.extras = extras
    psycopg2.extensions = extensions
    sys.modules["psycopg2"] = psycopg2
    sys.modules["psycopg2.extras"] = extras
    sys.modules["psycopg2.extensions"] = extensions

from app.consumo_agua.models.consumo import ConsumoAgua, SetPointAgua, ConsumoDB
from app.consumo_agua.repositories import postgres_repository as repo
from app.consumo_agua.services import clasificacion as ref_cls
from app.consumo_agua.services import consumo_service as ref_srv
from app.consumo_agua.utils import normalizacion as ref_norm


def cargar_baseline():
    path = Path(__file__).parent / "fixtures" / "consumoAgua_94fa418.py"
    src = path.read_text(encoding="utf-8")
    src = src.replace(
        "from .utils.databaseMSSQL import get_connection",
        "from app.shared.database.databaseMSSQL import get_connection",
        1,
    )
    src = src.replace(
        "from .utils.databasePostgress import get_connection as get_conn",
        "from app.shared.database.databasePostgress import get_connection as get_conn",
        1,
    )
    mod = types.ModuleType("baseline_consumoAgua")
    mod.__file__ = str(path)
    exec(compile(src, str(path), "exec"), mod.__dict__)
    return mod


BASE = cargar_baseline()


def make_consumo(cls, **kwargs):
    data = dict(
        idVBox=5443,
        Sucursal="Sucursal",
        Cliente="GSE - GRUPO WALMART",
        Area="Agua potable",
        NombreMedidor="Medidor",
        Ciudad="Ciudad",
        Formato="BA",
        Mercado="",
        Campo="",
        tipo="Invasivo",
        M2=100.0,
        Fecha=datetime(2026, 9, 21, 0, 0),
        consumoTotal=1000.0,
        hora=0,
        ConsumoHora=10.0,
    )
    data.update(kwargs)
    return cls(**data)


def attrs(c):
    return {
        k: getattr(c, k)
        for k in (
            "idVBox", "Sucursal", "Cliente", "Area", "NombreMedidor", "Ciudad",
            "Formato", "Mercado", "Campo", "tipo", "M2", "Fecha", "consumoTotal",
            "hora", "ConsumoHora", "tag", "categoria", "motivo", "setPointSemana",
            "setPointFinSemana", "setpoint_agua",
        )
    }


def test_normalizacion_equivalente():
    casos = [None, " agua principal ", "AGUA   PRINCIPAL.", " Pipas* ", ""]
    for x in casos:
        assert BASE.normalizar_medidor(x) == ref_norm.normalizar_medidor(x)
        assert BASE.formato_es_m3(x) == ref_norm.formato_es_m3(x)


def test_claves_equivalentes():
    a = make_consumo(BASE.ConsumoAgua, Area=" Agua   potable. ")
    b = make_consumo(ConsumoAgua, Area=" Agua   potable. ")
    assert BASE.clave_medidor(1, " agua  potable. ") == ref_norm.clave_medidor(1, " agua  potable. ")
    assert BASE.clave_consumo(a) == ref_norm.clave_consumo(b)


def test_aplicar_setpoints_walmart_equivalente():
    base_consumos = [
        make_consumo(BASE.ConsumoAgua, Cliente="GSE - GRUPO WALMART", Fecha=datetime(2026, 9, 21), hora=1),
        make_consumo(BASE.ConsumoAgua, Cliente="OTRO CLIENTE", Fecha=datetime(2026, 9, 20), hora=2),
        make_consumo(BASE.ConsumoAgua, idVBox=9999, Area="Sin SP", hora=3),
    ]
    ref_consumos = [
        make_consumo(ConsumoAgua, Cliente="GSE - GRUPO WALMART", Fecha=datetime(2026, 9, 21), hora=1),
        make_consumo(ConsumoAgua, Cliente="OTRO CLIENTE", Fecha=datetime(2026, 9, 20), hora=2),
        make_consumo(ConsumoAgua, idVBox=9999, Area="Sin SP", hora=3),
    ]
    bs = {BASE.clave_medidor(5443, "Agua potable"): BASE.SetPointAgua(5443, "Agua potable", 11.92, 13.5)}
    rs = {ref_norm.clave_medidor(5443, "Agua potable"): SetPointAgua(5443, "Agua potable", 11.92, 13.5)}
    BASE.aplicar_setpoints(base_consumos, bs)
    ref_srv.aplicar_setpoints(ref_consumos, rs)
    assert [attrs(x) for x in base_consumos] == [attrs(x) for x in ref_consumos]
    assert ref_consumos[0].setpoint_agua == 11920.0
    assert ref_consumos[1].setpoint_agua == 13.5
    assert ref_consumos[2].setpoint_agua == 0.0


def test_cargar_setpoints_suma_duplicados_equivalente():
    description = [("idVBox",), ("Medidor",), ("weekdaysValue",), ("weekendValue",)]
    rows = [
        (100, "Agua principal", 1.5, 2.0),
        (100, "Agua principal", 3.0, 4.0),
        (101, "Suministro", 5.0, 6.0),
    ]

    class Cursor:
        def __init__(self):
            self.description = description
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def execute(self, *a, **k): pass
        def fetchall(self): return rows
        def nextset(self): return False

    class Conn:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def cursor(self): return Cursor()

    old_b = BASE.get_connection
    old_r = ref_srv.get_connection
    BASE.get_connection = lambda: Conn()
    ref_srv.get_connection = lambda: Conn()
    try:
        b = BASE.cargar_setpoints()
        r = ref_srv.cargar_setpoints()
    finally:
        BASE.get_connection = old_b
        ref_srv.get_connection = old_r

    def simple(d):
        return {k: (v.idVBox, v.Medidor, v.weekdaysValue, v.weekendValue) for k, v in d.items()}
    assert simple(b) == simple(r)
    assert r[(100, "AGUA PRINCIPAL")].weekdaysValue == 4.5
    assert r[(100, "AGUA PRINCIPAL")].weekendValue == 6.0


def test_row_a_consumo_db_equivalente():
    row = (
        77, 5443, "Suc", "Nom", "BA", "M", "C", "Invasivo", 100,
        datetime(2026, 9, 21), 1200, 4, 20, "TAG", "CAT", 11920,
        "GSE - GRUPO WALMART", "Agua potable", "Ciudad",
    )
    b = BASE.row_a_consumo_db(row)
    r = repo.row_a_consumo_db(row)
    assert b.id == r.id == 77
    assert attrs(b.consumo) == attrs(r.consumo)


def build_days(base=False):
    C = BASE.ConsumoAgua if base else ConsumoAgua
    D = BASE.ConsumoDB if base else ConsumoDB
    rows = []
    start = datetime(2026, 9, 1)
    rid = 1
    for day in range(10):
        fecha = start + timedelta(days=day)
        total = 120 if day in {0,1,2,6,7,8,9} else 50
        for hora in range(24):
            c = make_consumo(
                C,
                Cliente="OTRO",
                Fecha=fecha,
                hora=hora,
                consumoTotal=total,
                ConsumoHora=5 if hora < 6 else 1,
            )
            c.setpoint_agua = 100
            rows.append(D(rid, c))
            rid += 1
    agrupar = BASE.agrupar_por_dia if base else ref_cls.agrupar_por_dia
    return agrupar(rows)


def test_reglas_clasificacion_equivalentes():
    # Estas reglas conservan equivalencia con el monolito solo donde la
    # especificación de negocio no cambió. Fugas y madrugada fueron ajustadas
    # posteriormente por requerimiento explícito.
    for nombre in ("evaluar_consumo_elevado",):
        bdias = build_days(base=True)
        rdias = build_days(base=False)
        ba = BASE.ActualizadorTags()
        ra = repo.ActualizadorTags()
        getattr(BASE, nombre)(bdias, ba)
        getattr(ref_cls, nombre)(rdias, ra)
        assert ba.actualizaciones == ra.actualizaciones, nombre


def test_valor_numeric_postgres_equivalente():
    casos = [None, 1, "2.5", float("inf"), 1e30, "x"]
    for x in casos:
        assert BASE.valor_numeric_postgres(x, "campo") == repo.valor_numeric_postgres(x, "campo")


def test_consumo_total_hora_equivalente():
    rows = [(
        5443, "Sucursal", "GSE - GRUPO WALMART", "Agua potable", "Medidor",
        "Ciudad", "BA", "Mercado", "Campo", "Invasivo", 100.0,
        datetime(2026, 9, 21), 1234.0, 5, 50.0,
    )]

    class Cursor:
        description = [("x",)]
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def execute(self, *a, **k): pass
        def fetchall(self): return rows
        def nextset(self): return False
    class Conn:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def cursor(self): return Cursor()

    old_b = BASE.get_connection
    old_r = ref_srv.get_connection
    BASE.get_connection = lambda: Conn()
    ref_srv.get_connection = lambda: Conn()
    try:
        b = BASE.consumoTotalHora("2026-09-21")
        r = ref_srv.consumoTotalHora("2026-09-21")
    finally:
        BASE.get_connection = old_b
        ref_srv.get_connection = old_r
    assert [attrs(x) for x in b] == [attrs(x) for x in r]


def test_marcar_offline_y_sin_agua_equivalente():
    fecha = datetime(2026, 9, 23)
    prev = datetime(2026, 9, 22)
    bc_prev = make_consumo(BASE.ConsumoAgua, Fecha=prev, consumoTotal=100, hora=0)
    bc_last = make_consumo(BASE.ConsumoAgua, Fecha=fecha, consumoTotal=-1, hora=0)
    rc_prev = make_consumo(ConsumoAgua, Fecha=prev, consumoTotal=100, hora=0)
    rc_last = make_consumo(ConsumoAgua, Fecha=fecha, consumoTotal=-1, hora=0)
    bkey = BASE.clave_medidor(bc_last.idVBox, bc_last.Area)
    rkey = ref_norm.clave_medidor(rc_last.idVBox, rc_last.Area)
    bdb = {BASE.clave_consumo(bc_last): BASE.ConsumoDB(10, bc_last)}
    rdb = {ref_norm.clave_consumo(rc_last): ConsumoDB(10, rc_last)}
    ba = BASE.ActualizadorTags()
    ra = repo.ActualizadorTags()
    BASE.marcar_offline_y_sin_agua(
        {bkey: [bc_prev, bc_last]}, {}, fecha.date(), ba, bdb
    )
    ref_cls.marcar_offline_y_sin_agua(
        {rkey: [rc_prev, rc_last]}, {}, fecha.date(), ra, rdb
    )
    assert ba.actualizaciones == ra.actualizaciones == {10: "MEDIDOR OFFLINE"}


def test_sin_categorizar_equivalente():
    bc = make_consumo(BASE.ConsumoAgua, hora=7)
    rc = make_consumo(ConsumoAgua, hora=7)
    bdb = {BASE.clave_consumo(bc): BASE.ConsumoDB(55, bc)}
    rdb = {ref_norm.clave_consumo(rc): ConsumoDB(55, rc)}

    captured = []
    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def execute(self, query, args): captured.append(args[0])
    class Conn:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def cursor(self): return Cursor()
        def commit(self): pass

    old = BASE.get_conn
    BASE.get_conn = lambda: Conn()
    try:
        BASE.marcar_sin_categorizar([bc], bdb)
    finally:
        BASE.get_conn = old
    ids = ref_cls.preparar_sin_categorizar([rc], rdb)
    assert captured == [[55]]
    assert ids == [55]
    assert bc.tag == rc.tag == "SIN CATEGORIZAR"


def test_verificar_sincronizacion_equivalente():
    bc = make_consumo(BASE.ConsumoAgua)
    rc = make_consumo(ConsumoAgua)
    bdb = {BASE.clave_consumo(bc): BASE.ConsumoDB(1, bc)}
    rdb = {ref_norm.clave_consumo(rc): ConsumoDB(1, rc)}
    assert BASE.verificar_sincronizacion([bc], bdb) is True
    assert ref_srv.verificar_sincronizacion([rc], rdb) is True
