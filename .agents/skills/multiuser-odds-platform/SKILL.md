---
name: multiuser-odds-platform
description: Diseña arquitectura, datos y permisos para aplicaciones multiusuario que guardan y analizan eventos y cuotas.
---

# Plataforma multiusuario de cuotas

Modela usuarios, roles, operadores, eventos, mercados, selecciones y observaciones de cuota con identificadores estables y relaciones explícitas. Decide qué datos son globales y cuáles pertenecen a una cuenta; aplica autorización en cada lectura y escritura, no solo en la interfaz. Conserva quién creó o editó datos y cuándo.

Separa ingestión, normalización, cálculo y presentación para que cada resultado pueda reproducirse desde sus entradas. Diseña validación, deduplicación, manejo de cuotas obsoletas, copias de seguridad y registro de cambios según el alcance. No elijas tecnologías sin revisar las existentes; propone cambios incrementales y documenta decisiones sensibles a volumen, privacidad o costos.
