# Atípicos positivos de consumo de agua

## Objetivo

Agregar una clasificación final `ATÍPICO POSITIVO` sin alterar las reglas principales existentes. El detector solo se ejecuta después de `ATÍPICO/OFFLINE`, los tags principales y `SIN CATEGORIZAR`.

## Flujo implementado

1. Los consumos solicitados se buscan primero en `data/consumo_agua_historico.parquet`.
2. Solo las fechas faltantes se consultan a MSSQL y se agregan al Parquet.
3. Se ejecuta el clasificador actual sin cambios de reglas.
4. Se marca `SIN CATEGORIZAR` como hasta ahora.
5. Si existe `data/models/atipico_positivo.pkl`, únicamente los registros con tag vacío o `SIN CATEGORIZAR` y categoría vacía pasan al detector.
6. Si el consumo horario supera el límite robusto de su perfil, el tag cambia a `ATÍPICO POSITIVO`.
7. Si no cumple, el tag se deja vacío.

Si el PKL aún no existe, el clasificador conserva `SIN CATEGORIZAR` y no modifica esos registros.

## Mapeo del modelo

El PKL guarda perfiles históricos por:

- `idVBox`
- `Area` normalizada
- `hora`
- tipo de día (`ENTRE_SEMANA` / `FIN_SEMANA`)

Cuando no hay suficientes muestras para separar tipo de día, usa un perfil de respaldo por `idVBox + Area + hora`.

Cada perfil almacena:

- número de muestras
- mediana
- MAD
- Q1
- Q3
- límite superior robusto

El límite se calcula como el máximo entre:

- `mediana + 3.5 * (1.4826 * MAD)`
- `Q3 + 1.5 * IQR`

Solo se consideran consumos horarios mayores a 0.

## Archivos generados localmente

```text
data/
├── consumo_agua_historico.parquet
├── consumo_agua_historico.meta.json
└── models/
    └── atipico_positivo.pkl
```

Estos archivos están excluidos de Git porque son datos/modelos generados localmente.

## Primer uso

### 1. Instalar dependencias

```bash
pip install -r requirements.txt
```

### 2. Llenar el histórico

Ejecutar normalmente el clasificador/cronjob. La primera corrida consulta MSSQL y genera el Parquet. Las siguientes reutilizan las fechas ya guardadas.

```bash
python cronjob.py
```

### 3. Entrenar el PKL

```bash
python train_atipicos_positivos.py
```

Por defecto exige al menos 7 observaciones por perfil. Puede ajustarse:

```bash
python train_atipicos_positivos.py --min-muestras 10
```

### 4. Volver a ejecutar el clasificador

Con el PKL existente, el paso final evalúa los candidatos y actualiza los tags.

```bash
python cronjob.py
```

## Seguridad del flujo

El detector no cambia registros que ya tengan un tag principal ni registros con categoría definida. Si no hay suficiente histórico para un perfil, no marca el consumo como atípico positivo.
