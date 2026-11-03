"""Regresiones: fuga reparada exige 5 días CONTIGUOS y SP estricto."""
import unittest
from dataclasses import dataclass
from datetime import date, timedelta
from app.consumo_agua.services.clasificacion import evaluar_fuga_reparada

@dataclass
class Dia:
    fecha: date
    consumo_total: float
    set_point: float = 100.0
    @property
    def supera_sp(self):
        return self.set_point > 0 and self.consumo_total > self.set_point
    @property
    def registros(self):
        return [self.fecha]

class Actualizador:
    def __init__(self):
        self.tags = []
    def agregar(self, registros, tag):
        self.tags.append((registros, tag))

class RegresionFugaReparada(unittest.TestCase):
    def evaluar(self, fechas, consumos):
        dias = {f: Dia(f, n) for f,n in zip(fechas, consumos)}
        actualizador = Actualizador()
        evaluar_fuga_reparada(dias, actualizador)
        return actualizador.tags

    def test_cinco_dias_consecutivos_validos(self):
        base = date(2026,8,28)
        dias = [base + timedelta(days=n) for n in range(5)]
        self.assertEqual(len(self.evaluar(dias, [140,50,50,50,50])),1)

    def test_hueco_entre_antecedente_y_cuatro_dias(self):
        base = date(2026,8,27)
        dias = [base, *(base+timedelta(days=n) for n in (2,3,4,5))]
        self.assertEqual(self.evaluar(dias, [140,50,50,50,50]),[])

    def test_igualdad_al_setpoint_rompe_racha(self):
        base = date(2026,8,28)
        dias = [base + timedelta(days=n) for n in range(5)]
        self.assertEqual(self.evaluar(dias,[140,50,100,50,50]),[])

    def test_dia_de_antecedente_debe_superar_sp(self):
        base = date(2026,8,28)
        dias = [base + timedelta(days=n) for n in range(5)]
        self.assertEqual(self.evaluar(dias,[99,50,50,50,50]),[])

if __name__ == '__main__':
    unittest.main(verbosity=2)
