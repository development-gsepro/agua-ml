"""Valida setpoint_agua del refactorizado contra SP PONCHO del Excel del profesor."""
from pathlib import Path
import sys

import pandas as pd
from dotenv import load_dotenv

load_dotenv()

from app.shared.database.databasePostgress import get_connection


def main(excel_path: str):
    archivo = Path(excel_path).expanduser()
    if not archivo.exists():
        raise SystemExit(f"No existe el Excel: {archivo}")

    df_excel = pd.read_excel(archivo, sheet_name="CONSUMO AGUA 2")
    rel = df_excel["REL"]
    rel_false = (
        rel.eq(False)
        | rel.astype(str).str.strip().str.upper().isin(["FALSE", "FALSO", "0"])
    )
    df_errores = df_excel[rel_false].copy()

    conn = get_connection()
    try:
        query = '''
            SELECT DISTINCT ON ("idVBox", "Area", "Fecha", "hora")
                "idVBox", "Area", "Fecha", "hora", "Cliente", "Formato",
                setpoint_agua
            FROM public.consumo_agua
            WHERE "Fecha" >= '2026-09-16'
              AND "Fecha" <  '2026-09-24'
            ORDER BY "idVBox", "Area", "Fecha", "hora", id DESC;
        '''
        cursor = conn.cursor()
        cursor.execute(query)
        columnas = [d[0] for d in cursor.description]
        df_db = pd.DataFrame(cursor.fetchall(), columns=columnas)
    finally:
        conn.close()

    for df in (df_errores, df_db):
        df["idVBox"] = pd.to_numeric(df["idVBox"], errors="coerce")
        df["hora"] = pd.to_numeric(df["hora"], errors="coerce")
        df["Fecha"] = pd.to_datetime(df["Fecha"]).dt.normalize()
        df["Area"] = df["Area"].fillna("").astype(str).str.strip().str.upper()

    comparacion = df_errores.merge(
        df_db,
        on=["idVBox", "Area", "Fecha", "hora"],
        how="left",
        suffixes=("_excel", "_nuevo"),
        indicator=True,
    )
    comparacion["SP PONCHO"] = pd.to_numeric(comparacion["SP PONCHO"], errors="coerce")
    comparacion["setpoint_agua_nuevo"] = pd.to_numeric(
        comparacion["setpoint_agua_nuevo"], errors="coerce"
    )
    encontrados = comparacion["_merge"].eq("both")
    coinciden = encontrados & (
        (comparacion["setpoint_agua_nuevo"] - comparacion["SP PONCHO"]).abs() <= 0.01
    )
    no_coinciden = encontrados & ~coinciden

    print("=" * 80)
    print("VALIDACIÓN WALMART - REFACTORIZADO")
    print("=" * 80)
    print(f"REL=false originales: {len(comparacion)}")
    print(f"Encontrados en PostgreSQL: {int(encontrados.sum())}")
    print(f"Ahora coinciden con SP PONCHO: {int(coinciden.sum())}")
    print(f"Siguen diferentes: {int(no_coinciden.sum())}")
    print(f"No encontrados: {int((~encontrados).sum())}")
    if encontrados.sum():
        print(f"Porcentaje corregido: {coinciden.sum()/encontrados.sum()*100:.2f}%")

    salida = Path("validacion_setpoints_refactorizado.xlsx")
    comparacion.to_excel(salida, index=False)
    print(f"Archivo generado: {salida}")

    if no_coinciden.any() or (~encontrados).any():
        raise SystemExit(1)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(
            'Uso: python tests/validar_walmart_excel.py "/ruta/CONSUMO AGUA .xlsx"'
        )
    main(sys.argv[1])
