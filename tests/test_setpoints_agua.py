import unittest
from datetime import date, datetime, timedelta
from app.consumo_agua.models.consumo import ConsumoAgua, ConsumoDB, DiaConsumo
from app.consumo_agua.services.clasificacion import (
    evaluar_consumo_elevado, evaluar_fuga_reparada, evaluar_consumo_madrugada,
)


def d(dia, total, sp, hora=0, consumo_hora=0):
    c = ConsumoAgua(
        cc=11587, idVBox=1234, Sucursal='Prueba', Cliente='Prueba',
        Area='Agua principal', NombreMedidor='Principal', Ciudad='Prueba',
        Formato='', Mercado='', Campo='', tipo='', M2=100,
        Fecha=datetime.combine(dia, datetime.min.time()),
        consumoTotal=total, hora=hora, ConsumoHora=consumo_hora,
        setpoint_agua=sp,
    )
    return DiaConsumo(dia, [ConsumoDB(id=dia.toordinal(), consumo=c)])


class Spy:
    def __init__(self):
        self.actualizaciones = {}
    def agregar(self, registros, tag):
        for r in registros:
            self.actualizaciones[r.id] = tag


class TestSetpointsHistoricos(unittest.TestCase):
    def test_sp_desconocido_no_se_supera(self):
        hoy = date(2026, 9, 1)
        self.assertFalse(d(hoy, 120, 0).supera_sp)
        self.assertFalse(d(hoy, 120, -1).supera_sp)
        self.assertTrue(d(hoy, 120, 100).supera_sp)

    def test_siete_dias_con_sp_valido_elevan(self):
        hoy = date(2026, 9, 1)
        dias = {hoy + timedelta(days=i): d(hoy + timedelta(days=i), 120, 100)
                for i in range(7)}
        spy = Spy()
        evaluar_consumo_elevado(dias, spy)
        self.assertGreater(len(spy.actualizaciones), 0)

    def test_siete_dias_con_un_sp_faltante_no_elevan(self):
        hoy = date(2026, 9, 1)
        dias = {hoy + timedelta(days=i): d(hoy + timedelta(days=i), 120, 0 if i == 3 else 100)
                for i in range(7)}
        spy = Spy()
        evaluar_consumo_elevado(dias, spy)
        self.assertFalse(spy.actualizaciones)

    def test_no_inventa_fuga_reparada_sin_setpoint(self):
        hoy = date(2026, 9, 1)
        dias = {hoy: d(hoy, 120, 100)}
        for i in range(1, 5):
            dia = hoy + timedelta(days=i)
            dias[dia] = d(dia, 50, 0 if i == 2 else 100)
        spy = Spy()
        evaluar_fuga_reparada(dias, spy)
        self.assertFalse(spy.actualizaciones)

    def test_no_inventa_madrugada_sin_setpoint(self):
        hoy = date(2026, 9, 1)
        spy = Spy()
        evaluar_consumo_madrugada({hoy: d(hoy, 100, 0, hora=0, consumo_hora=20)}, spy)
        self.assertFalse(spy.actualizaciones)


if __name__ == '__main__':
    unittest.main(verbosity=2)
