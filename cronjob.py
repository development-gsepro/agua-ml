from datetime import datetime, timedelta
from app.consumo_agua.consumo_agua import clasificador

ayer = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')

clasificador(ayer, ayer)