# Decisiones analíticas estructuradas

## Objetivo

El asistente necesita colaboración humana, no solo aprobaciones. Se separan dos
preguntas distintas:

- El `PolicyGate` decide si una acción está permitida por las políticas.
- El coordinador analítico pregunta cómo quiere la persona realizar el trabajo.

Una preferencia técnica nunca concede permisos ni demuestra cumplimiento legal.

## Contrato

Cada `AnalyticalDecisionRequest` contiene un identificador, tipo, pregunta,
motivo, componente solicitante, alternativas, consecuencias conocidas,
referencias a evidencias y si la respuesta es bloqueante. La respuesta conserva
la opción, origen humano o automático, identidad y rol declarados y una
justificación breve.

El coordinador valida que la respuesta corresponda con la pregunta y con una
opción existente. Después registra:

```text
analytical_decision_requested
        ↓
analytical_decision_answered
        ↓
analytical_decision_applied
```

La evidencia completa se guarda en `artifacts/analytical_decisions.json` y se
incorpora al informe. El meta-auditor A8 detecta decisiones bloqueantes sin
respuesta.

## Primera decisión implementada

Cuando el plan contiene skills de modelado y el caso no declara
`modeling_config`, el sistema pregunta si debe:

1. Comparar todos los candidatos y seleccionar el mejor.
2. Validar únicamente la regresión logística.
3. Validar únicamente Random Forest.

La pregunta se deriva del plan validado, no de palabras clave del objetivo. La
alternativa aplicada configura la validación cruzada y queda relacionada con la
evidencia posterior de candidatos y selección.

En tests y CI, el resolver automático interno aplica la opción recomendada,
pero la traza la marca como `automatic_default` y nunca como respuesta humana.
En una ejecución interactiva se ofrece LLM con confirmación humana (por defecto)
o LLM sin confirmación (experimental).

La CLI exige identificar al responsable de toda ejecución. Si los parámetros
se omiten, los solicita antes de cargar el caso, crear la traza o llamar al
proveedor LLM. No admite valores vacíos:

```powershell
mads run examples/cases/caso_credito_aleman_arbol.json `
  --output runs/decision-humana `
  --decision-actor-id analyst-17 `
  --decision-actor-role senior_data_scientist
```

La identidad queda en `case_started`, `actor_context_established`, las
decisiones analíticas, las respuestas al Gate y el informe analítico. Esto crea la
base de evidencia para una futura segregación de funciones, pero los roles aún
no conceden permisos: siguen siendo valores declarados.

## Cómo añadir un nuevo punto de decisión

1. La skill o el orquestador identifica una ambigüedad material.
2. Construye una solicitud con alternativas y consecuencias comprensibles.
3. Aporta referencias a los artefactos que justifican la pregunta.
4. El coordinador obtiene y valida la respuesta.
5. La configuración elegida se guarda como artefacto y es consumida por las
   skills posteriores.
6. Se añade una prueba que demuestre que cada opción cambia realmente el flujo.

Buenos candidatos siguientes son la métrica de negocio, el coste relativo de
falsos positivos y negativos, el tratamiento de una posible fuga, la estrategia
de valores nulos y el umbral de decisión.

## Límites actuales

- La confirmación humana del modo `llm-confirm` es síncrona: todavía no existe
  pausa y reanudación entre procesos.
- La identidad obligatoria es declarada, no está autenticada contra un
  directorio corporativo. En producción deberá proceder de SSO o de un
  proveedor de identidad y no de parámetros de terminal.
- Solo existe un punto de decisión analítica integrado.
- Una respuesta humana no reemplaza una base jurídica, una evaluación de
  impacto ni una revisión del DPO o del área de cumplimiento.

Estos límites se mantienen explícitos para no presentar el prototipo como un
sistema de producción.
