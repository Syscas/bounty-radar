#!/usr/bin/env python3
"""
Bounty Radar - bot Telegram multi-utilisateurs pour Superteam Earn.

Chaque utilisateur regle son profil (pays, competences, recompense minimale,
audience oui/non) et recoit une alerte des qu'une nouvelle mission Superteam
Earn lui correspond. Les missions reservees a une autre region ou jugees
surtout sur l'audience sont filtrees ou signalees.

Une seule dependance : requests. Stockage : SQLite local.
Lancement : python bounty_radar.py   (liste rapide : python bounty_radar.py scan-once)
"""
import html
import json
import os
import re
import sqlite3
import sys
import time
import unicodedata
from datetime import datetime, timezone

import requests

# ---------------------------------------------------------------- config

def load_env(path=".env"):
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

load_env()
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
BASE_URL = os.environ.get("SUPERTEAM_BASE_URL", "https://superteam.fun").rstrip("/")
SCAN_INTERVAL_MIN = float(os.environ.get("SCAN_INTERVAL_MIN", "10"))
LISTING_TYPES = [t.strip() for t in os.environ.get("LISTING_TYPES", "bounty,project").split(",") if t.strip()]
DB_PATH = os.environ.get("DB_PATH", "bounty_radar.db")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "")
AUDIENCE_THRESHOLD = float(os.environ.get("AUDIENCE_THRESHOLD", "40"))
TG = f"https://api.telegram.org/bot{BOT_TOKEN}"
HTTP = requests.Session()
HTTP.headers["User-Agent"] = "BountyRadar/1.0 (+telegram bot)"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

SKILLS = ["Frontend", "Backend", "Blockchain", "Mobile", "Design",
          "Community", "Growth", "Content", "Other"]
SKILL_FR = {"Frontend": "Frontend", "Backend": "Backend", "Blockchain": "Blockchain",
            "Mobile": "Mobile", "Design": "Design", "Community": "Communaute",
            "Growth": "Growth / marketing", "Content": "Contenu / redaction", "Other": "Autre"}

# Groupes de regions utilises par Superteam Earn (src/constants/country.ts)
REGION_GROUPS = {
    "european union": ["Austria", "Belgium", "Bulgaria", "Croatia", "Cyprus", "Czech Republic",
                       "Denmark", "Estonia", "Finland", "France", "Germany", "Greece", "Hungary",
                       "Ireland", "Italy", "Latvia", "Lithuania", "Luxembourg", "Malta",
                       "Netherlands", "Poland", "Portugal", "Romania", "Slovakia", "Slovenia",
                       "Spain", "Sweden"],
    "gulf cooperation council": ["Saudi Arabia", "United Arab Emirates", "Qatar", "Kuwait",
                                 "Bahrain", "Oman"],
    "north america": ["United States", "Canada"],
    "latin america": ["Mexico", "Guatemala", "Honduras", "El Salvador", "Nicaragua", "Costa Rica",
                      "Panama", "Colombia", "Venezuela", "Ecuador", "Peru", "Bolivia", "Brazil",
                      "Paraguay", "Uruguay", "Argentina", "Chile"],
}
REGION_ALIASES = {"eu": "european union", "gcc": "gulf cooperation council",
                  "north-america": "north america", "latam": "latin america"}
# Noms francais courants -> nom anglais utilise par Superteam
COUNTRY_FR = {
    "allemagne": "Germany", "espagne": "Spain", "italie": "Italy", "belgique": "Belgium",
    "suisse": "Switzerland", "royaume-uni": "United Kingdom", "royaume uni": "United Kingdom",
    "pays-bas": "Netherlands", "pays bas": "Netherlands", "irlande": "Ireland",
    "grece": "Greece", "pologne": "Poland", "roumanie": "Romania", "suede": "Sweden",
    "danemark": "Denmark", "finlande": "Finland", "autriche": "Austria", "hongrie": "Hungary",
    "croatie": "Croatia", "chypre": "Cyprus", "estonie": "Estonia", "lettonie": "Latvia",
    "lituanie": "Lithuania", "slovaquie": "Slovakia", "slovenie": "Slovenia", "bulgarie": "Bulgaria",
    "republique tcheque": "Czech Republic", "tchequie": "Czech Republic", "malte": "Malta",
    "maroc": "Morocco", "algerie": "Algeria", "tunisie": "Tunisia", "senegal": "Senegal",
    "cote d'ivoire": "Ivory Coast", "cameroun": "Cameroon", "etats-unis": "United States",
    "etats unis": "United States", "usa": "United States", "canada": "Canada", "mexique": "Mexico",
    "bresil": "Brazil", "argentine": "Argentina", "chili": "Chile", "colombie": "Colombia",
    "perou": "Peru", "inde": "India", "chine": "China", "japon": "Japan", "coree du sud": "South Korea",
    "vietnam": "Vietnam", "indonesie": "Indonesia", "malaisie": "Malaysia", "singapour": "Singapore",
    "philippines": "Philippines", "nigeria": "Nigeria", "kenya": "Kenya", "afrique du sud": "South Africa",
    "emirats arabes unis": "United Arab Emirates", "arabie saoudite": "Saudi Arabia",
    "turquie": "Turkey", "ukraine": "Ukraine", "luxembourg": "Luxembourg", "portugal": "Portugal",
    "france": "France", "uk": "United Kingdom",
}
AUDIENCE_WORDS = ["reach", "view", "impression", "engagement", "follower", "likes", "retweet",
                  "repost", "audience", "virality", "viral", "traction", "referral", "signups",
                  "sign-ups", "portee", "abonnes", "vues"]
STABLES = {"USDC", "USDT", "USDG", "PYUSD", "USD"}


def norm(s):
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s).strip().lower()


def to_country(raw):
    n = norm(raw)
    if n in COUNTRY_FR:
        return COUNTRY_FR[n]
    return " ".join(w.capitalize() for w in n.split()) if n else ""

# ---------------------------------------------------------------- database

def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.executescript("""
    CREATE TABLE IF NOT EXISTS users(
        chat_id INTEGER PRIMARY KEY, username TEXT, country TEXT DEFAULT '',
        skills TEXT DEFAULT '[]', min_usd REAL DEFAULT 0, has_audience INTEGER DEFAULT 0,
        paused INTEGER DEFAULT 0, created_at TEXT);
    CREATE TABLE IF NOT EXISTS listings(
        slug TEXT PRIMARY KEY, data TEXT, first_seen TEXT, open INTEGER DEFAULT 1);
    CREATE TABLE IF NOT EXISTS sent(
        chat_id INTEGER, slug TEXT, sent_at TEXT, PRIMARY KEY(chat_id, slug));
    CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
    """)
    return con


def kv_get(con, k, default=None):
    r = con.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return r["v"] if r else default


def kv_set(con, k, v):
    con.execute("INSERT OR REPLACE INTO kv(k,v) VALUES(?,?)", (k, str(v)))
    con.commit()


def get_user(con, chat_id, username=""):
    r = con.execute("SELECT * FROM users WHERE chat_id=?", (chat_id,)).fetchone()
    if r:
        return dict(r)
    con.execute("INSERT INTO users(chat_id, username, created_at) VALUES(?,?,?)",
                (chat_id, username, now_iso()))
    con.commit()
    return dict(con.execute("SELECT * FROM users WHERE chat_id=?", (chat_id,)).fetchone())


def set_user(con, chat_id, **fields):
    for k, v in fields.items():
        con.execute(f"UPDATE users SET {k}=? WHERE chat_id=?", (v, chat_id))
    con.commit()


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

# ---------------------------------------------------------------- Superteam

def fetch_open_listings():
    out = {}
    for t in LISTING_TYPES:
        r = HTTP.get(f"{BASE_URL}/api/listings",
                     params={"type": t, "take": 100, "isWinnersAnnounced": "false"}, timeout=30)
        r.raise_for_status()
        data = r.json()
        items = data if isinstance(data, list) else data.get("listings") or data.get("data") or []
        for it in items:
            if it.get("slug"):
                it.setdefault("type", t)
                out[it["slug"]] = it
    return out


def fetch_details(slug):
    r = HTTP.get(f"{BASE_URL}/api/listings/details/{slug}", timeout=30)
    r.raise_for_status()
    return r.json()

# ---------------------------------------------------------------- analysis

def strip_html(s):
    s = re.sub(r"<(br|/p|/li|/h\d)[^>]*>", "\n", str(s or ""), flags=re.I)
    return html.unescape(re.sub(r"<[^>]+>", " ", s))


def listing_skills(lst):
    raw = lst.get("skills") or []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = [raw]
    out = set()
    for s in raw:
        name = s.get("skills") if isinstance(s, dict) else s
        if name:
            out.add(str(name))
    return out


def region_ok(region, country):
    """True si un residant de `country` peut participer."""
    r = norm(region)
    if not r or r in ("global", "worldwide", "all"):
        return True
    if not country:
        return False
    c = norm(country)
    r = REGION_ALIASES.get(r, r)
    if r == c:
        return True
    if r in REGION_GROUPS:
        return c in (norm(x) for x in REGION_GROUPS[r])
    # region multiple separee par des virgules
    parts = [REGION_ALIASES.get(p.strip(), p.strip()) for p in re.split(r"[,/;]", r)]
    for p in parts:
        if p == c or (p in REGION_GROUPS and c in (norm(x) for x in REGION_GROUPS[p])):
            return True
    return False


def audience_share(description):
    """Part (en %) des criteres de jugement lies a l'audience. Heuristique."""
    total = 0.0
    for line in re.split(r"[\n;]|\.\s", strip_html(description)):
        line = norm(line)
        if "%" not in line:
            continue
        if any(w in line for w in AUDIENCE_WORDS):
            for p in re.findall(r"(\d{1,3})\s*%", line):
                total += float(p)
    return min(total, 100.0)


def usd_value(lst):
    for k in ("usdValue", "rewardUsd"):
        if lst.get(k):
            try:
                return float(lst[k])
            except (TypeError, ValueError):
                pass
    amt = lst.get("rewardAmount") or lst.get("maxRewardAsk")
    if amt and str(lst.get("token", "")).upper() in STABLES:
        return float(amt)
    return None


def enrich(item, details):
    d = dict(item)
    for k in ("region", "description", "eligibility", "skills", "compensationType",
              "minRewardAsk", "maxRewardAsk", "usdValue"):
        if details.get(k) not in (None, ""):
            d[k] = details[k]
    d.setdefault("region", "Global")
    d["audience_pct"] = audience_share(d.get("description", ""))
    d.pop("description", None)  # on ne stocke pas la description complete
    d.pop("eligibility", None)
    return d


def match(user, lst):
    """Retourne (ok, avertissements)."""
    warnings = []
    if not region_ok(lst.get("region", "Global"), user["country"]):
        return False, []
    if norm(lst.get("region")) not in ("", "global"):
        warnings.append(f"Reservee a : {lst['region']} (ton pays est eligible)")
    uskills = set(json.loads(user["skills"] or "[]"))
    lskills = listing_skills(lst)
    if uskills and lskills and not (uskills & lskills):
        return False, []
    usd = usd_value(lst)
    if user["min_usd"] and usd is not None and usd < user["min_usd"]:
        return False, []
    if usd is None and user["min_usd"]:
        warnings.append("Montant en token non stable : verifie qu'il atteint ton minimum")
    aud = lst.get("audience_pct", 0)
    if aud > AUDIENCE_THRESHOLD:
        if not user["has_audience"]:
            return False, []
        warnings.append(f"Jugee a ~{aud:.0f}% sur l'audience (portee, vues...)")
    elif aud > 0:
        warnings.append(f"~{aud:.0f}% du jugement lie a l'audience")
    return True, warnings


def fmt_deadline(s):
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return "?"
    days = (dt - datetime.now(timezone.utc)).total_seconds() / 86400
    left = "terminee" if days < 0 else (f"dans {days*24:.0f} h" if days < 1 else f"dans {days:.0f} j")
    return f"{dt:%d/%m %H:%M} UTC ({left})"


def fmt_listing(lst, warnings, header="Nouvelle mission"):
    e = html.escape
    amt = lst.get("rewardAmount")
    token = lst.get("token") or ""
    if amt:
        reward = f"{amt:,.0f} {token}".replace(",", " ")
    elif lst.get("compensationType") == "range":
        reward = f"{lst.get('minRewardAsk', '?')}-{lst.get('maxRewardAsk', '?')} {token}"
    else:
        reward = f"variable ({token})" if token else "variable"
    subs = (lst.get("_count") or {}).get("Submission")
    sponsor = (lst.get("sponsor") or {}).get("name", "")
    kind = "Projet (candidature)" if lst.get("type") == "project" else "Bounty"
    lines = [f"<b>{e(header)}</b> - {e(kind)}",
             f"<b>{e(lst.get('title', '?'))}</b>" + (f" - {e(sponsor)}" if sponsor else ""),
             f"Recompense : {e(reward)}",
             f"Fin : {e(fmt_deadline(lst.get('deadline')))}"]
    if subs is not None:
        lines.append(f"Soumissions : {subs}")
    sk = listing_skills(lst)
    if sk:
        lines.append("Competences : " + e(", ".join(sorted(sk))))
    if str(lst.get("agentAccess", "")).upper() in ("AGENT_ALLOWED", "AGENT_ONLY"):
        lines.append("Agents IA acceptes")
    for w in warnings:
        lines.append(f"! {e(w)}")
    lines.append(f'<a href="{BASE_URL}/earn/listing/{e(lst["slug"])}">Voir la mission</a>')
    return "\n".join(lines)

# ---------------------------------------------------------------- Telegram

def tg(method, **params):
    r = HTTP.post(f"{TG}/{method}", json=params, timeout=40)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {method}: {data.get('description')}")
    return data["result"]


def send(chat_id, text, keyboard=None):
    params = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": True}
    if keyboard:
        params["reply_markup"] = {"inline_keyboard": keyboard}
    return tg("sendMessage", **params)


def skills_keyboard(user):
    chosen = set(json.loads(user["skills"] or "[]"))
    rows, row = [], []
    for s in SKILLS:
        row.append({"text": ("[x] " if s in chosen else "[ ] ") + SKILL_FR[s], "callback_data": f"sk:{s}"})
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([{"text": "Toutes (aucun filtre)", "callback_data": "sk:*"},
                 {"text": "Valider", "callback_data": "sk:ok"}])
    return rows


HELP = (
    "<b>Bounty Radar</b> - alertes Superteam Earn selon ton profil.\n\n"
    "/pays France - ton pays de residence (filtre les missions reservees a d'autres regions)\n"
    "/competences - choisir tes competences\n"
    "/min 100 - recompense minimale en USD (0 = tout)\n"
    "/audience oui|non - as-tu une audience (X, YouTube...) ?\n"
    "/profil - voir ton profil\n"
    "/missions - missions ouvertes qui te correspondent\n"
    "/stop - mettre les alertes en pause (/start pour reprendre)\n"
    "/supprimer - effacer tes donnees\n\n"
    "Bot independant, non affilie a Superteam. Il ne soumet rien a ta place."
)


def profile_text(u):
    sk = json.loads(u["skills"] or "[]")
    return ("<b>Ton profil</b>\n"
            f"Pays : {html.escape(u['country'] or 'non renseigne (seules les missions Global)')}\n"
            f"Competences : {html.escape(', '.join(SKILL_FR[s] for s in sk) if sk else 'toutes')}\n"
            f"Minimum : {u['min_usd']:.0f} USD\n"
            f"Audience : {'oui' if u['has_audience'] else 'non'}\n"
            f"Alertes : {'en pause' if u['paused'] else 'actives'}")


def open_listings(con):
    out = []
    for r in con.execute("SELECT data FROM listings WHERE open=1"):
        lst = json.loads(r["data"])
        try:
            dl = datetime.fromisoformat(str(lst.get("deadline")).replace("Z", "+00:00"))
            if dl < datetime.now(timezone.utc):
                continue
        except ValueError:
            pass
        out.append(lst)
    out.sort(key=lambda x: str(x.get("deadline")))
    return out


def handle_message(con, msg):
    chat_id = msg["chat"]["id"]
    if msg["chat"].get("type") != "private":
        return
    text = (msg.get("text") or "").strip()
    if not text.startswith("/"):
        send(chat_id, "Tape /aide pour voir les commandes.")
        return
    cmd, _, arg = text.partition(" ")
    cmd = cmd.split("@")[0].lower()
    arg = arg.strip()
    u = get_user(con, chat_id, msg.get("from", {}).get("username", ""))

    if cmd == "/start":
        set_user(con, chat_id, paused=0)
        send(chat_id, "Bienvenue ! Je t'alerte des nouvelles missions Superteam Earn qui te "
                      "correspondent.\n\nCommence par /pays (ex : <code>/pays France</code>), "
                      "puis /competences.\n\n" + HELP)
    elif cmd in ("/aide", "/help"):
        send(chat_id, HELP)
    elif cmd == "/pays":
        if not arg:
            send(chat_id, "Exemple : <code>/pays France</code>")
            return
        c = to_country(arg)
        set_user(con, chat_id, country=c)
        send(chat_id, f"Pays enregistre : <b>{html.escape(c)}</b>. Les missions reservees a d'autres "
                      "regions seront ignorees.")
    elif cmd == "/competences":
        send(chat_id, "Coche tes competences (aucune = toutes les missions) :", skills_keyboard(u))
    elif cmd == "/min":
        try:
            v = max(0.0, float(arg.replace(",", ".").replace("$", "")))
        except ValueError:
            send(chat_id, "Exemple : <code>/min 100</code>")
            return
        set_user(con, chat_id, min_usd=v)
        send(chat_id, f"Minimum : {v:.0f} USD.")
    elif cmd == "/audience":
        a = norm(arg)
        if a not in ("oui", "non", "yes", "no"):
            send(chat_id, "Reponds <code>/audience oui</code> ou <code>/audience non</code>.")
            return
        has = 1 if a in ("oui", "yes") else 0
        set_user(con, chat_id, has_audience=has)
        send(chat_id, "Note. " + ("Les missions jugees sur l'audience seront signalees."
                                  if has else "Les missions jugees surtout sur l'audience seront masquees."))
    elif cmd == "/profil":
        send(chat_id, profile_text(u))
    elif cmd == "/missions":
        hits = []
        for lst in open_listings(con):
            ok, w = match(u, lst)
            if ok:
                hits.append((lst, w))
        if not hits:
            send(chat_id, "Aucune mission ouverte ne correspond a ton profil pour l'instant.")
            return
        send(chat_id, f"{len(hits)} mission(s) ouverte(s) pour toi" +
             (" (les 10 plus proches de la fin) :" if len(hits) > 10 else " :"))
        for lst, w in hits[:10]:
            send(chat_id, fmt_listing(lst, w, header="Mission ouverte"))
            time.sleep(0.05)
    elif cmd == "/stop":
        set_user(con, chat_id, paused=1)
        send(chat_id, "Alertes en pause. /start pour reprendre.")
    elif cmd == "/supprimer":
        con.execute("DELETE FROM sent WHERE chat_id=?", (chat_id,))
        con.execute("DELETE FROM users WHERE chat_id=?", (chat_id,))
        con.commit()
        send(chat_id, "Tes donnees sont effacees. /start pour recommencer.")
    elif cmd == "/stats" and ADMIN_CHAT_ID and str(chat_id) == ADMIN_CHAT_ID:
        n = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        a = con.execute("SELECT COUNT(*) FROM users WHERE paused=0").fetchone()[0]
        s = con.execute("SELECT COUNT(*) FROM sent").fetchone()[0]
        l = con.execute("SELECT COUNT(*) FROM listings WHERE open=1").fetchone()[0]
        send(chat_id, f"Utilisateurs : {n} ({a} actifs)\nAlertes envoyees : {s}\nMissions ouvertes : {l}\n"
                      f"Dernier scan : {kv_get(con, 'last_scan', '-')}")
    else:
        send(chat_id, "Commande inconnue. /aide")


def handle_callback(con, cq):
    chat_id = cq["message"]["chat"]["id"]
    data = cq.get("data", "")
    u = get_user(con, chat_id)
    chosen = set(json.loads(u["skills"] or "[]"))
    if data == "sk:ok":
        tg("answerCallbackQuery", callback_query_id=cq["id"], text="Enregistre")
        tg("editMessageText", chat_id=chat_id, message_id=cq["message"]["message_id"],
           text="Competences : " + (", ".join(SKILL_FR[s] for s in SKILLS if s in chosen) or "toutes"))
        return
    if data == "sk:*":
        chosen = set()
    elif data.startswith("sk:") and data[3:] in SKILLS:
        chosen ^= {data[3:]}
    set_user(con, chat_id, skills=json.dumps(sorted(chosen)))
    u["skills"] = json.dumps(sorted(chosen))
    tg("answerCallbackQuery", callback_query_id=cq["id"])
    tg("editMessageReplyMarkup", chat_id=chat_id, message_id=cq["message"]["message_id"],
       reply_markup={"inline_keyboard": skills_keyboard(u)})

# ---------------------------------------------------------------- scan

def scan(con, notify=True):
    current = fetch_open_listings()
    known = {r["slug"] for r in con.execute("SELECT slug FROM listings")}
    first_run = kv_get(con, "initialized") is None
    new = []
    for slug, item in current.items():
        if slug in known:
            # rafraichit les champs qui bougent (soumissions, deadline) sans re-telecharger
            row = con.execute("SELECT data FROM listings WHERE slug=?", (slug,)).fetchone()
            d = json.loads(row["data"])
            for k in ("_count", "deadline", "rewardAmount", "status"):
                if k in item:
                    d[k] = item[k]
            con.execute("UPDATE listings SET data=?, open=1 WHERE slug=?", (json.dumps(d), slug))
            continue
        try:
            det = fetch_details(slug)
        except Exception as ex:  # on reessaiera au prochain scan
            print(f"[scan] details {slug}: {ex}")
            continue
        d = enrich(item, det)
        con.execute("INSERT INTO listings(slug,data,first_seen,open) VALUES(?,?,?,1)",
                    (slug, json.dumps(d), now_iso()))
        new.append(d)
        time.sleep(0.3)
    closed = known - set(current)
    for slug in closed:
        con.execute("UPDATE listings SET open=0 WHERE slug=?", (slug,))
    con.commit()
    kv_set(con, "last_scan", now_iso())
    if first_run:
        kv_set(con, "initialized", "1")
        print(f"[scan] premier passage : {len(new)} missions enregistrees sans alerte")
        return 0
    sent = 0
    if notify and new:
        users = [dict(r) for r in con.execute("SELECT * FROM users WHERE paused=0")]
        for lst in new:
            for u in users:
                ok, w = match(u, lst)
                if not ok:
                    continue
                if con.execute("SELECT 1 FROM sent WHERE chat_id=? AND slug=?",
                               (u["chat_id"], lst["slug"])).fetchone():
                    continue
                try:
                    send(u["chat_id"], fmt_listing(lst, w))
                    sent += 1
                except RuntimeError as ex:
                    if "blocked" in str(ex) or "deactivated" in str(ex) or "not found" in str(ex):
                        set_user(con, u["chat_id"], paused=1)
                    print(f"[send] {u['chat_id']}: {ex}")
                con.execute("INSERT OR IGNORE INTO sent VALUES(?,?,?)", (u["chat_id"], lst["slug"], now_iso()))
                time.sleep(0.05)
        con.commit()
    print(f"[scan] {now_iso()} ouvertes={len(current)} nouvelles={len(new)} alertes={sent}")
    return sent


def run():
    if not BOT_TOKEN:
        sys.exit("TELEGRAM_BOT_TOKEN manquant dans .env")
    con = db()
    me = tg("getMe")
    print(f"Bounty Radar demarre : @{me['username']} (scan toutes les {SCAN_INTERVAL_MIN:g} min)")
    tg("setMyCommands", commands=[
        {"command": c, "description": d} for c, d in [
            ("start", "Activer les alertes"), ("pays", "Ton pays"), ("competences", "Tes competences"),
            ("min", "Recompense minimale (USD)"), ("audience", "As-tu une audience ? oui/non"),
            ("profil", "Voir ton profil"), ("missions", "Missions ouvertes pour toi"),
            ("stop", "Mettre en pause"), ("aide", "Aide")]])
    offset = int(kv_get(con, "offset", "0"))
    next_scan = 0
    while True:
        if time.time() >= next_scan:
            try:
                scan(con)
            except Exception as ex:
                print(f"[scan] erreur : {ex}")
            next_scan = time.time() + SCAN_INTERVAL_MIN * 60
        try:
            updates = tg("getUpdates", offset=offset, timeout=25,
                         allowed_updates=["message", "callback_query"])
        except Exception as ex:
            print(f"[poll] {ex}")
            time.sleep(5)
            continue
        for up in updates:
            offset = up["update_id"] + 1
            try:
                if "message" in up:
                    handle_message(con, up["message"])
                elif "callback_query" in up:
                    handle_callback(con, up["callback_query"])
            except Exception as ex:
                print(f"[update] {ex}")
        kv_set(con, "offset", offset)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "scan-once":
        c = db()
        scan(c, notify=False)
        for lst in open_listings(c):
            print(f"- {lst['title']} | region={lst.get('region')} | audience={lst.get('audience_pct', 0):.0f}%")
    else:
        run()
