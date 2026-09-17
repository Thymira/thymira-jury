# Trazabilidad y auditoría

La trazabilidad es la parte más importante del proyecto: todo lo demás
existe para que la traza sea completa y verificable.

## Qué se registra (y con qué evento)

| Pregunta de auditoría | Evento(s) en `trace.jsonl` |
|---|---|
| ¿Qué objetivo recibió el sistema? | `case_started` (caso completo + policy pack + catálogo) |
| ¿Qué riesgo declaró el usuario? | `case_started.case.risk_level` (entrada no autoritativa) |
| ¿Quién inició el caso y con qué rol? | `case_started.initiated_by` + `actor_context_established` |
| ¿Qué proveedor inició la clasificación? | `risk_classification_started` (provider, model, revision) |
| ¿Qué riesgo y categoría clasificó el sistema? | `risk_assessed` (assessment, estado, inconsistencias y solicitudes de información) |
| ¿Qué contexto adicional se solicitó y aportó? | `risk_information_provided` o `risk_information_unresolved` + nueva revisión de `risk_assessed` |
| ¿Falló la clasificación antes de planificar? | `risk_classifier_failed` + `case_finished(status=failed)` |
| ¿Cómo se autorizó el catálogo? | `skill_authorizations` (policy pack, riesgo clasificado y veredictos) |
| ¿Qué estado y acciones observó el agente? | `agent_state_observed` (resumen, disponibles y motivos de exclusión) |
| ¿Qué acción seleccionó el LLM? | `agentic_step_proposed` (acción, parámetros, motivo o finalización) |
| ¿Qué skills se ejecutaron? | `worker_started` (id, versión, herramientas, productos y fingerprints) |
| ¿Qué tareas se ejecutaron y en qué orden? | secuencia de `worker_started`/`worker_finished` (campo `seq`) |
| ¿Qué datos/herramientas/artefactos se usaron? | `case_started` (dataset), `worker_started` (allowed_tools), `worker_finished` (artifacts) |
| ¿Con qué clave y versiones terminó cada paso? | `agentic_step_recorded` |
| ¿Qué criterios y evidencias justificaron cada decisión? | `justification.criteria` + `evidence_refs` + `manifest.json` |
| ¿Qué políticas se aplicaron? | `skill_authorizations` + `gate_decision` (rule_id, policy pack y evaluación recalculada) |
| ¿Se requirió y resolvió revisión por la clasificación? | `human_review_required` + `approval_requested` + `human_response` + `human_review_resolved` |
| ¿Qué aprobaciones/rechazos humanos hubo? | `approval_requested` + `human_response` |
| ¿Qué preferencias analíticas se solicitaron y aplicaron? | `analytical_decision_requested` + `analytical_decision_answered` + `analytical_decision_applied` |
| ¿Qué modelo y proveedor LLM participaron? | `llm_call` (provider, model) |
| ¿Qué prompts se usaron? | `llm_call.prompt_redacted` (tras `utils.redact`) |
| ¿Qué respondió el LLM? | `llm_call.output_redacted` (también redactado) |
| ¿Qué resultados, errores y costes hubo? | `worker_finished`, `llm_call` (tokens, coste), `case_finished` |

`case_started` conserva localmente el `Case` completo para poder auditar la
entrada. El selector remoto recibe el objetivo, un resumen compacto del estado,
evidencias, acciones disponibles e historial reciente. No recibe las filas del
dataset ni puede conceder permisos.

Las llamadas de clasificación se distinguen mediante
`llm_call.purpose = risk_classification`. Structured Outputs acredita el formato
de la respuesta, no su corrección semántica. `risk_assessed.inconsistencies`
registra las comprobaciones deterministas posteriores, incluida toda
infradeclaración (`minimal` → `elevated/high`) o diferencia extrema, que
siempre exige revisión humana sin intentar
validar por heurísticas la justificación libre. La confianza trazada es
orientativa y no calibrada; no equivale a un permiso.

Un `risk_classifier_failed` cierra el caso sin fallback a riesgo mínimo o a
un doble privado de test. No debe aparecer después ningún `agent_state_observed` ni
`worker_started`.

`case_finished` incluye además el acumulado de todas las llamadas LLM del
caso: `llm_call_count`, `total_input_tokens`, `total_output_tokens` y
`total_cost_usd`. Así no hace falta recorrer la traza para saber cuánto
costó un caso. El coste por llamada lo calcula LiteLLM
(`llm/litellm_provider.py`, `litellm.completion_cost`/`litellm.model_cost`
— ver [`docs/litellm.md`](litellm.md)); si el modelo usado no tiene precio
mapeado, `cost_estimate_usd` (por llamada) y `total_cost_usd` (del caso)
quedan en `None` en vez de inventar un número. `llm_call.metadata` guarda
además `model_requested` (lo solicitado) junto al `model` de la respuesta
(lo realmente respondido), y `cost_known`/`cost_source` para que quede claro
de dónde sale la cifra de coste.

**Nunca se guarda chain-of-thought.** La justificación es estructurada:
`decision_code` + `criteria` + `alternatives_considered` + `evidence_refs`.
Es verificable contra artefactos; un monólogo del modelo no lo sería.

## La cadena de hashes

Cada evento incluye:

```
hash = sha256( json_canónico(evento_sin_campo_hash) )
prev_hash = hash del evento anterior (o 64 ceros en el primero)
```

El JSON canónico (claves ordenadas, sin espacios) garantiza que el mismo
contenido produce siempre el mismo hash. Consecuencias:

- **Editar** una línea → su hash ya no coincide → inválida.
- **Borrar** una línea → el `prev_hash` del siguiente no casa → inválida.
- **Insertar** una línea → rompe secuencia y encadenado → inválida.
- **Truncar por el final** → la cadena sigue siendo válida, pero el caso
  queda sin `case_finished` y el meta-auditor lo señala (A2).

`mads verify <run_dir>` recomputa toda la cadena. Es O(n) y sin estado:
cualquier tercero puede ejecutarlo y llegar al mismo resultado.

Los artefactos siguen la misma filosofía: `artifacts/manifest.json` guarda
el sha256 de cada fichero producido, y `ArtifactStore.verify()` los
recomprueba.

## El meta-auditor (A1–A18)

Al cerrar un caso, el orquestador ejecuta automáticamente las comprobaciones
deterministas sobre la traza **ya escrita**. `mads audit <run_dir>` permite
repetirlas manualmente y de forma independiente:

| Código | Severidad | Detecta |
|---|---|---|
| A1 | critical | cadena de hashes inválida (traza manipulada) |
| A2 | warning | caso sin `case_finished` (crash o corte) |
| A3 | critical | worker ejecutado sin `gate_decision` previa |
| A4 | critical | worker ejecutado tras un veredicto `blocked` |
| A5 | critical | evidencias ausentes o artefactos modificados (doble verificación: eventos + manifest) |
| A6 | warning | worker que intentó usar herramientas no concedidas |
| A7 | warning | pregunta al humano sin respuesta registrada |
| A8 | critical/warning | decisión analítica sin respuesta; es crítica cuando bloqueaba el análisis |
| A9 | critical | acciones iniciadas o terminadas sin su ciclo completo, duplicadas o fallidas |
| A10 | critical | evidencias prometidas, declaradas y registradas en el manifest que no coinciden |
| A11 | critical | solapamiento, duplicados, huecos, índices inválidos o recuentos incoherentes en train/test |
| A12 | critical | linaje incoherente entre split, candidatos, selección, modelo y evaluación |
| A13 | critical/warning | selección del modelo que no coincide con el recálculo independiente o cuya estrategia no puede verificarse |
| A14 | critical | acceso anticipado a test, uso de una partición incorrecta o acceso fuera de la tarea autorizada |
| A15 | critical | selección o `worker_started` sin `risk_assessed` previo, o un worker ejecutado después de `risk_classifier_failed` |
| A16 | critical | decisión del Gate que no coincide al reevaluar la política exacta congelada al iniciar el caso |
| A17 | warning | procedencia insuficiente o incoherente del caso, dataset, código, dependencias, catálogo o semillas |
| A18 | critical | hechos del informe analítico que no coinciden con los artefactos de origen |

Es determinista a propósito (reglas, no LLM): un auditor debe ser él mismo
explicable. Y corre a posteriori: nunca decide en caliente ni bloquea.

El orden de cierre es deliberado:

1. `generate_analytical_report` produce `case_report.md`.
2. `case_finished` cierra la cadena de eventos.
3. Se verifica la integridad completa de `trace.jsonl`.
4. El meta-auditor genera `audit_report.json`.
5. Se genera `assurance_report.md` con el estado de controles, hashes,
   hallazgos y situación de la revisión experta.

Los dos documentos de aseguramiento se guardan en la raíz de la ejecución y
no dentro de `artifacts/`: incluirlos en el manifest auditado produciría una
dependencia circular. Si no existe un `expert_review.json` válido, el informe
lo declara expresamente y no afirma que haya revisión experta completada.

La salida de `mads audit` no se limita a enumerar hallazgos. Incluye el
estado global (`passed`, `passed_with_warnings` o `failed`) y un resultado por
control (`passed`, `failed`, `not_applicable` o `not_evaluated`), junto con las
referencias a las evidencias utilizadas. Los controles A11–A18 contrastan la
coherencia a partir de los artefactos; no confían en un indicador de éxito
escrito por la misma skill auditada.

Las ejecuciones nuevas conservan además `policy_snapshot.json`,
`run_environment.json` y `report_facts.json`. El acceso real a train/test se
registra como `dataset_partition_accessed` desde el cargador central de
particiones. No se añaden controles sin un riesgo concreto y una evidencia
independiente que justifique su mantenimiento.

El código se separa en `audit/meta_auditor.py` (coordinación),
`audit/extended_controls.py` (A14, A16, A17 y A18) y
`audit/provenance.py` (captura del entorno). Esta separación no altera la API
de `mads audit`, pero permite probar y evolucionar las familias de controles
sin acoplarlas al núcleo.

## Cómo ampliar la traza

Para registrar algo nuevo: `trace.event(tipo, actor, payload)`. Reglas:

1. El `actor` es siempre uno de: `system`, `worker:<tipo>`, `human`,
   `llm:<proveedor>`.
2. Si el payload contiene texto que pudo tocar datos de usuario o prompts,
   pásalo por `utils.redact` ANTES de trazar.
3. Si añades un tipo de evento con implicaciones de gobernanza, añade la
   comprobación correspondiente al meta-auditor y su test.

## Clasificación, decisiones analíticas y autorizaciones

La clasificación responde qué riesgo describe el contexto y produce un
`RiskAssessment`; no devuelve permisos ni `Verdict`. El policy pack usa ese
assessment para calcular autorizaciones deterministas. Finalmente, el Gate las
recalcula antes de ejecutar desde el `RiskAssessment` con el que fue construido;
no acepta otro nivel en `check()` ni consulta `case.risk_level`.

El `PolicyGate` responde si una acción está permitida, necesita aprobación o
debe bloquearse. Las decisiones analíticas resuelven cómo realizar el trabajo:
por ejemplo, comparar candidatos o validar un algoritmo concreto. Son circuitos
separados para que una preferencia técnica nunca pueda interpretarse como una
autorización regulatoria.

Cada decisión analítica conserva la pregunta, el motivo, las alternativas y
sus consecuencias, la opción aplicada, la identidad y rol declarados, la
justificación y si procedía de una persona o de un valor automático. Su copia
verificable se guarda en `artifacts/analytical_decisions.json`.

La CLI no inicia un caso sin `actor_id` y `actor_role`: los toma de los
parámetros o los solicita de forma interactiva. En esta fase son datos
declarados y no autenticados, por lo que permiten atribución experimental pero
no una segregación de funciones efectiva. La autorización continúa dependiendo
del Policy Gate; una fase empresarial deberá obtener identidad y roles desde un
proveedor corporativo confiable.
