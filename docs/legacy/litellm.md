# Conectar MADS con un modelo LLM (vía LiteLLM)

LiteLLM es la única capa de acceso a modelos LLM del proyecto:
`LiteLLMProvider` (`src/mads/llm/litellm_provider.py`) es la única
implementación de producción del contrato `LLMProvider`
(`src/mads/llm/base.py`). No hay un SDK por proveedor ni una rama de código
por `--provider`: el proveedor real se resuelve a partir del identificador
de modelo.

## 1. Instalar el proyecto

Desde la carpeta que contiene `pyproject.toml`, única instalación soportada:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

## 2. Configurar las claves

Abre `.env` y completa la clave del proveedor real que vaya a usar el modelo
elegido. LiteLLM lee estas variables **directamente** del entorno; MADS no
las procesa, no las guarda y no las expone en código, traza, artefactos,
mensajes de error ni documentación:

```text
OPENAI_API_KEY=tu_clave_real
ANTHROPIC_API_KEY=tu_clave_real
```

No pongas comillas y no compartas ni subas `.env` (está en `.gitignore`).
`.env.example` es la plantilla sin secretos.

## 3. Elegir un modelo

Dos formas, en este orden de prioridad:

1. `--model <identificador>` en `mads run` o `mads llm-test`.
2. La variable de entorno `MADS_MODEL` (convención propia de MADS — **no**
   es una variable nativa de LiteLLM).

Si no se indica ninguna, se usa el valor por defecto (`gpt-5.6-luna`, el
mismo que ya era el predeterminado antes de esta integración).

El identificador sigue el formato oficial de LiteLLM: un nombre sin prefijo
se asume OpenAI (p. ej. `gpt-5.6-luna`); con el prefijo
`"<proveedor>/<modelo>"` se enruta a otro proveedor (p. ej.
`anthropic/claude-sonnet-4-5`).

```powershell
mads llm-test
mads llm-test --model anthropic/claude-sonnet-4-5
mads run examples/cases/caso_credito_aleman_arbol.json --output runs/caso1 --model anthropic/claude-sonnet-4-5
```

## 4. Probar solo la conexión

```powershell
mads llm-test
```

Muestra la integración (LiteLLM), el proveedor real, el modelo solicitado,
el modelo respondido, la respuesta, los tokens y el coste estimado. No
imprime ninguna clave.

## 5. Cómo queda trazado

Cada llamada LLM se registra como un evento `llm_call` con actor
`llm:<proveedor real>` (tomado de la respuesta, no de la configuración).
`llm_call.metadata` incluye, además de lo habitual (tokens, prompt/respuesta
redactados):

- `integration`: siempre `"litellm"`.
- `model_requested`: el identificador pedido (`--model`/`MADS_MODEL`/por
  defecto).
- `model` (en el propio `LLMResponse`, no en `metadata`): el modelo que
  realmente respondió — puede diferir del solicitado (p. ej. un alias
  resuelto a un snapshot fechado concreto).
- `cost_source` / `cost_known`: de dónde sale el coste y si se pudo
  calcular con certeza.

Ver [`docs/trazabilidad.md`](trazabilidad.md) para el resto de eventos.

## 6. Coste

El coste de cada llamada lo calcula LiteLLM (`litellm.completion_cost`, a
partir de su tabla de precios mantenida, `litellm.model_cost`). Si el
modelo usado no tiene precio conocido, `cost_estimate_usd` queda en `None`
en vez de inventar un número — nunca se cuenta dos veces la misma llamada.

**Limitación conocida:** antes de esta integración, el proveedor de OpenAI
calculaba a mano tramos de precio distintos para entrada cacheada, escritura
de caché y contexto largo. LiteLLM da un único coste total por llamada; ese
desglose fino se pierde. Es una limitación deliberada, no un efecto
colateral: sustituir un mapa de precios mantenido a mano por el mecanismo
oficial de LiteLLM era justamente el objetivo.

## 7. Qué NO hace esta integración

Deliberadamente, en esta primera versión:

- **Sin fallback automático** entre proveedores o modelos.
- **Sin reintentos activados por MADS.** `litellm.num_retries` es `None`
  por defecto (verificado en el código fuente de LiteLLM) y no se activa
  aquí — una llamada fallida no se reintenta sola.
- **Sin caché** de respuestas.
- **Sin LiteLLM Proxy, servidor ni Docker.** Es el SDK Python, dentro del
  proceso de MADS.

## 8. Qué sigue siendo la evidencia oficial

La traza local (`trace.jsonl`, encadenada por hash y verificable con
`mads verify`) sigue siendo la única evidencia auditable oficial de una
ejecución. LiteLLM es una capa de acceso, no un sistema de auditoría.
