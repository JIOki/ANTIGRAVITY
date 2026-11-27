"""Asistente de Cuotas Claro: base de conocimiento + Ollama, con respaldo sin IA y botones de acción."""
from __future__ import annotations

import json
import os
import re
import threading
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator
from urllib.error import URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
KNOWLEDGE_PATH = ROOT / "assistant" / "knowledge.md"

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://ollama:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gemma3:4b")
OLLAMA_TIMEOUT = int(os.environ.get("OLLAMA_TIMEOUT", "120"))
OLLAMA_NUM_CTX = int(os.environ.get("OLLAMA_NUM_CTX", "6144"))
ASSISTANT_ENABLED = os.environ.get("ASSISTANT_AI", "true").lower() == "true"
# Ollama en CPU atiende una respuesta a la vez; las demás esperan o usan el respaldo.
OLLAMA_SLOTS = threading.BoundedSemaphore(int(os.environ.get("OLLAMA_CONCURRENCY", "2")))
QUEUE_WAIT_SECONDS = 25

MAX_MESSAGE_CHARS = 800
MAX_HISTORY = 6
BUTTONS_MARKER = "BOTONES:"
MAX_BUTTONS = 4

# Catálogo de botones. "action" lo ejecuta el navegador; "prompt" envía una pregunta; "href" abre un enlace.
ACTIONS: dict[str, dict] = {
    "crear_cuenta": {"label": "Crear cuenta", "action": "open_register", "guest": True},
    "iniciar_sesion": {"label": "Iniciar sesión", "action": "open_login", "guest": True},
    "nuevo_evento": {"label": "＋ Nuevo evento", "action": "new_event", "user": True},
    "ir_eventos": {"label": "Ver mis eventos", "action": "goto:events-list", "user": True},
    "ir_api": {"label": "Ir a Cuotas por API", "action": "goto:integrations-panel", "user": True},
    "ir_correo": {"label": "Configurar correo", "action": "goto:email-form", "user": True},
    "ir_ajustes": {"label": "Umbral, vigencia y reparto", "action": "goto:settings-form", "user": True},
    "ver_reparto": {"label": "Ver reparto en mis eventos", "action": "goto:events-list", "user": True},
    "sincronizar": {"label": "Sincronizar ahora", "action": "sync_now", "user": True},
    "como_empezar": {"label": "¿Cómo empiezo?", "prompt": "¿Cómo empiezo a usar Cuotas Claro?"},
    "explicar_calculo": {"label": "¿Cómo se calcula?", "prompt": "¿Cómo se calcula el underround?"},
    "explicar_vencidas": {"label": "Cuotas vencidas", "prompt": "¿Qué es una cuota vencida?"},
    "explicar_api": {"label": "¿Qué es Cuotas por API?", "prompt": "¿Cómo funcionan las cuotas por API?"},
    "explicar_reparto": {"label": "¿Cuánto apuesto en cada casa?", "prompt": "¿Cómo calcula el sistema cuánto apostar en cada casa?"},
    "sitio_odds_api": {"label": "Sitio de The Odds API ↗", "href": "https://the-odds-api.com"},
}

# Palabras clave → acciones sugeridas (se usan además de las que proponga el modelo).
KEYWORD_ACTIONS = [
    (("registr", "cuenta", "crear cuenta", "unirme"), ["crear_cuenta"]),
    (("iniciar sesion", "entrar", "login", "contrasena"), ["iniciar_sesion"]),
    (("nuevo evento", "crear evento", "agregar evento", "registrar evento", "capturar", "cuota de"), ["nuevo_evento"]),
    (("mis eventos", "tarjeta", "historial", "eliminar", "editar", "otra casa", "comparativa", "no aparece", "no se agrega",
      "momio", "caliente", "codere", "playdoit"), ["ir_eventos"]),
    (("api", "the odds", "liga", "importar", "automatic", "sincroniz", "credito"), ["ir_api", "explicar_api"]),
    (("correo", "email", "mail", "notific", "aviso"), ["ir_correo"]),
    (("umbral", "vigencia", "ajuste", "98", "minutos"), ["ir_ajustes"]),
    (("vencid", "reemplazad", "antigua", "vieja"), ["explicar_vencidas", "ir_ajustes"]),
    (("cuanto apost", "cuanto le apuest", "reparto", "repartir", "monto", "redonde", "surebet", "arbitraje", "cuanto poner", "cuanto meto"),
     ["ver_reparto", "explicar_reparto"]),
    (("calcul", "underround", "probabilidad", "suma", "formula", "margen"), ["explicar_calculo"]),
    (("empez", "comenz", "como uso", "como funciona", "primer"), ["como_empezar"]),
]

GREETING = ("¡Hola! Soy el asistente de **Cuotas Claro**. Te ayudo a encontrar surebets: registrar eventos de varias casas, "
            "entender el cálculo, saber cuánto apostar en cada casa, configurar alertas y conectar cuotas por API. ¿Qué quieres hacer?")
GREETING_ACTIONS_GUEST = ["como_empezar", "explicar_calculo", "explicar_reparto", "crear_cuenta"]
GREETING_ACTIONS_USER = ["nuevo_evento", "ver_reparto", "explicar_reparto", "ir_api"]


@dataclass
class Section:
    title: str
    body: str
    actions: list[str]
    tokens: set[str]
    title_tokens: set[str]


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    return "".join(ch for ch in text if not unicodedata.combining(ch))


STOPWORDS = set("""a al algo como con cual cuando de del el en es esta este esto la las lo los me mi mis no o para pero
por puedo que se si sin sobre su sus te tengo tu un una uno y ya hay hace hacer quiero puede son ser""".split())


def tokenize(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", normalize(text))
    # Raíz simple de 5 letras para tolerar plurales y conjugaciones (vencida/vencidas, calcula/calculo).
    return {w[:5] for w in words if len(w) > 2 and w not in STOPWORDS}


def load_knowledge(path: Path = KNOWLEDGE_PATH) -> tuple[str, list[Section]]:
    raw = path.read_text(encoding="utf-8")
    raw = re.sub(r"<!--.*?-->", "", raw, flags=re.S)
    sections = []
    for chunk in re.split(r"^## ", raw, flags=re.M)[1:]:
        title, _, body = chunk.partition("\n")
        actions: list[str] = []
        lines = []
        for line in body.strip().splitlines():
            if line.startswith("acciones:"):
                actions = [a.strip() for a in line.split(":", 1)[1].split(",") if a.strip() in ACTIONS]
            else:
                lines.append(line)
        text = "\n".join(lines).strip()
        sections.append(Section(title.strip(), text, actions, tokenize(text), tokenize(title)))
    prompt_text = "\n\n".join(f"### {s.title}\n{s.body}" for s in sections)
    return prompt_text, sections


KNOWLEDGE_TEXT, SECTIONS = load_knowledge()


# --------------------------------------------------------------- botones

def available(key: str, logged_in: bool) -> bool:
    action = ACTIONS.get(key)
    if not action:
        return False
    if action.get("user") and not logged_in:
        return False
    if action.get("guest") and logged_in:
        return False
    return True


def build_buttons(keys: list[str], logged_in: bool) -> list[dict]:
    buttons, seen = [], set()
    for key in keys:
        # Para visitantes, las acciones de panel se convierten en "Iniciar sesión".
        if key in ACTIONS and ACTIONS[key].get("user") and not logged_in:
            key = "iniciar_sesion"
        if key in seen or not available(key, logged_in):
            continue
        seen.add(key)
        action = {k: v for k, v in ACTIONS[key].items() if k not in ("guest", "user")}
        buttons.append({"id": key, **action})
        if len(buttons) == MAX_BUTTONS:
            break
    return buttons


def keyword_actions(text: str) -> list[str]:
    norm = normalize(text)
    result: list[str] = []
    for keywords, actions in KEYWORD_ACTIONS:
        if any(k in norm for k in keywords):
            result.extend(a for a in actions if a not in result)
    return result


def parse_marker(text: str) -> tuple[str, list[str]]:
    index = text.upper().find(BUTTONS_MARKER)
    if index < 0:
        return text.strip(), []
    keys = [k.strip().strip(".[]`*").lower() for k in re.split(r"[,\s]+", text[index + len(BUTTONS_MARKER):]) if k.strip()]
    return text[:index].rstrip(), [k for k in keys if k in ACTIONS]


# ------------------------------------------------------- respaldo sin IA

def best_sections(question: str, limit: int = 2) -> list[Section]:
    query = tokenize(question)
    if not query:
        return []
    scored = sorted(((3 * len(query & s.title_tokens) + len(query & s.tokens), i, s) for i, s in enumerate(SECTIONS)),
                    key=lambda x: (-x[0], x[1]))
    return [s for score, _, s in scored[:limit] if score > 0]


GREETING_WORDS = {"hola", "buenas", "buenos", "buen", "hey", "saludos", "que tal", "gracias", "ok", "listo"}


def fallback_answer(question: str, logged_in: bool, reason: str = "") -> dict:
    words = re.findall(r"[a-z]+", normalize(question))
    if words and len(words) <= 4 and all(w in GREETING_WORDS or w in STOPWORDS or w in ("dias", "tardes", "noches", "muchas") for w in words):
        thanks = any(w in ("gracias", "ok", "listo") for w in words)
        text = "¡Con gusto! ¿Te ayudo con algo más?" if thanks else GREETING
        return {"text": text, "buttons": build_buttons(GREETING_ACTIONS_USER if logged_in else GREETING_ACTIONS_GUEST, logged_in)}
    sections = best_sections(question, 1)
    if sections:
        section = sections[0]
        text = f"**{section.title}**\n{section.body}"
        keys = section.actions + keyword_actions(question)
        if not build_buttons(keys, logged_in):
            keys += GREETING_ACTIONS_USER if logged_in else GREETING_ACTIONS_GUEST
    else:
        text = ("No encontré ese tema en la información de Cuotas Claro. Puedo ayudarte con: cómo empezar, "
                "cómo se calcula el underround, cuotas vencidas, alertas por correo, umbral y vigencia, cuotas por API y tu cuenta.")
        keys = GREETING_ACTIONS_USER if logged_in else GREETING_ACTIONS_GUEST
    if reason:
        text += f"\n\n_{reason}_"
    return {"text": text, "buttons": build_buttons(keys, logged_in)}


# ------------------------------------------------------------- Ollama

def system_prompt(logged_in: bool, user_context: str) -> str:
    allowed = [k for k in ACTIONS if available(k, logged_in)]
    who = ("El usuario YA inició sesión; puedes guiarlo dentro de su panel. Si pregunta cuánto apostar, usa EXACTAMENTE los "
           "repartos que aparecen en el CONTEXTO DEL USUARIO (no los recalcules ni inventes cifras); para otro monto, dile que lo "
           "cambie en la tarjeta del evento y pulse \"Recalcular\" o en \"Umbral, vigencia y reparto\". Si un evento no aparece con "
           "reparto, explica por qué (incompleto o suma de 100% o más). Siempre que des un reparto, agrega la advertencia de riesgos."
           if logged_in else
           "El usuario es un visitante sin sesión. El cálculo de cuánto apostar en cada casa (reparto) es SOLO para usuarios con "
           "sesión: si lo pide, NO calcules montos; explica que debe crear una cuenta o iniciar sesión para obtenerlo.")
    return f"""Eres el asistente oficial de Cuotas Claro. Respondes SIEMPRE en español de México, con tono amable, claro y breve (máximo 120 palabras), usando viñetas o pasos numerados cuando ayuden.

Reglas:
- Responde solo con la información de la BASE DE CONOCIMIENTO y del CONTEXTO DEL USUARIO. Si algo no está ahí, dilo y ofrece los temas que sí conoces. No inventes funciones, precios ni casas de apuestas.
- Solo hablas de Cuotas Claro y de cómo usarlo. Si preguntan otra cosa, redirige amablemente.
- No recomiendes apuestas concretas ni prometas ganancias. Si hablan de ganar dinero, recuerda las advertencias.
- Para cálculos, usa 1/cuota y muestra el procedimiento.
- {who}
- Al final escribe una línea exactamente así: {BUTTONS_MARKER} seguido de 1 a 3 acciones separadas por coma, elegidas SOLO de esta lista: {", ".join(allowed)}

BASE DE CONOCIMIENTO:
{KNOWLEDGE_TEXT}

CONTEXTO DEL USUARIO:
{user_context or "Sin datos."}"""


def clean_history(history: object) -> list[dict]:
    messages = []
    if isinstance(history, list):
        for item in history[-MAX_HISTORY:]:
            if isinstance(item, dict) and item.get("role") in ("user", "assistant"):
                content = str(item.get("content", ""))[:MAX_MESSAGE_CHARS]
                if content.strip():
                    messages.append({"role": item["role"], "content": content})
    return messages


def stream_ollama(messages: list[dict]) -> Iterator[str]:
    payload = json.dumps({
        "model": OLLAMA_MODEL,
        "messages": messages,
        "stream": True,
        "keep_alive": "30m",
        "options": {"temperature": 0.3, "num_ctx": OLLAMA_NUM_CTX, "num_predict": 400},
    }).encode()
    request = Request(f"{OLLAMA_URL}/api/chat", data=payload, headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=OLLAMA_TIMEOUT) as response:
        for line in response:
            if not line.strip():
                continue
            data = json.loads(line)
            if data.get("error"):
                raise RuntimeError(data["error"])
            chunk = data.get("message", {}).get("content", "")
            if chunk:
                yield chunk
            if data.get("done"):
                break


def answer_stream(question: str, history: object, logged_in: bool, user_context: str = "") -> Iterator[dict]:
    """Produce eventos {"type": "delta"|"done", ...}. Siempre termina con "done" e incluye botones."""
    question = question.strip()[:MAX_MESSAGE_CHARS]
    if not ASSISTANT_ENABLED:
        yield {"type": "done", **fallback_answer(question, logged_in), "source": "kb"}
        return
    if not OLLAMA_SLOTS.acquire(timeout=QUEUE_WAIT_SECONDS):
        yield {"type": "done", **fallback_answer(question, logged_in, "El asistente con IA está ocupado; te respondí con la guía rápida."), "source": "kb"}
        return
    full = ""
    emitted = 0
    try:  # el finally libera el turno aunque el cliente cierre la conexión a media respuesta
        messages = [{"role": "system", "content": system_prompt(logged_in, user_context)},
                    *clean_history(history), {"role": "user", "content": question}]
        try:
            for chunk in stream_ollama(messages):
                full += chunk
                marker = full.upper().find(BUTTONS_MARKER)
                # No mostramos la línea de botones; retenemos el final por si el marcador viene partido.
                safe_end = marker if marker >= 0 else max(emitted, len(full) - len(BUTTONS_MARKER))
                if safe_end > emitted:
                    yield {"type": "delta", "text": full[emitted:safe_end]}
                    emitted = safe_end
        except (URLError, OSError, ValueError, RuntimeError) as exc:
            if emitted == 0:
                yield {"type": "done", **fallback_answer(question, logged_in, "La IA no está disponible en este momento; te respondí con la guía rápida."),
                       "source": "kb", "error": exc.__class__.__name__}
                return
            text, keys = parse_marker(full)
            yield {"type": "done", "text": text, "buttons": build_buttons(keys + keyword_actions(question), logged_in),
                   "source": "ai", "partial": True}
            return
    finally:
        OLLAMA_SLOTS.release()
    text, keys = parse_marker(full)
    if not text:
        yield {"type": "done", **fallback_answer(question, logged_in), "source": "kb"}
        return
    keys = keys + keyword_actions(question + " " + text)
    yield {"type": "done", "text": text, "buttons": build_buttons(keys, logged_in), "source": "ai"}


def greeting(logged_in: bool) -> dict:
    return {"text": GREETING, "buttons": build_buttons(GREETING_ACTIONS_USER if logged_in else GREETING_ACTIONS_GUEST, logged_in),
            "ai": ASSISTANT_ENABLED, "model": OLLAMA_MODEL if ASSISTANT_ENABLED else None}


def ollama_status() -> dict:
    """Comprueba si Ollama responde y si el modelo está descargado."""
    try:
        with urlopen(Request(f"{OLLAMA_URL}/api/tags"), timeout=5) as response:
            models = [m.get("name", "") for m in json.loads(response.read()).get("models", [])]
    except (URLError, OSError, ValueError) as exc:
        return {"reachable": False, "error": exc.__class__.__name__, "model": OLLAMA_MODEL}
    has_model = any(m == OLLAMA_MODEL or m.split(":")[0] == OLLAMA_MODEL for m in models)
    return {"reachable": True, "model": OLLAMA_MODEL, "model_available": has_model, "models": models}


def ensure_model() -> None:
    """Al arrancar, descarga el modelo en Ollama si falta (en segundo plano)."""
    status = ollama_status()
    print(f"[asistente] Ollama: {status}")
    if status.get("reachable") and not status.get("model_available"):
        print(f"[asistente] Descargando {OLLAMA_MODEL} en Ollama…")
        try:
            request = Request(f"{OLLAMA_URL}/api/pull", data=json.dumps({"model": OLLAMA_MODEL, "stream": False}).encode(),
                              headers={"Content-Type": "application/json"})
            with urlopen(request, timeout=3600) as response:
                print(f"[asistente] Descarga: {response.read()[:200]!r}")
        except (URLError, OSError) as exc:
            print(f"[asistente] No se pudo descargar el modelo: {exc!r}")
