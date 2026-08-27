from .app.consumoAgua import ejecutar_por_dia, consumoTotalHora

# '2026-08-01', '2026-08-24'

fec_ini = ''
fec_fin = ''

consumos = ejecutar_por_dia(
        fec_ini,
        fec_fin,
        consumoTotalHora
    ) 


# VISUALIZAR Y ANALIZAR > 0 EN CONSUMO TOTAL | PUNTO FUERA DE LO COMUN CLASIFICARLO COMO ATIPICO POSITIVO
    # Total -> Dia | 24 "ATIPICO POSITIVO EN CONSUMO TOTAL" -> RANGO DE FECHA (25) 
        # HORA -> DIA | 24 "ATIPICO POSITIVO EN CONSUMO HORA" -> HORA DEL ATIPICO

    
