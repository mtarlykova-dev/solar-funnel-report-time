#!/usr/bin/env python3
"""
Данные для отчёта «Сроки по этапам» (Inbound / Outbound) → docs/data.json

Для каждой сделки:
  • берётся ТЕКУЩАЯ воронка сделки, и читаются только поля «Дата перехода: …» этой воронки
    (одноимённые поля Inbound/Outbound различаются по порядку в карточке: верхнее — Inbound);
  • этапы проходятся в порядке воронки; время на этапе = дата следующего заполненного этапа − дата этапа;
    вход на стартовый этап без поля («Новая заявка», «Холодная база») = дата создания сделки;
  • в отчёт попадают сделки, созданные с 1 июня, и более старые — если у них были переходы с 1 июня
    (у старых время на стартовом этапе не считается: дата входа на него неизвестна);
  • если после этапа сделка ушла в «Отказ» — это время записывается отдельно как «до отказа»;
  • для открытых сделок — сколько дней сделка уже стоит на текущем этапе.

Вкладка «Последнее касание»: все открытые сделки Inbound/Outbound (любой даты создания) —
значение поля «Последнее касание», менеджер, этап, название сделки, основной контакт и компания.

Данные публикуются только в зашифрованном виде (секрет REPORT_PASSWORD).

Запуск: AMO_TOKEN=... python build_report.py
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

SUBDOMAIN = "solarstaff"
TOKEN = os.environ.get("AMO_TOKEN", "")
PIPELINES = ["Inbound", "Outbound"]      # порядок важен: Inbound-поля в карточке выше Outbound
FIELD_PREFIX = "Дата перехода:"
TOUCH_FIELD = "Последнее касание"        # поле сделки (дата и время) — последнее исходящее касание менеджера
INCOMING_DAYS = 180                      # за сколько дней искать входящие касания (звонки, сообщения, письма)
# Входящие события: все типы amo с ключом incoming_*, кроме заявок/неразобранного.
# Если список типов недоступен — берётся этот запасной набор.
INCOMING_FALLBACK = ["incoming_call", "incoming_chat_message", "incoming_sms"]
INCOMING_EXCLUDE = ("lead", "unsorted")
MSK = timezone(timedelta(hours=3))
CREATED_FROM = datetime(2026, 6, 1, tzinfo=MSK)   # с этой даты есть история переходов
FROM_TS = int(CREATED_FROM.timestamp())
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "data.json")
ENC_OUT = os.path.join(os.path.dirname(OUT), "data.enc.json")
ENC_ITER = 310000
LOST, WON = 143, 142

# Нормы срока на этапе (в днях) — для этапов, где долгое нахождение ожидаемо.
# Такие этапы не считаются «узким местом», а сделки на них — «зависшими», пока не превысят норму.
# Ключ — название этапа (одинаково для обеих воронок) или "Воронка/Этап" для одной воронки.
STAGE_NORMS = {
    "Сопровождение продаж": 90,   # клиента ведут ~3 месяца, затем передают в аккаунтинг
}

BASE = f"https://{SUBDOMAIN}.amocrm.ru/api/v4"
S = requests.Session()
S.headers.update({"Authorization": f"Bearer {TOKEN}"})


def api(url, params=None):
    if not url.startswith("http"):
        url = BASE + url
    for attempt in range(8):
        try:
            r = S.get(url, params=params, timeout=90)
        except requests.exceptions.RequestException as e:
            # amo иногда обрывает соединение — ждём и повторяем
            wait = min(60, 3 * 2 ** attempt)
            print(f"  …сбой соединения ({e.__class__.__name__}), повтор через {wait} с")
            time.sleep(wait)
            continue
        time.sleep(0.2)
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code == 204:
            return None
        if not r.ok:
            raise RuntimeError(f"GET {url} -> {r.status_code}: {r.text[:500]}")
        return r.json()
    raise RuntimeError(f"GET {url}: не удалось после повторов")


def paginate(path, params, key):
    url, p = path, params
    while url:
        data = api(url, p)
        if not data:
            return
        yield from data.get("_embedded", {}).get(key, [])
        url = data.get("_links", {}).get("next", {}).get("href")
        p = None


# ── Производственный календарь РФ ─────────────────────────────────────────────
# calendar_ru.json в репозитории + свежие данные с xmlcalendar.ru при каждой сборке.
# Для лет вне календаря — обычное правило: суббота и воскресенье выходные.
CAL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "calendar_ru.json")
CAL = {}            # год → строка '0'/'1' по дням года (1 — нерабочий)
OFF_DAYS = set()    # номера нерабочих дней (дни от 01.01.1970 по МСК) в годах календаря
CAL_YEARS = set()


def load_calendar():
    global CAL
    try:
        with open(CAL_FILE, encoding="utf-8") as fh:
            CAL = dict(json.load(fh)["years"])
    except (OSError, ValueError, KeyError) as e:
        print(f"! {CAL_FILE}: {e}")
    this_year = datetime.now(MSK).year
    for y in range(2025, this_year + 2):
        try:
            r = requests.get(f"https://xmlcalendar.ru/data/ru/{y}/calendar.json", timeout=5)
            if r.status_code == 404:
                continue
            r.raise_for_status()
            off = set()
            for m in r.json()["months"]:
                for t in m["days"].split(","):
                    if t and not t.endswith("*"):           # «*» — сокращённый рабочий день
                        off.add((m["month"], int(t.rstrip("+"))))
            start = datetime(y, 1, 1)
            n = (datetime(y + 1, 1, 1) - start).days
            days = [(start + timedelta(i)) for i in range(n)]
            CAL[str(y)] = "".join("1" if (d.month, d.day) in off else "0" for d in days)
        except Exception as e:   # сеть/формат — остаёмся на сохранённом календаре
            print(f"  календарь {y}: xmlcalendar.ru недоступен ({e.__class__.__name__}), беру сохранённый")
    OFF_DAYS.clear(); CAL_YEARS.clear()
    for y, sdays in CAL.items():
        base = (datetime(int(y), 1, 1) - datetime(1970, 1, 1)).days
        CAL_YEARS.add(int(y))
        OFF_DAYS.update(base + i for i, c in enumerate(sdays) if c == "1")
    print(f"Производственный календарь: {', '.join(sorted(CAL)) or 'нет'} "
          f"(нерабочих дней: {', '.join(f'{y}: {v.count(chr(49))}' for y, v in sorted(CAL.items()))})")


def _workday(d):
    y = (datetime(1970, 1, 1) + timedelta(days=d)).year
    if y in CAL_YEARS:
        return d not in OFF_DAYS
    return (d + 3) % 7 < 5                   # 01.01.1970 — четверг; пн..пт


def biz_days(t0, t1):
    """Длительность в рабочих днях: часы выходных и праздников (по МСК, производственный календарь РФ) не считаются."""
    if t0 is None or t1 is None or t1 <= t0:
        return 0.0
    a, b = t0 + 10800, t1 + 10800            # в московское время
    da, db = a // 86400, b // 86400
    if da == db:
        return (b - a) / 86400 if _workday(da) else 0.0
    sec = ((da + 1) * 86400 - a if _workday(da) else 0) + (b - db * 86400 if _workday(db) else 0)
    return sum(1 for d in range(da + 1, db) if _workday(d)) + sec / 86400


def norm(s):
    return " ".join(str(s).lower().replace("ё", "е").split())


def load_structure():
    pipelines = api("/leads/pipelines")["_embedded"]["pipelines"]
    fields = list(paginate("/leads/custom_fields", {"limit": 250}, "custom_fields"))

    pipes = {}
    for kw in PIPELINES:
        found = [p for p in pipelines if norm(p["name"]) == norm(kw)] or \
                [p for p in pipelines if norm(kw) in norm(p["name"])]
        if not found:
            sys.exit(f"Не найдена воронка «{kw}»")
        pipes[kw] = found[0]

    by_name = {}
    for f in fields:
        if norm(f["name"]).startswith(norm(FIELD_PREFIX)) and ":" in f["name"]:
            by_name.setdefault(norm(f["name"].split(":", 1)[1]), []).append(f)
    for lst in by_name.values():
        lst.sort(key=lambda x: (x.get("sort", 0), x["id"]))

    owners = {}
    for kw, p in pipes.items():
        for st in p["_embedded"]["statuses"]:
            owners.setdefault(norm(st["name"]), []).append(kw)

    touch = [f for f in fields if norm(f["name"]) == norm(TOUCH_FIELD)]
    touch_fid = touch[0]["id"] if touch else None
    print(f"Поле «{TOUCH_FIELD}»: " + (f"id {touch_fid}" if touch_fid else "НЕ НАЙДЕНО — вкладка будет пустой"))

    meta = {}
    for kw, p in pipes.items():
        statuses = sorted(p["_embedded"]["statuses"], key=lambda s: s.get("sort", 0))
        stages = []   # рабочие этапы по порядку (без «Неразобранное» и «Отказ»)
        for st in statuses:
            if st["id"] == LOST:
                continue
            cands, own = by_name.get(norm(st["name"]), []), owners.get(norm(st["name"]), [])
            fid = cands[own.index(kw)]["id"] if len(cands) == len(own) and kw in own else None
            stages.append({"id": st["id"], "name": st["name"], "field": fid})
        # «Неразобранное» (до первого этапа с полем и не стартовый) отбрасываем:
        first_field = next(i for i, s in enumerate(stages) if s["field"])
        start = stages[first_field - 1] if first_field > 0 else None
        stages = ([dict(start, field=None, start=True)] if start else []) + stages[first_field:]
        lost_field = None
        cands, own = by_name.get(norm("Отказ"), []), owners.get(norm("Отказ"), [])
        if len(cands) == len(own) and kw in own:
            lost_field = cands[own.index(kw)]["id"]
        open_st = [{"id": st["id"], "name": st["name"]} for st in statuses if st["id"] not in (WON, LOST)]
        meta[kw] = {"pipeline_id": p["id"], "stages": stages, "lost_field": lost_field, "open": open_st}
        print(f"{kw}: этапы {[s['name'] for s in stages]}")
    return meta, touch_fid


def load_users():
    try:
        return {u["id"]: u["name"] for u in paginate("/users", {"limit": 250}, "users")}
    except RuntimeError as e:
        print(f"! Список пользователей недоступен ({e}); будут ID")
        return {}


def names_by_id(path, key, ids):
    """Названия контактов/компаний по ID (пачками по 250)."""
    out, ids = {}, sorted(ids)
    for i in range(0, len(ids), 250):
        params = [("limit", 250)] + [("filter[id][]", x) for x in ids[i:i + 250]]
        try:
            data = api(path, params)
        except RuntimeError as e:
            print(f"! {path}: {e}")
            continue
        for x in (data or {}).get("_embedded", {}).get(key, []):
            out[x["id"]] = (x.get("name") or "").strip()
    return out


def touch_record(lead, kw, m, users, touch_fid):
    lt = None
    for v in lead.get("custom_fields_values") or []:
        if v["field_id"] == touch_fid and v.get("values"):
            try:
                lt = int(v["values"][0]["value"])
            except (TypeError, ValueError):
                pass
    emb = lead.get("_embedded") or {}
    contacts = emb.get("contacts") or []
    main = next((c["id"] for c in contacts if c.get("is_main")), contacts[0]["id"] if contacts else None)
    companies = emb.get("companies") or []
    st_idx = next((i for i, s in enumerate(m["open"]) if s["id"] == lead["status_id"]), None)
    return {
        "id": lead["id"], "p": kw,
        "m": users.get(lead.get("responsible_user_id"), str(lead.get("responsible_user_id"))),
        "n": (lead.get("name") or "").strip(),
        "ct": main, "co": companies[0]["id"] if companies else None,
        "_cts": [c["id"] for c in contacts], "_cos": [c["id"] for c in companies],
        "st": st_idx,                  # индекс в списке открытых этапов воронки
        "lt": lt,                      # «Последнее касание» (исходящее), unix-время; None — не было
        "li": None,                    # последнее входящее касание (звонок/сообщение/письмо клиента)
        "cr": lead["created_at"],
    }


def incoming_types():
    try:
        data = api("/events/types", {"language_code": "ru"}) or {}
        items = data.get("_embedded", {}).get("events_types", [])
        keys = [str(t.get("key") or t.get("type")) for t in items]
        chosen = [k for k in keys if k.startswith("incoming_") and not any(x in k for x in INCOMING_EXCLUDE)]
        if chosen:
            return chosen
    except RuntimeError as e:
        print(f"! Типы событий недоступны ({e})")
    return INCOMING_FALLBACK


def apply_incoming(touches, now):
    """Последнее входящее касание по каждой открытой сделке: события на самой сделке,
    её контактах или компании."""
    by_lead, by_contact, by_company = {}, {}, {}
    for t in touches:
        by_lead[t["id"]] = [t]
        for c in t["_cts"]:
            by_contact.setdefault(c, []).append(t)
        for c in t["_cos"]:
            by_company.setdefault(c, []).append(t)
    maps = {"lead": by_lead, "leads": by_lead, "contact": by_contact, "contacts": by_contact,
            "company": by_company, "companies": by_company}

    types = incoming_types()
    print(f"Входящие типы событий: {', '.join(types)}")
    since = now - INCOMING_DAYS * 86400
    counts = {}
    for i in range(0, len(types), 10):
        params = [("limit", 100), ("filter[created_at][from]", since)] + [("filter[type][]", x) for x in types[i:i + 10]]
        for ev in paginate("/events", params, "events"):
            counts[ev.get("type")] = counts.get(ev.get("type"), 0) + 1
            for t in maps.get(ev.get("entity_type"), {}).get(ev.get("entity_id"), []):
                if t["li"] is None or ev["created_at"] > t["li"]:
                    t["li"] = ev["created_at"]
    print(f"Входящих событий за {INCOMING_DAYS} дн: {counts or 0}")
    print(f"Сделок с входящими касаниями: {sum(1 for t in touches if t['li'])}")


def lead_record(lead, kw, m, users, now):
    cf = {}
    for v in lead.get("custom_fields_values") or []:
        vals = v.get("values") or []
        if vals and vals[0].get("value"):
            try:
                cf[v["field_id"]] = int(vals[0]["value"])
            except (TypeError, ValueError):
                pass

    stages = m["stages"]
    # дата входа на каждый этап — только из полей ЭТОЙ воронки
    entries = []
    for i, s in enumerate(stages):
        if s.get("start"):
            # дата входа на стартовый этап известна только для сделок, созданных после начала истории
            ts = lead["created_at"] if lead["created_at"] >= FROM_TS else None
        else:
            ts = cf.get(s["field"]) if s["field"] else None
        entries.append(ts)

    status = lead["status_id"]
    state = "won" if status == WON else "lost" if status == LOST else "open"
    seg, prev, anomalies = [], None, 0
    for i, ts in enumerate(entries):
        if ts is None:
            continue
        if prev is not None:
            if ts >= entries[prev]:
                seg.append([prev, round((ts - entries[prev]) / 86400, 3), i,
                            round(biz_days(entries[prev], ts), 3)])
                prev = i
            else:
                anomalies += 1
            continue
        prev = i

    lost_from = lost_days = lost_bdays = None
    if state == "lost" and prev is not None:
        lts = cf.get(m["lost_field"]) if m["lost_field"] else None
        if lts is None:
            lts = lead.get("closed_at")
        if lts and lts >= entries[prev]:
            lost_from, lost_days = prev, round((lts - entries[prev]) / 86400, 3)
            lost_bdays = round(biz_days(entries[prev], lts), 3)

    cur = age = age_b = None
    if state == "open":
        idx = next((i for i, s in enumerate(stages) if s["id"] == status), None)
        if idx is not None:
            cur = idx
            if entries[idx]:
                age = round((now - entries[idx]) / 86400, 3)
                age_b = round(biz_days(entries[idx], now), 3)

    cycle = cycle_b = None
    won_idx = next((i for i, s in enumerate(stages) if s["id"] == WON), None)
    if state == "won" and won_idx is not None and entries[won_idx]:
        cycle = round((entries[won_idx] - lead["created_at"]) / 86400, 3)
        cycle_b = round(biz_days(lead["created_at"], entries[won_idx]), 3)

    own = [t for t in entries if t] + ([cf[m["lost_field"]]] if m["lost_field"] and m["lost_field"] in cf else [])
    active = min([t for t in own if t >= FROM_TS], default=None)
    if lead["created_at"] < FROM_TS and active is None:
        return None   # старая сделка без движения с начала истории — в отчёт не берём
    d_ts = lead["created_at"] if lead["created_at"] >= FROM_TS else active

    return {
        "id": lead["id"],                 # только ID — без названия сделки и компании
        "p": kw,
        "m": users.get(lead.get("responsible_user_id"), str(lead.get("responsible_user_id"))),
        "c": datetime.fromtimestamp(lead["created_at"], MSK).strftime("%Y-%m-%d"),
        "d": datetime.fromtimestamp(d_ts, MSK).strftime("%Y-%m-%d"),  # дата в периоде: создание или первый переход с начала истории
        "s": state,
        "seg": seg,                       # [индекс этапа, дней на этапе, индекс следующего этапа, рабочих дней]
        "lf": lost_from, "ld": lost_days, "lb": lost_bdays,  # отказ: с какого этапа, дней / рабочих дней до отказа
        "cur": cur, "age": age, "ab": age_b,  # открытые: текущий этап, дней / рабочих дней на нём
        "cy": cycle, "cb": cycle_b,       # успешные: от создания до «Передача в аккаунтинг», дней / рабочих дней
        "an": anomalies,
    }


def main():
    if not TOKEN:
        sys.exit("Нет токена: задайте AMO_TOKEN")
    load_calendar()
    meta, touch_fid = load_structure()
    users = load_users()
    now = int(time.time())

    deals, touches = [], []
    for kw, m in meta.items():
        params = {"filter[pipeline_id][]": m["pipeline_id"],
                  "with": "contacts",
                  "limit": 100}
        n = 0
        for lead in paginate("/leads", params, "leads"):
            if lead.get("pipeline_id") != m["pipeline_id"]:
                continue   # страховка: берём поля только той воронки, где сделка сейчас
            if lead["status_id"] not in (WON, LOST):
                touches.append(touch_record(lead, kw, m, users, touch_fid))
            rec = lead_record(lead, kw, m, users, now)
            if rec:
                deals.append(rec)
                n += 1
        print(f"{kw}: сделок в отчёте по срокам {n}")
    print(f"Открытых сделок для вкладки касаний: {len(touches)}")

    apply_incoming(touches, now)

    # кто клиент: имена основных контактов и компаний (только для открытых сделок)
    cnames = names_by_id("/contacts", "contacts", {t["ct"] for t in touches if t["ct"]})
    onames = names_by_id("/companies", "companies", {t["co"] for t in touches if t["co"]})
    for t in touches:
        t["ct"] = cnames.get(t["ct"], "") if t["ct"] else ""
        t["co"] = onames.get(t["co"], "") if t["co"] else ""
        del t["_cts"], t["_cos"]

    out = {
        "generated": datetime.fromtimestamp(now, MSK).strftime("%d.%m.%Y %H:%M"),
        "created_from": CREATED_FROM.strftime("%Y-%m-%d"),
        "pipelines": {kw: [s["name"] for s in m["stages"]] for kw, m in meta.items()},
        "norms": {kw: {s["name"]: STAGE_NORMS.get(f"{kw}/{s['name']}", STAGE_NORMS.get(s["name"]))
                       for s in m["stages"] if STAGE_NORMS.get(f"{kw}/{s['name']}", STAGE_NORMS.get(s["name"]))}
                  for kw, m in meta.items()},
        "deals": deals,
        "now": now,
        "touch_field": bool(touch_fid),
        "incoming_days": INCOMING_DAYS,
        "cal": CAL,                        # производственный календарь для расчётов на странице
        "open_stages": {kw: [s["name"] for s in m["open"]] for kw, m in meta.items()},
        "touches": touches,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    raw = json.dumps(out, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    password = os.environ.get("REPORT_PASSWORD", "")

    if password:
        # Шифрование: PBKDF2-SHA256 → ключ AES-256-GCM. Страница расшифровывает в браузере по паролю.
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
        from cryptography.hazmat.primitives import hashes
        import base64
        salt, iv = os.urandom(16), os.urandom(12)
        key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=ENC_ITER).derive(password.encode("utf-8"))
        ct = AESGCM(key).encrypt(iv, raw, None)   # ciphertext + 16-байтный тег, как ждёт WebCrypto
        b64 = lambda b: base64.b64encode(b).decode()
        with open(ENC_OUT, "w", encoding="utf-8") as fh:
            json.dump({"v": 1, "iter": ENC_ITER, "salt": b64(salt), "iv": b64(iv), "ct": b64(ct)}, fh)
        if os.path.exists(OUT):
            os.remove(OUT)   # открытая копия не должна попасть на сайт
        print(f"Готово: {len(deals)} сделок → {ENC_OUT} (зашифровано)")
    elif os.environ.get("REQUIRE_ENCRYPTION") == "1":
        sys.exit("Не задан секрет REPORT_PASSWORD — публикация без шифрования запрещена")
    else:
        with open(OUT, "wb") as fh:
            fh.write(raw)
        print(f"Готово: {len(deals)} сделок → {OUT} (без шифрования, для локальной проверки)")


if __name__ == "__main__":
    main()
