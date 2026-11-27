"""Reparto de apuestas para un mercado con suma de probabilidades implícitas menor a 100% (surebet).

Con un monto total T y las mejores cuotas c_i de cada resultado:
    S   = Σ 1/c_i                      (suma de probabilidades implícitas)
    a_i = T · (1/c_i) / S              (apuesta ideal en cada resultado)
    pago = a_i · c_i = T / S           (igual sin importar qué resultado gane)

Luego cada apuesta se redondea a múltiplos de `step` buscando la combinación que
maximiza la ganancia mínima garantizada sin pasarse del monto total.
"""
from __future__ import annotations

import itertools
import math

ROUNDING_RANGE = (1, 10)
AMOUNT_RANGE = (10, 10_000_000)
MAX_EXHAUSTIVE_OUTCOMES = 10  # 2^10 = 1024 combinaciones de redondeo

RISK_WARNING = ("Las cuotas cambian rápido, algunas casas limitan o cierran cuentas que hacen arbitraje y, "
                "si una casa anula una apuesta, la otra queda descubierta. Coloca todas las apuestas casi al "
                "mismo tiempo y verifica cada cuota en la casa antes de apostar.")


def _evaluate(stakes: list[float], odds: list[float]) -> tuple[float, float, list[float]]:
    spent = sum(stakes)
    payouts = [s * o for s, o in zip(stakes, odds)]
    return spent, min(payouts) - spent, payouts


def calculate_stakes(best_odds: dict[str, dict], outcomes: list[str], total: float, step: int = 1) -> dict | None:
    """Devuelve el reparto redondeado o None si el mercado está incompleto o no es surebet."""
    if any(o not in best_odds for o in outcomes) or total <= 0 or step <= 0:
        return None
    odds = [float(best_odds[o]["decimal_odds"]) for o in outcomes]
    implied = [1 / o for o in odds]
    probability_sum = sum(implied)
    if probability_sum >= 1:
        return None

    ideal = [total * p / probability_sum for p in implied]
    ideal_payout = total / probability_sum

    floors = [math.floor(a / step) * step for a in ideal]
    if len(outcomes) <= MAX_EXHAUSTIVE_OUTCOMES:
        candidates = itertools.product(*[(f, f + step) for f in floors])
    else:
        candidates = [[round(a / step) * step for a in ideal]]

    best = None
    for combo in candidates:
        stakes = [float(s) for s in combo]
        if min(stakes) <= 0:
            continue
        spent, min_profit, _ = _evaluate(stakes, odds)
        within_budget = spent <= total + 1e-9
        # Prioridad: no pasarse del monto, mayor ganancia mínima, gastar lo más cerca del monto.
        key = (within_budget, round(min_profit, 6), -abs(total - spent))
        if best is None or key > best[0]:
            best = (key, stakes)
    if best is None or not best[0][0]:
        # Ninguna combinación redondeada cabe en el monto: nunca proponemos apostar de más.
        return {
            "total_requested": total, "total_staked": 0, "rounding_step": step, "probability_sum": probability_sum,
            "ideal_payout": round(ideal_payout, 2), "ideal_profit": round(ideal_payout - total, 2),
            "min_profit": 0, "max_profit": 0, "min_profit_percent": 0, "guaranteed": False, "feasible": False,
            "rows": [], "warning": RISK_WARNING,
        }

    stakes = best[1]
    spent, min_profit, payouts = _evaluate(stakes, odds)
    rows = []
    for outcome, stake, odd, payout, exact in zip(outcomes, stakes, odds, payouts, ideal):
        rows.append({
            "outcome": outcome,
            "bookmaker": best_odds[outcome]["bookmaker"],
            "decimal_odds": odd,
            "stake": stake,
            "ideal_stake": round(exact, 2),
            "payout": round(payout, 2),
            "profit": round(payout - spent, 2),
        })
    return {
        "total_requested": total,
        "total_staked": round(spent, 2),
        "rounding_step": step,
        "probability_sum": probability_sum,
        "ideal_payout": round(ideal_payout, 2),
        "ideal_profit": round(ideal_payout - total, 2),
        "min_profit": round(min_profit, 2),
        "max_profit": round(max(payouts) - spent, 2),
        "min_profit_percent": round(min_profit / spent * 100, 2) if spent else 0,
        "guaranteed": min_profit > 0,
        "feasible": True,
        "rows": rows,
        "warning": RISK_WARNING,
    }


def format_stakes_text(plan: dict) -> str:
    """Texto plano del reparto para correos y para el asistente."""
    lines = [f"Reparto para ${plan['total_requested']:,.2f} (redondeo a ${plan['rounding_step']}):"]
    if not plan.get("feasible", True):
        lines.append("El monto es demasiado bajo para repartirlo con este redondeo sin pasarte; aumenta el monto o reduce el redondeo.")
        return "\n".join(lines)
    for row in plan["rows"]:
        lines.append(f"- Apuesta ${row['stake']:,.0f} a {row['outcome']} en {row['bookmaker']} (cuota {row['decimal_odds']}) "
                     f"→ si gana cobras ${row['payout']:,.2f} (ganancia ${row['profit']:,.2f})")
    lines.append(f"Total apostado: ${plan['total_staked']:,.2f}. Ganancia mínima garantizada: ${plan['min_profit']:,.2f} "
                 f"({plan['min_profit_percent']:.2f}%).")
    if not plan["guaranteed"]:
        lines.append("Con este monto y redondeo NO se garantiza ganancia en todos los resultados; aumenta el monto o reduce el redondeo.")
    return "\n".join(lines)
