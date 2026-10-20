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

На странице нет названий сделок, компаний и контактов — только менеджер, воронка, этапы и сроки.

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
MSK = timezone(timedelta(hours=3))
CREATED_FROM = datetime(2026, 6, 1, tzinfo=MSK)   # с этой даты есть история переходов
FROM_TS = int(CREATED_FROM.timestamp())
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "data.json")
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
    for attempt in range(6):
        r = S.get(url, params=params, timeout=60)
        time.sleep(0.16)
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
        meta[kw] = {"pipeline_id": p["id"], "stages": stages, "lost_field": lost_field}
        print(f"{kw}: этапы {[s['name'] for s in stages]}")
    return meta


def load_users():
    try:
        return {u["id"]: u["name"] for u in paginate("/users", {"limit": 250}, "users")}
    except RuntimeError as e:
        print(f"! Список пользователей недоступен ({e}); будут ID")
        return {}


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
                seg.append([prev, round((ts - entries[prev]) / 86400, 3), i])
                prev = i
            else:
                anomalies += 1
            continue
        prev = i

    lost_from = lost_days = None
    if state == "lost" and prev is not None:
        lts = cf.get(m["lost_field"]) if m["lost_field"] else None
        if lts is None:
            lts = lead.get("closed_at")
        if lts and lts >= entries[prev]:
            lost_from, lost_days = prev, round((lts - entries[prev]) / 86400, 3)

    cur = age = None
    if state == "open":
        idx = next((i for i, s in enumerate(stages) if s["id"] == status), None)
        if idx is not None:
            cur = idx
            if entries[idx]:
                age = round((now - entries[idx]) / 86400, 3)

    cycle = None
    won_idx = next((i for i, s in enumerate(stages) if s["id"] == WON), None)
    if state == "won" and won_idx is not None and entries[won_idx]:
        cycle = round((entries[won_idx] - lead["created_at"]) / 86400, 3)

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
        "seg": seg,                       # [индекс этапа, дней на этапе, индекс следующего этапа]
        "lf": lost_from, "ld": lost_days,  # с какого этапа отказ и сколько дней до отказа
        "cur": cur, "age": age,           # открытые: текущий этап и дней на нём
        "cy": cycle,                      # успешные: дней от создания до «Передача в аккаунтинг»
        "an": anomalies,
    }


def main():
    if not TOKEN:
        sys.exit("Нет токена: задайте AMO_TOKEN")
    meta = load_structure()
    users = load_users()
    now = int(time.time())

    deals = []
    for kw, m in meta.items():
        params = {"filter[pipeline_id][]": m["pipeline_id"],
                  "limit": 250}
        n = 0
        for lead in paginate("/leads", params, "leads"):
            if lead.get("pipeline_id") != m["pipeline_id"]:
                continue   # страховка: берём поля только той воронки, где сделка сейчас
            rec = lead_record(lead, kw, m, users, now)
            if rec:
                deals.append(rec)
                n += 1
        print(f"{kw}: сделок {n}")

    out = {
        "generated": datetime.fromtimestamp(now, MSK).strftime("%d.%m.%Y %H:%M"),
        "created_from": CREATED_FROM.strftime("%Y-%m-%d"),
        "pipelines": {kw: [s["name"] for s in m["stages"]] for kw, m in meta.items()},
        "norms": {kw: {s["name"]: STAGE_NORMS.get(f"{kw}/{s['name']}", STAGE_NORMS.get(s["name"]))
                       for s in m["stages"] if STAGE_NORMS.get(f"{kw}/{s['name']}", STAGE_NORMS.get(s["name"]))}
                  for kw, m in meta.items()},
        "deals": deals,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, separators=(",", ":"))
    print(f"Готово: {len(deals)} сделок → {OUT}")


if __name__ == "__main__":
    main()
