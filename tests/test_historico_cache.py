from app.consumo_agua.services import historico_parquet as hp


def test_rango_fechas_inclusivo():
    assert hp._rango_fechas("2026-09-16", "2026-09-18") == [
        "2026-09-16", "2026-09-17", "2026-09-18"
    ]
