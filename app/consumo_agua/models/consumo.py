from dataclasses import dataclass
from datetime import datetime, date
from typing import Optional

HORAS_MADRUGADA = set(range(0, 6))


@dataclass(slots=True)
class ConsumoAgua:
    idVBox: int
    Sucursal: str
    Cliente: str
    Area: str
    NombreMedidor: str
    Ciudad: str
    Formato: str
    Mercado: str
    Campo: str
    tipo: str
    M2: float
    Fecha: datetime
    consumoTotal: float
    hora: int
    ConsumoHora: float

    tag: str = ""
    categoria: str = ""
    motivo: str = ""
    setPointSemana: float = 0.0
    setPointFinSemana: float = 0.0
    setpoint_agua: float = 0.0


@dataclass(slots=True)
class SetPointAgua:
    idVBox: int
    Medidor: str
    weekdaysValue: float
    weekendValue: float


@dataclass(slots=True)
class ConsumoDB:
    id: Optional[int]
    consumo: ConsumoAgua


@dataclass(slots=True)
class DiaConsumo:
    fecha: date
    registros: list[ConsumoDB]

    @property
    def consumo_total(self) -> float:
        return (
            float(self.registros[0].consumo.consumoTotal or 0)
            if self.registros
            else 0.0
        )

    @property
    def set_point(self) -> float:
        if not self.registros:
            return 0.0
        return float(self.registros[0].consumo.setpoint_agua or 0)

    @property
    def supera_sp(self) -> bool:
        return self.consumo_total > self.set_point

    @property
    def tiene_consumo_horario(self) -> bool:
        return any(
            float(r.consumo.ConsumoHora or 0) > 0
            for r in self.registros
        )

    @property
    def consumo_24h(self) -> bool:
        if len(self.registros) != 24:
            return False
        return all(
            float(r.consumo.ConsumoHora or 0) > 0
            for r in self.registros
        )

    @property
    def consumo_madrugada(self) -> float:
        return sum(
            float(r.consumo.ConsumoHora or 0)
            for r in self.registros
            if r.consumo.hora in HORAS_MADRUGADA
        )
