# Capa normativa (AI Act, RGPD)

> **Aviso permanente**: esta capa ayuda a *generar evidencias* y *evaluar
> preparación* frente al AI Act y el RGPD. No constituye asesoramiento
> jurídico ni garantía de cumplimiento legal.

## Las cinco piezas, claramente separadas

1. **Fuentes oficiales versionadas** → `compliance/fuentes.md`.
   Cada fuente tiene un `source_id` estable y la versión/fecha exacta.
   Las fuentes no se editan: se añaden versiones nuevas.

2. **Mapeo requisitos → controles técnicos** →
   `compliance/mapeo_requisitos_controles.json`.
   Conecta cada requisito legal (con su fuente y artículo) con el
   mecanismo del repo que genera su evidencia. Ejemplo: "registro de
   eventos (AI Act art. 12)" → "traza JSONL con hashes" → "`mads verify`".

3. **Políticas deterministas** → `src/mads/policy_packs/*.json` + `gate.py`.
   Las reglas que deciden allow/needs_approval/blocked son datos
   evaluables en orden, sin LLM y sin ambigüedad. Son la *aplicación*
   técnica de los requisitos mapeados.

4. **Recuperación futura de citas (RAG)** → `src/mads/rag/contracts.py`.
   Aún no implementado; solo existe el contrato que fija sus límites. No hay
   vector store, embeddings, buscador ni dependencia de recuperación.

5. **Revisión y aprobación humana** → `gate.py` (needs_approval) +
   eventos `approval_requested`/`human_response` en la traza.
   Es la pieza que ninguna de las anteriores puede sustituir.

## Límites del futuro RAG (por diseño, no por promesa)

Un posible RAG normativo o documental sería **solo informativo**:

- Podría aportar contexto y citas versionadas (fuente + versión + artículo +
  fragmento) a evidencias futuras.
- Esas evidencias podrían alimentar `RiskAssessment.evidence_refs`, que solo
  referencia su procedencia.
- **Nunca** concedería permisos, aprobaría acciones, modificaría políticas ni
  devolvería un `Verdict`.

Ese límite está codificado en los tipos: la interfaz `NormativeRAG` solo
devuelve citas; no existe ningún método que devuelva un `Verdict`. El circuito
de decisión (clasificador → policy pack → Gate → humano) no tiene ningún punto
de entrada desde el RAG. Añadir una referencia a `evidence_refs` aportaría
contexto verificable, no autoridad.

## Riesgo declarado y contexto regulatorio

El Anexo III del AI Act clasifica como alto riesgo los sistemas de IA en
empleo y gestión de trabajadores. Es una señal contextual para la taxonomía,
no una determinación jurídica automática. Del mismo modo, `case.risk_level`
es un valor declarado por el usuario. Su valor compatible por defecto es
`high` para no asumir riesgo bajo, pero no clasifica ni autoriza: el
`RiskClassifier` evalúa el `Case` no confiable por separado y el Gate usa ese
`RiskAssessment` como única fuente de riesgo.
