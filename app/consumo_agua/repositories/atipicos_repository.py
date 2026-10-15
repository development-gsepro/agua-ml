from ...shared.database.databasePostgress import get_connection as get_conn


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
