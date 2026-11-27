# Cuotas Claro

MVP local para capturar cuotas por evento y mercado, calcular la suma de probabilidades implícitas y crear alertas al llegar a 98% o menos.

## Iniciar

Requiere Python 3.10 o posterior; usa solo la biblioteca estándar.

```powershell
python server.py
```

Abre <http://127.0.0.1:8000>, crea una cuenta y agrega un evento. Escribe todos los resultados mutuamente excluyentes separados por comas y después guarda cuotas decimales para cada resultado.

## Regla del cálculo

Para cada resultado se toma la mejor cuota decimal capturada entre las casas. La probabilidad implícita bruta es `1 / cuota`. El sistema suma las probabilidades de todos los resultados del mercado. Cuando el mercado está completo y la suma es `≤ 0.98`, crea una alerta. El underround calculado es `1 - suma`.

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

Para SMTP sobre SSL implícito, define `SMTP_USE_SSL=true` y el puerto que indique el proveedor (a menudo 465). El MVP solo envía una alerta cuando cambia la combinación de mejores cuotas; si el correo falla, deja la alerta como pendiente para revisión en el panel. No se debe exponer el servidor directamente a internet sin HTTPS, almacenamiento persistente de sesiones, límites de intentos y una revisión de seguridad.

## Datos

La base SQLite se crea en `data/odds.db`. Los eventos y sus alertas quedan asociados a la cuenta que los creó. Las sesiones actuales viven en memoria y se cierran al reiniciar el servidor; pensado para uso local y validación del MVP.
