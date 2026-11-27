# Base de conocimiento de Cuotas Claro

<!--
Cada sección "## " es un tema. La línea "acciones:" (opcional) indica los botones
que se muestran cuando el asistente responde con ese tema sin IA. Las acciones
válidas están en assistant.py (ACTIONS). Mantén cada sección breve: todo este
archivo se envía al modelo en cada pregunta.
-->

## Qué es Cuotas Claro
Cuotas Claro es una herramienta web para encontrar surebets (arbitraje): el mismo evento en distintas casas de apuestas, tomando la mejor cuota de cada resultado, con una suma de probabilidades implícitas MENOR a 100%. Una sola casa siempre suma más de 100% (es su margen); combinando casas a veces baja de 100%, y entonces el sistema calcula cuánto apostar en cada casa para ganar lo mismo sin importar el resultado. El usuario registra las cuotas a mano o las importa por API, y recibe una alerta cuando la suma llega a su umbral (98% por defecto). Lema: "Fuentes visibles. Cálculos claros. Tú decides cuándo actuar." No es una casa de apuestas: no recibe apuestas ni dinero y no da recomendaciones de apuesta. Es gratis.
acciones: crear_cuenta, como_empezar, explicar_calculo

## Cómo empezar
1. Crea una cuenta con tu correo y una contraseña de al menos 10 caracteres.
2. Pulsa "Nuevo evento" y escribe el evento, el deporte, el mercado y TODOS los resultados posibles separados por coma (por ejemplo: Tigres, Empate, América).
3. En la tarjeta del evento, captura la cuota decimal de cada resultado y la casa donde la viste.
4. Cuando el mercado tiene una cuota vigente para cada resultado, el panel muestra la suma de probabilidades implícitas y el underround.
5. Opcional: configura tu correo de alertas, tu umbral y la vigencia de las cuotas, o conecta cuotas por API.
acciones: crear_cuenta, nuevo_evento, ir_eventos

## Cómo se calcula
- Probabilidad implícita de una cuota decimal = 1 / cuota. Ejemplo: cuota 2.50 → 40%.
- Para cada resultado se toma la MEJOR cuota vigente entre todas las casas.
- Se suman las probabilidades implícitas de todos los resultados del mercado.
- Underround teórico = 100% − suma. Ejemplo: cuotas 3.2, 3.6 y 3.4 → 31.25% + 27.78% + 29.41% = 88.44%, underround 11.56%.
- Si la suma es menor a 100% hay surebet y el panel muestra el reparto sugerido. Si además es menor o igual a tu umbral, aparece "Umbral alcanzado" y se genera una alerta.
- Una suma mayor a 100% es lo normal: es el margen de las casas.
acciones: ir_eventos, ir_ajustes

## Reparto de apuestas: cuánto apostar en cada casa
- Solo para usuarios con sesión iniciada: aparece en la tarjeta de cada evento con suma menor a 100%, en el correo de alerta y en este chat.
- Tú defines el monto total a apostar (por defecto $1,000) y el redondeo de cada apuesta, de $1 a $10 (por defecto $1), en "Umbral, vigencia y reparto".
- Fórmula: apuesta en cada resultado = monto × (1/cuota) / suma. Así el pago es igual gane quien gane.
- Ejemplo con $1,000: Tigres 2.10 en Casa 1 y América 2.05 en Casa 2 (suma 96.40%) → $494 a Tigres en Casa 1 y $506 a América en Casa 2; cualquiera que gane cobras unos $1,037, ganancia mínima de $37.30.
- El redondeo se elige para maximizar la ganancia mínima sin pasarte del monto. Si el monto es muy bajo, el sistema avisa que no se garantiza ganancia.
- En la tarjeta puedes cambiar el monto y pulsar "Recalcular".
- Riesgos: las cuotas cambian rápido, algunas casas limitan o cierran cuentas que hacen arbitraje y, si una casa anula una apuesta, la otra queda descubierta. Coloca todas las apuestas casi al mismo tiempo.
acciones: ver_reparto, ir_ajustes, explicar_calculo

## Cuotas vigentes, reemplazadas y vencidas
- De cada casa solo cuenta la captura más reciente por resultado; las anteriores quedan como "Reemplazada". La casa se reconoce sin importar mayúsculas ni espacios.
- Una cuota más antigua que tu vigencia (60 minutos por defecto) queda "Vencida" y se ignora en el cálculo; hay que volver a capturarla.
- El panel indica hasta qué hora es válido el cálculo y cuántas cuotas vencidas se ignoraron.
- En el "Historial de capturas" de cada evento ves todas las capturas con su origen (manual o API) y puedes eliminar cualquiera.
acciones: ir_eventos, ir_ajustes

## Eventos y mercados
- Un evento tiene: nombre, deporte (opcional), mercado (por ejemplo 1X2, Ganador, Más/Menos 2.5), resultados posibles (de 2 a 20) y fecha opcional.
- Es indispensable escribir todos los resultados del mercado; si falta uno, el cálculo no es válido.
- Puedes editar o eliminar un evento con los botones de su tarjeta. Si quitas un resultado al editar, se borran sus cuotas.
- Los indicadores del panel muestran eventos en seguimiento, mercados completos y alertas.
acciones: nuevo_evento, ir_eventos

## Alertas por correo
- Cuando un mercado completo llega al umbral, se crea una alerta con el cálculo y las cuotas usadas. La misma combinación de cuotas no se repite.
- El correo de alertas se configura en la sección "Alertas por correo"; al registrarte se usa tu correo de cuenta.
- Si el servidor no tiene SMTP configurado, la alerta queda "pendiente" y se ve en el panel.
acciones: ir_correo, ir_ajustes

## Umbral, vigencia y reparto
- Umbral de alerta: entre 50% y 98% (por defecto 98%). El máximo es 98% para dejar al menos 2% de margen de seguridad. Un umbral más bajo da menos alertas, pero con más ganancia.
- Vigencia de cuotas: entre 1 minuto y 7 días (por defecto 60 minutos).
- Si usas cuotas por API, la vigencia debe ser igual o mayor al intervalo de sincronización (120 minutos) para que no venzan entre una sincronización y otra.
- Monto total a apostar y redondeo ($1 a $10) para el reparto.
- Se cambian en la sección "Umbral, vigencia y reparto" del panel.
acciones: ir_ajustes

## Cuotas por API
- En la sección "Cuotas por API" puedes seguir un deporte o liga completo (por ejemplo, Liga MX) y elegir las regiones de casas: Estados Unidos, Estados Unidos 2, Reino Unido, Europa y Australia.
- El sistema importa automáticamente los eventos de las próximas 72 horas con las cuotas del mercado ganador (h2h) y los marca como "API · The Odds API". También puedes pulsar "Sincronizar ahora".
- Las cuotas de la API y las capturadas a mano se combinan: puedes completar a mano casas que el proveedor no cubre, como Caliente o Codere México.
- El proveedor es The Odds API; cada región consume un crédito por sincronización. El panel muestra los créditos restantes y el consumo mensual estimado.
- Si eliminas un evento importado, no se vuelve a crear. Sus resultados los define el proveedor.
acciones: ir_api, sitio_odds_api

## Cuenta, contraseña y seguridad
- Para crear una cuenta solo necesitas correo y contraseña (10 caracteres o más). Las contraseñas se guardan cifradas.
- La sesión dura 7 días; "Cerrar sesión" la termina.
- Tras varios intentos fallidos de inicio de sesión hay que esperar 15 minutos.
- Cada usuario solo ve sus propios eventos, cuotas y alertas.
- Por ahora no hay recuperación automática de contraseña; si la olvidas, contacta al administrador del sitio.
acciones: iniciar_sesion, crear_cuenta

## Advertencias: garantías, ganancias y juego responsable
- Un underround teórico no garantiza que las cuotas sigan disponibles, que se acepten todas las apuestas ni que exista una ganancia neta.
- Las cuotas cambian rápido, algunas casas limitan o cierran cuentas que hacen arbitraje y, si una casa anula una apuesta, la otra queda descubierta: coloca todas las apuestas casi al mismo tiempo.
- Las casas pueden limitar montos o cobrar comisiones (los exchanges cobran comisión).
- Verifica siempre la cuota en la casa antes de decidir. Cuotas Claro solo calcula: no asesora ni garantiza resultados.
- Apuesta solo si eres mayor de edad y de forma responsable.
acciones: explicar_calculo
