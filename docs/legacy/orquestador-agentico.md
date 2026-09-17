# Orquestador LangGraph

`AgenticOrchestrator` es la única forma de ejecutar un caso en MADS. La CLI
`mads run` lo construye siempre y no ofrece una selección de modo.

## Bucle de ejecución

Cada vuelta del `StateGraph` realiza este ciclo:

```text
observar estado y artefactos vigentes
        ↓
filtrar skills por fase y calcular acciones técnicamente disponibles
        ↓
LLM elige una sola acción o propone finalizar
        ↓
validar acción y parámetros
        ↓
clasificador de riesgo + Policy Gate
        ↓
ejecutar una skill en un worker temporal
        ↓
actualizar versiones, linaje e invalidaciones
        └───────────────────────────────↺
```

El selector no crea un plan completo. Recibe el objetivo, la fase actual, un resumen compacto
del estado, las evidencias vigentes, las acciones disponibles y las últimas
acciones. Su respuesta estructurada contiene `action`, `reason`, `parameters`
y `done`; también puede solicitar una reapertura controlada con `reopen_phase`.
El código rechaza una skill que no esté en `available_actions` y
nunca interpreta una respuesta del modelo como un permiso.

Las pruebas utilizan un doble determinista privado sobre el mismo contrato y
grafo. No está disponible como proveedor de una ejecución real.

## Estado, linaje y vigencia

`AgentState` registra la fase y revisión analítica, además de la versión y fingerprint del dataset, variables, split,
configuración de modelado y modelo entrenado; también conserva el historial de
acciones, versiones de dataset, artefactos vigentes e invalidados, la última
observación y el motivo de terminación.

Cada artefacto vigente guarda los fingerprints de sus entradas y una clave de
ejecución estable. Un fichero presente no es evidencia suficiente: debe ser
compatible con el estado actual. Al sobrescribir un nombre estable, la versión
anterior se archiva bajo `.history/`; invalidar cambia su vigencia, no elimina
el histórico.

El dataset actual se obtiene del estado y de su linaje. Una transformación que
materializa un CSV nuevo lo convierte en la versión vigente; no existe una
prioridad por nombres como `treated` o `encoded`. Por ello son válidos tanto
`treat_data → encode_categorical` como `encode_categorical → treat_data` si sus
condiciones locales se cumplen.

## Disponibilidad, autorización y repetición

El registro asocia cada skill con un `SkillContract`: requisitos técnicos,
aplicabilidad, productos, partes del estado modificadas, invalidaciones,
mutaciones y regla de repetición. `calculate_available_actions()` evalúa estos
contratos y el estado de forma determinista. No hay un orden fijo entre las
skills de una fase, pero las fases cerradas dejan de estar disponibles.

## Fases y retroceso controlado

El ciclo de vida normal es:

```text
UNDERSTANDING → PREPARATION → MODELING → EVALUATION → REPORTING
```

Un objetivo descriptivo puede saltar de `UNDERSTANDING` a `REPORTING`. El LLM
decide qué skill usar dentro de la fase y cuándo solicitar su cierre; un gate
determinista comprueba las evidencias mínimas antes de avanzar. Las fases tienen
un límite propio de iteraciones además del cortacircuitos global.

Desde modelado, evaluación o reporting, el selector puede pedir reabrir `PREPARATION` o
`MODELING`. El retroceso exige un motivo y aprobación, tiene un máximo de
reaperturas y genera una nueva revisión analítica. Los artefactos posteriores
quedan invalidados, se conserva su historial y las skills pueden recalcularse
bajo la nueva revisión. Un cambio externo que elimina la validación vigente
fuerza de forma determinista el retorno a `UNDERSTANDING`.

Las responsabilidades permanecen separadas:

- las dependencias y la aplicabilidad determinan qué acción se puede intentar;
- el LLM elige entre esas acciones;
- el clasificador calcula el riesgo aplicable;
- el Gate devuelve `allow`, `needs_approval` o `blocked`.

Una acción ejecutada se identifica por skill, fingerprints de entrada,
parámetros y revisión analítica. La misma clave no puede repetirse, pero una
skill vuelve a estar disponible si cambian sus entradas, parámetros o existe
una reapertura autorizada.

## Modelado y finalización

Las skills de modelado dependen de `model_ready`, no de una cadena universal
de preparación. Esta condición comprueba split, target, variables efectivas,
representación numérica aceptable, nulos, compatibilidad de fingerprints y
suficiencia de muestras y clases.

Cuando el LLM propone finalizar, el código verifica que el objetivo tenga
evidencia vigente y que no exista una inconsistencia conocida. Solo entonces
habilita `generate_analytical_report`, que también pasa por el Gate. El estado
distingue finalización correcta, bloqueo de autorización, bloqueo por datos o
configuración, fallo técnico y límite de iteraciones.

Las pruebas de comportamiento están en `tests/test_agentic_orchestrator.py` y
`tests/test_agentic_state.py`.

## `AgenticOrchestrator` como coordinador delgado

`AgenticOrchestrator.run_case()` no implementa el bucle descrito arriba: lo
ensambla a partir de ocho componentes pequeños e inyectables, cada uno con
una sola responsabilidad y testeable por separado sin levantar el grafo
completo. La separación de seguridad no cambia — **el selector propone, el
Gate autoriza, el coordinador de ejecución ejecuta y el servicio de
transición actualiza estado** — solo deja de estar todo entrelazado en un
único método:

```text
RunLifecycle            abre Trace/ArtifactStore/entorno, cuenta coste LLM,
                         cierra el caso (temprano o al final del grafo)
RiskCoordinator          clasifica riesgo, resuelve información faltante,
                         gestiona la revisión humana inicial
ActionAvailabilityService  envoltorio inyectable sobre actions.py
                         (calculate_available_actions, observe_state)
ActionSelector           prompt + schema restringido + llamada al LLM +
                         validación de una única acción existente
AuthorizationCoordinator único punto que habla con PolicyGate
ExecutionCoordinator     crea Task/SkillContext, ejecuta el SkillWorker
                         efímero, normaliza resultado/errores
StateTransitionService   aplica el resultado exitoso al AgentState
PhaseCoordinator         valida cierre de fase, avanza, reabre e invalida
                         evidencia posterior
```

Sigue habiendo **un único orquestador y un único flujo LangGraph** de tres
nodos (`observe_and_select` → `gate_check` → `execute_action`); estos
componentes no son agentes ni sub-orquestadores por fase, son piezas
internas que `run_case` ensambla y cuyas dependencias se inyectan por
constructor. `ActionSelector` en particular no tiene acceso a workers, a un
`ArtifactStore` mutable ni a `PolicyGate`: una selección del LLM inválida
(schema incorrecto, más de una opción a la vez, acción fuera de las
disponibles) se rechaza antes de llegar a `ExecutionCoordinator`. Las
skills de Data Science del catálogo siguen siendo las únicas acciones
elegibles por el LLM; estos componentes son internos y nunca aparecen como
skills. Cada uno vive en su propio módulo bajo `src/mads/orchestrator/`
(`run_lifecycle.py`, `risk_coordination.py`, `availability.py`,
`selection.py`, `authorization.py`, `execution.py`,
`state_transition.py`, `phase_control.py`), y tiene su propia cobertura en
`tests/test_action_selector.py`, `tests/test_execution_coordinator.py` y
`tests/test_phase_coordinator.py`.
