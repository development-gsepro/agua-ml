#!/usr/bin/env python3
"""Audita clasificador real con MSSQL y PG en SOLO LECTURA, sin modelo PKL nuevo."""
import os
from dotenv import load_dotenv
load_dotenv('.env', override=True)

# Cualquier intento accidental de escritura en PG será rechazado.
os.environ['PGOPTIONS'] = '-c default_transaction_read_only=on'
os.environ['AGUA_MODO_SIMULACION'] = '1'

from app.consumo_agua.consumo_agua import clasificador

if __name__ == '__main__':
    import sys
    if len(sys.argv) != 3:
        raise SystemExit('Uso: python -m scripts.simular_clasificador_agua FECHA_INICIO FECHA_FIN')
    clasificador(sys.argv[1], sys.argv[2])
