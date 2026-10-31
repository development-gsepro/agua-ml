from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import csv
import pickle
from pathlib import Path
import re
from typing import Iterable

import numpy as np
import pandas as pd

from ..models.consumo import ConsumoAgua, ConsumoDB, es_medidor_virtual
from ..utils.normalizacion import clave_fila


ROOT_DIR = Path(__file__).resolve().parents[3]
DATA_DIR = ROOT_DIR / "data"
MODELS_DIR = DATA_DIR / "models"
MODEL_PATH = MODELS_DIR / "atipico_positivo.pkl"
REPORT_DIR = DATA_DIR / "reportes_atipicos"

TAG_ATIPICO_POSITIVO = "ATÍPICO POSITIVO"
TAG_ATIPICO_POSITIVO_DIA = "ATÍPICO POSITIVO DÍA"
TAG_ATIPICO_POSITIVO_HORA = "ATÍPICO POSITIVO HORA"
TAG_ATIPICO_BAJO_DIA = "ATÍPICO BAJO DÍA"
TAG_SIN_CATEGORIZAR = "SIN CATEGORIZAR"
MODEL_VERSION = 6

# Diario: (consumo_total, fecha_iso)
ObservacionDiaria = tuple[float, str]
# Horario: (consumo_hora, fecha_iso)
ObservacionHoraria = tuple[float, str]


# MULTIPLICADOR DE AUMENTO
# 1.0 = valores originales; > 1.0 = mayor tolerancia.
MULTIPLICADOR_AUMENTO = 2

# Percentiles para calcular los cuartiles.
PERCENTIL_Q1 = 0.25
PERCENTIL_Q3 = 0.75

# Factor de escala para estimar la desviación estándar robusta.
FACTOR_ESCALA_MAD = 1.4826

# Factores base para los límites de detección.
FACTOR_LIMITE_MAD_BASE = 3.5
FACTOR_LIMITE_IQR_BASE = 1.5

# Factores ajustados según la prueba.
FACTOR_LIMITE_MAD = FACTOR_LIMITE_MAD_BASE * MULTIPLICADOR_AUMENTO
FACTOR_LIMITE_IQR = FACTOR_LIMITE_IQR_BASE * MULTIPLICADOR_AUMENTO

def normalizar_grupo(valor: object) -> str:
    texto = str(valor or "").strip().upper()
    return re.sub(r"\s+", " ", texto)


def _clave_base(id_vbox: object, sucursal: object, area: object) -> tuple[int, str, str]:
    return (
        int(id_vbox),
        normalizar_grupo(sucursal),
        normalizar_grupo(area),
    )


def _fecha_iso(valor: object) -> str:
    return pd.Timestamp(valor).normalize().date().isoformat()


@dataclass(slots=True)
class PerfilDiario:
    id_vbox: int
    sucursal: str
    area: str
    n: int
    mediana: float
    mad: float
    q1: float
    q3: float
    limite_superior: float
    observaciones: tuple[ObservacionDiaria, ...]


@dataclass(slots=True)
class PerfilHorario:
    id_vbox: int
    sucursal: str
    area: str
    hora: int
    n: int
    mediana: float
    mad: float
    q1: float
    q3: float
    limite_superior: float
    observaciones: tuple[ObservacionHoraria, ...]


@dataclass(slots=True)
class ResultadoAtipicoPositivo:
    consumo: ConsumoAgua
    es_atipico: bool
    atipico_diario: bool
    atipico_horario: bool
    valor_diario: float
    valor_horario: float
    limite_diario: float | None
    limite_horario: float | None
    n_diario: int
    n_horario: int
    posicion_diaria: int | None = None
    total_posiciones_diarias: int = 0
    percentil_diario: float | None = None
    posicion_horaria: int | None = None
    total_posiciones_horarias: int = 0
    percentil_horario: float | None = None
    direccion_diaria: str | None = None
    limite_inferior_diario: float | None = None

    # Validaciones adicionales agregadas a nivel sucursal.
    atipico_grupo_diario: bool = False
    atipico_grupo_horario: bool = False
    atipico_sucursal_diario: bool = False
    atipico_sucursal_horario: bool = False
    valor_sucursal_diario: float = 0.0
    valor_sucursal_horario: float = 0.0
    limite_sucursal_diario: float | None = None
    limite_sucursal_horario: float | None = None

    @property
    def tiene_perfil(self) -> bool:
        return self.limite_diario is not None or self.limite_horario is not None


class ModeloAtipicosPositivos:
    """Modelo de atípicos positivos por medidor.

    Mantiene dos mapas independientes:
      1) Diario: idVBox + Sucursal + Area -> consumoTotal por fecha.
      2) Horario: idVBox + Sucursal + Area + Fecha -> ConsumoHora por hora.

    ``tipo`` / tipo de día no participa en la IA. Ese dato permanece fuera del
    modelo y puede seguir utilizándose para seleccionar el SetPoint correcto.
    """

    def __init__(self, min_muestras: int = 7):
        self.min_muestras = int(min_muestras)
        self.perfiles_diarios: dict[tuple[int, str, str], PerfilDiario] = {}
        self.perfiles_horarios: dict[tuple[int, str, str, int], PerfilHorario] = {}

        # Segunda capa: comportamiento completo de la sucursal.
        self.perfiles_sucursal_diarios: dict[tuple[int, str], PerfilDiario] = {}
        self.perfiles_sucursal_horarios: dict[tuple[int, str, int], PerfilHorario] = {}

        self.entrenado_en: str | None = None
        self.version = MODEL_VERSION

    @staticmethod
    def _estadisticas(valores: np.ndarray) -> tuple[float, float, float, float, float]:
        valores = valores[np.isfinite(valores)]
        mediana = float(np.median(valores))
        q1 = float(np.quantile(valores, PERCENTIL_Q1))
        q3 = float(np.quantile(valores, PERCENTIL_Q3))
        iqr = q3 - q1
        mad = float(np.median(np.abs(valores - mediana)))
        sigma_robusta = FACTOR_ESCALA_MAD * mad
        limite_mad = mediana + FACTOR_LIMITE_MAD * sigma_robusta
        limite_iqr = q3 + FACTOR_LIMITE_IQR * iqr
        limite = max(limite_mad, limite_iqr, mediana)
        return mediana, mad, q1, q3, float(limite)

    @staticmethod
    def _normalizar_df(df: pd.DataFrame) -> pd.DataFrame:
        requerido = {
            "idVBox", "Sucursal", "Area", "Fecha",
            "consumoTotal", "hora", "ConsumoHora",
        }
        faltantes = requerido - set(df.columns)
        if faltantes:
            raise ValueError(
                "Faltan columnas para entrenar atípicos positivos: "
                + ", ".join(sorted(faltantes))
            )

        trabajo = df.copy()
        trabajo["Fecha"] = pd.to_datetime(trabajo["Fecha"], errors="coerce")
        trabajo["idVBox"] = pd.to_numeric(trabajo["idVBox"], errors="coerce")
        trabajo["consumoTotal"] = pd.to_numeric(
            trabajo["consumoTotal"], errors="coerce"
        )
        trabajo["hora"] = pd.to_numeric(trabajo["hora"], errors="coerce")
        trabajo["ConsumoHora"] = pd.to_numeric(
            trabajo["ConsumoHora"], errors="coerce"
        )
        trabajo = trabajo.dropna(subset=["Fecha", "idVBox", "hora"])

        trabajo["sucursal_norm"] = trabajo["Sucursal"].map(normalizar_grupo)
        trabajo["area_norm"] = trabajo["Area"].map(normalizar_grupo)
        trabajo = trabajo[
            trabajo["sucursal_norm"].ne("")
            & trabajo["area_norm"].ne("")
            & ~trabajo["Area"].map(es_medidor_virtual)
        ].copy()

        trabajo["idVBox"] = trabajo["idVBox"].astype(int)
        trabajo["hora"] = trabajo["hora"].astype(int)
        trabajo["fecha_dia"] = trabajo["Fecha"].dt.normalize()
        return trabajo

    @classmethod
    def _perfil_diario(
        cls,
        clave: tuple[int, str, str],
        grupo: pd.DataFrame,
    ) -> PerfilDiario:
        # consumoTotal se repite en las filas horarias del mismo día. El mapa
        # diario necesita una sola observación por fecha.
        por_fecha = (
            grupo.sort_values(["fecha_dia", "hora"], kind="stable")
            .drop_duplicates(subset=["fecha_dia"], keep="first")
        )
        por_fecha = por_fecha[
            np.isfinite(por_fecha["consumoTotal"]) & (por_fecha["consumoTotal"] > 0)
        ]
        valores = por_fecha["consumoTotal"].to_numpy(dtype=float)
        mediana, mad, q1, q3, limite = cls._estadisticas(valores)
        obs = tuple(
            (float(f.consumoTotal), _fecha_iso(f.fecha_dia))
            for f in por_fecha.itertuples(index=False)
        )
        return PerfilDiario(
            id_vbox=clave[0], sucursal=clave[1], area=clave[2],
            n=len(valores), mediana=mediana, mad=mad, q1=q1, q3=q3,
            limite_superior=limite, observaciones=obs,
        )

    @classmethod
    def _perfil_horario(
        cls,
        clave: tuple[int, str, str, int],
        grupo: pd.DataFrame,
    ) -> PerfilHorario:
        por_fecha = (
            grupo.sort_values(["fecha_dia"], kind="stable")
            .drop_duplicates(subset=["fecha_dia"], keep="first")
        )
        por_fecha = por_fecha[
            np.isfinite(por_fecha["ConsumoHora"])
            & (por_fecha["ConsumoHora"] > 0)
        ]
        valores = por_fecha["ConsumoHora"].to_numpy(dtype=float)
        mediana, mad, q1, q3, limite = cls._estadisticas(valores)
        obs = tuple(
            (
                float(f.ConsumoHora),
                _fecha_iso(f.fecha_dia),
            )
            for f in por_fecha.itertuples(index=False)
        )
        return PerfilHorario(
            id_vbox=clave[0],
            sucursal=clave[1],
            area=clave[2],
            hora=clave[3],
            n=len(valores),
            mediana=mediana,
            mad=mad,
            q1=q1,
            q3=q3,
            limite_superior=limite,
            observaciones=obs,
        )

    def entrenar(self, df: pd.DataFrame) -> "ModeloAtipicosPositivos":
        trabajo = self._normalizar_df(df)

        diarios: dict[tuple[int, str, str], PerfilDiario] = {}
        for raw_clave, grupo in trabajo.groupby(
            ["idVBox", "sucursal_norm", "area_norm"], sort=True, dropna=False
        ):
            clave = (int(raw_clave[0]), str(raw_clave[1]), str(raw_clave[2]))
            validos = (
                grupo.sort_values(["fecha_dia", "hora"], kind="stable")
                .drop_duplicates(subset=["fecha_dia"], keep="first")
            )
            validos = validos[
                np.isfinite(validos["consumoTotal"]) & (validos["consumoTotal"] > 0)
            ]
            if len(validos) >= self.min_muestras:
                diarios[clave] = self._perfil_diario(clave, grupo)

        horarios: dict[tuple[int, str, str, int], PerfilHorario] = {}
        for raw_clave, grupo in trabajo.groupby(
            ["idVBox", "sucursal_norm", "area_norm", "hora"],
            sort=True,
            dropna=False,
        ):
            clave = (
                int(raw_clave[0]),
                str(raw_clave[1]),
                str(raw_clave[2]),
                int(raw_clave[3]),
            )
            validos = (
                grupo.sort_values(["fecha_dia"], kind="stable")
                .drop_duplicates(subset=["fecha_dia"], keep="first")
            )
            validos = validos[
                np.isfinite(validos["ConsumoHora"])
                & (validos["ConsumoHora"] > 0)
            ]
            if len(validos) >= self.min_muestras:
                horarios[clave] = self._perfil_horario(clave, grupo)

        # --------------------------------------------------------------
        # Validación adicional por SUCURSAL.
        #
        # consumoTotal está repetido en cada fila horaria, por eso para
        # sucursal/día primero tomamos una sola observación por Area y fecha
        # y después sumamos todas las áreas físicas de la sucursal.
        # --------------------------------------------------------------
        sucursal_diarios: dict[tuple[int, str], PerfilDiario] = {}

        for raw_clave, grupo in trabajo.groupby(
            ["idVBox", "sucursal_norm"],
            sort=True,
            dropna=False,
        ):
            por_area_dia = (
                grupo.sort_values(
                    ["fecha_dia", "area_norm", "hora"],
                    kind="stable",
                )
                .drop_duplicates(
                    subset=["fecha_dia", "area_norm"],
                    keep="first",
                )
            )

            por_area_dia = por_area_dia[
                np.isfinite(por_area_dia["consumoTotal"])
                & (por_area_dia["consumoTotal"] > 0)
            ]

            por_dia = (
                por_area_dia
                .groupby("fecha_dia", as_index=False)["consumoTotal"]
                .sum()
                .sort_values("fecha_dia", kind="stable")
            )

            por_dia = por_dia[
                np.isfinite(por_dia["consumoTotal"])
                & (por_dia["consumoTotal"] > 0)
            ]

            if len(por_dia) >= self.min_muestras:
                valores = por_dia["consumoTotal"].to_numpy(dtype=float)
                mediana, mad, q1, q3, limite = self._estadisticas(valores)

                clave_sucursal = (
                    int(raw_clave[0]),
                    str(raw_clave[1]),
                )

                sucursal_diarios[clave_sucursal] = PerfilDiario(
                    id_vbox=clave_sucursal[0],
                    sucursal=clave_sucursal[1],
                    area="",
                    n=len(valores),
                    mediana=mediana,
                    mad=mad,
                    q1=q1,
                    q3=q3,
                    limite_superior=limite,
                    observaciones=tuple(
                        (
                            float(f.consumoTotal),
                            _fecha_iso(f.fecha_dia),
                        )
                        for f in por_dia.itertuples(index=False)
                    ),
                )

        sucursal_horarios: dict[tuple[int, str, int], PerfilHorario] = {}

        por_area_hora = (
            trabajo.sort_values(
                ["idVBox", "sucursal_norm", "fecha_dia", "hora", "area_norm"],
                kind="stable",
            )
            .drop_duplicates(
                subset=[
                    "idVBox",
                    "sucursal_norm",
                    "fecha_dia",
                    "hora",
                    "area_norm",
                ],
                keep="first",
            )
        )

        por_area_hora = por_area_hora[
            np.isfinite(por_area_hora["ConsumoHora"])
            & (por_area_hora["ConsumoHora"] > 0)
        ]

        por_sucursal_hora = (
            por_area_hora
            .groupby(
                ["idVBox", "sucursal_norm", "fecha_dia", "hora"],
                as_index=False,
            )["ConsumoHora"]
            .sum()
        )

        for raw_clave, grupo in por_sucursal_hora.groupby(
            ["idVBox", "sucursal_norm", "hora"],
            sort=True,
            dropna=False,
        ):
            grupo = (
                grupo.sort_values(["fecha_dia"], kind="stable")
                .drop_duplicates(subset=["fecha_dia"], keep="first")
            )

            grupo = grupo[
                np.isfinite(grupo["ConsumoHora"])
                & (grupo["ConsumoHora"] > 0)
            ]

            if len(grupo) >= self.min_muestras:
                valores = grupo["ConsumoHora"].to_numpy(dtype=float)
                mediana, mad, q1, q3, limite = self._estadisticas(valores)

                clave_sucursal = (
                    int(raw_clave[0]),
                    str(raw_clave[1]),
                    int(raw_clave[2]),
                )

                sucursal_horarios[clave_sucursal] = PerfilHorario(
                    id_vbox=clave_sucursal[0],
                    sucursal=clave_sucursal[1],
                    area="",
                    hora=clave_sucursal[2],
                    n=len(valores),
                    mediana=mediana,
                    mad=mad,
                    q1=q1,
                    q3=q3,
                    limite_superior=limite,
                    observaciones=tuple(
                        (
                            float(f.ConsumoHora),
                            _fecha_iso(f.fecha_dia),
                        )
                        for f in grupo.itertuples(index=False)
                    ),
                )

        self.perfiles_diarios = diarios
        self.perfiles_horarios = horarios
        self.perfiles_sucursal_diarios = sucursal_diarios
        self.perfiles_sucursal_horarios = sucursal_horarios
        self.entrenado_en = datetime.now().isoformat(timespec="seconds")
        return self

    def _buscar_diario(self, consumo: ConsumoAgua) -> PerfilDiario | None:
        return self.perfiles_diarios.get(
            _clave_base(consumo.idVBox, consumo.Sucursal, consumo.Area)
        )

    def _buscar_horario(self, consumo: ConsumoAgua) -> PerfilHorario | None:
        base = _clave_base(consumo.idVBox, consumo.Sucursal, consumo.Area)
        return self.perfiles_horarios.get((*base, int(consumo.hora)))

    def _evaluar_diario(
        self, consumo: ConsumoAgua
    ) -> tuple[
        bool,
        float | None,
        float | None,
        int,
        int | None,
        int,
        float | None,
        str | None,
    ]:
        try:
            valor = float(consumo.consumoTotal)
        except (TypeError, ValueError, OverflowError):
            return False, None, None, 0, None, 0, None, None

        if not np.isfinite(valor) or valor <= 0:
            return False, None, None, 0, None, 0, None, None

        perfil = self._buscar_diario(consumo)
        if perfil is None:
            return False, None, None, 0, None, 0, None, None

        fecha_actual = _fecha_iso(consumo.Fecha)
        referencia = [
            v
            for v, fecha in perfil.observaciones
            if fecha != fecha_actual
        ]
        referencia = np.asarray(referencia, dtype=float)
        referencia = referencia[
            np.isfinite(referencia) & (referencia > 0)
        ]
        referencia.sort()

        if len(referencia) < self.min_muestras:
            return (
                False,
                None,
                None,
                len(referencia),
                None,
                0,
                None,
                None,
            )

        q1 = float(np.quantile(referencia, 0.25))
        q3 = float(np.quantile(referencia, 0.75))
        iqr = q3 - q1

        limite_inferior = None
        limite_superior = q3 + (MULTIPLICADOR_AUMENTO * iqr)

        posicion = int(
            np.searchsorted(referencia, valor, side="right")
        ) + 1
        total = len(referencia) + 1
        percentil = 100.0 * posicion / total

        if valor > limite_superior:
            exceso_limite = (
                (valor / limite_superior) - 1.0
                if limite_superior > 0
                else float("inf")
            )

            menores = referencia[referencia < valor]
            anterior = float(menores[-1]) if len(menores) else None

            salto_anterior = (
                (valor / anterior) - 1.0
                if anterior is not None and anterior > 0
                else float("inf")
            )

            if exceso_limite >= 0.10 or salto_anterior >= 0.20:
                direccion = "ALTO"
            else:
                direccion = None
        else:
            direccion = None

        return (
            direccion is not None,
            None,
            float(limite_superior),
            len(referencia),
            posicion,
            total,
            percentil,
            direccion,
        )

    def _evaluar_horario(
        self, consumo: ConsumoAgua
    ) -> tuple[bool, float | None, int, int | None, int, float | None]:
        try:
            valor = float(consumo.ConsumoHora)
        except (TypeError, ValueError, OverflowError):
            return False, None, 0, None, 0, None
        if not np.isfinite(valor) or valor <= 0:
            return False, None, 0, None, 0, None

        perfil = self._buscar_horario(consumo)
        if perfil is None:
            return False, None, 0, None, 0, None

        fecha_actual = _fecha_iso(consumo.Fecha)
        referencia = [
            v
            for v, fecha in perfil.observaciones
            if fecha != fecha_actual
        ]
        referencia = np.asarray(referencia, dtype=float)
        referencia = referencia[np.isfinite(referencia)]
        referencia.sort()
        if len(referencia) < self.min_muestras:
            return False, None, len(referencia), None, 0, None

        _med, _mad, _q1, _q3, limite = self._estadisticas(referencia)
        posicion = int(np.searchsorted(referencia, valor, side="right")) + 1
        total = len(referencia) + 1
        percentil = 100.0 * posicion / total

        supera_limite = valor > limite
        supera_margen = valor >= (limite * 1.30)

        return (
            bool(supera_limite and supera_margen),
            limite,
            len(referencia),
            posicion,
            total,
            percentil,
        )

    def _evaluar_sucursal_diario(
        self,
        consumo: ConsumoAgua,
        valor: float,
    ) -> tuple[bool, float | None, int]:
        if not np.isfinite(valor) or valor <= 0:
            return False, None, 0

        clave = (
            int(consumo.idVBox),
            normalizar_grupo(consumo.Sucursal),
        )
        perfil = self.perfiles_sucursal_diarios.get(clave)
        if perfil is None:
            return False, None, 0

        fecha_actual = _fecha_iso(consumo.Fecha)
        referencia = np.asarray(
            [
                v
                for v, fecha in perfil.observaciones
                if fecha != fecha_actual
            ],
            dtype=float,
        )
        referencia = referencia[np.isfinite(referencia)]

        if len(referencia) < self.min_muestras:
            return False, None, len(referencia)

        _med, _mad, _q1, _q3, limite = self._estadisticas(referencia)
        return bool(valor > limite), limite, len(referencia)

    def _evaluar_sucursal_horario(
        self,
        consumo: ConsumoAgua,
        valor: float,
    ) -> tuple[bool, float | None, int]:
        if not np.isfinite(valor) or valor <= 0:
            return False, None, 0

        clave = (
            int(consumo.idVBox),
            normalizar_grupo(consumo.Sucursal),
            int(consumo.hora),
        )
        perfil = self.perfiles_sucursal_horarios.get(clave)
        if perfil is None:
            return False, None, 0

        fecha_actual = _fecha_iso(consumo.Fecha)
        referencia = np.asarray(
            [
                v
                for v, fecha in perfil.observaciones
                if fecha != fecha_actual
            ],
            dtype=float,
        )
        referencia = referencia[np.isfinite(referencia)]

        if len(referencia) < self.min_muestras:
            return False, None, len(referencia)

        _med, _mad, _q1, _q3, limite = self._estadisticas(referencia)
        return bool(valor > limite), limite, len(referencia)

    def _valor_sucursal_diario_actual(self, consumo: ConsumoAgua) -> float:
        clave = (int(consumo.idVBox), normalizar_grupo(consumo.Sucursal))
        perfil = self.perfiles_sucursal_diarios.get(clave)
        if perfil is None:
            return 0.0
        fecha_actual = _fecha_iso(consumo.Fecha)
        for valor, fecha in perfil.observaciones:
            if fecha == fecha_actual:
                return float(valor)
        return 0.0

    def _valor_sucursal_horario_actual(self, consumo: ConsumoAgua) -> float:
        clave = (
            int(consumo.idVBox),
            normalizar_grupo(consumo.Sucursal),
            int(consumo.hora),
        )
        perfil = self.perfiles_sucursal_horarios.get(clave)
        if perfil is None:
            return 0.0

        fecha_actual = _fecha_iso(consumo.Fecha)
        for valor, fecha in perfil.observaciones:
            if fecha == fecha_actual:
                return float(valor)

        return 0.0

    def evaluar(
        self,
        consumo: ConsumoAgua,
        valor_sucursal_diario: float = 0.0,
        valor_sucursal_horario: float = 0.0,
    ) -> ResultadoAtipicoPositivo:
        try:
            valor_diario = float(consumo.consumoTotal or 0)
        except (TypeError, ValueError, OverflowError):
            valor_diario = 0.0
        try:
            valor_horario = float(consumo.ConsumoHora or 0)
        except (TypeError, ValueError, OverflowError):
            valor_horario = 0.0

        diario = self._evaluar_diario(consumo)
        horario = self._evaluar_horario(consumo)

        (
            atipico_grupo_diario,
            limite_inferior_d,
            limite_d,
            n_d,
            pos_d,
            total_d,
            pct_d,
            direccion_d,
        ) = diario

        (
            atipico_grupo_horario,
            limite_h,
            n_h,
            pos_h,
            total_h,
            pct_h,
        ) = horario

        (
            atipico_sucursal_diario,
            limite_sd,
            _n_sd,
        ) = self._evaluar_sucursal_diario(
            consumo,
            float(valor_sucursal_diario or 0),
        )

        (
            atipico_sucursal_horario,
            limite_sh,
            _n_sh,
        ) = self._evaluar_sucursal_horario(
            consumo,
            float(valor_sucursal_horario or 0),
        )

        atipico_diario = bool(atipico_grupo_diario)
        atipico_horario = bool(
            atipico_grupo_horario
        )

        return ResultadoAtipicoPositivo(
            consumo=consumo,
            es_atipico=bool(atipico_diario or atipico_horario),
            atipico_diario=atipico_diario,
            atipico_horario=atipico_horario,
            valor_diario=valor_diario,
            valor_horario=valor_horario,
            limite_diario=limite_d,
            limite_horario=limite_h,
            n_diario=n_d,
            n_horario=n_h,
            posicion_diaria=pos_d,
            total_posiciones_diarias=total_d,
            percentil_diario=pct_d,
            posicion_horaria=pos_h,
            total_posiciones_horarias=total_h,
            percentil_horario=pct_h,
            direccion_diaria=direccion_d,
            limite_inferior_diario=limite_inferior_d,
            atipico_grupo_diario=bool(atipico_grupo_diario),
            atipico_grupo_horario=bool(atipico_grupo_horario),
            atipico_sucursal_diario=bool(atipico_sucursal_diario),
            atipico_sucursal_horario=bool(atipico_sucursal_horario),
            valor_sucursal_diario=float(valor_sucursal_diario or 0),
            valor_sucursal_horario=float(valor_sucursal_horario or 0),
            limite_sucursal_diario=limite_sd,
            limite_sucursal_horario=limite_sh,
        )

    def guardar(self, ruta: Path = MODEL_PATH) -> None:
        ruta.parent.mkdir(parents=True, exist_ok=True)
        with ruta.open("wb") as archivo:
            pickle.dump(self, archivo, protocol=pickle.HIGHEST_PROTOCOL)

    @classmethod
    def cargar(cls, ruta: Path = MODEL_PATH) -> "ModeloAtipicosPositivos":
        with ruta.open("rb") as archivo:
            modelo = pickle.load(archivo)
        if not isinstance(modelo, cls):
            raise TypeError("El PKL no contiene un ModeloAtipicosPositivos válido")
        if getattr(modelo, "version", None) != MODEL_VERSION:
            raise ValueError(
                "El PKL corresponde a una versión anterior del modelo. "
                "El cronjob lo reemplaza automáticamente durante el entrenamiento."
            )
        return modelo


def entrenar_y_guardar_modelo(
    df: pd.DataFrame,
    min_muestras: int = 7,
    ruta: Path = MODEL_PATH,
) -> ModeloAtipicosPositivos:
    """Entrena y guarda el PKL. Es la misma función usada por cronjob y CLI."""
    modelo = ModeloAtipicosPositivos(min_muestras=min_muestras).entrenar(df)
    modelo.guardar(ruta)
    return modelo


def es_evaluable_atipico(consumo: ConsumoAgua) -> bool:
    if es_medidor_virtual(consumo.Area):
        return False

    try:
        total = float(consumo.consumoTotal or 0)
    except (TypeError, ValueError, OverflowError):
        return False

    return np.isfinite(total) and total > 0


def es_candidato(consumo: ConsumoAgua) -> bool:
    if not es_evaluable_atipico(consumo):
        return False

    tag = str(consumo.tag or "").strip().upper()
    categoria = str(consumo.categoria or "").strip()

    return tag in {"", TAG_SIN_CATEGORIZAR} and categoria == ""


def generar_evidencia_atipicos_positivos(
    resultados: list[ResultadoAtipicoPositivo],
    modelo: ModeloAtipicosPositivos,
) -> None:
    """Genera mapas CSV auditables, sin crear docenas de gráficas automáticas."""
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    positivos_por_clave = {}

    for resultado in resultados:
        if not resultado.es_atipico:
            continue

        clave = clave_consumo(resultado.consumo)
        anterior = positivos_por_clave.get(clave)

        if anterior is None:
            positivos_por_clave[clave] = resultado
        elif resultado.atipico_diario and not anterior.atipico_diario:
            positivos_por_clave[clave] = resultado

    positivos = list(positivos_por_clave.values())

    detalle_path = REPORT_DIR / "detalle_atipicos_positivos.csv"
    columnas = [
        "idVBox", "Sucursal", "Area", "Fecha", "hora",
        "consumo_total", "consumo_hora",
        "atipico_diario", "atipico_horario",
        "atipico_grupo_diario", "atipico_grupo_horario",
        "atipico_sucursal_diario", "atipico_sucursal_horario",
        "consumo_sucursal_diario", "consumo_sucursal_horario",
        "direccion_diaria",
        "limite_inferior_diario", "limite_diario", "limite_horario",
        "limite_sucursal_diario", "limite_sucursal_horario",
        "n_referencia_diaria", "n_referencia_horaria",
        "posicion_diaria", "total_posiciones_diarias", "percentil_diario",
        "posicion_horaria", "total_posiciones_horarias", "percentil_horario",
    ]
    with detalle_path.open("w", newline="", encoding="utf-8-sig") as archivo:
        writer = csv.DictWriter(archivo, fieldnames=columnas)
        writer.writeheader()
        for r in positivos:
            c = r.consumo
            writer.writerow({
                "idVBox": c.idVBox,
                "Sucursal": c.Sucursal,
                "Area": c.Area,
                "Fecha": _fecha_iso(c.Fecha),
                "hora": c.hora,
                "consumo_total": r.valor_diario,
                "consumo_hora": r.valor_horario,
                "atipico_diario": r.atipico_diario,
                "atipico_horario": r.atipico_horario,
                "atipico_grupo_diario": r.atipico_grupo_diario,
                "atipico_grupo_horario": r.atipico_grupo_horario,
                "atipico_sucursal_diario": r.atipico_sucursal_diario,
                "atipico_sucursal_horario": r.atipico_sucursal_horario,
                "consumo_sucursal_diario": r.valor_sucursal_diario,
                "consumo_sucursal_horario": r.valor_sucursal_horario,
                "direccion_diaria": r.direccion_diaria,
                "limite_inferior_diario": r.limite_inferior_diario,
                "limite_diario": r.limite_diario,
                "limite_horario": r.limite_horario,
                "limite_sucursal_diario": r.limite_sucursal_diario,
                "limite_sucursal_horario": r.limite_sucursal_horario,
                "n_referencia_diaria": r.n_diario,
                "n_referencia_horaria": r.n_horario,
                "posicion_diaria": r.posicion_diaria,
                "total_posiciones_diarias": r.total_posiciones_diarias,
                "percentil_diario": r.percentil_diario,
                "posicion_horaria": r.posicion_horaria,
                "total_posiciones_horarias": r.total_posiciones_horarias,
                "percentil_horario": r.percentil_horario,
            })

    diario_path = REPORT_DIR / "mapeo_consumo_total_por_fecha.csv"
    with diario_path.open("w", newline="", encoding="utf-8-sig") as archivo:
        writer = csv.writer(archivo)
        writer.writerow(["idVBox", "Sucursal", "Area", "Fecha", "consumo_total"])
        for clave, perfil in sorted(modelo.perfiles_diarios.items()):
            for valor, fecha in perfil.observaciones:
                writer.writerow([clave[0], perfil.sucursal, perfil.area, fecha, valor])

    horario_path = REPORT_DIR / "mapeo_consumo_hora_por_fecha.csv"
    with horario_path.open("w", newline="", encoding="utf-8-sig") as archivo:
        writer = csv.writer(archivo)
        writer.writerow(["idVBox", "Sucursal", "Area", "Fecha", "hora", "consumo_hora"])
        for clave, perfil in sorted(modelo.perfiles_horarios.items()):
            for valor, fecha in sorted(
                perfil.observaciones,
                key=lambda x: x[1],
            ):
                writer.writerow([
                    clave[0],
                    perfil.sucursal,
                    perfil.area,
                    fecha,
                    perfil.hora,
                    valor,
                ])

    sucursal_diario_path = REPORT_DIR / "mapeo_sucursal_total_por_fecha.csv"
    with sucursal_diario_path.open(
        "w", newline="", encoding="utf-8-sig"
    ) as archivo:
        writer = csv.writer(archivo)
        writer.writerow(
            ["idVBox", "Sucursal", "Fecha", "consumo_sucursal"]
        )
        for _clave, perfil in sorted(
            modelo.perfiles_sucursal_diarios.items()
        ):
            for valor, fecha in perfil.observaciones:
                writer.writerow([
                    perfil.id_vbox,
                    perfil.sucursal,
                    fecha,
                    valor,
                ])

    sucursal_horario_path = REPORT_DIR / "mapeo_sucursal_hora_por_fecha.csv"
    with sucursal_horario_path.open(
        "w", newline="", encoding="utf-8-sig"
    ) as archivo:
        writer = csv.writer(archivo)
        writer.writerow(
            ["idVBox", "Sucursal", "Fecha", "hora", "consumo_sucursal"]
        )
        for _clave, perfil in sorted(
            modelo.perfiles_sucursal_horarios.items()
        ):
            for valor, fecha in sorted(
                perfil.observaciones,
                key=lambda x: x[1],
            ):
                writer.writerow([
                    perfil.id_vbox,
                    perfil.sucursal,
                    fecha,
                    perfil.hora,
                    valor,
                ])

    print(f"[ATÍPICO POSITIVO] Evidencia generada en {REPORT_DIR}")
    print(
        "[ATÍPICO POSITIVO] Mapas: "
        f"{diario_path.name} | {horario_path.name}"
    )


def aplicar_modelo_atipicos_positivos(
    consumos: Iterable[ConsumoAgua],
    registros_db: dict,
    modelo: ModeloAtipicosPositivos,
) -> tuple[dict[int, str], list[int], list[ResultadoAtipicoPositivo]]:
    """Evalúa todos los consumos positivos físicos; solo modifica tags de candidatos."""
    tags_positivos: dict[int, str] = {}
    ids_vacios: list[int] = []
    resultados: list[ResultadoAtipicoPositivo] = []

    for consumo in consumos:
        if not es_evaluable_atipico(consumo):
            continue

        candidato_tag = es_candidato(consumo)

        try:
            valor_sd = modelo._valor_sucursal_diario_actual(consumo)
            valor_sh = modelo._valor_sucursal_horario_actual(consumo)
        except (TypeError, ValueError, OverflowError):
            valor_sd = 0.0
            valor_sh = 0.0

        resultado = modelo.evaluar(
            consumo,
            valor_sucursal_diario=valor_sd,
            valor_sucursal_horario=valor_sh,
        )
        resultados.append(resultado)

        registro: ConsumoDB | None = registros_db.get(clave_fila(consumo))
        if not registro or registro.id is None:
            continue

        # Todos los consumos positivos físicos se evalúan y quedan en la
        # evidencia. Solo los candidatos finales pueden modificar su tag.
        if not candidato_tag:
            continue

        if resultado.atipico_diario:
            tags_positivos[registro.id] = TAG_ATIPICO_POSITIVO_DIA
            consumo.tag = TAG_ATIPICO_POSITIVO_DIA
        else:
            consumo.tag = ""
            ids_vacios.append(registro.id)

    positivos_unicos = set(tags_positivos)
    vacios_unicos = set(ids_vacios)
    conflicto = positivos_unicos & vacios_unicos

    # Un positivo nunca puede borrarse posteriormente como vacío.
    ids_vacios_finales = sorted(vacios_unicos - positivos_unicos)

    print(
        "[ATÍPICO POSITIVO] IDs positivos únicos: "
        f"{len(positivos_unicos)}"
    )
    print(
        "[ATÍPICO POSITIVO] DÍA: "
        f"{sum(tag == TAG_ATIPICO_POSITIVO_DIA for tag in tags_positivos.values())}"
    )
    print(
        "[ATÍPICO POSITIVO] IDs presentes en positivos Y vacíos antes de depurar: "
        f"{len(conflicto)}"
    )
    print(
        "[ATÍPICO POSITIVO] IDs vacíos finales: "
        f"{len(ids_vacios_finales)}"
    )

#    generar_evidencia_atipicos_positivos(resultados, modelo)
    return tags_positivos, ids_vacios_finales, resultados
