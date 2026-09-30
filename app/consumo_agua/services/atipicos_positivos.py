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

from ..models.consumo import ConsumoAgua, ConsumoDB
from ..utils.normalizacion import clave_consumo


ROOT_DIR = Path(__file__).resolve().parents[3]
DATA_DIR = ROOT_DIR / "data"
MODELS_DIR = DATA_DIR / "models"
MODEL_PATH = MODELS_DIR / "atipico_positivo.pkl"
REPORT_DIR = DATA_DIR / "reportes_atipicos"

TAG_ATIPICO_POSITIVO = "ATÍPICO POSITIVO"
TAG_SIN_CATEGORIZAR = "SIN CATEGORIZAR"
MODEL_VERSION = 3

# Diario: (consumo_total, fecha_iso)
ObservacionDiaria = tuple[float, str]
# Horario: (consumo_hora, hora)
ObservacionHoraria = tuple[float, int]


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
    fecha: str
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
        self.perfiles_horarios: dict[tuple[int, str, str, str], PerfilHorario] = {}
        self.entrenado_en: str | None = None
        self.version = MODEL_VERSION

    @staticmethod
    def _estadisticas(valores: np.ndarray) -> tuple[float, float, float, float, float]:
        valores = valores[np.isfinite(valores)]
        mediana = float(np.median(valores))
        q1 = float(np.quantile(valores, 0.25))
        q3 = float(np.quantile(valores, 0.75))
        iqr = q3 - q1
        mad = float(np.median(np.abs(valores - mediana)))
        sigma_robusta = 1.4826 * mad
        limite_mad = mediana + 3.5 * sigma_robusta
        limite_iqr = q3 + 1.5 * iqr
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
            trabajo["sucursal_norm"].ne("") & trabajo["area_norm"].ne("")
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
        clave: tuple[int, str, str, str],
        grupo: pd.DataFrame,
    ) -> PerfilHorario:
        # Un punto por hora para el mapa del día. Si MSSQL trae una fila
        # duplicada para la misma hora, no la contamos dos veces.
        por_hora = (
            grupo.sort_values(["hora"], kind="stable")
            .drop_duplicates(subset=["hora"], keep="first")
        )
        por_hora = por_hora[
            np.isfinite(por_hora["ConsumoHora"]) & (por_hora["ConsumoHora"] > 0)
        ]
        valores = por_hora["ConsumoHora"].to_numpy(dtype=float)
        mediana, mad, q1, q3, limite = cls._estadisticas(valores)
        obs = tuple(
            (float(f.ConsumoHora), int(f.hora))
            for f in por_hora.itertuples(index=False)
        )
        return PerfilHorario(
            id_vbox=clave[0], sucursal=clave[1], area=clave[2], fecha=clave[3],
            n=len(valores), mediana=mediana, mad=mad, q1=q1, q3=q3,
            limite_superior=limite, observaciones=obs,
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

        horarios: dict[tuple[int, str, str, str], PerfilHorario] = {}
        for raw_clave, grupo in trabajo.groupby(
            ["idVBox", "sucursal_norm", "area_norm", "fecha_dia"],
            sort=True,
            dropna=False,
        ):
            clave = (
                int(raw_clave[0]), str(raw_clave[1]), str(raw_clave[2]),
                _fecha_iso(raw_clave[3]),
            )
            validos = (
                grupo.sort_values(["hora"], kind="stable")
                .drop_duplicates(subset=["hora"], keep="first")
            )
            validos = validos[
                np.isfinite(validos["ConsumoHora"]) & (validos["ConsumoHora"] > 0)
            ]
            if len(validos) >= self.min_muestras:
                horarios[clave] = self._perfil_horario(clave, grupo)

        self.perfiles_diarios = diarios
        self.perfiles_horarios = horarios
        self.entrenado_en = datetime.now().isoformat(timespec="seconds")
        return self

    def _buscar_diario(self, consumo: ConsumoAgua) -> PerfilDiario | None:
        return self.perfiles_diarios.get(
            _clave_base(consumo.idVBox, consumo.Sucursal, consumo.Area)
        )

    def _buscar_horario(self, consumo: ConsumoAgua) -> PerfilHorario | None:
        base = _clave_base(consumo.idVBox, consumo.Sucursal, consumo.Area)
        return self.perfiles_horarios.get((*base, _fecha_iso(consumo.Fecha)))

    def _evaluar_diario(
        self, consumo: ConsumoAgua
    ) -> tuple[bool, float | None, int, int | None, int, float | None]:
        try:
            valor = float(consumo.consumoTotal)
        except (TypeError, ValueError, OverflowError):
            return False, None, 0, None, 0, None
        if not np.isfinite(valor) or valor <= 0:
            return False, None, 0, None, 0, None

        perfil = self._buscar_diario(consumo)
        if perfil is None:
            return False, None, 0, None, 0, None

        fecha_actual = _fecha_iso(consumo.Fecha)
        referencia = [v for v, fecha in perfil.observaciones if fecha != fecha_actual]
        referencia = np.asarray(referencia, dtype=float)
        referencia = referencia[np.isfinite(referencia)]
        referencia.sort()
        if len(referencia) < self.min_muestras:
            return False, None, len(referencia), None, 0, None

        _med, _mad, _q1, _q3, limite = self._estadisticas(referencia)
        posicion = int(np.searchsorted(referencia, valor, side="right")) + 1
        total = len(referencia) + 1
        percentil = 100.0 * posicion / total
        return bool(valor > limite), limite, len(referencia), posicion, total, percentil

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

        # Para evaluar una hora no la comparamos contra sí misma: usamos las
        # otras horas del mismo idVBox + Sucursal + Area + Fecha.
        referencia = [
            v for v, hora in perfil.observaciones if int(hora) != int(consumo.hora)
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
        return bool(valor > limite), limite, len(referencia), posicion, total, percentil

    def evaluar(self, consumo: ConsumoAgua) -> ResultadoAtipicoPositivo:
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

        atipico_diario, limite_d, n_d, pos_d, total_d, pct_d = diario
        atipico_horario, limite_h, n_h, pos_h, total_h, pct_h = horario

        return ResultadoAtipicoPositivo(
            consumo=consumo,
            es_atipico=bool(atipico_diario or atipico_horario),
            atipico_diario=bool(atipico_diario),
            atipico_horario=bool(atipico_horario),
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


def es_candidato(consumo: ConsumoAgua) -> bool:
    tag = str(consumo.tag or "").strip().upper()
    categoria = str(consumo.categoria or "").strip()
    return tag in {"", TAG_SIN_CATEGORIZAR} and categoria == ""


def generar_evidencia_atipicos_positivos(
    resultados: list[ResultadoAtipicoPositivo],
    modelo: ModeloAtipicosPositivos,
) -> None:
    """Genera mapas CSV auditables, sin crear docenas de gráficas automáticas."""
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    positivos = [r for r in resultados if r.es_atipico]

    detalle_path = REPORT_DIR / "detalle_atipicos_positivos.csv"
    columnas = [
        "idVBox", "Sucursal", "Area", "Fecha", "hora",
        "consumo_total", "consumo_hora",
        "atipico_diario", "atipico_horario",
        "limite_diario", "limite_horario",
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
                "limite_diario": r.limite_diario,
                "limite_horario": r.limite_horario,
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
            for valor, hora in sorted(perfil.observaciones, key=lambda x: x[1]):
                writer.writerow([
                    clave[0], perfil.sucursal, perfil.area, perfil.fecha, hora, valor
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
) -> tuple[list[int], list[int], list[ResultadoAtipicoPositivo]]:
    """Evalúa solo registros que terminaron el flujo normal sin clasificación."""
    ids_positivos: list[int] = []
    ids_vacios: list[int] = []
    resultados: list[ResultadoAtipicoPositivo] = []

    for consumo in consumos:
        if not es_candidato(consumo):
            continue

        resultado = modelo.evaluar(consumo)
        resultados.append(resultado)

        registro: ConsumoDB | None = registros_db.get(clave_consumo(consumo))
        if not registro or registro.id is None:
            continue

        if resultado.es_atipico:
            consumo.tag = TAG_ATIPICO_POSITIVO
            ids_positivos.append(registro.id)
        else:
            consumo.tag = ""
            ids_vacios.append(registro.id)

    generar_evidencia_atipicos_positivos(resultados, modelo)
    return ids_positivos, ids_vacios, resultados
