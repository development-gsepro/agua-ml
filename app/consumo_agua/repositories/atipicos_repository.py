from ...shared.database.databasePostgress import get_connection as get_conn


def actualizar_tags_atipicos_positivos(
    ids_positivos: list[int],
    ids_vacios: list[int],
) -> None:
    if not ids_positivos and not ids_vacios:
        return
    with get_conn() as conn:
        with conn.cursor() as cursor:
            if ids_positivos:
                cursor.execute(
                    """
                    UPDATE public.consumo_agua
                    SET tag = 'ATÍPICO POSITIVO'
                    WHERE id = ANY(%s)
                    """,
                    (ids_positivos,),
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
