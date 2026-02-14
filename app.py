import time
from datetime import datetime, timezone, timedelta
from urllib.parse import quote

import requests
import pandas as pd
import streamlit as st
from supabase import create_client
from streamlit_autorefresh import st_autorefresh

# =====================
# Config
# =====================
UI_REFRESH_SEC = 1          # 타이머는 1초 단위
POLL_SEC = 60               # Riot API/DB 업데이트는 60초 단위(렉 방지)
FETCH_MATCH_IDS = 20        # 세션 중 놓치지 않게 넉넉히
SOLOQ_QUEUE_ID = 420
REGION = "asia"

ALERT_SHOW_SEC = 4          # 새 승/패 감지 시 오버레이 유지 시간(초)

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
# 방송용: URL 뒤에 ?overlay=1
overlay = st.query_params.get("overlay", "0") == "1"

st.set_page_config(page_title="5:5 전광판", layout="wide")
st_autorefresh(interval=UI_REFRESH_SEC * 1000, key="ui_tick")

# 컴팩트 CSS
if overlay:
    st.markdown(
        """
        <style>
        .block-container {padding-top: 0.5rem; padding-bottom: 0.5rem; padding-left: 0.6rem; padding-right: 0.6rem;}
        header, footer {visibility: hidden;}
        h1 {font-size: 18px !important; margin: 0.2rem 0 0.4rem 0;}
        h2 {font-size: 14px !important; margin: 0.2rem 0 0.3rem 0;}
        </style>
        """,
        unsafe_allow_html=True
    )

# =====================
# Session state (alert)
# =====================
if "alert_until" not in st.session_state:
    st.session_state["alert_until"] = 0.0
if "alert_text" not in st.session_state:
    st.session_state["alert_text"] = ""
if "last_poll_ts" not in st.session_state:
    st.session_state["last_poll_ts"] = 0.0

# =====================
# Riot helpers (Riot ID -> puuid)
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
# Supabase helpers
# =====================
def get_active_session():
    resp = (
        supabase.table("sessions")
        .select("*")
        .is_("ended_at", "null")
        .order("id", desc=True)
        .limit(1)
        .execute()
    )
    rows = resp.data or []
    return rows[0] if rows else None

def create_session(title: str, duration_minutes: int, team_a_name: str, team_b_name: str):
    supabase.table("sessions").insert({
        "title": title,
        "duration_minutes": duration_minutes,
        "team_a_name": team_a_name,
        "team_b_name": team_b_name,
    }).execute()

def update_team_names(session_id: int, team_a_name: str, team_b_name: str):
    supabase.table("sessions").update({
        "team_a_name": team_a_name,
        "team_b_name": team_b_name,
    }).eq("id", session_id).execute()

def start_session(session_id: int):
    supabase.table("sessions").update({
        "started_at": datetime.now(tz=timezone.utc).isoformat()
    }).eq("id", session_id).execute()

def end_session(session_id: int):
    supabase.table("sessions").update({
        "ended_at": datetime.now(tz=timezone.utc).isoformat()
    }).eq("id", session_id).execute()

def upsert_session_player(session_id: int, real_name: str, riot_id: str, puuid: str, team: str):
    # nickname 컬럼에는 riot_id 저장(닉#태그)
    supabase.table("session_players").upsert({
        "session_id": session_id,
        "real_name": real_name,
        "nickname": riot_id,
        "puuid": puuid,
        "team": team,
    }, on_conflict="session_id,nickname").execute()

def load_players(session_id: int):
    resp = (
        supabase.table("session_players")
        .select("real_name,nickname,puuid,team")
        .eq("session_id", session_id)
        .execute()
    )
    return resp.data or []

def existing_match_ids(session_id: int, riot_id: str, match_ids: list[str]) -> set:
    if not match_ids:
        return set()
    resp = (
        supabase.table("session_results")
        .select("match_id")
        .eq("session_id", session_id)
        .eq("nickname", riot_id)
        .in_("match_id", match_ids)
        .execute()
    )
    return {r["match_id"] for r in (resp.data or [])}

def insert_results(rows: list[dict]):
    if rows:
        supabase.table("session_results").upsert(rows, on_conflict="session_id,nickname,match_id").execute()

def load_results(session_id: int):
    resp = (
        supabase.table("session_results")
        .select("nickname,win,played_at,match_id")
        .eq("session_id", session_id)
        .execute()
    )
    return resp.data or []

# =====================
# UI: Admin sidebar (운영 화면만)
# =====================
if not overlay:
    with st.sidebar:
        st.header("⚙️ 운영 설정")

        # 세션 생성
        title = st.text_input("세션 제목", value=f"5:5 솔랭 승부 {datetime.now().strftime('%m/%d %H:%M')}")
        duration = st.number_input("타이머(분)", min_value=10, max_value=600, value=180, step=10)

        team_a_name_new = st.text_input("팀 A 이름", value="RED")
        team_b_name_new = st.text_input("팀 B 이름", value="BLUE")

        if st.button("➕ 새 세션 만들기"):
            create_session(title, int(duration), team_a_name_new, team_b_name_new)
            st.rerun()

        active = get_active_session()
        if active:
            st.divider()
            st.subheader("현재 활성 세션")
            st.write(f"#{active['id']} — {active.get('title','')}")
            st.caption("이미 세션이 있으면 여기서 팀 이름만 수정해도 됨")

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
            st.subheader("👥 팀 구성 (본명,게임닉#태그)")
            st.caption("예: 홍길동,Hide on bush#KR1")

            team_a_text = st.text_area("팀 A (최대 5줄)", height=140)
            team_b_text = st.text_area("팀 B (최대 5줄)", height=140)

            def parse_lines(txt: str):
                out = []
                for line in txt.split("\n"):
                    line = line.strip()
                    if not line:
                        continue
                    if "," not in line:
                        # 본명 누락
                        out.append((None, line))
                    else:
                        rn, rid = line.split(",", 1)
                        out.append((rn.strip(), rid.strip()))
                return out[:5]

            if st.button("💾 팀 저장(riot_id→puuid 조회)"):
                active2 = get_active_session()
                if not active2:
                    st.stop()
                sid = active2["id"]

                A = parse_lines(team_a_text)
                B = parse_lines(team_b_text)

                fail = []

                for rn, rid in A:
                    puuid = riotid_to_puuid(rid)
                    if rn and puuid:
                        upsert_session_player(sid, rn, rid, puuid, "A")
                    else:
                        fail.append(rid)

                for rn, rid in B:
                    puuid = riotid_to_puuid(rid)
                    if rn and puuid:
                        upsert_session_player(sid, rn, rid, puuid, "B")
                    else:
                        fail.append(rid)

                if fail:
                    st.error("저장 실패(본명 누락/형식 오류/태그 확인):\n- " + "\n- ".join(fail))
                else:
                    st.success("팀 저장 완료")
                st.rerun()

# =====================
# Load active session
# =====================
active = get_active_session()
if not active:
    st.info("활성 세션이 없습니다. (운영 화면에서 먼저 세션을 만들고 시작하세요)")
    st.stop()

session_id = active["id"]
started_at = active.get("started_at")
ended_at = active.get("ended_at")
duration_min = int(active.get("duration_minutes", 180))
team_a_name = active.get("team_a_name") or "TEAM A"
team_b_name = active.get("team_b_name") or "TEAM B"

players = load_players(session_id)

# =====================
# Polling (60초마다만)
# =====================
now = datetime.now(tz=timezone.utc)
do_poll = (not ended_at) and started_at and (time.time() - st.session_state["last_poll_ts"] >= POLL_SEC)

new_events = []  # (real_name, "W"/"L")
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

            # 솔랭만 + 세션 시작 이후만
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

            time.sleep(0.15)  # rate limit 완화

        insert_results(inserts)

    # ✅ 새 결과가 있으면 "오버레이" 트리거 (화면 전환 없이 표 위로 띄움)
    if new_events:
        n, r = new_events[-1]
        st.session_state["alert_text"] = f"{n} {'승리' if r=='W' else '패배'}"
        st.session_state["alert_until"] = time.time() + ALERT_SHOW_SEC

# =====================
# Timer text (1초 단위)
# =====================
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
# Scoreboard aggregation (본명 + W/L only)
# =====================
results = load_results(session_id)
df_res = pd.DataFrame(results) if results else pd.DataFrame(columns=["nickname", "win"])

# riot_id -> real_name
rid_to_real = {p["nickname"]: (p.get("real_name") or p["nickname"]) for p in players}
teamA = [p["nickname"] for p in players if p.get("team") == "A"]
teamB = [p["nickname"] for p in players if p.get("team") == "B"]

def wl(riot_id: str):
    if df_res.empty:
        return 0, 0
    sub = df_res[df_res["nickname"] == riot_id]
    w = int((sub["win"] == True).sum())
    l = int((sub["win"] == False).sum())
    return w, l

def team_wins(team_list: list[str]):
    if df_res.empty:
        return 0
    sub = df_res[df_res["nickname"].isin(team_list)]
    return int((sub["win"] == True).sum())

A_wins = team_wins(teamA)
B_wins = team_wins(teamB)

# =====================
# Render helpers (compact HTML tables)
# =====================
def render_team_table(team_list, title):
    rows = ""
    for rid in team_list:
        real = rid_to_real.get(rid, rid)
        w, l = wl(rid)
        rows += f"""
        <tr>
          <td style="padding:2px 6px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">{real}</td>
          <td style="padding:2px 6px; text-align:right; width:42px;">{w}</td>
          <td style="padding:2px 6px; text-align:right; width:42px;">{l}</td>
        </tr>
        """
    font = "11px" if overlay else "14px"
    title_font = "12px" if overlay else "16px"
    return f"""
    <div style="border:1px solid rgba(255,255,255,0.12); border-radius:12px; padding:6px;">
      <div style="font-weight:900; margin-bottom:4px; font-size:{title_font};">{title}</div>
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

# =====================
# Main UI
# =====================
if not overlay:
    st.title("🏟️ 5:5 전광판")
else:
    st.markdown("## 🏟️ 5:5 전광판")

# 상단: 타이머 상시 + 팀 스코어 상시
st.markdown(
    f"""
    <div style="display:flex; justify-content:space-between; align-items:center; gap:10px; margin-bottom:6px;">
      <div style="font-weight:900; font-size:{'12px' if overlay else '18px'};">⏱ {timer_text}</div>
      <div style="font-weight:1000; font-size:{'16px' if overlay else '30px'};">
        🟥 {team_a_name} {A_wins} : {B_wins} {team_b_name} 🟦
      </div>
    </div>
    """,
    unsafe_allow_html=True
)

# 전광판 표(항상 표시)
c1, c2 = st.columns(2, gap="small")
with c1:
    st.markdown(render_team_table(teamA, f"🟥 {team_a_name}"), unsafe_allow_html=True)
with c2:
    st.markdown(render_team_table(teamB, f"🟦 {team_b_name}"), unsafe_allow_html=True)

# 새 결과 오버레이(표 위에 뜨고 자동 사라짐)
show_alert = time.time() < st.session_state["alert_until"]
if show_alert:
    font_size = "20px" if overlay else "48px"
    st.markdown(
        f"""
        <style>
        .alert-overlay {{
          position: fixed;
          left: 50%;
          top: 28%;
          transform: translate(-50%, -50%);
          z-index: 9999;
          padding: 14px 18px;
          border-radius: 18px;
          background: rgba(0,0,0,0.78);
          border: 1px solid rgba(255,255,255,0.28);
          color: white;
          font-weight: 1000;
          font-size: {font_size};
          white-space: nowrap;
          box-shadow: 0 14px 44px rgba(0,0,0,0.35);
        }}
        </style>
        <div class="alert-overlay">🔔 {st.session_state["alert_text"]}</div>
        """,
        unsafe_allow_html=True
    )

# 운영 화면에서만(선택) 디버그 정보
if (not overlay) and (not started_at):
    st.info("세션 시작 전입니다. 좌측에서 '세션 시작' 누르면 승/패 집계가 시작됩니다.")
