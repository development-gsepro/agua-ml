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
                "ATÍPICO POSITIVO HORA",
                "ATÍPICO BAJO DÍA",
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
) -> None:
    """
    Guarda en PostgreSQL el resultado del modelo de atípicos:

    - atipico_dia
    - idoneo_dia
    - atipico_hora
    - idoneo_hora

    El valor idóneo corresponde a la mediana histórica del perfil
    diario u horario del mismo medidor/área.
    """

    valores = []

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

        valores.append(
            (
                bool(resultado.atipico_diario),
                idoneo_dia,
                bool(resultado.atipico_horario),
                idoneo_hora,
                int(registro.id),
            )
        )

    if not valores:
        print("[POSTGRES] Sin métricas de atípicos para actualizar.")
        return

    query = """
        UPDATE public.consumo_agua
        SET
            atipico_dia = %s,
            idoneo_dia = %s,
            atipico_hora = %s,
            idoneo_hora = %s
        WHERE id = %s
    """

    with get_conn() as conn:
        with conn.cursor() as cursor:
            cursor.executemany(
                query,
                valores,
            )

        conn.commit()

    print(
        f"[POSTGRES] Métricas de atípicos actualizadas: "
        f"{len(valores)} registros"
    )
