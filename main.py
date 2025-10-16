# fincoach_llm_agent_ultra_v4.py
import os, json, sqlite3, csv, re
from uuid import uuid4
from io import StringIO
from datetime import datetime, timedelta
from statistics import mean
from collections import defaultdict, Counter
from flask import Flask, request, jsonify, render_template_string, session
import openai

openai.api_key = os.getenv("OPENAI_API_KEY", "")
app = Flask(__name__)
app.secret_key = "change-this-secret"
DB_PATH = "fincoach.db"

def db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    with db() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS profiles (sid TEXT, key TEXT, value TEXT, PRIMARY KEY (sid,key));")
        conn.execute("CREATE TABLE IF NOT EXISTS txns (sid TEXT, date TEXT, description TEXT, amount REAL, category TEXT);")
        conn.execute("CREATE TABLE IF NOT EXISTS history (sid TEXT, role TEXT, content TEXT, ts TEXT);")
        conn.execute("CREATE TABLE IF NOT EXISTS caps (sid TEXT, category TEXT, weekly REAL, PRIMARY KEY (sid,category));")
        conn.commit()
init_db()

CATEGORY_KEYWORDS = {
    "food & dining": ["swiggy","zomato","restaurant","cafe","uber eats","food","eat"],
    "groceries": ["grocery","supermarket","bigbasket","dmart","more","relmart"],
    "transport": ["uber","ola","metro","fuel","petrol","diesel","train","irctc","bus"],
    "utilities": ["electricity","water","gas","internet","broadband","wifi","mobile","phone","recharge","dth"],
    "rent": ["rent","landlord"],
    "shopping": ["amazon","flipkart","myntra","ajio","nykaa","shopping"],
    "entertainment": ["netflix","spotify","youtube","prime","hotstar","movie","theatre"],
    "health": ["pharmacy","medical","hospital","clinic","doctor","med"],
    "education": ["course","udemy","coursera","byju","unacademy","exam","tuition"],
    "fees & charges": ["fee","charge","penalty","fine"],
    "income": ["salary","payout","payment","credit","freelance","upwork","fiverr"],
    "transfer": ["upi","imps","neft","rtgs","transfer"],
    "other": []
}

PROFILE_FIELDS = [
    ("starting_balance","What’s your current account balance (₹)?","number"),
    ("monthly_income","What’s your typical monthly income (₹)?","number"),
    ("monthly_fixed_bills","About how much are your fixed bills per month (₹)?","number"),
    ("weekly_food","Average weekly spend on Food & Dining (₹)?","number"),
    ("weekly_transport","Average weekly spend on Transport/Fuel (₹)?","number"),
    ("weekly_shopping","Average weekly spend on Shopping/Other (₹)?","number"),
    ("goal_name","What’s your current savings goal?","text"),
    ("goal_target","What’s the target amount for that goal (₹)?","number"),
    ("has_debt","Do you have any loan/credit card debt? (yes/no)","boolean"),
    ("monthly_emi","What’s your monthly EMI or minimum payment (₹)? (0 if none)","number"),
]

DEMO_CSV = """date,description,amount
2025-09-25,Salary September,80000
2025-09-26,Amazon Shopping,-1499
2025-09-27,Uber Ride,-220
2025-09-30,Electricity Bill,-1450
2025-10-01,UPI Transfer,-600
2025-10-02,Swiggy,-350
2025-10-05,IRCTC,-980
2025-10-07,Salary October,80000
2025-10-09,Netflix,-499
2025-10-10,DMart,-1200
2025-10-12,Ola Ride,-260
2025-10-14,Mobile Recharge,-399
"""

def ensure_sid():
    if "sid" not in session:
        session["sid"] = str(uuid4())
    return session["sid"]

def load_profile(sid):
    with db() as conn:
        rows = conn.execute("SELECT key,value FROM profiles WHERE sid=?", (sid,)).fetchall()
    out = {}
    t = {k:typ for (k,_,typ) in PROFILE_FIELDS}
    for r in rows:
        typ = t.get(r["key"], "text")
        v = r["value"]
        if typ == "number":
            try: out[r["key"]] = float(str(v).replace(",",""))
            except: out[r["key"]] = 0.0
        elif typ == "boolean":
            out[r["key"]] = str(v).lower() in ["yes","y","true","1"]
        else:
            out[r["key"]] = v
    return out

def save_profile_field(sid, key, value):
    with db() as conn:
        conn.execute("INSERT INTO profiles(sid,key,value) VALUES(?,?,?) ON CONFLICT(sid,key) DO UPDATE SET value=excluded.value", (sid, key, str(value)))
        conn.commit()

def set_history(sid, role, content):
    with db() as conn:
        conn.execute("INSERT INTO history(sid,role,content,ts) VALUES(?,?,?,?)", (sid, role, content, datetime.utcnow().isoformat()))
        conn.commit()

def get_history(sid, limit=80):
    with db() as conn:
        rows = conn.execute("SELECT role,content FROM history WHERE sid=? ORDER BY ts ASC",(sid,)).fetchall()
    return [{"role":r["role"],"content":r["content"]} for r in rows][-limit:]

def clear_state(sid):
    with db() as conn:
        conn.execute("DELETE FROM profiles WHERE sid=?", (sid,))
        conn.execute("DELETE FROM txns WHERE sid=?", (sid,))
        conn.execute("DELETE FROM history WHERE sid=?", (sid,))
        conn.execute("DELETE FROM caps WHERE sid=?", (sid,))
        conn.commit()

def insert_txns(sid, rows):
    with db() as conn:
        conn.execute("DELETE FROM txns WHERE sid=?", (sid,))
        for r in rows:
            conn.execute("INSERT INTO txns(sid,date,description,amount,category) VALUES(?,?,?,?,?)", (sid, r["date"].isoformat(), r["description"], r["amount"], r["category"]))
        conn.commit()

def get_txns(sid):
    with db() as conn:
        rows = conn.execute("SELECT date,description,amount,category FROM txns WHERE sid=? ORDER BY date ASC", (sid,)).fetchall()
    out = []
    for r in rows:
        out.append({"date": datetime.fromisoformat(r["date"]).date(), "description": r["description"], "amount": float(r["amount"]), "category": r["category"]})
    return out

def set_cap(sid, category, weekly):
    with db() as conn:
        conn.execute("INSERT INTO caps(sid,category,weekly) VALUES(?,?,?) ON CONFLICT(sid,category) DO UPDATE SET weekly=excluded.weekly", (sid, category.lower(), float(weekly)))
        conn.commit()

def set_caps_bulk(sid, items):
    with db() as conn:
        for it in items:
            conn.execute("INSERT INTO caps(sid,category,weekly) VALUES(?,?,?) ON CONFLICT(sid,category) DO UPDATE SET weekly=excluded.weekly", (sid, it["category"].lower(), float(it["weekly"])))
        conn.commit()

def list_caps(sid):
    with db() as conn:
        rows = conn.execute("SELECT category,weekly FROM caps WHERE sid=?", (sid,)).fetchall()
    return [{"category": r["category"], "weekly": float(r["weekly"])} for r in rows]

def infer_category(desc, amount):
    d = (desc or "").lower()
    if amount > 0:
        for cat,kws in CATEGORY_KEYWORDS.items():
            if cat == "income" and any(k in d for k in kws):
                return "income"
        return "income"
    for cat,kws in CATEGORY_KEYWORDS.items():
        if cat == "income": continue
        if any(k in d for k in kws):
            return cat
    if "rent" in d: return "rent"
    return "other"

def parse_csv(text):
    f = StringIO(text.strip())
    reader = csv.DictReader(f)
    rows = []
    for r in reader:
        date_str = r.get("date") or r.get("Date") or r.get("DATE") or ""
        desc = r.get("description") or r.get("Description") or r.get("DESC") or r.get("narration") or ""
        amt_str = r.get("amount") or r.get("Amount") or r.get("AMOUNT") or r.get("amt") or "0"
        dt = None
        for fmt in ("%Y-%m-%d","%d/%m/%Y","%d-%m-%Y","%m/%d/%Y"):
            try:
                dt = datetime.strptime(date_str.strip(), fmt).date(); break
            except: pass
        if not dt:
            try: dt = datetime.fromisoformat(date_str.strip()).date()
            except: continue
        try: amount = float(str(amt_str).replace(",","").strip())
        except: continue
        rows.append({"date": dt, "description": desc.strip(), "amount": amount})
    rows.sort(key=lambda x: x["date"])
    return rows

def enrich_transactions(rows):
    return [{**r, "category": infer_category(r["description"], r["amount"])} for r in rows]

def detect_recurring(txns, min_occ=3):
    def root(d):
        p = (d or "").lower().split()
        return " ".join(p[:2]) if p else ""
    g = defaultdict(list)
    for t in txns:
        sign = "IN" if t["amount"] > 0 else "OUT"
        g[(root(t["description"]),sign)].append(t["date"])
    rec = {"income":[], "bills":[]}
    for (k,sign),dates in g.items():
        dates = sorted(dates)
        if len(dates) < min_occ: continue
        gaps = [(dates[i]-dates[i-1]).days for i in range(1,len(dates))]
        if not gaps: continue
        avg = mean(gaps)
        if 20 <= avg <= 40:
            amts = [t["amount"] for t in txns if (root(t["description"]), "IN" if t["amount"]>0 else "OUT")== (k,sign)]
            amts.sort()
            med = amts[len(amts)//2]
            if sign=="IN": rec["income"].append({"name": k or "recurring income","amount": abs(med)})
            else: rec["bills"].append({"name": k or "recurring bill","amount": abs(med)})
    return rec

def summarize_cashflow(txns, horizon_days=28, starting_balance=0.0):
    if not txns: return {"projected_min": starting_balance, "projected_end": starting_balance, "daily_avg": 0.0}
    end = txns[-1]["date"]
    start = end - timedelta(days=30)
    daily = defaultdict(float)
    for t in txns:
        if t["date"] >= start: daily[t["date"]] += t["amount"]
    if daily: avg = mean(daily.values())
    else:
        span = (txns[-1]["date"] - txns[0]["date"]).days + 1
        avg = sum(t["amount"] for t in txns) / max(span,1)
    bal = starting_balance
    m = bal
    for _ in range(horizon_days):
        bal += avg
        m = min(m, bal)
    return {"projected_min": round(m,2), "projected_end": round(bal,2), "daily_avg": round(avg,2)}

def category_spend(txns, days=30):
    if not txns: return {}
    end = txns[-1]["date"]; start = end - timedelta(days=days)
    out = defaultdict(float)
    for t in txns:
        if t["date"] >= start and t["amount"] < 0:
            out[t["category"]] += abs(t["amount"])
    return dict(sorted(out.items(), key=lambda x:x[1], reverse=True))

def currency(n):
    try:
        return f"₹{float(n):,.0f}"
    except:
        return f"₹{n}"

def make_recommendations_from_txns(txns, balance_hint=0.0, caps=None):
    if not txns: return make_recommendations_from_profile({})
    inc = sum(t["amount"] for t in txns if t["amount"]>0)
    exp = sum(-t["amount"] for t in txns if t["amount"]<0)
    net = inc - exp
    rec = detect_recurring(txns)
    monthly_inc = sum(i["amount"] for i in rec["income"]) or max(inc,0.0)
    monthly_bills = sum(b["amount"] for b in rec["bills"])
    fc = summarize_cashflow(txns, 28, balance_hint)
    cats_sorted = list(category_spend(txns,30).items())
    top3 = cats_sorted[:3]
    essential = monthly_bills if monthly_bills>0 else min(10000.0, exp)
    target_em = max(10000.0, round(essential*1.0,0))
    micro = max(300.0, round(0.1*(monthly_inc/4.0),0))
    actions = []
    if fc["projected_min"] < 0:
        actions.append({"title":"Shortfall risk in next 4 weeks","detail":f"Projected minimum balance dips by **{currency(abs(fc['projected_min']))}**.","cta":"Set weekly caps on top categories"})
    for c,a in top3:
        cap = round(0.8*a/4.0,0)
        current = None
        if caps:
            for x in caps:
                if x["category"] == c:
                    current = x["weekly"]
                    break
        if current is None:
            actions.append({"title":f"Cap **{c}** spending","detail":f"Last 30 days: **{currency(a)}**. Suggested weekly cap: **{currency(cap)}**.","cta":f"Apply weekly cap for {c}"})
        else:
            actions.append({"title":f"Weekly cap set: **{c}**","detail":f"Cap: **{currency(current)}**. Last 30 days: **{currency(a)}**.","cta":"Adjust cap if needed"})
    actions.append({"title":"Build your Emergency Fund","detail":f"Target at least **{currency(target_em)}** (≈ 1 month of essentials).","cta":f"Auto-save **{currency(micro)}** weekly"})
    incomes = [t for t in txns if t["amount"]>0]
    srcs = Counter([" ".join((t["description"] or "").lower().split()[:2]) for t in incomes])
    if len(srcs)>1:
        actions.append({"title":"Income is variable","detail":"Multiple income sources detected. Maintain 10–15 days of average expenses as buffer.","cta":f"Increase buffer by {currency(1000)}–{currency(2000)} this week"})
    summary = f"**Overview**\n- Income: **{currency(inc)}**  |  Expense: **{currency(exp)}**  |  Net: **{currency(net)}**\n- Est. monthly income: **{currency(monthly_inc)}** | Bills: **{currency(monthly_bills)}**\n- 28-day forecast avg/day: **{currency(fc['daily_avg'])}** | Projected min: **{currency(fc['projected_min'])}**"
    return {"summary": summary, "actions": actions, "top3": top3}

def make_recommendations_from_profile(p, caps=None):
    sb = float(p.get("starting_balance") or 0)
    inc = float(p.get("monthly_income") or 0)
    bills = float(p.get("monthly_fixed_bills") or 0)
    wf = float(p.get("weekly_food") or 0)
    wt = float(p.get("weekly_transport") or 0)
    ws = float(p.get("weekly_shopping") or 0)
    gname = (p.get("goal_name") or "Emergency Fund").strip()
    gtarget = float(p.get("goal_target") or 10000)
    has_debt = str(p.get("has_debt") or "no").lower() in ["yes","y","true","1"]
    emi = float(p.get("monthly_emi") or 0)
    var_m = 4*(wf+wt+ws)
    net_m = inc - (bills + var_m + emi)
    avg = net_m/28.0 if inc else 0.0
    bal = sb; m = bal
    for _ in range(28):
        bal += avg; m = min(m, bal)
    actions = []
    if m < 0:
        actions.append({"title":"Shortfall risk in next 4 weeks","detail":f"Projected min dips by **{currency(abs(m))}**. Reduce weekly variable spend by 15–25% and consider a small buffer transfer.","cta":"Apply 20% caps this month"})
    caps_suggest = {"Food & Dining": round(0.85*wf,0), "Transport": round(0.85*wt,0), "Shopping/Other": round(0.85*ws,0)}
    for k,v in caps_suggest.items():
        if v>0:
            current=None
            if caps:
                for x in caps:
                    if x["category"].lower()==k.lower(): current=x["weekly"]
            if current is None:
                actions.append({"title":f"Cap **{k}** weekly spend","detail":f"Suggested weekly cap: **{currency(v)}**.","cta":f"Apply {k} cap"})
            else:
                actions.append({"title":f"Weekly cap set: **{k}**","detail":f"Cap: **{currency(current)}**.","cta":"Adjust cap if needed"})
    essential = bills + emi
    target_em = max(10000.0, essential*1.0)
    micro = max(300.0, round((inc*0.1)/4.0,0)) if inc else 300.0
    actions.append({"title":f"Build **{gname}**","detail":f"Target **{currency(gtarget)}**. Start with auto-save **{currency(micro)}** weekly.","cta":f"Auto-save {currency(micro)} weekly"})
    actions.append({"title":"Emergency Fund first","detail":f"Keep at least **{currency(target_em)}** as 1 month of essentials.","cta":"Create bills/emergency envelope"})
    if has_debt and emi>0:
        actions.append({"title":"Debt payoff strategy","detail":f"Pay EMI on time; add a small extra payment ({currency(300)}–{currency(700)}) monthly if possible to reduce interest.","cta":"Set extra EMI reminder"})
    summary = f"**Your Plan (profile-based)**\n- Starting balance: **{currency(sb)}**  |  Monthly income: **{currency(inc)}**\n- Fixed bills: **{currency(bills)}**  |  Variable/month (est): **{currency(var_m)}**  |  EMI: **{currency(emi)}**\n- 28-day forecast avg/day: **{currency(avg)}**  |  Projected min: **{currency(m)}**"
    return {"summary": summary, "actions": actions}

SYSTEM_PROMPT = """
You are FinCoach, an Indian-rupee-focused financial coach with full conversational control. Always format money as ₹ with Indian-style thousand separators. Proactively lead the conversation to collect missing data and set budgets. Use tools to: get_state, set_profile_field, set_cap, set_caps_bulk, list_caps, analyze, reset_state, load_demo_data. If the user says “set weekly caps”, choose sensible categories and call the tools to save those caps. Prefer transaction-based analysis if transactions exist; otherwise use profile fields. Never invent numbers. Respond in clean Markdown with headings, bullets, and bold key figures. End with up to three clear next steps.
"""

TOOLS = [
    {"type":"function","function":{"name":"get_state","description":"Return current profile, missing fields, caps, and whether transactions exist.","parameters":{"type":"object","properties":{}}}},
    {"type":"function","function":{"name":"set_profile_field","description":"Set or update one profile field.","parameters":{"type":"object","properties":{"field":{"type":"string"},"value":{"type":["string","number","boolean"]}},"required":["field","value"]}}},
    {"type":"function","function":{"name":"set_cap","description":"Create or update a weekly spending cap for a category.","parameters":{"type":"object","properties":{"category":{"type":"string"},"weekly":{"type":"number"}},"required":["category","weekly"]}}},
    {"type":"function","function":{"name":"set_caps_bulk","description":"Create or update multiple weekly caps.","parameters":{"type":"object","properties":{"items":{"type":"array","items":{"type":"object","properties":{"category":{"type":"string"},"weekly":{"type":"number"}},"required":["category","weekly"]}}},"required":["items"]}}},
    {"type":"function","function":{"name":"list_caps","description":"List all active weekly caps.","parameters":{"type":"object","properties":{}}}},
    {"type":"function","function":{"name":"analyze","description":"Analyze using transactions if present; else profile; include active caps.","parameters":{"type":"object","properties":{}}}},
    {"type":"function","function":{"name":"reset_state","description":"Clear memory for this session.","parameters":{"type":"object","properties":{}}}},
    {"type":"function","function":{"name":"load_demo_data","description":"Load built-in sample transactions for this session.","parameters":{"type":"object","properties":{}}}},
]

def profile_missing(profile):
    miss = []
    has_debt = str(profile.get("has_debt","")).lower()
    for key,prompt,typ in PROFILE_FIELDS:
        if key=="monthly_emi" and has_debt in ["no","n","false","0"]: continue
        v = profile.get(key)
        if v in [None,""] or (typ=="number" and v==0): miss.append({"field":key,"prompt":prompt,"type":typ})
    return miss

def tool_get_state(sid):
    prof = load_profile(sid)
    ms = profile_missing(prof)
    tx = get_txns(sid)
    caps = list_caps(sid)
    return {"profile": prof, "missing": ms, "has_transactions": bool(tx), "caps": caps}

def tool_set_profile_field(sid, field, value):
    keys = [k for (k,_,_) in PROFILE_FIELDS]
    if field not in keys: return {"ok": False, "error": f"Unknown field {field}"}
    t = {k:typ for (k,_,typ) in PROFILE_FIELDS}.get(field, "text")
    if t=="boolean":
        val = str(value).lower().strip() in ["yes","y","true","1"]
        save_profile_field(sid, field, val)
        if not val: save_profile_field(sid, "monthly_emi", 0)
    elif t=="number":
        try: v = float(str(value).replace(",",""))
        except: return {"ok": False, "error":"Expected a number"}
        save_profile_field(sid, field, v)
    else:
        save_profile_field(sid, field, str(value))
    return {"ok": True, "profile": load_profile(sid)}

def tool_set_cap(sid, category, weekly):
    set_cap(sid, category, weekly)
    return {"ok": True, "caps": list_caps(sid)}

def tool_set_caps_bulk(sid, items):
    set_caps_bulk(sid, items)
    return {"ok": True, "caps": list_caps(sid)}

def tool_list_caps(sid):
    return {"caps": list_caps(sid)}

def tool_analyze(sid):
    prof = load_profile(sid)
    tx = get_txns(sid)
    caps = list_caps(sid)
    if tx:
        return make_recommendations_from_txns(tx, balance_hint=float(prof.get("starting_balance") or 0), caps=caps)
    return make_recommendations_from_profile(prof, caps=caps)

def tool_reset_state(sid):
    clear_state(sid)
    return {"ok": True}

def tool_load_demo_data(sid):
    rows = enrich_transactions(parse_csv(DEMO_CSV))
    insert_txns(sid, rows)
    return {"ok": True, "count": len(rows)}

def run_llm(sid, user_text):
    msgs = [{"role":"system","content": SYSTEM_PROMPT}]
    for m in get_history(sid,80): msgs.append(m)
    msgs.append({"role":"user","content": user_text})
    for _ in range(10):
        resp = openai.chat.completions.create(model="gpt-4o-mini", messages=msgs, tools=TOOLS, temperature=0.2)
        msg = resp.choices[0].message
        if msg.tool_calls:
            for tc in msg.tool_calls:
                name = tc.function.name
                args = json.loads(tc.function.arguments or "{}")
                if name=="get_state": out = tool_get_state(sid)
                elif name=="set_profile_field": out = tool_set_profile_field(sid, args.get("field",""), args.get("value"))
                elif name=="set_cap": out = tool_set_cap(sid, args.get("category",""), args.get("weekly",0))
                elif name=="set_caps_bulk": out = tool_set_caps_bulk(sid, args.get("items",[]))
                elif name=="list_caps": out = tool_list_caps(sid)
                elif name=="analyze": out = tool_analyze(sid)
                elif name=="reset_state": out = tool_reset_state(sid)
                elif name=="load_demo_data": out = tool_load_demo_data(sid)
                else: out = {"error":"unknown tool"}
                msgs.append({"role":"assistant","tool_calls":[tc]})
                msgs.append({"role":"tool","tool_call_id": tc.id, "content": json.dumps(out)})
            continue
        return msg.content or "…"
    return "Updated. Ask for **Advice** or say **Start** to continue."

INDEX_HTML = """
<!doctype html>
<html>
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1"/>
<title>FinCoach</title>
<style>
:root{--bg:#0B0B0C;--bg2:#0A0A0B;--card:#121214;--panel:#161618;--ink:#ECEDEE;--muted:#9BA0A6;--line:#202024;--brand:#FFFFFF;}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:
radial-gradient(1400px 700px at 10% -10%,#1D1D22 0%,#0B0B0C 60%),
radial-gradient(1200px 600px at 120% 10%,#121217 0%,#0B0B0C 60%),
linear-gradient(180deg,#0B0B0C,#0A0A0B);color:var(--ink);font-family:Inter,ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,"Helvetica Neue",Arial}
.top{position:sticky;top:0;z-index:30;background:rgba(18,18,20,.9);backdrop-filter:saturate(160%) blur(12px);border-bottom:1px solid var(--line)}
.topw{max-width:1000px;margin:0 auto;padding:12px 16px;display:flex;gap:12px;align-items:center;justify-content:space-between}
.brand{font-weight:900;letter-spacing:.3px}
.actions{display:flex;gap:10px;align-items:center}
.btn{border:1px solid var(--line);background:#1B1B1E;color:var(--ink);padding:12px 16px;border-radius:14px;font-weight:800;cursor:pointer;transition:.12s}
.btn:hover{background:#212124;transform:translateY(-1px)}
.btn.primary{background:var(--brand);color:#0A0A0A;border-color:var(--brand)}
.wrap{max-width:1000px;margin:18px auto;padding:0 16px}
.card{background:linear-gradient(180deg,#131316,#101013);border:1px solid var(--line);border-radius:18px;padding:16px;box-shadow:0 18px 60px rgba(0,0,0,.35)}
.header{display:flex;align-items:center;gap:12px;margin:4px 0 14px}
.pulse{width:10px;height:10px;border-radius:50%;background:#4ade80;box-shadow:0 0 0 0 rgba(74,222,128,.6);animation:pulse 1.6s infinite}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(74,222,128,.65)}70%{box-shadow:0 0 0 12px rgba(74,222,128,0)}100%{box-shadow:0 0 0 0 rgba(74,222,128,0)}}
.chat{height:62vh;min-height:420px;overflow:auto;border:1px solid var(--line);background:var(--panel);border-radius:16px;padding:14px;scroll-behavior:smooth}
.row{position:sticky;bottom:0;background:linear-gradient(180deg,rgba(16,16,19,.0),rgba(16,16,19,1));padding-top:10px;display:flex;gap:10px;align-items:flex-end;margin-top:12px}
.ta{flex:1;min-height:60px;padding:14px;border:1px solid var(--line);border-radius:16px;background:#0E0E10;color:var(--ink);resize:vertical;outline:none;box-shadow:inset 0 0 0 1px rgba(255,255,255,.02)}
.ta:focus{box-shadow:0 0 0 2px rgba(255,255,255,.08), inset 0 0 0 1px rgba(255,255,255,.03)}
.send{padding:14px 18px;border:1px solid #2A2A2D;border-radius:16px;background:#1C1C1F;color:var(--ink);font-weight:900;cursor:pointer;transition:.12s}
.send:hover{background:#232327;transform:translateY(-1px)}
.msg{margin:12px 0;display:flex;gap:10px;align-items:flex-start}
.avatar{width:28px;height:28px;border-radius:999px;background:#0f172a;display:flex;align-items:center;justify-content:center;font-size:12px;color:#cbd5e1;border:1px solid var(--line)}
.me .avatar{background:#1b2a48}
.bub{max-width:86%;padding:14px 16px;border-radius:18px;border:1px solid var(--line);box-shadow:0 8px 22px rgba(0,0,0,.28)}
.bot .bub{background:#141417}
.me .bub{background:#1B1B1F}
.side{display:flex;gap:10px;align-items:flex-start}
.me{justify-content:flex-end}
.muted{color:var(--muted)}
.chips{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}
.chip{background:#141417;border:1px solid var(--line);padding:10px 12px;border-radius:999px;cursor:pointer;font-size:13px;color:var(--muted);transition:.12s}
.chip:hover{color:var(--ink);transform:translateY(-1px)}
.typing{font-size:13px;color:var(--muted);margin-top:8px;display:none}
.footer{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-top:12px;color:var(--muted);font-size:12px}
.kb{display:flex;gap:6px;flex-wrap:wrap}
.key{padding:8px 10px;border:1px dashed var(--line);border-radius:10px;background:#121216;color:var(--muted)}
.hint{font-size:12px;color:var(--muted)}
.toast{position:fixed;bottom:14px;left:50%;transform:translateX(-50%);background:#121216;border:1px solid var(--line);color:var(--ink);padding:10px 14px;border-radius:12px;box-shadow:0 10px 30px rgba(0,0,0,.35);display:none;z-index:40}
@media (max-width:740px){
  .topw{padding:10px 14px}
  .btn{padding:10px 14px;border-radius:12px}
  .chat{height:58vh;min-height:360px}
  .bub{max-width:94%}
  .ta{min-height:56px;border-radius:14px}
  .send{border-radius:14px;padding:12px 16px}
}
</style>
</head>
<body>
<div class="top">
  <div class="topw">
    <div class="brand">FinCoach</div>
    <div class="actions">
      <button class="btn primary" id="demo">Use Sample Data</button>
      <button class="btn" id="reset">Reset</button>
    </div>
  </div>
</div>
<div class="wrap">
  <div class="card">
    <div class="header"><div class="pulse"></div><div class="muted">Assistant is ready</div></div>
    <div id="chat" class="chat"></div>
    <div class="chips">
      <div class="chip" data-q="Start">Start</div>
      <div class="chip" data-q="Advice">Advice</div>
      <div class="chip" data-q="Set weekly caps">Set weekly caps</div>
      <div class="chip" data-q="Update goal">Update goal</div>
      <div class="chip" data-q="I have debt">I have debt</div>
    </div>
    <div class="row">
      <textarea id="msg" class="ta" placeholder="Say hi, or ask for advice…"></textarea>
      <button id="send" class="send">Send</button>
    </div>
    <div id="typing" class="typing">FinCoach is typing…</div>
    <div class="footer">
      <div class="hint">Your profile and chat persist locally. Use Sample Data for an instant demo. All figures in ₹.</div>
      <div class="kb">
        <div class="key">Shift + Enter = new line</div>
        <div class="key">Type “Advice” anytime</div>
      </div>
    </div>
  </div>
</div>
<div id="toast" class="toast"></div>
<script>
const chat=document.getElementById('chat')
const sendBtn=document.getElementById('send')
const msg=document.getElementById('msg')
const demoBtn=document.getElementById('demo')
const resetBtn=document.getElementById('reset')
const typing=document.getElementById('typing')
const toast=document.getElementById('toast')
function showToast(t){toast.textContent=t;toast.style.display='block';setTimeout(()=>{toast.style.display='none'},1600)}
function md(x){return (x||"").replace(/\\n/g,'<br>').replace(/\\*\\*(.*?)\\*\\*/g,'<strong>$1</strong>')}
function bubble(role,text){
  const row=document.createElement('div');row.className='msg '+(role==='user'?'me':'bot')
  const wrap=document.createElement('div');wrap.className='side'
  const av=document.createElement('div');av.className='avatar';av.textContent=role==='user'?'U':'AI'
  const b=document.createElement('div');b.className='bub';b.innerHTML=md(text)
  if(role==='user'){wrap.appendChild(b);wrap.appendChild(av);row.appendChild(wrap)}else{wrap.appendChild(av);wrap.appendChild(b);row.appendChild(wrap)}
  chat.appendChild(row);chat.scrollTop=chat.scrollHeight
}
async function init(){
  const r=await fetch('/init');const d=await r.json();bubble('bot',d.text)
}
async function ask(text){
  bubble('user',text);msg.value='';typing.style.display='block'
  const r=await fetch('/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text})})
  const d=await r.json();typing.style.display='none';bubble('bot',d.text)
}
sendBtn.onclick=()=>{const t=(msg.value||'').trim();if(!t)return;ask(t)}
msg.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();sendBtn.click()}})
document.querySelectorAll('.chip').forEach(c=>{c.onclick=()=>ask(c.dataset.q)})
demoBtn.onclick=async()=>{
  const csvPreview=`date,description,amount
2025-09-25,Salary September,80000
2025-09-26,Amazon Shopping,-1499
2025-09-27,Uber Ride,-220
2025-09-30,Electricity Bill,-1450
2025-10-01,UPI Transfer,-600`
  const intro="Use sample data"
  msg.value=intro+"\\n\\n"+csvPreview
  bubble('user',msg.value)
  typing.style.display='block'
  const r=await fetch('/demo',{method:'POST'})
  const d=await r.json()
  typing.style.display='none'
  bubble('bot',d.text)
  showToast('Sample data added to your session')
  setTimeout(()=>{msg.value="Advice";sendBtn.click()},380)
}
resetBtn.onclick=async()=>{await fetch('/reset',{method:'POST'});chat.innerHTML='';showToast('Session cleared');init()}
init()
</script>
</body>
</html>
"""

WELCOME = "Hi! I’m **FinCoach**. I’ll ask a few quick questions to tailor advice in **₹**. You can tap **Use Sample Data** for an instant demo. Shall we begin?"

@app.route("/")
def index():
    ensure_sid()
    return render_template_string(INDEX_HTML)

@app.route("/init")
def init():
    sid = ensure_sid()
    if not get_history(sid):
        set_history(sid,"assistant",WELCOME)
    return jsonify({"text": WELCOME})

@app.route("/reset", methods=["POST"])
def reset():
    sid = ensure_sid()
    clear_state(sid)
    return jsonify({"ok": True})

@app.route("/demo", methods=["POST"])
def demo():
    sid = ensure_sid()
    tool_load_demo_data(sid)
    set_history(sid,"assistant","Sample data loaded ✅. I can analyze it now.")
    return jsonify({"text":"Sample data loaded ✅. I’ll run an analysis next. Type **Advice** or **Set weekly caps**."})

@app.route("/chat", methods=["POST"])
def chat_route():
    sid = ensure_sid()
    data = request.get_json(silent=True) or {}
    user_text = (data.get("text") or "hi").strip()
    if re.search(r"\bsample\b", user_text.lower()):
        tool_load_demo_data(sid)
    set_history(sid,"user",user_text)
    if not openai.api_key:
        reply = "LLM is disabled (missing OPENAI_API_KEY). Use **Sample Data**, then type **Advice** for a fixed analysis."
        set_history(sid,"assistant",reply)
        return jsonify({"text": reply})
    msgs = [{"role":"system","content": SYSTEM_PROMPT}]
    for m in get_history(sid,80): msgs.append(m)
    msgs.append({"role":"user","content": user_text})
    for _ in range(10):
        resp = openai.chat.completions.create(model="gpt-4o-mini", messages=msgs, tools=TOOLS, temperature=0.2)
        msg = resp.choices[0].message
        if msg.tool_calls:
            for tc in msg.tool_calls:
                name = tc.function.name
                args = json.loads(tc.function.arguments or "{}")
                if name=="get_state": out = tool_get_state(sid)
                elif name=="set_profile_field": out = tool_set_profile_field(sid, args.get("field",""), args.get("value"))
                elif name=="set_cap": out = tool_set_cap(sid, args.get("category",""), args.get("weekly",0))
                elif name=="set_caps_bulk": out = tool_set_caps_bulk(sid, args.get("items",[]))
                elif name=="list_caps": out = tool_list_caps(sid)
                elif name=="analyze": out = tool_analyze(sid)
                elif name=="reset_state": out = tool_reset_state(sid)
                elif name=="load_demo_data": out = tool_load_demo_data(sid)
                else: out = {"error":"unknown tool"}
                msgs.append({"role":"assistant","tool_calls":[tc]})
                msgs.append({"role":"tool","tool_call_id": tc.id, "content": json.dumps(out)})
            continue
        out_text = (msg.content or "…").replace("₦","₹")
        set_history(sid,"assistant",out_text)
        return jsonify({"text": out_text})
    out_text = "Updated. Ask for **Advice** or say **Start** to continue."
    set_history(sid,"assistant",out_text)
    return jsonify({"text": out_text})

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
