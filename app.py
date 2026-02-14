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
REFRESH_SEC = 60
FETCH_MATCH_IDS = 20        # 3시간 동안 놓치지 않게 조금 넉넉히
SOLOQ_QUEUE_ID = 420        # 솔로랭크
REGION = "asia"             # KR 계정의 account-v1, match-v5는 보통 asia 라우팅

# =====================
# Secrets
# =====================
RIOT_API_KEY = st.secrets["RIOT_API_KEY"]
SUPABASE_URL = st.secrets["SUPABASE_URL"]
SUPABASE_ANON_KEY = st.secrets["SUPABASE_ANON_KEY"]

HEADERS = {"X-Riot-Token": RIOT_API_KEY}
supabase = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)

st.set_page_config(page_title="5:5 솔랭 전광판", layout="wide")
st.title("🏟️ 5:5 솔랭 전광판 (타이머 + 실시간 승/패 + 팀 합산)")

st.caption("입력 형식: **닉네임#태그** (예: Hide on bush#KR1) / 솔랭(420)만 집계 / 60초 자동 갱신")

# 자동 새로고침
st_autorefresh(interval=REFRESH_SEC * 1000, key="auto_refresh")

# =====================
# Riot API helpers (최신 Riot ID)
# =====================
@st.cache_data(ttl=3600)
def riotid_to_puuid(riot_id: str) -> str | None:
    """
    riot_id = 'gameName#tagLine'
    Riot Account API로 puuid 얻기
    """
    if "#" not in riot_id:
        return None
    game_name, tag_line = riot_id.split("#", 1)
    game_name = quote(game_name.strip(), safe="")
    tag_line = quote(tag_line.strip(), safe="")

    url = f"https://{REGION}.api.riotgames.com/riot/account/v1/accounts/by-riot-id/{game_name}/{tag_line}"
    r = requests.get(url, headers=HEADERS, timeout=10)
    if r.status_code == 200:
        return r.json().get("puuid")
    return None

def get_match_ids(puuid: str, count: int = FETCH_MATCH_IDS) -> list[str]:
    url = f"https://{REGION}.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids?count={count}"
    r = requests.get(url, headers=HEADERS, timeout=10)
    return r.json() if r.status_code == 200 else []

@st.cache_data(ttl=60)
def get_match_detail(match_id: str) -> dict | None:
    url = f"https://{REGION}.api.riotgames.com/lol/match/v5/matches/{match_id}"
    r = requests.get(url, headers=HEADERS, timeout=10)
    return r.json() if r.status_code == 200 else None

def parse_match_for_player(match_detail: dict, puuid: str):
    """
    return: (queue_id:int, played_at:datetime(utc), win:bool|None)
    """
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

def create_session(title: str, duration_minutes: int):
    supabase.table("sessions").insert({
        "title": title,
        "duration_minutes": duration_minutes,
    }).execute()

def start_session(session_id: int):
    supabase.table("sessions").update({
        "started_at": datetime.now(tz=timezone.utc).isoformat()
    }).eq("id", session_id).execute()

def end_session(session_id: int):
    supabase.table("sessions").update({
        "ended_at": datetime.now(tz=timezone.utc).isoformat()
    }).eq("id", session_id).execute()

def upsert_session_player(session_id: int, riot_id: str, puuid: str, team: str):
    supabase.table("session_players").upsert({
        "session_id": session_id,
        "nickname": riot_id,   # 여기서는 nickname 컬럼에 riot_id를 그대로 저장(닉#태그)
        "puuid": puuid,
        "team": team,
    }, on_conflict="session_id,nickname").execute()

def load_players(session_id: int):
    resp = (
        supabase.table("session_players")
        .select("nickname,puuid,team")
        .eq("session_id", session_id)
        .execute()
    )
    return resp.data or []

def existing_match_ids(session_id: int, nickname: str, match_ids: list[str]) -> set:
    if not match_ids:
        return set()
    resp = (
        supabase.table("session_results")
        .select("match_id")
        .eq("session_id", session_id)
        .eq("nickname", nickname)
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
# 사이드바: 세션/팀 설정
# =====================
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
        st.info("새 세션을 만든 뒤 팀을 구성해줘.")
        st.stop()

    st.success(f"활성 세션: #{active['id']}\n\n{active['title']}")
    started_at = active.get("started_at")
    ended_at = active.get("ended_at")

    if not started_at:
        if st.button("▶️ 세션 시작"):
            start_session(active["id"])
            st.rerun()
    else:
        if st.button("⏹ 세션 종료(점수 확정)"):
            end_session(active["id"])
            st.rerun()

    st.divider()
    st.subheader("👥 팀 구성 (각 5명)")
    team_a_text = st.text_area("팀 A (한 줄에 1명, 닉네임#태그)", height=140)
    team_b_text = st.text_area("팀 B (한 줄에 1명, 닉네임#태그)", height=140)

    if st.button("💾 팀 저장(riot_id→puuid 조회)"):
        team_a = [x.strip() for x in team_a_text.split("\n") if x.strip()][:5]
        team_b = [x.strip() for x in team_b_text.split("\n") if x.strip()][:5]

        errors = []
        for rid in team_a:
            puuid = riotid_to_puuid(rid)
            if puuid:
                upsert_session_player(active["id"], rid, puuid, "A")
            else:
                errors.append(rid)

        for rid in team_b:
            puuid = riotid_to_puuid(rid)
            if puuid:
                upsert_session_player(active["id"], rid, puuid, "B")
            else:
                errors.append(rid)

        if errors:
            st.error("puuid 조회 실패(형식/태그 확인):\n- " + "\n- ".join(errors))
        else:
            st.success("팀 저장 완료")
        st.rerun()

session_id = active["id"]
started_at = active.get("started_at")
duration_min = int(active.get("duration_minutes", 180))
ended_at = active.get("ended_at")

players = load_players(session_id)

# =====================
# 타이머
# =====================
st.subheader("⏱ 타이머")
if not started_at:
    st.warning("세션이 아직 시작되지 않았어. (좌측에서 '세션 시작' 누르기)")
else:
    start_dt = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    end_dt = start_dt + timedelta(minutes=duration_min)
    now = datetime.now(tz=timezone.utc)

    if ended_at:
        st.success("세션 종료됨 ✅")
    else:
        remaining = end_dt - now
        if remaining.total_seconds() <= 0:
            st.error("⏰ 설정한 시간이 끝났어! 좌측에서 '세션 종료'를 눌러 점수 확정해줘.")
        else:
            st.metric("남은 시간", str(remaining).split(".")[0])

# =====================
# 실시간 수집(세션 진행 중일 때만)
# =====================
new_events = []

if started_at and (not ended_at) and players:
    start_dt = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    now = datetime.now(tz=timezone.utc)

    for p in players:
        nickname = p["nickname"]  # riot_id
        puuid = p["puuid"]

        mids = get_match_ids(puuid, FETCH_MATCH_IDS)
        exist = existing_match_ids(session_id, nickname, mids)
        new_ids = [m for m in mids if m not in exist]

        inserts = []
        for mid in new_ids:
            detail = get_match_detail(mid)
            if not detail:
                continue

            queue_id, played_at, win_val = parse_match_for_player(detail, puuid)

            # 솔랭(420)만 + 세션 시작 이후 경기만
            if queue_id != SOLOQ_QUEUE_ID:
                continue
            if played_at < start_dt or played_at > now:
                continue
            if win_val is None:
                continue

            inserts.append({
                "session_id": session_id,
                "nickname": nickname,
                "match_id": mid,
                "win": win_val,
                "played_at": played_at.isoformat(),
            })
            new_events.append((nickname, "승리" if win_val else "패배", played_at.strftime("%H:%M"), mid))

            time.sleep(0.2)  # rate limit 완화

        insert_results(inserts)

# =====================
# 결과 집계 (전광판)
# =====================
results = load_results(session_id)
df_res = pd.DataFrame(results) if results else pd.DataFrame(columns=["nickname","win","played_at","match_id"])

team_map = {p["nickname"]: p["team"] for p in players}
teamA = [p["nickname"] for p in players if p["team"] == "A"]
teamB = [p["nickname"] for p in players if p["team"] == "B"]

def player_wl(riot_id: str):
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
# 상단 알림(새 경기)
# =====================
st.subheader("🔔 실시간 승/패 알림")
if new_events:
    st.success("새 경기 감지!")
    notif = pd.DataFrame(new_events, columns=["플레이어(riot_id)", "결과", "시간(UTC)", "match_id"])
    st.dataframe(notif, use_container_width=True, height=220)
else:
    st.info("이번 갱신 주기에서 새 결과 없음")

# =====================
# 전광판 UI: 팀 A vs 팀 B
# =====================
st.subheader("🏁 전광판")
left, right = st.columns(2)

with left:
    st.markdown("## 🟥 TEAM A")
    st.metric("TEAM A 총 승리 합", A_wins)
    rows = []
    for rid in teamA:
        w, l = player_wl(rid)
        rows.append({"플레이어": rid, "승": w, "패": l, "승률(%)": round(w*100/(w+l),1) if (w+l)>0 else 0.0})
    st.dataframe(pd.DataFrame(rows), use_container_width=True, height=260)

with right:
    st.markdown("## 🟦 TEAM B")
    st.metric("TEAM B 총 승리 합", B_wins)
    rows = []
    for rid in teamB:
        w, l = player_wl(rid)
        rows.append({"플레이어": rid, "승": w, "패": l, "승률(%)": round(w*100/(w+l),1) if (w+l)>0 else 0.0})
    st.dataframe(pd.DataFrame(rows), use_container_width=True, height=260)

# 승부 결과 표시
st.divider()
if started_at:
    if ended_at:
        if A_wins > B_wins:
            st.success(f"🏆 최종 승리: TEAM A ({A_wins} : {B_wins})")
        elif B_wins > A_wins:
            st.success(f"🏆 최종 승리: TEAM B ({A_wins} : {B_wins})")
        else:
            st.warning(f"🤝 무승부! ({A_wins} : {B_wins})")
    else:
        st.info(f"진행중… 현재 스코어: TEAM A {A_wins} : {B_wins} TEAM B")
else:
    st.info("세션 시작 전에는 점수가 집계되지 않아.")
