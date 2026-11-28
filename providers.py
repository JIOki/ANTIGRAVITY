"""Proveedores de cuotas por API.

Cada proveedor convierte la respuesta de su API a una lista de `ProviderEvent`
con cuotas decimales. Para agregar otro proveedor, implementa `OddsProvider`
y regístralo en `PROVIDERS`.
"""
from __future__ import annotations

import json
import os
import ssl
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


class ProviderError(Exception):
    """Error al consultar un proveedor; el mensaje se muestra al usuario."""


@dataclass
class ProviderQuote:
    outcome: str
    bookmaker: str
    decimal_odds: float


@dataclass
class ProviderEvent:
    external_id: str
    name: str
    sport: str
    market: str
    starts_at: str
    outcomes: list[str]
    quotes: list[ProviderQuote] = field(default_factory=list)


@dataclass
class FetchResult:
    events: list[ProviderEvent]
    requests_remaining: int | None = None
    requests_used: int | None = None
    requests_last: int | None = None


class OddsProvider:
    key = ""
    title = ""
    regions: dict[str, str] = {}
    api_key_env = ""

    @property
    def api_key(self) -> str:
        return os.environ.get(self.api_key_env, "").strip()

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def list_sports(self) -> list[dict]:
        raise NotImplementedError

    def fetch_odds(self, sport_key: str, regions: list[str], horizon_hours: int) -> FetchResult:
        raise NotImplementedError

    def credits_per_call(self, regions: list[str]) -> int:
        return 1


class TheOddsAPI(OddsProvider):
    """https://the-odds-api.com — mercado h2h (ganador), cuotas decimales."""

    key = "the-odds-api"
    title = "The Odds API"
    api_key_env = "ODDS_API_KEY"
    base_url = "https://api.the-odds-api.com/v4"
    regions = {"us": "Estados Unidos", "us2": "Estados Unidos 2", "uk": "Reino Unido", "eu": "Europa", "au": "Australia"}
    SPORTS_CACHE_SECONDS = 6 * 3600

    def __init__(self) -> None:
        self._sports_cache: tuple[float, list[dict]] | None = None

    def credits_per_call(self, regions: list[str]) -> int:
        return len(regions)  # 1 mercado (h2h) x número de regiones

    def _get(self, path: str, params: dict) -> tuple[object, dict]:
        if not self.configured:
            raise ProviderError(f"Falta la variable de entorno {self.api_key_env} con tu clave de {self.title}.")
        query = urlencode({**params, "apiKey": self.api_key})
        request = Request(f"{self.base_url}{path}?{query}", headers={"Accept": "application/json", "User-Agent": "CuotasClaro/0.3"})
        try:
            with urlopen(request, timeout=20, context=ssl.create_default_context()) as response:
                return json.loads(response.read()), dict(response.headers)
        except HTTPError as exc:
            detail = ""
            try:
                detail = json.loads(exc.read()).get("message", "")
            except (ValueError, AttributeError):
                pass
            if exc.code == 401:
                raise ProviderError(f"{self.title}: clave inválida o sin créditos. {detail}".strip())
            if exc.code == 429:
                raise ProviderError(f"{self.title}: límite de solicitudes alcanzado. {detail}".strip())
            raise ProviderError(f"{self.title} respondió {exc.code}. {detail}".strip())
        except (URLError, TimeoutError, ValueError) as exc:
            raise ProviderError(f"No se pudo consultar {self.title}: {exc.__class__.__name__}.")

    def list_sports(self) -> list[dict]:
        if self._sports_cache and time.monotonic() - self._sports_cache[0] < self.SPORTS_CACHE_SECONDS:
            return self._sports_cache[1]
        data, _ = self._get("/sports/", {})  # no consume créditos
        sports = [
            {"key": s["key"], "title": s.get("title", s["key"]), "group": s.get("group", "")}
            for s in data if isinstance(s, dict) and s.get("key") and not s.get("has_outrights")
        ]
        sports.sort(key=lambda s: (s["group"], s["title"]))
        self._sports_cache = (time.monotonic(), sports)
        return sports

    def list_events(self, sport_key: str, hours: int = 168) -> list[dict]:
        """Calendario de partidos sin cuotas: no consume créditos."""
        until = datetime.now(timezone.utc) + timedelta(hours=hours)
        data, _ = self._get(f"/sports/{quote(sport_key, safe='')}/events/",
                            {"dateFormat": "iso", "commenceTimeTo": until.strftime("%Y-%m-%dT%H:%M:%SZ")})
        if not isinstance(data, list):
            raise ProviderError(f"{self.title} devolvió un calendario inesperado.")
        return [{"id": str(e["id"]), "commence_time": str(e["commence_time"])} for e in data
                if isinstance(e, dict) and e.get("id") and e.get("commence_time")]

    def fetch_odds(self, sport_key: str, regions: list[str], horizon_hours: int) -> FetchResult:
        bad = [r for r in regions if r not in self.regions]
        if bad or not regions:
            raise ProviderError("Regiones inválidas.")
        until = datetime.now(timezone.utc) + timedelta(hours=horizon_hours)
        data, headers = self._get(f"/sports/{quote(sport_key, safe='')}/odds/", {
            "regions": ",".join(regions),
            "markets": "h2h",
            "oddsFormat": "decimal",
            "dateFormat": "iso",
            "commenceTimeTo": until.strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
        if not isinstance(data, list):
            raise ProviderError(f"{self.title} devolvió una respuesta inesperada.")
        lower = {k.lower(): v for k, v in headers.items()}

        def header_int(name: str) -> int | None:
            try:
                return int(float(lower[name]))
            except (KeyError, ValueError):
                return None

        return FetchResult(
            events=[event for event in (self._parse_event(item) for item in data) if event],
            requests_remaining=header_int("x-requests-remaining"),
            requests_used=header_int("x-requests-used"),
            requests_last=header_int("x-requests-last"),
        )

    @staticmethod
    def _parse_event(item: dict) -> ProviderEvent | None:
        if not isinstance(item, dict) or not item.get("id"):
            return None
        books = []
        for bookmaker in item.get("bookmakers") or []:
            market = next((m for m in bookmaker.get("markets") or [] if m.get("key") == "h2h"), None)
            if not market:
                continue
            prices = {}
            for outcome in market.get("outcomes") or []:
                try:
                    price = float(outcome["price"])
                except (KeyError, TypeError, ValueError):
                    continue
                if outcome.get("name") and price > 1:
                    name = str(outcome["name"])
                    prices["Empate" if name == "Draw" else name] = price
            if len(prices) >= 2:
                books.append((bookmaker.get("title") or bookmaker.get("key") or "?", prices))
        if not books:
            return None
        # Solo se combinan casas que ofrecen exactamente el mismo conjunto de resultados
        # (p. ej., en fútbol se descartan casas que publiquen un mercado sin empate).
        sets = Counter(frozenset(prices) for _, prices in books)
        outcome_set = sets.most_common(1)[0][0]
        home, away = item.get("home_team"), item.get("away_team")
        order = [o for o in (home, away) if o in outcome_set] + sorted(o for o in outcome_set if o not in (home, away))
        quotes = [ProviderQuote(outcome, title, prices[outcome])
                  for title, prices in books if frozenset(prices) == outcome_set for outcome in order]
        name = f"{home} vs {away}" if home and away else str(item.get("sport_title", "Evento"))
        return ProviderEvent(
            external_id=str(item["id"]),
            name=name,
            sport=str(item.get("sport_title", "")),
            market="Ganador (h2h)",
            starts_at=str(item.get("commence_time", "")),
            outcomes=order,
            quotes=quotes,
        )


PROVIDERS: dict[str, OddsProvider] = {provider.key: provider for provider in (TheOddsAPI(),)}
