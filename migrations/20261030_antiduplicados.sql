-- PostgreSQL 15+
-- Evita duplicados exactos de las 16 columnas originales de MSSQL.
-- No elimina registros históricos.

CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS
ux_consumo_agua_origen_cc_v2
ON public.consumo_agua (
    cc, "idVBox", "Sucursal", "NombreMedidor",
    "Formato", "Mercado", "Campo", tipo, "M2",
    "Fecha", "consumoTotal", hora, "ConsumoHora",
    "Cliente", "Area", "Ciudad"
)
NULLS NOT DISTINCT
WHERE cc IS NOT NULL;
