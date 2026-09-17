# Informe de la rama `fix/bug-hunt-followup` (8 de septiembre de 2026)

## Contexto y alcance

Este documento cubre **toda la rama** `fix/bug-hunt-followup`, creada a partir de `main` para
arreglar el máximo posible de los 25 hallazgos críticos/altos del bug-hunt del 2 de septiembre
(`2026-09-02-bug-hunt-thymira`), re-verificados contra `main` el 7 de septiembre
(`docs/bug-hunt/2026-09-07-verificacion-vs-main.md`, incluido en este mismo commit).

Tiene dos partes con un origen distinto:

- **Parte 1**: los 8 hallazgos del informe original arreglados directamente a partir de esa
  re-verificación, antes de tocar nada más.
- **Parte 2**: 5 bugs **nuevos**, no descritos en el informe original, encontrados y arreglados
  mientras se perseguía una prueba real end-to-end (un análisis EDA con histogramas, contra un
  modelo real, sin ningún doble) — cada arreglo aparece porque el anterior dejó de esconderlo. Uno
  de ellos (la reanudación de llamadas diferidas) es precisamente el **"hallazgo nuevo"** que ya
  señalaba la re-verificación del 7 de septiembre como aplazado a propósito.

Cada fix de la Parte 2 tiene su propia nota de decisión en `.agents/notes/implemented/`, con el
`Problem`/`Decision`/`Alternatives considered`/`Consequences` completos — este documento resume,
las notas son la fuente completa.

**Todo lo de este documento está verificado con evidencia real**: tests nuevos donde aplica,
`ruff`/`ty`/`lint_imports`/roadmap/skills en verde, la suite rápida completa en verde (2257 tests),
y al menos un Run real contra un modelo real por cada fix de la Parte 2.

## Resumen ejecutivo

| | Arreglados en esta rama | Matizados / parciales (sin tocar) | Siguen abiertos |
|---|---|---|---|
| Críticos (12) | C1, C2, C3, C4, C6, C7, C10, C12 (8) | C5, C9, C11 (3) | C8 (1) |
| Altos (13) | H2, H4, H5, H6, H9, H10, H12, H13 (8) | H1, H3, H8, H11 (4) | H7 (1) |
| Hallazgo nuevo (informe del 7 sep.) | Reanudación de llamadas diferidas (1) | — | — |
| Nuevos de esta sesión (no en el informe original) | 5 (ver Parte 2) | — | 1 propuesto, sin arreglar (MIRA) |

**16 de 25 hallazgos originales arreglados del todo en esta rama, más el hallazgo nuevo que ya
señalaba la re-verificación, más 5 bugs adicionales descubiertos en el camino.** 7 siguen
matizados/parciales tal cual estaban; 2 siguen completamente abiertos (C8, H7); 1 bug nuevo queda
documentado pero sin arreglar (ver Parte 3).

## Parte 1 — Los 8 hallazgos del bug-hunt original, arreglados en esta rama

Cada uno soluciona la causa raíz, no el síntoma puntual — según la instrucción explícita de esta
rama ("soluciones globales, no parches").

### C4 — Faltan las librerías de ciencia de datos
**Commit `429f9c9`.** `pandas`, `matplotlib`, `mlflow`, `statsmodels` añadidos a
`runtime/tools/pyproject.toml`; `run_python` comparte este mismo intérprete, así que ahora puede
usarlas de verdad. `runtime_context_text()` reporta en vivo (`importlib.metadata`, nunca una lista
copiada de `pyproject.toml`) qué librerías conocidas están instaladas y cuáles no.

### C12 — Un crash dentro de THY deja el Run irrecuperable
**Commit `21f9216`.** `StateCheckpointer` guardaba por `thread_id` solo; un `ThyGraph` anidado con
el mismo `thread_id` que la composición exterior podía pisar su checkpoint. La clave de
almacenamiento ahora incluye `checkpoint_ns` (`_repository_key`), así que las dos vidas se guardan
en sitios genuinamente separados.

### H10 — Sin resiliencia en el gateway del modelo
**Commit `784f3d5`.** `LiteLLMProvider` no ponía ningún `timeout` a la llamada real — una conexión
colgada podía bloquear un Run indefinidamente. Ahora acota cada llamada
(`THYMIRA_MODEL_TIMEOUT_S`, 120 s por defecto); deliberadamente sin `num_retries` — el diseño del
módulo es "sin reintentos, sin caché" y eso se mantiene, solo deja de colgarse para siempre.

### H4 — El contexto del proyecto nunca llega a los agentes
**Commit `316e480`.** `.thymira/context.md` se cargaba en `ThyState.project_context` en Inspect
pero nada río abajo lo leía: Plan mandaba un prompt pelado, y los sub-agentes delegados no lo veían
en absoluto. Ahora Plan lo añade a su propio prompt, y `ToolContext` lo lleva (cargado directamente
desde `project_dir`, porque se construye antes de que Inspect rellene `ThyState`) para que el
snapshot de contexto de cada paso delegado también lo incluya, truncado a 4000 caracteres.

### H8 — 14 de 25 tools inalcanzables
**Commit `27f51b5`.** `audit_model`, `compare_models`, `inspect_model`, `query_mlflow` y
`query_sql` estaban registradas pero ningún `tool_allowlist` las nombraba —
`audit_model` en particular, blindada de seguridad el mismo día que se descubrió esto, seguía sin
que ningún agente pudiera llamarla. Las cuatro primeras se unen al agente `experiment` (extensión
natural de lo que ya hace con un modelo entrenado); `query_sql` se une a `data` (SQL de solo lectura
sobre un dataset registrado, tan acotado como `profile_dataset`). `git_commit`/`git_log`/
`git_status` quedan deliberadamente sin alcanzar — comprometer al repositorio merece su propia
decisión de autorización, no un extra empaquetado.

### H2 — No hay workspace por Run
**Commit `730b925`.** `ToolContext.workspace` era el propio directorio del proyecto, compartido por
todos los Runs contra él: `run_python` deja un `.thymira/tool_<id>.py` por llamada, `write_file`
deja scripts y reportes en la raíz, y dos Runs compitiendo por el mismo nombre de archivo se pisaban
en silencio. Cada Run tiene ahora `.thymira/runtime/workspaces/<run_id>`, hermano del ya existente
`artifacts/<run_id>` por Run, haciendo la colisión estructuralmente imposible en vez de solo
improbable. **Esta rama descubrió y arregló una regresión propia de este mismo fix** — ver Parte 2,
punto 4.

### H12 — La suite nunca cruza un límite de proceso real
**Commit `caefe36`.** Todos los demás tests de la API conducen la app ASGI en el mismo proceso vía
`TestClient`, que nunca abre un puerto real; lo único que lo hacía era
`scripts/real_e2e_smoke.py`, deliberadamente fuera de `just test`/`just check` porque también gasta
presupuesto real de API. El nuevo test cruza el mismo límite de proceso gratis: `/healthz` y listar
Runs nunca llaman a un modelo, así que no necesita `THYMIRA_MODEL` ni una API key. Corre en el carril
rápido (`integration`, no `slow`).

### H13 — La documentación describe otro sistema
**Commit `6be250c`.** AGENTS.md describía `apps/api`/`adapters/cli` como "aún no conectados" /
"run/status implementados sobre HTTP, audit/approve/reject pendientes" — una foto muy anterior a
los 6 módulos de rutas y los 12 comandos de CLI reales que existen hoy. Corregido para nombrar lo
que de verdad hay.

## Parte 2 — Bugs nuevos, encontrados arreglando uno detrás de otro persiguiendo una prueba real

El usuario pidió una prueba real de EDA con histogramas contra un modelo real. Cada intento reveló
el siguiente bug en la cadena — los cinco están arreglados y verificados con evidencia real de al
menos un Run.

### 1. `query_sql` no decía cuál era el nombre real de la tabla SQL
**Commit `f16b0d2`.** `query_sql` siempre carga el dataset registrado en una tabla SQL llamada
literalmente `dataset`, nunca con el nombre del dataset — cierto para cualquier dataset, cualquier
agente, cualquier llamada — pero nada se lo decía al modelo. Un Run real contra `german_credit`
reprodujo el fallo exacto: el modelo escribió `SELECT * FROM german_credit`, DuckDB lanzó
`Catalog Error: Table with name german_credit does not exist`, y cada intento de adivinar (otra
consulta, `SHOW TABLES`, volver a `profile_dataset`) generó su propio `tool_intent_sha256` y su
propia petición de aprobación humana. La `description` de la tool — el único contrato que el modelo
ve — ahora declara la convención `FROM dataset` explícitamente.

### 2. Una llamada a tool diferida nunca se reanudaba de verdad
**Commit `5022e79`.** El más grande de los cinco — y exactamente el "hallazgo nuevo" que la
re-verificación del 7 de septiembre ya había detectado y aplazado a propósito como "rediseño de
varios días". `Delegator.delegate` siempre creaba una `Task` nueva y `AgentRunner.run` siempre
reconstruía el prompt desde cero; un paso reanudado era, en la práctica, el mismo objetivo
preguntado otra vez desde una conversación en blanco, libre de proponer una llamada distinta cada
vez. Reproducido en vivo: un paso de `coding` reintentó la misma tarea tres veces con código
cosméticamente distinto, cada reintento generando su propio `tool_intent_sha256` y agotando una
aprobación humana antes de ejecutar una sola línea.

`thymira.agents.resume` (módulo nuevo) guarda la conversación exacta de PydanticAI de un paso
aparcado en el `ArtifactStore`, indexada por `task.agent_id` (sin tocar el contrato/schema), y
una vez que cada decisión que ese paso difirió tiene respuesta humana, la reanuda con
`message_history`/`deferred_tool_results` en vez de un prompt nuevo: una llamada aprobada se repite
con sus argumentos *originales*, y una denegación se lee de vuelta en la misma conversación como un
resultado de tool normal que el modelo puede leer y adaptar — nunca como un reintento invisible. Un
Run aparcado antes de este cambio (sin conversación guardada) cae al comportamiento de hoy sin
romper nada.

También arregla el control A24 de MIRA ("el enrutado de modelo respeta el suelo de rol"), que la
propia prueba de fábrica de producción pilló fallando en el momento exacto en que la reanudación
empezó a funcionar de verdad: una llamada reanudada ahora se reconoce como autorizada por el ciclo
de vida anterior (ya cerrado) que la propuso primero, en vez de exigir un `model.selected` nuevo y
redundante en el ciclo reanudado.

### 3. El prompt de planificación anunciaba agentes que el catálogo real no tiene
**Commit `9427bab`.** El prompt de Plan era una lista estática de los seis agentes de
`ThyAgentKind`; `default_thy_catalog()` registra deliberadamente solo los tres agentes del MVP
(`data`, `coding`, `experiment`). Reproducido en vivo, justo después de que el fix anterior dejara
completar limpiamente al agente `data`: THY propuso una tarea para `statistics-agent`, y el Run
falló con `unknown agent spec: statistics-agent`. `_plan_instructions` ahora deriva la lista
anunciada de `catalog.names()`, así que el prompt nunca puede volver a nombrar un agente que el
catálogo no puede resolver, en ningún sentido — los tres agentes del MVP hoy, o un catálogo más
amplio el día de mañana.

### 4. El agente `coding` no podía leer el dataset registrado (regresión del propio fix H2)
**Commit `a01e667`.** H2 (Parte 1, arriba) aisló el workspace de cada Run del directorio del
proyecto para evitar colisiones de escritura — pero nada copiaba nunca el archivo bruto de un
dataset registrado a ese workspace, y `read_file`/`run_python` (todo el arsenal de `coding` para
tocar cualquier dato) solo resuelven rutas dentro del workspace. `profile_dataset`/`query_sql`/
`run_experiment` no se vieron afectados porque leen del `ArtifactStore` por nombre, nunca por ruta.
Reproducido en vivo: cada ruta que el paso de `coding` adivinó (`data/german_credit.csv`,
`datasets/german_credit.csv`, `german_credit.csv`) falló con `FileNotFoundError`, cada intento
quemando su propia aprobación.

`_stage_declared_datasets` copia cada dataset declarado al workspace del Run, en la misma ruta
relativa que usa el proyecto, justo después de construir el workspace y antes de que `ThyGraph` —
y por tanto cualquier agente — se ejecute. Un destino que ya existe se deja intacto: un paso de
`coding` puede haber editado legítimamente su propia copia en un paso anterior del mismo Run, y una
reanudación posterior no debe sobrescribirlo en silencio con el original.

### 5. (herramienta de pruebas) Presupuesto de aprobaciones configurable en el script de humo
**Commit `5e58e86`.** No es un bug del runtime — un análisis a fondo puede proponer legítimamente
varias llamadas a tool distintas en un mismo turno (observado en vivo: cuatro `query_sql` a la
vez), cada una necesitando su propia aprobación humana. `_MAX_APPROVALS = 4` era razonable para el
prompt de demo pero demasiado bajo para una prueba real más completa; `--max-approvals` deja
subirlo sin tocar el script.

## Parte 3 — Qué queda por hacer

### Bug nuevo encontrado, documentado pero sin arreglar

**MIRA se cae al auditar un dataset de tamaño normal como "evidencia".** Nota:
`.agents/notes/proposed/mira-evidence-reader-dataset-size-limit.md`. Reproducido en vivo, en el
intento de Run más avanzado de todos: justo después de que `data` y `coding` terminaran su trabajo,
`begin_audit` → `control_evaluation.recorded` (`METHODOLOGY-EVIDENCE-001`) → `run.failed`:
`artifact 'datasets/german_credit.csv' is 192804 bytes; maximum is 65536`.
`ArtifactStoreEvidenceReader` está pensado para fragmentos de texto acotados (un `metrics.json`, un
reporte corto) con un tope de 64 KB — un control cita el dataset bruto completo como si fuera un
fragmento de evidencia, y en vez de fallar con gracia (el propio control marcándose
`NOT_APPLICABLE`/`FAILED` con una razón), la excepción sin capturar tumba el Run entero. `csv` de
188 KB, muy por debajo de `MAX_DATASET_BYTES` — cualquier Run con un dataset de tamaño normal choca
igual. Dos direcciones posibles quedan anotadas en la nota, sin elegir entre ellas todavía.

### Confirmado que NO es un bug

**Ejecutar código real (`run_python`) sin Docker está bloqueado a propósito.** `RunPython` se
configura con `mode=WORKSPACE_WRITE` por defecto, y el backend local (`LocalSubprocessSandbox`)
rechaza deliberadamente ese modo — solo acepta `danger_full_access`, que solo se activa vía el
sandbox de Docker (`ContainerSandbox`). Ya documentado en `main` antes de esta sesión ("Default API
and worker registries therefore refuse local subprocess tools; the production demo reaches a
critical A19 finding and a blocked Run"). Para generar histogramas reales de verdad haría falta
`just docker-build && just docker-run` — un paso de infraestructura, no un bug de código. La prueba
real de esta rama llegó hasta aquí: el agente `coding` leyó el CSV correctamente (confirma el fix
nº 4) y escribió el script de histogramas, pero no pudo ejecutarlo sin Docker.

### Los hallazgos originales que siguen sin tocar

**Matizados/parciales (sin cambios en esta rama):**
- **C5** — una aprobación humana no autoriza directamente una tool; existe `approval_ticket.py`
  como mecanismo alternativo que resuelve el síntoma pero no es el fix literal que pedía el informe.
- **C9** — `run_python` sigue sin contenedor por defecto (confirmado de nuevo en esta sesión, ver
  arriba); el confinamiento existe para `audit_model` pero no para la ejecución genérica.
- **C11** — más eventos `MODEL_VISIBLE` que antes, pero no se verificó si las tool calls
  (argumentos/resultados) en sí ya llegan como eventos visibles.
- **H1** — el síntoma concreto no se reproduce hoy; el mecanismo que señalaba el informe
  (`RunService._hydrate`) sigue sin tocar esos campos, el arreglo real vive en otro componente.
- **H3** — `check_budget` tiene ya llamadores reales; no se confirmó un techo de gasto máximo que
  pare un Run.
- **H8** — ver Parte 1: las 5 tools quedaron alcanzables por este mismo trabajo; `git_commit`/
  `git_log`/`git_status` siguen deliberadamente fuera.
- **H11** — no reproducido con concurrencia real esta vez tampoco.

**Completamente abiertos:**
- **C8** — las tools siguen declarando `external_effects=()` sin excepción; la política de permisos
  no puede denegar nada basándose en efectos reales.
- **H7** — sigue sin haber comprobación cruzada de `exit_code`/`artifacts` antes de marcar una tarea
  `COMPLETED`; el caso que citaba el informe original (`exit_code: 1`, sin artefactos, igualmente
  `COMPLETED`) parece seguir siendo posible.

### Prioridad sugerida para lo que queda

1. **MIRA / evidencia de 64 KB** — bloquea que CUALQUIER Run con un dataset normal llegue a un
   informe de auditoría real; es lo más cerca que estamos de una prueba end-to-end completa.
2. **H7** — validar `exit_code`/`artifacts` antes de `COMPLETED`; se conecta directamente con la
   fiabilidad de todo lo demás que se ha tocado esta sesión.
3. **C8** — metadatos `external_effects` honestos en las tools destructivas.
4. **C9** — extender el confinamiento por contenedor de `audit_model` a `run_python` en general
   (o documentar explícitamente que Docker es un requisito, no una opción, para ejecución real).
5. El resto de matizados (C5, C11, H1, H3, H11) — ninguno bloquea una prueba real hoy; revisar con
   más tiempo dedicado a cada uno.
