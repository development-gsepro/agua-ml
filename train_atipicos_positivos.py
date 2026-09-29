#!/usr/bin/env python3

import argparse

from app.consumo_agua.services.atipicos_positivos import (
    MODEL_PATH,
    ModeloAtipicosPositivos,
)
from app.consumo_agua.services.historico_parquet import PARQUET_PATH, leer_historico


def main():
    parser = argparse.ArgumentParser(
        description="Entrena el mapeo robusto de ATÍPICO POSITIVO desde el histórico Parquet."
    )
    parser.add_argument(
        "--min-muestras",
        type=int,
        default=7,
        help="Mínimo de observaciones positivas por perfil (default: 7).",
    )
    args = parser.parse_args()

    df = leer_historico()
    if df.empty:
        raise SystemExit(
            f"No hay histórico en {PARQUET_PATH}. Ejecuta primero el clasificador "
            "para llenar el cache o agrega histórico al Parquet."
        )

    modelo = ModeloAtipicosPositivos(min_muestras=args.min_muestras).entrenar(df)
    modelo.guardar(MODEL_PATH)

    print("=" * 70)
    print("MODELO ATÍPICOS POSITIVOS")
    print("=" * 70)
    print(f"Parquet: {PARQUET_PATH}")
    print(f"PKL: {MODEL_PATH}")
    print(f"Filas históricas: {len(df)}")
    print(f"Perfiles generados: {len(modelo.perfiles)}")
    print(f"Mínimo de muestras: {modelo.min_muestras}")
    print(f"Entrenado en: {modelo.entrenado_en}")


if __name__ == "__main__":
    main()
