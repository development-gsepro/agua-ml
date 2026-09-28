"""Capa de compatibilidad con el módulo monolítico anterior."""

from .consumo_agua.consumo_agua import clasificador
from .consumo_agua.models.consumo import ConsumoAgua, SetPointAgua, ConsumoDB, DiaConsumo
from .consumo_agua.repositories.postgres_repository import (
    ActualizadorTags,
    insertar_consumos,
    obtener_historico_postgres,
    row_a_consumo_db,
    valor_numeric_postgres,
)
from .consumo_agua.services.clasificacion import (
    agrupar_por_dia,
    evaluar_consumo_elevado,
    evaluar_fugas,
    evaluar_fuga_reparada,
    evaluar_consumo_madrugada,
    marcar_offline_y_sin_agua,
)
from .consumo_agua.services.consumo_service import (
    aplicar_setpoints,
    buscar_setpoint as _buscar_setpoint,
    cargar_setpoints,
    consumoTotalHora,
    ejecutar_por_dia,
    verificar_sincronizacion,
)
from .consumo_agua.utils.normalizacion import (
    clave_consumo,
    clave_medidor,
    fecha_solo,
    formato_es_m3,
    normalizar_medidor,
)

__all__ = [name for name in globals() if not name.startswith("__")]
