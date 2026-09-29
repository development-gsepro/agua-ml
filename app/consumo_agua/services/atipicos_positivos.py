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
MODEL_VERSION = 2

# (valor, fecha_iso, hora, idVBox, sucursal, area)
ObservacionHistorica = tuple[float, str, int, int, str, str]


def normalizar_grupo(valor: object) -> str:
    texto = str(valor or "").strip().upper()
    return re.sub(r"\s+", " ", texto)


@dataclass(slots=True)
class PerfilAtipicoPositivo:
    ciudad: str
    cliente: str
    n: int
    mediana: float
    mad: float
    q1: float
    q3: float
    limite_superior: float
    valores_ordenados: tuple[float, ...]
    observaciones_ordenadas: tuple[ObservacionHistorica, ...]


@dataclass(slots=True)
class ResultadoAtipicoPositivo:
    consumo: ConsumoAgua
    es_atipico: bool
    limite: float | None
    valor: float
    n_historico: int
    perfil_usado: str | None
    ciudad: str = ""
    cliente: str = ""
    posicion: int | None = None
    total_posiciones: int = 0
    percentil: float | None = None
    mediana: float | None = None
    q1: float | None = None
    q3: float | None = None


class ModeloAtipicosPositivos:
    """Modelo robusto agrupado EXCLUSIVAMENTE por Ciudad + Cliente.

    Cada perfil conserva los consumos históricos ordenados y su procedencia
    para poder explicar:
      1) respecto a qué datos se comparó un consumo;
      2) qué posición ocupa dentro de esos datos.

    El modelo se aplica únicamente al final del flujo, sobre registros que
    quedaron sin clasificación principal.
    """

    def __init__(self, min_muestras: int = 7):
        self.min_muestras = int(min_muestras)
        self.perfiles: dict[tuple[str, str], PerfilAtipicoPositivo] = {}
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
            "Ciudad", "Cliente", "ConsumoHora", "Fecha",
            "hora", "idVBox", "Sucursal", "Area",
        }
        faltantes = requerido - set(df.columns)
        if faltantes:
            raise ValueError(
                "Faltan columnas para entrenar atípicos positivos por "
                "Ciudad + Cliente: " + ", ".join(sorted(faltantes))
            )

        trabajo = df.copy()
        trabajo["Fecha"] = pd.to_datetime(trabajo["Fecha"], errors="coerce")
        trabajo["ConsumoHora"] = pd.to_numeric(
            trabajo["ConsumoHora"], errors="coerce"
        )
        trabajo["hora"] = pd.to_numeric(trabajo["hora"], errors="coerce")
        trabajo["idVBox"] = pd.to_numeric(trabajo["idVBox"], errors="coerce")

        trabajo = trabajo.dropna(
            subset=["Fecha", "hora", "idVBox", "ConsumoHora"]
        )
        trabajo = trabajo[trabajo["ConsumoHora"] > 0]

        if "tag" in trabajo.columns:
            tags = (
                trabajo["tag"].fillna("").astype(str).str.strip().str.upper()
            )
            trabajo = trabajo[tags.isin(["", TAG_SIN_CATEGORIZAR])]

        if "categoria" in trabajo.columns:
            categorias = (
                trabajo["categoria"].fillna("").astype(str).str.strip()
            )
            trabajo = trabajo[categorias.eq("")]

        trabajo["ciudad_norm"] = trabajo["Ciudad"].map(normalizar_grupo)
        trabajo["cliente_norm"] = trabajo["Cliente"].map(normalizar_grupo)
        trabajo = trabajo[
            trabajo["ciudad_norm"].ne("")
            & trabajo["cliente_norm"].ne("")
        ]

        trabajo["hora"] = trabajo["hora"].astype(int)
        trabajo["idVBox"] = trabajo["idVBox"].astype(int)
        return trabajo

    @classmethod
    def _crear_perfil(
        cls,
        ciudad: str,
        cliente: str,
        grupo: pd.DataFrame,
    ) -> PerfilAtipicoPositivo:
        ordenado = grupo.sort_values(
            ["ConsumoHora", "Fecha", "hora", "idVBox"],
            kind="stable",
        )
        valores = ordenado["ConsumoHora"].to_numpy(dtype=float)
        mediana, mad, q1, q3, limite = cls._estadisticas(valores)

        observaciones: list[ObservacionHistorica] = []
        for fila in ordenado.itertuples(index=False):
            fecha = pd.Timestamp(fila.Fecha).isoformat()
            observaciones.append(
                (
                    float(fila.ConsumoHora),
                    fecha,
                    int(fila.hora),
                    int(fila.idVBox),
                    str(fila.Sucursal or ""),
                    str(fila.Area or ""),
                )
            )

        return PerfilAtipicoPositivo(
            ciudad=ciudad,
            cliente=cliente,
            n=len(valores),
            mediana=mediana,
            mad=mad,
            q1=q1,
            q3=q3,
            limite_superior=limite,
            valores_ordenados=tuple(float(v) for v in valores),
            observaciones_ordenadas=tuple(observaciones),
        )

    def entrenar(self, df: pd.DataFrame) -> "ModeloAtipicosPositivos":
        trabajo = self._normalizar_df(df)
        perfiles: dict[tuple[str, str], PerfilAtipicoPositivo] = {}

        # Regla solicitada: el mapeo se construye por Ciudad + Cliente.
        for (ciudad, cliente), grupo in trabajo.groupby(
            ["ciudad_norm", "cliente_norm"],
            dropna=False,
            sort=True,
        ):
            if len(grupo) < self.min_muestras:
                continue
            perfiles[(ciudad, cliente)] = self._crear_perfil(
                ciudad,
                cliente,
                grupo,
            )

        self.perfiles = perfiles
        self.entrenado_en = datetime.now().isoformat(timespec="seconds")
        return self

    def _buscar_perfil(
        self,
        consumo: ConsumoAgua,
    ) -> PerfilAtipicoPositivo | None:
        ciudad = normalizar_grupo(consumo.Ciudad)
        cliente = normalizar_grupo(consumo.Cliente)
        if not ciudad or not cliente:
            return None
        return self.perfiles.get((ciudad, cliente))

    @staticmethod
    def _misma_observacion(
        obs: ObservacionHistorica,
        consumo: ConsumoAgua,
        valor: float,
    ) -> bool:
        """Identifica la misma fila para evitar compararla contra sí misma."""
        obs_valor, obs_fecha, obs_hora, obs_id, obs_sucursal, obs_area = obs
        try:
            fecha_consumo = pd.Timestamp(consumo.Fecha).isoformat()
        except Exception:
            fecha_consumo = str(consumo.Fecha)

        return (
            int(obs_id) == int(consumo.idVBox)
            and int(obs_hora) == int(consumo.hora)
            and str(obs_fecha) == fecha_consumo
            and normalizar_grupo(obs_sucursal)
            == normalizar_grupo(consumo.Sucursal)
            and normalizar_grupo(obs_area)
            == normalizar_grupo(consumo.Area)
            and bool(np.isclose(float(obs_valor), valor))
        )

    def _referencia_sin_actual(
        self,
        perfil: PerfilAtipicoPositivo,
        consumo: ConsumoAgua,
        valor: float,
    ) -> np.ndarray:
        """Devuelve el grupo Ciudad + Cliente excluyendo una copia del actual.

        Si el registro evaluado no está en el histórico (por ejemplo, un dato
        futuro), se conserva todo el perfil.
        """
        observaciones = list(perfil.observaciones_ordenadas)
        indice_actual = None

        for i, obs in enumerate(observaciones):
            if self._misma_observacion(obs, consumo, valor):
                indice_actual = i
                break

        if indice_actual is not None:
            observaciones.pop(indice_actual)

        return np.asarray(
            [float(obs[0]) for obs in observaciones],
            dtype=float,
        )

    def evaluar(self, consumo: ConsumoAgua) -> ResultadoAtipicoPositivo:
        try:
            valor = float(consumo.ConsumoHora)
        except (TypeError, ValueError, OverflowError):
            valor = 0.0

        ciudad = normalizar_grupo(consumo.Ciudad)
        cliente = normalizar_grupo(consumo.Cliente)

        if not np.isfinite(valor) or valor <= 0:
            return ResultadoAtipicoPositivo(
                consumo, False, None, valor, 0, None,
                ciudad=ciudad, cliente=cliente,
            )

        perfil = self._buscar_perfil(consumo)
        if perfil is None:
            return ResultadoAtipicoPositivo(
                consumo, False, None, valor, 0, None,
                ciudad=ciudad, cliente=cliente,
            )

        # Para una evaluación auditable, el registro actual no puede formar
        # parte del conjunto contra el que se compara.
        referencia = self._referencia_sin_actual(
            perfil,
            consumo,
            valor,
        )
        referencia = referencia[np.isfinite(referencia)]

        if len(referencia) < self.min_muestras:
            return ResultadoAtipicoPositivo(
                consumo, False, None, valor, len(referencia), None,
                ciudad=perfil.ciudad,
                cliente=perfil.cliente,
            )

        mediana, _mad, q1, q3, limite = self._estadisticas(referencia)

        # Posición del dato evaluado si se insertara en la referencia ordenada.
        # El total incluye N datos de referencia + el dato actual.
        posicion = int(
            np.searchsorted(referencia, valor, side="right")
        ) + 1
        total_posiciones = len(referencia) + 1
        percentil = 100.0 * posicion / total_posiciones

        es_atipico = valor > limite

        return ResultadoAtipicoPositivo(
            consumo=consumo,
            es_atipico=bool(es_atipico),
            limite=limite,
            valor=valor,
            n_historico=len(referencia),
            perfil_usado="CIUDAD_CLIENTE",
            ciudad=perfil.ciudad,
            cliente=perfil.cliente,
            posicion=posicion,
            total_posiciones=total_posiciones,
            percentil=percentil,
            mediana=mediana,
            q1=q1,
            q3=q3,
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
            raise TypeError(
                "El PKL no contiene un ModeloAtipicosPositivos válido"
            )

        if getattr(modelo, "version", None) != MODEL_VERSION:
            raise ValueError(
                "El PKL corresponde a una versión anterior del modelo. "
                "Elimina data/models/atipico_positivo.pkl y vuelve a ejecutar "
                "python train_atipicos_positivos.py"
            )

        return modelo


def es_candidato(consumo: ConsumoAgua) -> bool:
    tag = str(consumo.tag or "").strip().upper()
    categoria = str(consumo.categoria or "").strip()
    return tag in {"", TAG_SIN_CATEGORIZAR} and categoria == ""


def _nombre_archivo(texto: str) -> str:
    limpio = re.sub(r"[^A-Z0-9]+", "_", normalizar_grupo(texto))
    return limpio.strip("_")[:80] or "SIN_DATO"


def generar_evidencia_atipicos_positivos(
    resultados: list[ResultadoAtipicoPositivo],
    modelo: ModeloAtipicosPositivos,
    max_graficas: int = 12,
) -> None:
    """Genera evidencia auditable del mapeo Ciudad + Cliente.

    Archivos:
      - detalle_atipicos_positivos.csv
      - resumen_ciudad_cliente.csv
      - mapeo_historico_ciudad_cliente.csv
      - reporte_funcionamiento.pdf
      - graficas/*.png
    """
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    graficas_dir = REPORT_DIR / "graficas"
    graficas_dir.mkdir(parents=True, exist_ok=True)

    positivos = [r for r in resultados if r.es_atipico]

    # 1) Detalle de cada atípico positivo.
    detalle_path = REPORT_DIR / "detalle_atipicos_positivos.csv"
    columnas_detalle = [
        "Ciudad", "Cliente", "idVBox", "Sucursal", "Area",
        "Fecha", "hora", "ConsumoHora", "n_referencia",
        "posicion", "total_posiciones", "percentil",
        "mediana", "q1", "q3", "limite_superior", "exceso",
    ]
    with detalle_path.open("w", newline="", encoding="utf-8-sig") as archivo:
        writer = csv.DictWriter(archivo, fieldnames=columnas_detalle)
        writer.writeheader()
        for r in positivos:
            c = r.consumo
            writer.writerow(
                {
                    "Ciudad": r.ciudad,
                    "Cliente": r.cliente,
                    "idVBox": c.idVBox,
                    "Sucursal": c.Sucursal,
                    "Area": c.Area,
                    "Fecha": str(c.Fecha),
                    "hora": c.hora,
                    "ConsumoHora": r.valor,
                    "n_referencia": r.n_historico,
                    "posicion": r.posicion,
                    "total_posiciones": r.total_posiciones,
                    "percentil": round(r.percentil or 0.0, 4),
                    "mediana": r.mediana,
                    "q1": r.q1,
                    "q3": r.q3,
                    "limite_superior": r.limite,
                    "exceso": (
                        r.valor - r.limite
                        if r.limite is not None
                        else None
                    ),
                }
            )

    # 2) Mapeo completo: qué datos forman cada Ciudad + Cliente y qué
    # posición ocupa cada observación histórica.
    mapeo_path = REPORT_DIR / "mapeo_historico_ciudad_cliente.csv"
    with mapeo_path.open("w", newline="", encoding="utf-8-sig") as archivo:
        writer = csv.writer(archivo)
        writer.writerow(
            [
                "Ciudad", "Cliente", "posicion", "ConsumoHora",
                "Fecha", "hora", "idVBox", "Sucursal", "Area",
            ]
        )
        for (ciudad, cliente), perfil in sorted(modelo.perfiles.items()):
            for posicion, obs in enumerate(
                perfil.observaciones_ordenadas, start=1
            ):
                valor, fecha, hora, id_vbox, sucursal, area = obs
                writer.writerow(
                    [
                        ciudad, cliente, posicion, valor,
                        fecha, hora, id_vbox, sucursal, area,
                    ]
                )

    # 3) Resumen por Ciudad + Cliente.
    filas_resumen = []
    for (ciudad, cliente), perfil in sorted(modelo.perfiles.items()):
        candidatos = [
            r for r in resultados
            if r.ciudad == ciudad and r.cliente == cliente
        ]
        positivos_grupo = [r for r in candidatos if r.es_atipico]
        filas_resumen.append(
            {
                "Ciudad": ciudad,
                "Cliente": cliente,
                "n_historico": perfil.n,
                "candidatos_evaluados": len(candidatos),
                "atipicos_positivos": len(positivos_grupo),
                "mediana": perfil.mediana,
                "q1": perfil.q1,
                "q3": perfil.q3,
                "limite_superior": perfil.limite_superior,
            }
        )

    resumen_path = REPORT_DIR / "resumen_ciudad_cliente.csv"
    pd.DataFrame(filas_resumen).to_csv(
        resumen_path,
        index=False,
        encoding="utf-8-sig",
    )

    # 4) PDF + gráficas para los casos más alejados del límite.
    try:
        import matplotlib.pyplot as plt
        from matplotlib.backends.backend_pdf import PdfPages
    except ImportError:
        print(
            "[ATÍPICO POSITIVO] Evidencia CSV generada. "
            "Instala matplotlib para generar PDF y gráficas."
        )
        return

    pdf_path = REPORT_DIR / "reporte_funcionamiento.pdf"

    ordenados = sorted(
        positivos,
        key=lambda r: (
            r.valor / r.limite
            if r.limite and r.limite > 0
            else r.valor
        ),
        reverse=True,
    )

    with PdfPages(pdf_path) as pdf:
        fig = plt.figure(figsize=(11, 8.5))
        fig.text(
            0.07, 0.93,
            "Reporte de funcionamiento - ATÍPICO POSITIVO",
            fontsize=18,
        )
        texto = (
            f"Modelo: agrupación por Ciudad + Cliente\n"
            f"Versión: {modelo.version}\n"
            f"Entrenado en: {modelo.entrenado_en}\n\n"
            f"Perfiles Ciudad + Cliente: {len(modelo.perfiles)}\n"
            f"Candidatos finales evaluados: {len(resultados)}\n"
            f"Atípicos positivos: {len(positivos)}\n\n"
            "Criterio:\n"
            "Cada consumo se compara exclusivamente con los consumos "
            "históricos positivos de su misma Ciudad + Cliente.\n"
            "El límite superior es el mayor entre:\n"
            "  - mediana + 3.5 * (1.4826 * MAD)\n"
            "  - Q3 + 1.5 * IQR\n\n"
            "Para cada atípico se conserva su posición dentro del "
            "mapeo ordenado y el percentil correspondiente.\n\n"
            "Archivos de soporte:\n"
            "  - detalle_atipicos_positivos.csv\n"
            "  - resumen_ciudad_cliente.csv\n"
            "  - mapeo_historico_ciudad_cliente.csv"
        )
        fig.text(0.07, 0.84, texto, va="top", fontsize=11)
        plt.axis("off")
        pdf.savefig(fig, bbox_inches="tight")
        plt.close(fig)

        for indice, r in enumerate(ordenados[:max_graficas], start=1):
            perfil = modelo.perfiles.get((r.ciudad, r.cliente))
            if not perfil:
                continue

            valores = np.asarray(perfil.valores_ordenados, dtype=float)
            posiciones = np.arange(1, len(valores) + 1)

            fig, ax = plt.subplots(figsize=(11, 6))
            ax.plot(posiciones, valores, linewidth=1.2)
            ax.axhline(
                r.limite if r.limite is not None else perfil.limite_superior,
                linestyle="--",
                label=(
                    f"Límite = "
                    f"{(r.limite if r.limite is not None else perfil.limite_superior):.2f}"
                ),
            )
            ax.scatter(
                [r.posicion],
                [r.valor],
                s=80,
                zorder=5,
                label=(
                    f"Atípico: {r.valor:.2f} "
                    f"(posición {r.posicion}/{r.total_posiciones})"
                ),
            )
            ax.set_title(
                f"{r.ciudad} | {r.cliente}\n"
                f"idVBox {r.consumo.idVBox} - "
                f"{r.consumo.Sucursal} - {r.consumo.Area}"
            )
            ax.set_xlabel("Posición dentro del grupo Ciudad + Cliente")
            ax.set_ylabel("ConsumoHora")
            ax.legend()
            ax.grid(alpha=0.2)

            nota = (
                f"Percentil: {r.percentil:.2f}% | "
                f"Mediana: {perfil.mediana:.2f} | "
                f"Q1: {perfil.q1:.2f} | Q3: {perfil.q3:.2f}"
            )
            fig.text(0.5, 0.01, nota, ha="center", fontsize=9)
            fig.tight_layout(rect=[0, 0.04, 1, 1])

            nombre = (
                f"{indice:02d}_"
                f"{_nombre_archivo(r.ciudad)}_"
                f"{_nombre_archivo(r.cliente)}_"
                f"{r.consumo.idVBox}.png"
            )
            fig.savefig(graficas_dir / nombre, dpi=160, bbox_inches="tight")
            pdf.savefig(fig, bbox_inches="tight")
            plt.close(fig)

    print(
        f"[ATÍPICO POSITIVO] Evidencia generada en {REPORT_DIR}"
    )
    print(
        f"[ATÍPICO POSITIVO] Reporte PDF: {pdf_path.name} | "
        f"gráficas: {min(len(positivos), max_graficas)}"
    )


def aplicar_modelo_atipicos_positivos(
    consumos: Iterable[ConsumoAgua],
    registros_db: dict,
    modelo: ModeloAtipicosPositivos,
) -> tuple[list[int], list[int], list[ResultadoAtipicoPositivo]]:
    """Evalúa únicamente los candidatos finales.

    Si cumple el criterio, se marca ATÍPICO POSITIVO.
    Si no cumple, el tag queda vacío.
    También genera evidencia auditable del mapeo Ciudad + Cliente.
    """
    ids_positivos: list[int] = []
    ids_vacios: list[int] = []
    resultados: list[ResultadoAtipicoPositivo] = []

    for consumo in consumos:
        if not es_candidato(consumo):
            continue

        resultado = modelo.evaluar(consumo)
        resultados.append(resultado)

        registro: ConsumoDB | None = registros_db.get(
            clave_consumo(consumo)
        )
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
