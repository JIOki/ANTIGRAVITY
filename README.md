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

## Cuotas por API (The Odds API)

Además de la captura manual, el panel puede seguir deportes completos e importar sus eventos y cuotas del mercado **ganador (h2h)** desde [The Odds API](https://the-odds-api.com). Las cuotas importadas y las capturadas a mano se combinan en el mismo cálculo, así que puedes completar a mano las casas que el proveedor no cubre (por ejemplo, Caliente o Codere MX).

1. Crea una clave en the-odds-api.com (el plan gratuito da 500 créditos al mes).
2. Inicia el servidor con la clave en una variable de entorno; nunca la guardes en el repositorio:

   ```powershell
   $env:ODDS_API_KEY = "tu-clave"
   python server.py
   ```

3. En el panel, en **Cuotas por API**, elige un deporte o liga y las regiones de casas, y luego usa **Sincronizar ahora**.

Cómo funciona:

- Cada sincronización hace **una llamada por combinación de deporte y regiones**, aunque varias cuentas sigan lo mismo. Cuesta 1 crédito por región. El panel muestra los créditos restantes y un consumo mensual estimado.
- La sincronización automática corre cada `ODDS_API_INTERVAL_MINUTES` (120 por defecto, mínimo 5) y solo trae eventos que empiezan dentro de `ODDS_API_HORIZON_HOURS` (72 por defecto). Los eventos ya iniciados no se importan.
- Si una cuota no cambió, solo se renueva su hora de observación, sin duplicar el historial. Si cambió, se guarda como una captura nueva.
- Para que las cuotas de la API no venzan entre sincronizaciones, la vigencia de tu cuenta debe ser mayor o igual al intervalo; el panel avisa cuando no lo es.
- Solo se combinan casas que publican el mismo conjunto de resultados; por ejemplo, en fútbol se descartan los mercados sin empate.
- Si eliminas un evento importado, no se vuelve a crear. Sus resultados los define el proveedor y no se pueden editar.
- Para agregar otro proveedor, implementa `OddsProvider` en `providers.py` y regístralo en `PROVIDERS`.

Las cuotas de exchanges (por ejemplo, Betfair en la región Europa) no descuentan comisión. Revisa siempre la cuota en la casa antes de decidir.

## Despliegue en VPS (Docker + Nginx)

`deploy/docker-compose.yml` levanta el servidor en un contenedor `python:3.12-slim`. Al arrancar, clona el repo en `/opt/cuotas-claro/app` o lo actualiza a la última versión de `GIT_BRANCH` (main por defecto); la base de datos queda fuera del código, en `/opt/cuotas-claro/data`. El puerto solo se publica en `127.0.0.1:8010`, así que el acceso público pasa siempre por Nginx con HTTPS.

Para actualizar después de hacer push basta con reiniciar el contenedor.

En el VPS (una sola vez), con `deploy/nginx-cuotasclaro.conf`:

```bash
sudo cp /opt/cuotas-claro/app/deploy/nginx-cuotasclaro.conf /etc/nginx/sites-available/cuotasclaro.ponlopallevar.com
sudo ln -s /etc/nginx/sites-available/cuotasclaro.ponlopallevar.com /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d cuotasclaro.ponlopallevar.com
```

Variables de producción ya incluidas en el compose:

- `ODDS_TRUST_PROXY=true`: toma la IP real de `X-Real-IP` para los límites de intentos. Actívala solo detrás de un proxy, porque el cliente podría falsificar ese encabezado.
- `ODDS_SECURE_COOKIE=true`: la cookie de sesión solo viaja por HTTPS.

## Datos

La base SQLite se crea en `data/odds.db`. Los eventos y sus alertas quedan asociados a la cuenta que los creó. Las sesiones se guardan en la misma base (solo el hash SHA-256 del token), duran 7 días y sobreviven a reinicios del servidor; cerrar sesión las invalida. El inicio de sesión se bloquea 15 minutos tras 5 intentos fallidos por correo o 20 por IP, y el registro está limitado a 10 por IP cada 15 minutos. Las bases creadas con la versión anterior se migran solas al iniciar.

Limitación conocida: sin verificación de correo, el registro todavía permite deducir si un correo ya tiene cuenta (aunque el mensaje es genérico y está limitado por IP). Detrás de un proxy inverso, todas las peticiones comparten la IP del proxy para el límite por IP.

## Pruebas

```powershell
python -m unittest discover tests
```
