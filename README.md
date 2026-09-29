# agua-ml

Clasificador modular de consumo de agua.

La lógica principal se encuentra en `app/consumo_agua/` y la infraestructura compartida en `app/shared/`.

## Ejecución

```bash
python cronjob.py
```

## Pruebas

```bash
python -m pytest -q
```

## Atípicos positivos y cache Parquet

El proyecto incluye un histórico incremental para evitar repetir consultas MSSQL y un modelo PKL de perfiles robustos para detectar `ATÍPICO POSITIVO` como etapa final.

Consulta `ATIPICOS_POSITIVOS.md` para el flujo, entrenamiento y archivos generados.
