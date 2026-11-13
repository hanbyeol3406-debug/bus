import streamlit as st
import pandas as pd
from datetime import datetime, timedelta
import os

# 1) 데이터 로드 (첨부 엑셀 파일 경로) - 현재는 시트별로 최대한 일반화
DATA_FILES = ["/home/user/input/data_7575608.xlsx","/home/user/input/data_7575609.xlsx"]

@st.cache_data(ttl=3600)
def load_all_data(paths):
    dfs = []
    for p in paths:
        if not os.path.exists(p):
            continue
        try:
            x = pd.ExcelFile(p)
            for sh in x.sheet_names:
                df = x.parse(sh)
                dfs.append({"sheet":sh, "path":p, "df":df})
        except Exception:
            pass
    return dfs

DATA = load_all_data(DATA_FILES)

def guess_next_eta(df, bus_no, current_stop, dest_stop, now=None):
    # Very naive matcher: look for rows where bus_no appears in any cell and dest_stop in any cell after current_stop
    if now is None:
        now = datetime.now()
    try:
        # try to find time string hh:mm or hh:mm:ss in any numeric-like column
        times = []
        for val in df.values:
            for v in (val if isinstance(val, tuple) else (val,)):
                if isinstance(v, str):
                    # find times like 08:30
                    import re
                    m = re.findall(r"\b\d{1,2}:\d{2}(?::\d{2})?\b", v)
                    for t in m:
                        try:
                            hh,mm = map(int,t.split(':')[:2])
                            times.append((hh*60+mm, t))
                        except Exception:
                            pass
        if not times:
            return None
        # take earliest time after now in minutes
        now_min = now.hour*60 + now.minute
        candidates = [t for t in times if t[0] >= now_min]
        if not candidates:
            return None
        next_min = min(c[0] for c in candidates) - now_min
        return max(0, int(next_min))
    except Exception:
        return None

def compute_eta_for_row(df, bus_no, current_stop, dest_stop):
    eta = guess_next_eta(df,bus_no,current_stop,dest_stop)
    return eta

st.set_page_config(page_title="금산 버스 ETA MVP", layout="wide")
st.title("금산 버스 ETA MVP (시간표 기반)")

with st.sidebar:
    st.header("입력")
    cur = st.text_input("현재 정류장", value="")
    bus = st.text_input("타야하는 버스 번호", value="")
    dest = st.text_input("도착 정류장", value="")

# ETA 상단 고정
eta_text = "도착 예측 정보를 확인할 수 없습니다."
if cur and bus and dest:
    eta_vals = []
    for item in DATA:
        df = item["df"]
        eta = compute_eta_for_row(df, bus, cur, dest)
        if eta is not None:
            eta_vals.append(eta)
    if eta_vals:
        eta_text = f"도착까지 남은 시간: 약 {min(eta_vals)}분"

st.markdown(f"## 상단 ETA: {eta_text}")

# 4개 모드 중 시간표 안내를 기본으로 표시
mode = st.selectbox("모드 선택", ["시간표 안내", "대기시간", "도착 예측(간단)", "구간 색상(미적용)"]) 

# 데이터 표시 간단 예시
st.subheader("시간표 데이터 요약(샘플)")
rows = []
for item in DATA:
    df = item["df"]
    # 가장 첫 행의 예시를 만들어 보여주기
    if not df.empty:
        rows.append({"시트": item["sheet"], "행수": int(df.shape[0]), "열수": int(df.shape[1])})
        break
if rows:
    st.table(pd.DataFrame(rows))
else:
    st.info("데이터 파일을 로드 중입니다...")

# 간단한 입력 확인 UI
st.write("입력값 확인:", {"현재정류장":cur, "버스":bus, "도착정류장":dest})

# 4개 모드 중 하나 예시 동작
if mode == "시간표 안내":
    st.info("시간표 기반 ETA를 표시합니다. 실제 시간표 매칭 로직은 데이터 구조에 맞춰 확장 필요합니다.")
elif mode == "대기시간":
    st.info("대기시간 모드: 상단 ETA를 기준으로 남은 시간 표시를 제공합니다.")
elif mode == "도착 예측(간단)":
    st.info("도착 예측(간단): 현재 ETA와 추가 예측 항목을 간단히 보여줍니다.")
else:
    st.info("구간 색상 모드: 실시간 데이터가 연결되면 색상 맵으로 표시합니다.")
