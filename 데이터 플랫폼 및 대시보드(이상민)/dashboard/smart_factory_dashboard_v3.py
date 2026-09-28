"""
스마트 팩토리 실시간 관제 대시보드 (v3 파일명 유지, 내용은 v7)

v1/v2 대비 개선 사항
--------------------
1. 테이블 통일
   - manage_smart_factory_db.py 가 사용하던 patrol_zones 테이블을 없애고
     patrol_logs 테이블 하나로 통일했습니다.
   - 이제 "구역 마스터 정보(로봇 담당 구역, 로봇 ID)"와 "로그(감지 이벤트)"를
     명확히 분리된 두 테이블로 관리합니다. (patrol_zones = 구역/로봇 마스터,
     patrol_logs = 실제 감지 로그) 이렇게 하면 관리(CRUD)용 스크립트와
     대시보드가 같은 스키마를 보게 됩니다.
   - (v4) 이 테이블 정의(DDL) 자체를 common/schema.py 로 옮겨서, pipeline과
     integration 어댑터가 각자 다른 스키마를 만들어내는 일이 없도록 했습니다.

2. init_db() 캐싱 함정 수정
   - v2에서는 @st.cache_resource 가 실패 시 반환값(False)까지 캐싱해서,
     DB가 잠깐 안 떠 있다가 정상화돼도 앱이 영원히 "실패" 상태로 남는
     문제가 있었습니다. v3부터는 예외를 그대로 올려 캐싱되지 않게 합니다.

3. 커넥션/커서 안전 관리 — 모든 DB 접근을 try/finally 로 감쌌습니다.

4. sleep + st.rerun() 무한 루프 제거 — st.fragment(run_every=...) 사용.

5. (v6) 영상(젯슨 직접) + SLAM 미니맵(다은님 노트북) 통합
       젯슨(VLM 추론 + 박스 검출) --영상(8500)--> 대시보드
       젯슨 --UDP(각도, 9091)--> 다은님 노트북(SLAM 융합) --미니맵(8091)--> 대시보드

6. (v7) 화면 구조 개편 — "무엇을 먼저 보여줄 것인가"
   -------------------------------------------------------------
   v6까지의 배치는 아래와 같았고, 관제 화면으로서 순서가 거꾸로였습니다.

       제목 → [영상 | 미니맵] 520px → 메트릭 4개 → 구역 카드
       사이드바: IP 입력 + 상세 로그 표(6열)

   고친 것 6가지:

   (1) 경보를 맨 위로 올렸습니다.
       예전에는 제목(80px) + 영상 블록(520px + 캡션) 약 700px를 지나야
       "위험 몇 건"이 나왔습니다. 1080p 모니터에서 겨우 걸치고, 노트북
       (768p)에서는 구역 카드가 아예 스크롤해야 보였습니다. 지금은
       경보 배너 → 구역 카드 → 영상 순서입니다.

   (2) 미니맵에 본문 전체 폭을 줍니다.
       minimap_web.html 은 폭 900px 아래에서 좁은 화면용 레이아웃으로
       접히는데(탐지 목록이 지도 밑으로 내려가 44vh를 먹음), st.columns(2)
       안에서는 1920px 모니터에서도 약 745px밖에 안 됐습니다. 즉 대시보드
       안에서 한 번도 설계대로 뜬 적이 없었습니다. 이제 미니맵은
       render_minimap()으로 한 줄을 통째로 씁니다.

   (3) 상세 로그를 사이드바에서 본문으로 옮겼습니다.
       6열짜리 표(특히 한글 문장이 들어가는 '사유' 열)를 300px 사이드바에
       넣으면 열당 50px입니다. 사이드바에는 설정만 남겼습니다.

   (4) 위험 상태를 색조가 아니라 "반전"으로 표시합니다.
       정상/주의는 옅은 배경 + 진한 글씨인데, 위험만 진한 빨강 배경 +
       흰 글씨로 뒤집힙니다. 색상 이름이 아니라 명암 관계가 달라지므로
       색각 이상이 있어도, 멀리서 봐도 구분됩니다. 여기에 위험 건수가
       있을 때만 상단 배너가 추가로 나타납니다.

   (5) 죽은 메트릭을 없앴습니다.
       "전체 구역 3개"는 상수인데 가장 눈에 띄는 첫 칸을 썼고, "정상 구역
       수"와 "미연결 로봇 수"는 바로 밑 구역 카드와 중복이었습니다.
       메트릭 4칸(약 140px)을 상태 바 한 줄(약 40px)로 줄였습니다.

   (6) response_plans 를 화면에 띄웁니다.
       RagResponseAgent 가 만든 대응안이 DB에만 쌓이고 화면 어디에도
       안 나왔습니다(v6까지의 알려진 이슈). 이제 영상 옆에 최신 1건을
       크게 보여주고, 아래 탭에서 전체 이력을 볼 수 있습니다.

   갱신 주기도 손봤습니다. 예전에는 두 영역이 각각 0.5초마다 도느라 초당
   6번 질의했는데(2쿼리+1쿼리 × 2회/초), 사람 반응 시간을 생각하면 1초와
   0.5초는 차이가 없습니다. 지금은 초당 약 3.2번으로 절반입니다.

7. (v8) 배치는 그대로 두고, "데이터가 화면에 도달하는 방식"을 고쳤습니다.
   -------------------------------------------------------------
   v7까지의 문제는 배치가 아니라 아래 다섯 가지였습니다.

   (1) 상세 감지 로그가 사실상 "최근 1초"였습니다.
       ResponsePlanThrottle 은 response_plans 에만 걸리고 patrol_logs 적재는
       억제하지 않습니다(patrol_pipeline_rag.py 참고). 젯슨이 30fps로 넣으면
       LIMIT 30 은 1초치입니다. 시각을 0.1초 단위까지 찍고 있었던 것 자체가
       그 증거였습니다. 이제 연속된 같은 (객체, 위험도) 구간을 한 줄로 접어서
       "시작~종료 · N건 · 최근접 거리"로 보여줍니다(감지 요약). 프레임 단위로
       봐야 할 때를 위해 "원본 로그" 보기를 따로 남겼습니다.

   (2) 화면에서 제일 중요한 영역에만 예외 처리가 없었습니다.
       대응안 패널은 try 로 감싸져 있는데 정작 경보/구역 카드를
       그리는 _render_status() 는 아니어서, DB가 잠깐 끊기면 화면 맨 위가
       빨간 파이썬 트레이스백으로 바뀌었습니다. 관제 화면에서 "정보 없음"과
       "이상 없음"은 반드시 구분돼야 하므로, 이제 회색 "상태 불명" 카드와
       배너를 그립니다. (커넥션 캐시는 get_cursor() 가 비우므로 다음 틱에
       저절로 복구됩니다.)

   (3) JETSON_IP / DAEUN_LAPTOP_IP 환경변수가 죽어 있었습니다.
       사이드바 기본값이 "localhost" 로 고정이었고, 받는 쪽 코드는
       `jetson_ip or JETSON_IP` 라서 "localhost"(truthy)에서 fallback 으로
       넘어갈 일이 없었습니다. 즉 환경변수를 설정해도 아무 일도 안 났습니다.
       이제 사이드바 기본값 자체를 환경변수에서 읽습니다.

   (4) 로봇이 죽어도 조용했습니다.
       '위험'은 배너 + 반전 + 펄스로 3중 강조인데 '미연결'은 회색 점선 카드
       하나뿐이었습니다. 순찰 로봇이 응답하지 않는 것은 위험 감지만큼 즉각
       대응이 필요한 사건이므로, 미연결에도 배너를 답니다(빨강과 구분되는
       주황). 한 번도 로그가 없는 구역과 오다가 끊긴 구역은 문구로 나눕니다.

   (5) '주의'가 상태 바에서 증발했습니다.
       상태 바가 위험/정상/미연결만 세어서, 세 구역이 전부 '주의'면
       "위험 0 · 정상 0/3 · 미연결 0" 이 되고 세 구역이 어디로 갔는지 설명이
       없었습니다. 주의 칸을 추가했습니다.

   함께 정리한 것
   - ZONES/TOTAL_ZONES 가 common/schema.py 의 DEFAULT_ZONES 를 복제하고
     있었습니다. 이 파일이 존재하는 이유가 그 drift 를 막기 위해서인데
     대시보드만 예외였습니다. 이제 import 해서 씁니다.
   - AI 권장 조치 카드에 날짜가 없어서, 어제 것과 5분 전 것이 똑같이 빨간
     테두리로 떠 있었습니다. 상대 시각을 붙이고, 오래된 대응안은 경보색을
     빼고 회색으로 낮춥니다.
   - '대응안 이력'을 볼 때 구역 라디오가 눌리기만 하고 표는 안 바뀌었습니다
     (fetch_plans 가 zone 을 안 봤습니다). 이제 로그/대응안 양쪽 모두 구역
     선택을 따르고, '전체' 선택지를 추가했습니다.
   - 표에서 '위험' 행을 눈으로 찾아야 했습니다. 위험도에 🔴/🟡/🟢 를 붙이고
     행 배경도 칠합니다(Styler 를 못 쓰는 환경이면 이모지만 남습니다).
   - st.dataframe(use_container_width=) 는 Streamlit 1.49 에서 deprecated 되고
     width="stretch" 로 대체됐습니다. requirements.txt 가 버전을 안 박아둬서
     새 PC에 설치하면 시연 당일 표가 깨질 수 있었습니다. 버전을 보고 알맞은
     인자를 고르도록 하고, requirements.txt 에도 하한을 넣었습니다.

8. (v9) 사용성 — "화면을 보고 있는 사람"을 전제하지 않기
   -------------------------------------------------------------
   v8까지는 정보가 정확하게 표시되는 것까지만 봤습니다. 실제로 관제석에 앉혀
   보면 남는 문제는 "그래서 사람이 무엇을 하느냐"였습니다.

   (1) 경보에 소리가 없었습니다.
       시각 경보만으로는 그 순간 화면을 보고 있는 사람이 필요합니다. 순찰
       로봇은 사람이 없는 시간대를 위해 도는 것이라 전제가 뒤집혀 있었습니다.
       사이드바에서 켜면 새 위험이 들어올 때만 짧은 경보음이 납니다. 브라우저
       자동재생 정책 때문에 기본값은 꺼짐입니다 — 체크박스를 켜는 클릭 자체가
       그 정책이 요구하는 "사용자 조작"이 됩니다.

   (2) 경보를 확인(acknowledge)할 방법이 없었습니다.
       마지막 위험 로그로부터 5분간, 담당자가 이미 조치했든 말든 배너가 계속
       떠 있었습니다. 계속 울리는 경보는 곧 무시하는 경보가 됩니다. 이제 배너에
       "확인" 버튼이 있고, 누르면 **그 시점까지의 위험**에 대해서만 배너와 소리가
       멈춥니다. 새 위험이 들어오면 다시 뜹니다. 구역 카드의 빨강은 그대로
       둡니다 — 확인은 "봤다"이지 "해결됐다"가 아니기 때문입니다.

   (3) 영상과 지도를 동시에 볼 수 없었습니다.
       미니맵에 전체 폭을 준 v5 판단은 맞지만(900px 미디어쿼리), 그 대가로
       1080p에서 영상과 지도가 같은 화면에 안 들어옵니다. 순찰 관제에서 "지금
       뭐가 보이나"와 "로봇이 어디 있나"는 같이 봐야 하는 짝입니다. 사이드바의
       화면 구성에서 '관제 집중'을 고르면 상세 조회를 접고 미니맵 높이를 줄여
       둘을 한 화면에 넣습니다. 폭은 그대로 전체를 쓰므로 미니맵이 좁은 화면용
       레이아웃으로 접히지 않습니다.

   (4) 조회 범위가 고정이었습니다.
       v8의 감지 요약은 최근 2000행(30fps 기준 약 1분)만 훑었습니다. "아까
       그거"가 3분 전이면 여전히 안 보입니다. 이제 1분/5분/30분 중에 고릅니다.
       스캔 상한도 함께 올라가지만, 넓은 범위는 갱신 주기를 늘려서 DB 부하가
       비례해서 늘지 않게 합니다.

   (5) 표를 밖으로 꺼낼 수 없었습니다.
       주간 보고나 발표자료에 넣으려면 화면을 캡처하는 수밖에 없었습니다.
       표마다 CSV 내려받기를 답니다. 인코딩은 utf-8-sig 입니다 — 그냥 utf-8로
       주면 Excel이 한글을 깨뜨립니다.

9. (v10) 영상 / 지도 크게 보기
   -------------------------------------------------------------
   1080p에서 영상은 좌측 5/12 칸이라 약 600px, 지도는 560px 높이입니다. 화면에
   무엇이 잡혔는지 확인하려면 그 크기로는 부족해서, 결국 브라우저로 젯슨
   주소를 따로 열어보게 됩니다. 둘 중 하나를 본문 전체에 띄우는 모드를 넣었습니다.

   (1) 조작 방식이 둘로 갈립니다 — 이게 설계의 핵심입니다.
       영상은 **그림을 직접 클릭**하면 확대됩니다. MJPEG 은 자체 조작이 없는
       정지영상 스트림이라 클릭을 가져가도 뺏기는 기능이 없습니다.
       지도는 **옆의 버튼으로만** 엽니다. iframe 안의 클릭은 minimap_web.html
       자신의 확대/이동/탐지목록 조작이라, 링크로 감싸면 미니맵을 못 쓰게 됩니다.

   (2) 상태를 session_state 가 아니라 쿼리 파라미터에 둡니다.
       HTML <img> 는 Streamlit 위젯이 아니라서 session_state 를 건드릴 방법이
       없습니다. <a href="?expand=video" target="_self"> 로 감싸면 브라우저가
       주소를 바꾸고 Streamlit 이 그걸 읽습니다. 둘 중 하나를 진실로 정해야
       하므로 쿼리 파라미터로 통일했습니다(주소창만 봐도 지금 무엇이 확대돼
       있는지 드러나고, 그 주소를 그대로 공유할 수도 있습니다).

   (3) 확대 중에도 경보와 구역 카드는 그대로 둡니다.
       영상을 크게 보려고 눌렀다가 그 사이 난 위험을 놓치면, 확대 기능이 사고
       원인이 됩니다. 접히는 것은 반대쪽 패널과 상세 조회뿐입니다.

   (4) 클릭이 막히더라도 기능이 죽지 않게 버튼을 함께 답니다.
       Streamlit 의 HTML 정화기가 버전에 따라 <a> 의 속성을 어떻게 다루는지에
       기대고 싶지 않았습니다. 영상 제목 옆의 "⛶ 크게" 버튼은 순수 Streamlit
       위젯이라 어떤 버전에서도 동작합니다.

10. (v11) 대응안 — 중요한 몇 개 + 이전 기록은 들어가서
   -------------------------------------------------------------
   (1) 영상 옆 패널이 최신 1건만 보여줬습니다.
       대응안은 (구역, 객체)마다 따로 나오므로, 두 구역에서 동시에 뭔가
       벌어지면 한쪽이 화면에서 통째로 사라집니다. 이제 첫 건을 크게,
       나머지 두 건을 한 줄씩 보여줍니다(PLAN_PANEL_LIMIT=3).

       요약 줄에는 권장 조치 **문장을 넣지 않습니다.** 세 줄이 다 문장이면
       어느 것이 지금 중요한지 눈으로 못 고릅니다. "어디서 무엇이"까지만
       보여주고 자세한 내용은 기록 화면에서 봅니다.

   (2) 무엇을 첫 칸에 둘지 — 최신순도 심각도순도 아닙니다.
       최신순만 쓰면 방금 들어온 '주의' 3건이 4분 전 '위험'을 밀어냅니다.
       심각도만 쓰면 3시간 전 '위험' 하나가 자리를 영원히 차지합니다(v8에서
       고친 "오래된 대응안이 경보처럼 보이는" 문제가 그대로 돌아옵니다).

       그래서 신선한 것 안에서만 심각도로 줄을 세웁니다.

           신선한 위험(2) > 신선한 주의(1) > 신선한 정상(0) > 오래된 것(-1)

       오래된 것끼리는 전부 -1 이라 뒤의 created_at DESC 로 최신순이 됩니다.
       즉 최근 5분에 아무 일도 없었으면 예전과 똑같이 동작합니다.

   (3) 이전 기록은 "들어가는" 화면으로 뺐습니다.
       패널의 "🗂 이전 기록 보기"를 누르면 ?expand=plans 로 들어갑니다(v10의
       크게 보기와 같은 방식). 거기서는 기간을 1시간/24시간/전체 기록 중에
       고르고 최대 200건을 봅니다. 상세 조회의 1분/5분/30분과 값이 다른 이유는
       대상 테이블이 다르기 때문입니다 — patrol_logs 는 30fps로 쌓여서 '전체'를
       열어줄 수 없지만, response_plans 는 ResponsePlanThrottle 이 걸려 있어
       전체를 훑어도 안전합니다.

       자동 갱신은 걸지 않습니다. 지나간 기록을 읽는 화면이라 몇 초마다 표가
       다시 그려지면 읽던 위치만 잃습니다.

   (4) 상세 조회에서 "AI 대응안 이력" 탭을 뺐습니다.
       같은 것을 보는 곳이 두 군데면 어느 쪽이 최신인지 매번 헷갈립니다.
       상세 조회는 이제 patrol_logs 만 다룹니다(감지 요약 / 원본 로그는 같은
       테이블을 다른 해상도로 보는 것이라 한 자리에 묶입니다).

   (5) 기록 화면의 필터는 상세 조회와 따로입니다(plan_zone / plan_span).
       로그를 A 구역으로 좁혀 놓았다고 기록 화면까지 A 구역으로 열리면,
       들어가자마자 "기록이 없다"를 보고 필터가 걸린 줄 모르게 됩니다.

필요한 st.secrets 키: db_user, db_password, db_host, db_port, db_name
"""

import base64
import html
import importlib.util
import io
import math
import os
import re
import struct
import sys
import time
import wave
from contextlib import contextmanager

import pandas as pd
import psycopg2
import streamlit as st
import streamlit.components.v1 as components
from psycopg2.extras import RealDictCursor

# common 패키지(공통 스키마 모듈)를 찾을 수 있도록 프로젝트 루트를 sys.path에 추가.
# Streamlit은 `streamlit run dashboard/smart_factory_dashboard_v3.py` 처럼
# 스크립트를 직접 실행하므로, 상대 import 대신 이 방식을 사용합니다.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from common.schema import DEFAULT_ZONES, OBJECT_KO_NAME, init_all_tables  # noqa: E402
from edge_video.dashboard_video_minimap_section import (  # noqa: E402
    MINIMAP_EXPANDED_HEIGHT,
    # 이 헬퍼는 미니맵 섹션 모듈에도 필요해서 그쪽으로 옮겼습니다. 여기서 다시
    # 정의하면 Streamlit 버전 판정이 두 벌이 되어 한쪽만 고치는 사고가 납니다.
    full_width_kwargs as _full_width_kwargs,
    render_minimap,
    render_patrol_control,
    render_robot_status,
    render_video,
)

# ==========================================
# 1. DB 연결 설정
# ==========================================
DB_USER = st.secrets["db_user"]
DB_PASSWORD = st.secrets["db_password"]
DB_HOST = st.secrets["db_host"]
DB_PORT = st.secrets["db_port"]
DB_NAME = st.secrets["db_name"]

# 구역 목록은 common/schema.py 가 단일 출처입니다. 예전에는 여기에 ["A","B","C"]
# 를 따로 적어둬서, 구역을 늘리면 DB/파이프라인만 알고 대시보드는 조용히 무시하는
# drift 가 생길 수 있었습니다 — 그 drift 를 막으라고 있는 모듈인데 대시보드만
# 예외였습니다.
ZONES = tuple(DEFAULT_ZONES)
TOTAL_ZONES = len(ZONES)

ACTIVE_THRESHOLD_SECONDS = 10
RECENT_ALERT_MINUTES = 5

DETAIL_ROW_LIMIT = 30
RAW_LOG_LIMIT = 60

# 이 시간이 지난 대응안은 "지금 벌어지는 일"이 아니므로 경보색을 뺍니다.
PLAN_FRESH_SECONDS = 300

# 영상 옆 패널에 보여줄 대응안 개수. 첫 건은 크게, 나머지는 한 줄씩입니다.
# 3보다 늘리면 영상과 높이가 안 맞아서 그 줄 전체가 길어집니다.
PLAN_PANEL_LIMIT = 3

# "이전 기록" 화면에서 한 번에 가져올 최대 건수. patrol_logs 와 달리
# response_plans 는 ResponsePlanThrottle 로 (구역, 객체) 조합당 30초에 1건까지만
# 쌓이므로, 200건이면 웬만한 시연 전체가 들어옵니다.
PLAN_HISTORY_LIMIT = 200

# 기록 화면의 기간 선택. 상세 조회의 DETAIL_SPANS 와 값이 다른 이유는 대상
# 테이블이 다르기 때문입니다 — patrol_logs 는 30fps로 쌓여서 '전체'를 열어줄 수
# 없지만, response_plans 는 억제가 걸려 있어 전체를 훑어도 안전합니다.
PLAN_SPAN_1H = "최근 1시간"
PLAN_SPAN_24H = "최근 24시간"
PLAN_SPAN_ALL = "전체 기록"
PLAN_SPAN_MINUTES = {PLAN_SPAN_1H: 60, PLAN_SPAN_24H: 60 * 24, PLAN_SPAN_ALL: None}
PLAN_SPAN_OPTIONS = [PLAN_SPAN_1H, PLAN_SPAN_24H, PLAN_SPAN_ALL]
DEFAULT_PLAN_SPAN = PLAN_SPAN_24H

# 상세 조회 선택지. 라디오 레이블 문자열로 분기하면 나중에 이모지 하나만 바꿔도
# 조용히 다른 화면이 뜨므로 상수로 묶어둡니다.
ZONE_ALL = "전체"
ZONE_OPTIONS = [ZONE_ALL] + list(ZONES)
VIEW_SUMMARY = "📋 감지 요약"
VIEW_RAW = "🔍 원본 로그"
# 상세 조회는 patrol_logs 만 다룹니다. 감지 요약과 원본 로그는 같은 테이블을 다른
# 해상도로 보는 것이라 한 자리에 묶이지만, 대응안은 테이블도 성격도 다릅니다.
# v10까지 여기에 "AI 대응안 이력" 탭이 같이 있었는데, v11에서 대응안 패널의
# "이전 기록" 화면으로 옮겼습니다 — 같은 것을 보는 곳이 두 군데면 어느 쪽이
# 최신인지 매번 헷갈립니다.
DETAIL_VIEWS = [VIEW_SUMMARY, VIEW_RAW]

# 조회 범위. (분, 스캔 상한 행, 갱신 주기).
#
# v8에서는 2000행 고정이었는데, 30fps 기준으로 약 1분입니다. "아까 그거"가 3분
# 전이면 여전히 안 보였습니다. 그렇다고 범위만 넓히면 2초마다 수만 행을 집계하게
# 되므로, 넓은 범위는 갱신 주기도 함께 늘립니다 — 30분치를 보고 있다는 것은 지금
# 흐르는 상황이 아니라 지나간 일을 읽고 있다는 뜻이라 1초 단위 갱신이 필요 없습니다.
#
# 두 가지로 함께 자릅니다. 시간(WHERE)은 화면에 적힌 범위를 정직하게 만들고,
# 행 수(LIMIT)는 트래픽이 갑자기 몰려도 집계량이 폭발하지 않게 막습니다.
SPAN_1M = "최근 1분"
SPAN_5M = "최근 5분"
SPAN_30M = "최근 30분"
DETAIL_SPANS = {
    SPAN_1M:  {"minutes": 1,  "scan_rows": 2000,  "refresh": 2.0},
    SPAN_5M:  {"minutes": 5,  "scan_rows": 8000,  "refresh": 3.0},
    SPAN_30M: {"minutes": 30, "scan_rows": 20000, "refresh": 6.0},
}
DETAIL_SPAN_OPTIONS = [SPAN_1M, SPAN_5M, SPAN_30M]
DEFAULT_SPAN = SPAN_5M

# 화면 구성. '관제 집중'은 상세 조회를 접고 미니맵을 낮춰서, 영상과 지도가 1080p
# 한 화면에 같이 들어오게 합니다. 폭은 그대로 전체를 쓰므로 미니맵이 좁은 화면용
# 레이아웃으로 접히지 않습니다(v5 주석 참고).
LAYOUT_FULL = "전체"
LAYOUT_FOCUS = "관제 집중"
LAYOUT_OPTIONS = [LAYOUT_FULL, LAYOUT_FOCUS]
MINIMAP_FOCUS_HEIGHT = 400

# 크게 보기(확대). 영상과 지도 중 하나를 본문 전체에 띄웁니다.
#
# 상태를 session_state 가 아니라 **쿼리 파라미터**에 둡니다. 영상은 클릭해서
# 확대하는데, HTML <img> 는 Streamlit 위젯이 아니라서 session_state 를 건드릴
# 방법이 없습니다. <a href="?expand=video"> 로 감싸면 브라우저가 주소를 바꾸고
# Streamlit 이 그걸 읽습니다. 둘 중 하나를 진실로 정해야 하므로 쿼리 파라미터로
# 통일했습니다(주소창을 보면 지금 무엇이 확대돼 있는지도 드러납니다).
EXPAND_PARAM = "expand"
EXPAND_VIDEO = "video"
EXPAND_MAP = "map"
EXPAND_PLANS = "plans"          # AI 대응안 이전 기록
EXPAND_PANELS = (EXPAND_VIDEO, EXPAND_MAP, EXPAND_PLANS)

# 갱신 주기. 예전에는 전 영역이 0.5초였는데, 사람이 화면 변화를 알아차리고
# 반응하는 데 최소 1초는 걸리므로 상태 영역은 1초로도 충분합니다. 대신
# patrol_logs 조회 횟수가 절반으로 줄어듭니다(브라우저 탭 수만큼 곱해집니다).
REFRESH_STATUS_SECONDS = 1.0
REFRESH_PLAN_SECONDS = 2.0
# 상세 조회는 선택한 범위에 따라 달라집니다(DETAIL_SPANS). 이 값은 세션 상태를
# 아직 못 읽었을 때의 기본값입니다.
REFRESH_DETAIL_SECONDS = DETAIL_SPANS[DEFAULT_SPAN]["refresh"]


def _expanded_panel():
    """지금 확대된 패널 이름, 없으면 None.

    st.query_params 는 Streamlit 1.30 부터입니다(requirements 하한이 1.33이라
    충족). 그래도 감싸 둡니다 — 여기서 예외가 나면 확대 기능 하나 때문에 관제
    화면 전체가 안 뜨게 되는데, 그건 맞바꿀 만한 거래가 아닙니다.
    """
    try:
        value = st.query_params.get(EXPAND_PARAM)
    except Exception:
        return None
    return value if value in EXPAND_PANELS else None


def _expand_link(panel) -> str:
    """확대/복귀용 상대 주소. panel 이 None 이면 확대를 푸는 주소입니다.

    "?" 만 남기면 브라우저가 같은 경로의 빈 쿼리로 이동하므로 파라미터가 사라집니다.
    """
    return f"?{EXPAND_PARAM}={panel}" if panel else "?"


def _set_expand(panel) -> None:
    """버튼용 콜백. 쿼리 파라미터를 직접 고칩니다.

    지도는 iframe 이라 링크로 감쌀 수 없습니다(감싸면 지도 자신의 확대/이동
    클릭을 뺏습니다). 그래서 지도 쪽은 이 콜백을 쓰는 버튼으로 엽니다.
    """
    try:
        if panel:
            st.query_params[EXPAND_PARAM] = panel
        elif EXPAND_PARAM in st.query_params:
            del st.query_params[EXPAND_PARAM]
    except Exception:
        pass


def _ago(seconds) -> str:
    """경과 초 -> "3분 전" 같은 상대 시각.

    시:분:초만 찍으면 어제 14:32 와 5분 전 14:32 를 구분할 수 없습니다.
    관제 화면에서 이 둘은 완전히 다른 의미입니다.

    None 뿐 아니라 NaN 도 받습니다. created_at 이 NULL 인 행이 하나라도 섞이면
    pandas 가 그 컬럼을 float 으로 만들면서 None 을 NaN 으로 바꿔놓기 때문에,
    None 검사만으로는 int(float(nan)) 에서 ValueError 로 표 전체가 날아갑니다.
    """
    try:
        s = int(float(seconds))
    except (TypeError, ValueError):
        return "시각 불명"
    if s < 0:          # DB 시계가 살짝 앞서는 경우
        return "방금"
    if s < 60:
        return "방금" if s < 10 else f"{s}초 전"
    if s < 3600:
        return f"{s // 60}분 전"
    if s < 86400:
        return f"{s // 3600}시간 전"
    return f"{s // 86400}일 전"


def _percent(value) -> str:
    """0.0~1.0 -> "85%". None/NaN 은 "-".

    float(nan) 은 예외를 던지지 않고 nan 을 그대로 돌려주므로 try/except 만으로는
    막히지 않습니다("nan%" 가 그대로 표에 찍힙니다). NaN 은 자기 자신과 같지
    않다는 성질로 거릅니다.
    """
    try:
        f = float(value)
    except (TypeError, ValueError):
        return "-"
    return "-" if f != f else f"{f:.0%}"


# ------------------------------------------
# 경보음 — 파일 없이 코드에서 만듭니다
# ------------------------------------------
# mp3/wav 파일을 저장소에 넣지 않는 이유는 edge_video 의 자리표시자 SVG와 같습니다:
# 폐쇄망이나 저장소를 일부만 받은 상태에서도 경보가 나야 하고, 바이너리는 리뷰가
# 안 됩니다. 880Hz / 660Hz 두 음을 번갈아 내는 짧은 소리를 표준 라이브러리로 굽습니다.
_BEEP_CACHE = {}


def _beep_data_uri() -> str:
    """경보음 WAV 의 data: URI. 한 번 만들고 재사용합니다(약 4ms, 30KB).

    사이렌처럼 길게 끌지 않고 0.72초에서 끊습니다. 관제음은 "무슨 일이 났다"를
    알리는 용도이고, 계속 울리면 사람이 스피커를 꺼버립니다(그러면 다음 경보도
    못 듣습니다). 반복은 하지 않고, 새 위험이 들어올 때만 한 번씩 납니다.

    길이는 REFRESH_STATUS_SECONDS(1초)보다 짧아야 합니다. 소리를 심는
    components.html 은 이 조각의 출력이라, 다음 갱신에서 조각이 다시 그려지면
    <audio> 엘리먼트가 사라지면서 재생도 끊깁니다. 소리를 늘리고 싶으면 상태
    갱신 주기도 같이 늘려야 합니다.
    """
    if "uri" in _BEEP_CACHE:
        return _BEEP_CACHE["uri"]

    rate = 16000
    frames = bytearray()
    for freq, seconds in ((880, 0.18), (660, 0.18), (880, 0.18), (660, 0.18)):
        count = int(rate * seconds)
        for i in range(count):
            # 앞뒤 5ms 를 페이드 인/아웃 합니다. 그냥 자르면 딱딱거리는 클릭음이
            # 같이 납니다(파형이 0이 아닌 곳에서 끊기기 때문).
            fade = min(1.0, i / (rate * 0.005), (count - i) / (rate * 0.005))
            value = 0.35 * fade * math.sin(2 * math.pi * freq * i / rate)
            frames += struct.pack("<h", int(value * 32767))

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(bytes(frames))

    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    _BEEP_CACHE["uri"] = "data:audio/wav;base64," + encoded
    return _BEEP_CACHE["uri"]


def _play_alarm(nonce) -> None:
    """경보음을 한 번 재생합니다.

    st.markdown 으로는 안 됩니다 — Streamlit 이 <audio> 태그를 걸러냅니다.
    components.html 은 iframe 안에서 진짜 HTML 로 돌아가므로 재생됩니다.
    st.audio(autoplay=True) 는 1.37 부터라 requirements 의 하한(1.33)과 안 맞고,
    플레이어가 눈에 보입니다. 여기서는 height=0 으로 숨깁니다.

    nonce 에는 위험 로그 id 를 넘깁니다. 내용이 완전히 같으면 Streamlit 이 기존
    iframe 을 그대로 두기 때문에, 값이 바뀌어야 새로 그려지면서 다시 재생됩니다.

    브라우저 자동재생 정책상 사용자가 페이지를 한 번 건드린 뒤에만 소리가 납니다.
    사이드바 체크박스를 켜는 동작이 그 조건을 만족시킵니다.
    """
    components.html(
        f'<audio autoplay src="{_beep_data_uri()}"></audio>'
        f"<!-- {html.escape(str(nonce))} -->",
        height=0,
    )


@st.cache_resource
def get_db_connection():
    """앱 생명주기 동안 재사용되는 커넥션.

    연결 자체가 실패하면 예외를 그대로 올립니다.
    (여기서 잡아서 None/False를 리턴하면 그 값이 캐싱되어
    다음 rerun에서도 재시도하지 않는 문제가 생깁니다.)
    """
    conn = psycopg2.connect(
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
        host=DB_HOST,
        port=DB_PORT,
    )
    # 이 대시보드는 조회만 합니다. autocommit을 끄면 psycopg2가 SELECT마다
    # 트랜잭션을 열고 커밋하지 않아, 커넥션이 "idle in transaction" 상태로
    # 계속 남습니다. 그러면 그 시점 이후의 죽은 행을 VACUUM이 회수하지 못해
    # 초당 수십 건씩 쌓이는 patrol_logs가 급격히 비대해집니다.
    conn.autocommit = True
    return conn


@st.cache_resource
def init_db():
    """테이블이 없으면 생성합니다. 최초 1회만 실제로 실행됩니다.

    실패 시 예외를 그대로 올려서 캐싱되지 않게 합니다.
    (실패 결과 자체를 캐싱하면 DB가 나중에 살아나도 앱이 영구히
    '초기화 실패' 상태로 남는 v2의 버그를 반복하게 됩니다.)

    데이터는 지우지 않습니다 — 이 함수를 호출하는 진입점이 5개이고
    그중 일부는 DB에 재연결할 때마다 호출되므로, 여기서 비우면 서로의
    기록을 지우게 됩니다. 자세한 이유는 admin/README.md 참고.
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        # patrol_zones / patrol_logs / response_plans / detection_events 를
        # 공통 모듈의 DDL로 생성. pipeline, integration 어댑터와 완전히 동일한
        # 스키마를 보장하기 위해 이 대시보드가 직접 CREATE TABLE 문을 따로
        # 정의하지 않습니다 (v3에서는 여기 있었지만 스키마 drift 방지를 위해 이동).
        init_all_tables(cursor)
        conn.commit()
        return True
    finally:
        cursor.close()


@contextmanager
def get_cursor():
    """cursor를 매번 새로 열고, 예외가 나도 반드시 close 되도록 보장.

    커넥션은 @st.cache_resource로 캐싱되므로, 한 번 끊기면 그 죽은 객체가
    계속 반환되어 DB가 되살아나도 앱은 재시작 전까지 영구히 실패합니다.
    끊김을 감지하면 캐시를 비워서 다음 rerun에 새로 연결되게 합니다.
    """
    conn = get_db_connection()
    if conn.closed:
        get_db_connection.clear()
        conn = get_db_connection()

    cursor = conn.cursor(cursor_factory=RealDictCursor)
    try:
        yield cursor
    except (psycopg2.OperationalError, psycopg2.InterfaceError):
        get_db_connection.clear()
        raise
    finally:
        try:
            cursor.close()
        except Exception:
            pass


# ==========================================
# 2. 초기화
# ==========================================
st.set_page_config(page_title="스마트 팩토리 관제 플랫폼", page_icon="🚨", layout="wide")

try:
    init_db()
except Exception:
    # DB가 잠시 내려가도 영상과 미니맵까지 함께 중단할 이유는 없습니다.
    # 각 DB 패널은 아래 조회 단계에서 자체적으로 '상태 불명'을 표시합니다.
    st.warning(
        "데이터베이스 초기화 실패. PostgreSQL 상태를 확인하세요. "
        "영상과 미니맵은 계속 표시합니다."
    )


# ------------------------------------------
# 2-1. 화면 스타일
# ------------------------------------------
# 카드는 배경색과 글자색을 자기 안에 다 갖고 있어서 Streamlit의 밝은/어두운
# 테마 어느 쪽에서도 그대로 읽힙니다(테마 변수에 기대지 않음).
# 위험만 "옅은 배경 + 진한 글씨" 규칙을 깨고 반전시켰습니다 — 색상 차이가
# 아니라 명암 관계가 달라지므로 색각 이상이 있어도 구분됩니다.
st.markdown(
    """
    <style>
    .sfp-card{
      border-radius:14px; padding:20px 14px 16px; text-align:center;
      border:1px solid rgba(0,0,0,.09); margin-bottom:4px;
      box-shadow:0 1px 3px rgba(0,0,0,.13);
    }
    .sfp-card .z{font-size:14px; font-weight:600; letter-spacing:.04em; opacity:.8}
    .sfp-card .s{font-size:27px; font-weight:800; line-height:1.2; margin-top:5px}
    .sfp-card .t{font-size:12px; margin-top:9px; opacity:.75}
    .sfp-ok    {background:#e7f6e9; color:#14532d; border-color:#bfe3c6}
    .sfp-warn  {background:#fff4d6; color:#7c4a03; border-color:#f0d9a0}
    .sfp-off   {background:#eceff1; color:#546e7a; border-style:dashed}
    .sfp-danger{background:#c1121f; color:#fff; border-color:#8b0d17}
    /* DB를 못 읽어서 "이상 없음"인지 "모름"인지 알 수 없는 상태.
       정상(초록)으로도, 미연결(로봇 문제)로도 보이면 안 되므로 따로 둡니다. */
    .sfp-unknown{background:#37474f; color:#cfd8dc; border-color:#263238}

    /* 배너 3종. 레이아웃은 공유하고 색만 다릅니다.
       빨강=위험 감지, 주황=로봇 미연결, 회색=관제 시스템 자체가 눈이 먼 상태.
       셋을 같은 빨강으로 칠하면 "무엇에 대응해야 하는지"가 사라집니다. */
    .sfp-banner, .sfp-offline-banner, .sfp-mute-banner{
      color:#fff; border-radius:12px;
      padding:13px 18px; margin:0 0 14px;
      display:flex; align-items:center; gap:14px; flex-wrap:wrap;
    }
    .sfp-banner        {background:#c1121f}
    .sfp-offline-banner{background:#8a5a00}
    .sfp-mute-banner   {background:#455a64}
    .sfp-banner .big, .sfp-offline-banner .big, .sfp-mute-banner .big{
      font-size:21px; font-weight:800; letter-spacing:-.01em;
    }
    .sfp-banner .sub, .sfp-offline-banner .sub, .sfp-mute-banner .sub{
      font-size:13px; opacity:.9;
    }

    .sfp-bar{
      display:flex; flex-wrap:wrap; gap:20px; align-items:baseline;
      font-size:13px; padding:6px 2px 0; opacity:.85;
    }
    .sfp-bar b{font-size:15px; font-weight:700}

    .sfp-plan{
      border:1px solid rgba(0,0,0,.1); border-left:5px solid #c1121f;
      border-radius:10px; padding:14px 16px; background:rgba(193,18,31,.05);
    }
    .sfp-plan .h{font-size:12px; opacity:.7; margin-bottom:6px}
    .sfp-plan .g{
      display:inline-block; font-size:12px; font-weight:700;
      padding:3px 8px; margin:0 0 8px; border-radius:999px;
      color:#5d3b00; background:rgba(255,193,7,.2);
    }
    .sfp-plan .a{font-size:16px; font-weight:600; line-height:1.5}
    .sfp-plan .r{font-size:12px; opacity:.7; margin-top:9px}
    /* 시간이 지난 대응안. 최신 1건을 늘 빨간 테두리로 띄우면, 상황이 끝난
       뒤에도 화면은 계속 경보 상태로 보입니다. 내용은 남기되 색만 뺍니다. */
    .sfp-plan.stale{border-left-color:#90a4ae; background:rgba(0,0,0,.03)}

    /* 첫 건 아래에 붙는 요약 줄. 큰 카드와 경쟁하지 않도록 글자와 색을 낮춥니다 —
       "더 있다"는 사실과 "어디서 무엇이"까지만 전달하면 됩니다. */
    .sfp-plan-row{
      display:flex; gap:10px; align-items:baseline; font-size:13px;
      padding:7px 11px; margin-top:6px; border-radius:0 8px 8px 0;
      border-left:3px solid #90a4ae; background:rgba(0,0,0,.035);
    }
    .sfp-plan-row.danger{border-left-color:#c1121f}
    .sfp-plan-row.warn  {border-left-color:#c98a00}
    .sfp-plan-row .w{margin-left:auto; opacity:.6; font-size:12px; white-space:nowrap}
    .sfp-idle{
      border:1px dashed rgba(0,0,0,.18); border-radius:10px;
      padding:22px 16px; text-align:center; font-size:13px; opacity:.6;
    }

    /* 움직임에 민감한 사용자를 위해 애니메이션은 선택적으로만 켭니다. */
    @media (prefers-reduced-motion: no-preference){
      .sfp-danger, .sfp-banner{animation:sfp-pulse 1.4s ease-out infinite}
    }
    @keyframes sfp-pulse{
      0%  {box-shadow:0 0 0 0    rgba(193,18,31,.55)}
      70% {box-shadow:0 0 0 11px rgba(193,18,31,0)}
      100%{box-shadow:0 0 0 0    rgba(193,18,31,0)}
    }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("🚨 스마트 팩토리 실시간 관제 대시보드")

# 첫 화면은 "전체 구역 · 감지 요약 · 최근 5분" 입니다. 특정 구역을 먼저 띄우면 다른
# 구역에서 벌어진 일이 화면에 없는데도 담당자는 표를 다 봤다고 느낍니다.
if "selected_zone" not in st.session_state:
    st.session_state.selected_zone = ZONE_ALL
if "detail_view" not in st.session_state:
    st.session_state.detail_view = VIEW_SUMMARY
if "detail_span" not in st.session_state:
    st.session_state.detail_span = DEFAULT_SPAN

# 대응안 기록 화면의 필터. 상세 조회와 따로 둡니다 — 로그를 A 구역으로 좁혀
# 놓았다고 해서 기록 화면까지 A 구역으로 열리면, 들어가자마자 "기록이 없다"를
# 보고 필터가 걸린 줄 모르는 일이 생깁니다.
if "plan_zone" not in st.session_state:
    st.session_state.plan_zone = ZONE_ALL
if "plan_span" not in st.session_state:
    st.session_state.plan_span = DEFAULT_PLAN_SPAN

# 경보 확인(acknowledge) 상태. "여기까지의 위험은 봤다"는 표시로, 확인한 시점의
# 위험 로그 id 를 담아둡니다. 이보다 큰 id 가 들어오면 = 새 위험이므로 다시 울립니다.
# 해결됐다는 뜻이 아니므로 구역 카드의 빨강은 이 값과 무관하게 유지됩니다.
if "ack_danger_id" not in st.session_state:
    st.session_state.ack_danger_id = 0
# 소리를 이미 낸 위험 id. 같은 경보로 매 초 다시 울리는 것을 막습니다.
if "played_danger_id" not in st.session_state:
    st.session_state.played_danger_id = 0


# ==========================================
# 3. 사이드바 — 설정만 (상세 로그는 본문으로 옮겼습니다)
# ==========================================
# 영상은 젯슨(ai_inference_sender.py, 8500포트)에서, 미니맵은 다은님 노트북
# (minimap_renderer.py, 8091포트)에서 각각 나오므로 IP를 따로 받습니다.
#
# 기본값을 환경변수에서 읽습니다. 예전에는 "localhost" 로 고정돼 있었는데,
# 받는 쪽(dashboard_video_minimap_section.py)이 `jetson_ip or JETSON_IP` 라서
# "localhost" 는 truthy — 즉 JETSON_IP/DAEUN_LAPTOP_IP 를 설정해도 절대
# 반영되지 않았습니다. 시연 때마다 IP를 손으로 치게 만들던 원인입니다.
st.sidebar.title("⚙️ 관제 시스템 설정")
jetson_ip = st.sidebar.text_input(
    "젯슨 IP 주소 (영상)",
    value=os.environ.get("JETSON_IP", "203.0.113.10"),
    help="환경변수 JETSON_IP 로 기본값을 지정할 수 있습니다.",
)
daeun_laptop_ip = st.sidebar.text_input(
    "다은님 노트북 IP 주소 (미니맵)",
    value=os.environ.get("DAEUN_LAPTOP_IP", "localhost"),
    help="환경변수 DAEUN_LAPTOP_IP 로 기본값을 지정할 수 있습니다.",
)

st.sidebar.markdown("---")

# 대시보드와 RAG 파이프라인은 같은 이름의 Rag_to_Jetson.py를 서로 다른
# 배치 경로에서 사용할 수 있습니다. 평범한 ``import Rag_to_Jetson``은 먼저
# 읽은 파일을 sys.modules에서 계속 돌려주므로, 실행 중 파일을 갱신하면 새 함수가
# 디스크에 있어도 ImportError가 났습니다. 선택한 파일을 고유한 모듈 이름으로
# 읽고 mtime이 바뀐 경우에만 다시 로드해 이 충돌과 오래된 캐시를 피합니다.
_TTS_DIR_CANDIDATES = (
    os.path.join(_PROJECT_ROOT, "tts"),
    os.path.join(os.path.dirname(_PROJECT_ROOT), "TTS Engine and pipeline"),
)
_TTS_DIR_DEFAULT = _TTS_DIR_CANDIDATES[-1]
_DASHBOARD_TTS_MODULE_NAME = "_smart_factory_dashboard_tts_sender"


def _resolve_tts_module_path():
    tts_dir = os.environ.get("TTS_MODULE_DIR", "").strip()
    if not tts_dir:
        tts_dir = next(
            (candidate for candidate in _TTS_DIR_CANDIDATES
             if os.path.isfile(os.path.join(candidate, "Rag_to_Jetson.py"))),
            _TTS_DIR_DEFAULT,
        )
    return tts_dir, os.path.join(tts_dir, "Rag_to_Jetson.py")


def _load_tts_module():
    tts_dir, module_path = _resolve_tts_module_path()
    try:
        source_mtime = os.stat(module_path).st_mtime_ns
    except OSError:
        return None, tts_dir

    cached = sys.modules.get(_DASHBOARD_TTS_MODULE_NAME)
    if (cached is not None
            and getattr(cached, "__dashboard_source_path__", "") == module_path
            and getattr(cached, "__dashboard_source_mtime__", None) == source_mtime):
        return cached, tts_dir

    spec = importlib.util.spec_from_file_location(
        _DASHBOARD_TTS_MODULE_NAME, module_path)
    if spec is None or spec.loader is None:
        return None, tts_dir
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(_DASHBOARD_TTS_MODULE_NAME)
    sys.modules[_DASHBOARD_TTS_MODULE_NAME] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        if previous is None:
            sys.modules.pop(_DASHBOARD_TTS_MODULE_NAME, None)
        else:
            sys.modules[_DASHBOARD_TTS_MODULE_NAME] = previous
        raise
    module.__dashboard_source_path__ = module_path
    module.__dashboard_source_mtime__ = source_mtime
    return module, tts_dir


# 방송 WAV를 증폭하는 기존 TTS 음량 대신, Jetson USB 스피커의 ALSA 출력값을
# 직접 제어합니다. 슬라이더 변경 때만 인증된 TCP 제어 요청을 보내므로 자동
# 새로고침마다 Jetson mixer를 반복 변경하지 않습니다.
try:
    _speaker_output_default = max(
        0, min(100, int(os.environ.get("SPEAKER_OUTPUT_PERCENT", "70"))))
except ValueError:
    _speaker_output_default = 70

if "speaker_output_percent" not in st.session_state:
    st.session_state.speaker_output_percent = _speaker_output_default
if "speaker_output_last_ui_percent" not in st.session_state:
    st.session_state.speaker_output_last_ui_percent = (
        st.session_state.speaker_output_percent)

speaker_output_percent = st.sidebar.slider(
    "🔊 스피커 출력",
    min_value=0,
    max_value=100,
    step=5,
    key="speaker_output_percent",
    help="Jetson USB 스피커의 실제 ALSA 출력 볼륨입니다. 0%는 음소거입니다.",
)

def _set_jetson_speaker_output(percent: int):
    """대시보드에서 Jetson 수신기의 ALSA mixer를 직접 제어합니다."""
    try:
        module, tts_dir = _load_tts_module()
        setter = getattr(module, "set_speaker_output_volume", None) if module else None
        if setter is None:
            return None, f"{tts_dir}/Rag_to_Jetson.py에 스피커 제어 함수가 없습니다"
        return setter(percent, jetson_ip=jetson_ip)
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)

# 예전 코드는 실패한 값도 'requested'로 기록해 같은 값을 다시 보내지
# 않았습니다. 현재 UI 값과 최근 실제 적용 값을 따로 기록하고, 수신기가
# 나중에 실행된 경우에도 사용자가 즉시 재시도할 수 있게 합니다.
_speaker_control_version = 2
_speaker_first_sync = (
    st.session_state.get("speaker_output_control_version") != _speaker_control_version)
_previous_speaker_status = st.session_state.get("speaker_output_result")
_speaker_recover_failed = (
    _speaker_first_sync and isinstance(_previous_speaker_status, tuple)
    and _previous_speaker_status[:1] == ("error",))
_speaker_value_changed = (
    st.session_state.get("speaker_output_last_ui_percent") != speaker_output_percent)
_speaker_retry = st.sidebar.button(
    "🔁 스피커 출력 다시 적용",
    key="speaker_output_retry",
    help="TTS 수신기를 나중에 켰거나 제어가 실패했을 때 현재 값을 다시 보냅니다.",
    **_full_width_kwargs(),
)
st.session_state.speaker_output_last_ui_percent = speaker_output_percent
st.session_state.speaker_output_control_version = _speaker_control_version

if _speaker_recover_failed or _speaker_value_changed or _speaker_retry:
    _speaker_result = _set_jetson_speaker_output(speaker_output_percent)
    if isinstance(_speaker_result, tuple):
        st.session_state.speaker_output_result = (
            "error", f"스피커 출력 제어 모듈 오류: {_speaker_result[1]}")
    elif _speaker_result.ok:
        st.session_state.speaker_output_applied_percent = speaker_output_percent
        st.session_state.speaker_output_result = (
            "ok", f"Jetson 스피커 출력 {speaker_output_percent}% 적용 "
                  f"({_speaker_result.detail})")
    else:
        st.session_state.speaker_output_result = (
            "error", _speaker_result.detail or "Jetson 스피커 출력 설정에 실패했습니다.")

_speaker_status = st.session_state.get("speaker_output_result")
if _speaker_status:
    _speaker_kind, _speaker_message = _speaker_status
    st.sidebar.caption(("✅ " if _speaker_kind == "ok" else "⚠️ ") + _speaker_message)

st.sidebar.markdown("---")

# 화면 구성. 영상과 지도를 한 화면에서 같이 보려면 '관제 집중'.
layout_mode = st.sidebar.radio(
    "화면 구성", LAYOUT_OPTIONS, key="layout_mode",
    help="'관제 집중'은 상세 조회를 접고 미니맵을 낮춰서, 영상과 지도가 "
         "한 화면에 같이 들어오게 합니다.",
)

# 상세 조회의 갱신 주기는 선택한 범위를 따릅니다. 위젯은 본문 아래쪽에 있지만,
# 라디오를 바꾸면 스크립트가 통째로 다시 도므로 이 시점의 session_state 에는
# 이미 새 값이 들어 있습니다.
detail_span = st.session_state.get("detail_span", DEFAULT_SPAN)
refresh_detail_seconds = DETAIL_SPANS.get(
    detail_span, DETAIL_SPANS[DEFAULT_SPAN])["refresh"]

auto_refresh = st.sidebar.checkbox(
    "자동 갱신", value=True,
    help="끄면 화면이 멈춥니다. 로그를 천천히 읽거나 화면을 캡처할 때 쓰세요.",
)
alarm_sound = st.sidebar.checkbox(
    "소리 경보", value=False,
    help="새 위험이 들어올 때만 짧게 울립니다. 브라우저 정책상 이 체크박스를 "
         "켜는 동작이 있어야 소리가 나므로 기본값은 꺼짐입니다.",
)
st.sidebar.caption(
    f"상태 {REFRESH_STATUS_SECONDS:g}초 · 대응안 {REFRESH_PLAN_SECONDS:g}초 · "
    f"상세 {refresh_detail_seconds:g}초 간격"
)

# 색 규칙을 화면에 적어둡니다. 시연이나 인수인계 때 "저 주황은 뭐냐"를 매번
# 설명하지 않기 위해서입니다 — 색으로 대응 대상을 나눈 의미가 전달돼야 합니다.
st.sidebar.markdown("---")
st.sidebar.caption(
    "**색 규칙**  \n"
    "🔴 빨강 — 위험 감지, 즉시 조치  \n"
    "🟠 주황 — 순찰 로봇 미연결  \n"
    "⚪ 회색 — DB 응답 없음(상태 불명)  \n"
    "🟡 노랑 — 주의  ·  🟢 초록 — 정상"
)


# ==========================================
# 4. 데이터 조회 함수
# ==========================================
def fetch_summary():
    """상단 배너 + 구역 카드에 필요한 값을 조회합니다.

    시각 기준을 DB 쪽(LOCALTIMESTAMP)으로 통일했습니다. 예전에는 파이썬의
    datetime.now()로 만든 값을 넘겨서 비교했는데, 대시보드 PC와 DB 서버가
    다른 기기이거나 시간대가 다르면 "미연결" 판정과 "최근 5분" 집계가 통째로
    어긋났습니다. 이제 age_seconds 도, 화면에 찍는 시각도 DB가 내려줍니다.
    (그래서 이 시각이 갱신되고 있다는 것 자체가 DB 왕복이 살아 있다는 증거입니다.)

    구역별 최신 1건은 ROW_NUMBER() 윈도우 함수 대신 DISTINCT ON 을 씁니다.
    윈도우 함수는 patrol_logs 전체를 읽고 정렬해야 하지만, DISTINCT ON 은
    idx_patrol_logs_zone_time 인덱스를 그대로 타고 구역마다 첫 행만 집습니다.

    last_danger_id 는 경보 확인(acknowledge)과 소리 재생의 기준입니다. 시각이
    아니라 id 를 쓰는 이유는 두 가지입니다 — SERIAL 이라 단조 증가가 보장되고,
    같은 log_time 에 여러 건이 들어와도 하나로 뭉개지지 않습니다.
    """
    with get_cursor() as cursor:
        cursor.execute(
            """
            SELECT COUNT(*) AS count, MAX(id) AS last_danger_id,
                   LOCALTIMESTAMP AS db_now
            FROM patrol_logs
            WHERE risk_level = '위험'
              AND log_time >= LOCALTIMESTAMP - (%s * INTERVAL '1 minute')
            """,
            (RECENT_ALERT_MINUTES,),
        )
        head = cursor.fetchone()

        cursor.execute(
            """
            SELECT DISTINCT ON (zone)
                   zone,
                   risk_level,
                   detected_object,
                   log_time,
                   EXTRACT(EPOCH FROM (LOCALTIMESTAMP - log_time)) AS age_seconds
            FROM patrol_logs
            ORDER BY zone, log_time DESC
            """
        )
        latest_status = cursor.fetchall()
    return head["count"], head["db_now"], latest_status, head["last_danger_id"] or 0


def _span(name: str) -> dict:
    """조회 범위 이름 -> {minutes, scan_rows, refresh}. 모르는 이름은 기본값."""
    return DETAIL_SPANS.get(name, DETAIL_SPANS[DEFAULT_SPAN])


def _detail_filter(zone: str, minutes: int, time_column: str = "log_time"):
    """(WHERE 절, 파라미터 튜플). 구역과 시간 범위를 함께 겁니다.

    시간과 행 수 두 가지로 자릅니다. 시간은 화면에 적힌 "최근 N분"을 정직하게
    만들고, 행 수(호출부의 LIMIT)는 갑자기 트래픽이 몰려도 집계량이 폭발하지
    않게 막습니다. 둘 중 하나만으로는 정직하거나 안전하거나 하나만 됩니다.
    """
    clauses = [f"{time_column} >= LOCALTIMESTAMP - (%s * INTERVAL '1 minute')"]
    params = [minutes]
    if zone != ZONE_ALL:
        clauses.append("zone = %s")
        params.append(zone)
    return "WHERE " + " AND ".join(clauses), tuple(params)


def fetch_log_summary(zone: str, span: str = DEFAULT_SPAN, limit: int = DETAIL_ROW_LIMIT):
    """연속된 같은 (객체, 위험도) 구간을 한 줄로 접어서 돌려줍니다.

    왜 필요한가
    -----------
    ResponsePlanThrottle 은 response_plans 에만 걸리고 patrol_logs 적재는
    억제하지 않습니다. 젯슨이 30fps로 넣으므로 단순 LIMIT 30 은 "최근 1초"이고,
    관제 담당자가 "아까 그거 뭐였지"를 확인할 수 없습니다. 그래서 선택한 범위
    (span) 안의 행을 훑어 구간으로 접습니다.

    구현
    ----
    gaps-and-islands: 구역 안에서의 순번에서 (구역, 객체, 위험도)별 순번을 빼면,
    연속으로 이어지는 같은 조합끼리 같은 값(grp)을 갖습니다.

    두 순번 모두 zone 으로 partition 합니다. '전체'를 보면 여러 구역의 로그가
    시간순으로 섞여 들어오는데, 구역을 빼고 세면 A 구역의 연속 감지가 중간에
    낀 B 구역 로그 한 줄 때문에 두 구간으로 쪼개집니다. 구역별로 세면 서로의
    끼어듦과 무관해집니다(한 구역만 볼 때는 어차피 같은 결과).

    issue 로는 묶지 않습니다. classify_risk() 가 만드는 문구에 거리가 박혀
    있어서("차량 근접 위험 (1.20m)") 매 프레임 문자열이 달라지고, 그걸로
    묶으면 구간이 프레임 단위로 잘게 쪼개져 압축이 통째로 무의미해집니다.
    대신 구간 안에서 가장 최근 행의 issue 를 대표로 씁니다.

    distance 의 -1 은 시차 계산 실패값이므로 최근접 거리 계산에서 뺍니다
    (FILTER 로 걸러야 -1이 최솟값으로 뽑히는 일이 없습니다).

    윈도우의 ORDER BY 에는 id 를 덧붙여 동률을 없앱니다. 같은 log_time 이 두 개
    있으면 순번이 실행마다 달라져 구간이 흔들릴 수 있습니다. 안쪽 LIMIT 쪽에는
    일부러 넣지 않았습니다 — 거기에 id 를 넣으면 인덱스(zone, log_time DESC)로
    상위 N건만 집어오지 못하고 정렬이 생깁니다. 경계 한 줄이 어느 쪽이 되든
    결과에는 영향이 없습니다.
    """
    conf = _span(span)
    where, params = _detail_filter(zone, conf["minutes"])
    with get_cursor() as cursor:
        cursor.execute(
            f"""
            SELECT
                MIN(log_time) AS first_time,
                MAX(log_time) AS last_time,
                COUNT(*)      AS hits,
                zone,
                detected_object,
                risk_level,
                MIN(distance) FILTER (WHERE distance >= 0) AS min_distance,
                (array_agg(issue ORDER BY log_time DESC))[1] AS issue
            FROM (
                SELECT log_time, zone, detected_object, risk_level, distance, issue,
                       ROW_NUMBER() OVER (PARTITION BY zone
                                          ORDER BY log_time DESC, id DESC)
                     - ROW_NUMBER() OVER (PARTITION BY zone, detected_object, risk_level
                                          ORDER BY log_time DESC, id DESC) AS grp
                FROM (
                    SELECT id, log_time, zone, detected_object, risk_level, distance, issue
                    FROM patrol_logs
                    {where}
                    ORDER BY log_time DESC
                    LIMIT {int(conf["scan_rows"])}
                ) recent
            ) marked
            GROUP BY grp, zone, detected_object, risk_level
            ORDER BY MAX(log_time) DESC
            LIMIT %s
            """,
            params + (limit,),
        )
        return cursor.fetchall()


def fetch_raw_logs(zone: str, span: str = DEFAULT_SPAN, limit: int = RAW_LOG_LIMIT):
    """프레임 단위 원본. 요약이 뭉갠 것을 확인해야 할 때만 씁니다."""
    where, params = _detail_filter(zone, _span(span)["minutes"])
    with get_cursor() as cursor:
        cursor.execute(
            f"""
            SELECT log_time, zone, detected_object, distance,
                   box_position, risk_level, issue
            FROM patrol_logs
            {where}
            ORDER BY log_time DESC LIMIT %s
            """,
            params + (limit,),
        )
        return cursor.fetchall()


def fetch_plans(limit: int = PLAN_PANEL_LIMIT, zone: str = ZONE_ALL,
                minutes: int = None, prioritize: bool = False):
    """RagResponseAgent 가 생성한 대응안을 조회합니다.

    idx_response_plans_created (created_at DESC), 구역을 지정하면
    idx_response_plans_zone_created 를 탑니다.

    age_seconds 를 DB가 계산해 내려줍니다. created_at 을 시:분:초로만 찍으면
    어제 14:32 의 대응안과 5분 전 것이 화면에서 구분되지 않는데, 관제에서
    이 둘은 완전히 다른 의미입니다. (fetch_summary 와 같은 이유로 기준 시각은
    대시보드 PC가 아니라 DB 쪽 LOCALTIMESTAMP 입니다.)

    minutes=None 이면 시간 제한이 없습니다. 영상 옆 패널이 이 경로를 씁니다 —
    마지막 대응안이 20분 전 것이라도 그게 최신이라는 사실 자체가 정보이고,
    범위를 걸어 비워버리면 "대응안이 없다"로 잘못 읽힙니다.

    prioritize
    ----------
    영상 옆 패널은 자리가 3칸뿐이라 "무엇을 버릴지"를 정해야 합니다. 최신순만
    쓰면 방금 들어온 '주의' 3건이 4분 전 '위험'을 밀어냅니다. 그렇다고 심각도만
    쓰면 3시간 전 '위험' 하나가 자리를 영원히 차지합니다(v8에서 고친 "오래된
    대응안이 경보처럼 보이는" 문제가 그대로 돌아옵니다).

    그래서 **신선한 것 안에서만** 심각도로 줄을 세웁니다.

        신선한 위험(2) > 신선한 주의(1) > 신선한 정상(0) > 오래된 것(-1)

    오래된 것끼리는 전부 -1 이라 뒤의 created_at DESC 로 최신순이 됩니다. 즉
    최근 5분에 아무 일도 없었으면 예전과 똑같이 "가장 최근 대응안"이 위에 옵니다.
    """
    clauses, params = [], []
    if minutes is not None:
        clauses.append("created_at >= LOCALTIMESTAMP - (%s * INTERVAL '1 minute')")
        params.append(minutes)
    if zone != ZONE_ALL:
        clauses.append("zone = %s")
        params.append(zone)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    if prioritize:
        order = """
            CASE WHEN created_at >= LOCALTIMESTAMP - (%s * INTERVAL '1 second')
                 THEN CASE risk_level WHEN '위험' THEN 2 WHEN '주의' THEN 1 ELSE 0 END
                 ELSE -1 END DESC,
            created_at DESC
        """
        params.append(PLAN_FRESH_SECONDS)
    else:
        order = "created_at DESC"

    with get_cursor() as cursor:
        cursor.execute(
            f"""
            SELECT created_at, zone, detected_object, risk_level,
                   recommended_action, reference_docs, confidence, generation_mode,
                   EXTRACT(EPOCH FROM (LOCALTIMESTAMP - created_at)) AS age_seconds
            FROM response_plans
            {where}
            ORDER BY {order}
            LIMIT %s
            """,
            tuple(params) + (limit,),
        )
        return cursor.fetchall()


# ==========================================
# 5. 화면 조각들
# ==========================================
def _zone_style(row):
    """구역 한 칸의 (CSS 클래스, 상태 문구)를 정합니다."""
    if row is None:
        return "sfp-off", "데이터 없음"
    if float(row["age_seconds"] or 0) > ACTIVE_THRESHOLD_SECONDS:
        return "sfp-off", "미연결"
    return {
        "위험": ("sfp-danger", "위험"),
        "주의": ("sfp-warn", "주의"),
    }.get(row["risk_level"], ("sfp-ok", "정상"))


def _render_zone_card(col, zone: str, cls: str, status_text: str, detail: str):
    """구역 카드 한 칸. zone/status_text 는 코드 상수, detail 은 호출부에서 이미
    이스케이프된 문자열이어야 합니다."""
    col.markdown(
        f'<div class="sfp-card {cls}">'
        f'<div class="z">{zone} 구역</div>'
        f'<div class="s">{status_text}</div>'
        f'<div class="t">{detail}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )


def _render_status_unavailable():
    """DB를 못 읽었을 때의 상태 영역.

    예전에는 fetch_summary() 가 감싸져 있지 않아서, DB가 잠깐 끊기면 화면
    맨 위(= 경보가 있어야 할 자리)가 빨간 파이썬 트레이스백으로 바뀌었습니다.
    관제 화면에서 "정보 없음"을 "이상 없음"처럼 보이게 두는 것도, 트레이스백을
    띄우는 것도 둘 다 사고로 이어집니다. 그래서 색을 초록도 빨강도 아닌
    회색으로 두고, 무엇을 모르는지 명시합니다.

    커넥션 캐시는 get_cursor() 가 비우므로 DB가 살아나면 다음 틱에 저절로
    정상 화면으로 돌아옵니다.
    """
    st.markdown(
        '<div class="sfp-mute-banner">'
        '<span style="font-size:26px">📡</span>'
        '<span class="big">상태 불명 — 데이터베이스 응답 없음</span>'
        '<span class="sub">경보 판정이 멈췄습니다. 화면의 값은 신뢰하지 마세요. '
        'PostgreSQL 서버를 확인하세요 (연결이 돌아오면 자동 복구됩니다).</span>'
        '</div>',
        unsafe_allow_html=True,
    )
    cols = st.columns(len(ZONES))
    for i, z in enumerate(ZONES):
        _render_zone_card(cols[i], z, "sfp-unknown", "상태 불명", "DB 조회 실패")
    st.markdown(
        '<div class="sfp-bar"><span>📡 데이터베이스에 연결하지 못했습니다 — '
        '위험 건수와 로봇 연결 상태를 판정할 수 없습니다.</span></div>',
        unsafe_allow_html=True,
    )


def _acknowledge(danger_id: int) -> None:
    """경보 확인 버튼의 콜백. "여기까지는 봤다"를 기록합니다.

    소리 기준(played_danger_id)도 같이 올립니다. 안 그러면 확인 직후에 소리만
    한 번 더 나서, 방금 확인한 사람이 다시 화면을 보게 됩니다.
    """
    st.session_state.ack_danger_id = danger_id
    st.session_state.played_danger_id = danger_id


def _render_status():
    """경보 배너 + 구역 카드 + 상태 바. 화면에서 가장 위에 옵니다."""
    try:
        danger_count, db_now, latest_status, last_danger_id = fetch_summary()
    except Exception:
        _render_status_unavailable()
        return

    by_zone = {row["zone"]: row for row in latest_status}
    # age_seconds 는 DB가 계산해 내려준 값이라 대시보드 PC 시계와 무관합니다.
    active = [r for r in latest_status if float(r["age_seconds"] or 0) <= ACTIVE_THRESHOLD_SECONDS]
    normal_count = sum(1 for r in active if r["risk_level"] == "정상")
    warn_count = sum(1 for r in active if r["risk_level"] == "주의")
    disconnected = TOTAL_ZONES - len(active)
    danger_zones = [z for z in ZONES if _zone_style(by_zone.get(z))[0] == "sfp-danger"]

    # 미연결을 두 가지로 나눕니다. 오다가 끊긴 것(로봇/네트워크 장애)과 한 번도
    # 안 온 것(아직 안 켰거나 구역 설정이 틀림)은 대응이 다릅니다.
    stale_zones = [z for z in ZONES
                   if z in by_zone and _zone_style(by_zone[z])[0] == "sfp-off"]
    never_zones = [z for z in ZONES if z not in by_zone]

    # --- 경보 배너: 위험이 있을 때만 나타납니다 --------------------
    # 항상 떠 있는 경고는 곧 배경이 되어 아무도 안 봅니다. 같은 이유로,
    # 담당자가 "확인"을 누른 뒤로 새 위험이 없으면 배너를 내립니다.
    # 확인은 "봤다"이지 "해결됐다"가 아니므로 구역 카드의 빨강은 그대로 둡니다.
    has_danger = bool(danger_zones or danger_count)
    acknowledged = last_danger_id <= st.session_state.get("ack_danger_id", 0)

    if has_danger and not acknowledged:
        where = ", ".join(f"{z} 구역" for z in danger_zones) if danger_zones else "최근 이력"
        banner, ack_col = st.columns([9, 1])
        with banner:
            st.markdown(
                f'<div class="sfp-banner">'
                f'<span style="font-size:26px">🚨</span>'
                f'<span class="big">위험 감지 — {html.escape(where)}</span>'
                f'<span class="sub">최근 {RECENT_ALERT_MINUTES}분 위험 로그 {danger_count}건</span>'
                f'</div>',
                unsafe_allow_html=True,
            )
        with ack_col:
            # on_click 콜백을 쓰는 것이 중요합니다. 반환값(if st.button(...))으로
            # 처리하면 그 시점에는 배너가 이미 그려진 뒤라 다음 틱까지 배너가
            # 남습니다. 콜백은 Streamlit 이 재실행 "전에" 실행하므로, 눌린 직후의
            # 그리기에서는 acknowledged 가 이미 True 입니다.
            # 조각 안의 버튼이라 이 조각만 다시 그려집니다(전체 rerun 아님).
            st.button("✅ 확인", key="ack_button",
                      on_click=_acknowledge, args=(last_danger_id,),
                      help="이 시점까지의 위험을 확인 처리합니다. 새 위험이 "
                           "들어오면 배너와 소리가 다시 납니다.",
                      **_full_width_kwargs())

    elif has_danger and acknowledged:
        # 완전히 지우지는 않습니다. 확인했다고 위험이 사라진 것은 아니고, 교대
        # 인수인계 때 "지금 무슨 상태인지"가 화면에 남아 있어야 합니다.
        st.markdown(
            f'<div class="sfp-bar"><span>✅ 위험 확인됨 — '
            f'최근 {RECENT_ALERT_MINUTES}분 위험 로그 <b>{danger_count}</b>건 '
            f'(새 위험이 들어오면 다시 경보합니다)</span></div>',
            unsafe_allow_html=True,
        )

    # --- 소리 경보 -------------------------------------------------
    # 확인하지 않은 "새" 위험일 때만 한 번 울립니다. 매 초 다시 울리면 사람이
    # 스피커를 꺼버리고, 그러면 다음 경보도 못 듣게 됩니다.
    if (alarm_sound and has_danger and not acknowledged
            and last_danger_id > st.session_state.get("played_danger_id", 0)):
        st.session_state.played_danger_id = last_danger_id
        _play_alarm(last_danger_id)

    # --- 미연결 배너 -----------------------------------------------
    # 순찰 로봇이 응답하지 않는 것은 위험 감지만큼 즉각 대응이 필요한데,
    # v7까지는 회색 점선 카드 하나가 전부라 자리를 비운 사이 조용히 넘어갔습니다.
    # 빨강과 구분되는 주황을 써서 "무엇에 대응할 일인지"를 색으로 나눕니다.
    if stale_zones or never_zones:
        parts = []
        if stale_zones:
            parts.append(", ".join(f"{z} 구역" for z in stale_zones)
                         + f" {ACTIVE_THRESHOLD_SECONDS}초 이상 응답 없음")
        if never_zones:
            parts.append(", ".join(f"{z} 구역" for z in never_zones) + " 수신 이력 없음")
        st.markdown(
            f'<div class="sfp-offline-banner">'
            f'<span style="font-size:26px">📴</span>'
            f'<span class="big">순찰 로봇 미연결 {disconnected}대</span>'
            f'<span class="sub">{html.escape(" · ".join(parts))}</span>'
            f'</div>',
            unsafe_allow_html=True,
        )

    # --- 구역 카드 -------------------------------------------------
    cols = st.columns(len(ZONES))
    for i, z in enumerate(ZONES):
        row = by_zone.get(z)
        cls, status_text = _zone_style(row)

        if row is None:
            detail = "수신된 로그 없음"
        elif cls == "sfp-off":
            detail = f"{int(float(row['age_seconds'] or 0))}초째 응답 없음"
        else:
            # detected_object 는 DB에서 온 값이므로 반드시 이스케이프합니다.
            obj = html.escape(_obj_ko(row["detected_object"]))
            detail = f"{obj} · {row['log_time'].strftime('%H:%M:%S')}"

        # z 는 상수 ZONES 에서 오고 status_text 는 고정 문구입니다.
        _render_zone_card(cols[i], z, cls, status_text, detail)

    # --- 상태 바 ---------------------------------------------------
    # 메트릭 4칸(약 140px)을 대신합니다. "전체 구역 수"는 상수라 뺐고,
    # 정상/미연결 개수는 위 카드와 중복이지만 한 줄 요약으로만 남깁니다.
    #
    # '주의' 칸이 v8에서 추가됐습니다. 없을 때는 세 구역이 전부 주의여도
    # "위험 0 · 정상 0/3 · 미연결 0" 이라, 나머지 세 구역이 어디로 갔는지
    # 화면만 봐서는 알 수 없었습니다(네 숫자의 합이 구역 수와 안 맞음).
    clock = db_now.strftime("%H:%M:%S") if db_now else "--:--:--"
    st.markdown(
        f'<div class="sfp-bar">'
        f'<span>🔴 최근 {RECENT_ALERT_MINUTES}분 위험 <b>{danger_count}</b>건</span>'
        f'<span>🟡 주의 <b>{warn_count}</b>/{TOTAL_ZONES} 구역</span>'
        f'<span>🟢 정상 <b>{normal_count}</b>/{TOTAL_ZONES} 구역</span>'
        f'<span>⚫ 미연결 <b>{disconnected}</b>대</span>'
        f'<span>🕐 DB 시각 <b>{clock}</b></span>'
        f'</div>',
        unsafe_allow_html=True,
    )


# ==========================================
# 현장 음성 안내 (수동 방송)
# ==========================================
# 화면의 recommended_action은 관리자용 상세 지침입니다. 현장에서는 이 긴 문장을
# 그대로 읽지 않고, 구역·감지 객체에 맞춘 짧은 고정 문장을 방송합니다. 자동 경로는
# patrol_pipeline_rag.py --tts에서 화재 등 긴급 트리거만 감지 즉시 5회 방송합니다.
# 후보를 순서대로 봅니다. 이 PC에서는 저장소 레이아웃이 맞지만, 대시보드 폴더만
# 따로 옮겨 쓰는 경우(젯슨처럼 tts/ 를 옆에 두는 배치)에도 찾을 수 있어야 합니다.
# 파이프라인(patrol_pipeline_rag.py)과 같은 규칙입니다 — 두 곳이 다른 곳을 보면
# 대시보드 버튼만 안 되거나 그 반대가 되고, 둘 다 조용히 실패합니다.
def _load_tts_sender():
    """(plan 송신 함수, 공통 문장 builder, 탐색한 경로).

    비동기(send_alert_async)가 아니라 동기 함수를 씁니다. 버튼을 눌렀으면
    성공/실패를 그 자리에서 보여줘야 하는데, 비동기로 보내면 화면에는 항상
    "보냈습니다"만 뜨고 실제로 나갔는지 알 수 없습니다.
    """
    try:
        module, tts_dir = _load_tts_module()
        builder = getattr(module, "build_speech_text", None) if module else None
        sender = getattr(module, "send_plan_to_jetson", None) if module else None
    except Exception:
        return None, None, tts_dir
    return sender, builder, tts_dir


def _plan_speech_text(plan) -> str:
    """자동 방송과 같은 공통 builder로 구역·객체·대응안을 조립합니다."""
    _sender, builder, _tts_dir = _load_tts_sender()
    return builder(plan) if builder is not None else ""


def _speak_plan(plan_key: str, plan):
    """버튼 콜백 — 젯슨 TTS 수신기로 문장을 보냅니다.

    결과를 session_state 에 남깁니다. 대응안 패널은 자동 갱신 조각이라
    콜백이 끝나면 곧바로 다시 그려지는데, 지역 변수로 들고 있으면
    결과 메시지가 그 순간 사라집니다.
    """
    sender, builder, tts_dir = _load_tts_sender()
    text = builder(plan) if builder is not None else ""

    if sender is None:
        result = ("error", f"TTS 모듈을 찾지 못했습니다 ({tts_dir}). "
                           "TTS_MODULE_DIR 환경변수로 위치를 지정하세요.")
    elif not text:
        result = ("error", "방송할 문장이 비어 있습니다.")
    else:
        try:
            # 스피커 출력은 위 ALSA mixer 슬라이더가 제어한다. 여기서는 WAV를
            # 별도로 증폭하지 않아 실제 출력값과 화면 표시가 어긋나지 않게 한다.
            ok = sender(plan, volume_percent=100)
        except Exception as exc:                               # noqa: BLE001
            result = ("error", f"방송에 실패했습니다 ({type(exc).__name__}).")
        else:
            if ok:
                # queued ACK까지 받은 상태입니다. 실제 Piper/aplay 완료 ACK는
                # 아직 없으므로 '재생 완료'라고 표현하지 않습니다.
                result = ("ok", f"젯슨 재생 대기열에 등록했습니다 — {text[:40]}…"
                                if len(text) > 40
                                else f"젯슨 재생 대기열에 등록했습니다 — {text}")
            else:
                result = ("error", "젯슨 TTS 수신기가 요청을 수락하지 않았습니다 "
                                   "(수신기·JETSON_IP·토큰·큐 상태를 확인하세요).")

    st.session_state["tts_result"] = result
    st.session_state["tts_result_at"] = time.time()
    st.session_state["tts_last_key"] = plan_key


def _plan_key(plan) -> str:
    """대응안 한 건을 가리키는 버튼 키. id 컬럼이 없어 생성 시각으로 만듭니다."""
    created = plan.get("created_at")
    stamp = created.isoformat() if created else "none"
    return f"{stamp}|{plan.get('zone')}|{plan.get('detected_object')}"


def _render_tts_result():
    """직전 방송 결과. 오래된 메시지는 지웁니다."""
    result = st.session_state.get("tts_result")
    if not result:
        return
    if time.time() - st.session_state.get("tts_result_at", 0) > 20:
        st.session_state["tts_result"] = None
        return
    kind, message = result
    (st.success if kind == "ok" else st.error)(message)


def _obj_ko(name) -> str:
    """객체명을 화면용 한글로 바꿉니다. 모르는 값은 원문 그대로 둡니다.

    DB 에는 영문 원문(젯슨 text_prompt 의 클래스)이 그대로 쌓입니다 — 조회와
    집계가 그 키로 돌아가므로 저장 값은 건드리지 않고 **표시할 때만** 바꿉니다.

    이 변환이 없던 동안 같은 사건이 화면마다 다른 이름으로 보였습니다.
    대시보드는 "person with no helmet", 미니맵은 "헬멧 미착용" 이었습니다.
    """
    # None 만 거르면 안 됩니다. 전 컬럼이 NULL 인 행이 있으면 pandas 가 컬럼을
    # float 으로 만들면서 None -> NaN 이 되는데, float("nan") 은 truthy 라
    # `name or ""` 를 그냥 통과해 화면에 "nan" 이 찍힙니다. NaN 은 자기 자신과
    # 같지 않다는 성질로 걸러냅니다.
    if name is None or name != name:
        return "-"
    key = str(name).strip()
    return OBJECT_KO_NAME.get(key, key or "-")


def _plain_plan_text(value) -> str:
    """관리자 대응안을 대시보드용 일반 텍스트로 정리합니다.

    경량 LLM은 지시에도 마크다운 제목과 굵은 글씨를 반환할 수
    있습니다. HTML 카드에서는 그 기호가 서식이 아니라 문자로 보이므로,
    출력 시점에서 제거합니다. 괄호는 내용은 보존하고 기호만 빼서
    조치 내용이 유실되지 않게 합니다.
    """
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    # [표시 문구](URL) 형태는 표시 문구만 남깁니다.
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", text)
    text = re.sub(r"(?m)^\s*[-*+]\s+", "", text)
    text = re.sub(r"(?m)^\s*>\s?", "", text)
    text = text.replace("**", "").replace("__", "").replace("`", "")
    text = text.replace("(", " ").replace(")", " ")
    text = text.replace("（", " ").replace("）", " ")
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _plan_text_html(value) -> str:
    """정리한 대응안을 안전한 HTML 줄바꿈으로 변환합니다."""
    return html.escape(_plain_plan_text(value)).replace("\n", "<br>")


def _plan_row_class(risk_level: str, stale: bool) -> str:
    """요약 줄의 왼쪽 색띠. 오래된 것은 등급과 무관하게 회색입니다."""
    if stale:
        return "sfp-plan-row"
    return {
        "위험": "sfp-plan-row danger",
        "주의": "sfp-plan-row warn",
    }.get(risk_level, "sfp-plan-row")


def _generation_label(mode) -> str:
    """저장된 생성 경로를 관제자용 표시로 바꿉니다.

    +llm은 LLM 서버를 단순히 켠 것이 아니라 실제 응답 문장을 채택했다는
    뜻입니다. keyword/vector/builtin만 있으면 LLM 실패 또는 미사용 폴백입니다.
    """
    value = str(mode or "unknown").strip().lower()
    if value in {"", "unknown", "none", "nan"}:
        return "❔ 기존 기록 · 생성 방식 미기록"
    base = value.replace("+llm", "")
    source = {
        "keyword": "키워드 RAG",
        "vector": "벡터 RAG",
        "builtin": "내장 지침",
        "mock": "테스트 Mock",
    }.get(base, "출처 미기록")
    return f"🧠 LLM 생성 · {source}" if value.endswith("+llm") else f"📋 규칙 폴백 · {source}"


def _render_plan_panel():
    """영상 옆 "관리자용 상세 대응안" 패널.

    v10까지는 최신 1건만 보여줬습니다. 그런데 대응안은 (구역, 객체)마다 따로
    나오므로, 두 구역에서 동시에 뭔가 벌어지면 한쪽이 화면에서 통째로 사라집니다.
    지금은 첫 건을 크게, 나머지를 한 줄씩 보여주고, 그보다 이전 것은 "이전 기록"
    화면으로 들어가서 봅니다.

    무엇을 첫 칸에 둘지는 fetch_plans(prioritize=True) 가 정합니다 — 최근
    PLAN_FRESH_SECONDS 안에서는 심각한 순, 그 밖은 최신 순입니다.
    """
    st.caption("🧠 관리자용 상세 대응안 (LLM/RAG)")
    try:
        plans = fetch_plans(limit=PLAN_PANEL_LIMIT, prioritize=True)
    except Exception:
        st.markdown('<div class="sfp-idle">대응안 조회 실패</div>', unsafe_allow_html=True)
        return

    # DB의 '가장 최근 기록'은 현재 상황이 아닙니다. 이 필터가 없으면
    # 대시보드를 처음 열었을 때 7시간 전 화재 대응안도 현재 경보처럼
    # 즉시 나타납니다. 지난 대응안은 '이전 기록'에서만 조회합니다.
    plans = [
        plan for plan in plans
        if plan.get("age_seconds") is not None
        and float(plan["age_seconds"]) <= PLAN_FRESH_SECONDS
    ]

    if not plans:
        st.markdown(
            '<div class="sfp-idle">현재 활성 경보가 없습니다.<br>'
            "실제 위험 이벤트가 발생하면 상세 대응안이 여기에 표시됩니다.</div>",
            unsafe_allow_html=True,
        )
        _render_plan_history_link()
        return

    p = plans[0]
    when = p["created_at"].strftime("%H:%M:%S") if p["created_at"] else "-"

    # 상대 시각을 붙입니다. 시:분:초만 있으면 어제 14:32 의 대응안이 5분 전 것과
    # 똑같이 빨간 테두리로 떠 있어서, 상황이 끝난 뒤에도 화면은 계속 경보
    # 상태로 보였습니다. PLAN_FRESH_SECONDS 를 넘으면 경보색을 뺍니다.
    age = p.get("age_seconds")
    stale = age is not None and float(age) > PLAN_FRESH_SECONDS
    when = f"{when} ({_ago(age)})" if age is not None else when

    # confidence 는 RagResponseAgent 가 0.0~1.0 으로 넣습니다 (response_agent.py 참고).
    conf = f" · 확신도 {float(p['confidence']):.0%}" if p["confidence"] is not None else ""
    # reference_docs 는 "\n".join(...) 으로 저장되는데 HTML에서는 줄바꿈이
    # 그냥 공백으로 뭉개지므로, 한 줄 카드에서는 가운뎃점으로 끊어 보여줍니다.
    docs = " · ".join(x.strip() for x in str(p["reference_docs"] or "").splitlines() if x.strip())
    generation = _generation_label(p.get("generation_mode"))

    # recommended_action / reference_docs 는 LLM이 만든 문자열이 그대로
    # 들어옵니다. HTML로 그리는 이상 예외 없이 이스케이프합니다.
    st.markdown(
        f'<div class="sfp-plan{" stale" if stale else ""}">'
        f'<div class="h">{html.escape(str(p["zone"] or "-"))} 구역 · '
        f'{html.escape(_obj_ko(p["detected_object"]))} · '
        f'{html.escape(str(p["risk_level"] or "-"))} · {when}{conf}'
        + ("  ·  지난 대응안" if stale else "")
        + "</div>"
        f'<div class="g">{html.escape(generation)}</div>'
        f'<div class="a">{_plan_text_html(p["recommended_action"] or "권장 조치 없음")}</div>'
        + (f'<div class="r">근거: {html.escape(docs)}</div>' if docs else "")
        + "</div>",
        unsafe_allow_html=True,
    )

    # 현장 방송 버튼. 관리자용 상세 대응안을 그대로 읽지 않고, 짧은 현장
    # 경보 문장만 보냅니다. 한 번 나간 방송은 되돌릴 수 없습니다.
    st.button("🔊 현장 짧은 안내 방송", key=f"tts_{_plan_key(p)}",
              on_click=_speak_plan, args=(_plan_key(p), dict(p)),
              help="관리자용 상세 대응안 대신 짧고 명확한 현장 문장을 젯슨 스피커로 방송합니다.",
              **_full_width_kwargs())
    _render_tts_result()

    # --- 나머지 몇 건은 한 줄 요약으로 --------------------------------
    # 권장 조치 문장은 넣지 않습니다. 세 줄이 다 문장이면 어느 것이 지금 중요한지
    # 눈으로 못 고릅니다. 여기서는 "어디서 무엇이" 까지만 보여주고, 자세한 내용은
    # 이전 기록 화면에서 봅니다.
    for extra in plans[1:]:
        e_age = extra.get("age_seconds")
        e_stale = e_age is not None and float(e_age) > PLAN_FRESH_SECONDS
        st.markdown(
            f'<div class="{_plan_row_class(extra["risk_level"], e_stale)}">'
            f'<span>{html.escape(str(extra["zone"] or "-"))} 구역 · '
            f'{html.escape(_obj_ko(extra["detected_object"]))} · '
            f'{html.escape(str(extra["risk_level"] or "-"))} · '
            f'{html.escape(_generation_label(extra.get("generation_mode")))}</span>'
            f'<span class="w">{_ago(e_age)}</span>'
            f'</div>',
            unsafe_allow_html=True,
        )

    _render_plan_history_link()


def _render_plan_history_link():
    """"이전 기록" 으로 들어가는 버튼.

    조각(fragment) 안에서 눌리지만, on_click 이 쿼리 파라미터를 바꾸므로
    스크립트 전체가 다시 돌면서 기록 화면으로 전환됩니다.
    """
    st.button("🗂 이전 기록 보기", key="plan_history_btn",
              on_click=_set_expand, args=(EXPAND_PLANS,),
              help="지금까지 생성된 AI 대응안을 기간별로 전부 봅니다.",
              **_full_width_kwargs())


RISK_MARK = {"위험": "🔴 위험", "주의": "🟡 주의", "정상": "🟢 정상"}
_RISK_ROW_CSS = {
    "위험": "background-color:#c1121f; color:#ffffff",
    "주의": "background-color:#fff4d6; color:#7c4a03",
}


def _show_table(df, sort_key_column: str = "위험도", download_name: str = None):
    """표를 그립니다. 위험도에 따라 행 배경을 칠하고, 실패하면 그냥 그립니다.

    30줄짜리 흑백 표에서 '위험' 행을 눈으로 찾는 것은 관제 화면에서 제일
    하기 싫은 일입니다. 이모지(🔴/🟡/🟢)는 항상 붙고, 행 배경은 Styler 로
    칠합니다. Styler 는 jinja2 를 요구하므로(streamlit -> altair 의존성으로
    보통 이미 깔려 있습니다) 없으면 조용히 이모지만 남깁니다.

    download_name 을 주면 CSV 내려받기 버튼을 함께 답니다.
    """
    # NULL 을 그대로 두면 표에 "NaN" 이 찍힙니다(pandas 가 None 을 그렇게 바꿉니다).
    # 숫자 컬럼은 건드리지 않습니다 — 문자열로 채우면 dtype 이 통째로 바뀝니다.
    df = df.copy()
    text_cols = list(df.select_dtypes(exclude="number").columns)
    if text_cols:
        df[text_cols] = df[text_cols].fillna("-")

    width = _full_width_kwargs()
    try:
        def _paint(row):
            css = ""
            for level, style in _RISK_ROW_CSS.items():
                if level in str(row[sort_key_column]):
                    css = style
                    break
            return [css] * len(row)

        st.dataframe(df.style.apply(_paint, axis=1),
                     hide_index=True, height=380, **width)
    except Exception:
        st.dataframe(df, hide_index=True, height=380, **width)

    if download_name:
        _download_csv(df, download_name)


def _download_csv(df, base_name: str) -> None:
    """화면에 보이는 그대로를 CSV 로 내려받게 합니다.

    인코딩이 utf-8-sig 인 것이 핵심입니다. 그냥 utf-8 로 주면 Excel 이 BOM 이
    없다는 이유로 시스템 코드페이지(한국어 Windows 는 cp949)로 열어서 한글이
    전부 깨집니다. 주간 보고에 붙이는 것이 목적이라 이게 안 되면 기능이 없는
    것과 같습니다.

    보이는 프레임을 그대로 씁니다 — 원본 컬럼명이 아니라 화면의 한글 헤더가,
    원시 float 이 아니라 "1.24 m" 가 들어갑니다. 표에서 본 것과 파일이 다르면
    그게 더 헷갈립니다.
    """
    try:
        payload = df.to_csv(index=False).encode("utf-8-sig")
    except Exception:
        return
    stamp = time.strftime("%Y%m%d_%H%M%S")
    st.download_button(
        "⬇️ CSV 내려받기",
        data=payload,
        file_name=f"{base_name}_{stamp}.csv",
        mime="text/csv",
        key=f"dl_{base_name}",
        help="화면에 보이는 표를 그대로 내려받습니다 (Excel 호환 utf-8-sig).",
    )


def _selected_zone() -> str:
    return st.session_state.get("selected_zone", ZONE_ALL)


def _selected_span() -> str:
    return st.session_state.get("detail_span", DEFAULT_SPAN)


def _zone_label(zone: str) -> str:
    return "전체 구역" if zone == ZONE_ALL else f"{zone} 구역"


def _zone_slug(zone: str) -> str:
    """파일명에 넣을 구역 표기. 공백/한글이 섞이지 않게."""
    return "all" if zone == ZONE_ALL else str(zone)


def _render_log_summary():
    """연속 구간으로 접은 감지 요약.

    단순 LIMIT 30 은 30fps 앞에서 "최근 1초"라 이력 조회로 쓸 수 없었습니다
    (fetch_log_summary 주석 참고).
    """
    zone, span = _selected_zone(), _selected_span()
    try:
        rows = fetch_log_summary(zone, span)
    except Exception as e:
        st.warning(f"감지 요약 조회 실패: {e}")
        return

    if not rows:
        st.info(f"{_zone_label(zone)} · {span}에 감지 기록이 없습니다.")
        return

    df = pd.DataFrame(rows)
    # NULL 이 섞이면 컬럼이 object dtype 이 되어 .dt 접근이 AttributeError 로
    # 죽습니다. to_datetime 을 한 번 거치면 NaT 로 정규화됩니다.
    first = pd.to_datetime(df["first_time"], errors="coerce")
    last = pd.to_datetime(df["last_time"], errors="coerce")
    df["기간"] = (last.dt.strftime("%H:%M:%S") + " ~ " + first.dt.strftime("%H:%M:%S"))
    df["지속"] = ((last - first).dt.total_seconds()
                  .map(lambda s: "-" if pd.isna(s) else f"{float(s):.1f}초"))
    df["건수"] = df["hits"]
    df["위험도"] = df["risk_level"].map(lambda r: RISK_MARK.get(r, r))
    # min_distance 는 distance >= 0 만 골라 집계했으므로, NULL 이면 그 구간
    # 전체가 거리 미상이라는 뜻입니다.
    df["최근접"] = df["min_distance"].map(
        lambda d: "측정불가" if d is None or pd.isna(d) else f"{float(d):.2f} m"
    )
    df["detected_object"] = df["detected_object"].map(_obj_ko)
    df = df.rename(columns={"zone": "구역", "detected_object": "객체명", "issue": "사유"})

    columns = ["기간", "지속", "건수", "객체명", "위험도", "최근접", "사유"]
    if zone == ZONE_ALL:
        columns.insert(3, "구역")
    _show_table(df[columns], download_name=f"감지요약_{_zone_slug(zone)}")
    st.caption(
        f"{span} · 최대 {_span(span)['scan_rows']:,}건의 원본 로그에서 연속된 같은 "
        f"(객체, 위험도) 구간을 한 줄로 접었습니다. "
        f"프레임 단위로 봐야 하면 '{VIEW_RAW}'를 선택하세요."
    )


def _render_raw_logs():
    """프레임 단위 원본. 30fps 라면 화면에 보이는 것은 최근 몇 초입니다."""
    zone, span = _selected_zone(), _selected_span()
    try:
        logs = fetch_raw_logs(zone, span)
    except Exception as e:
        st.warning(f"원본 로그 조회 실패: {e}")
        return

    if not logs:
        st.info(f"{_zone_label(zone)} · {span}에 감지 기록이 없습니다.")
        return

    df = pd.DataFrame(logs)
    df["log_time"] = pd.to_datetime(df["log_time"], errors="coerce")
    df["log_time"] = df["log_time"].dt.strftime("%H:%M:%S.%f").str[:-5]
    # 거리 -1.0 은 시차 계산 실패값입니다. 그대로 두면 "-1m 떨어진 물체"로
    # 읽히므로 화면에서는 측정불가로 바꿉니다.
    df["distance"] = df["distance"].map(
        lambda d: "측정불가" if d is None or float(d) < 0 else f"{float(d):.2f} m"
    )
    df["risk_level"] = df["risk_level"].map(lambda r: RISK_MARK.get(r, r))
    df["detected_object"] = df["detected_object"].map(_obj_ko)
    df = df.rename(columns={
        "log_time": "시간", "zone": "구역", "detected_object": "객체명",
        "distance": "거리", "box_position": "카메라 내 위치",
        "risk_level": "위험도", "issue": "사유",
    })

    columns = ["시간", "객체명", "거리", "카메라 내 위치", "위험도", "사유"]
    if zone == ZONE_ALL:
        columns.insert(1, "구역")
    _show_table(df[columns], download_name=f"원본로그_{_zone_slug(zone)}")
    st.caption(
        f"{span} 안에서 최신 {RAW_LOG_LIMIT}건. 젯슨이 30fps로 적재하므로 실제로는 "
        f"몇 초치밖에 안 됩니다 — 이력을 보려면 '{VIEW_SUMMARY}'를 쓰세요."
    )


def _render_plan_history():
    """"이전 기록" 화면. 패널의 버튼으로 들어옵니다.

    상세 조회(patrol_logs)와 기간 선택지를 공유하지 않습니다. patrol_logs 는
    30fps로 쌓여서 '전체'를 열어줄 수 없지만, response_plans 는 억제가 걸려
    있어서 전체를 훑어도 안전합니다. 그래서 여기만 시간/일 단위입니다.

    자동 갱신도 걸지 않습니다 — 지나간 기록을 읽는 화면이라 몇 초마다 표가
    다시 그려지면 읽던 위치만 잃습니다.
    """
    head, close = st.columns([9, 1])
    with head:
        st.subheader("🗂 AI 대응안 기록")
    with close:
        st.button("✖ 닫기", key="plan_history_close",
                  on_click=_set_expand, args=(None,),
                  help="관제 화면으로 돌아갑니다.", **_full_width_kwargs())

    col_zone, col_span = st.columns([3, 3])
    with col_zone:
        st.radio("구역", ZONE_OPTIONS, horizontal=True, key="plan_zone",
                 format_func=_zone_label)
    with col_span:
        st.radio("기간", PLAN_SPAN_OPTIONS, horizontal=True, key="plan_span")

    zone = st.session_state.get("plan_zone", ZONE_ALL)
    span = st.session_state.get("plan_span", DEFAULT_PLAN_SPAN)
    minutes = PLAN_SPAN_MINUTES.get(span, PLAN_SPAN_MINUTES[DEFAULT_PLAN_SPAN])

    try:
        # v7까지 zone 을 넘기지 않아서, 구역 라디오를 눌러도 표가 그대로였습니다
        # (사용자 입장에서는 화면이 멈춘 것과 구분되지 않습니다).
        plans = fetch_plans(limit=PLAN_HISTORY_LIMIT, zone=zone, minutes=minutes)
    except Exception as e:
        st.warning(f"대응안 조회 실패: {e}")
        return

    if not plans:
        st.info(f"{_zone_label(zone)} · {span}에 생성된 대응안이 없습니다. "
                "위험 등급 이벤트가 발생하면 여기에 쌓입니다. "
                "더 이전 것을 보려면 기간을 넓히세요.")
        return

    df = pd.DataFrame(plans)
    df["created_at"] = pd.to_datetime(df["created_at"], errors="coerce")
    df["경과"] = df["age_seconds"].map(_ago)
    df["created_at"] = df["created_at"].dt.strftime("%m-%d %H:%M:%S")
    df["risk_level"] = df["risk_level"].map(lambda r: RISK_MARK.get(r, r))
    # None 만 거르면 안 됩니다. NULL 이 하나라도 섞이면 pandas 가 컬럼을 float 으로
    # 만들면서 None -> NaN 이 되고, f"{nan:.0%}" 는 예외 없이 "nan%" 를 찍습니다.
    df["confidence"] = df["confidence"].map(_percent)
    df["detected_object"] = df["detected_object"].map(_obj_ko)
    df["generation_mode"] = df["generation_mode"].map(_generation_label)
    df["recommended_action"] = df["recommended_action"].map(_plain_plan_text)
    df = df.rename(columns={
        "created_at": "생성 시각", "zone": "구역", "detected_object": "객체명",
        "risk_level": "위험도", "recommended_action": "권장 조치",
        "reference_docs": "근거 문서", "confidence": "확신도",
        "generation_mode": "생성 방식",
    })

    columns = ["생성 시각", "경과", "객체명", "위험도", "생성 방식", "권장 조치", "확신도", "근거 문서"]
    if zone == ZONE_ALL:
        columns.insert(2, "구역")
    _show_table(df[columns], download_name=f"대응안_{_zone_slug(zone)}")
    st.caption(
        f"{span} · 최대 {PLAN_HISTORY_LIMIT}건. 같은 (구역, 객체) 조합은 30초에 "
        "한 번만 생성되므로(ResponsePlanThrottle), 같은 상황이 계속돼도 표가 "
        "같은 문장으로 채워지지 않습니다."
    )


def _render_detail():
    # 레이블 문자열을 startswith 로 훑던 것을 상수 비교로 바꿨습니다. 예전에는
    # 이모지 하나만 바꿔도 분기가 조용히 다른 화면으로 떨어졌습니다.
    view = st.session_state.get("detail_view", VIEW_SUMMARY)
    if view == VIEW_RAW:
        _render_raw_logs()
    else:
        _render_log_summary()


# ==========================================
# 6. 자동 갱신 래퍼
# ==========================================
def auto(fn, seconds):
    """자동 갱신이 켜져 있으면 run_every 조각으로, 꺼져 있으면 정지 조각으로.

    st.fragment 가 없는 구버전 Streamlit 에서는 함수를 그대로 돌려주고,
    스크립트 맨 끝의 fallback 이 전체 rerun 을 담당합니다.
    """
    if not hasattr(st, "fragment"):
        return fn
    if auto_refresh:
        return st.fragment(run_every=seconds)(fn)
    return st.fragment(fn)


def _render_robot_panel():
    """로봇 상태와 순찰 제어를 같은 주기로 함께 갱신합니다."""
    render_robot_status(daeun_laptop_ip)
    st.markdown("---")
    render_patrol_control(daeun_laptop_ip)


render_status = auto(_render_status, REFRESH_STATUS_SECONDS)
render_robot_panel = auto(_render_robot_panel, REFRESH_STATUS_SECONDS)
render_plan_panel = auto(_render_plan_panel, REFRESH_PLAN_SECONDS)
# 상세 조회만 주기가 가변입니다. 넓은 범위는 훑는 행이 많은데, 30분치를 보고
# 있다는 것은 지금 흐르는 상황이 아니라 지나간 일을 읽고 있다는 뜻이라 자주
# 갱신할 이유도 없습니다. 범위를 바꾸면 스크립트가 통째로 다시 돌면서 이 조각도
# 새 주기로 다시 만들어집니다.
render_detail = auto(_render_detail, refresh_detail_seconds)


# ==========================================
# 7. 화면 구성 — 위에서부터 중요한 순서대로
# ==========================================

# (1) 경보 + 구역 상태. 스크롤 없이 보이는 위치.
#     확대 중에도 이건 **항상** 그립니다. 관제 화면에서 경보를 가리는 모드는
#     있으면 안 됩니다 — 영상을 크게 보려고 눌렀다가 그 사이 난 위험을 놓치면
#     확대 기능이 사고 원인이 됩니다.
render_status()

expanded = _expanded_panel()
focus_mode = layout_mode == LAYOUT_FOCUS

if expanded == EXPAND_PLANS:
    # ---------- AI 대응안 이전 기록 ----------
    # 자체 제목/닫기/필터를 갖고 있어서 여기서는 부르기만 합니다.
    st.markdown("---")
    _render_plan_history()

elif expanded:
    # ---------- 크게 보기 ----------
    # 한 패널만 본문 전체에 띄우고 나머지는 접습니다.
    st.markdown("---")
    title, close = st.columns([9, 1])
    with title:
        st.subheader("📹 실시간 순찰 영상 (확대)" if expanded == EXPAND_VIDEO
                     else "🗺️ 순찰 위치 지도 (SLAM) — 확대")
    with close:
        st.button("✖ 닫기", key="expand_close",
                  on_click=_set_expand, args=(None,),
                  help="원래 화면으로 돌아갑니다.",
                  **_full_width_kwargs())

    if expanded == EXPAND_VIDEO:
        # 확대 상태에서도 영상을 링크로 감쌉니다 — 다시 클릭하면 닫힙니다.
        render_video(jetson_ip, link=_expand_link(None), expanded=True)
    else:
        render_minimap(daeun_laptop_ip, height=MINIMAP_EXPANDED_HEIGHT)

    st.caption("확대 중에는 나머지 영역이 접힙니다. 위 경보와 구역 상태는 그대로 "
               "갱신됩니다.")

else:
    st.markdown("---")

    # (2) RGB 영상 | AI 권장 조치.
    #     미니맵은 여기에 넣지 않습니다 — 폭 900px 밑으로 내려가면 좁은 화면용
    #     레이아웃으로 접혀서 지도가 300px밖에 안 남습니다(v7 주석 참고).
    head_video, btn_video = st.columns([9, 1])
    with head_video:
        st.subheader("📹 실시간 순찰 영상")
    with btn_video:
        st.button("⛶ 크게", key="expand_video_btn",
                  on_click=_set_expand, args=(EXPAND_VIDEO,),
                  help="RGB 영상을 화면 전체 폭으로 봅니다. 영상을 직접 눌러도 됩니다.",
                  **_full_width_kwargs())

    # 오른쪽 칸은 터틀봇 상태(배터리 등)가 들어갈 자리입니다.
    col_rgb, col_status = st.columns([8, 7])
    with col_rgb:
        # 영상 자체를 클릭해도 확대됩니다. MJPEG 은 자체 조작이 없는 정지영상
        # 스트림이라 클릭을 가져가도 뺏기는 기능이 없습니다.
        st.caption("RGB · AI 탐지 박스")
        render_video(jetson_ip, show_caption=False, link=_expand_link(EXPAND_VIDEO))
    with col_status:
        st.caption("터틀봇 상태 · 배터리")
        # 상태와 제어 결과가 외부 이벤트에도 따라오도록 별도 자동 갱신 조각으로 묶습니다.
        render_robot_panel()

    st.markdown("---")

    # (3) 미니맵 + AI 대응안.
    #     지도 옆에 대응안을 두는 이유는, 관제자가 "어디서 났는지"와 "무엇을 할지"를
    #     눈을 옮기지 않고 함께 보기 위해서입니다. 예전에는 대응안이 영상 옆에 있어서
    #     지도와 대응안이 세로로 멀리 떨어져 있었습니다.
    #
    #     폭 배분에 주의가 필요합니다. minimap_web.html 은 좁아지면 좁은 화면용
    #     레이아웃으로 접혀서 탐지 목록이 지도 밑으로 내려갑니다. 그래서 지도에
    #     7/10 을 주고, HTML 쪽 접힘 기준도 900px -> 760px 으로 함께 낮췄습니다
    #     (1366px 노트북에서도 지도 칸이 약 870px 이라 접히지 않습니다).
    #
    #     지도는 영상과 달리 **링크로 감싸지 않습니다.** iframe 안의 클릭은 지도
    #     자신의 확대/이동/탐지목록 조작이라, 가로채면 미니맵을 못 쓰게 됩니다.
    #     그래서 확대는 옆의 버튼으로만 엽니다.
    head_map, btn_map = st.columns([9, 1])
    with head_map:
        st.subheader("🗺️ 순찰 위치 지도 (SLAM)")
    with btn_map:
        st.button("⛶ 크게", key="expand_map_btn",
                  on_click=_set_expand, args=(EXPAND_MAP,),
                  help="지도를 더 높게 봅니다. 지도 안의 클릭은 지도 자체 조작이라 "
                       "확대에 쓰지 않습니다.",
                  **_full_width_kwargs())

    col_map, col_plan = st.columns([7, 3])
    with col_map:
        render_minimap(daeun_laptop_ip,
                       height=MINIMAP_FOCUS_HEIGHT if focus_mode else None)
    with col_plan:
        render_plan_panel()

    # (4) 상세. '관제 집중'에서는 통째로 접습니다 — 영상과 지도를 한 화면에 넣는 것이
    #     이 모드의 목적이라, 아래에 표가 붙어 있으면 그만큼 스크롤이 생깁니다.
    if focus_mode:
        st.caption("상세 조회는 '관제 집중' 화면 구성에서 숨겨집니다. "
                   "사이드바에서 '전체'로 바꾸면 다시 나옵니다.")
    else:
        st.markdown("---")

        # 선택 위젯은 조각 "바깥"에 둡니다 — 조각 안에 넣으면 자동 갱신 때마다 다시
        # 그려져서 선택이 흔들립니다. key= 로 session_state 에 바로 묶여 있으므로
        # 조각 안에서는 값만 읽습니다.
        #
        # 구역에 '전체'를 넣었습니다. 세 보기 모두 이 선택을 따르므로, "지금 어디서든
        # 무슨 일이 있었나"를 한 번에 볼 수 있고, 동시에 v7의 "대응안 이력에서는
        # 구역 라디오가 눌리기만 하고 표는 안 바뀌는" 문제도 사라집니다.
        st.subheader("📊 상세 조회")
        col_zone, col_view, col_span = st.columns([2, 3, 2])
        with col_zone:
            st.radio("구역", ZONE_OPTIONS, horizontal=True, key="selected_zone",
                     format_func=_zone_label)
        with col_view:
            st.radio("보기", DETAIL_VIEWS, horizontal=True, key="detail_view")
        with col_span:
            st.radio("범위", DETAIL_SPAN_OPTIONS, horizontal=True, key="detail_span",
                     help="넓게 볼수록 갱신 주기가 길어집니다(DB 부하를 일정하게 "
                          "유지하기 위함). 지금 주기는 사이드바에 표시됩니다.")

        render_detail()


# ==========================================
# 8. 구버전 Streamlit fallback
# ==========================================
# st.fragment 가 없으면 전체 스크립트를 재실행하는 수밖에 없습니다.
# v1/v2와 동일하게 무거우므로, 가능하면 Streamlit 을 올리는 것을 권장합니다.
if not hasattr(st, "fragment") and auto_refresh:
    time.sleep(REFRESH_STATUS_SECONDS)
    st.rerun()
