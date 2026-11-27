---
name: odds-data-capture
description: Diseña procesos y esquemas para capturar, normalizar y mantener trazables las cuotas de operadores de juego.
---

# Captura y calidad de cuotas

Registra cada observación con operador, evento, mercado, selección, cuota original, formato, cuota normalizada, fuente, método de captura y hora de observación con zona horaria. Mantén historial en vez de sobrescribir observaciones; relaciona cada precio con el evento y mercado correctos.

Valida cuotas positivas y parseables, duplicados, mercados incompletos, eventos ambiguos y observaciones antiguas. Expón la antigüedad y fuente junto al resultado calculado. Nunca rellenes valores desconocidos con estimaciones silenciosas. Acepta captura manual o fuentes de datos autorizadas; no diseñes evasión de restricciones de acceso.
