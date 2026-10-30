"""Pruebas aisladas: no conectan ni escriben en una base real."""
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.consumo_agua.repositories import postgres_repository as repo


class PruebasInsercionIdempotente(unittest.TestCase):
    def _consumo(self):
        return SimpleNamespace(
            cc=999999, idVBox=700, Sucursal="Sucursal test", NombreMedidor="Agua",
            Formato="", Mercado="", Campo="", tipo="", M2=1,
            Fecha=date(2026, 8, 1), consumoTotal=42, hora=10,
            ConsumoHora=42, tag="", categoria="", setpoint_agua=0,
            Cliente="Cliente test", Area="Agua principal", Ciudad="",
        )

    def _ejecutar(self, existentes, esperados):
        clave = (700, "AGUA PRINCIPAL", date(2026, 8, 1), 10)
        conn = MagicMock()
        conn.__enter__.return_value = conn
        cursor = MagicMock()
        cursor.__enter__.return_value = cursor
        conn.cursor.return_value = cursor
        with patch.object(repo, "get_conn", return_value=conn), \
             patch.object(repo, "_conteos_existentes_dia", return_value={clave: existentes} if existentes[0] else {}), \
             patch.object(repo, "execute_values", side_effect=lambda cur, sql, valores, **kwargs: [(1000+i,) for i in range(len(valores))]) as execute:
            resultados = repo.insertar_consumos([self._consumo(), self._consumo()])
            insertadas = sum(len(call.args[2]) for call in execute.call_args_list)
            self.assertEqual(insertadas, esperados)
            self.assertEqual(len(resultados), 1)
            self.assertIsNotNone(resultados[clave].id)
            conn.commit.assert_called_once()

    def test_primera_ejecucion_conserva_duplicados_legitimos(self):
        self._ejecutar((0, None), 2)

    def test_ejecucion_parcial_inserta_solo_faltante(self):
        self._ejecutar((1, 99), 1)

    def test_segunda_ejecucion_no_inserta(self):
        self._ejecutar((2, 100), 0)


if __name__ == "__main__":
    unittest.main()
