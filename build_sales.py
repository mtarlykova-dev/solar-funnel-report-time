#!/usr/bin/env python3
"""
Данные для дашборда «Solar Staff — аналитика продаж» (воронка Inbound) → docs/sales/data.enc.json

Что собирается:
  • все сделки Inbound, созданные с 01.01.2026, и все открытые сделки Inbound (для нагрузки);
  • история этапов из журнала событий amoCRM (lead_status_changed с 01.01.2026);
    запасной источник — поля «Дата перехода: …» (заполнены с 01.06.2026);
  • «Дата первой оплаты» (фактическая первая оплата), «Дата первого пополнения», «Дата активации»;
  • источник («Автоматический источник лида»), причина отказа, «Последнее касание»;
  • открытые задачи по сделкам Inbound.

Достижение этапа: сделка дошла до этапа, если была на нём или на любом более позднем (кроме «Отказа»).
Дата достижения — самая ранняя дата входа на этот или более поздний этап.

Если загрузка оборвалась (не все страницы, ошибка API), новый срез НЕ публикуется:
берётся последний полный срез с сайта, а в docs/sales/status.json пишется статус ошибки.

Запуск: AMO_TOKEN=... REPORT_PASSWORD=... python build_sales.py
"""

import base64
import json
import os
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone

import requests

from build_report import api, paginate, load_users, ENC_ITER

MSK = timezone(timedelta(hours=3))
FROM = datetime(2026, 1, 1, tzinfo=MSK)
FROM_TS = int(FROM.timestamp())
PIPELINE_NAME = "Inbound"
LOST, WON = 143, 142
ROOT = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(ROOT, "docs", "sales")
ENC_OUT = os.path.join(OUT_DIR, "data.enc.json")
STATUS_OUT = os.path.join(OUT_DIR, "status.json")
PREV_URL = os.environ.get("SALES_PREV_URL",
                          "https://mtarlykova-dev.github.io/solar-funnel-report-time/sales/data.enc.json")

F_PAY = 703344          # Дата первой оплаты
F_TOPUP = 701899        # Дата первого пополнения
F_ACT = 289801          # Дата активации
F_SRC = 705031          # Автоматический источник лида
F_LOST = 701893         # Причина отказа.
F_TOUCH = 708203        # Последнее касание
TRANS_PREFIX = "Дата перехода:"

# ключевые этапы для таблиц (названия в воронке Inbound)
KEY = {"qual": "Квалифицирован", "dog": "Согласование договора", "act": "Активация",
       "top": "Пополнение баланса", "pst": "Оплата задачи"}


class Incomplete(RuntimeError):
    pass


def norm(s):
    return " ".join(str(s).lower().replace("ё", "е").split())


def cfv(lead, fid):
    for f in lead.get("custom_fields_values") or []:
        if f.get("field_id") == fid and f.get("values"):
            return f["values"][0].get("value")
    return None


def ts_or_none(v):
    try:
        v = int(v)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def paged_all(path, params, key, counter):
    """Все страницы; если цепочка страниц оборвалась — Incomplete."""
    url, p, pages = path, params, 0
    out = []
    while url:
        data = api(url, p)
        pages += 1
        if not data:
            break
        out.extend(data.get("_embedded", {}).get(key, []))
        url = data.get("_links", {}).get("next", {}).get("href")
        p = None
        if pages > 5000:
            raise Incomplete(f"{path}: слишком много страниц")
    counter[key] = counter.get(key, 0) + pages
    return out


def build():
    t0 = time.time()
    pages = {}
    pipe = next((p for p in api("/leads/pipelines")["_embedded"]["pipelines"] if norm(p["name"]) == norm(PIPELINE_NAME)), None)
    if not pipe:
        raise Incomplete("Воронка Inbound не найдена")
    pid = pipe["id"]
    statuses = sorted(pipe["_embedded"]["statuses"], key=lambda s: s.get("sort", 0))
    # рабочие этапы по порядку: без «Неразобранного» (первый, type=1) и «Отказа»
    stages = [s for s in statuses if s["id"] != LOST and s.get("type", 0) != 1]
    idx = {s["id"]: i for i, s in enumerate(stages)}
    names = [s["name"] for s in stages]
    key_idx = {}
    for k, nm in KEY.items():
        i = next((i for i, n in enumerate(names) if norm(n) == norm(nm)), None)
        if i is None:
            raise Incomplete(f"В Inbound нет этапа «{nm}»")
        key_idx[k] = i
    print(f"Inbound {pid}: этапы {names}")

    # поля «Дата перехода: <этап>» этой воронки — запасной источник истории
    fields = list(paginate("/leads/custom_fields", {"limit": 250}, "custom_fields"))
    trans = {}
    for f in fields:
        if f["name"].startswith(TRANS_PREFIX):
            nm = norm(f["name"].split(":", 1)[1])
            trans.setdefault(nm, []).append(f)
    trans_fid = {}
    for i, n in enumerate(names):
        c = sorted(trans.get(norm(n), []), key=lambda x: (x.get("sort", 0), x["id"]))
        if c:
            trans_fid[i] = c[0]["id"]   # верхнее поле — Inbound (как в отчёте по срокам)

    users = load_users()
    disp = lambda uid: users.get(uid, str(uid)).replace(" (деактивирован)", "")

    # ── сделки ──
    now = int(time.time())
    raw = paged_all("/leads", {"filter[pipeline_id][]": pid, "limit": 250}, "leads", pages)
    leads = [l for l in raw if l.get("pipeline_id") == pid and (l["created_at"] >= FROM_TS or l["status_id"] not in (WON, LOST))]
    ids = {l["id"] for l in leads}
    print(f"Сделок Inbound всего {len(raw)}, в дашборд {len(leads)} (с 01.01.2026 и все открытые)")
    if len(raw) == 0:
        raise Incomplete("amo вернул 0 сделок")

    # ── история этапов ──
    first_in, last_in = {}, {}
    n_ev = 0
    for ev in paged_all("/events", [("filter[type][]", "lead_status_changed"), ("filter[created_at][from]", FROM_TS),
                                    ("limit", 100)], "events", pages):
        if ev.get("entity_id") not in ids:
            continue
        try:
            st = ev["value_after"][0]["lead_status"]
        except (KeyError, IndexError, TypeError):
            continue
        if st.get("pipeline_id") != pid or st.get("id") not in idx:
            continue
        n_ev += 1
        i, t, lid = idx[st["id"]], ev["created_at"], ev["entity_id"]
        d = first_in.setdefault(lid, {})
        if i not in d or t < d[i]:
            d[i] = t
        li = last_in.setdefault(lid, {})
        if i not in li or t > li[i]:
            li[i] = t
    print(f"Событий смены этапа в Inbound: {n_ev}")

    # ── задачи ──
    tasks_raw = paged_all("/tasks", {"filter[is_completed]": 0, "filter[entity_type]": "leads", "limit": 250}, "tasks", pages)
    tasks = [[t["entity_id"], disp(t.get("responsible_user_id")), t.get("complete_till")]
             for t in tasks_raw if t.get("entity_id") in ids and not t.get("is_completed")]
    print(f"Открытых задач по сделкам Inbound: {len(tasks)} (всего открытых задач по сделкам {len(tasks_raw)})")

    out_leads, src_none, gaps = [], 0, {"no_src": 0, "lost_no_reason": 0, "pay_no_stage": 0, "no_touch_open": 0,
                                         "fallback_fields": 0}
    for l in leads:
        lid, cur = l["id"], l["status_id"]
        fi = dict(first_in.get(lid, {}))
        # запасной источник: поля «Дата перехода»
        for i, fid in trans_fid.items():
            v = ts_or_none(cfv(l, fid))
            if v and i not in fi:
                fi[i] = v
                gaps["fallback_fields"] += 1
        state = "won" if cur == WON else "lost" if cur == LOST else "open"
        fi_real = dict(fi)
        fi_real.setdefault(0, l["created_at"])
        if cur in idx and idx[cur] not in fi:
            fi[idx[cur]] = l["created_at"]
        fi.setdefault(0, l["created_at"])       # первый рабочий этап («Новая заявка») — с создания
        reach = [0] * len(names)
        best = None
        for i in range(len(names) - 1, -1, -1):
            if i in fi:
                best = fi[i] if best is None else min(best, fi[i])
            reach[i] = best or 0
        mx = max(fi) if fi else 0
        sa = (last_in.get(lid, {}).get(idx.get(cur)) if cur in idx else None) or (fi.get(idx.get(cur)) if cur in idx else None)
        src = cfv(l, F_SRC)
        if not src:
            gaps["no_src"] += 1
        lr = cfv(l, F_LOST) if state == "lost" else None
        if state == "lost" and not lr:
            gaps["lost_no_reason"] += 1
        pay = ts_or_none(cfv(l, F_PAY))
        if pay and not reach[key_idx["pst"]]:
            gaps["pay_no_stage"] += 1
        lt = ts_or_none(cfv(l, F_TOUCH))
        if state == "open" and not lt:
            gaps["no_touch_open"] += 1
        out_leads.append({
            "id": lid, "n": (l.get("name") or "").strip(), "c": l["created_at"],
            "m": disp(l.get("responsible_user_id")), "src": src or "", "s": state,
            "cur": idx.get(cur), "mx": mx, "r": reach,
            "ra": [fi_real.get(i, 0) for i in range(len(names))],   # подтверждённый вход на этап (0 — не было)
            "pay": pay, "top": ts_or_none(cfv(l, F_TOPUP)), "act": ts_or_none(cfv(l, F_ACT)),
            "lr": lr or "", "lt": lt, "sa": sa, "cl": l.get("closed_at"),
        })

    return {
        "generated": datetime.fromtimestamp(now, MSK).strftime("%d.%m.%Y %H:%M"), "now": now,
        "from": FROM.strftime("%Y-%m-%d"), "pipeline": PIPELINE_NAME, "stages": names, "key": key_idx,
        "leads": out_leads, "tasks": tasks,
        "load": {"ok": True, "pages": pages, "leads_total_inbound": len(raw), "leads": len(out_leads),
                 "events": n_ev, "tasks": len(tasks), "seconds": round(time.time() - t0), "gaps": gaps,
                 "history_fields_from": "2026-06-01"},
    }


def encrypt(obj, password):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes
    raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    salt, iv = os.urandom(16), os.urandom(12)
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=ENC_ITER).derive(password.encode("utf-8"))
    b64 = lambda b: base64.b64encode(b).decode()
    return {"v": 1, "iter": ENC_ITER, "salt": b64(salt), "iv": b64(iv), "ct": b64(AESGCM(key).encrypt(iv, raw, None))}


def write_status(ok, msg, extra=None):
    st = {"ok": ok, "at": datetime.now(MSK).strftime("%d.%m.%Y %H:%M"), "message": msg}
    if extra:
        st.update(extra)
    with open(STATUS_OUT, "w", encoding="utf-8") as fh:
        json.dump(st, fh, ensure_ascii=False)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    password = os.environ.get("REPORT_PASSWORD", "")
    if not password and os.environ.get("REQUIRE_ENCRYPTION") == "1":
        sys.exit("Не задан REPORT_PASSWORD — публикация без шифрования запрещена")
    try:
        data = build()
    except Exception as e:
        traceback.print_exc()
        msg = f"{e.__class__.__name__}: {e}"[:300]
        print(f"! Загрузка не завершена: {msg}. Оставляю последний полный срез.")
        try:
            r = requests.get(PREV_URL, timeout=30)
            r.raise_for_status()
            with open(ENC_OUT, "wb") as fh:
                fh.write(r.content)
            write_status(False, f"Последнее обновление не завершилось ({msg}). Показан последний полный срез.")
        except Exception as e2:
            write_status(False, f"Обновление не завершилось ({msg}), предыдущий срез недоступен ({e2.__class__.__name__}).")
        return
    if password:
        with open(ENC_OUT, "w", encoding="utf-8") as fh:
            json.dump(encrypt(data, password), fh)
    else:
        with open(os.path.join(OUT_DIR, "data.json"), "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
    write_status(True, "Обновление завершено полностью", {"leads": data["load"]["leads"], "tasks": data["load"]["tasks"]})
    print(f"Готово: {len(data['leads'])} сделок, {len(data['tasks'])} задач → {OUT_DIR}")


if __name__ == "__main__":
    main()
