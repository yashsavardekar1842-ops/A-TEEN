#!/usr/bin/env python3
"""
Aanya - Telegram AI companion / business mentor / accountability partner.
Groq (Llama 3.1) + encrypted JSON memory + proactive & conditional nagging.
Designed to run in short GitHub Actions jobs (long-poll loop of ~4.5 minutes).
"""
from __future__ import annotations

import copy
import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from cryptography.fernet import Fernet, InvalidToken

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("aanya")


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
def env(name: str, default: str = "", required: bool = False) -> str:
    value = os.environ.get(name, default).strip()
    if required and not value:
        log.error("Missing required env var: %s", name)
        sys.exit(1)
    return value


TG_TOKEN = env("TELEGRAM_BOT_TOKEN", required=True)
GROQ_KEY = env("GROQ_API_KEY", required=True)
MEM_KEY = env("MEMORY_KEY", required=True)
OWNER_ENV = env("OWNER_CHAT_ID")
MODEL = env("GROQ_MODEL", "llama-3.1-8b-instant")
BOT_NAME = env("BOT_NAME", "Aanya")
TZ = ZoneInfo(env("BOT_TZ", "Asia/Kolkata"))
RUN_SECONDS = int(env("RUN_SECONDS", "280"))
LONG_POLL = 20

TG_API = f"https://api.telegram.org/bot{TG_TOKEN}"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
MEMORY_FILE = Path("memory.enc")

try:
    FERNET = Fernet(MEM_KEY.encode())
except Exception:
    log.error("MEMORY_KEY invalid hai. Fernet key generate karke daalo.")
    sys.exit(1)

# Proactive check-ins: (name, hour, minute, goal)
SLOTS = [
    ("morning", 9, 0,
     "Good morning wala check-in. Aaj ke top 3 kaam pucho (Purix ya Tally ka kam se kam ek), "
     "aur breakfast ke baare mein pucho ya suggest karo (weight gain ke hisaab se)."),
    ("afternoon", 14, 0,
     "Lunch check-in. Pucho lunch kya khaya/kya khane wala hai, protein aur paani ka dhyan, "
     "aur subah ke kaam ka progress."),
    ("evening", 18, 0,
     "6 baje ka accountability check-in. Pucho aaj ka kaam kitna hua, kaunsa commitment pending hai. "
     "Excuse allow nahi. Shaam ke snack ka bhi pucho."),
    ("night", 22, 0,
     "Raat ka day-review. Dinner, aaj ka total khana, weight update, aaj ki jeet aur kal ka ek "
     "sabse important kaam pucho. Mood bhi check karo aur sone ko encourage karo."),
]
SLOT_WINDOW_MIN = 90           # slot is skipped if bot is later than this (cron delays)
NAG_GAPS_MIN = [30, 45, 90]    # silence gaps before nag #1, #2, #3
NAG_ALLOWED_HOURS = (8, 23)    # nags only between 08:00 and 23:00
PENDING_EXPIRY_HOURS = 5

NAG_STYLES = [
    "Halka-phulka aur funny: pucho gayab kahan ho, pyaar se.",
    "Ab thoda serious: pichhla sawal yaad dilao, excuse ki jagah action maango, halka taunt.",
    "Strict mode: ego pe touch karo, seedha aur sharp. Yaad dilao ki usne khud commit kiya tha. "
    "Sirf uske behavior pe attack karo, gaali nahi, uski worth ya family pe nahi.",
]

DEFAULT_MEMORY = {
    "version": 1,
    "owner_chat_id": None,
    "tg_offset": 0,
    "profile": {
        "name": "Boss",
        "gender": "male",
        "businesses": {
            "Purix": "D2C brand (details baatchit se update hongi)",
            "Tally": "Tally software business (details baatchit se update hongi)",
        },
        "goals": {"health": "Healthy weight gain", "business": "Purix aur Tally dono grow karna"},
        "weaknesses": ["procrastination", "excuses"],
    },
    "facts": [],
    "health": {"weights": [], "food_log": {}},
    "commitments": [],
    "mood_log": [],
    "history": [],
    "state": {
        "sent_slots": {},
        "pending": None,
        "last_user_ts": None,
        "paused": False,
    },
}

_last_saved: str | None = None


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def now_ist() -> datetime:
    return datetime.now(TZ)


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def safe(exc: Exception) -> str:
    """Error text with secrets removed (Actions logs are public in public repos)."""
    text = f"{type(exc).__name__}: {exc}"
    for secret in (TG_TOKEN, GROQ_KEY, MEM_KEY):
        if secret:
            text = text.replace(secret, "***")
    return text[:300]


def fill_defaults(data: dict, defaults: dict) -> dict:
    for key, value in defaults.items():
        if key not in data:
            data[key] = copy.deepcopy(value)
        elif isinstance(value, dict) and isinstance(data[key], dict):
            fill_defaults(data[key], value)
    return data


# --------------------------------------------------------------------------
# Memory (encrypted JSON)
# --------------------------------------------------------------------------
def load_memory() -> dict:
    global _last_saved
    if not MEMORY_FILE.exists():
        mem = copy.deepcopy(DEFAULT_MEMORY)
        if OWNER_ENV.lstrip("-").isdigit():
            mem["owner_chat_id"] = int(OWNER_ENV)
        _last_saved = None
        return mem
    try:
        raw = FERNET.decrypt(MEMORY_FILE.read_bytes())
    except InvalidToken:
        log.error("memory.enc decrypt nahi hui: MEMORY_KEY galat hai. Overwrite nahi karunga.")
        sys.exit(1)
    mem = fill_defaults(json.loads(raw), DEFAULT_MEMORY)
    if mem["owner_chat_id"] is None and OWNER_ENV.lstrip("-").isdigit():
        mem["owner_chat_id"] = int(OWNER_ENV)
    _last_saved = json.dumps(mem, ensure_ascii=False, sort_keys=True)
    return mem


def save_memory(mem: dict) -> None:
    global _last_saved
    blob = json.dumps(mem, ensure_ascii=False, sort_keys=True)
    if blob == _last_saved:
        return
    tmp = MEMORY_FILE.with_suffix(".tmp")
    tmp.write_bytes(FERNET.encrypt(blob.encode("utf-8")))
    tmp.replace(MEMORY_FILE)
    _last_saved = blob


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------
def tg(method: str, payload: dict | None = None, timeout: int = 30):
    resp = requests.post(f"{TG_API}/{method}", json=payload or {}, timeout=timeout)
    try:
        data = resp.json()
    except ValueError:
        raise RuntimeError(f"Telegram {method}: non-JSON response ({resp.status_code})")
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {method} failed: {data.get('description')}")
    return data["result"]


def send(chat_id: int, text: str) -> None:
    text = (text or "").strip() or "..."
    for i in range(0, len(text), 4000):
        tg("sendMessage", {"chat_id": chat_id, "text": text[i:i + 4000]})


def typing(chat_id: int) -> None:
    try:
        tg("sendChatAction", {"chat_id": chat_id, "action": "typing"}, timeout=10)
    except Exception:
        pass


# --------------------------------------------------------------------------
# Groq
# --------------------------------------------------------------------------
def groq_chat(messages: list, temperature: float = 0.85, max_tokens: int = 600,
              json_mode: bool = False) -> str:
    body = {
        "model": MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    headers = {"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"}

    last_err = "unknown"
    for attempt in range(4):
        try:
            r = requests.post(GROQ_URL, headers=headers, json=body, timeout=60)
        except requests.RequestException as e:
            last_err = safe(e)
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code == 429 or r.status_code >= 500:
            try:
                wait = float(r.headers.get("retry-after", 3 * (attempt + 1)))
            except ValueError:
                wait = 3 * (attempt + 1)
            last_err = f"HTTP {r.status_code}"
            time.sleep(min(wait, 20))
            continue
        if r.status_code != 200:
            raise RuntimeError(f"Groq HTTP {r.status_code}: {r.text[:200]}")
        return r.json()["choices"][0]["message"]["content"].strip()
    raise RuntimeError(f"Groq failed after retries: {last_err}")


# --------------------------------------------------------------------------
# Prompts
# --------------------------------------------------------------------------
def memory_context(mem: dict, now: datetime) -> str:
    p = mem["profile"]
    today = now.date().isoformat()
    weights = mem["health"]["weights"][-5:]
    food_today = mem["health"]["food_log"].get(today, [])
    open_c = [c for c in mem["commitments"] if c["status"] == "open"][-10:]
    moods = mem["mood_log"][-3:]
    lines = [
        f"Naam: {p['name']} | Gender: {p['gender']}",
        f"Businesses: {json.dumps(p['businesses'], ensure_ascii=False)}",
        f"Goals: {json.dumps(p['goals'], ensure_ascii=False)}",
        f"Kamzoriyan: {', '.join(p['weaknesses'])}",
        "Yaad rakhne wali baatein: " + ("; ".join(mem["facts"][-40:]) or "abhi kuch nahi"),
        "Recent weights (kg): " + (", ".join(f"{w['date']}: {w['kg']}" for w in weights) or "koi record nahi"),
        "Aaj ka khana: " + (", ".join(food_today) or "abhi tak kuch log nahi hua"),
        "Open commitments: " + (
            "; ".join(f"#{c['id']} {c['task']} (due {c['due'] or 'kabhi bhi'})" for c in open_c) or "koi nahi"),
        "Recent mood: " + (", ".join(f"{m['date']}: {m['mood']}" for m in moods) or "pata nahi"),
    ]
    return "\n".join(lines)


def build_system_prompt(mem: dict, now: datetime) -> str:
    return f"""Tu {BOT_NAME} hai: {mem['profile']['name']} ki sabse close female friend, sharp business mentor aur accountability partner.
Abhi ka time: {now.strftime('%A, %d %b %Y, %I:%M %p')} (IST).

LANGUAGE & TONE
- Sirf natural Hinglish (Roman script), jaise close dost WhatsApp pe baat karti hai. Casual, warm, thodi cheeky.
- Messages chhote rakho (zyada tar 2-5 lines). Bullet lists/lectures avoid karo jab tak maanga na jaye. Emojis kam aur natural.
- Ek message mein ek hi mukhya sawal pucho. Repeat mat karo.
- Chill mood mein normal insaan ki tarah jokes, teasing, desi references.
- Agar woh seriously pooche ki tum AI ho ya insaan, toh sach bolo ki tum AI companion ho.

MENTOR MODE
- Purix (D2C brand): CAC, ROAS, creatives, COD/RTO, margins, repeat purchase, retention, offers, reviews, supply chain.
- Tally software business: leads, demo, follow-up discipline, pricing, renewals, support, referrals.
- Human psychology use karo: habit loops, chhote wins, implementation intentions ("kab, kahan, kya"), identity-based change, procrastination ke asli reasons.
- Har baar ek concrete next action do, vague gyaan nahi.

ACCOUNTABILITY MODE
- Delay ya excuse pe: pehle pyaar se samjhao. Agar pattern repeat ho raha ho toh strict ho jao aur ego pe touch karo ("tumse yeh expected nahi tha", "bolna aasan hai, karke dikhao").
- Strict roasting sirf uske BEHAVIOR par. Gaali nahi, uski worth, family ya body ko neecha nahi dikhana.
- Jo kaam kare, uski dil se tareef karo.

HEALTH MODE (weight gain)
- Roz khane-peene, paani aur weight ka hisaab maango aur guide karo: calorie surplus, protein, har 3 ghante mein meal, Indian options (paneer, eggs, dal, rajma, banana shake, dry fruits, peanut butter, ghee, curd).
- Koi medical diagnosis ya dawai suggest mat karo. Serious issue pe doctor ya dietitian ko dikhane bolo.

LOW PHASE MODE
- Agar woh low, thaka ya tension mein lage: pehle empathy, strictness band. Phir halke suggestions: YouTube pe 432Hz/528Hz relaxing music, 10 min meditation ya breathing, dhoop mein walk, paani, thoda stretching, kisi dost se baat. Inhe "mann shaant karne ke liye achha lagta hai" bolo, medical cure ya miracle mat batao.
- Agar self-harm ya hopelessness ke signs hon: jokes band, gentle rahkar kisi bharosemand insaan ya local helpline se baat karne ko encourage karo aur saath raho.

MEMORY RULES
- Neeche di gayi memory use karo. Jo pata nahi, woh invent mat karo, pooch lo.

=== USER MEMORY ===
{memory_context(mem, now)}
"""


def history_messages(mem: dict, limit: int = 16) -> list:
    return [{"role": m["role"], "content": m["content"]} for m in mem["history"][-limit:]]


def add_history(mem: dict, role: str, content: str) -> None:
    mem["history"].append({"role": role, "content": content, "ts": iso(now_ist())})
    del mem["history"][:-40]


# --------------------------------------------------------------------------
# Memory extraction
# --------------------------------------------------------------------------
EXTRACT_SYSTEM = (
    "You are a precise information extractor. Read the user's message and output ONLY a JSON object "
    "with keys: facts (list of short durable facts about the user, in Hinglish/English, max 3), "
    "weight_kg (number or null), food (list of food/drink items the user says they ate or drank, "
    "else empty), new_commitments (list of {\"task\": str, \"due\": \"YYYY-MM-DD\" or null} for "
    "promises/plans the user commits to), completed_ids (list of integer ids of open commitments the "
    "user says are finished), mood (short string or null). Do not invent anything. Use empty values "
    "when nothing applies."
)


def extract_updates(mem: dict, user_text: str, now: datetime) -> dict:
    open_c = [{"id": c["id"], "task": c["task"]} for c in mem["commitments"] if c["status"] == "open"]
    prompt = (
        f"Aaj ki date: {now.date().isoformat()}\n"
        f"Open commitments: {json.dumps(open_c, ensure_ascii=False)}\n"
        f"User message:\n\"\"\"{user_text}\"\"\""
    )
    out = groq_chat(
        [{"role": "system", "content": EXTRACT_SYSTEM}, {"role": "user", "content": prompt}],
        temperature=0, max_tokens=400, json_mode=True,
    )
    data = json.loads(out)
    return data if isinstance(data, dict) else {}


def apply_updates(mem: dict, data: dict, now: datetime) -> None:
    today = now.date().isoformat()

    # facts
    known = {f.lower() for f in mem["facts"]}
    for f in data.get("facts") or []:
        if isinstance(f, str) and 3 <= len(f.strip()) <= 200 and f.strip().lower() not in known:
            mem["facts"].append(f.strip())
            known.add(f.strip().lower())
    del mem["facts"][:-150]

    # weight
    w = data.get("weight_kg")
    if isinstance(w, (int, float)) and not isinstance(w, bool) and 20 <= w <= 300:
        weights = mem["health"]["weights"]
        if weights and weights[-1]["date"] == today:
            weights[-1]["kg"] = float(w)
        else:
            weights.append({"date": today, "kg": float(w)})
        del weights[:-365]

    # food
    food = [x.strip() for x in (data.get("food") or []) if isinstance(x, str) and x.strip()]
    if food:
        mem["health"]["food_log"].setdefault(today, []).extend(food[:10])
        fl = mem["health"]["food_log"]
        for k in sorted(fl)[:-60]:
            del fl[k]

    # commitments
    cs = mem["commitments"]
    next_id = max((c["id"] for c in cs), default=0) + 1
    for c in data.get("new_commitments") or []:
        if isinstance(c, dict) and isinstance(c.get("task"), str) and c["task"].strip():
            due = c.get("due")
            due = due if isinstance(due, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", due) else None
            cs.append({"id": next_id, "task": c["task"].strip()[:200], "due": due,
                       "status": "open", "created": today})
            next_id += 1
    done_ids = {i for i in (data.get("completed_ids") or []) if isinstance(i, int)}
    for c in cs:
        if c["id"] in done_ids and c["status"] == "open":
            c["status"] = "done"
            c["done_on"] = today
    open_ = [c for c in cs if c["status"] == "open"][-50:]
    closed = [c for c in cs if c["status"] != "open"][-50:]
    mem["commitments"] = sorted(open_ + closed, key=lambda c: c["id"])

    # mood
    mood = data.get("mood")
    if isinstance(mood, str) and mood.strip():
        mem["mood_log"].append({"date": today, "mood": mood.strip()[:60]})
        del mem["mood_log"][:-60]


# --------------------------------------------------------------------------
# Conversation handling
# --------------------------------------------------------------------------
def mark_user_active(mem: dict, now: datetime) -> None:
    mem["state"]["pending"] = None          # user replied, nagging loop reset
    mem["state"]["last_user_ts"] = iso(now)


def handle_user_messages(mem: dict, chat_id: int, texts: list) -> None:
    now = now_ist()
    mark_user_active(mem, now)
    for t in texts:
        add_history(mem, "user", t)

    typing(chat_id)
    try:
        messages = [{"role": "system", "content": build_system_prompt(mem, now)}] + history_messages(mem)
        reply = groq_chat(messages)
    except Exception as e:
        log.error("Reply generation failed: %s", safe(e))
        send(chat_id, "Yaar thoda network/server issue aa gaya, 2 min mein dobara bhej na 🙏")
        return

    add_history(mem, "assistant", reply)
    send(chat_id, reply)

    # long-term memory update (separate call so a failure never blocks the reply)
    try:
        apply_updates(mem, extract_updates(mem, "\n".join(texts), now), now)
    except Exception as e:
        log.warning("Memory extraction skipped: %s", safe(e))


def handle_command(mem: dict, chat_id: int, text: str) -> None:
    cmd = text.split()[0].split("@")[0].lower()
    now = now_ist()
    mark_user_active(mem, now)

    if cmd == "/start":
        send(chat_id,
             f"Heyy! Main {BOT_NAME} hoon 😄 teri dost, business mentor aur tera accountability partner. "
             "Excuses yahan nahi chalenge, par saath hamesha rahungi.\n\n"
             "Chal shuru karte hain: apna naam, abhi ka weight aur height bata. "
             "Aur Purix aur Tally ke baare mein 2-2 lines mein samjha de.")
    elif cmd == "/memory":
        p = mem["profile"]
        w = mem["health"]["weights"]
        open_c = [c for c in mem["commitments"] if c["status"] == "open"]
        lines = [f"Naam: {p['name']}",
                 f"Last weight: {w[-1]['kg']} kg ({w[-1]['date']})" if w else "Weight: record nahi",
                 f"Facts yaad hain: {len(mem['facts'])}",
                 "Open commitments:"]
        lines += [f"- #{c['id']} {c['task']} (due {c['due'] or 'any'})" for c in open_c] or ["- koi nahi"]
        if mem["facts"]:
            lines.append("Recent facts:")
            lines += [f"- {f}" for f in mem["facts"][-8:]]
        send(chat_id, "\n".join(lines))
    elif cmd == "/pause":
        mem["state"]["paused"] = True
        send(chat_id, "Theek hai, proactive messages aur nagging pause. Wapas chahiye toh /resume likhna 😌")
    elif cmd == "/resume":
        mem["state"]["paused"] = False
        send(chat_id, "Wapas aa gayi! Ab bachke dikha 😎")
    else:
        send(chat_id, "Commands: /start, /memory, /pause, /resume")


# --------------------------------------------------------------------------
# Proactive messages + conditional nagging
# --------------------------------------------------------------------------
def generate_proactive(mem: dict, goal: str, slot: str, now: datetime) -> str:
    trigger = (
        f"[INTERNAL, yeh user ne nahi bheja] Abhi {slot} check-in ka time hai. Tumhara kaam: {goal} "
        "Pichhli chat aur memory ka dhyan rakho. Sirf ek natural Telegram message likho "
        "(max 5 lines), ek hi mukhya sawal ke saath."
    )
    messages = ([{"role": "system", "content": build_system_prompt(mem, now)}]
                + history_messages(mem, 10) + [{"role": "user", "content": trigger}])
    return groq_chat(messages, temperature=0.9)


def generate_nag(mem: dict, level: int, silent_min: int, now: datetime) -> str:
    trigger = (
        f"[INTERNAL, yeh user ne nahi bheja] User ne {silent_min} minute se tumhare pichhle message ka "
        f"reply nahi diya. Yeh nag #{level + 1} hai. Style: {NAG_STYLES[level]} "
        "Sirf ek chhota Telegram message likho (max 3 lines)."
    )
    messages = ([{"role": "system", "content": build_system_prompt(mem, now)}]
                + history_messages(mem, 10) + [{"role": "user", "content": trigger}])
    return groq_chat(messages, temperature=0.95, max_tokens=300)


def check_slots(mem: dict, chat_id: int, now: datetime) -> None:
    st = mem["state"]
    if st["paused"]:
        return
    today = now.date().isoformat()
    sent = st["sent_slots"]
    for d in [d for d in sent if d != today]:
        del sent[d]
    done_today = sent.setdefault(today, [])

    for name, hour, minute, goal in SLOTS:
        if name in done_today:
            continue
        slot_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        delta_min = (now - slot_dt).total_seconds() / 60
        if 0 <= delta_min <= SLOT_WINDOW_MIN:
            try:
                typing(chat_id)
                text = generate_proactive(mem, goal, name, now)
                send(chat_id, text)
            except Exception as e:
                log.error("Proactive %s failed: %s", name, safe(e))
                return
            done_today.append(name)
            add_history(mem, "assistant", text)
            st["pending"] = {"slot": name, "sent_ts": iso(now), "last_ts": iso(now), "nags": 0}
            return  # one proactive message per loop


def check_nag(mem: dict, chat_id: int, now: datetime) -> None:
    st = mem["state"]
    p = st.get("pending")
    if not p or st["paused"]:
        return
    sent_ts = datetime.fromisoformat(p["sent_ts"])
    if (now - sent_ts).total_seconds() > PENDING_EXPIRY_HOURS * 3600:
        st["pending"] = None
        return
    if not (NAG_ALLOWED_HOURS[0] <= now.hour < NAG_ALLOWED_HOURS[1]):
        return
    n = p["nags"]
    if n >= len(NAG_GAPS_MIN):
        return
    last_ts = datetime.fromisoformat(p["last_ts"])
    if (now - last_ts).total_seconds() / 60 < NAG_GAPS_MIN[n]:
        return
    silent_min = int((now - sent_ts).total_seconds() / 60)
    try:
        typing(chat_id)
        text = generate_nag(mem, n, silent_min, now)
        send(chat_id, text)
    except Exception as e:
        log.error("Nag failed: %s", safe(e))
        return
    add_history(mem, "assistant", text)
    p["nags"] += 1
    p["last_ts"] = iso(now)


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------
def poll_once(mem: dict, deadline: float) -> None:
    timeout = int(min(LONG_POLL, deadline - time.monotonic() - 2))
    updates = []
    if timeout >= 1:
        updates = tg("getUpdates",
                     {"offset": mem["tg_offset"], "timeout": timeout, "allowed_updates": ["message"]},
                     timeout=timeout + 15)

    texts: list = []
    for u in updates:
        mem["tg_offset"] = u["update_id"] + 1
        msg = u.get("message")
        if not msg or msg.get("chat", {}).get("type") != "private":
            continue
        cid = msg["chat"]["id"]
        if mem["owner_chat_id"] is None:
            mem["owner_chat_id"] = cid          # first person to message becomes owner
        if cid != mem["owner_chat_id"]:
            try:
                send(cid, "Yeh ek private bot hai 🙂")
            except Exception:
                pass
            continue
        text = msg.get("text")
        if not text:
            mark_user_active(mem, now_ist())
            send(cid, "Abhi main sirf text samajh paati hoon yaar, type karke bata 🙂")
        elif text.startswith("/"):
            handle_command(mem, cid, text)
        else:
            texts.append(text)

    owner = mem["owner_chat_id"]
    if owner is None:
        return
    if texts:
        handle_user_messages(mem, owner, texts)
    now = now_ist()
    check_slots(mem, owner, now)
    check_nag(mem, owner, now)


def main() -> None:
    mem = load_memory()
    deadline = time.monotonic() + RUN_SECONDS
    log.info("Run started (model=%s)", MODEL)
    try:
        while time.monotonic() < deadline - 3:
            try:
                poll_once(mem, deadline)
            except Exception as e:
                log.error("Loop error: %s", safe(e))
                time.sleep(5)
            save_memory(mem)
    finally:
        save_memory(mem)
    log.info("Run finished")


if __name__ == "__main__":
    main()
