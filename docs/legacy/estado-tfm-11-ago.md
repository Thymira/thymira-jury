# Estado del TFM — 11 de agosto de 2026

Resumen para la sincronización de equipo. Datos verificados directamente contra
el repositorio remoto en el momento de escribir esto (no son de memoria).

## 1. Componentes que ya existen y funcionan

**Núcleo de gobernanza (estable, en `main`):**
- `Orchestrator` — planifica un caso completo una vez y lo ejecuta.
- `PolicyGate` — autorización determinista de acciones (reglas AML-002, AML-003...).
- `Trace` — traza en `trace.jsonl` con cadena de hashes, verificable.
- `ArtifactStore` — artefactos con manifiesto SHA-256.
- Meta-auditor con controles A1-A16 (ampliándose ahora mismo con A15, ver más abajo).
- Catálogo de 34 algoritmos, 7 familias, preprocesado obligatorio por algoritmo,
  selección jerárquica (familia → algoritmo).

**Skills del pipeline:**
- Preparación: `validate_data`, `profile_data`, `deduplicate_rows`,
  `handle_missing_values`, `encode_categorical`, `engineer_features`,
  `treat_data`, `select_features`, `propose_features`.
- Modelado: `baseline_model`, `cross_validate_models`, `select_model`,
  `tune_hyperparameters`, `train_model`, `evaluate_model`.
- Informe: `generate_analytical_report`.

**Decisiones analíticas** (columna a columna, resueltas por LLM sin supervisión
o LLM con confirmación humana obligatoria; automático queda interno para tests).

**Coste y trazabilidad real** para OpenAI y Anthropic (tokens, USD, por llamada).

**Clasificación de riesgo** (`risk.py`, Lafuente) — ya integrada en Orchestrator/
Gate/Planner en `dev` (ver sección de ramas).

**Orquestador agéntico** (LangGraph, prototipo) — camino alternativo que decide
un paso cada vez en vez de planificar todo de una vez.

## 2. Cómo funciona una ejecución — todas las combinaciones posibles hoy

`mads run <caso.json> --output <dir> [opciones]`

| Dimensión | Opciones |
|---|---|
| Orquestador | `normal` (planifica todo una vez) / `agentic` (LangGraph, paso a paso) |
| Proveedor LLM | `openai` (predeterminado) / `anthropic`; dobles solo en tests |
| Quién resuelve decisiones analíticas | `llm-confirm` (predeterminado: LLM propone, humano confirma) / `llm` (experimental, sin supervisión); `automatic` queda interno para tests y CI |
| Aprobación de acciones del Gate | por consola (defecto) / `--auto-approve` (sin intervención, solo pruebas) |
| Configuración de modelado | declarada en el JSON del caso / por flags CLI (`--task-type`, `--algorithm-family`, `--algorithm`) / sin especificar (se pregunta en `normal`, se usa el defecto sin preguntar en `agentic`) |

Diferencia clave entre orquestadores: el `normal` pregunta 4 decisiones de caso
(tipo de problema, familia de algoritmos, estrategia, métrica) si no las
declaras; el `agentic` **nunca** las pregunta — si no las declaras, usa
siempre los valores por defecto (clasificación binaria, todo el catálogo,
métrica compuesta) en silencio. Confirmado en 3 datasets reales (Titanic,
fraude, crédito alemán): el agéntico siempre comparó el catálogo entero salvo
que se le forzara el algoritmo por JSON.

## 3. Estado real de las ramas (verificado ahora mismo, no es una foto vieja)

| Rama | Commits sobre `main` | Última actividad | Estado |
|---|---|---|---|
| `main` | — | base estable | referencia |
| `dev` (Lafuente) | +7 | **hoy, 12:51** | activa — la más grande estructuralmente |
| `cgt-integracion-langgraph` (Carlos G.) | +10 | hoy, 18:25 | activa — sin conflicto con `main`, **sí con `dev`** |
| `adrian-cambios` (Adrián + Mario) | +4 | **hoy, 17:38** | activa — Mario añadiendo cosas ahora mismo |
| `cgt-skills-preparacion-datos` | +0 | 7 ago | ya fusionada / obsoleta, sin nada nuevo |
| `mario-cambios-skills` | +0 | 5 ago | ya fusionada / obsoleta |
| `mario-skills` | +0 | 7 ago | ya fusionada / obsoleta |

Las 3 últimas no aportan nada pendiente — se pueden ignorar (o borrar, si
alguien quiere limpiar el repo).

### Qué trae cada rama activa

**`dev` — integración de riesgo y autorización** (51 archivos, +3622/-683 vs `main`):
Conecta `Case → RiskClassifier → RiskAssessment → autorizaciones → catálogo
anotado → Planner → validación → Gate → workers`. Cambia la firma de
`PolicyGate.__init__` y de `CaseOutcome`. Toca `orchestrator.py` (+426),
`gate.py` (+291), `policies.py` (+271), `cli.py`, `contracts.py`, y añade
`risk_metadata` a las 17 skills.

**`cgt-integracion-langgraph` — orquestador agéntico + robustez de decisiones**
(21 archivos, +2059/-156 vs `main`): `AgenticOrchestrator` (LangGraph),
fusión del diseño de `treat_data` (3 opciones: winsorizar/eliminar/conservar),
arreglo de una fuga real en la validación cruzada, arreglo de un bug real con
Anthropic (`<UNKNOWN>` como opción inválida), `LLMConfirmedDecisionResolver`,
suite paralelizada con `pytest-xdist`.

**`adrian-cambios` — perfil condiciona el pipeline + coste OpenAI** (34
archivos, +2655/-208 vs `main`): scope de `profile_data` acotado a columnas
declaradas, descarte de identificadores en `select_features`, decisión de
`hyperparameter_tuning` en ejecución, narración ampliada en el informe, y
(añadido hoy por Mario) estimación de costes para modelos OpenAI + casos de
ejemplo con Titanic.

### Conflictos reales ya verificados (no especulación)

- **`cgt-integracion-langgraph` × `dev`**: si se fusiona `dev` sin tocar
  `agentic.py`, el `AgenticOrchestrator` se rompe con `TypeError` (cambia la
  firma de `PolicyGate.__init__`) y hay un bug silencioso en `CaseOutcome`
  (dos campos nuevos insertados a mitad de la tupla posicional). Arreglo
  estimado: 90-130 líneas nuevas en `agentic.py` + reconciliar `cli.py`,
  `contracts.py`, `policies.py`/`gate.py`. ~2-3 horas de trabajo cuidadoso.
- **`cgt-integracion-langgraph` × `adrian-cambios`**: los dos rediseñaron
  `treat_data` el mismo día, por separado, con diseños distintos. Ya está
  **resuelto** dentro de mi rama (fusioné las dos ideas: 3 opciones de Adrián
  + el gate corregido + mi arreglo de la fuga en la CV, que la versión de
  Adrián no tenía). Cuando se fusione, mi `treatment.py` ya cubre lo de los
  dos — solo hace falta que alguien lo confirme con Adrián/Mario.
- **`dev` × `adrian-cambios`**: **verificado con `git merge-tree`, hay
  solapamiento real en 12 archivos de código** (`treatment.py`,
  `contracts.py`, `cli.py`, `planner.py`, `reporting.py`,
  `feature_selection.py`, `feature_proposal.py`, `encoding.py`,
  `preparation.py`, `tuning.py`, `tabular.py`, `extended_controls.py`) más
  varios tests y `docs/skills.md`/`docs/openai.md`. Es el par de ramas con
  más fricción esperada de las tres, más incluso que con la mía. Refuerza
  fusionar `dev` primero: los cambios de Adrián/Mario son sobre todo
  contenido de skills, más fácil de reconciliar sobre un `main` ya
  actualizado que al revés.

## 4. Orden de fusión recomendado

**1º `dev` → `main`.** Es la rama que toca los cimientos (`Orchestrator`,
`Gate`, `contracts.py`) de la forma más profunda. Si se fusiona la última,
todo el mundo que dependa de esos archivos tiene que rehacer su reconciliación
otra vez contra ella. Fusionarla primero significa que las demás ramas solo
tienen que reconciliarse una vez, contra un `main` ya estable.

**2º `adrian-cambios` → `main` (post-`dev`).** Reconcilia el cambio de
`PolicyGate`/`CaseOutcome` de `dev` con el trabajo de perfilado/`treat_data`/
coste OpenAI. Como mi `treatment.py` ya fusiona el diseño de Adrián, en este
punto convendría que Adrián/Mario revisen si mi versión (en
`cgt-integracion-langgraph`) les vale como base en vez de la suya, para no
reconciliar `treat_data` una tercera vez.

**3º `cgt-integracion-langgraph` → `main` (el último).** Necesita
reconciliarse con `dev` de todas formas (el `TypeError` del Gate), así que
hacerlo al final significa reconciliar una sola vez, contra un `main` que ya
tiene `dev` y `adrian-cambios` dentro, en vez de perseguir tres ramas móviles
a la vez.

**Antes de nada:** alguien debería revisar rápido si `dev` y `adrian-cambios`
chocan entre sí en `orchestrator.py`/`treatment.py` — no lo he verificado a
fondo y podría cambiar este orden.

## 5. Lo que NO está hecho / decisiones pendientes de equipo

- La memoria del TFM está poco avanzada — es el mayor riesgo de plazo ahora
  mismo (entrega semana del 19 de septiembre), no el código.
- Nadie ha coordinado explícitamente quién fusiona qué ni cuándo — las 3
  ramas activas llevan commits de **hoy mismo**, así que la ventana para
  coordinar esto se cierra rápido cuanto más se tarde.
- Ideas discutidas pero sin construir (fuera de alcance salvo que el equipo
  decida lo contrario): skill de visualización de variables, explicabilidad
  (SHAP), métricas de equidad formales, RAG normativo, un agente de mejora de
  modelo (bucle reactivo tras `evaluate_model`). Ninguna es requisito.
- "Convertir esto en producto" (login, base de datos, API, frontend) —
  **no es requisito del TFM**, descartado del plan de las próximas semanas.
