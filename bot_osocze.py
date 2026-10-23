"""
Robot pilnujący wolnych terminów na oddanie osocza w RCKiK Poznań.

Co 1–2 minuty odpytuje publiczne API systemu rezerwacji (Qmatic) o wolne
terminy w dniu TARGET_DATE. Priorytet: RCKiK Poznań, ul. Marcelińska 44,
dodatkowo wszystkie oddziały terenowe. Gdy pojawi się NOWY wolny termin,
wysyła e-mail. Rezerwację kończy się ręcznie na stronie.

Użycie:
    python bot_osocze.py            # praca ciągła
    python bot_osocze.py --once     # jedno sprawdzenie (bez wysyłki maila)
    python bot_osocze.py --test     # wysyła testowy e-mail
    python bot_osocze.py --date 2026-10-30   # inna data (do testów)
"""

import argparse
import configparser
import json
import logging
import os
import random
import smtplib
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

TARGET_DATE = "2026-10-29"

API = "https://ebilet.rckik.poznan.pl/qmaticwebbooking/rest/schedule"
BOOKING_URL = "https://ebilet.rckik.poznan.pl/qmaticwebbooking/#/"

# Marcelińska obsługuje usługę "Osocze", oddziały terenowe "OT: Osocze".
# Sprawdzamy obie w każdej placówce, na wypadek gdyby RCKiK coś zmieniło.
SERVICES = {
    "daabb71c3c272a6e585a637de585b3eee798fa5b5564a4ec24448515b1756162": "Osocze",
    "8519ecd246506744c2626bd379344b5f92bdd3b334a9578d252a40e75842c7bf": "OT: Osocze",
}

PRIORITY_BRANCH = "94050c1d24aa357dae214dc6846941ff7f1510cb85700bb480c666438ae92adb"  # Marcelińska 44

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.ini"
STATE_FILE = BASE_DIR / "wyslane_terminy.json"
LOG_FILE = BASE_DIR / "bot_osocze.log"

log = logging.getLogger("osocze")


def get_json(url):
    """Zwraca JSON albo None, gdy API odpowie błędem 400 (= brak terminów)."""
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (osocze-monitor)",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code == 400:
            return None
        raise


def load_branches():
    branches = get_json(f"{API}/branches") or []
    branches.sort(key=lambda b: (b["id"] != PRIORITY_BRANCH, b["name"]))
    return branches


def find_slots(branches, date):
    """Zwraca listę wolnych terminów w danym dniu, Marcelińska na początku."""
    slots = []
    for b in branches:
        for service_id in SERVICES:
            params = f";servicePublicId={service_id};customSlotLength=60"
            dates = get_json(f"{API}/branches/{b['id']}/dates{params}") or []
            if not any(d.get("date") == date for d in dates):
                continue
            times = get_json(f"{API}/branches/{b['id']}/dates/{date}/times{params}") or []
            for t in times:
                slots.append({
                    "branch_id": b["id"],
                    "branch": b["name"],
                    "address": f"{b.get('addressLine1', '')}, {b.get('addressCity', '')}",
                    "phone": b.get("phone", ""),
                    "time": t["time"],
                    "priority": b["id"] == PRIORITY_BRANCH,
                })
            time.sleep(0.5)  # nie zasypujemy serwera zapytaniami
    return slots


def slot_key(s):
    return f"{s['branch_id']}|{s['time']}"


def load_state():
    try:
        return set(json.loads(STATE_FILE.read_text(encoding="utf-8")))
    except (FileNotFoundError, ValueError):
        return set()


def save_state(keys):
    STATE_FILE.write_text(json.dumps(sorted(keys)), encoding="utf-8")


def build_email(slots, new_keys, date):
    date_pl = datetime.strptime(date, "%Y-%m-%d").strftime("%d.%m.%Y")
    priority = [s for s in slots if s["priority"]]
    others = [s for s in slots if not s["priority"]]

    if priority:
        hours = ", ".join(s["time"] for s in priority)
        subject = f"WOLNY TERMIN osocze {date_pl} – Marcelińska {hours}"
    else:
        names = sorted({s["branch"].replace("Oddział Terenowy ", "OT ") for s in others})
        subject = f"Wolny termin osocze {date_pl} – {', '.join(names)}"

    def line(s):
        mark = "  [NOWY]" if slot_key(s) in new_keys else ""
        return f"  • {s['time']}{mark}"

    parts = [
        f"Pojawiły się wolne terminy na oddanie osocza w dniu {date_pl}.",
        "",
        "Zarezerwuj jak najszybciej, bo terminy szybko znikają:",
        BOOKING_URL,
        "(wybierz: Osocze -> placówka -> data -> godzina)",
        "",
        "=== PRIORYTET: RCKiK Poznań, ul. Marcelińska 44 ===",
    ]
    if priority:
        parts += [line(s) for s in priority]
    else:
        parts.append("  brak wolnych terminów w tym momencie")

    if others:
        parts += ["", "=== Oddziały terenowe ==="]
        current = None
        for s in others:
            if s["branch"] != current:
                current = s["branch"]
                parts.append(f"{s['branch']} ({s['address']}, tel. {s['phone']})")
            parts.append(line(s))

    parts += [
        "",
        f"Sprawdzono: {datetime.now():%d.%m.%Y %H:%M:%S}",
        "Wiadomość wysłana automatycznie przez robota monitorującego terminy.",
    ]
    return subject, "\n".join(parts)


def send_email(cfg, subject, body):
    smtp = cfg["smtp"]
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = smtp.get("from", smtp["user"])
    recipients = [a.strip() for a in smtp["to"].split(",") if a.strip()]
    msg["To"] = ", ".join(recipients)
    msg.set_content(body)

    host, port = smtp["host"], smtp.getint("port", 465)
    context = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=context, timeout=30) as s:
            s.login(smtp["user"], smtp["password"])
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls(context=context)
            s.login(smtp["user"], smtp["password"])
            s.send_message(msg)
    log.info("Wysłano e-mail: %s", subject)


def load_config():
    """config.ini lokalnie, a na GitHub Actions zmienne środowiskowe (Secrets)."""
    cfg = configparser.ConfigParser()
    if cfg.read(CONFIG_FILE, encoding="utf-8"):
        return cfg
    env = {k: os.environ.get(k, "") for k in
           ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM", "MAIL_TO")}
    if not all(env[k] for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "MAIL_TO")):
        sys.exit(f"Brak pliku {CONFIG_FILE.name} i zmiennych SMTP_* – nie ma skąd wziąć ustawień poczty.")
    cfg["smtp"] = {
        "host": env["SMTP_HOST"],
        "port": env["SMTP_PORT"] or "465",
        "user": env["SMTP_USER"],
        "password": env["SMTP_PASSWORD"],
        "from": env["SMTP_FROM"] or env["SMTP_USER"],
        "to": env["MAIL_TO"],
    }
    cfg["bot"] = {"interval_seconds": os.environ.get("INTERVAL_SECONDS", "90")}
    return cfg


def check(cfg, branches, date, sent, notify=True):
    slots = find_slots(branches, date)
    current = {slot_key(s) for s in slots}
    new_keys = current - sent

    if slots:
        summary = ", ".join(f"{s['branch']} {s['time']}" for s in slots)
        log.info("Wolne terminy %s: %s", date, summary)
    else:
        log.info("Brak wolnych terminów na %s", date)

    if new_keys and notify:
        subject, body = build_email(slots, new_keys, date)
        send_email(cfg, subject, body)
        sent |= new_keys
    # Termin, który zniknął i wróci (ktoś znów odwoła), ma wywołać nowego maila.
    sent &= current
    save_state(sent)
    return slots


def main():
    parser = argparse.ArgumentParser(description="Monitor terminów na osocze – RCKiK Poznań")
    parser.add_argument("--date", default=TARGET_DATE, help="data RRRR-MM-DD (domyślnie %(default)s)")
    parser.add_argument("--once", action="store_true", help="jedno sprawdzenie, bez maila")
    parser.add_argument("--test", action="store_true", help="wyślij testowy e-mail i zakończ")
    parser.add_argument("--max-minutes", type=int, default=0,
                        help="zakończ po tylu minutach (GitHub Actions: limit 6 h na zadanie)")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(message)s",
        datefmt="%d.%m %H:%M:%S",
        handlers=[logging.StreamHandler(), logging.FileHandler(LOG_FILE, encoding="utf-8")],
    )

    if args.once:
        branches = load_branches()
        for s in find_slots(branches, args.date):
            print(f"{s['branch']:35} {s['time']}")
        return

    cfg = load_config()
    interval = cfg.getint("bot", "interval_seconds", fallback=90)

    if args.test:
        send_email(cfg, "TEST – robot osocze RCKiK działa",
                   f"To jest wiadomość testowa.\nRobot pilnuje terminów na {args.date}.\n{BOOKING_URL}")
        return

    branches = load_branches()
    log.info("Start. Data: %s, placówek: %d, co ok. %d s.", args.date, len(branches), interval)
    sent = load_state()
    end = datetime.strptime(args.date, "%Y-%m-%d").replace(hour=18)
    stop_at = time.monotonic() + args.max_minutes * 60 if args.max_minutes else float("inf")

    while datetime.now() < end and time.monotonic() < stop_at:
        try:
            check(cfg, branches, args.date, sent)
        except Exception as e:  # sieć, serwer, SMTP – próbujemy dalej
            log.warning("Błąd: %s", e)
        time.sleep(interval + random.randint(-15, 15))

    log.info("Minął dzień %s – koniec pracy.", args.date)


if __name__ == "__main__":
    main()
