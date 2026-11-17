# from datetime import datetime, timedelta
from app.consumo_agua.consumo_agua import clasificador

# ayer = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')


clasificador('2026-08-16', '2026-08-31')

input(f"Termino: {'2026-08-16', '2026-08-31'} Vuelve a prender MSSQL | Presione ENTER para continuar... ")

clasificador('2026-09-01', '2026-09-15')

input(f"Termino: {'2026-09-01', '2026-09-15'} Vuelve a prender MSSQL | Presione ENTER para continuar... ")

clasificador('2026-09-16', '2026-09-30')

input(f"Termino: {'2026-09-16', '2026-09-30'} Vuelve a prender MSSQL | Presione ENTER para continuar... ")

clasificador('2026-10-01', '2026-10-15')

input(f"Termino: {'2026-10-01', '2026-10-15'} Vuelve a prender MSSQL | Presione ENTER para continuar... ")

clasificador('2026-10-16', '2026-10-31')

input(f"Termino: {'2026-10-16', '2026-10-31'} Vuelve a prender MSSQL | Presione ENTER para continuar... ")

clasificador('2026-11-01', '2026-11-15 ')