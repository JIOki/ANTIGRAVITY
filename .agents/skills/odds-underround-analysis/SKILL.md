---
name: odds-underround-analysis
description: Calcula probabilidades implícitas y underround/overround a partir de cuotas completas, con supuestos y límites visibles.
---

# Análisis de cuotas

Para cuotas decimales, calcula probabilidad implícita bruta como `1 / cuota`. Para un mercado completo, suma las probabilidades de cada resultado mutuamente excluyente: menor que 1 es underround, igual a 1 es suma justa y mayor que 1 es overround. Informa la suma también como porcentaje y explica si se comparan operadores distintos.

Usa datos del mismo evento y mercado, resultados completos y cuotas todavía vigentes. Para cálculos entre operadores, distribuye el stake usando las probabilidades normalizadas por las cuotas disponibles y muestra retorno bruto, beneficio neto, comisiones y supuestos. No asumas que todas las selecciones se pueden ejecutar ni que las cuotas permanecerán disponibles. Separa la probabilidad implícita de la probabilidad real de un resultado y nunca describas el umbral de 98% como garantía o probabilidad de ganar. Redondea solo al presentar; calcula con precisión completa.
