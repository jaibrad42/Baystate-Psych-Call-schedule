"""
Baystate Psychiatry Call Scheduler — Streamlit Web App
Deploy free at: https://streamlit.io/cloud
"""

import streamlit as st
import json, os, re, calendar, io
from datetime import date, timedelta
from copy import deepcopy

st.set_page_config(
    page_title="Baystate Call Scheduler",
    page_icon="📅",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─────────────────────────────────────────────────────────────
# CONFIG — stored in st.session_state so it persists across
# reruns within a session. On Streamlit Cloud the user can
# download/upload config.json to persist across sessions.
# ─────────────────────────────────────────────────────────────

DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")

def _load_default_config():
    if os.path.exists(DEFAULT_CONFIG_PATH):
        with open(DEFAULT_CONFIG_PATH) as f:
            return json.load(f)
    return {"residents": [], "holidays": [], "program_no_call": [], "no_call_requests": []}

def get_cfg():
    if "config" not in st.session_state:
        st.session_state.config = _load_default_config()
    return st.session_state.config

def save_cfg(cfg):
    st.session_state.config = cfg

# ─────────────────────────────────────────────────────────────
# RESIDENT HELPERS
# ─────────────────────────────────────────────────────────────
BLOCKED_ROTATIONS = {"Medicine Wards", "Emergency Medicine", "Neurology"}

def active_residents(cfg):
    return [r for r in cfg["residents"] if r.get("active", True)]

def res_by_id(cfg):
    return {r["id"]: r for r in cfg["residents"]}

def pgy_pools(cfg):
    rs = active_residents(cfg)
    return {
        "interns":  [r["id"] for r in rs if r["pgy"] == 1],
        "pgy34":    [r["id"] for r in rs if r["pgy"] >= 3],
        "upper":    [r["id"] for r in rs if r["pgy"] >= 2],
        "all":      [r["id"] for r in rs],
    }

def make_res_id(full_name):
    parts = full_name.lower().split()
    slug = "_".join(reversed(parts))
    return re.sub(r"[^a-z0-9_]", "", slug)[:32]

# ─────────────────────────────────────────────────────────────
# SCHEDULING LOGIC  (ported from scheduler_app.py)
# ─────────────────────────────────────────────────────────────
def is_away(res_id, d, cfg):
    for r in cfg["residents"]:
        if r["id"] != res_id: continue
        for period in r.get("away_periods", []):
            try:
                if date.fromisoformat(period["start"]) <= d <= date.fromisoformat(period["end"]):
                    return d.weekday() < 5
            except (KeyError, ValueError): pass
    return False

def pgy1_blocked(res_id, d, cfg):
    for r in cfg["residents"]:
        if r["id"] != res_id: continue
        for block in r.get("blocked_rotations", []):
            try:
                if date.fromisoformat(block["start"]) <= d <= date.fromisoformat(block["end"]):
                    return block.get("name", "") in BLOCKED_ROTATIONS
            except (KeyError, ValueError): pass
    return False

def get_holiday(d, cfg):
    dk = d.isoformat()
    for h in cfg["holidays"]:
        for entry in h["entries"]:
            if entry["date"] == dk:
                return {"name": h["name"], "aptu": entry.get("aptu",""), "consult": entry.get("consult","")}
    return None

def holiday_soon(res_id, d, cfg, within=3):
    for i in range(1, within+1):
        h = get_holiday(d + timedelta(days=i), cfg)
        if h and (h["aptu"] == res_id or h["consult"] == res_id):
            return True
    return False

def is_no_call(res_id, d, cfg):
    dk = d.isoformat()
    return any(e["resident"] == res_id and e["date"] == dk
               for e in cfg.get("no_call_requests", []))

def is_program_nc(d, cfg):
    return d.isoformat() in cfg.get("program_no_call", [])

def intern_limits(month, year):
    am = (year - 2026) * 12 + (month - 7) + 1
    am = max(am, 1)
    wd = 2
    we = 0 if am == 1 else (1 if am <= 3 else 2)
    solo_we = am >= 2
    return wd, we, solo_we

SOFT_CAPS = {1: 2, 2: 5, 3: 3, 4: 2}
HARD_CAPS = {1: 3, 2: 6, 3: 5, 4: 4}

class State:
    def __init__(self, cfg):
        rs = active_residents(cfg)
        self.last  = {r["id"]: None for r in rs}
        self.month = {r["id"]: 0    for r in rs}
        self.wf    = {r["id"]: 0    for r in rs}
        self.iwd   = {r["id"]: 0    for r in rs if r["pgy"] == 1}
        self.we_sats = {r["id"]: set() for r in rs}
        self._current_month = None

    def reset_month_counters(self, year, month):
        if self._current_month != (year, month):
            for k in self.iwd: self.iwd[k] = 0
            self._current_month = (year, month)

    def eligible(self, res_id, d, role, cfg):
        if is_away(res_id, d, cfg): return False
        if is_no_call(res_id, d, cfg): return False
        if is_program_nc(d, cfg): return False
        if holiday_soon(res_id, d, cfg): return False
        rb = res_by_id(cfg)
        if res_id not in rb: return False
        p = rb[res_id]["pgy"]
        if role == "aptu_wd":      return p >= 2
        if role == "aptu_we_jul":  return p >= 2 and not self._over_consec(res_id, d)
        if role == "aptu_we_aug":
            if p == 1 and pgy1_blocked(res_id, d, cfg): return False
            return not self._over_consec(res_id, d)
        if role == "consult":
            return p >= 3 and not self._over_consec(res_id, d)
        if role == "intern":
            return p == 1 and not pgy1_blocked(res_id, d, cfg)
        return False

    def _over_consec(self, res_id, d):
        sat = d if d.weekday() == 5 else d - timedelta(days=(d.weekday()-5)%7)
        w = self.we_sats[res_id]
        return (sat-timedelta(7)) in w and (sat-timedelta(14)) in w and (sat-timedelta(21)) in w

    def score(self, res_id, d, role, cfg):
        last = self.last[res_id]
        gap  = (d - last).days if last else 99
        if gap < 2: return 1e9
        q = 0 if gap >= 4 else (100 if gap == 3 else 500)
        rb = res_by_id(cfg)
        pgy = rb[res_id]["pgy"] if res_id in rb else 2
        cap = SOFT_CAPS.get(pgy, 5)
        cnt_pen = self.month[res_id] * (100 / max(cap, 1))
        wf_pen  = self.wf[res_id] * 12 if role == "aptu_wd" and d.weekday() in (2,4) else 0
        return q + cnt_pen + wf_pen - pgy * 0.5

    def best(self, pool, d, role, cfg, exclude=()):
        cands = [r for r in pool if r not in exclude and self.eligible(r, d, role, cfg)]
        if not cands: return None
        cands.sort(key=lambda r: self.score(r, d, role, cfg))
        return cands[0] if self.score(cands[0], d, role, cfg) < 1e8 else None

    def fallback(self, pool, d, role, cfg, exclude=()):
        cands = [r for r in pool if r not in exclude and self.eligible(r, d, role, cfg)
                 and (self.last[r] is None or (d - self.last[r]).days >= 2)]
        if cands:
            cands.sort(key=lambda r: -(d-self.last[r]).days if self.last[r] else -99)
            return cands[0]
        cands2 = [r for r in pool if r not in exclude and self.eligible(r, d, role, cfg)
                  and (self.last[r] is None or (d - self.last[r]).days >= 1)]
        if cands2:
            cands2.sort(key=lambda r: self.month[r])
            return cands2[0]
        return None

    def record(self, res_id, d, role):
        if res_id not in self.last: return
        self.last[res_id] = d
        self.month[res_id] += 1
        if role == "aptu_wd" and d.weekday() in (2,4):
            self.wf[res_id] += 1
        if d.weekday() >= 5 and role in ("aptu_we_jul","aptu_we_aug","consult"):
            sat = d if d.weekday()==5 else d - timedelta(days=(d.weekday()-5)%7)
            self.we_sats[res_id].add(sat)
        if res_id in self.iwd and role == "intern_wd":
            self.iwd[res_id] += 1


def schedule_month(year, month, state, cfg):
    pools = pgy_pools(cfg)
    first = date(year, month, 1)
    last  = date(year, month+1, 1) - timedelta(1) if month < 12 else date(year+1,1,1) - timedelta(1)
    days  = [first + timedelta(i) for i in range((last-first).days+1)]
    iwd_lim, iwe_lim, solo_we = intern_limits(month, year)
    state.reset_month_counters(year, month)

    for d in days:
        h = get_holiday(d, cfg)
        if h:
            if h["aptu"]:    state.month[h["aptu"]]    = state.month.get(h["aptu"],0)    + 1
            if h["consult"]: state.month[h["consult"]] = state.month.get(h["consult"],0) + 1

    sched = {}; warnings = []; prev_intern = None

    for d in days:
        dk = d.isoformat(); dow = d.weekday(); is_wk = dow <= 4

        if is_program_nc(d, cfg):
            sched[dk] = {"type": "no_call"}; continue

        h = get_holiday(d, cfg)
        if h:
            entry_h = {"type":"holiday","name":h["name"]}
            if h["aptu"]:    entry_h["aptu"]    = h["aptu"]
            if h["consult"]: entry_h["consult"] = h["consult"]
            sched[dk] = entry_h
            if h["aptu"]:    state.record(h["aptu"],    d, "aptu_wd" if is_wk else "aptu_we_jul")
            if h["consult"]: state.record(h["consult"], d, "consult")
            prev_intern = None; continue

        entry = {"type": "weekday" if is_wk else "weekend"}

        if not is_wk:
            aptu_role = "aptu_we_aug" if solo_we else "aptu_we_jul"
            aptu_pool = pools["all"] if solo_we else pools["upper"]
            con = state.best(pools["pgy34"], d, "consult", cfg) or \
                  state.fallback(pools["pgy34"], d, "consult", cfg)
            if con is None:
                warnings.append(f"{dk}: NO eligible Consult — UNCOVERED")
            elif state.last.get(con) and (d - state.last[con]).days < 2:
                warnings.append(f"{dk}: Consult {con} back-to-back — needs review")
            aptu = state.best([r for r in aptu_pool if r != con], d, aptu_role, cfg) or \
                   state.fallback([r for r in aptu_pool if r != con], d, aptu_role, cfg)
            if aptu is None:
                warnings.append(f"{dk}: NO eligible APTU — UNCOVERED")
            elif state.last.get(aptu) and (d - state.last[aptu]).days < 2:
                warnings.append(f"{dk}: APTU {aptu} back-to-back — needs review")
            if con:  entry["consult"] = con;  state.record(con,  d, "consult")
            if aptu: entry["aptu"]    = aptu; state.record(aptu, d, aptu_role)
            prev_intern = None

        else:
            aptu = state.best(pools["upper"], d, "aptu_wd", cfg) or \
                   state.fallback(pools["upper"], d, "aptu_wd", cfg)
            if aptu is None:
                warnings.append(f"{dk}: NO eligible APTU — UNCOVERED")
            else:
                gap = (d - state.last[aptu]).days if state.last.get(aptu) else 99
                if gap == 2:
                    warnings.append(f"{dk}: APTU {aptu} at q3 — needs review")
            intern = None
            rb = res_by_id(cfg)
            if aptu and rb.get(aptu,{}).get("pgy",0) >= 3 and iwd_lim > 0:
                for r in pools["interns"]:
                    if state.iwd.get(r,0) < iwd_lim and state.eligible(r, d, "intern", cfg) and r != prev_intern:
                        intern = r; break
            if aptu:   entry["aptu"]   = aptu;   state.record(aptu, d, "aptu_wd")
            if intern: entry["intern"] = intern; state.record(intern, d, "intern_wd")
            prev_intern = intern

        sched[dk] = entry
    return sched, warnings


def schedule_jeopardy(sched, cfg):
    rb = res_by_id(cfg)
    for dk, entry in sched.items():
        if entry.get("type") == "no_call": continue
        d = date.fromisoformat(dk)
        assigned = {entry.get(k) for k in ("aptu","consult","intern") if entry.get(k)}
        cands = [r["id"] for r in active_residents(cfg)
                 if r["pgy"] >= 3 and r["id"] not in assigned
                 and not is_away(r["id"], d, cfg) and not is_no_call(r["id"], d, cfg)]
        if cands:
            def month_load(rid):
                return sum(1 for dk2,e2 in sched.items()
                           if date.fromisoformat(dk2).month == d.month
                           and e2.get("jeopardy") == rid)
            cands.sort(key=lambda r: (month_load(r), r))
            entry["jeopardy"] = cands[0]
        else:
            entry["jeopardy"] = "UNCOVERED"
    return sched


# ─────────────────────────────────────────────────────────────
# EXCEL EXPORT
# ─────────────────────────────────────────────────────────────
def export_xlsx_bytes(all_scheds, all_warnings, cfg):
    from openpyxl import Workbook
    from openpyxl.styles import PatternFill, Font, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    import copy

    wb = Workbook()
    wb.remove(wb.active)
    SHORT = {r["id"]: r["full"] for r in cfg["residents"]}

    C_HDR="1E3A8A"; C_WKND="EDE9FE"; C_HOL="FEE2E2"; C_GRN="D1FAE5"
    C_WF="DBEAFE"; C_UNC="FCA5A5"; C_AMBI="FEF9C3"; C_WHT="FFFFFF"
    C_SAT="F3F0FF"; C_SUN="EDE9FE"; C_DAYN="F8FAFC"; DAYS_WIDE=24

    def fill(h): return PatternFill("solid", fgColor=h)
    def font(bold=False, sz=10, color="111827", italic=False):
        return Font(name="Arial", bold=bold, size=sz, color=color, italic=italic)
    def border(style="thin", color="D1D5DB"):
        s=Side(style=style,color=color); return Border(left=s,right=s,top=s,bottom=s)
    def align(h="center", v="center", wrap=False):
        return Alignment(horizontal=h, vertical=v, wrap_text=wrap)

    ROWS_PER_WEEK=5; ROW_H_DAY=15; ROW_H_ROLE=18

    combined = {r["id"]:{"total":0,"wk":0,"we":0,"wf":0} for r in cfg["residents"]}

    for (year, month), sched in all_scheds.items():
        warnings = all_warnings.get((year,month),[])
        mlabel   = date(year,month,1).strftime("%b %Y")
        ws = wb.create_sheet(mlabel)
        ws.merge_cells("A1:G1")
        ws["A1"].value = date(year,month,1).strftime("%B %Y").upper() + \
                         "  —  BAYSTATE PSYCHIATRY CALL SCHEDULE"
        ws["A1"].font = font(bold=True,sz=13,color="FFFFFF")
        ws["A1"].fill = fill(C_HDR); ws["A1"].alignment = align()
        ws.row_dimensions[1].height = 26

        for col, day in enumerate(["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"],1):
            c = ws.cell(2,col,day)
            c.font=font(bold=True,sz=11,color="FFFFFF"); c.fill=fill(C_HDR)
            c.alignment=align(); c.border=border()
            ws.column_dimensions[get_column_letter(col)].width = DAYS_WIDE
        ws.row_dimensions[2].height = 20

        first_day  = date(year,month,1); start_col = first_day.weekday()
        num_days   = calendar.monthrange(year,month)[1]; current_row = 3
        total_weeks = ((num_days+start_col-1)//7)+1

        for week_i in range(total_weeks):
            r0 = current_row + week_i * ROWS_PER_WEEK
            ws.row_dimensions[r0].height = ROW_H_DAY
            for rr in range(1,ROWS_PER_WEEK): ws.row_dimensions[r0+rr].height = ROW_H_ROLE

        for day_num in range(1, num_days+1):
            d=date(year,month,day_num); dow=d.weekday(); col=dow+1
            slot=day_num-1+start_col; week_i=slot//7; r0=current_row+week_i*ROWS_PER_WEEK
            e=sched.get(d.isoformat(),{}); t=e.get("type",""); wf=dow in (2,4) and t=="weekday"

            if   t=="holiday":   bg=C_HOL
            elif t=="no_call":   bg=C_AMBI
            elif dow==5:         bg=C_SAT
            elif dow==6:         bg=C_SUN
            elif wf:             bg=C_WF
            else:                bg=C_WHT

            dn=ws.cell(r0,col); dn.value=day_num
            dn.font=font(bold=True,sz=11,color="374151")
            dn.fill=fill(C_DAYN if bg==C_WHT else bg)
            dn.alignment=align(h="right"); dn.border=border()
            if t=="holiday":
                dn.value=f"{day_num}  🔒 {e.get('name','')}"; dn.font=font(bold=True,sz=9,color="991B1B")
            elif t=="no_call":
                dn.value=f"{day_num}  — No call"; dn.font=font(bold=True,sz=9,color="92400E",italic=True)

            roles_map={"weekday":[("aptu","APTU"),("consult",""),("intern","Intern")],
                       "weekend":[("aptu","APTU"),("consult","Consult"),("","")],
                       "holiday":[("aptu","APTU"),("consult","Consult"),("","")],
                       "no_call":[("",""),("",""),("","")]}
            roles=roles_map.get(t,[("",""),("",""),("","")])
            for ri,(key,label) in enumerate(roles):
                rc=r0+1+ri; rid=e.get(key,"") if key else ""; name=SHORT.get(rid,rid) if rid else ""
                cell=ws.cell(rc,col); cell.border=border(); cell.alignment=align(h="left",wrap=True)
                if not label or (t=="weekday" and ri==1):
                    cell.fill=fill(bg); cell.value=""
                elif name=="" and ri<2 and t not in ("no_call",):
                    cell.value="UNCOVERED"; cell.font=font(bold=True,sz=9,color="7F1D1D"); cell.fill=fill(C_UNC)
                else:
                    cell.value=f"  {name}" if name else ""
                    if ri==2 and name:
                        cell.fill=fill(C_GRN); cell.font=font(sz=9,color="065F46")
                    else:
                        cell.fill=fill(bg); cell.font=font(sz=9,color="1E3A8A" if name else "9CA3AF",bold=bool(name))

            jrc=r0+4; jname=SHORT.get(e.get("jeopardy",""),e.get("jeopardy",""))
            jcell=ws.cell(jrc,col); jcell.border=border(); jcell.alignment=align(h="left",wrap=True)
            if t=="no_call" or not e.get("jeopardy"):
                jcell.fill=fill(bg); jcell.value=""
            elif jname=="UNCOVERED":
                jcell.value="  Jep: UNCOVERED"; jcell.fill=fill(C_UNC); jcell.font=font(bold=True,sz=9,color="7F1D1D")
            else:
                jcell.value=f"  Jep: {jname}"; jcell.fill=PatternFill("solid",fgColor="FFF7CD")
                jcell.font=font(sz=9,color="854D0E",bold=True)

        for week_i in range(total_weeks):
            r0=current_row+week_i*ROWS_PER_WEEK
            for col in range(1,8):
                for rr in range(ROWS_PER_WEEK):
                    cell=ws.cell(r0+rr,col)
                    if cell.value is None:
                        cell.fill=fill("F8FAFC"); cell.border=border(color="E2E8F0")

        # Warnings sheet
        ws_w=wb.create_sheet(f"{mlabel[:3]} Warnings")
        ws_w.column_dimensions["A"].width=80
        ws_w.cell(1,1,f"Warnings — {mlabel}").font=font(bold=True,sz=12)
        if not warnings: ws_w.cell(2,1,"✓  No constraint violations.").font=font(sz=11,color="065F46")
        else:
            for i,w in enumerate(warnings,2):
                ws_w.cell(i,1,f"•  {w}").font=font(sz=10,color="7F1D1D" if "UNCOVERED" in w or "back-to-back" in w else "92400E")

        # Accumulate
        for dk,ev in sched.items():
            d=date.fromisoformat(dk)
            if d.month!=month: continue
            is_we=d.weekday()>=5 or ev.get("type")=="holiday"
            for role in ["aptu","consult","intern"]:
                rid=ev.get(role)
                if rid and rid in combined:
                    combined[rid]["total"]+=1
                    if is_we: combined[rid]["we"]+=1
                    else: combined[rid]["wk"]+=1
            aptu=ev.get("aptu")
            if aptu and not is_we and d.weekday() in (2,4): combined[aptu]["wf"]+=1

    # All Counts sheet
    ws_c=wb.create_sheet("All Counts")
    hdrs=["Resident","PGY","Total Calls","Weekday","Weekend","Wed/Fri APTU"]
    for c,h in enumerate(hdrs,1):
        cell=ws_c.cell(1,c,h); cell.fill=fill(C_HDR)
        cell.font=font(bold=True,sz=11,color="FFFFFF"); cell.alignment=align(); cell.border=border()
    ws_c.column_dimensions["A"].width=24
    for col in "BCDEF": ws_c.column_dimensions[col].width=14
    pgy_fill={1:"FEF3C7",2:"DBEAFE",3:"EDE9FE",4:"D1FAE5"}
    for i,r in enumerate(cfg["residents"],2):
        rf=fill(pgy_fill.get(r["pgy"],"FFFFFF"))
        for c,val in enumerate([r["full"],f"PGY-{r['pgy']}",
                                 combined[r["id"]]["total"],combined[r["id"]]["wk"],
                                 combined[r["id"]]["we"],combined[r["id"]]["wf"]],1):
            cell=ws_c.cell(i,c,val); cell.fill=copy.copy(rf); cell.font=font(sz=10)
            cell.alignment=align(h="left" if c==1 else "center"); cell.border=border()

    # Move All Counts to front
    wb.move_sheet("All Counts", offset=-len(wb.sheetnames)+1)

    buf = io.BytesIO(); wb.save(buf); buf.seek(0)
    return buf.getvalue()


# ─────────────────────────────────────────────────────────────
# CSS
# ─────────────────────────────────────────────────────────────
st.markdown("""
<style>
[data-testid="stSidebar"] { background: #1C1C1E; }
[data-testid="stSidebar"] * { color: #F2F2F7 !important; }
.stTabs [data-baseweb="tab-list"] { gap: 4px; }
.stTabs [data-baseweb="tab"] { background: #2C2C2E; border-radius: 6px; padding: 6px 16px; color: #8E8E93; }
.stTabs [aria-selected="true"] { background: #3B82F6 !important; color: white !important; }
.cal-grid { display: grid; grid-template-columns: repeat(7, 1fr); gap: 3px; margin-top: 8px; }
.cal-header-row { display: grid; grid-template-columns: repeat(7, 1fr); gap: 3px; margin-top: 12px; }
.dow-hdr { text-align: center; font-size: 11px; font-weight: 600; color: #8E8E93; padding: 4px; }
.cal-cell { background: #2C2C2E; border-radius: 6px; padding: 6px 8px; min-height: 78px; font-size: 11px; }
.cal-cell.weekend { background: #26263A; }
.cal-cell.holiday { background: #3B1515; border: 1px solid #7F1D1D; }
.cal-cell.no-call  { background: #2A2A1A; }
.cal-cell.wf       { border-left: 2px solid #3B82F6; }
.cal-cell.empty    { background: transparent; min-height: 0; }
.day-num { font-weight: 700; color: #8E8E93; margin-bottom: 3px; }
.pill { display: inline-block; padding: 1px 6px; border-radius: 3px; font-size: 10px; font-weight: 500; margin: 1px 0; width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.pill-ul      { background: #1e3a8a33; color: #93C5FD; }
.pill-aptu    { background: #3d247333; color: #C4B5FD; }
.pill-consult { background: #06574633; color: #6EE7B7; }
.pill-intern  { background: #78350f33; color: #FCD34D; }
.pill-hol     { background: #7f1d1d55; color: #FCA5A5; }
.pill-jep     { background: #78350f22; color: #FCD34D; font-style: italic; }
.pill-uncov   { background: #7f1d1d; color: white; font-weight: 700; }
.role-lbl     { font-size: 9px; color: #555; margin-bottom: 1px; }
.warn-hard    { background: #3B1515; border-left: 3px solid #EF4444; padding: 6px 10px; border-radius: 4px; font-family: monospace; font-size: 12px; margin-bottom: 4px; color: #FCA5A5; }
.warn-soft    { background: #2A1F0A; border-left: 3px solid #F59E0B; padding: 6px 10px; border-radius: 4px; font-family: monospace; font-size: 12px; margin-bottom: 4px; color: #FCD34D; }
.warn-ok      { background: #0A2A1A; border-left: 3px solid #10B981; padding: 6px 10px; border-radius: 4px; font-size: 12px; color: #6EE7B7; }
</style>
""", unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────
# SIDEBAR
# ─────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## 📅 Baystate Scheduler")
    st.markdown("---")

    # Config upload/download
    with st.expander("💾 Config file", expanded=False):
        cfg_bytes = json.dumps(get_cfg(), indent=2).encode()
        st.download_button("⬇ Download config.json", cfg_bytes,
                           file_name="config.json", mime="application/json",
                           use_container_width=True)
        up = st.file_uploader("⬆ Upload config.json", type="json", label_visibility="collapsed")
        if up:
            try:
                new_cfg = json.load(up)
                save_cfg(new_cfg)
                st.success("Config loaded!")
                st.rerun()
            except Exception as e:
                st.error(f"Invalid JSON: {e}")

    st.markdown("---")
    st.markdown("**Generate schedule**")
    MONTH_MAP = {"July":7,"August":8,"September":9,"October":10,
                 "November":11,"December":12,"January":1,"February":2,
                 "March":3,"April":4,"May":5,"June":6}
    sel_month  = st.selectbox("Starting month", list(MONTH_MAP.keys()), index=0)
    sel_year   = st.selectbox("Year", [2026, 2027], index=0)
    sel_n      = st.selectbox("# of months", [1,2,3,4,5,6,12], index=5)

    if st.button("▶  Generate Schedule", type="primary", use_container_width=True):
        cfg = get_cfg()
        if not active_residents(cfg):
            st.error("No active residents in config.")
        else:
            with st.spinner("Generating…"):
                state = State(cfg)
                all_scheds = {}; all_warns = {}
                y, m = sel_year, MONTH_MAP[sel_month]
                for _ in range(sel_n):
                    sched, warns = schedule_month(y, m, state, cfg)
                    schedule_jeopardy(sched, cfg)
                    all_scheds[(y,m)] = sched; all_warns[(y,m)] = warns
                    m += 1
                    if m > 12: m = 1; y += 1
                st.session_state.all_scheds = all_scheds
                st.session_state.all_warns  = all_warns
                total_warns = sum(len(v) for v in all_warns.values())
                if total_warns:
                    st.warning(f"{total_warns} warning(s) — check Warnings tab")
                else:
                    st.success("Done! No warnings.")

    if "all_scheds" in st.session_state and st.session_state.all_scheds:
        cfg = get_cfg()
        xlsx = export_xlsx_bytes(st.session_state.all_scheds, st.session_state.all_warns, cfg)
        months_list = sorted(st.session_state.all_scheds.keys())
        label = date(months_list[0][0],months_list[0][1],1).strftime("%b%Y")
        if len(months_list) > 1:
            label += f"_to_{date(months_list[-1][0],months_list[-1][1],1).strftime('%b%Y')}"
        st.download_button("⬇ Download Excel", xlsx,
                           file_name=f"Baystate_Schedule_{label}.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                           use_container_width=True)

    if "all_scheds" in st.session_state:
        if st.button("🗑 Clear schedule", use_container_width=True):
            del st.session_state["all_scheds"]
            del st.session_state["all_warns"]
            st.rerun()


# ─────────────────────────────────────────────────────────────
# MAIN TABS
# ─────────────────────────────────────────────────────────────
tab_cal, tab_res, tab_hol, tab_nc, tab_pnc = st.tabs([
    "📅 Schedule", "👥 Residents", "🔒 Holidays", "🚫 No-Call Requests", "📵 Program No-Call"
])

# ─── helpers ────────────────────────────────────────────────
def res_display_name(res_id, cfg):
    rb = res_by_id(cfg)
    return rb[res_id]["full"] if res_id in rb else res_id

def render_calendar(sched, cfg, year, month):
    rb = res_by_id(cfg)
    def rname(rid): return rb[rid]["full"].split()[-1] if rid in rb else rid

    first_dow = date(year,month,1).weekday()
    num_days  = calendar.monthrange(year,month)[1]

    html = '<div class="cal-header-row">'
    for d in ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]:
        html += f'<div class="dow-hdr">{d}</div>'
    html += '</div><div class="cal-grid">'

    for slot in range(42):
        day_num = slot - first_dow + 1
        if day_num < 1 or day_num > num_days:
            html += '<div class="cal-cell empty"></div>'
            continue
        d = date(year,month,day_num); dk = d.isoformat()
        e = sched.get(dk, {}); t = e.get("type","")
        dow = d.weekday()
        is_wknd = dow >= 5; is_hol = t=="holiday"; is_nc = t=="no_call"
        is_wf   = dow in (2,4) and t=="weekday"

        cls = "cal-cell"
        if is_hol:   cls += " holiday"
        elif is_nc:  cls += " no-call"
        elif is_wknd:cls += " weekend"
        if is_wf:    cls += " wf"

        dow_abbr = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"][dow]
        html += f'<div class="{cls}"><div class="day-num">{day_num} <span style="font-weight:400;font-size:9px;color:#555">{dow_abbr}</span>'
        if is_hol: html += f' <span style="font-size:9px;color:#FCA5A5">🔒</span>'
        if is_wf:  html += f' <span style="font-size:9px;color:#93C5FD">★</span>'
        html += '</div>'

        if is_nc:
            html += '<div style="font-size:10px;color:#8E8E93;font-style:italic">no call</div>'
        elif is_hol:
            aptu = rname(e.get("aptu","")) if e.get("aptu") else "—"
            con  = rname(e.get("consult","")) if e.get("consult") else "—"
            html += f'<div class="pill pill-hol">A: {aptu}</div>'
            html += f'<div class="pill pill-hol">C: {con}</div>'
        elif t == "weekend":
            aptu = e.get("aptu"); con = e.get("consult")
            html += f'<div class="pill {"pill-aptu" if aptu else "pill-uncov"}">'
            html += f'{"A: "+rname(aptu) if aptu else "UNCOV"}</div>'
            html += f'<div class="pill {"pill-consult" if con else "pill-uncov"}">'
            html += f'{"C: "+rname(con) if con else "UNCOV"}</div>'
            if e.get("intern"):
                html += f'<div class="pill pill-intern">I: {rname(e["intern"])}</div>'
            if e.get("jeopardy"):
                html += f'<div class="pill pill-jep">J: {rname(e["jeopardy"])}</div>'
        elif t == "weekday":
            aptu = e.get("aptu")
            html += f'<div class="pill {"pill-ul" if aptu else "pill-uncov"}">'
            html += f'{"UL: "+rname(aptu) if aptu else "UNCOV"}</div>'
            if e.get("intern"):
                html += f'<div class="pill pill-intern">I: {rname(e["intern"])}</div>'
            if e.get("jeopardy"):
                html += f'<div class="pill pill-jep">J: {rname(e["jeopardy"])}</div>'
        html += '</div>'
    html += '</div>'
    return html


# ─── Tab 1: Schedule ─────────────────────────────────────────
with tab_cal:
    if "all_scheds" not in st.session_state or not st.session_state.all_scheds:
        st.info("No schedule generated yet. Use the sidebar to generate one.")
    else:
        cfg = get_cfg()
        months = sorted(st.session_state.all_scheds.keys())
        month_labels = [date(y,m,1).strftime("%B %Y") for y,m in months]
        sel_idx = st.selectbox("Month", range(len(months)), format_func=lambda i: month_labels[i])
        year, month = months[sel_idx]
        sched = st.session_state.all_scheds[(year,month)]

        st.markdown(f"### {month_labels[sel_idx]}")

        # Legend
        cols = st.columns(6)
        legend = [("pill-ul","Weekday UL"),("pill-aptu","Wknd APTU"),
                  ("pill-consult","Consult"),("pill-intern","Intern"),
                  ("pill-hol","Holiday"),("pill-jep","Jeopardy")]
        for col,(cls,lbl) in zip(cols,legend):
            col.markdown(f'<span class="pill {cls}">{lbl}</span>', unsafe_allow_html=True)

        st.markdown(render_calendar(sched, cfg, year, month), unsafe_allow_html=True)

        # Warnings for this month
        warns = st.session_state.all_warns.get((year,month),[])
        if warns:
            st.markdown("---")
            st.markdown("**⚠ Warnings for this month**")
            for w in warns:
                cls = "warn-hard" if ("UNCOVERED" in w or "back-to-back" in w) else "warn-soft"
                st.markdown(f'<div class="{cls}">{w}</div>', unsafe_allow_html=True)
        else:
            st.markdown('<div class="warn-ok">✓ No warnings for this month</div>', unsafe_allow_html=True)


# ─── Tab 2: Residents ─────────────────────────────────────────
with tab_res:
    cfg = get_cfg()
    st.markdown("### Resident Roster")

    col1, col2, col3, col4 = st.columns([2,1,1,2])
    with col1:
        if st.button("➕ Add Resident"):
            st.session_state.res_action = "add"
            st.session_state.res_edit_id = None
    with col4:
        if st.button("🎓 Progress Year (July)", help="Advance all PGY levels by 1. PGY-4s become inactive."):
            cfg2 = get_cfg()
            graduated = []
            for r in cfg2["residents"]:
                if not r.get("active",True): continue
                if r["pgy"] >= 4: r["active"]=False; graduated.append(r["full"])
                else: r["pgy"] += 1
            save_cfg(cfg2)
            st.success(f"Year progressed! Graduated: {', '.join(graduated) if graduated else 'none'}")
            st.rerun()

    # Resident table
    rows = sorted(active_residents(cfg) + [r for r in cfg["residents"] if not r.get("active",True)],
                  key=lambda r:(r["pgy"], r["full"]))
    for r in rows:
        active = r.get("active",True)
        away = ", ".join(f"{p['start']} – {p['end']}" for p in r.get("away_periods",[]))
        blocks = ", ".join(f"{b['name']} ({b['start'][:7]})" for b in r.get("blocked_rotations",[]))
        opacity = "1.0" if active else "0.45"
        with st.container():
            c1,c2,c3,c4,c5,c6 = st.columns([3,1,1,2,3,1])
            c1.markdown(f'<span style="opacity:{opacity};font-weight:500">{r["full"]}</span>',
                       unsafe_allow_html=True)
            c2.write(f"PGY-{r['pgy']}")
            c3.write("✅" if active else "⏸")
            c4.write(away or "—")
            c5.write(blocks or "—")
            if c6.button("✎", key=f"edit_{r['id']}"):
                st.session_state.res_action = "edit"
                st.session_state.res_edit_id = r["id"]

    # Add/Edit form
    action = st.session_state.get("res_action")
    if action in ("add","edit"):
        edit_id = st.session_state.get("res_edit_id")
        existing = next((r for r in cfg["residents"] if r["id"]==edit_id), None) if edit_id else None
        st.markdown("---")
        st.markdown(f"#### {'Add' if action=='add' else 'Edit'} Resident")

        with st.form("res_form"):
            full = st.text_input("Full Name", value="" if not existing else existing["full"])
            pgy  = st.selectbox("PGY Level", [1,2,3,4],
                                index=(existing["pgy"]-1 if existing else 0))
            active_sw = st.checkbox("Active", value=True if not existing else existing.get("active",True))

            st.markdown("**Away Periods** (weekdays blocked)")
            away_raw = st.text_area("One per line: YYYY-MM-DD to YYYY-MM-DD",
                value="\n".join(f"{p['start']} to {p['end']}" for p in (existing or {}).get("away_periods",[])),
                height=80)

            st.markdown("**Blocked Rotations** (Medicine Wards / Emergency Medicine / Neurology)")
            ROTATION_NAMES = ["Orientation","Medicine Wards","Emergency Medicine","Neurology",
                              "Emergency Psychiatry","Substance Use","Geriatric Psychiatry","Other"]
            blocks_raw = st.text_area("One per line: RotationName YYYY-MM-DD to YYYY-MM-DD",
                value="\n".join(f"{b['name']} {b['start']} to {b['end']}"
                                for b in (existing or {}).get("blocked_rotations",[])),
                height=80)

            col_s, col_c = st.columns(2)
            submitted = col_s.form_submit_button("Save", type="primary")
            cancelled = col_c.form_submit_button("Cancel")

        if cancelled:
            del st.session_state["res_action"]
            st.rerun()

        if submitted:
            if not full.strip():
                st.error("Enter a full name.")
            else:
                # Parse away periods
                away_data = []
                for line in away_raw.strip().split("\n"):
                    line=line.strip()
                    if not line: continue
                    m = re.match(r"(\d{4}-\d{2}-\d{2})\s+to\s+(\d{4}-\d{2}-\d{2})", line)
                    if m: away_data.append({"start":m.group(1),"end":m.group(2)})

                # Parse blocked rotations
                block_data = []
                for line in blocks_raw.strip().split("\n"):
                    line=line.strip()
                    if not line: continue
                    m = re.match(r"(.+?)\s+(\d{4}-\d{2}-\d{2})\s+to\s+(\d{4}-\d{2}-\d{2})", line)
                    if m: block_data.append({"name":m.group(1).strip(),"start":m.group(2),"end":m.group(3)})

                cfg2 = get_cfg()
                if action=="add":
                    new_id = make_res_id(full)
                    existing_ids = {r["id"] for r in cfg2["residents"]}
                    suffix=2; base=new_id
                    while new_id in existing_ids: new_id=f"{base}_{suffix}"; suffix+=1
                    cfg2["residents"].append({"id":new_id,"full":full.strip(),"pgy":pgy,
                                              "active":True,"away_periods":away_data,
                                              "blocked_rotations":block_data})
                else:
                    for r in cfg2["residents"]:
                        if r["id"]==edit_id:
                            r["full"]=full.strip(); r["pgy"]=pgy; r["active"]=active_sw
                            r["away_periods"]=away_data; r["blocked_rotations"]=block_data
                save_cfg(cfg2)
                del st.session_state["res_action"]
                st.rerun()


# ─── Tab 3: Holidays ─────────────────────────────────────────
with tab_hol:
    cfg = get_cfg()
    st.markdown("### Holiday Coverage")
    st.caption("Holidays are pre-assigned and locked into the generated schedule.")

    rb = res_by_id(cfg)
    all_names = [""] + [r["full"] for r in active_residents(cfg)]
    res_id_map = {r["full"]:r["id"] for r in cfg["residents"]}

    # Navigation
    if "hol_ym" not in st.session_state: st.session_state.hol_ym = (2026,7)
    hy, hm = st.session_state.hol_ym
    c1,c2,c3 = st.columns([1,3,1])
    if c1.button("◀", key="hol_prev"):
        hm-=1
        if hm<1: hm=12; hy-=1
        st.session_state.hol_ym=(hy,hm); st.rerun()
    c2.markdown(f"<h4 style='text-align:center'>{date(hy,hm,1).strftime('%B %Y')}</h4>", unsafe_allow_html=True)
    if c3.button("▶", key="hol_next"):
        hm+=1
        if hm>12: hm=1; hy+=1
        st.session_state.hol_ym=(hy,hm); st.rerun()

    # Build holiday lookup
    hol_lookup = {}
    for hi,hol in enumerate(cfg["holidays"]):
        for ei,entry in enumerate(hol["entries"]):
            hol_lookup[entry["date"]] = (hol["name"],entry,hi,ei)

    first_dow = date(hy,hm,1).weekday(); num_days = calendar.monthrange(hy,hm)[1]
    html = '<div class="cal-header-row">'
    for d in ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]:
        html += f'<div class="dow-hdr">{d}</div>'
    html += '</div><div class="cal-grid">'
    for slot in range(42):
        day_num = slot - first_dow + 1
        if day_num<1 or day_num>num_days: html+='<div class="cal-cell empty"></div>'; continue
        d=date(hy,hm,day_num); dk=d.isoformat()
        if dk in hol_lookup:
            hname,entry,_,_ = hol_lookup[dk]
            aptu_name = rb[entry.get("aptu","")]["full"].split()[-1] if entry.get("aptu","") in rb else "—"
            con_name  = rb[entry.get("consult","")]["full"].split()[-1] if entry.get("consult","") in rb else "—"
            html+=f'<div class="cal-cell holiday"><div class="day-num">{day_num} 🔒</div>'
            html+=f'<div style="font-size:9px;color:#FCA5A5;margin-bottom:2px">{hname[:14]}</div>'
            html+=f'<div class="pill pill-hol">A: {aptu_name}</div>'
            html+=f'<div class="pill pill-hol">C: {con_name}</div></div>'
        else:
            dow=d.weekday(); cls="cal-cell"+('' if dow<5 else ' weekend')
            html+=f'<div class="{cls}"><div class="day-num" style="color:#555">{day_num}</div>'
            html+=f'<div style="font-size:9px;color:#3A3A3C">+ assign</div></div>'
    html+='</div>'
    st.markdown(html, unsafe_allow_html=True)

    st.markdown("---")
    st.markdown("#### Assign / Edit Holiday")
    with st.form("hol_form"):
        hol_date = st.date_input("Date", value=date(hy,hm,1),
                                 min_value=date(2026,1,1), max_value=date(2027,12,31))
        existing_hol_names = list({h["name"] for h in cfg["holidays"]}) + ["— New holiday —"]
        hol_name_sel = st.selectbox("Holiday group", existing_hol_names)
        new_hol_name = st.text_input("Or enter new holiday name")
        aptu_sel    = st.selectbox("APTU", all_names)
        consult_sel = st.selectbox("Consult (blank = none)", all_names)
        c1,c2 = st.columns(2)
        save_h = c1.form_submit_button("Assign / Update", type="primary")
        del_h  = c2.form_submit_button("Delete this date's assignment")

    if save_h:
        hname = new_hol_name.strip() or (hol_name_sel if hol_name_sel != "— New holiday —" else "")
        if not hname: st.error("Enter a holiday name.")
        else:
            cfg2=get_cfg(); dk2=hol_date.isoformat()
            entry_new={"date":dk2,"aptu":res_id_map.get(aptu_sel,""),
                       "consult":res_id_map.get(consult_sel,"")}
            existing_group = next((h for h in cfg2["holidays"] if h["name"]==hname), None)
            if existing_group:
                existing_entry = next((e for e in existing_group["entries"] if e["date"]==dk2), None)
                if existing_entry: existing_entry.update(entry_new)
                else: existing_group["entries"].append(entry_new)
            else:
                cfg2["holidays"].append({"name":hname,"entries":[entry_new]})
            save_cfg(cfg2); st.success("Saved!"); st.rerun()

    if del_h:
        cfg2=get_cfg(); dk2=hol_date.isoformat()
        for h in cfg2["holidays"]:
            h["entries"]=[e for e in h["entries"] if e["date"]!=dk2]
        cfg2["holidays"]=[h for h in cfg2["holidays"] if h["entries"]]
        save_cfg(cfg2); st.success("Deleted."); st.rerun()


# ─── Tab 4: No-Call Requests ─────────────────────────────────
with tab_nc:
    cfg = get_cfg()
    st.markdown("### No-Call Requests")

    if not active_residents(cfg):
        st.info("No residents in config.")
    else:
        res_options = {r["full"]:r["id"] for r in active_residents(cfg)}
        sel_res_name = st.selectbox("Resident", list(res_options.keys()))
        sel_res_id   = res_options[sel_res_name]

        if "nc_ym" not in st.session_state: st.session_state.nc_ym = (2026,7)
        ny,nm = st.session_state.nc_ym
        c1,c2,c3 = st.columns([1,3,1])
        if c1.button("◀", key="nc_prev"):
            nm-=1
            if nm<1: nm=12; ny-=1
            st.session_state.nc_ym=(ny,nm); st.rerun()
        c2.markdown(f"<h4 style='text-align:center'>{date(ny,nm,1).strftime('%B %Y')}</h4>", unsafe_allow_html=True)
        if c3.button("▶", key="nc_next"):
            nm+=1
            if nm>12: nm=1; ny+=1
            st.session_state.nc_ym=(ny,nm); st.rerun()

        nc_dates = {e["date"] for e in cfg.get("no_call_requests",[])
                    if e["resident"]==sel_res_id}

        first_dow=date(ny,nm,1).weekday(); num_days=calendar.monthrange(ny,nm)[1]
        html='<div class="cal-header-row">'
        for d in ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]:
            html+=f'<div class="dow-hdr">{d}</div>'
        html+='</div><div class="cal-grid">'
        for slot in range(42):
            day_num=slot-first_dow+1
            if day_num<1 or day_num>num_days: html+='<div class="cal-cell empty"></div>'; continue
            d=date(ny,nm,day_num); dk=d.isoformat(); is_nc=dk in nc_dates
            dow=d.weekday(); cls="cal-cell"+('' if dow<5 else ' weekend')
            if is_nc: cls+=' no-call'
            html+=f'<div class="{cls}"><div class="day-num">{day_num}</div>'
            if is_nc: html+='<div style="font-size:10px;color:#93C5FD">🚫 no call</div>'
            html+='</div>'
        html+='</div>'
        st.markdown(html, unsafe_allow_html=True)

        st.markdown("---")
        st.markdown("**Toggle no-call dates**")
        with st.form("nc_form"):
            nc_date_input = st.date_input("Date", value=date(ny,nm,1))
            c1,c2 = st.columns(2)
            add_nc = c1.form_submit_button("🚫 Mark no-call", type="primary")
            rem_nc = c2.form_submit_button("✅ Remove no-call")

        if add_nc:
            cfg2=get_cfg(); dk2=nc_date_input.isoformat()
            if not any(e["resident"]==sel_res_id and e["date"]==dk2
                       for e in cfg2["no_call_requests"]):
                cfg2["no_call_requests"].append({"resident":sel_res_id,"date":dk2})
            save_cfg(cfg2); st.rerun()
        if rem_nc:
            cfg2=get_cfg(); dk2=nc_date_input.isoformat()
            cfg2["no_call_requests"]=[e for e in cfg2["no_call_requests"]
                                      if not (e["resident"]==sel_res_id and e["date"]==dk2)]
            save_cfg(cfg2); st.rerun()

        # List all no-call dates for this resident
        all_nc = sorted(e["date"] for e in cfg.get("no_call_requests",[])
                        if e["resident"]==sel_res_id)
        if all_nc:
            with st.expander(f"All no-call dates for {sel_res_name} ({len(all_nc)})"):
                for dk in all_nc:
                    st.write(f"• {date.fromisoformat(dk).strftime('%A, %B %-d, %Y')}")


# ─── Tab 5: Program No-Call Days ─────────────────────────────
with tab_pnc:
    cfg = get_cfg()
    st.markdown("### Program-Wide No-Call Days")
    st.caption("No one is assigned on these dates — retreats, in-service exam, graduation, etc.")

    pnc_dates = sorted(cfg.get("program_no_call",[]))

    if pnc_dates:
        for dk in pnc_dates:
            c1,c2 = st.columns([4,1])
            c1.write(f"📵  {date.fromisoformat(dk).strftime('%A, %B %-d, %Y')}")
            if c2.button("Remove", key=f"delpnc_{dk}"):
                cfg2=get_cfg()
                cfg2["program_no_call"]=[d for d in cfg2["program_no_call"] if d!=dk]
                save_cfg(cfg2); st.rerun()
    else:
        st.info("No program no-call days configured.")

    st.markdown("---")
    with st.form("pnc_form"):
        pnc_input = st.date_input("Add date", value=date(2026,9,26))
        if st.form_submit_button("➕ Add", type="primary"):
            cfg2=get_cfg(); dk2=pnc_input.isoformat()
            if dk2 not in cfg2["program_no_call"]:
                cfg2["program_no_call"].append(dk2)
                cfg2["program_no_call"].sort()
            save_cfg(cfg2); st.rerun()
