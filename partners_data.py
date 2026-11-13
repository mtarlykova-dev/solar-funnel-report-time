"""
Данные для вкладки «Партнёры»: партнёры (воронка «Партнерство») и их лиды.

Лид относится к партнёру, если (по порядку):
  1. в лиде заполнено «referrer партнера» — его ставит скрипт amo-partner-link;
  2. лид пришёл по партнёрской ссылке (utm_source / last_utm_source = partner или в поле referrer
     числовой код из виджета) — код берётся из utm_medium / last utm_medium / referrer;
  3. в поле лида «Партнер» вручную выбрана компания партнёра.
Код сверяется с ID сделки партнёра, её полями «referrer партнера» и «utm medium партнера»
(у PRMOnline код 31358193 не совпадает с ID сделки 33583531).
"""

import json
import re
from datetime import datetime

PARTNERS_PIPELINE = 8923730          # Партнерство
LEAD_PIPELINES = [11489426,          # Партнерство: лидогенерация
                  8454514,           # Inbound (партнёрские лиды до переезда в отдельную воронку)
                  8454522]           # Маркетинг
LOST, WON = 143, 142

# поля лида
F_UTM_SOURCE, F_LAST_UTM_SOURCE = 645259, 705866
F_UTM_MEDIUM, F_LAST_UTM_MEDIUM = 645261, 705868
F_REFERRER = 645275
F_PARTNER_LINK = 707209              # «Партнер» (связь с компанией партнёра)
# поля сделки партнёра (и лида — «referrer партнера»)
F_PARTNER_REFERRER = 705296
F_PARTNER_UTM_MEDIUM = 705258
F_PARTNER_URL = 707793


def _cf(e, fid):
    for f in e.get("custom_fields_values") or []:
        if f.get("field_id") == fid and f.get("values"):
            return f["values"][0].get("value")
    return None


def _code(v):
    m = re.search(r"\d{6,}", str(v)) if v is not None else None
    return m.group(0) if m else None


def _link_company(lead):
    raw = _cf(lead, F_PARTNER_LINK)
    if not raw:
        return None
    try:
        cos = json.loads(raw).get("companies") or []
        return int(cos[0]) if cos else None
    except (TypeError, ValueError, AttributeError):
        return None


def build(api, paginate, users, msk, names_by_id):
    pipelines = {p["id"]: p for p in api("/leads/pipelines")["_embedded"]["pipelines"]}

    def stages(pid):
        p = pipelines.get(pid)
        if not p:
            return []
        return [{"id": s["id"], "name": s["name"]}
                for s in sorted(p["_embedded"]["statuses"], key=lambda s: s.get("sort", 0))]

    st_name = {}
    for pid in [PARTNERS_PIPELINE] + LEAD_PIPELINES:
        for s in stages(pid):
            st_name[(pid, s["id"])] = s["name"]

    # ── партнёры ──
    partners, by_code, by_company = [], {}, {}
    for d in paginate("/leads", {"filter[pipeline_id][]": PARTNERS_PIPELINE, "with": "contacts", "limit": 250}, "leads"):
        emb = d.get("_embedded") or {}
        cts = emb.get("contacts") or []
        main = next((c["id"] for c in cts if c.get("is_main")), cts[0]["id"] if cts else None)
        cos = [c["id"] for c in emb.get("companies") or []]
        ref = _code(_cf(d, F_PARTNER_REFERRER)) or str(d["id"])
        rec = {
            "id": d["id"], "n": (d.get("name") or "").strip(), "ref": ref,
            "st": st_name.get((PARTNERS_PIPELINE, d["status_id"]), str(d["status_id"])),
            "s": "won" if d["status_id"] == WON else "lost" if d["status_id"] == LOST else "open",
            "m": users.get(d.get("responsible_user_id"), str(d.get("responsible_user_id"))),
            "c": datetime.fromtimestamp(d["created_at"], msk).strftime("%Y-%m-%d"),
            "_ct": main, "_co": cos[0] if cos else None,
        }
        partners.append(rec)
        for c in {str(d["id"]), _code(_cf(d, F_PARTNER_REFERRER)), _code(_cf(d, F_PARTNER_UTM_MEDIUM))}:
            if c and (c not in by_code or c == str(d["id"])):
                by_code[c] = rec
        for co in cos:
            by_company.setdefault(co, rec)
    print(f"Партнёры: {len(partners)} сделок, кодов {len(by_code)}")

    # ── лиды партнёров ──
    leads, how = [], {"ref": 0, "utm": 0, "company": 0}
    for pid in LEAD_PIPELINES:
        if pid not in pipelines:
            print(f"! Воронка {pid} не найдена — пропускаю")
            continue
        n = 0
        for l in paginate("/leads", {"filter[pipeline_id][]": pid, "limit": 250}, "leads"):
            p, via = None, None
            c = _code(_cf(l, F_PARTNER_REFERRER))
            if c and c in by_code:
                p, via = by_code[c], "ref"
            if not p:
                is_partner = "partner" in (str(_cf(l, F_UTM_SOURCE) or "").lower(),
                                           str(_cf(l, F_LAST_UTM_SOURCE) or "").lower()) \
                    or _code(_cf(l, F_REFERRER)) is not None
                if is_partner:
                    for fid in (F_UTM_MEDIUM, F_LAST_UTM_MEDIUM, F_REFERRER):
                        c = _code(_cf(l, fid))
                        if c and c in by_code:
                            p, via = by_code[c], "utm"
                            break
            if not p:
                co = _link_company(l)
                if co and co in by_company:
                    p, via = by_company[co], "company"
            if not p and pid != LEAD_PIPELINES[0]:
                continue
            # в воронке «Партнерство: лидогенерация» показываем и лиды без определённого партнёра
            how[via or "none"] = how.get(via or "none", 0) + 1
            n += 1
            cos = (l.get("_embedded") or {}).get("companies") or []
            leads.append({
                "id": l["id"], "n": (l.get("name") or "").strip(), "pa": p["id"] if p else None,
                "pl": pipelines[pid]["name"], "st": st_name.get((pid, l["status_id"]), str(l["status_id"])),
                "s": "won" if l["status_id"] == WON else "lost" if l["status_id"] == LOST else "open",
                "m": users.get(l.get("responsible_user_id"), str(l.get("responsible_user_id"))),
                "c": datetime.fromtimestamp(l["created_at"], msk).strftime("%Y-%m-%d"),
                "_co": cos[0]["id"] if cos else None,
            })
        print(f"  {pipelines[pid]['name']}: лидов партнёров {n}")
    print(f"Лиды партнёров: {len(leads)} (по referrer партнера {how['ref']}, по utm {how['utm']}, "
          f"по полю «Партнер» {how['company']}, партнёр не определён {how.get('none', 0)})")

    # имена контактов/компаний партнёров и компаний клиентов
    cnames = names_by_id("/contacts", "contacts", {p["_ct"] for p in partners if p["_ct"]})
    onames = names_by_id("/companies", "companies",
                         {p["_co"] for p in partners if p["_co"]} | {x["_co"] for x in leads if x["_co"]})
    for p in partners:
        p["ct"] = cnames.get(p.pop("_ct"), "") if p.get("_ct") else ""
        p.pop("_ct", None)
        co = p.pop("_co", None)
        p["co"] = onames.get(co, "") if co else ""
    for x in leads:
        co = x.pop("_co", None)
        x["co"] = onames.get(co, "") if co else ""

    stage_order = {pipelines[pid]["name"]: [s["name"] for s in stages(pid)] for pid in LEAD_PIPELINES if pid in pipelines}
    return {"partners": partners, "leads": leads, "stages": stage_order,
            "pl_order": [pipelines[pid]["name"] for pid in LEAD_PIPELINES if pid in pipelines]}
