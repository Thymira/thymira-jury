# Arquitectura y decisiones de diseño

## Principios

1. **Contratos primero.** Los módulos intercambian estructuras tipadas y las
   skills declaran requisitos, aplicabilidad, efectos e invalidaciones.
2. **Gobernanza como datos.** Las reglas viven en los policy packs JSON.
3. **Fases flexibles.** La disponibilidad depende de la fase, evidencias y
   fingerprints vigentes; dentro de cada fase no hay una receta lineal.
4. **Verificar, no confiar.** La traza encadena hashes, los artefactos llevan
   sha256 y el meta-auditor reconstruye controles de forma independiente.
5. **Minimizar datos.** El selector recibe un resumen del estado y evidencias,
   no el dataset ni el caso completo.

## Flujo de un caso

1. La CLI exige identidad y rol declarados, carga el `Case` y crea siempre
   `AgenticOrchestrator`.
2. `RiskClassifier` clasifica el contexto antes de cualquier ejecución. El
   riesgo declarado por el usuario no concede permisos.
3. El código valida la salida estructurada, solicita contexto o revisión cuando
   corresponde y calcula las autorizaciones deterministas del catálogo.
4. Se inicializa `AgentState` con el dataset fuente, sus fingerprints y las
   versiones de variables y configuración.
5. LangGraph observa el estado y calcula `available_actions` a partir de la
   fase vigente, los contratos técnicos y la aplicabilidad real.
6. El LLM selecciona una sola acción mediante `NextStepProposal`. El código
   rechaza acciones o parámetros fuera del conjunto disponible.
7. La acción pasa por `PolicyGate`:
   - `allow`: continúa;
   - `needs_approval`: solicita y traza una respuesta humana;
   - `blocked`: no crea el worker y termina como bloqueo de autorización.
8. Un `SkillWorker` temporal ejecuta una única skill. Las decisiones analíticas
   siguen pasando por los decision resolvers y nunca sustituyen al Gate.
9. El orquestador comprueba los artefactos prometidos, registra fingerprints de
   entrada y actualiza versiones, dataset vigente, linaje e invalidaciones.
10. El grafo vuelve a observar. `done=true` solicita cerrar la fase, y un gate
    determinista decide si puede avanzar. En reporting solo se acepta el cierre
    si el objetivo tiene evidencia vigente y el informe final puede ejecutarse.
11. `case_finished` cierra la traza; después se verifican la cadena y los
    controles A1–A18.

Un fallo del clasificador, del schema, del worker o de las invariantes termina
cerrado y deja evidencia. El límite de iteraciones es un cortacircuitos
configurable, no el control normal del flujo.

## Estado e invalidación

`AgentState` conserva únicamente la información necesaria para decidir vigencia:

- dataset actual, fingerprint, versión e historial de versiones;
- versiones y fingerprints de variables, split y configuración de modelado;
- versión y fingerprint del modelo entrenado;
- acciones ejecutadas, artefactos vigentes e invalidados;
- última observación y motivo de finalización o error.
- fase actual, revisiones y transiciones hacia delante o de reapertura.

La existencia física de un artefacto no implica vigencia. Cada registro queda
ligado a los fingerprints que lo produjeron. Las reglas centralizadas invalidan
perfil, split, transformaciones ajustadas con train y modelado cuando cambian
sus entradas. Las versiones antiguas se conservan para auditoría.

`model_ready` actúa como gate técnico de salida de preparación. Comprueba
la compatibilidad efectiva entre dataset, variables y split, además de target,
nulos, representación aceptable, muestras y clases.

## Decisiones de diseño

**¿Por qué una acción por iteración?** Porque las transformaciones cambian las
evidencias disponibles. Observar de nuevo evita decidir con un perfil, split o
modelo que ya no representa el dataset vigente.

**¿Por qué separar disponibilidad y autorización?** Una acción puede ser
técnicamente correcta y estar prohibida por una política. El selector nunca
puede elevar permisos ni rebajar el riesgo.

**¿Por qué fingerprints y no nombres de fichero?** Los nombres estables son una
interfaz de artefactos, no una prueba de compatibilidad. El linaje permite usar
la última transformación real con independencia del orden en que se ejecutó.

**¿Por qué conservar skills y workers?** Las skills siguen siendo código Python
versionado y testeable. El worker es un envoltorio efímero de permisos,
ejecución y resultado; añadir una capacidad no exige otro orquestador.

**¿Por qué JSON para políticas?** Evita otra dependencia y mantiene las reglas
reproducibles. El Gate no cambia al añadir dominios.

**¿Por qué fases y no solo instrucciones al LLM?** Las descripciones orientan,
pero no garantizan el comportamiento. La fase oculta capacidades anteriores
una vez cerradas. Si una evaluación descubre leakage u otro defecto, una
reapertura justificada y aprobada crea una nueva revisión e invalida únicamente
la evidencia posterior; no se permite un retroceso accidental.

**¿Por qué dividir `AgenticOrchestrator` en componentes en vez de un único
método?** Un `run_case()` de más de mil líneas con todo entrelazado por
closures compartidas no se podía testear ni sustituir por partes: probar
"¿el juicio de reapertura decide bien?" exigía levantar el grafo completo,
la traza, los artefactos y el Gate. Separarlo en ocho componentes
inyectables (`RunLifecycle`, `RiskCoordinator`, `ActionAvailabilityService`,
`ActionSelector`, `AuthorizationCoordinator`, `ExecutionCoordinator`,
`StateTransitionService`, `PhaseCoordinator`; ver
[`docs/orquestador-agentico.md`](orquestador-agentico.md)) no cambia el
comportamiento observable ni la separación selector/Gate/ejecución/estado:
solo hace que cada pieza se pueda testear, leer y sustituir por separado.
Sigue habiendo un único orquestador y un único flujo LangGraph.

**¿Por qué LiteLLM detrás de `LLMProvider` y no un SDK por proveedor?**
`RiskClassifier`, `ActionSelector`, los resolutores de decisiones,
`AgenticOrchestrator` y `RunLifecycle` solo conocen el contrato
`LLMProvider` (`mads/llm/base.py`): `complete()`/`complete_structured()`
sobre un `LLMResponse`. `LiteLLMProvider` (`mads/llm/litellm_provider.py`)
es la única implementación de producción de ese contrato; ningún otro
módulo importa LiteLLM directamente. El proveedor real (OpenAI, Anthropic...)
se resuelve a partir del identificador de modelo, no de una rama de código
por SDK — ver [`docs/litellm.md`](litellm.md) para el formato de
identificador, cómo se traza proveedor/modelo real y coste, y por qué no hay
fallback, reintentos ni caché en esta integración.

## Límites conocidos

- La redacción de PII es por expresiones regulares y no es infalible.
- La cadena de hashes detecta modificaciones, pero no impide que alguien con
  acceso de escritura regenere una cadena completa.
- La confianza producida por un LLM no está calibrada y nunca concede permisos.
- El doble privado de los tests aporta una línea base determinista, no demuestra la
  fiabilidad general de un selector remoto.
