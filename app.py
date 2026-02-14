import streamlit as st
import requests
import pandas as pd
import time
import json
import os

API_KEY = "RGAPI-f782bd50-2346-467e-8758-4b4b30b9f53b"
HEADERS = {"X-Riot-Token": API_KEY}

REGION = "asia"
PLATFORM = "kr"

REFRESH_INTERVAL = 30  # 30초마다 자동 새로고침

st.title("🎮 실시간 랭크 전적판")

nicknames = st.text_area(
    "닉네임 입력",
    "닉네임1\n닉네임2"
)

DATA_FILE = "last_matches.json"

# 저장된 match 불러오기
if os.path.exists(DATA_FILE):
    with open(DATA_FILE, "r") as f:
        last_matches = json.load(f)
else:
    last_matches = {}

def get_puuid(name):
    url = f"https://{PLATFORM}.api.riotgames.com/lol/summoner/v4/summoners/by-name/{name}"
    res = requests.get(url, headers=HEADERS)
    if res.status_code == 200:
        return res.json()["puuid"]
    return None

def get_last_match(puuid):
    url = f"https://{REGION}.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids?count=1"
    res = requests.get(url, headers=HEADERS)
    if res.status_code == 200:
        return res.json()[0]
    return None

def get_result(match_id, puuid):
    url = f"https://{REGION}.api.riotgames.com/lol/match/v5/matches/{match_id}"
    res = requests.get(url, headers=HEADERS)

    if res.status_code != 200:
        return None

    data = res.json()

    for p in data["info"]["participants"]:
        if p["puuid"] == puuid:
            return "승리" if p["win"] else "패배"

    return None


names = [n.strip() for n in nicknames.split("\n") if n.strip()]

results = []
new_matches = []

for name in names:

    puuid = get_puuid(name)
    if not puuid:
        continue

    match_id = get_last_match(puuid)

    if name not in last_matches:
        last_matches[name] = match_id

    elif last_matches[name] != match_id:

        result = get_result(match_id, puuid)

        new_matches.append({
            "닉네임": name,
            "결과": result
        })

        last_matches[name] = match_id

    results.append({
        "닉네임": name,
        "최근 matchId": match_id
    })

# 저장
with open(DATA_FILE, "w") as f:
    json.dump(last_matches, f)

# 새 게임 표시
if new_matches:
    st.success("🎉 새로운 게임 감지!")
    st.dataframe(pd.DataFrame(new_matches))
else:
    st.info("새로운 게임 없음")

st.dataframe(pd.DataFrame(results))

# 자동 새로고침
time.sleep(REFRESH_INTERVAL)
st.rerun()
