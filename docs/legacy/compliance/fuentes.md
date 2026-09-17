# Fuentes normativas versionadas

Registro de las fuentes oficiales que fundamentan la capa normativa.
Cada fuente lleva un `source_id` estable (lo usará el futuro RAG en sus
citas) y la versión/fecha exacta consultada.

> **Aviso**: este proyecto genera *evidencias y evaluación de preparación*.
> No constituye asesoramiento jurídico ni garantía de cumplimiento.

| source_id | Fuente | Versión consultada | Relevancia para el proyecto |
|---|---|---|---|
| `ai-act-2024` | Reglamento (UE) 2024/1689 (AI Act) | DOUE 12/07/2024 | Clasificación de riesgo (Anexo III: empleo = alto riesgo), supervisión humana (art. 14), registro de eventos (art. 12), documentación técnica (art. 11) |
| `rgpd-2016` | Reglamento (UE) 2016/679 (RGPD) | Texto consolidado 04/05/2016 | Categorías especiales de datos (art. 9), decisiones automatizadas (art. 22), minimización (art. 5) |
| `aepd-guias` | Guías AEPD sobre IA y protección de datos | Según documento | Criterio nacional de referencia |

## Cómo añadir una fuente

1. Añadir fila con `source_id` nuevo (no reutilizar ids).
2. Anotar la versión/fecha exacta del texto consultado.
3. Si el texto cambia, **no** se edita la fila: se añade una nueva con
   versión nueva. El historial es parte de la evidencia.
