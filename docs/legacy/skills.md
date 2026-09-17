# Skills y workers temporales

## Idea sencilla

- Una **skill** es una capacidad reutilizable y versionada.
- Una **tarea** indica qué skills necesita y qué herramientas permite.
- Un **worker temporal** es el contenedor que ejecuta esas skills.
- El **policy pack** calcula autorizaciones a partir del riesgo clasificado y
  de hechos técnicos de las skills.
- El **Gate** recalcula la autorización antes de crear el worker.

El flujo es:

```text
Case no confiable
        ↓
RiskClassifier único → RiskAssessment (clasifica; no autoriza)
        ↓
Policy pack + catálogo factual → autorizaciones por skill
        ↓
Estado + contratos → acciones técnicamente disponibles
        ↓
LLM selecciona una acción disponible
        ↓
Gate recalcula desde su RiskAssessment
        ↓
Orquestador resuelve las skills del catálogo
        ↓
Crea SkillWorker(skills)
        ↓
Ejecuta, registra resultados y descarta el worker
```

`risk_metadata` describe acceso a datos, efectos locales o externos y
reversibilidad; nunca contiene `allow`, `needs_approval` ni `blocked`. Esos
veredictos pertenecen al policy pack. `SkillContract` declara por separado los
requisitos técnicos, la aplicabilidad, los productos, las mutaciones, las
invalidaciones y la repetición. El LLM solo recibe las acciones que el código
ha calculado como disponibles; después el Gate autoriza, pregunta o bloquea.

El Gate no confía en la autorización copiada dentro de una tarea. La reproduce
desde el `RiskAssessment` con el que fue construido y usa
`self.assessment.risk_level` como única fuente, por lo que `case.risk_level`
—declaración no autoritativa del usuario— no se usa como nivel del Gate ni puede
conceder o rebajar permisos. Solo una discrepancia
material con el riesgo clasificado endurece el flujo al exigir revisión humana.
La confianza del LLM tampoco concede permisos: una confianza baja o una marca
de revisión humana pueden elevar `allow` a `needs_approval`, mientras que una
confianza alta no evita las reglas.

## Añadir una skill nueva

1. Crear una clase que cumpla el protocolo `Skill` de `skills/base.py`.
2. Declarar `skill_id`, `version`, `required_tools`, `produces`,
   `description` y `risk_metadata` (un `SkillRiskMetadata`: hechos técnicos
   —acceso a datos, efectos locales, efectos externos, reversibilidad—,
   nunca permisos).
3. Implementar `execute()` y devolver un `SkillResult`.
4. Registrarla en `default_skill_registry()`.
5. Asociarle un `SkillContract` pequeño en `skills/registry.py`.
6. Implementar sus condiciones técnicas y de aplicabilidad en el cálculo
   determinista de acciones disponibles.
7. Añadir las reglas necesarias al policy pack.
8. Probar resultado, permisos, artefactos y traza.

El worker genérico no se modifica al añadir nuevas capacidades.

## Validación y perfilado de datos

El pipeline separa dos responsabilidades que no deben confundirse:

- `validate_data` es el filtro estructural obligatorio. Comprueba que el CSV
  pueda cargarse, contenga filas, tenga cabeceras completas y únicas, incluya
  features, target y atributos sensibles, y que el target no sea una feature.
  Produce `data_validation.json` con el hash del dataset validado.
- `profile_data` es una capacidad descriptiva seleccionable. Solo puede
  ejecutarse tras una validación satisfactoria y genera `data_profile.json`
  con tipos, estadísticos, distribuciones, nulos, unicidad, duplicados,
  outliers IQR, desbalanceo, frecuencias, correlaciones y posibles
  indicadores de fuga.

El perfilado no modifica el dataset ni decide eliminar filas o variables. Sus
alertas son evidencia para revisión humana. Antes de perfilar, compara el hash
actual del CSV con el registrado durante la validación para asegurar que ambos
pasos describen exactamente la misma entrada.

### Alcance: el caso no tiene por qué declarar todo el CSV

La validación exige `declaradas ⊆ columnas del archivo`, **nunca igualdad**:
un CSV de 10 columnas puede trabajarse con un caso que declare 5. Lo que sí
se registra es cuáles quedan fuera (`data_validation.json`
→ `undeclared_columns`).

El perfilado se acota a las columnas declaradas (features + target +
sensibles). Perfilar el archivo entero producía evidencia engañosa: un caso
que declaraba 5 de 10 columnas obtenía estadísticas, nulos y outliers de las
10, y el informe describía como analizadas variables que ni se trataban ni
llegaban al modelo. `data_profile.json` publica `scope`,
`profiled_columns` e `ignored_columns` para que la omisión sea explícita.

Dos matices deliberados:

- **`duplicate_rows` se sigue contando sobre todas las columnas.** Una fila
  duplicada es propiedad del dato de origen, no del subconjunto declarado, y
  es además el criterio exacto que aplica `deduplicate_rows`; contarlas solo
  sobre lo declarado haría que ambas skills discreparan.
- **Las no declaradas conservan un inventario mínimo**
  (`undeclared_column_summary`: tipo, cardinalidad y % de nulos). No forma
  parte del análisis, pero `propose_features` lo necesita: su trabajo es
  justamente ofrecer las columnas que el caso no declaró, y sin esto acotar
  el perfilado la habría dejado ciega.

El bloque del target (`target_distribution`/`class_imbalance` frente a
`target_statistics`) se decide por el `task_type` efectivo del caso
(`modeling_config.task_type`, por defecto `binary_classification`), no por
el tipo de columna inferido: un target binario 0/1 se infiere como numérico
igual que cualquier otra columna cuantitativa, y aun así debe seguir
tratándose como clasificación por defecto. La inferencia de columna solo se
usa como aviso secundario (`task_type_consistency_warning`) cuando no
coincide con lo esperado — p. ej. un target que parece continuo pero
`task_type` sigue siendo de clasificación.

### Restricciones de negocio (`column_constraints`)

`validate_data` puede comprobar además reglas de negocio declaradas
explícitamente en el caso, opcionales y desactivadas por defecto:

```json
{
  "column_constraints": {
    "amount": {"min": 0},
    "category": {"allowed_values": ["online", "presencial", "cajero"]}
  }
}
```

Cada columna admite `min`/`max` (numérico) y/o `allowed_values` (lista de
valores permitidos). Solo evalúa valores no ausentes — un nulo es
competencia de `handle_missing_values`, no de esta validación. Si alguna
fila incumple una restricción, `validate_data` bloquea igual que cualquier
otro fallo estructural (cabeceras vacías, columnas ausentes, etc.), con el
recuento de violaciones y hasta 20 posiciones de fila en el mensaje de
error. Con `column_constraints` vacío (el valor por defecto de todos los
casos existentes), el comportamiento no cambia en nada.

## Propuesta de features (`propose_features`)

Todo el resto del pipeline de variables es **sustractivo**:
`select_features` solo quita, `treat_data` solo recorta,
`deduplicate_rows` solo elimina filas. Ninguna otra skill añade
información, así que las variables que ve el modelo son exactamente las
que alguien escribió a mano en `case.features`, menos las que el sistema
decida descartar — un pipeline que en el mejor caso deja los datos como
estaban tiene un techo de calidad que ningún algoritmo puede superar.
Medido: en Titanic, incorporar `Sex`/`Embarked` (declaradas como
`sensitive_attribute` la primera, simplemente no declarada la segunda)
subió el F1 del modelo final más que cualquier cambio de algoritmo o
hiperparámetro.

`propose_features` es la única skill que amplía el conjunto de variables.
Está disponible con una validación y un perfil vigentes y columnas candidatas
con decisión pendiente. Usa el perfil descriptivo del dataset completo, sin
ajustar ningún estadístico. Recorre las columnas
del CSV que no están en `case.features`, ni son el target, ni son
`sensitive_attributes`, descarta primero las claramente inutilizables
(vacías, con más del 40 % de nulos, o con más del 50 % de valores
distintos respecto al total de filas — casi seguro un identificador) y,
por cada una que sobrevive ese filtro, abre una decisión analítica con el
contexto real: tipo detectado, porcentaje de nulos, cardinalidad. Nunca
mira la correlación con el target como argumento — eso sería decidir qué
variables usar mirando la respuesta, y con el dataset entero.

El sistema **propone**, nunca incorpora por su cuenta: la opción
recomendada es siempre "no incorporar", y en resolución automática
(`AutomaticDecisionResolver`) se queda así. Cada decisión — columna a
columna, igual que `handle_missing_values` — queda en
`analytical_decisions.json`. El resultado se escribe en
`feature_proposal.json` (`accepted_features`, `proposed_features` =
`case.features` + las aceptadas) y lo recoge
`skills/modeling/common.py::resolve_declared_features`, el primer eslabón
de la cadena de features que consume el resto del pipeline
(`case.features` → propose_features → encode_categorical →
engineer_features → select_features → lo que entra al modelo).

## Deduplicación de filas exactas

`deduplicate_rows` es una acción disponible cuando el perfil vigente detecta
duplicados todavía no resueltos. Puede ejecutarse antes o después del split.
Si se ejecuta después, la nueva versión del dataset invalida el split y las
transformaciones o modelos ajustados con el train anterior; el agente podrá
crear una partición nueva.

El criterio es el mismo que ya usa `profile_data` para *contar* duplicados
(filas idénticas en todas las columnas) — aquí, si el agente la elige, se
actúa sobre ello: se conserva la primera aparición de cada fila duplicada,
el resto se excluye. No borra nada del CSV original: materializa
`deduplicated_dataset.csv` y `deduplication_report.json`
(`n_duplicates_removed`, posiciones eliminadas). `split_data` (y todo lo que
venga después) recoge automáticamente este dataset a través de
`skills/dataset_resolution.py`, igual que recoge `imputed_dataset.csv` o
`treated_dataset.csv`.

Es una asunción de dominio, no una verdad universal: dos filas idénticas en
todas las columnas observables no son necesariamente el mismo evento (podrían
ser dos transacciones legítimas coincidentes). Por eso es opcional y queda
trazado, nunca aplicado en silencio.

## Tratamiento de valores nulos (sin fuga train/test)

`handle_missing_values` se ejecuta **después de `split_data`**
(`requires_artifacts` incluye `data_split.json`, así que solo aparece como
acción cuando existe una partición vigente). El orden importa localmente: el
valor de relleno (mediana/media/moda) se calcula **solo con las filas de
`train_indices`**, nunca con el dataset completo, y ese mismo valor se
aplica después a train y test por igual — igual que se haría con datos
nuevos en producción. Calcularlo con todas las filas (como en la primera
versión de esta skill) sería fuga de información: el valor de imputación
de train estaría contaminado con datos de test.

Para cada columna afectada se pide, **columna a columna**, una decisión
analítica independiente — mediante el modo automático interno para tests, vía
LLM sin supervisión (`--decision-resolver llm`) o vía LLM con confirmación humana obligatoria
(`--decision-resolver llm-confirm`: el LLM propone y justifica, pero una
persona tiene que aceptarlo o elegir otra opción por consola antes de que
se aplique nada — ver `LLMConfirmedDecisionResolver` en `decisions.py`).
El contexto que se ofrece (nulos, outliers,
correlaciones) sigue viniendo de `profile_data` sobre el dataset completo
(es solo diagnóstico, no lo que se aplica); pero los valores concretos que
ofrece cada opción (`"la mediana de train (28.25)"`) ya son el resultado
train-only. Columnas distintas pueden acabar con estrategias distintas
dentro del mismo caso (p. ej. imputar `amount` con la mediana y eliminar
filas por `category`).

Las opciones son imputar, eliminar las filas afectadas o **prescindir de la
variable** (`drop_column`). La tercera existe porque las dos primeras dejan
de ser defendibles a partir de cierto porcentaje de nulos: con un 70 %,
imputar por la moda fabrica el 70 % de la columna —y la convierte casi en
una constante— mientras que eliminar filas se lleva el 70 % del dataset por
una sola variable. Por encima de `_HIGH_MISSING_PERCENTAGE` (50 %),
`drop_column` pasa a ser la opción recomendada y ninguna imputación lo es.

Eliminar la variable no toca el CSV, igual que "eliminar filas" no borra
filas: se registra en `missing_values_report.json["dropped_columns"]` y la
resta la aplica `resolve_declared_features`, el **primer** eslabón de la
cadena de features. Tiene que ser el primero porque
si se elimina una variable, aumenta su versión e invalida cualquier
codificación o modelado que dependiera del conjunto anterior.

La skill en sí nunca decide nada — solo calcula el contexto y aplica lo que
`SkillContext.decision_coordinator` resuelva. Si una columna está
completamente vacía (sin dato del que partir), no se le pregunta a nadie:
se marca como no resoluble y el caso falla explícitamente.

La skill nunca modifica el CSV original: escribe `missing_values_report.json`
(qué se decidió y por qué, columna a columna, con la justificación de quien
decidió) y, si había nulos, materializa `imputed_dataset.csv` como artefacto
nuevo con **todas** las filas del original (ninguna se borra físicamente).
Si alguna columna elige "eliminar filas", esas filas no desaparecen del
CSV — se **excluyen de `train_indices`/`test_indices`** en
`data_split.json`, que esta skill reescribe junto con su `dataset_sha256`
para que apunte al dataset imputado. Borrarlas físicamente rompería los
índices posicionales que ya calculó `split_data`.

A partir de aquí, las skills de modelado leen automáticamente el dataset
imputado en vez del original (`skills/dataset_resolution.py` centraliza esa
decisión); si no se ejecutó esta skill o no había nulos, siguen usando el
original sin cambios.

## Tratamiento de outliers

`treat_data` decide, **columna a columna**, cómo tratar los valores
extremos de cada feature numérica vigente (`case.features` más lo que hayan
aportado `propose_features`/`encode_categorical`): winsorizarlos, eliminar
sus filas o conservarlos. Es un paso **opcional**: el agente solo lo
incluye si el objetivo lo pide, igual que `handle_missing_values`. Se
sitúa **después** de `split_data` y,
si el plan también incluyó `handle_missing_values`/`encode_categorical`,
después de esas skills — lee el dataset más avanzado disponible vía
`skills/dataset_resolution.py` (que resuelve, de más a menos reciente:
tratado > derivado > codificado > imputado > deduplicado > original) y
reescribe `data_split.json.dataset_sha256` para que apunte a
`treated_dataset.csv` una vez termina. Los límites se ajustan
exclusivamente con las filas de train y se aplican después a todo el
dataset (train y test), preservando el orden de filas para que los índices
de `data_split.json` sigan siendo válidos. Calcularlos con todo el dataset
filtraría información de test hacia el tratamiento de train — la misma
fuga que el proyecto ya evita ajustando el `StandardScaler` dentro de cada
fold en `cross_validate_models`.

**Qué se hace con cada columna es una decisión analítica**, no una política
fija, con tres opciones: recortar al intervalo de train (`winsorize`,
recomendada), eliminar las filas que caen fuera (`drop_rows`, que excluye
posiciones de `train_indices`/`test_indices` sin borrar nada del CSV) o
conservarlos intactos (`keep`, sabiendo que entrarán al modelo e influirán
en los algoritmos sensibles a la escala). La skill calcula primero, para
cada columna candidata, cuántas filas recortaría winsorizar de verdad
(con los límites de train) — **no** el conteo de atípicos IQR·1,5 de
`profile_data`, que es un criterio distinto y puede desalinearse del
recorte real (ver más abajo). Solo se pregunta si ese recorte real
afectaría al menos a una fila; si no afectaría a ninguna, las tres
opciones serían idénticas y no hay nada real que decidir, así que se
aplica `winsorize` automáticamente sin preguntar.

Sin `decision_coordinator` la skill **no aborta**: aplica el recorte, que es
lo que hacía siempre, y la evidencia registra que nadie lo eligió
(`decision_source: "automatic_default"`). Es lo contrario de
`handle_missing_values`, que sí falla, y la diferencia es deliberada: allí
no hay default defensible —rellenar huecos con la mediana sin que nadie lo
haya elegido *es* la decisión analítica— mientras que aquí el recorte es el
comportamiento documentado de la skill.

**Winsorización p1–p99, no IQR·1,5** (`skills/outlier_bounds.py`, módulo
compartido con la CV fold-safe — ver más abajo). El criterio IQR tenía un
modo de fallo grave y silencioso: con ≥50 % del mismo valor en una columna
(habitual en variables de recuento, y *universal* en las dummies que
genera `encode_categorical`), `q1 == q3`, luego `IQR = 0`, luego los
límites son `[q1, q1]` y **toda la columna se aplasta a una constante**.
Medido sobre `data/titanic.csv`: `Parch` perdía así sus 213 valores no
nulos, y después `select_features` la descartaba honestamente por
"varianza casi nula" — documentando en la traza un diagnóstico correcto
sobre un dato que el propio pipeline acababa de destruir. Dos guardas en
`fit_column_bounds` lo impiden:

- **Cardinalidad mínima** (`MIN_DISTINCT_TO_TREAT = 10`): una columna
  discreta o dummy no tiene cola que recortar; se deja intacta.
- **Rango no degenerado**: si los percentiles no abren un intervalo, no
  hay recorte que aplicar sin colapsar la columna.

Ambas quedan trazadas en `data_treatment.json["skipped_by_guard"]`
(`"low_cardinality"` o `"degenerate_bounds"` por columna) — la evidencia
dice qué NO se tocó y por qué, no solo qué se tocó.

**Decisión analítica por columna.** Superar las guardas no basta para que
una columna se recorte: un recorte uniforme puede comerse señal real (en un
caso medido, recortar `Fare` bajó el F1 del modelo final en un dataset de
supervivencia, porque los pasajes caros predicen genuinamente mejor
supervivencia, no son ruido). Pero preguntar solo tiene sentido si
winsorizar realmente recortaría algo: si una columna supera las guardas
pero ningún valor de train ni de test cae fuera de los límites de train,
las tres opciones son idénticas y no hay nada real que decidir — no se
pregunta, se aplica `winsorize` (un no-op) y queda registrado como
`automatic_default`. Es deliberadamente el mismo criterio para decidir
"¿pregunto?" que para aplicar el tratamiento — no dos cálculos que puedan
desincronizarse y dejar una columna sin tratar en silencio porque el
diagnóstico usado para preguntar no coincidía con el que se aplica.

Para cada columna donde SÍ hay algo que recortar, `treat_data` construye un
`AnalyticalDecisionRequest` con los límites calculados y cuántas filas de
train/test se verían afectadas, y lo resuelve vía
`context.decision_coordinator` (automático interno/LLM/LLM confirmado) — mismo patrón que
`handle_missing_values` con los nulos. Tres opciones:

- `winsorize` (recomendada por defecto: es el comportamiento histórico de
  la skill) — recorta los valores fuera de los límites de train.
- `drop_rows` — excluye esas filas de train/test (no las borra del CSV:
  ver más abajo, mismo patrón que `handle_missing_values`).
- `keep` — conserva la columna tal cual.

**Sin `context.decision_coordinator` disponible**, se aplica `winsorize`
automáticamente y se registra como `decision_source: automatic_default` —
a diferencia de `handle_missing_values` (que sí falla sin coordinador: no
hay un default defensible para fabricar un valor que antes no existía),
aquí winsorizar es el comportamiento documentado de la skill desde
siempre, así que aplicarlo por defecto no es inventar nada nuevo.

Las columnas que se decidieron `keep` quedan en
`data_treatment.json["kept_by_decision"]` (columna → `lower_bound`/
`upper_bound`/`decision_rationale`/`decision_source`); las `drop_rows`,
dentro de `outlier_treatment` con `method: "drop_rows_outside_train_bounds"`
y `n_rows_excluded`; las `winsorize` (por decisión, por defecto sin nada
que recortar, o por el fallback sin coordinador), también en
`outlier_treatment`, con `decision_rationale`/`decision_source` — mismo
formato que usa `missing_values_report.json`. `rows_excluded_from_split` y
`rows_excluded_by_feature` (a nivel de todo el tratamiento) documentan
cuántas filas se excluyeron y por qué columna.

`treat_data` no imputa nulos: esa decisión, columna a columna y con
contexto real, es responsabilidad exclusiva de `handle_missing_values`. Un
valor nulo que llegue hasta aquí (porque esa skill no corrió, o porque una
columna concreta no tenía nulos que imputar) se deja tal cual, sin fabricar
un valor; solo se calculan límites con los números ya presentes en train.
`treat_data` tampoco toca `target` ni `sensitive_attributes`: recortar un
atributo sensible inventaría un dato protegido (RGPD art. 9). Su alcance
sigue acotado a columnas ya numéricas — no codifica variables categóricas
nuevas; para eso está `encode_categorical` (ver más abajo), que se ejecuta
antes en la cadena precisamente para que sus columnas dummy (0/1) ya sean
numéricas cuando `treat_data` las vea (y la guarda de cardinalidad mínima
las deja intactas, así que el recorte sobre una dummy nunca cambia nada).

`data_treatment.json` conserva los límites de winsorización y cuántos
valores se recortaron en train y en test por separado. `profile_data` sigue
leyendo el dataset original sin cambios: es puramente descriptiva y no
depende de `treat_data` ni al revés.

**Fuga cerrada dentro de la validación cruzada:** los límites (y, si
`select_features` corrió, su criterio de varianza/correlación) se siguen
ajustando una única vez sobre todo train para el **dataset final** que usan
`train_model`/`evaluate_model` (correcto: el modelo final legítimamente se
ajusta con el 100 % de train). Pero `cross_validate_models` y
`tune_hyperparameters` ya no reutilizan esos límites globales para
*estimar* el rendimiento: los recalculan de forma independiente **dentro de
cada fold**, solo con las filas de ajuste de ese fold — ver
"Fold-safe: recorte y selección recalculados por fold" más abajo.

### Filtro previo por columna (`treat_data_columns`)

`case.treat_data_columns` (opcional, `None` por defecto) es un filtro
**previo y más grueso** a la decisión analítica por columna: sin
declararlo, todas las features numéricas entran en la decisión; declarado,
solo las de la lista entran — el resto ni siquiera se le pregunta a nadie:

```json
{
  "treat_data_columns": ["amount"]
}
```

Con esto, solo `amount` puede llegar a tratarse; el resto de features
numéricas pasan intactas. Las columnas excluidas por el caso quedan
trazadas en `data_treatment.json["skipped_by_case_config"]`, junto a
`excluded_columns` (target/atributos sensibles, que nunca se tocan pase lo
que pase) — cuatro motivos distintos para que una columna no termine
winsorizada, cada uno con su propia clave en la evidencia:
`skipped_by_case_config` (fuera del filtro del caso), `skipped_by_guard`
(cardinalidad o límites degenerados), `kept_by_decision` (decisión
explícita de conservarla) y las entradas `drop_rows_outside_train_bounds`
dentro de `outlier_treatment` (decisión explícita de eliminar sus filas).

## Partición, entrenamiento y evaluación

`split_data` es un prerrequisito obligatorio del modelado. La skill genera
`data_split.json` con los índices de entrenamiento y prueba, la configuración
efectiva, su procedencia, la distribución del target y comprobaciones de
cobertura y ausencia de solapamiento. Por defecto realiza una división
estratificada con 80 % para entrenamiento, 20 % para test y semilla 42.

El caso puede sobrescribir esos valores de forma explícita y auditable:

```json
{
  "split_config": {
    "test_size": 0.25,
    "random_seed": 7,
    "stratify": true
  }
}
```

Si `split_config` no aparece, se aplican los valores predeterminados de la
skill.

## Candidatos, selección y evaluación final

Las skills de modelado viven en `skills/modeling/`, pero no forman una cadena
universal de preparación. El estado calcula `model_ready` a partir del split,
el target, las variables efectivas, la representación numérica, los nulos, la
compatibilidad de fingerprints y la suficiencia de muestras y clases.

`handle_missing_values`, `encode_categorical`, `engineer_features`,
`treat_data` y `select_features` solo aparecen cuando sus condiciones locales
se cumplen. El agente puede intercalarlas y volver a ejecutarlas si cambia una
entrada relevante. Por ejemplo, `treat_data → encode_categorical` y
`encode_categorical → treat_data` son recorridos válidos.

Una vez `model_ready`, las dependencias técnicas de modelado son:

```text
baseline_model                         (evidencia opcional)
cross_validate_models → select_model → (tune_hyperparameters)
                                      ↓
                                  train_model → evaluate_model
```

`train_model` consume un ajuste vigente cuando existe y es aplicable, pero el
baseline no es una dependencia obligatoria del ajuste. Cada resultado queda
ligado a las versiones de dataset, variables, split y configuración.
`skills/dataset_resolution.py` recibe el dataset actual desde `AgentState` y
su linaje, sin elegirlo por el nombre del artefacto.

### Codificación de variables categóricas

`encode_categorical` convierte en one-hot las features categóricas
vigentes (`skills/modeling/common.py::resolve_declared_features`:
`case.features` más lo que `propose_features` haya incorporado), ajustado
exclusivamente con train — mismo principio anti-fuga que
`treat_data`/`handle_missing_values`. Puede ejecutarse antes o después de
`treat_data`; solo exige que sus propias categóricas no tengan nulos
incompatibles. Tras codificar, `select_features` puede evaluar las dummies
igual que cualquier otra columna numérica (una categoría casi ausente en
train se descarta automáticamente por varianza casi nula, sin código
adicional).

Para cada feature: si ya es numérica en train, pasa intacta. Si es
categórica, calcula las categorías vistas **solo en train**; si son más de
15 (`_HIGH_CARDINALITY_THRESHOLD`, probablemente un identificador y no una
categoría real), la excluye de las features efectivas en vez de generar
más de 15 columnas dummy, dejando constancia explícita del motivo. Si no,
genera una columna `{columna}__{categoria}` por cada categoría de train,
aplicada después a todo el dataset (train y test); una categoría de test no
vista en train, o un valor nulo, produce todas las dummies de esa columna
en cero — no es fuga, es lo mismo que pasaría con un valor nuevo en
producción.

No modifica ni elimina ninguna columna del CSV original: añade las dummies
nuevas al final en `encoded_dataset.csv` (`target` y `sensitive_attributes`
intactos; la columna categórica original se conserva pero no forma parte de
las features efectivas, porque `to_xy` no puede convertirla a float) y
escribe `categorical_encoding.json` con las decisiones por columna y
`encoded_features` — la lista que sustituye a las features declaradas en
el resto del pipeline.

### Variables derivadas (`engineer_features`)

Junto con `propose_features`, es lo único del pipeline que **añade**
información en vez de recortarla — la diferencia es que aquélla incorpora
columnas que ya existían en el CSV, y ésta crea columnas nuevas a partir de
las que el modelo ya usa. Se sitúa **después** de `encode_categorical` y
**antes** de `treat_data`: opera sobre las columnas originales (numéricas
vigentes, incluidas las declaradas por `propose_features`), no sobre las
dummies.

Dos familias, cada una con su propia decisión analítica columna a columna
(mismo patrón que `handle_missing_values`):

- **Indicador de nulo** (`columna__is_missing`): se ofrece cuando una
  columna tiene entre el 5 % y el 95 % de nulos. La ausencia de un dato
  suele ser predictiva por sí misma y se pierde entera al imputar.
- **Transformación logarítmica** (`columna__log1p`): se ofrece cuando una
  columna numérica no negativa está muy sesgada a la derecha (asimetría de
  Pearson en train `≥ 0.75`) — importes, tarifas, rentas, donde el orden de
  magnitud suele discriminar mejor que el valor absoluto.

Ambas se calculan fila a fila, sin ningún estadístico ajustado sobre el
conjunto (el umbral de asimetría se mide solo con train, pero la
transformación en sí no ajusta ningún parámetro), así que no introducen
fuga y no hace falta replicarlas en `fold_preprocessing`. Una familia
futura que sí ajuste estadísticos (medias por cliente, agregados
temporales) tendría que calcularlos solo con train y añadirse también ahí,
como ya hacen `treat_data`/`select_features`.

No modifica las columnas originales: añade las derivadas al final en
`engineered_dataset.csv` y escribe `feature_engineering.json` con las
decisiones y `engineered_features` — parte de `resolve_base_features`
(post-codificación), no de las features declaradas, para no perder las
dummies que `encode_categorical` acabara de incorporar.

### Selección de features

`select_features` decide, con datos exclusivamente de train, qué
subconjunto de las features base pasa al modelado — no modifica el
dataset, solo escribe `feature_selection.json` con la lista resultante
(`selected_features`) y el porqué de cada descarte
(`feature_decisions`). Descarta tres tipos de features, todos con criterios
deterministas y auditables (no es una decisión analítica con
`decision_coordinator`, a diferencia de `handle_missing_values`, porque no
requiere juicio de dominio: son señales objetivas):

- **Identificador**: prácticamente un valor distinto por fila en train
  (`≥ 95 %`) y sin relación con el target (`|Pearson| ≤ 0.15`). Un ID no
  aporta señal generalizable: el modelo solo puede memorizar filas
  concretas. El hueco que cubre este criterio es el identificador
  **numérico** (`PassengerId`, `id_cliente`), que tiene varianza enorme y
  correlación ~0, de modo que ni el filtro de varianza ni el de redundancia
  lo tocaban; los de texto ya los excluía `encode_categorical` por alta
  cardinalidad (>15 categorías).

  Tres matices: (1) no se aplica por debajo de 20 filas, porque con pocas
  observaciones casi cualquier variable continua legítima es única; (2) el
  umbral de relevancia es más permisivo que el de la red de seguridad de
  varianza (0.05) porque responde a otra pregunta — la correlación empírica
  de un ID con el target se mueve en torno a 0,1 por puro ruido de
  muestreo; (3) si la columna es casi única **y** sí correlaciona, no se
  descarta: se conserva marcada con `identifier_suspected`, porque esa
  combinación suele indicar fuga de información y no una variable inútil, y
  esa lectura es del analista.

  La unicidad se mide sobre **train**, no sobre el `uniqueness` de
  `data_profile.json` (que la calcula sobre el dataset completo, test
  incluido). Seleccionar variables con una estadística que ha visto test es
  fuga, igual que lo sería calcular aquí la varianza sobre todo el dataset;
  el perfil publica la suya para describir, no para decidir.
- **Varianza casi nula en train** (`≤ 1e-12`): la feature es prácticamente
  constante y no puede aportar señal a ningún modelo. **Red de
  seguridad**: si además correlaciona con el target por encima de 0.05 en
  train, no se descarta — casi siempre significa que un paso anterior la
  degradó (era exactamente el caso de `Parch` tras el recorte IQR de
  `treat_data` antes de la winsorización) — se conserva y se registra el
  conflicto (`target_relevance` en `feature_decisions`) en vez de perder la
  variable en silencio.
- **Redundante con otra feature ya conservada** (correlación de Pearson en
  train `≥ 0.9`): de cada par por encima del umbral se descarta la **menos
  relacionada con el target** (`|Pearson(feature, target)|` en train). Si
  la relevancia no es calculable para alguna de las dos (target no
  numérico, o muy pocos pares completos) o empatan, se cae al criterio
  determinista anterior — descartar la segunda en el orden declarado.
  Antes de esto, el desempate era siempre por orden de declaración: si
  `amount` y `amount_eur` correlaban 0.98, sobrevivía la que el usuario
  escribió primero en el JSON, no la que mejor predecía.

Las "features base" que evalúa son las declaradas
(`resolve_declared_features`), salvo que `encode_categorical` y/o
`engineer_features` corrieran antes, en cuyo caso son su resultado (dummies
y derivadas incluidas) vía `skills/modeling/common.py::resolve_base_features`.
Así también evalúa las dummies y derivadas nuevas. Una feature que sigue
sin ser numérica (porque `encode_categorical` no corrió, o la excluyó por
alta cardinalidad) se conserva sin evaluar, nunca se descarta a ciegas. Si
el filtro dejaría el caso sin ninguna feature utilizable, se activa una red
de seguridad adicional: se conservan todas y se deja constancia en
`safety_net_applied`.

El resto del pipeline recoge el resultado a través de
`skills/modeling/common.py::resolve_effective_features` (usado
internamente por `load_partition`, y por tanto por `train_model` y
`evaluate_model`): si `select_features` corrió, usa
`feature_selection.json.selected_features`; si no, usa `resolve_base_features`
(las features base, ver arriba). `model.json` registra las features
realmente usadas, así que `generate_docs` documenta el subconjunto efectivo
aunque no haya tocado `select_features` directamente.

### Fold-safe: recorte y selección recalculados por fold

`cross_validate_models` y `tune_hyperparameters` no reutilizan
`treated_dataset.csv`/`feature_selection.json` (los límites/selección
globales, ajustados una única vez sobre todo train) para *estimar*
métricas: usan `skills/modeling/common.py::load_fold_safe_train`, que carga
train tal como estaba **antes** de `treat_data` (features base, sin
selección) y, **dentro de cada fold**, si `treat_data`/`select_features`
corrieron en el plan:

1. Ajusta los límites de winsorización solo con las filas de ajuste de ese
   fold (`skills/modeling/fold_preprocessing.py::fit_iqr_bounds`, que
   reutiliza `outlier_bounds.fit_column_bounds` — mismas guardas que
   `treat_data`) y los aplica a ajuste y validación, pero **solo sobre las
   columnas que `treat_data` decidió tratar de verdad**
   (`data_treatment.json["treated_features"]`, propagado por
   `load_fold_safe_train` como `treat_columns`) — no basta con compartir las
   guardas: una columna que supera la guarda de cardinalidad pero que
   `treat_data` dejó intacta (por `case.treat_data_columns` o por su
   decisión analítica por columna) tampoco debe recortarse aquí, o la CV
   mediría un pipeline distinto del que finalmente se entrena.
2. Sobre esos valores ya recortados, recalcula la selección de
   varianza/correlación solo con las filas de ajuste
   (`select_by_variance_and_correlation`, mismo criterio y desempate que
   `select_features`) y aplica la misma máscara de columnas a ajuste y
   validación.

Así ningún fold de validación influye en los límites/selección que después
se le aplican a sí mismo — la fuga leve que tenían las versiones globales
(documentada en versiones anteriores de este documento) queda cerrada para
las métricas de la CV/tuning. `candidate_evaluation.json` y
`hyperparameter_search.json` registran `fold_local_preprocessing` (si se
recalculó recorte/selección por fold, y `treat_data_columns` con el listado
exacto de columnas incluidas en ese recorte). `train_model`/`evaluate_model`
siguen usando el
dataset globalmente tratado y seleccionado — ahí no hay fuga que cerrar,
porque el ajuste final legítimamente usa el 100 % de train.

Este preprocesado se calcula **una vez por fold** y lo comparten todos los
candidatos y todas las combinaciones de la rejilla
(`fold_preprocessing.py::prepare_folds`, usado por ambas skills). Depende solo
de las filas del fold y de la configuración del caso, nunca del algoritmo que
se ajuste después: hacerlo dentro del bucle de candidatos repetía el mismo
cálculo —incluida la correlación por pares, que es cuadrática en el número de
features— una vez por algoritmo. El resultado numérico es idéntico; lo que
cambia es cuántas veces se calcula. `tests/test_fold_reuse.py` fija esa
propiedad contando las llamadas, para que un refactor no devuelva el cálculo
al interior del bucle sin que nada falle.

- `baseline_model` (opcional) evalúa un modelo trivial (`DummyClassifier`
  con la clase mayoritaria / `DummyRegressor` con la mediana) con los
  mismos folds que los candidatos reales, y escribe su propio
  `baseline.json` — **no** entra en `candidate_evaluation.json` ni puede
  ganar `select_model`. Sin una referencia, una métrica no se interpreta:
  un F1 de 0.53 puede ser un modelo mediocre o un problema
  intrínsecamente difícil, y el número por sí solo no distingue los dos
  casos. "El modelo supera al baseline en X" sí es una afirmación
  verificable.
- `cross_validate_models` compara los candidatos del `task_type` efectivo
  mediante validación cruzada dentro de train (`StratifiedKFold` para
  clasificación binaria y multiclase, `KFold` para regresión), sobre los
  datos ya preparados (si `handle_missing_values` y/o `treat_data`
  corrieron). Cada algoritmo aplica **dentro de cada fold** el preprocesado
  obligatorio que declara el catálogo (estandarización para lineales, KNN,
  SVM y redes; reescalado o binarización para las variantes de Naive Bayes;
  nada para los árboles y el boosting) — ver "Preprocesado obligatorio" más
  abajo. También
  calcula `minority_class_ratio` (proporción de la clase menos frecuente en
  train), que usa `select_model` — ver más abajo.
- `select_model`, por defecto, combina con el mismo peso las métricas de
  selección del `task_type` (leídas del propio artefacto de evidencia,
  nunca de una lista fija, para que cada tipo de problema use las suyas).
  Selecciona la mayor media compuesta y deja registradas las métricas
  ganadas por cada candidato. Si persiste el empate, prioriza menor
  variabilidad entre folds y, finalmente, el candidato más simple
  (`simplicity_rank` del catálogo, un orden global por `task_type` que va
  de los modelos lineales a las redes neuronales, pasando por Naive Bayes,
  vecinos, árboles, kernel y boosting). `model_catalog.py::MODEL_CATALOG` es
  la única fuente de `simplicity_rank` — el meta-auditor (control A13) lee de
  ahí también, en vez de duplicar su propio ranking, precisamente para no
  desincronizarse cada vez que se añade un algoritmo nuevo.

  Cuando la comparación se restringió a una familia, el motivo registrado lo
  dice explícitamente: "el mejor" de un grupo no es "el mejor" del catálogo,
  y `model_selection.json` guarda `candidate_scope`, `n_candidates_compared`
  y `selected_algorithm_family` para que eso no se pierda.

  **Excepción consciente del desbalanceo.** La media a peso igual incluye
  `accuracy`, y con una clase minoritaria pequeña el modelo que nunca la
  predice saca accuracy altísima y arrastra el compuesto hacia arriba — se
  premia al modelo inútil. Si el usuario no fijó `selection_metric`, el
  `task_type` es `binary_classification` y `minority_class_ratio < 0.20`,
  `select_model` selecciona automáticamente por PR-AUC en su lugar (la
  métrica que sí mide el rendimiento sobre la clase rara) y lo deja
  registrado en `reason` y en `selection_metric_source: "imbalance_default"`.
  Es solo el *criterio por defecto*: si el caso declara `selection_metric`
  explícitamente, esa decisión manda siempre sobre el automatismo
  (`selection_metric_source: "case"`).

  `modeling_config.selection_metric` (opcional, `None` por defecto) permite
  declarar una única métrica como criterio de selección en vez de la media
  compuesta — útil cuando importa más una métrica concreta que el resto
  (p. ej. `"f1"` en un problema desbalanceado, donde subir accuracy/ROC-AUC
  a costa de F1 no interesa). Se valida contra
  `candidate_evaluation.json["selection_metrics"]` del `task_type` real —
  pedir `"mae"` en clasificación falla explícitamente, no se ignora en
  silencio. Con `selection_metric` fijado (por el caso o por el
  automatismo de desbalanceo), el desempate ya no usa "métricas ganadas"
  (eso reintroduciría el peso compuesto que la opción busca evitar): solo
  la variabilidad de esa métrica y, al final, `simplicity_rank`.
  `model_selection.json` registra `selection_strategy`
  (`"equal_weight_composite"` o `"single_metric"`), `selection_metric` y
  `selection_metric_source` para que quede trazado qué criterio decidió y
  por qué. El control A13 del meta-auditor reproduce ambas estrategias de
  forma independiente, no solo la compuesta.
- `tune_hyperparameters` (opcional) ajusta los hiperparámetros del
  algoritmo ya seleccionado — no vuelve a comparar familias de modelos,
  solo afina la elegida. Ver detalle más abajo.
- `train_model` ajusta el ganador con todo train y conserva el estimador,
  configuración, versión de librerías, hash y parámetros aprendidos. Si
  `tune_hyperparameters` corrió y aplicó una rejilla, usa sus mejores
  parámetros en vez de los valores por defecto del catálogo.
- `evaluate_model` abre test una única vez y genera métricas globales y por
  subgrupos. Test nunca participa en la comparación ni en la selección.

La configuración es opcional:

```json
{
  "modeling_config": {
    "task_type": "binary_classification",
    "algorithm": "auto",
    "cv_folds": 5,
    "random_seed": 42,
    "class_balancing": "none"
  }
}
```

`task_type` admite `binary_classification` (por defecto), `multiclass_classification`
y `regression`, y determina qué catálogo de algoritmos y qué métricas son
aplicables. Clasificación binaria y multiclase comparten el mismo catálogo
(18 algoritmos, todos con soporte multiclase nativo); regresión tiene el
suyo propio (16). El catálogo completo, con familia, preprocesado
obligatorio y rejilla de cada algoritmo, vive en `mads/model_catalog.py`.

**Algoritmos con dependencia opcional.** XGBoost, LightGBM y CatBoost viven en
el extra `boosting`, que no entra en la instalación mínima pero sí viene
incluido en `dev` (`pip install -e ".[dev]"`), para que el entorno de
desarrollo y los tests trabajen siempre con el catálogo completo. El catálogo declara la librería de cada uno en `requires_package` y
`is_available()` la comprueba con `importlib.util.find_spec`, que resuelve el
módulo **sin importarlo**: pintar un menú no debe cargar decenas de MB. La
importación real ocurre en `build_estimator`, cuando ya se va a entrenar.
Consecuencias:

- `algorithms_for()` devuelve el catálogo *efectivo*: sin la librería, el
  algoritmo no se ofrece al elegir ni entra en las comparaciones `auto`.
- Pedirlo explícitamente falla al construir el caso con un mensaje que dice
  qué instalar. Se distingue de "algoritmo no soportado" a propósito:
  `is_known_algorithm()` sigue devolviendo `True` para un algoritmo del
  catálogo cuya librería falta.
- El catálogo de skills publica `unavailable_models` con lo que se habilitaría
  al instalar el extra: una ausencia explicada es auditable; un hueco
  silencioso, no.
- Los tres fijan semilla y un único hilo (`n_jobs`/`thread_count`): con
  paralelismo el orden de reducción interno varía entre ejecuciones y dos runs
  con la misma semilla podrían no dar el mismo modelo. CatBoost además lleva
  `allow_writing_files=False`, porque por defecto escribe un directorio
  `catboost_info/` con logs fuera del ArtifactStore y de la cadena de hashes.
- `model.json` registra la versión de la librería usada junto a la de
  scikit-learn: citar solo sklearn no permitiría reproducir un ajuste hecho
  con XGBoost.
- XGBoost exige etiquetas `0..n-1`, y `to_xy` solo garantiza que sean enteras
  (un CSV con clases 1/2 es legítimo y el resto del catálogo lo acepta). Por
  eso su clasificador va envuelto en `LabelEncodedClassifier`, que codifica el
  target al ajustar y devuelve las etiquetas originales en `classes_` y
  `predict`. Es composición y no herencia para que la clase exista a nivel de
  módulo y `model.joblib` se pueda recargar en otro proceso.

**Familias.** Los algoritmos se agrupan en `linear_models`, `probabilistic`
(Naive Bayes), `neighbors`, `trees` (árbol suelto y ensembles por bagging),
`boosting` (derivados de gradient boosting), `kernel_methods` y
`neural_networks`. Naive Bayes no tiene contrapartida de regresión (no hay
una versión con sentido para ese algoritmo), así que esa familia solo
aparece en clasificación.

`algorithm_family` restringe la comparación a un grupo y `algorithm` admite
`auto` o el id de cualquier algoritmo del catálogo de ese `task_type`. Las
tres combinaciones válidas son: catálogo completo (`algorithm_family=null`,
`algorithm="auto"`), grupo (`algorithm_family="trees"`, `algorithm="auto"`)
y algoritmo único (`algorithm="random_forest"`, que rellena su propia
familia para que ambos campos no puedan contradecirse). Falla explícitamente
al construir el caso: un algoritmo del `task_type` equivocado (p. ej.
`logistic_regression` con `task_type=regression`), una familia que no existe
para ese `task_type`, una familia que no contiene al algoritmo pedido, o una
combinación que se quede sin ningún candidato.

Si el caso pide modelado pero no contiene esta configuración, la CLI abre
decisiones analíticas independientes y trazadas por separado — no una sola
pregunta con quince nombres, que ni sería una elección informada ni dejaría
claro qué se decidió:

1. `task_type` (`task_type_request()`): tipo de problema. Separada del resto
   para no obligar a inferir el tipo de problema por qué algoritmos se
   reconocen.
2. `algorithm_family` (`algorithm_family_request()`): grupo de algoritmos, o
   "comparar todos" (recomendada), que conserva el comportamiento anterior
   de enfrentar el catálogo completo. Cada opción enumera los algoritmos que
   incluye.
3. `modeling_strategy` (`modeling_strategy_request()`): solo si se eligió un
   grupo. Dentro de él, "el mejor del grupo" (recomendada) o un algoritmo
   concreto, con su preprocesado obligatorio a la vista.
4. `selection_metric` (`selection_metric_request()`): solo si queda más de un
   candidato compitiendo. "Media a peso igual" (recomendada) o una métrica
   concreta del `task_type` real. Con un algoritmo explícito —o con una
   familia de un único algoritmo— no hay nada que desempatar y se omite.

En ejecuciones automáticas de CI se aplican los valores recomendados
(`binary_classification` + comparar todos + `auto` + media a peso igual).

### Preprocesado obligatorio por algoritmo

Hay algoritmos que no admiten los datos tal cual salen del pipeline. KNN mide
distancias, y sin estandarizar las decide la variable de mayor rango; el
descenso de gradiente de las redes y de SGD no converge con escalas dispares;
`MultinomialNB` aborta con valores negativos; `BernoulliNB` modela
presencia/ausencia y necesita features binarias.

Ese preprocesado lo declara el catálogo (`preprocessing` de cada entrada, con
los tipos definidos en `PREPROCESSING_REQUIREMENTS`) y lo aplica
`build_estimator` envolviendo el estimador en un `Pipeline` de sklearn. Tres
consecuencias, las tres deliberadas:

- **Se ajusta dentro de cada fold.** Al ser parte del estimador, el escalador
  ve solo las filas de ajuste; aplicarlo antes de la validación cruzada
  filtraría información desde los folds de validación.
- **No es configurable ni desactivable por caso.** No es una preferencia
  analítica: es la condición para que el algoritmo esté bien usado.
- **Añadir un algoritmo no puede olvidarlo.** Antes había una cadena de
  `if algorithm == ...` que construía a mano el `Pipeline` de cada uno;
  olvidar el escalador de un algoritmo nuevo era un error silencioso que solo
  se notaba en métricas peores. Ahora un algoritmo sin `preprocessing`
  declarado ni siquiera se puede registrar.

Es distinto de `encode_categorical`, que es un paso del pipeline (one-hot
ajustado solo con train) común a todos los algoritmos: cuando actúa el
preprocesado obligatorio, las features ya son numéricas.

`class_balancing` compensa el desbalanceo de clases, aplicado
exclusivamente sobre el fold/partición de ajuste (nunca sobre validación ni
test), en el mismo punto donde ya se evita fuga al ajustar el escalador
solo con el fold de ajuste. Solo aplica a clasificación: con
`task_type=regression` debe ser `"none"` (cualquier otro valor falla
explícitamente al construir el caso, en vez de ignorarse en silencio) — el
balanceo de clases no tiene sentido para un target continuo.

- `"none"` (por defecto): sin compensación.
- `"class_weight"`: pesa cada fila por la inversa de la frecuencia de su
  clase (`sample_weight` en `.fit()`); no cambia el número de filas. Los
  algoritmos del catálogo que no aceptan `sample_weight` (KNN, LDA y las
  redes) quedan fuera de la comparación con esta estrategia, y el motivo se
  registra en `candidate_evaluation.json.excluded_candidates`: compararlos
  ignorando el balanceo pedido sería una comparación desigual y silenciosa.
  Si la exclusión dejara la configuración sin ningún candidato (p. ej. la
  familia `neighbors`, cuyo único algoritmo es KNN), el caso falla al
  construirse en vez de a mitad de `cross_validate_models`.
- `"random_oversample"`: duplica aleatoriamente filas de la clase minoritaria
  del fit hasta igualar la mayoritaria.
- `"random_undersample"`: elimina aleatoriamente filas de la clase
  mayoritaria del fit hasta igualar la minoritaria.

`candidate_evaluation.json` y `model.json` registran la estrategia usada y,
cuando cambia el número de filas, el recuento antes y después. Solo hay
sobre/infra-muestreo aleatorio simple (duplicar/eliminar filas), no SMOTE ni
variantes sintéticas — evita añadir una dependencia nueva.

### Ajuste de hiperparámetros

`tune_hyperparameters` ajusta los hiperparámetros del algoritmo que ya
eligió `select_model` — no compara familias de modelos de nuevo, solo
afina la ganadora. Reutiliza el mismo bucle fold/resampling/fit que
`cross_validate_models` (mismo `StratifiedKFold`/`KFold`, mismo
`class_balancing` aplicado solo sobre el fold de ajuste), así que no
introduce una forma distinta de fuga de información.

`modeling_config.hyperparameter_tuning` tiene **tres** estados, no dos:

- `null` (por defecto): el caso no se pronuncia y la skill plantea una
  decisión analítica **en ejecución**. Ese momento no es un detalle: la
  pregunta solo es respondible con las métricas delante, y ni el ganador ni
  el baseline existen hasta que han corrido `select_model` y
  `baseline_model`. Por eso no cabe en `resolve_missing_case_decisions`
  (decisions.py), que resuelve lo que falta *antes* de compilar el plan; se
  plantea aquí, igual que `handle_missing_values` decide columna a columna
  durante su propia ejecución.

  El enunciado lleva la puntuación compuesta del algoritmo elegido, la
  métrica de referencia del baseline (`f1` en clasificación, `r2` en
  regresión), la diferencia entre ambas y el número de combinaciones que
  costaría la búsqueda. Ajustar se marca como recomendado cuando el margen
  sobre el baseline es fino (`< 0.10`) o cuando no hay baseline: es ahí
  donde afinar puede cambiar la conclusión del caso. Con margen holgado, se
  recomienda no gastar el cómputo.

  Se compara sobre una única métrica y no sobre la puntuación compuesta a
  propósito: en regresión el compuesto mezcla métricas donde más es mejor
  (R²) con otras donde menos lo es (MAE, RMSE), y restar dos compuestos
  daría un "margen" sin significado.

  Antes el valor por defecto era `"none"`, indistinguible de un "no" del
  usuario: el ajuste solo ocurría si alguien lo escribía a mano en el JSON
  del caso, así que en la práctica no se hacía nunca.
- `"none"`: la skill escribe `hyperparameter_search.json` con
  `"applied": false` y no toca nada más — un no-op explícito y trazado, no
  un silencio. No se pregunta: el caso ya decidió.
- `"grid"`: expande `PARAMETER_GRIDS[algoritmo]` (en `model_catalog.py`) con
  `sklearn.model_selection.ParameterGrid` — combinatoria pura, sin ningún
  paso aleatorio — y evalúa cada combinación con validación cruzada sobre
  train. Hay un tope de 24 combinaciones: una rejilla que lo supere falla
  explícitamente en vez de disparar una ejecución de horas sin que nadie lo
  haya decidido conscientemente. Cada algoritmo del catálogo tiene la suya;
  una rejilla vacía significa "sin hiperparámetro relevante que ajustar" y
  produce una única combinación, la de los valores por defecto.

  Las rejillas se declaran con los nombres desnudos del estimador y
  `candidates.parameter_grid()` les añade el prefijo de los envoltorios que
  lleve: `classifier__`/`regressor__` si el preprocesado obligatorio lo metió
  en un `Pipeline`, `estimator__` si es XGBoost dentro de
  `LabelEncodedClassifier`, o ninguno. El prefijo se deduce del estimador ya
  construido, no de una tabla paralela. Escribirlo a mano hacía que envolver
  un algoritmo rompiera su rejilla en silencio: `set_params` recibía una clave
  que ya no existía.

### Ajuste del umbral de clasificación

Toda `classification_metrics` binaria depende de un umbral de decisión
(`probabilidad ≥ umbral → clase 1`); por defecto ese umbral es 0.5. Para
datasets desbalanceados, 0.5 rara vez es el punto que maximiza F1 — puede
haber bastante más margen ajustándolo que ampliando la rejilla de
hiperparámetros.

`cross_validate_models` calcula, para cada candidato de clasificación
**binaria** (no aplica a multiclase, que no tiene un único punto de corte,
ni a regresión), el umbral que maximiza F1 sobre sus predicciones
**out-of-fold** (`skills/modeling/metrics.py::find_best_threshold` —
barrido determinista de 0.01 en 0.01, empate resuelto por cercanía a 0.5).
"Out-of-fold" es la clave: cada fila de train se predice exactamente una
vez, con un modelo que nunca la vio en su ajuste — nunca se usa una
predicción in-sample de train (sobreajustaría el umbral) ni de test (fuga).
Ese umbral viaja por los mismos artefactos que ya lleva la búsqueda de
hiperparámetros: `candidate_evaluation.json["candidates"][alg]
["best_threshold"]` → `model_selection.json["classification_threshold"]`
(candidato ganador) → si `tune_hyperparameters` corrió y aplicó, su propio
`best_threshold` para la combinación ganadora tiene prioridad →
`model.json["classification_threshold"]` → `evaluate_model` lo usa tanto
para las métricas globales de test como para las de cada subgrupo sensible.

El umbral óptimo **no** cambia cómo se comparan/seleccionan los
candidatos: `select_model` sigue puntuando con las métricas a 0.5 (misma
vara de medir para todos), el umbral ajustado solo se aplica al modelo ya
ganador, en su entrenamiento final y evaluación.

`train_model` lee `hyperparameter_search.json` si existe: si `"applied"`
es verdadero y corresponde al mismo algoritmo y dataset que
`model_selection.json`, usa `best_parameters` en vez de los valores por
defecto de `estimator_parameters()`, y dejar constancia en `model.json`
(`hyperparameters_source`, `applied_hyperparameter_overrides`,
`hyperparameter_search_summary`). Si `tune_hyperparameters` no corrió, no
hay artefacto que leer y `train_model` usa los valores por defecto, igual
que siempre.

Los artefactos generados son `candidate_evaluation.json`,
`model_selection.json`, `model.joblib`, `model.json` y `evaluation.json`
(más `deduplicated_dataset.csv`/`deduplication_report.json` si
`deduplicate_rows` se ejecutó, `imputed_dataset.csv`/
`missing_values_report.json` si `handle_missing_values` se ejecutó,
`encoded_dataset.csv`/`categorical_encoding.json` si `encode_categorical`
se ejecutó, `treated_dataset.csv`/`data_treatment.json` si `treat_data` se
ejecutó, `feature_selection.json` si `select_features` se ejecutó, y
`hyperparameter_search.json` si `tune_hyperparameters` se ejecutó). El
binario `model.joblib` se considera un artefacto local de confianza, se
protege mediante su hash y no debe sustituirse por un fichero externo no
verificado.

Tras un `mads run` que entrena y evalúa, la CLI imprime además un resumen
compacto de las métricas de test (las mismas que
`SELECTION_METRICS_BY_TASK_TYPE` del `task_type` del caso), leído
directamente de `evaluation.json` — sin recalcular nada. Si el caso declara
`sensitive_attributes`, añade un puntero a `evaluation.json["by_subgroup"]`
en vez de volcar el desglose completo en terminal.

**`mads run` rechaza por defecto un `--output` que ya contenga una
ejecución** (`trace.jsonl` o `artifacts/` previos) —
`check_output_directory_is_reusable` en `cli.py`. Reutilizar el directorio
no solo acumula varios `case_started` en la misma traza: como
los artefactos conservan estado y linaje de la ejecución anterior, una nueva
ejecución no podría distinguirlos de sus propias entradas. `--force` permite reutilizarlo explícitamente cuando
de verdad se quiere.

`generate_analytical_report` se habilita cuando el agente propone finalizar y
el código comprueba que el objetivo está satisfecho con evidencia vigente. Se
ejecuta después de la decisión humana final. Produce `case_report.md`, que es un informe analítico y no una
certificación de auditoría. Incorpora el plan, las skills ejecutadas, las
aprobaciones humanas y las evidencias de validación, perfilado y evaluación.
Cuando se entrena un modelo, también documenta el algoritmo, los
hiperparámetros, la validación cruzada, la selección, los parámetros aprendidos
y las métricas globales y por subgrupos, indicando que la evaluación usa test.

El informe narra además **todo el trabajo intermedio**: qué se imputó y con
qué criterio, qué se codificó (o por qué la codificación no era aplicable),
qué atípicos se recortaron, eliminaron o conservaron, qué variables descartó
`select_features` y por qué, qué obtiene el baseline y si se ajustaron los
hiperparámetros. Esos seis artefactos existían desde hace tiempo pero el
informe no los leía: el documento describía el dataset de entrada y el modelo
de salida, y todo lo que pasaba en medio solo constaba en los JSON.

La sección del modelo usa las features **efectivas**
(`resolve_effective_features`), no `case.features`. La codificación añade
dummies y la selección descarta columnas, así que el conjunto que entrenó el
modelo casi nunca es el declarado en el caso; con `case.features`, las
guardas `len(coeficientes) == len(features)` fallaban en cuanto la
preparación cambiaba algo y el informe perdía en silencio los coeficientes,
las importancias y los parámetros de estandarización.

Si la selección de una acción falla o una tarea bloqueante interrumpe el flujo antes de
documentar, la trazabilidad técnica se conserva en `trace.jsonl`, aunque no se
genere un informe basado en resultados incompletos.

Después de `case_finished`, ya fuera de las skills analíticas, el orquestador
ejecuta automáticamente el meta-auditor y genera `audit_report.json` y
`assurance_report.md`. Esta separación evita que la propia skill analítica
afirme que una traza todavía abierta ha sido verificada.
