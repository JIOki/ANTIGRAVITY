# Cuotas Claro

MVP local para capturar cuotas por evento y mercado, calcular la suma de probabilidades implícitas y crear alertas al llegar a 98% o menos.

## Iniciar

Requiere Python 3.10 o posterior; usa solo la biblioteca estándar.

```powershell
python server.py
```

Abre <http://127.0.0.1:8000>, crea una cuenta y agrega un evento. Escribe todos los resultados mutuamente excluyentes separados por comas y después guarda cuotas decimales para cada resultado.

## Regla del cálculo

Para cada resultado y cada casa se usa solo la **captura más reciente** (la casa se compara sin distinguir mayúsculas ni espacios). Se descartan las cuotas con más antigüedad que la **vigencia** configurada (60 minutos por defecto). Entre las cuotas vigentes se toma la mejor de cada resultado. La probabilidad implícita bruta es `1 / cuota` y el sistema suma las probabilidades de todos los resultados del mercado. Cuando el mercado está completo con cuotas vigentes y la suma es menor o igual al **umbral** (98% por defecto), crea una alerta. El underround calculado es `1 - suma`.

El umbral (50%–100%) y la vigencia (1 minuto a 7 días) se configuran por cuenta en el panel. El panel muestra qué cuotas están vigentes, reemplazadas o vencidas, y permite editar o eliminar eventos y capturas. Si al editar un evento se quita un resultado, también se borran sus cuotas.

Un cálculo de 98% o menos es un umbral sobre la suma implícita de cuotas, no una afirmación de 98% de probabilidad de ganar. No garantiza que las cuotas estén vigentes, que acepten todas las apuestas, ni que el retorno neto sea positivo después de límites y comisiones. El MVP avisa; no coloca apuestas.

## Correo

El correo usado al crear la cuenta se guarda como destino inicial de alertas y se puede cambiar desde el panel. Para que el servidor envíe mensajes, configura estas variables de entorno antes de iniciarlo:

```powershell
$env:SMTP_HOST = "smtp.example.com"
$env:SMTP_PORT = "587"
$env:SMTP_FROM = "alertas@example.com"
$env:SMTP_USER = "usuario"
$env:SMTP_PASSWORD = "contraseña-de-aplicacion"
python server.py
```

`SMTP_SECURITY` controla el cifrado:

- `starttls` (por defecto, puerto 587): si el servidor no ofrece STARTTLS, el correo **no** se envía.
- `ssl` (puerto 465 por defecto): SSL implícito. `SMTP_USE_SSL=true` sigue funcionando como equivalente.
- `none`: sin cifrado, solo para un relay local; en este modo nunca se envían credenciales. El MVP solo envía una alerta cuando cambia la combinación de mejores cuotas; si el correo falla, deja la alerta como pendiente para revisión en el panel. No se debe exponer el servidor directamente a internet sin HTTPS, almacenamiento persistente de sesiones, límites de intentos y una revisión de seguridad.

## Datos

La base SQLite se crea en `data/odds.db`. Los eventos y sus alertas quedan asociados a la cuenta que los creó. Las sesiones se guardan en la misma base (solo el hash SHA-256 del token), duran 7 días y sobreviven a reinicios del servidor; cerrar sesión las invalida. El inicio de sesión se bloquea 15 minutos tras 5 intentos fallidos por correo o 20 por IP, y el registro está limitado a 10 por IP cada 15 minutos. Las bases creadas con la versión anterior se migran solas al iniciar.

Limitación conocida: sin verificación de correo, el registro todavía permite deducir si un correo ya tiene cuenta (aunque el mensaje es genérico y está limitado por IP). Detrás de un proxy inverso, todas las peticiones comparten la IP del proxy para el límite por IP.

## Pruebas

```powershell
python -m unittest discover tests
```
