import time
from datetime import datetime, timezone, timedelta
from urllib.parse import quote

import requests
import pandas as pd
import streamlit as st
from supabase import create_client
import streamlit.components.v1 as components

# =====================
# Config
# =====================
POLL_SEC = 60
FETCH_MATCH_IDS = 20
SOLOQ_QUEUE_ID = 420
REGION = "asia"
ALERT_SHOW_SEC = 4

# =====================
# Secrets
# =====================
RIOT_API_KEY = st.secrets["RIOT_API_KEY"]
SUPABASE_URL = st.secrets["SUPABASE_URL"]
SUPABASE_ANON_KEY = st.secrets["SUPABASE_ANON_KEY"]

HEADERS = {"X-Riot-Token": RIOT_API_KEY}
supabase = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)

# =====================
# UI mode
# =====================
overlay = st.query_params.get("overlay", "0") == "1"

st.set_page_config(
    page_title="5:5 전광판",
    layout="wide" if not overlay else "centered",
)

# =====================
# Session State
# =====================
if "alert_until" not in st.session_state:
    st.session_state.alert_until = 0.0
if "alert_text" not in st.session_state:
    st.session_state.alert_text = ""
if "last_poll" not in st.session_state:
    st.session_state.last_poll = 0.0

# =====================
# Riot helpers
# =====================
@st.cache_data(ttl=3600)
def riotid_to_puuid(riot_id: str):
    if "#" not in riot_id:
        return None
    name, tag = riot_id.split("#", 1)
    name = quote(name.strip(), safe="")
    tag = quote(tag.strip(), safe="")
    url = f"https://{REGION}.api.riotgames.com/riot/account/v1/accounts/by-riot-id/{name}/{tag}"
    r = requests.get(url, headers=HEADERS, timeout=10)
    return r.json().get("puuid") if r.status_code == 200 else None

def get_match_ids(puuid: str):
    url = f"https://{REGION}.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids?count={FETCH_MATCH_IDS}"
    r = requests.get(url, headers=HEADERS, timeout=10)
    return r.json() if r.status_code == 200 else []

@st.cache_data(ttl=60)
def get_match_detail(match_id: str):
    url = f"https://{REGION}.api.riotgames.com/lol/match/v5/matches/{match_id}"
    r = requests.get(url, headers=HEADERS, timeout=10)
    return r.json() if r.status_code == 200 else None

# =====================
# Supabase helpers
# =====================
def get_active_session():
    r = (
        supabase.table("sessions")
        .select("*")
        .is_("ended_at", "null")
        .order("id", desc=True)
        .limit(1)
        .execute()
    )
    return r.data[0] if r.data else None

def create_session(title: str, duration_minutes: int, team_a_name: str, team_b_name: str):
    supabase.table("sessions").insert({
        "title": title,
        "duration_minutes": duration_minutes,
        "team_a_name": team_a_name,
        "team_b_name": team_b_name
    }).execute()

def update_team_names(session_id: int, team_a_name: str, team_b_name: str):
    supabase.table("sessions").update({
        "team_a_name": team_a_name,
        "team_b_name": team_b_name
    }).eq("id", session_id).execute()

def start_session(session_id: int):
    supabase.table("sessions").update({
        "started_at": datetime.now(tz=timezone.utc).isoformat()
    }).eq("id", session_id).execute()

def end_session(session_id: int):
    supabase.table("sessions").update({
        "ended_at": datetime.now(tz=timezone.utc).isoformat()
    }).eq("id", session_id).execute()

def upsert_player(session_id: int, real_name: str, riot_id: str, puuid: str, team: str):
    supabase.table("session_players").upsert({
        "session_id": session_id,
        "real_name": real_name,
        "nickname": riot_id,
        "puuid": puuid,
        "team": team,
    }, on_conflict="session_id,nickname").execute()

def load_players(session_id: int):
    r = (
        supabase.table("session_players")
        .select("real_name,nickname,puuid,team")
        .eq("session_id", session_id)
        .execute()
    )
    return r.data or []

def load_results(session_id: int):
    r = (
        supabase.table("session_results")
        .select("nickname,win,match_id")
        .eq("session_id", session_id)
        .execute()
    )
    return r.data or []

def existing_match_ids(session_id: int, riot_id: str, match_ids: list[str]) -> set:
    if not match_ids:
        return set()
    r = (
        supabase.table("session_results")
        .select("match_id")
        .eq("session_id", session_id)
        .eq("nickname", riot_id)
        .in_("match_id", match_ids)
        .execute()
    )
    return {x["match_id"] for x in (r.data or [])}

def insert_results(rows: list[dict]):
    if rows:
        supabase.table("session_results").upsert(
            rows, on_conflict="session_id,nickname,match_id"
        ).execute()

# =====================
# Admin UI
# =====================
if not overlay:
    with st.sidebar:
        st.markdown("### 운영 설정")

        title = st.text_input("세션 제목", value=f"5:5 솔랭 승부 {datetime.now().strftime('%m/%d %H:%M')}")
        duration = st.number_input("타이머(분)", min_value=10, max_value=600, value=180, step=10)
        a_name = st.text_input("팀 A 이름", value="RED")
        b_name = st.text_input("팀 B 이름", value="BLUE")

        if st.button("➕ 새 세션 만들기"):
            create_session(title, int(duration), a_name, b_name)
            st.rerun()

        active = get_active_session()
        if active:
            st.divider()
            st.markdown(f"**활성 세션 #{active['id']}**")
            st.caption(active.get("title",""))

            cur_a = active.get("team_a_name") or "TEAM A"
            cur_b = active.get("team_b_name") or "TEAM B"

            edit_a = st.text_input("현재 팀 A 이름", value=cur_a, key="edit_a")
            edit_b = st.text_input("현재 팀 B 이름", value=cur_b, key="edit_b")
            if st.button("💾 팀 이름 저장"):
                update_team_names(active["id"], edit_a, edit_b)
                st.rerun()

            if not active.get("started_at"):
                if st.button("▶️ 세션 시작"):
                    start_session(active["id"])
                    st.rerun()
            else:
                if st.button("⏹ 세션 종료(확정)"):
                    end_session(active["id"])
                    st.rerun()

            st.divider()
            st.markdown("### 팀 구성 (본명,게임닉#태그)")
            st.caption("예: 홍길동,Hide on bush#KR1")

            team_a_text = st.text_area("팀 A (최대 5줄)", height=120)
            team_b_text = st.text_area("팀 B (최대 5줄)", height=120)

            def parse_lines(txt: str):
                out = []
                for line in txt.split("\n"):
                    line = line.strip()
                    if not line:
                        continue
                    if "," not in line:
                        out.append((None, line))
                    else:
                        rn, rid = line.split(",", 1)
                        out.append((rn.strip(), rid.strip()))
                return out[:5]

            if st.button("💾 팀 저장(riot_id→puuid 조회)"):
                sid = active["id"]
                A = parse_lines(team_a_text)
                B = parse_lines(team_b_text)

                failed = []
                for rn, rid in A:
                    puuid = riotid_to_puuid(rid)
                    if rn and puuid:
                        upsert_player(sid, rn, rid, puuid, "A")
                    else:
                        failed.append(rid)

                for rn, rid in B:
                    puuid = riotid_to_puuid(rid)
                    if rn and puuid:
                        upsert_player(sid, rn, rid, puuid, "B")
                    else:
                        failed.append(rid)

                if failed:
                    st.error("저장 실패(본명 누락/형식 오류/태그 확인):\n- " + "\n- ".join(failed))
                else:
                    st.success("팀 저장 완료")
                st.rerun()

# =====================
# Load session
# =====================
active = get_active_session()
if not active:
    st.info("활성 세션이 없습니다.")
    st.stop()

session_id = active["id"]
started_at = active.get("started_at")
ended_at = active.get("ended_at")
duration_min = int(active.get("duration_minutes", 180))
team_a_name = active.get("team_a_name") or "TEAM A"
team_b_name = active.get("team_b_name") or "TEAM B"

players = load_players(session_id)

# =====================
# Polling
# =====================
new_events = []
now = datetime.now(timezone.utc)

do_poll = (not ended_at) and started_at and (time.time() - st.session_state.last_poll >= POLL_SEC)

if do_poll and players:
    st.session_state.last_poll = time.time()
    start_dt = datetime.fromisoformat(started_at.replace("Z", "+00:00"))

    for p in players:
        real_name = p.get("real_name") or p["nickname"]
        riot_id = p["nickname"]
        puuid = p["puuid"]

        match_ids = get_match_ids(puuid)
        exist = existing_match_ids(session_id, riot_id, match_ids)
        new_ids = [m for m in match_ids if m not in exist]

        inserts = []
        for mid in new_ids:
            detail = get_match_detail(mid)
            if not detail:
                continue

            info = detail.get("info", {})
            if info.get("queueId") != SOLOQ_QUEUE_ID:
                continue

            ts = info.get("gameEndTimestamp") or info.get("gameStartTimestamp")
            if not ts:
                continue
            played = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)

            if played < start_dt or played > now:
                continue

            win_val = None
            for part in info.get("participants", []):
                if part.get("puuid") == puuid:
                    win_val = bool(part.get("win"))
                    break
            if win_val is None:
                continue

            inserts.append({
                "session_id": session_id,
                "nickname": riot_id,
                "match_id": mid,
                "win": win_val,
                "played_at": played.isoformat()
            })
            new_events.append((real_name, win_val))
            time.sleep(0.15)

        insert_results(inserts)

    if new_events:
        n, w = new_events[-1]
        st.session_state.alert_text = f"{n} {'승리' if w else '패배'}"
        st.session_state.alert_until = time.time() + ALERT_SHOW_SEC

# =====================
# Aggregate
# =====================
results = load_results(session_id)
df = pd.DataFrame(results) if results else pd.DataFrame(columns=["nickname", "win"])

def wl(riot_id: str):
    if df.empty:
        return 0, 0
    sub = df[df["nickname"] == riot_id]
    w = int((sub["win"] == True).sum())
    l = int((sub["win"] == False).sum())
    return w, l

teamA = [p for p in players if p.get("team") == "A"]
teamB = [p for p in players if p.get("team") == "B"]

A_wins = sum(wl(p["nickname"])[0] for p in teamA)
B_wins = sum(wl(p["nickname"])[0] for p in teamB)

# =====================
# Render HTML via components.html (stable)
# =====================
timer_line = "시작 전"
if started_at:
    start_dt = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    end_dt = start_dt + timedelta(minutes=duration_min)
    timer_line = f"{start_dt.strftime('%H:%M')} ~ {end_dt.strftime('%H:%M')}"
    if ended_at:
        timer_line += " (종료)"

def team_rows(team_list):
    rows = ""
    for p in team_list:
        rid = p["nickname"]
        name = p.get("real_name") or rid
        w, l = wl(rid)
        rows += f"""
        <tr>
          <td title="{name}">{name}</td>
          <td class="num">{w}</td>
          <td class="num">{l}</td>
        </tr>
        """
    return rows or "<tr><td colspan='3' class='small'>-</td></tr>"

css = """
<style>
:root{
  --bg:#0f1115;
  --panel:rgba(255,255,255,.06);
  --stroke:rgba(255,255,255,.12);
  --text:rgba(255,255,255,.92);
  --muted:rgba(255,255,255,.62);
  --red:#ff4b6e;
  --blue:#4aa3ff;
}
html,body{margin:0; padding:0; background:var(--bg); color:var(--text); font-family: ui-sans-serif, system-ui;}
.card{
  border:1px solid var(--stroke);
  background:var(--panel);
  border-radius:16px;
  padding:10px 10px;
}
.topline{
  display:flex; justify-content:space-between; align-items:center; gap:10px;
  margin-bottom:8px;
}
.timer{
  font-weight:800; font-size:11px; color:var(--muted); white-space:nowrap;
}
.score{
  font-weight:950; font-size:18px; white-space:nowrap;
}
.score .r{color:var(--red);}
.score .b{color:var(--blue);}
.grid{
  display:grid; grid-template-columns: 1fr 1fr; gap:8px;
}
.teamTitle{
  display:flex; justify-content:space-between; align-items:center;
  font-weight:900; font-size:11px; margin-bottom:6px;
}
.small{font-size:10px; color:var(--muted);}
.table{width:100%; border-collapse:collapse; font-size:11px;}
.table th{
  text-align:left; font-size:10px; color:var(--muted);
  padding:4px 0; border-bottom:1px solid rgba(255,255,255,.10);
}
.table td{
  padding:5px 0; border-bottom:1px solid rgba(255,255,255,.06);
  white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
}
.num{text-align:right; width:34px; font-weight:900;}
.overlayToast{
  position:fixed; left:50%; top:50%; transform:translate(-50%,-50%);
  z-index:9999;
  padding:12px 14px; border-radius:14px;
  background:rgba(0,0,0,.78);
  border:1px solid rgba(255,255,255,.22);
  color:white; font-weight:950; font-size:20px;
  white-space:nowrap; box-shadow:0 14px 40px rgba(0,0,0,.45);
}
</style>
"""

alert_html = ""
if time.time() < st.session_state.alert_until:
    alert_html = f"""<div class="overlayToast">🔔 {st.session_state.alert_text}</div>"""

html = f"""
{css}
<div class="card">
  <div class="topline">
    <div class="timer">⏱ {timer_line}</div>
    <div class="score">
      <span class="r">{team_a_name}</span> {A_wins} : {B_wins} <span class="b">{team_b_name}</span>
    </div>
  </div>

  <div class="grid">
    <div>
      <div class="teamTitle"><span>🟥 {team_a_name}</span><span class="small">W/L</span></div>
      <table class="table">
        <thead><tr><th>이름</th><th class="num">승</th><th class="num">패</th></tr></thead>
        <tbody>{team_rows(teamA)}</tbody>
      </table>
    </div>

    <div>
      <div class="teamTitle"><span>🟦 {team_b_name}</span><span class="small">W/L</span></div>
      <table class="table">
        <thead><tr><th>이름</th><th class="num">승</th><th class="num">패</th></tr></thead>
        <tbody>{team_rows(teamB)}</tbody>
      </table>
    </div>
  </div>
</div>
{alert_html}
"""

# 370x240에 맞게 height 지정 (약간 여유)
components.html(html, height=240 if overlay else 320, scrolling=False)

if not overlay:
    st.caption("방송 오버레이: URL 뒤에 `?overlay=1` (권장 크기 370×240)")


