"""
Voz de Vision con motores intercambiables (spec voz-fish-audio).

De fabrica Vision habla con la voz del navegador (Web Speech API, en
`frontend/js/tts.js`). Este paquete suma los motores que devuelven audio
generado del lado del backend, en una cadena de prioridad que arma el usuario
en Configuracion > Audio; la voz del navegador queda siempre como ultimo
eslabon (REQ-003: nunca mudo).

- `engines.py`: contrato comun y motores en la nube (Fish Audio, ElevenLabs).
- `local_clone.py`: clonacion local con Chatterbox (muestra de voz,
  instalacion con simulacion previa y sintesis).
- `settings.py`: configuracion guardada (cadena, voces, consentimiento, efecto).
- `service.py`: recorre la cadena, pausa los motores que fallan y arma el aviso.
"""
