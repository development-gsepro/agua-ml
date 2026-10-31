import unittest
from datetime import datetime

from app.consumo_agua.consumo_agua import separar_medidores_protegidos
from app.consumo_agua.models.consumo import ConsumoAgua


def c(vbox, area, hora, valor, dia=1):
    return ConsumoAgua(
        cc=11587, idVBox=vbox, Sucursal='Prueba', Cliente='Cliente',
        Area=area, NombreMedidor='Medidor', Ciudad='Ciudad',
        Formato='Formato', Mercado='Mercado', Campo='Campo',
        tipo='tipo', M2=100, Fecha=datetime(2026, 9, dia),
        consumoTotal=300, hora=hora, ConsumoHora=valor,
    )


class PruebaProteccion(unittest.TestCase):
    def test_protege_lecturas_ambiguas(self):
        datos = [c(100, 'Agua principal', 2, 10),
                 c(100, 'Agua principal', 2, 20),
                 c(101, 'Agua principal', 2, 10)]
        clasificables, protegidos = separar_medidores_protegidos(datos)
        self.assertEqual(len(clasificables), 1)
        self.assertEqual(clasificables[0].idVBox, 101)
        self.assertEqual(len(protegidos), 1)

    def test_protege_cambio_de_area_verificado(self):
        datos = [c(7369, 'Demasías', h, 200+h) for h in range(24)]
        clasificables, protegidos = separar_medidores_protegidos(datos)
        self.assertEqual(len(clasificables), 0)
        self.assertEqual(len(protegidos), 1)

    def test_protege_7369_el_2_de_septiembre(self):
        datos = [c(7369, 'Demasías', h, 200+h, dia=2) for h in range(24)]
        clasificables, protegidos = separar_medidores_protegidos(datos)
        self.assertEqual(len(clasificables), 0)
        self.assertEqual(len(protegidos), 1)

    def test_protege_7369_el_3_de_septiembre(self):
        datos = [c(7369, 'Demasías', h, 200+h, dia=3) for h in range(24)]
        clasificables, protegidos = separar_medidores_protegidos(datos)
        self.assertEqual(len(clasificables), 0)
        self.assertEqual(len(protegidos), 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
