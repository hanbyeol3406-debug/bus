"""금산 버스 길잡이 | 실행: streamlit run app.py

GitHub 저장소 루트에 app.py, requirements.txt 두 파일을 올리고
Streamlit Community Cloud에서 app.py를 실행하세요. Python 3.11 이상 권장.
인증키를 GitHub에 커밋하지 말고 Cloud > App settings > Secrets에 설정합니다.

[TOML 예시: 아래 이름을 최상위에 설정, 대괄호 섹션은 만들지 않습니다]
TAGO_API_KEY = "공공데이터포털 일반 인증키(Decoding)"
ITS_API_KEY = "국가교통정보센터 인증키"
# 선택 설정
# CITY_CODE = ""                  # 기본: TAGO 도시목록에서 금산 검색
# TIMETABLE_CSV_URL = "https://.../timetable.csv"
# ROAD_GEOJSON_URL = "https://.../geumsan_links.geojson"

필요한 TAGO 활용신청: 버스정류소정보, 버스노선정보, 버스위치정보,
버스도착정보. 각각 별도 승인 여부를 확인합니다.
키가 없으면 가상 정류장/가상 900번의 시연 모드로 작동합니다.
키가 있는데 실패하면 시연 정보로 대체하지 않습니다.

시간표 CSV(UTF-8): routeid,nodeid,trip_id,service_date,departure_time
service_date=YYYY-MM-DD, departure_time=HH:MM 또는 HH:MM:SS.
각 운행 trip_id마다 모든 정류장 통과 예정시각을 한 행씩 입력합니다.
반복 정류장이 있는 순환노선은 별도 stop_seq 열도 반드시 입력합니다.
예: REAL_ROUTE_ID,REAL_NODE_ID,run01,2026-11-11,08:30,1
검증된 공식 시간표만 관리자가 정리해 URL로 설정합니다.
TAGO 첫차/막차를 중간 정류장 상세 시간표로 오인하지 않습니다.

도로 GeoJSON: WGS84 FeatureCollection, geometry=LineString.
각 properties에는 linkid(ITS 표준링크 ID)가 필수입니다.
선택 properties: routeid, from_seq, to_seq.
이 세 필드를 함께 제공하면 해당 노선의 인접 정류장 사이 도로와
방향이 검증된 연결표로 사용합니다. 한 정류장 구간이 여러 도로 링크이면
같은 from_seq/to_seq로 각각 등록합니다. 도로는 실제 표준노드링크 형상을
사용하고 정류장 직선을 도로 형상으로 대신 입력하지 않습니다.
노선별 전 구간을 빠짐없이 제공한 경우에만 도로속도 기반 ETA를 계산합니다.
ITS 소통정보 미제공/10분 초과 오래된 정보/잘못된 시각은 회색 표시합니다.
색 기준: <15 정체, <30 서행, >=30 원활(km/h) — 앱 자체 기준.
ITS 제공 범위 밖의 군도/농어촌도로는 정보를 확보할 수 없을 수 있습니다.
"""
from __future__ import annotations

import html
import math
import re
import io
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import unquote
import xml.etree.ElementTree as ET

import pandas as pd
import pydeck as pdk
import requests
import streamlit as st

st.set_page_config(page_title="금산 버스 길잡이", page_icon="🚌", layout="wide")
KST = ZoneInfo("Asia/Seoul")
OFFICIAL = "https://www.geumsan.go.kr/kr/html/sub05/05050101.html"
BASE = "https://apis.data.go.kr/1613000/"


class DataError(Exception):
    """사용자에게 공개해도 안전한 데이터 오류만 저장."""


def config(name, default=""):
    try:
        return str(st.secrets.get(name, default)).strip()
    except (FileNotFoundError, KeyError):
        return default


def now():
    return datetime.now(KST)


def number(value, default=None):
    try:
        v = float(value)
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        return default


def as_list(value):
    if isinstance(value, list):
        return value
    if isinstance(value, dict) and value:
        return [value]
    return []


def fetch(url, params=None):
    try:
        r = requests.get(url, params=params, timeout=(5, 25))
        r.raise_for_status()
        if len(r.content) > 25_000_000:
            raise DataError("데이터가 25MB를 초과합니다. 금산 지역으로 축소하세요.")
        return r
    except requests.RequestException:
        # 원본 예외에는 키가 포함된 요청 URL이 노출될 수 있습니다.
        raise DataError("데이터 서버 연결 실패: 인증 승인, 제공 범위, 네트워크를 확인하세요.") from None


@st.cache_data(ttl=25, show_spinner=False)
def tago(service, operation, key, **params):
    """페이지 누락을 방지하고 XML 오류 응답도 키 노출 없이 처리."""
    result = []
    for page in range(1, 101):
        r = fetch(BASE + service + "/" + operation, dict(
            serviceKey=unquote(key), _type="json", pageNo=page,
            numOfRows=1000, **params))
        try:
            response = r.json()["response"]
            header = response["header"]
            if str(header.get("resultCode")) not in ("00", "0"):
                raise DataError("TAGO 응답 오류 (코드 " + str(header.get("resultCode")) + "). 활용신청 승인을 확인하세요.")
            body = response.get("body") or {}
            container = body.get("items") or {}
            rows = as_list(container.get("item")) if isinstance(container, dict) else []
            result.extend(rows)
            total = number(body.get("totalCount"))
            if (total is not None and len(result) >= total) or len(rows) < 1000:
                return result
        except DataError:
            raise
        except (ValueError, KeyError, TypeError, AttributeError):
            raise DataError("TAGO 응답을 읽을 수 없습니다. 인증키와 API별 활용 승인을 확인하세요.") from None
    raise DataError("검색 결과가 너무 많습니다. 관리자에게 지역 데이터 범위 점검을 요청하세요.")


@st.cache_data(ttl=3600, show_spinner=False)
def city_code(key):
    if config("CITY_CODE"):
        return config("CITY_CODE")
    rows = tago("BusSttnInfoInqireService", "getCtyCodeList", key)
    candidates = [r for r in rows if "금산" in str(r.get("cityname", ""))]
    if len(candidates) != 1:
        raise DataError("TAGO 도시목록에서 금산군을 확인하지 못했습니다. 금산군 제공 여부 또는 CITY_CODE 설정을 확인하세요.")
    return str(candidates[0]["citycode"])


@st.cache_data(ttl=3600, show_spinner=False)
def stops_data(key, city):
    rows = tago("BusSttnInfoInqireService", "getSttnNoList", key, cityCode=city)
    return sorted(rows, key=lambda r: (str(r.get("nodenm", "")), str(r.get("nodeid", ""))))


@st.cache_data(ttl=3600, show_spinner=False)
def route_stops(key, city, route):
    return sorted(tago("BusRouteInfoInqireService", "getRouteAcctoThrghSttnList", key,
                       cityCode=city, routeId=route), key=lambda r: number(r.get("nodeord"), 0))


def ordered_pairs(rows, start, end):
    return [(i, j) for i, a in enumerate(rows) for j, b in enumerate(rows)
            if str(a.get("nodeid")) == start and str(b.get("nodeid")) == end and j > i]


@st.cache_data(ttl=3600, show_spinner=False)
def options_for(key, city, start, end):
    routes = tago("BusSttnInfoInqireService", "getSttnThrghRouteList", key,
                  cityCode=city, nodeId=start)
    options, failures, seen = [], 0, set()
    for route in routes:
        rid = str(route.get("routeid", ""))
        if not rid or rid in seen:
            continue
        seen.add(rid)
        try:
            rows = route_stops(key, city, rid)
            for i, j in ordered_pairs(rows, start, end):
                options.append(dict(route=route, stops=rows, i=i, j=j))
        except DataError:
            failures += 1
    return options, failures


def stop_label(s):
    return f"{s.get('nodenm', '이름 없음')} · {s.get('nodeid', '')}"


def option_label(o):
    r, rows = o["route"], o["stops"]
    return (f"{r.get('routeno', '?')}번 · {r.get('endnodenm') or rows[-1].get('nodenm', '')} 방면"
            f" · 승차 {rows[o['i']].get('nodeord')}→하차 {rows[o['j']].get('nodeord')}"
            f" · {r.get('routeid')}")


def demo_bundle():
    # 가상 명칭/좌표/노선/시각. 실제 금산 운행 데이터가 아닙니다.
    rows = [dict(nodeid=f"DEMO{i}", nodenm=f"시연 정류장 {i+1}", nodeord=i+1,
                 gpslati=36.10+i*.009, gpslong=127.46+i*.007) for i in range(6)]
    return rows, dict(route=dict(routeid="DEMO900", routeno="가상 900", endnodenm=rows[-1]["nodenm"]),
                      stops=rows, i=0, j=5)


@st.cache_data(ttl=300, show_spinner=False)
def load_schedule(url):
    try:
        df = pd.read_csv(io.StringIO(fetch(url).content.decode("utf-8-sig")), dtype=str).fillna("")
        needed = {"routeid", "nodeid", "trip_id", "service_date", "departure_time"}
        if not needed.issubset(df.columns):
            raise DataError("시간표 CSV에 필수 열이 없습니다. app.py 상단 명세를 확인하세요.")
        for col in df.columns:
            df[col] = df[col].str.strip()
        return df
    except DataError:
        raise
    except (UnicodeError, ValueError, pd.errors.ParserError):
        raise DataError("시간표 CSV 형식 또는 UTF-8 인코딩을 확인하세요.") from None


def schedule_for(o, demo):
    rows, i, j = o["stops"], o["i"], o["j"]
    if demo:
        start = now().replace(second=0, microsecond=0) + timedelta(minutes=7)
        return pd.DataFrame([dict(운행=f"가상 운행 {n+1}", 승차=start+timedelta(minutes=n*60),
                                  하차=start+timedelta(minutes=n*60+(j-i)*3)) for n in range(3)])
    url = config("TIMETABLE_CSV_URL")
    if not url:
        return pd.DataFrame()
    df = load_schedule(url)
    df = df[(df.routeid == str(o["route"]["routeid"])) &
            (df.service_date == now().date().isoformat())]
    records = []
    repeated = len({str(x.get("nodeid")) for x in rows}) != len(rows)
    if repeated and "stop_seq" not in df:
        raise DataError("순환/중복 정류장 노선 시간표에는 stop_seq 열이 필요합니다.")
    for trip, group in df.groupby("trip_id"):
        a = group[group.nodeid == str(rows[i]["nodeid"])]
        b = group[group.nodeid == str(rows[j]["nodeid"])]
        if "stop_seq" in group:
            a = a[pd.to_numeric(a.stop_seq, errors="coerce") == number(rows[i]["nodeord"])]
            b = b[pd.to_numeric(b.stop_seq, errors="coerce") == number(rows[j]["nodeord"])]
        if len(a) != 1 or len(b) != 1:
            continue
        try:
            def parse(row):
                t = str(row.departure_time)
                if not re.fullmatch(r"\d{2}:\d{2}(:\d{2})?", t):
                    raise ValueError
                return datetime.fromisoformat(str(row.service_date)+"T"+t).replace(tzinfo=KST)
            departure, arrival = parse(a.iloc[0]), parse(b.iloc[0])
            if arrival <= departure:
                continue
            records.append(dict(운행=trip, 승차=departure, 하차=arrival))
        except ValueError:
            continue
    return pd.DataFrame(records).sort_values("승차") if records else pd.DataFrame()


@st.cache_data(ttl=300, show_spinner=False)
def road_geometry(url):
    try:
        geo = fetch(url).json()
        if geo.get("type") != "FeatureCollection":
            raise ValueError
        features = geo.get("features", [])
        for feature in features:
            g, p = feature["geometry"], feature["properties"]
            if g["type"] != "LineString" or not str(p.get("linkid", "")):
                raise ValueError
            if len(g["coordinates"]) < 2:
                raise ValueError
            for coord in g["coordinates"]:
                x, y = float(coord[0]), float(coord[1])
                if not (124 <= x <= 132 and 33 <= y <= 39):
                    raise ValueError
        return features
    except DataError:
        raise
    except (ValueError, KeyError, TypeError):
        raise DataError("도로 GeoJSON 형식이 잘못되었습니다. WGS84 LineString과 linkid를 확인하세요.") from None


@st.cache_data(ttl=60, show_spinner=False)
def its_data(key, bounds):
    r = fetch("https://openapi.its.go.kr/trafficInfo", dict(apiKey=key, type="all",
              minX=bounds[0], maxX=bounds[1], minY=bounds[2], maxY=bounds[3], getType="xml"))
    try:
        root = ET.fromstring(r.content)
        code = root.findtext(".//resultCode")
        if code not in ("0", "00"):
            raise DataError("ITS 응답 오류: 인증 승인 또는 소통정보 제공 상태를 확인하세요.")
        return {str(x.findtext("linkId")): {c.tag: c.text for c in x}
                for x in root.findall(".//item")}
    except ET.ParseError:
        raise DataError("ITS XML 응답을 읽지 못했습니다.") from None


def fresh_speed(record):
    try:
        stamp = datetime.strptime(record.get("createdDate", ""), "%Y%m%d%H%M%S").replace(tzinfo=KST)
        age = (now() - stamp).total_seconds()
        speed = number(record.get("speed"))
        if not (0 <= age <= 600) or speed is None or not 0 <= speed <= 160:
            return None
        return speed
    except (ValueError, TypeError):
        return None


def haversine(a, b):
    x1, y1, x2, y2 = map(math.radians, [a[0], a[1], b[0], b[1]])
    d = math.sin((y2-y1)/2)**2 + math.cos(y1)*math.cos(y2)*math.sin((x2-x1)/2)**2
    return 6371 * 2 * math.asin(min(1, math.sqrt(d)))


def traffic_for(o, demo):
    if demo:
        features, traffic = [], {}
        for i in range(len(o["stops"])-1):
            a, b = o["stops"][i:i+2]
            lid = f"DEMO_LINK{i}"
            features.append(dict(geometry=dict(type="LineString", coordinates=[
                [a["gpslong"], a["gpslati"]], [b["gpslong"], b["gpslati"]]]),
                properties=dict(linkid=lid, routeid="DEMO900", from_seq=i+1, to_seq=i+2)))
            traffic[lid] = dict(speed=[35, 22, 9, 31, 18][i], createdDate=now().strftime("%Y%m%d%H%M%S"), roadName="가상 도로")
        return features, traffic, "가상 도로/속도 시연 — 실제 교통정보 아님"
    key, url = config("ITS_API_KEY"), config("ROAD_GEOJSON_URL")
    if not key or not url:
        return [], {}, "실제 도로 색상에는 ITS_API_KEY와 ROAD_GEOJSON_URL이 모두 필요합니다."
    features = road_geometry(url)
    # 선택 경로 인근의 도로만 요청. 범위가 큰 전국 파일은 미리 금산 지역으로 잘라야 합니다.
    coords = [(number(r.get("gpslong")), number(r.get("gpslati"))) for r in o["stops"][o["i"]:o["j"]+1]]
    coords = [c for c in coords if None not in c]
    if not coords:
        return features, {}, "선택 노선 좌표가 없어 ITS 조회 범위를 만들 수 없습니다."
    bounds = (min(c[0] for c in coords)-.03, max(c[0] for c in coords)+.03,
              min(c[1] for c in coords)-.03, max(c[1] for c in coords)+.03)
    traffic = its_data(key, bounds)
    return features, traffic, "실제 ITS 소통정보 · 수집시각 10분 이내만 사용 · 회색은 미제공/오래된 정보"


def road_rows(features, traffic):
    result = []
    for f in features:
        p = f["properties"]
        item = traffic.get(str(p["linkid"]), {})
        speed = fresh_speed(item)
        color = [148, 163, 184] if speed is None else ([220, 38, 38] if speed < 15 else [245, 158, 11] if speed < 30 else [5, 150, 105])
        status = "정보 없음" if speed is None else "정체" if speed < 15 else "서행" if speed < 30 else "원활"
        result.append(dict(path=f["geometry"]["coordinates"], color=color, linkid=str(p["linkid"]),
            road=str(item.get("roadName") or "도로"), status=status,
            speed_text="—" if speed is None else f"{speed:g} km/h", speed=speed,
            created=str(item.get("createdDate") or "—"), props=p))
    return result


def road_eta(o, roads):
    total = 0.0
    for a, b in zip(o["stops"][o["i"]:o["j"]], o["stops"][o["i"]+1:o["j"]+1]):
        group = [r for r in roads if str(r["props"].get("routeid")) == str(o["route"]["routeid"])
                 and number(r["props"].get("from_seq")) == number(a.get("nodeord"))
                 and number(r["props"].get("to_seq")) == number(b.get("nodeord"))]
        if not group or any(r["speed"] is None or r["speed"] <= 0 for r in group):
            return None
        if len({r["linkid"] for r in group}) != len(group):
            return None
        for r in group:
            km = sum(haversine(x, y) for x, y in zip(r["path"], r["path"][1:]))
            total += km / r["speed"] * 60
        total += .5  # 정류장당 정차 30초라는 명시적 가정
    return total


def safe_call(fn, fallback):
    try:
        return fn()
    except DataError as e:
        st.warning(str(e))
        return fallback


st.markdown("""<style>
.stApp {background:#f5f7fb;color:#172b4d}
.block-container {max-width:1120px;padding-top:1.6rem;padding-bottom:3rem}
h1,h2,h3 {letter-spacing:-.04em}
[data-testid="stVerticalBlockBorderWrapper"] {border-radius:18px}
.bus-pin {position:fixed;top:3.75rem;left:50%;transform:translateX(-50%);
 width:min(1080px,calc(100% - 2rem));z-index:990;background:#123c36;color:white;
 border-radius:16px;padding:14px 20px;box-shadow:0 5px 22px #143c3620;
 display:flex;justify-content:space-between;gap:12px;align-items:center}
.bus-pin strong {font-size:24px;color:#bff4db}.bus-pin small{display:block;color:#dbefe9;font-size:13px}
.pin-spacer{height:105px}
[data-testid="stMetricValue"] {color:#087e68}
@media(max-width:640px){.bus-pin{top:3.6rem;padding:10px 14px;align-items:flex-start}
.bus-pin strong{font-size:19px}.pin-spacer{height:145px}.bus-pin small{font-size:12px}}
</style>""", unsafe_allow_html=True)
st.markdown('<div class="pin-spacer"></div>', unsafe_allow_html=True)
st.title("🚌 금산 버스 길잡이")
st.caption("정류장 두 곳과 버스 하나. 필요한 정보부터 크게 보여드립니다.")
key = config("TAGO_API_KEY")
demo = not bool(key)
if demo:
    st.warning("시연 모드 · 가상 노선/정류장/대기시간/교통속도입니다. 실제 버스 이용에 사용하지 마세요.")
    stops, prototype = demo_bundle()
    city = "DEMO"
else:
    with st.spinner("금산군 정류장 정보를 확인하고 있습니다…"):
        try:
            city = city_code(key)
            stops = stops_data(key, city)
        except DataError as e:
            st.error(str(e))
            st.info("인증키를 설정했다고 금산군 실시간 데이터 제공이 보장되지는 않습니다. 제공기관의 지역 지원을 확인하세요.")
            st.stop()
    if not stops:
        st.error("금산군 정류장 데이터가 없습니다. 제공기관의 지원 범위를 확인하세요.")
        st.stop()

with st.container(border=True):
    st.subheader("어디로 가시나요?")
    c1, c2 = st.columns(2)
    with c1:
        start_idx = st.selectbox("① 내가 있는 정류장", range(len(stops)),
                                format_func=lambda i: stop_label(stops[i]), index=None,
                                placeholder="이름으로 검색하고 선택하세요", key="start")
    destination_choices = [i for i in range(len(stops)) if i != start_idx]
    with c2:
        end_idx = st.selectbox("② 내가 가야 하는 정류장", destination_choices,
                              format_func=lambda i: stop_label(stops[i]), index=None,
                              placeholder="도착 정류장을 선택하세요", disabled=start_idx is None,
                              key=f"end_{start_idx}")
    options, failures = [], 0
    if start_idx is not None and end_idx is not None:
        if demo:
            if end_idx > start_idx:
                options = [dict(prototype, i=start_idx, j=end_idx)]
        else:
            with st.spinner("두 정류장을 순서대로 지나는 버스를 찾고 있습니다…"):
                options, failures = safe_call(lambda: options_for(
                    key, city, str(stops[start_idx]["nodeid"]), str(stops[end_idx]["nodeid"])), ([], 0))
    choice = st.selectbox("③ 타야 하는 버스 번호", range(len(options)),
                          format_func=lambda i: option_label(options[i]), index=None,
                          placeholder="버스 번호로 검색하고 선택하세요", disabled=not options,
                          key=f"route_{start_idx}_{end_idx}")
    if failures:
        st.warning(f"{failures}개 노선의 경유정보를 조회하지 못해 결과가 일부 누락될 수 있습니다.")
    if start_idx is not None and end_idx is not None and not options:
        st.info("이 방향으로 직통 운행하는 버스를 확인하지 못했습니다. 반대편 정류장, 방향, 제공 범위를 확인하세요. 환승 검색은 지원하지 않습니다.")


def pin(title, wait, location, caption):
    esc = lambda s: html.escape(str(s))
    st.markdown(f'<div class="bus-pin" role="status"><div><small>{esc(title)}</small>'
                f'<strong>{esc(wait)}</strong></div><div><b>{esc(location)}</b>'
                f'<small>{esc(caption)}</small></div></div>', unsafe_allow_html=True)


if choice is None:
    pin("금산 버스 길잡이", "정류장과 버스를 선택하세요", "① 출발 → ② 도착 → ③ 버스", "선택 후 위치와 대기시간이 이곳에 고정됩니다")
    st.info("위의 세 단계로 입력하면 시간표 · 대기시간 · 이동시간 예측 · 도로 소통정보를 확인할 수 있습니다.")
else:
    o = options[choice]
    rows, i, j, route = o["stops"], o["i"], o["j"], o["route"]
    rid = str(route["routeid"])

    @st.fragment(run_every="30s")
    def live_panel():
        # 조각 새로고침으로 입력은 유지하고 API 캐시 TTL에 따라 갱신합니다.
        st.caption("화면 30초 자동 갱신 · API 결과는 최대 25초, ITS는 최대 60초 캐시")
        arrivals, positions = [], []
        if demo:
            arrivals = [dict(routeid=rid, arrtime=420, arrprevstationcnt=2)]
            positions = [dict(nodenm="시연 정류장 1", nodeord=1,
                              gpslati=rows[0]["gpslati"], gpslong=rows[0]["gpslong"], vehicleno="가상 차량")]
        else:
            arrivals = safe_call(lambda: tago("ArvlInfoInqireService", "getSttnAcctoArvlPrearngeInfoList",
                key, cityCode=city, nodeId=str(rows[i]["nodeid"])), [])
            positions = safe_call(lambda: tago("BusLcInfoInqireService", "getRouteAcctoBusLcList",
                key, cityCode=city, routeId=rid), [])
        arrivals = sorted([a for a in arrivals if str(a.get("routeid")) == rid
                           and number(a.get("arrtime")) is not None and number(a.get("arrtime")) >= 0],
                          key=lambda a: number(a["arrtime"]))
        wait = math.ceil(number(arrivals[0]["arrtime"]) / 60) if arrivals else None
        # 경유 순번은 해당 방향 노선의 순서로 비교합니다. ETA 차량과는 식별 매칭 불가.
        upstream = [p for p in positions if number(p.get("nodeord")) is not None
                    and number(p.get("nodeord")) <= number(rows[i].get("nodeord"), -1)]
        nearest = max(upstream, key=lambda p: number(p["nodeord"])) if upstream else None
        location = f"관측 차량: {nearest.get('nodenm', '위치 확인 중')} 인근" if nearest else "승차 전 차량 위치 확인 불가"
        pin(f"{'[시연] ' if demo else ''}{route.get('routeno')}번 · {rows[i]['nodenm']} → {rows[j]['nodenm']}",
            (f"약 {wait}분 후 도착" if wait else "곧 도착") if wait is not None else "도착예정 정보 없음",
            location, "도착예정 차량과 위치 관측 차량은 다를 수 있음 · 조회 " + now().strftime("%H:%M:%S"))
        schedule = safe_call(lambda: schedule_for(o, demo), pd.DataFrame())
        features, traffic, traffic_note = safe_call(lambda: traffic_for(o, demo), ([], {}, "도로 소통정보를 불러오지 못했습니다."))
        roads = road_rows(features, traffic)
        travel = road_eta(o, roads)
        tabs = st.tabs(["🗓 시간표 안내", "⏱ 대기시간", "🎯 이동시간 예측", "🚦 도로 소통"])
        with tabs[0]:
            st.subheader("오늘의 시간표")
            if not schedule.empty:
                display = schedule.copy()
                display["상태"] = display["승차"].map(lambda t: "출발시각 지남" if t < now() else "예정")
                for col in ("승차", "하차"):
                    display[col] = display[col].map(lambda t: t.strftime("%H:%M"))
                st.dataframe(display, hide_index=True, use_container_width=True)
                st.caption("정류장별 계획 통과시각이며 실시간 도착예정과 다릅니다. 표시 날짜: " + now().date().isoformat())
            else:
                st.info("오늘 해당 방향의 정류장별 시간표가 등록되지 않았습니다. 첫차·막차만으로 전체 시간표를 생성하지 않습니다.")
            st.link_button("금산군 공식 농어촌버스 시간표 확인", OFFICIAL)
            st.caption("공식 안내의 시행일과 현재 유효 여부를 확인하세요. 공휴일·방학·임시 변경은 운수사에 확인이 필요합니다.")
        with tabs[1]:
            st.subheader("내 정류장까지 얼마나 남았나요?")
            c1, c2, c3 = st.columns(3)
            c1.metric("예상 대기", f"{wait}분" if wait is not None else "정보 없음")
            c2.metric("남은 정류장", str(arrivals[0].get("arrprevstationcnt", "—")) if arrivals else "—")
            c3.metric("노선 위치 관측 차량", f"{len(positions)}대")
            st.caption("도착정보가 없다는 것은 운행 종료나 버스가 없음을 의미하지 않습니다. TAGO 도착예정과 위치 응답은 차량 ID로 연결되지 않아 동일 차량이라고 단정하지 않습니다.")
            if positions:
                table = [dict(차량=str(p.get("vehicleno", "미제공")), 정류장=p.get("nodenm", "미제공"),
                              경유순번=p.get("nodeord", "미제공")) for p in positions]
                st.dataframe(pd.DataFrame(table), hide_index=True, use_container_width=True)
            st.caption("농어촌 통신 사정과 기관 수집 주기에 따라 실제 위치가 늦게 반영될 수 있습니다.")
        with tabs[2]:
            st.subheader("목적지까지 걸리는 시간")
            basis = ""
            estimated = travel
            if estimated is not None:
                basis = "실제 도로 소통속도 + 정류장 구간당 정차 30초 가정 (신호·승하차·운행대기 오차 있음)"
            else:
                upcoming = schedule[schedule["승차"] >= now()] if not schedule.empty else pd.DataFrame()
                if not upcoming.empty:
                    row = upcoming.iloc[0]
                    estimated = (row["하차"] - row["승차"]).total_seconds()/60
                    basis = "다음 예정 운행의 정류장별 시간표 차이 · 실시간 교통 미반영"
            if estimated is None:
                st.info("예측 근거가 부족합니다. 전체 승하차 구간의 검증된 도로 링크·소통정보 또는 정류장별 시간표가 필요합니다. 임의 평균속도로 예측하지 않습니다.")
            else:
                c1, c2, c3 = st.columns(3)
                c1.metric("탑승 후 이동", f"약 {math.ceil(estimated)}분")
                c2.metric("지금부터 총 소요", f"약 {math.ceil(estimated+wait)}분" if wait is not None else "대기시간 미확인")
                c3.metric("목적지 도착 참고시각", (now()+timedelta(minutes=estimated+wait)).strftime("%H:%M") if wait is not None else "—")
                st.caption(basis)
                if travel is None:
                    st.warning("시간표 기준 이동시간과 실시간 대기시간은 서로 다른 운행 차량일 수 있습니다. 총 소요와 도착시각은 참고값입니다.")
            st.caption(f"선택 경유 구간: {j-i}개 · 반복 정류장은 경유 순번으로 구분합니다.")
        with tabs[3]:
            st.subheader("도로 구간별 소통정보")
            st.caption(traffic_note)
            st.markdown("🟢 원활 30km/h 이상　 🟠 서행 15–30km/h 미만　 🔴 정체 15km/h 미만　 ⚪ 정보 없음")
            st.caption("색은 앱 자체 속도 기준이며 ITS 공식 혼잡등급이 아닙니다. 노선 경로와 도로의 정확한 연결은 관리자 검증이 필요합니다.")
            stop_points = []
            for idx in range(i, j+1):
                s = rows[idx]
                x, y = number(s.get("gpslong")), number(s.get("gpslati"))
                if x is not None and y is not None:
                    stop_points.append(dict(lon=x, lat=y, road=str(s["nodenm"]), status="정류장", speed_text="—", linkid="—", created="—"))
            bus_points = []
            for p in positions:
                x, y = number(p.get("gpslong")), number(p.get("gpslati"))
                if x is not None and y is not None:
                    bus_points.append(dict(lon=x, lat=y, road="버스 · "+str(p.get("nodenm", "위치 관측")), status="차량 관측", speed_text="—", linkid="—", created="—"))
            layers = []
            if roads:
                layers.append(pdk.Layer("PathLayer", data=[{k:v for k,v in r.items() if k != "props"} for r in roads],
                    get_path="path", get_color="color", get_width=6, width_units="pixels", pickable=True))
            for points, color, radius in [(stop_points, [15, 118, 110], 65), (bus_points, [37, 99, 235], 100)]:
                if points:
                    layers.append(pdk.Layer("ScatterplotLayer", data=points, get_position="[lon, lat]",
                        get_fill_color=color, get_radius=radius, radius_min_pixels=5, pickable=True))
            if stop_points or bus_points or roads:
                center = stop_points or bus_points
                if center:
                    lat, lon = center[0]["lat"], center[0]["lon"]
                else:
                    lon, lat = roads[0]["path"][0][:2]
                st.pydeck_chart(pdk.Deck(map_style="https://basemaps.cartocdn.com/gl/positron-gl-style/style.json",
                    initial_view_state=pdk.ViewState(latitude=lat, longitude=lon, zoom=11), layers=layers,
                    tooltip={"text":"{road}\n{status} · {speed_text}\n링크 {linkid}\n수집 {created}"}), use_container_width=True)
                st.caption("청록 점=정류장 · 파랑 점=노선 관측 버스 · 도로 형상이 없으면 정류장 간 직선을 혼잡 도로처럼 그리지 않습니다. 지도 타일은 외부 인터넷 연결이 필요합니다.")
            else:
                st.info("표시할 좌표 또는 도로 형상이 없습니다.")
            if roads:
                st.dataframe(pd.DataFrame([dict(도로=r["road"], 링크=r["linkid"], 상태=r["status"],
                    속도=r["speed_text"], 수집시각=r["created"]) for r in roads]), hide_index=True, use_container_width=True)

    live_panel()

with st.expander("설치·실데이터 연결 안내", expanded=False):
    st.markdown("""
**1. 배포**  
GitHub 공개 또는 비공개 저장소 루트에 `app.py`, `requirements.txt`를 올린 뒤
Streamlit Community Cloud에서 저장소와 `app.py`를 선택해 배포하세요.
로컬 실행: `pip install -r requirements.txt` → `streamlit run app.py`.

**2. 버스 API**  
공공데이터포털에서 국토교통부(TAGO) **버스정류소정보 · 버스노선정보 ·
버스위치정보 · 버스도착정보**를 각각 활용신청하세요.
Cloud의 App settings → Secrets에 아래 최상위 키를 설정합니다.
Decoding 키를 권장하며 Encoding 키도 한 번 디코딩해 전달합니다.
인증키를 코드나 GitHub에 저장하지 마세요.

**3. 실제 도로 색상**  
ITS 국가교통정보센터에서 교통소통정보 API 키를 신청하고, 금산 지역의
실제 표준링크 도로 형상을 GeoJSON URL로 준비해야 합니다.
ITS의 속도 응답만으로는 도로 형상을 얻을 수 없습니다.
전 구간의 `routeid/from_seq/to_seq` 연결표가 있어야 도로 기반 이동시간을 계산합니다.
데이터 미제공 군도는 회색이며 금산군 전 도로의 실시간 수집을 보장하지 않습니다.

**4. 상세 시간표**  
검증된 공식 시간표를 정류장별 CSV로 준비해 `TIMETABLE_CSV_URL`로 연결합니다.
파일 형식은 app.py 맨 위 설명을 참고하세요. 키만으로 상세 시간표가 생성되지는 않습니다.
승객 화면의 입력은 세 가지로 한정하고 데이터 설정은 관리자 Secrets에서만 관리합니다.
""")
    st.code('TAGO_API_KEY = "발급받은 인증키"\nITS_API_KEY = "발급받은 인증키"\n# 선택: 실제 데이터를 준비한 후 설정\n# TIMETABLE_CSV_URL = "https://.../timetable.csv"\n# ROAD_GEOJSON_URL = "https://.../roads.geojson"', language="toml")
    st.markdown("[TAGO 도착정보 안내](https://www.data.go.kr/data/15098530/openapi.do) · [TAGO Open API 안내](https://www.tago.go.kr/use/openapi) · [ITS 오픈데이터](https://www.its.go.kr/opendata/) · [금산군 공식 시간표]("+OFFICIAL+")")
