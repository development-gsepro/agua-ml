"""Pruebas aisladas sin conexiones a PostgreSQL/MSSQL."""
import sys
import unittest
from pathlib import Path
from datetime import datetime, date
from types import ModuleType
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# El ZIP recibido contiene solamente ocho archivos; sustituimos módulos de
# conexión por simuladores antes de cargar postgres_repository.
psycopg2 = ModuleType('psycopg2')
extras = ModuleType('psycopg2.extras')
extras.execute_values = lambda *a, **k: None
psycopg2.extras = extras
sys.modules.setdefault('psycopg2', psycopg2)
sys.modules.setdefault('psycopg2.extras', extras)
sys.modules.setdefault('app.shared', ModuleType('app.shared'))
sys.modules.setdefault('app.shared.database', ModuleType('app.shared.database'))
pg = ModuleType('app.shared.database.databasePostgress')
pg.get_connection = lambda: None
sys.modules['app.shared.database.databasePostgress'] = pg

from app.consumo_agua.models.consumo import ConsumoAgua
from app.consumo_agua.utils.normalizacion import clave_fila
from app.consumo_agua.repositories import postgres_repository as repo

class Cursor:
    def __init__(self, db):
        self.db = db
        self.rows = []
    def __enter__(self): return self
    def __exit__(self, *_): return False
    def __iter__(self): return iter(self.rows)
    def fetchone(self): return self.rows[0]
    def execute(self, sql, params=None):
        if 'pg_advisory_xact_lock' in sql:
            self.rows = [(None,)]
        elif 'information_schema.columns' in sql:
            self.rows = [(self.db.has_cc,)]
        elif 'SELECT MAX(id)' in sql:
            dia = params[0]
            d = {}
            for rid, values in self.db.data:
                if (values[9].date() if isinstance(values[9], datetime) else values[9]) != dia: continue
                raw = tuple(values[:13] + values[16:19])
                d[raw] = max(rid, d.get(raw, 0))
            self.rows = [(rid, *raw) for raw, rid in d.items()]
        else:
            raise AssertionError(sql)

class Conn:
    def __init__(self, db): self.db = db
    def __enter__(self): return self
    def __exit__(self, *_): return False
    def cursor(self): return Cursor(self.db)
    def commit(self): pass

class Database:
    def __init__(self):
        self.data = []
        self.has_cc = True
        self.next_id = 1
    def connect(self): return Conn(self)
    def execute_values(self, cur, sql, vals, page_size=None, fetch=False):
        assert 'INSERT INTO' in sql
        result = []
        for v in vals:
            self.data.append((self.next_id, v))
            result.append((self.next_id,))
            self.next_id += 1
        return result


def fila(*, area='Agua principal', nombre='principal', consumo=30,
         tag='', sp=3000):
    return ConsumoAgua(
        cc=345, idVBox=2028, Sucursal='KFC', Cliente='GSE',
        Area=area, NombreMedidor=nombre, Ciudad='Salamanca',
        Formato='KFC', Mercado='M', Campo='', tipo='', M2=100,
        Fecha=date(2026, 9, 1), consumoTotal=3780,
        hora=0, ConsumoHora=consumo, tag=tag, setpoint_agua=sp,
    )

class Pruebas(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        self.a = patch.object(repo, 'get_conn', self.db.connect)
        self.b = patch.object(repo, 'execute_values', self.db.execute_values)
        self.a.start(); self.b.start()
    def tearDown(self):
        self.b.stop(); self.a.stop()

    def test_diferentes_lecturas_y_nombre_no_se_pierden(self):
        consumos = [fila(), fila(consumo=31), fila(nombre='auxiliar'), fila(tag='CONSUMO ELEVADO', sp=0)]
        r = repo.insertar_consumos(consumos)
        self.assertEqual(len(r), 3)
        self.assertEqual(len(self.db.data), 3)
        self.assertEqual(len(set(x.id for x in r.values())), 3)
        self.assertEqual(len({clave_fila(c) for c in consumos}), 3)

    def test_segunda_ejecucion_no_inserta_aunque_cambien_tags(self):
        consumos = [fila(), fila(consumo=31)]
        a = repo.insertar_consumos(consumos)
        consumos[0].tag = 'CONSUMO DE MADRUGADA'
        consumos[0].setpoint_agua = 9999
        b = repo.insertar_consumos(consumos)
        self.assertEqual(len(self.db.data), 2)
        self.assertEqual({k:v.id for k,v in a.items()}, {k:v.id for k,v in b.items()})

    def test_cc_faltante_aborta_sin_insertar(self):
        self.db.has_cc = False
        with self.assertRaisesRegex(RuntimeError, 'cc'):
            repo.insertar_consumos([fila()])
        self.assertEqual(self.db.data, [])

    def test_fila_existente_de_clasificacion_distinta_se_reutiliza(self):
        r1 = repo.insertar_consumos([fila(tag='')])
        r2 = repo.insertar_consumos([fila(tag='CONSUMO ELEVADO', sp=0)])
        self.assertEqual(len(self.db.data), 1)
        self.assertEqual(next(iter(r1.values())).id, next(iter(r2.values())).id)

if __name__ == '__main__': unittest.main(verbosity=2)
