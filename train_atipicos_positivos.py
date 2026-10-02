#!/usr/bin/env python3

import argparse

from app.consumo_agua.services.atipicos_positivos import (
    MODEL_PATH,
    entrenar_y_guardar_modelo,
)
from app.consumo_agua.services.historico_parquet import PARQUET_PATH, leer_historico


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Entrenamiento manual opcional de ATÍPICO POSITIVO. "
            "El cronjob ya entrena automáticamente antes del flujo normal."
        )
    )
    parser.add_argument(
        "--min-muestras",
        type=int,
        default=7,
        help="Mínimo de observaciones por perfil (default: 7).",
    )
    args = parser.parse_args()

    df = leer_historico()
    if df.empty:
        raise SystemExit(f"No hay histórico disponible en {PARQUET_PATH}.")

    modelo = entrenar_y_guardar_modelo(df, min_muestras=args.min_muestras)

    print("=" * 70)
    print("MODELO ATÍPICOS POSITIVOS - ENTRENAMIENTO MANUAL OPCIONAL")
    print("=" * 70)
    print(f"Parquet: {PARQUET_PATH}")
    print(f"PKL: {MODEL_PATH}")
    print(f"Filas históricas: {len(df)}")
    print(f"Perfiles diarios: {len(modelo.perfiles_diarios)}")
    print(f"Perfiles horarios: {len(modelo.perfiles_horarios)}")
    print(f"Mínimo de muestras: {modelo.min_muestras}")
    print(f"Entrenado en: {modelo.entrenado_en}")


if __name__ == "__main__":
    main()
