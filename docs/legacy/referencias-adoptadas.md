# Ideas adoptadas y descartadas de los proyectos de referencia

Se analizaron tres proyectos como referencia (no se copió código; se
reescribió todo adaptado a nuestros contratos y a nuestro nivel).

## De `Nucleo`

**Adoptado:**
- **El Gate como único punto de control** con tres veredictos
  (allow / needs_approval / blocked) y aprobador inyectable (callable).
  Es la idea más limpia de las tres referencias → `gate.py`.
- **Policy packs como datos** evaluados en orden, con `default`
  conservador ("ante lo desconocido, preguntar") → `policies.py`,
  `src/mads/policy_packs/fraude_aml.json`.
- **Meta-auditor determinista con códigos de hallazgo** que corre a
  posteriori y nunca decide en caliente → `audit/meta_auditor.py`
  (A1–A18 adaptan y amplían sus controles; A15 comprueba que clasificación,
  planificación y workers mantengan el orden de seguridad).

**Descartado:**
- Su tracer sin cadena de hashes (JSONL simple): lo sustituimos por la
  cadena de Mario, porque la manipulación de trazas es exactamente lo que
  queremos poder detectar.
- Los checks globales entre casos (G1 abuso de default, G2 aprobación
  sistemática): buena idea, pospuesta a una fase futura para no crecer.
- Su sandbox de ejecución: complejidad que aún no necesitamos.
- YAML para políticas: preferimos JSON para mantener cero dependencias.

## De `tfm-mario`

**Adoptado:**
- **La cadena de hashes en la traza** (`HashChainedEventStore`):
  seq + prev_hash + hash sobre JSON canónico, con verificación O(n) sin
  estado → `tracing.py` (simplificado: sin spans ni threading).
- **WorkOrders tipados** con `allowed_tools`, `expected_artifacts` y
  `dependencies`, y el worker validando sus herramientas
  (`PermissionError` si faltan) → `contracts.Task` + `workers/base.py`.
- **Artefactos con sha256 en un manifest** verificable → `artifacts.py`.
- **Selección agéntica con pruebas reproducibles**: el doble privado y los
  proveedores remotos usan el mismo contrato de una acción por iteración →
  `orchestrator/agentic.py` y `orchestrator/actions.py`.
- **Clasificación de riesgo separada de la autorización**. Un único
  `RiskClassifier` interpreta el `Case` no confiable mediante el proveedor
  elegido; el policy pack y el Gate, no el LLM, conceden o bloquean la
  ejecución → `risk.py`, `policies.py`, `gate.py`.
- **Inicio con ML mínimo propio** para validar la arquitectura. Esta decisión
  evolucionó: las skills de `skills/modeling/` utilizan scikit-learn para
  disponer de candidatos mantenidos y validación cruzada reproducible.

**Descartado:**
- Los JSON Schema formales (`schemas/*.json`): en nuestro tamaño, los
  dataclasses tipados cumplen el mismo papel con menos maquinaria. Si el
  proyecto crece hacia interoperabilidad, se reintroducen.
- Los spans jerárquicos (span_id/parent_span_id): nuestra secuencia lineal
  por caso es suficiente y más fácil de leer.

## De `TFM - Arquitectura lucas`

**Adoptado:**
- **La idea del catálogo de capacidades**: las skills se registran con
  id, versión, descripción, herramientas, productos y hechos de riesgo; el
  agente selecciona contra el catálogo, no importa clases a dedo. El selector
  LLM recibe acciones calculadas por código y un estado compacto →
  `skills/registry.py::SkillRegistry`.
- La actitud de tratar la selección de agentes como un problema de
  *routing* documentable (su `justification` inspira nuestros
  `decision_code` + `criteria` + `alternatives_considered`).

**Descartado (la mayor parte del repo, deliberadamente):**
- Agent-packs, playbooks, MCPs, benchmarks, evals, perfiles, manifests,
  hooks, wrappers `$data_ref`… Es una arquitectura de plataforma en
  producción con decenas de subsistemas. Para un TFM de un mes sería
  exactamente el tipo de repo "que no entendemos": lo contrario de
  nuestro objetivo.
- Su catálogo como ficheros JSON externos con schemas: nuestro registro vive
  en código y mantiene tipos suficientes para el alcance actual.

## Resumen en una frase

Gobernanza de **Nucleo** (gate + policy packs + meta-auditor), evidencia
de **Mario** (hashes + work orders + manifest y clasificación separada),
selección por catálogo de **Lucas** — todo reducido al mínimo que un grupo de
estudiantes puede leer, defender y ampliar.
