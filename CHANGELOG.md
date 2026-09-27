# Changelog

Formato: una entrada por versión, lo más nuevo arriba. Subir `__version__` en
`backend/app/version.py` y registrar acá cada vez que se toca `backend/app/` o `frontend/`.

## 1.0.0 (2026-09-27)

Primera versión de **Vision genérico** (spec `vision-generico`), nacida del código de Vision
0.19.3 sin nada propio de una empresa en particular.

- **Proveedor de IA configurable**: Claude con clave de Anthropic (`LLM_PROVIDER=anthropic`, vía su
  capa compatible con OpenAI) o cualquier servicio compatible con OpenAI (`openai_compatible` +
  `LLM_BASE_URL`: OpenAI, Azure OpenAI, el gateway de una empresa). Sin clave o sin dirección, el
  chat explica qué falta configurar y nunca manda el pedido a otro servidor.
- **Lista de modelos**: `LLM_MODELS` si está completa; si no, la que publica el proveedor
  (`GET /models`). El salto automático viene precargado con la familia de Claude.
- **Dictado por voz** con su propio proveedor (`STT_BASE_URL`, `STT_API_KEY`, `STT_MODEL`). Sin
  configurar, el micrófono avisa que el dictado no está disponible.
- **Proxy de cualquier empresa**: `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY` del `.env` valen para la app
  y para instalar dependencias; localhost siempre directo.
- **Marca neutra**: logo propio, nombre de la empresa configurable (`VISION_COMPANY_NAME`) en la
  pantalla y en la presentación del asistente, temas "Rojo" y "Azul".
- **Configuración > Credenciales** suma el proveedor de IA y el dictado (claves cifradas).
- Carpeta de datos propia (`%LOCALAPPDATA%\Vision-personal`, índice de sesiones incluido) y puerto
  8030, para convivir con otras instalaciones de Vision en la misma PC.
- Tests: la batería de Vision adaptada al proveedor genérico, más `test_vision_generico.py`
  (proveedor, modelos, proxy, empresa y un guardián que falla si vuelve a aparecer contenido propio
  de una empresa en el código).
