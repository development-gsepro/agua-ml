import time

from psycopg2.extras import execute_values
from ...shared.database.databasePostgress import get_connection as get_conn
from ..utils.normalizacion import clave_consumo

def actualizar_tags_atipicos_positivos(
    tags_positivos: dict[int, str],
    ids_vacios: list[int],
) -> None:
    if not tags_positivos and not ids_vacios:
        return

    positivos = set(tags_positivos)

    # Protección adicional:
    # ningún positivo puede limpiarse posteriormente.
    ids_vacios = sorted(set(ids_vacios) - positivos)

    with get_conn() as conn:
        with conn.cursor() as cursor:
            for tag in (
                "ATÍPICO POSITIVO DÍA",
            ):
                ids_tag = [
                    id_registro
                    for id_registro, etiqueta in tags_positivos.items()
                    if etiqueta == tag
                ]

                if ids_tag:
                    cursor.execute(
                        """
                        UPDATE public.consumo_agua
                        SET tag = %s
                        WHERE id = ANY(%s)
                        """,
                        (tag, ids_tag),
                    )

            if ids_vacios:
                cursor.execute(
                    """
                    UPDATE public.consumo_agua
                    SET tag = ''
                    WHERE id = ANY(%s)
                    """,
                    (ids_vacios,),
                )

        conn.commit()

def actualizar_metricas_atipicos(
    resultados,
    registros_db: dict,
    modelo,
    batch_size: int = 5000,
) -> None:
    """
    Guarda por lotes las métricas calculadas por el modelo:

    - atipico_dia
    - idoneo_dia
    - atipico_hora
    - idoneo_hora
    """

    inicio = time.perf_counter()

    por_id = {}

    for resultado in resultados:
        consumo = resultado.consumo

        registro = registros_db.get(
            clave_consumo(consumo)
        )

        if registro is None or registro.id is None:
            continue

        perfil_dia = modelo._buscar_diario(consumo)
        perfil_hora = modelo._buscar_horario(consumo)

        idoneo_dia = (
            float(perfil_dia.mediana)
            if perfil_dia is not None
            else None
        )

        idoneo_hora = (
            float(perfil_hora.mediana)
            if perfil_hora is not None
            else None
        )

        id_registro = int(registro.id)

        nuevo = (
            bool(resultado.atipico_diario),
            idoneo_dia,
            bool(resultado.atipico_horario),
            idoneo_hora,
            id_registro,
        )

        anterior = por_id.get(id_registro)

        if anterior is None:
            por_id[id_registro] = nuevo
            continue

        por_id[id_registro] = (
            bool(anterior[0] or nuevo[0]),
            nuevo[1] if nuevo[1] is not None else anterior[1],
            bool(anterior[2] or nuevo[2]),
            nuevo[3] if nuevo[3] is not None else anterior[3],
            id_registro,
        )

    valores = list(por_id.values())

    if not valores:
        print("[POSTGRES] Sin métricas de atípicos para actualizar.")
        return

    total = len(valores)

    print(
        f"[POSTGRES] Métricas únicas a actualizar: {total}"
    )

    query = """
        UPDATE public.consumo_agua AS c
        SET
            atipico_dia = v.atipico_dia,
            idoneo_dia = v.idoneo_dia,
            atipico_hora = v.atipico_hora,
            idoneo_hora = v.idoneo_hora
        FROM (VALUES %s) AS v(
            atipico_dia,
            idoneo_dia,
            atipico_hora,
            idoneo_hora,
            id
        )
        WHERE c.id = v.id
    """

    actualizados = 0

    with get_conn() as conn:
        with conn.cursor() as cursor:

            for inicio_lote in range(
                0,
                total,
                batch_size,
            ):
                lote = valores[
                    inicio_lote:
                    inicio_lote + batch_size
                ]

                execute_values(
                    cursor,
                    query,
                    lote,
                    template="(%s, %s, %s, %s, %s)",
                    page_size=batch_size,
                )

                conn.commit()

                actualizados += len(lote)

                print(
                    f"[POSTGRES] Métricas: "
                    f"{actualizados}/{total}"
                )

    segundos = time.perf_counter() - inicio

    print(
        f"[POSTGRES] Métricas de atípicos actualizadas: "
        f"{actualizados} registros ({segundos:.2f}s)"
    )
