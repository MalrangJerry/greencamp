import time
from datetime import datetime, timezone, timedelta
from urllib.parse import quote

import requests
import pandas as pd
import streamlit as st
from supabase import create_client
from streamlit_autorefresh import st_autorefresh

# =====================
# 설정
# =====================
UI_REFRESH_SEC = 1          # 타이머/화면은 1초 단위
POLL_SEC = 60               # Riot API/DB 업데이트는 60초 단위
FETCH_MATCH_IDS = 20
SOLOQ_QUEUE_ID = 420
REGION = "asia"

RIOT_API_KEY = st.secrets["RIOT_API_KEY"]
SUPABASE_URL = st.secrets["SUPABASE_URL"]
SUPABASE_ANON_KEY = st.secrets["SUPABASE_ANON_KEY"]

HEADERS = {"X-Riot-Token": RIOT_API_KEY}
supabase = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)

st.set_page_config(page_title="5:5 전광판", layout="wide")

# ✅ 1초마다 UI만 갱신
st_autorefresh(interval=UI_REFRESH_SEC * 1000, key="ui_tick")

# =====================
# 컴팩트(방송 오버레이) CSS
# =====================
overlay = st.query_params.get("overlay", "0") == "1"   # URL 뒤에 ?overlay=1 붙이면 오버레이 모드
if overlay:
    st.markdown(
        """
        <style>
        /* 상단/하단 Streamlit 기본 여백 제거 */
        .block-container {padding-top: 0.6rem; padding-bottom: 0.6rem; padding-left: 0.8rem; padding-right: 0.8rem;}
        header, footer {visibility: hidden;}
        /* 제목/텍스트 작게 */
        h1 {font-size: 20px !important; margin: 0.2rem 0 0.4rem 0;}
        h2 {font-size: 16px !important; margin: 0.2rem 0 0.3rem 0;}
        .stMetric {padding: 0.2rem 0.4rem;}
        /* 데이터프레임은 너무 커서 숨기기 권장 (우린 HTML표 사용) */
        </style>
        """,
        unsafe_allow_html=True
    )

# =====================
# Riot helpers (Riot ID)
# =====================
@st.cache_data(ttl=3600)
def riotid_to_puuid(riot_id: str):
    if "#" not in riot_id:
        return None
    game_name, tag_line = riot_id.split("#", 1)
    game_name = quote(game_name.strip(), safe="")
    tag_line = quote(tag_line.strip(), safe="")
    url = f"https://{REGION}.api.riotgames.com/riot/account/v1/accounts/by-riot-id/{game_name}/{tag_line}"
    r = requests.get(url, headers=HEADERS, timeout=10)
    return r.json().get("puuid") if r.status_code == 200 else None

def get_match_ids(puuid: str, count: int = FETCH_MATCH_IDS):
    url = f"https://{REGION}.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids?count={count}"
    r = requests.get(url, headers=HEADERS, timeout=10)
    return r.json() if r.status_code == 200 else []

@st.cache_data(ttl=60)
def get_match_detail(match_id: str):
    url = f"https://{REGION}.api.riotgames.com/lol/match/v5/matches/{match_id}"
    r = requests.get(url, headers=HEADERS, timeout=10)
    return r.json() if r.status_code == 200 else None

def parse_match_for_player(match_detail: dict, puuid: str):
    info = match_detail.get("info", {})
    queue_id = info.get("queueId")
    ts = info.get("gameEndTimestamp") or info.get("gameStartTimestamp")
    played_at = datetime.fromtimestamp(ts / 1000, tz=timezone.utc) if ts else datetime.now(tz=timezone.utc)

    win_val = None
    for p in info.get("participants", []):
        if p.get("puuid") == puuid:
            win_val = bool(p.get("win"))
            break

    return queue_id, played_at, win_val

# =====================
# DB helpers
# =====================
def get_active_session():
    resp = (supabase.table("sessions")
            .select("*")
            .is_("ended_at", "null")
            .order("id", desc=True)
            .limit(1)
            .execute())
    rows = resp.data or []
    return rows[0] if rows else None

def create_session(title: str, duration_minutes: int):
    supabase.table("sessions").insert({"title": title, "duration_minutes": duration_minutes}).execute()

def start_session(session_id: int):
    supabase.table("sessions").update({"started_at": datetime.now(tz=timezone.utc).isoformat()}).eq("id", session_id).execute()

def end_session(session_id: int):
    supabase.table("sessions").update({"ended_at": datetime.now(tz=timezone.utc).isoformat()}).eq("id", session_id).execute()

def upsert_session_player(session_id: int, real_name: str, riot_id: str, puuid: str, team: str):
    supabase.table("session_players").upsert({
        "session_id": session_id,
        "real_name": real_name,
        "nickname": riot_id,  # riot_id 저장
        "puuid": puuid,
        "team": team
    }, on_conflict="session_id,nickname").execute()

def load_players(session_id: int):
    resp = (supabase.table("session_players")
            .select("real_name,nickname,puuid,team")
            .eq("session_id", session_id)
            .execute())
    return resp.data or []

def existing_match_ids(session_id: int, riot_id: str, match_ids: list[str]) -> set:
    if not match_ids:
        return set()
    resp = (supabase.table("session_results")
            .select("match_id")
            .eq("session_id", session_id)
            .eq("nickname", riot_id)
            .in_("match_id", match_ids)
            .execute())
    return {r["match_id"] for r in (resp.data or [])}

def insert_results(rows: list[dict]):
    if rows:
        supabase.table("session_results").upsert(rows, on_conflict="session_id,nickname,match_id").execute()

def load_results(session_id: int):
    resp = (supabase.table("session_results")
            .select("nickname,win,played_at,match_id")
            .eq("session_id", session_id)
            .execute())
    return resp.data or []

# =====================
# 사이드바 (overlay 모드에서는 숨기는 게 좋음)
# =====================
if not overlay:
    with st.sidebar:
        st.header("⚙️ 세션 설정")
        active = get_active_session()

        title = st.text_input("세션 제목", value=f"5:5 솔랭 승부 {datetime.now().strftime('%m/%d %H:%M')}")
        duration = st.number_input("타이머(분)", min_value=10, max_value=600, value=180, step=10)

        if st.button("➕ 새 세션 만들기"):
            create_session(title, int(duration))
            st.rerun()

        active = get_active_session()
        if not active:
            st.stop()

        if not active.get("started_at"):
            if st.button("▶️ 세션 시작"):
                start_session(active["id"])
                st.rerun()
        else:
            if st.button("⏹ 세션 종료"):
                end_session(active["id"])
                st.rerun()

        st.divider()
        st.subheader("👥 팀 입력 (본명,게임닉#태그)")
        st.caption("예: 스트리머 닉네임,조회할 롤닉네임 ex.로기닷#KR1")
        team_a_text = st.text_area("팀 A (최대 5줄)", height=140)
        team_b_text = st.text_area("팀 B (최대 5줄)", height=140)

        if st.button("💾 팀 저장"):
            active = get_active_session()
            if not active:
                st.stop()
            sid = active["id"]

            def parse_lines(txt):
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

            A = parse_lines(team_a_text)
            B = parse_lines(team_b_text)

            errors = []
            for rn, rid in A:
                puuid = riotid_to_puuid(rid)
                if puuid and rn:
                    upsert_session_player(sid, rn, rid, puuid, "A")
                else:
                    errors.append(line)

            for rn, rid in B:
                puuid = riotid_to_puuid(rid)
                if puuid and rn:
                    upsert_session_player(sid, rn, rid, puuid, "B")
                else:
                    errors.append(line)

            st.success("저장 완료(오류 있으면 입력 형식 확인)")
            st.rerun()

active = get_active_session()
if not active:
    st.info("세션이 없습니다. (overlay 모드면 운영 화면에서 세션을 먼저 만들어야 함)")
    st.stop()

session_id = active["id"]
started_at = active.get("started_at")
ended_at = active.get("ended_at")
duration_min = int(active.get("duration_minutes", 180))

players = load_players(session_id)

# =====================
# ✅ 60초마다만 Riot API polling (딜레이/깜빡임 줄이기 핵심)
# =====================
now = datetime.now(tz=timezone.utc)
if "last_poll_ts" not in st.session_state:
    st.session_state["last_poll_ts"] = 0

do_poll = (not ended_at) and started_at and (time.time() - st.session_state["last_poll_ts"] >= POLL_SEC)

new_events = []
if do_poll and players:
    st.session_state["last_poll_ts"] = time.time()
    start_dt = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    for p in players:
        real_name = p.get("real_name") or p["nickname"]
        riot_id = p["nickname"]
        puuid = p["puuid"]

        mids = get_match_ids(puuid, FETCH_MATCH_IDS)
        exist = existing_match_ids(session_id, riot_id, mids)
        new_ids = [m for m in mids if m not in exist]

        inserts = []
        for mid in new_ids:
            detail = get_match_detail(mid)
            if not detail:
                continue
            queue_id, played_at, win_val = parse_match_for_player(detail, puuid)

            if queue_id != SOLOQ_QUEUE_ID:
                continue
            if played_at < start_dt or played_at > now:
                continue
            if win_val is None:
                continue

            inserts.append({
                "session_id": session_id,
                "nickname": riot_id,
                "match_id": mid,
                "win": win_val,
                "played_at": played_at.isoformat(),
            })
            new_events.append((real_name, "W" if win_val else "L"))

            time.sleep(0.15)

        insert_results(inserts)

# =====================
# 타이머 (1초 단위로 즉시 반영)
# =====================
st.markdown("# 🏟️ 5:5 전광판" if not overlay else "## 🏟️ 5:5 전광판")

if started_at:
    start_dt = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    end_dt = start_dt + timedelta(minutes=duration_min)
    remaining = end_dt - now
    if ended_at:
        timer_text = "종료"
    else:
        timer_text = "00:00:00" if remaining.total_seconds() <= 0 else str(remaining).split(".")[0]
else:
    timer_text = "시작 전"

# =====================
# 결과 집계 (본명 기준)
# =====================
results = load_results(session_id)
df_res = pd.DataFrame(results) if results else pd.DataFrame(columns=["nickname","win"])

# riot_id -> real_name
rid_to_real = {p["nickname"]: (p.get("real_name") or p["nickname"]) for p in players}
teamA = [p["nickname"] for p in players if p["team"] == "A"]
teamB = [p["nickname"] for p in players if p["team"] == "B"]

def wl(riot_id):
    if df_res.empty:
        return 0, 0
    sub = df_res[df_res["nickname"] == riot_id]
    w = int((sub["win"] == True).sum())
    l = int((sub["win"] == False).sum())
    return w, l

def team_wins(team_list):
    if df_res.empty:
        return 0
    sub = df_res[df_res["nickname"].isin(team_list)]
    return int((sub["win"] == True).sum())

A_wins = team_wins(teamA)
B_wins = team_wins(teamB)

# =====================
# 🔥 방송용 전광판(작게)
# =====================
# 상단: 타이머 + 팀 점수
top = f"""
<div style="display:flex; justify-content:space-between; align-items:center; gap:12px;">
  <div style="font-size:{'14px' if overlay else '18px'}; font-weight:700;">⏱ {timer_text}</div>
  <div style="font-size:{'18px' if overlay else '28px'}; font-weight:800;">
    🟥 A {A_wins} : {B_wins} B 🟦
  </div>
</div>
"""
st.markdown(top, unsafe_allow_html=True)

# 새 알림은 작게(overlay에선 1줄만)
if new_events:
    if overlay:
        last = new_events[-1]
        st.markdown(f"<div style='font-size:12px; opacity:0.9;'>🔔 {last[0]} {last[1]}</div>", unsafe_allow_html=True)
    else:
        st.success("새 경기 감지: " + ", ".join([f"{n} {r}" for n, r in new_events]))

# 팀 표(본명 + 승/패만)
def render_team(team_list, title, color):
    rows = ""
    for rid in team_list:
        real = rid_to_real.get(rid, rid)
        w, l = wl(rid)
        rows += f"""
        <tr>
          <td style="padding:2px 6px; white-space:nowrap;">{real}</td>
          <td style="padding:2px 6px; text-align:right; width:40px;">{w}</td>
          <td style="padding:2px 6px; text-align:right; width:40px;">{l}</td>
        </tr>
        """
    font = "12px" if overlay else "14px"
    return f"""
    <div style="border:1px solid rgba(255,255,255,0.12); border-radius:10px; padding:6px;">
      <div style="font-weight:800; margin-bottom:4px; font-size:{font};">{title}</div>
      <table style="width:100%; border-collapse:collapse; font-size:{font};">
        <thead>
          <tr style="opacity:0.8;">
            <th style="text-align:left; padding:2px 6px;">이름</th>
            <th style="text-align:right; padding:2px 6px;">승</th>
            <th style="text-align:right; padding:2px 6px;">패</th>
          </tr>
        </thead>
        <tbody>
          {rows}
        </tbody>
      </table>
    </div>
    """

c1, c2 = st.columns(2, gap="small")
with c1:
    st.markdown(render_team(teamA, "🟥 TEAM A", "red"), unsafe_allow_html=True)
with c2:
    st.markdown(render_team(teamB, "🟦 TEAM B", "blue"), unsafe_allow_html=True)

