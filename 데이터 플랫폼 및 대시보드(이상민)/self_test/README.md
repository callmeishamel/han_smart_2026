# self_test — DB/네트워크 없이 돌려보는 단독 테스트 모음

프로젝트 전체 배경과 실행 순서는 최상위 [`../README.md`](../README.md)를 참고하세요.
이 폴더의 스크립트는 전부 `python`(또는 `python3`) 하나로 실행되며, 외부 서비스(DB,
Jetson, ROS2)가 없어도 핵심 로직을 검증할 수 있게 만든 것입니다. 크게 두 종류로 나뉩니다.

- **가짜 송신/수신기**: 실제 컴포넌트(Jetson, 이다은님 노트북)를 흉내내서 다른 스크립트를
  손으로 통합 테스트할 때 쓰는 도구 — `fake_jetson_sender.py`, `fake_minimap_server.py`.
- **자동 검증 스크립트 19개**: `assert` 기반으로 위험도·RAG·비동기 worker·대시보드·
  팀 간 배선·Jetson 송신·ROS2 주행·TTS를 DB/네트워크 없이 확인합니다. 아래 파일 구성
  표가 현재 전체 목록입니다.

전부 한 번에 돌리려면:

```powershell
Get-ChildItem self_test\test_*.py | ForEach-Object { python $_.FullName }
```

## 파일 구성

| 파일 | 설명 |
|---|---|
| `fake_jetson_sender.py` | 가짜 UDP 젯슨 패킷 11개를 9998로 전송 — `pipeline/jetson_patrol_pipeline.py` 수동 테스트용. **현재 비전 라벨 3종 + 구버전/미등록 라벨** |
| `fake_minimap_server.py` | `minimap_renderer.py`의 엔드포인트 4종(`/`, `/map_data`, `/state`, `/detections`)을 흉내내는 표준 라이브러리 전용 가짜 서버 — `event_logger.py`와 **대시보드 미니맵 화면** 수동 테스트용 |
| `test_minimap_logic.py` | 이다은님 폴더 원본 `minimap_renderer.py`의 `raycast_to_obstacle`/`SpatialObjectTracker` 로직을 rclpy/flask 스텁으로 검증 |
| `test_response_agent.py` | `integration/response_agent.py`의 `RagResponseAgent`/`resolve_risk_type` 로직 검증 |
| `test_response_agent_llm.py` | 선택적 LLM 생성 경로와 실패 시 규칙 기반 fallback 검증 |
| `test_response_plan_worker.py` | 비동기 대응안 worker의 큐 처리·종료·포화 정책 검증 |
| `test_schema_logic.py` | `common/schema.py`의 `classify_risk`/`build_detection_event`/`build_map_detection_event` 검증 |
| `test_response_throttle.py` | `common/schema.py`의 `ResponsePlanThrottle`(대응안 30초 중복 억제)/`risk_rank` 검증 |
| `test_dashboard_layout.py` | 대시보드 화면 조립 순서 + 미니맵 폭 + HTML 이스케이프 + 배너 3종(위험/미연결/상태불명) 검증 (Streamlit/pandas/DB 없이) |
| `test_dashboard_tables.py` | 상세 조회 표 3종을 **진짜 pandas 로** 그려봄 — layout 테스트가 못 닿는 사각지대 (pandas 없으면 거짓 통과 대신 실패) |
| `test_integration_wiring.py` | **팀 경계를 넘는 배선 계약** 검증 — 포트 짝, 목적지 IP, 수신 바인딩, 페이로드 필드 이름 |
| `test_jetson_sender.py` | 젯슨 `ai_inference_sender.py` 검증 — `/health`, 목적지 분리, 프레임 축소, 대기화면 캐시 (nanoowl/PIL 스텁) |
| `test_person_marking.py` | **젯슨 `person` → 미니맵 작업자 마커** 경로를 세 팀 폴더에 걸쳐 통째로 검증 |
| `test_auto_mapper.py` | 자율 매핑 주행(`auto_mapper.py`)의 **"어디로 갈지" 판단** 검증 — 미탐사 선호, 방문 기억, 회전 진동 방지 |
| `test_stereo_calibration.py` | 젯슨 `stereo_calibrate.py` 검증 — 저장/로드, 해상도 조정, 실측 f·B 복원, sender 배선 (카메라·체스보드 불필요) |
| `test_patrol_planner.py` | **지도 → 순찰 경로** 생성 검증 — 여유 공간, 도달 가능성, 구역 커버리지, 측지거리 순서 (ROS2 불필요) |
| `test_map_cleaner.py` | `maps/clean_map.py` 검증 — 의자 다리는 지우고 **벽·기둥·미탐사 옆은 남기는지** |
| `test_stall_monitor.py` | **LiDAR 사각의 낮은 장애물** 감지 검증 — 걸림 판정, 헛알람 억제, 기록 영속성, keepout 마스크 |
| `test_real_navigation_safety.py` | 실물 관제 스택의 낮은 장애물 배선과 순찰 정지 UI 정책 검증 |
| `test_scan_timing.py` | LaserScan 타임스탬프 교정과 중복 차단 로직 검증 |
| `test_tts_pipeline.py` | TTS 송수신 검증 — 최상위 `TTS Engine and pipeline/` 의 원본을 불러 돌리는 얇은 다리 |

## fake_jetson_sender.py

`ai_inference_sender.py`(젯슨) 없이도 `pipeline/jetson_patrol_pipeline.py`가 제대로
동작하는지 확인하기 위한 가짜 UDP 송신기. `FAKE_PACKETS` 11개를 `127.0.0.1:9998`로
0.5초 간격 전송합니다.

**라벨을 젯슨이 실제로 보내는 것과 맞췄습니다.** 예전에는 구버전 라벨
(`human`/`obstacle`/`hazardous leak`)만 보내서, 이 테스트가 전부 통과해도 **지금 실제로
오는 라벨이 올바르게 처리되는지는 검증되지 않았습니다.** 특히 안전모 미착용은
파이프라인이 "위험"으로 판정해야만 대응안이 생성되는데, 그 경로가 한 번도 확인되지
않았습니다.

| 구분 | 보내는 것 |
|---|---|
| 현재 비전 라벨 | `fire`, `person with no helmet`(거리 정상/측정실패), `vehicle`(1.2m/2.5m/8.0m — 위험/주의/정상 경계) |
| 한 프레임 2건 | `fire` + `vehicle` 동시 |
| 구버전 라벨 | `human`, `obstacle` — 폴백이 살아 있는지 |
| 미등록 라벨 | `forklift` — "미분류 → 주의" 경로 |
| 깨진 payload | `bbox` 없음 — 죽지 않는지 |

각 항목에 **기대 위험도**가 함께 출력되므로, 파이프라인 터미널의 `위험도:` 와 눈으로
대조하면 됩니다(예전에는 로그 줄 수만 세었습니다). 이 기대값 표가 `classify_risk()`와
어긋나지 않는지는 `test_schema_logic.py`가 자동으로 검사합니다 — 판정 정책을 바꿨는데
표만 옛날 값으로 남는 일을 막기 위해서입니다.

다른 PC의 파이프라인으로 보내려면 대상 IP를 인자로 주세요.

```powershell
python self_test\fake_jetson_sender.py 203.0.113.40
```

```powershell
# 터미널 A
python pipeline\jetson_patrol_pipeline.py --zone A
# "UDP 수신 대기 시작" 로그가 뜰 때까지 대기

# 터미널 B
python self_test\fake_jetson_sender.py
```

터미널 A에 "적재 완료 [A] ..." 로그가 5번 찍히고, 대시보드에서 A 구역 카드 상태와 사이드바
로그가 갱신되면 성공입니다.

## fake_minimap_server.py

이다은님 노트북(ROS2+SLAM)이나 실제 `minimap_renderer.py` 없이도 `event_logger.py`와
**대시보드 미니맵 화면**을 확인하는 가짜 서버. Flask 없이 표준 라이브러리
(`http.server`)만으로 실제 서버와 같은 엔드포인트를 흉내냅니다.

| 경로 | 내용 |
|---|---|
| `/` | 미니맵 페이지 — 대시보드 iframe이 부르는 주소 |
| `/map_data` | 가짜 occupancy grid (`digital_twin_1.world`의 랙 배치, 268×188셀) |
| `/state` | `patrol_node`의 웨이포인트를 도는 로봇 위치 + 탐지 3건 |
| `/detections` | `event_logger.py`가 폴링하는 엔드포인트 — **형식 그대로**. `session`도 실제 서버와 같이 프로세스마다 새로 만들므로, 재시작 시 중복/누락 동작까지 확인할 수 있습니다 |
| `/health` | 상태 확인 |

미니맵 페이지 HTML은 **복사하지 않고** 이다은님 폴더의 `minimap_web.html`을
상대경로로 읽습니다(사본이 갈라지는 것을 막기 위해). 저장소를 통째로 받아야
`/`가 동작합니다.

> 여기서 나오는 지도·로봇·탐지는 전부 가짜입니다.

**사용법 1 — event_logger.py 테스트**

```powershell
# 터미널 A
python self_test\fake_minimap_server.py

# 터미널 B
. ..\set_env.ps1
$env:DAEUN_LAPTOP_IP = "127.0.0.1"
python edge_video\event_logger.py
```

터미널 B에 "이벤트 기록: ..." 로그가 찍히고, `SELECT * FROM detection_events ORDER BY id
DESC LIMIT 5;`로 확인되면 성공입니다.

**사용법 2 — 대시보드 미니맵 화면 테스트**

```powershell
# 터미널 A
python self_test\fake_minimap_server.py

# 터미널 B
$env:DAEUN_LAPTOP_IP = "127.0.0.1"
streamlit run dashboard\smart_factory_dashboard_v3.py
```

"🗺️ 순찰 위치 지도 (SLAM)" 섹션에 지도와 움직이는 로봇이 나오면 성공입니다.
브라우저에서 `http://127.0.0.1:8091/`로 직접 열어도 됩니다.

## test_integration_wiring.py

4개 파트가 서로 다른 기기에서 돌기 때문에, 이 프로젝트에서 나는 사고는 전부 같은
모양입니다 — **아무 에러도 안 나고 그냥 데이터가 조용히 안 옵니다.** UDP는 목적지가
없어도 성공으로 처리되고, 포트나 필드 이름이 한쪽만 바뀌어도 예외가 없습니다.

이 테스트는 파일을 `import`하지 않고 **소스 텍스트에서 상수를 뽑아** 비교합니다.
젯슨 코드처럼 이 PC에서 아예 import되지 않는 파일(`cv2`, `nanoowl`, `PIL` 필요)도
검사해야 하기 때문입니다.

| 검사 | 내용 |
|---|---|
| 포트 짝 | `ROS2_UDP_PORT` 9999 / `DASHBOARD_UDP_PORT` 9998 / `DETECTION_UDP_PORT` 9091 / `JETSON_VIDEO_PORT` 8500 / `MINIMAP_PORT` 8091 / `TTS_TCP_PORT` 9997 이 **송·수신 양쪽에서** 같은 값인지 |
| 목적지 IP | 젯슨이 9999(ROS2 브릿지)와 9998(DB 파이프라인)을 **서로 다른 변수**로 보내는지 |
| 수신 바인딩 | 파이프라인·어댑터의 UDP 바인딩 기본값이 루프백이 아닌지 |
| UDP 페이로드 | `object`/`bbox`/`distance_meter`, `label`/`angle_offset` 이 보내는 쪽·읽는 쪽 모두에 있는지 |
| `/detections` | 미니맵이 내보내는 키와 `detection_event_params`·`build_map_detection_event`가 읽는 키가 같은지, `last_seen`(epoch)을 `to_timestamp()`로 넣는지, 미니맵이 `session`을 함께 보내고 `event_logger`가 그걸 읽는지 |
| INSERT 위치 | `INSERT INTO` 문이 `common/schema.py` **밖에** 다시 생기지 않았는지 (컬럼을 늘렸을 때 한쪽만 고치는 사고 방지) |
| TTS | `{"text": ...}` JSON 을 보내고 받는지 |

실제로 이 테스트는 아래 두 가지를 잡아냈습니다.

1. **`ai_inference_sender.py`가 9999와 9998을 `HOST_IP` 하나로 보냈습니다.**
   두 기기가 다르므로 동시에 맞출 값이 없어, 둘 중 하나는 반드시 못 받는 구조였습니다.
2. **파이프라인이 `127.0.0.1`에만 바인딩했습니다.** 젯슨은 다른 기기라 패킷이 하나도
   도착하지 않는데, 로그에는 "UDP 수신 대기 시작"만 찍힙니다. `run_pipeline_rag.sh`를
   포함해 어떤 실행 스크립트도 `--udp-ip`를 바꾸지 않았습니다.

```powershell
python self_test\test_integration_wiring.py
```

## test_jetson_sender.py

`젯슨 연결용 프로그램(손준영)/ai_inference_sender.py`를 젯슨 없이 검증합니다.
**이 스크립트는 저장소에서 유일하게 테스트가 하나도 없던 코드였습니다** — NanoOWL /
torch / PIL 이 Jetson L4T 컨테이너에만 있어서 다른 PC에서는 `import` 자체가 안 되기
때문입니다. 그 두 개만 스텁으로 꽂고 나머지는 실제 코드 그대로 돌립니다
(`cv2`/`numpy`는 `requirements-vision.txt`에 있는 일반 pip 패키지라 그대로 씁니다).

- **`/health`**: 실제 HTTP 서버를 띄워 응답을 검사합니다. 프레임이 없으면 `ok=false`,
  갱신되면 `true`, 6초간 없으면 다시 `false`. **영상만으로는 이 구분이 불가능합니다**
  — 카메라가 빠져도 마지막 프레임이 계속 재전송되어 화면은 멀쩡해 보입니다.
- **목적지 분리**: `send_jobs`에 `ros2_ip`와 `dashboard_ip`가 따로 있고 예전의
  `server_ip` 하나를 함께 쓰지 않는지.
- **`shrink_for_stream()`**: depth 축소가 원본 배열을 건드리지 않는지(거리 판정은
  축소 전 원본으로 해야 함), 이미 작으면 그대로 반환하는지.
- **대기 화면 캐시**: 같은 객체를 재사용하는지(매 프레임 새로 그리지 않음).

`cv2`/`numpy`가 없거나 젯슨 폴더를 못 찾으면 실패가 아니라 **건너뜁니다.**

```powershell
python self_test\test_jetson_sender.py
```

## test_minimap_logic.py

`minimap_renderer.py`를 `rclpy`/`nav_msgs`/`geometry_msgs`/`tf2_ros`/`flask`가 설치되어
있지 않은 환경에서도 검증하기 위해, 이 모듈들을 `sys.modules`에 가짜(스텁) 모듈로 미리
등록한 뒤 `importlib`로 직접 로드합니다. 실제 ROS2 통신은 테스트하지 않고 순수 계산
로직만 검증합니다.

**검증 대상은 이다은님 폴더의 원본 하나입니다.** 예전에는 `edge_video/minimap_renderer.py`
(rclpy 없이 읽기 위한 사본)만 검증했는데, 두 파일은 이미 갈라져 있었습니다 —
원본은 tf2로 `map → base_footprint`를 조회하는데 사본은 `/amcl_pose`를 구독했고,
`SpatialObjectTracker`도 원본은 `list` + `next_id`, 사본은 `dict` + `count`로 자료구조도
생성자 시그니처도 달랐습니다. **즉 이 테스트가 통과해도 실제로 도는 코드를 보증하지
못했습니다.** 사본은 삭제했고, 지금은 원본만 로드합니다. 원본이 없으면(저장소를
일부만 받은 경우) 경고를 찍고 종료합니다 — 엉뚱한 파일을 검증하고 통과했다고
말하는 것보다 낫습니다. (`fake_minimap_server.py`가 `minimap_web.html`을 복사하지 않고
상대경로로 읽는 것과 같은 원칙입니다.)

- `raycast_to_obstacle()`: 가짜 occupancy grid(`FakeGrid`, x=3.0m 지점에 벽)에 대해
  정면(0도) 방향은 벽을 찾고, 90도(벽 없는 방향)는 `None`을 반환하는지 확인.
- `SpatialObjectTracker`: 같은 사람이 살짝 흔들리며 4번 검출되면 1개로 병합되고
  `count == 4`, EMA로 좌표가 평균 근처로 수렴하는지, 멀리 떨어진 검출은 `min_hits`
  미달로 결과에서 제외되는지 확인.
- **LiDAR 장애물 빨간 점 레이어가 제거된 상태인지**: `extract_mapped_obstacles`/
  `looks_like_wall`/`get_mapped_obstacles`가 없어야 하고, 반대로 `/scan` 기반
  `extract_live_scan_obstacles`/`get_live_obstacles`는 남아 있어야 합니다 — 이건
  화면 표시용이 아니라 작업자 마커 위치를 정하는 데 쓰기 때문입니다.

```powershell
python self_test\test_minimap_logic.py
```

## test_person_marking.py

"젯슨이 잡은 `person`이 대시보드 SLAM 미니맵에 **작업자**로 찍히는가"를 끝에서 끝까지
확인합니다. 이 경로는 세 팀 폴더를 지나가고, 어느 한 곳만 끊겨도 증상이 똑같습니다 —
"화면에 작업자가 안 보인다". 그래서 구간별로 나눠 검증합니다.

1. **젯슨** `ai_inference_sender.py` — `person`과 `safety helmet`을 따로 탐지하고,
   안전모가 사람 머리 영역에 4초 연속 보이지 않을 때만 `person with no
   helmet`으로 전환하는가. 안전모가 다시 보이면 즉시 `person`으로 복귀하고,
   내부 판정용 `safety helmet` 박스는 전송하지 않는지도 확인합니다.
2. **다은 노트북** `minimap_renderer.py` — 레이캐스팅이 사람을 찾는가. **사람은 SLAM
   지도에 없습니다.** occupancy grid는 벽·설비만 담으므로, `raycast_to_obstacle()`만
   쓰면 열린 통로의 작업자는 `None`이 되어 통째로 버려지고, 벽 앞의 작업자는 레이가
   사람을 통과해 뒤쪽 벽에 찍혔습니다. `raycast_to_live_obstacle()`이 `/scan` 기반
   실시간 장애물(= 지도에 없는 물체 = 그 작업자)을 먼저 봅니다.
3. **다은 노트북** MJPEG 미니맵 — `detection_marker_style()`이 작업자/화재/안전모
   미착용을 서로 다른 색으로 그리는가. 예전에는 전부 같은 빨간 점 + 영문 원문이라
   화면만 봐서는 구분할 수 없었습니다.
4. **상민 PC** `common/schema.py` — `build_map_detection_event()`가 `person`을
   `작업자`로 바꾸고 지도 좌표를 싣는가.

1번은 `cv2`/`nanoowl`이, 2·3번은 `rclpy`가 필요하므로 둘 다 **실제 소스 파일 그 자체**를
읽되 무거운 의존성 없이 돌립니다 (1번은 `ast`로 순수 함수만 떼어내 실행, 2·3번은
`test_minimap_logic.py`와 같은 스텁 방식). 사본을 두지 않는 것은 같은 원칙입니다 —
사본이 갈라지면 검증이 조용히 무의미해집니다.

```powershell
python self_test	est_person_marking.py
```

## test_auto_mapper.py

이다은님 폴더 `smart_factory_sim/auto_mapper.py`의 주행 **판단**만 rclpy 없이
검증합니다(실제 속도 명령·안전 정지는 로봇/시뮬레이터가 필요하므로 제외).

예전 auto_mapper는 지나온 자리를 전혀 기억하지 않는 순수 반응형 랜덤워크라,
"갔던 곳을 왔다갔다" 하는 것처럼 보였습니다. 지금은 `/map`의 미탐사 영역과 방문
기억을 보고 방향을 정합니다. 이 테스트가 보는 것:

- `MapView`: 월드 좌표 → 셀 조회. **지도 밖(`None`)과 미탐사(`-1`)를 구분**하는지
- `count_unknown_along()`: 미탐사 방향의 점수가 이미 본 방향보다 높은지,
  그리고 **벽 뒤의 미탐사는 세지 않는지**(세면 로봇이 벽으로 달려듭니다)
- `VisitMemory`: 진입 횟수가 쌓이고 `cap`을 넘지 않는지, 여러 번 지난 방향의
  점수가 깎이는지, `visit_weight=0`이면 무시되는지
- `_choose_turn_direction()`: 회전 직후에는 방향을 뒤집지 않고(좌-우-좌 진동 방지),
  `turn_commit_sec`이 지나면 다시 자유롭게 고르는지. 그리고 라이다상 넓은 쪽이
  아니라 **미탐사 쪽**을 고르는지
- `_maybe_redirect()`: 같은 칸에 `revisit_threshold`번 들어왔거나 `stuck_timeout`을
  넘겼을 때 방향을 다시 잡는지, 쿨다운 안에서는 다시 틀지 않는지
- **지도·TF가 없을 때** 재조정을 시도하지 않고 예전과 같은 반응형 주행으로
  물러나는지 (매핑 시작 직후가 이 상태입니다)

```powershell
python self_test	est_auto_mapper.py
```

## test_stereo_calibration.py

젯슨 `smart_factory_project/stereo_calibrate.py`를 **카메라와 체스보드 없이**
검증합니다. 거리(`distance_meter`)는 `Z = f × B / d`로 나오고 그 `f`는 미니맵
방향각(`atan2(dx, f)`)에도 쓰이므로, 이 값이 틀리면 위험도 판정과 작업자 마커
위치가 함께 틀어집니다.

- **저장/로드 왕복**: 실측값이 보존되는지. 그리고 **파일이 없거나 깨졌을 때
  예외가 아니라 `None`**을 돌려주는지 — 여기서 예외를 던지면 캘리브레이션이
  없다는 이유만으로 탐지 전체가 죽습니다.
- **해상도 조정**: 카메라가 요청한 해상도를 거부하고 다른 크기를 줄 때 내부
  파라미터를 비례로 맞추는지. **가로세로 비가 다르면 거부**하는지 — 비가 바뀌면
  리사이즈가 아니라 크롭이라 비례 조정이 성립하지 않습니다. 틀린 거리를 자신
  있게 내놓느니 거리를 포기(`-1` = "거리 미상")하는 쪽이 안전합니다.
- **f · B · 주점 복원**: 정답을 아는 합성 카메라 쌍을 넣고 `stereoRectify`가 그
  값을 되돌려 주는지. 같은 시차를 옛 추측값(500 / 0.06)으로 풀면 얼마나 틀리는지도
  함께 출력합니다.
- **sender 배선**: 모듈만 맞고 `ai_inference_sender.py`에 연결이 빠지면 아무것도
  달라지지 않는데 다른 테스트는 전부 통과합니다. 그래서 소스에서 직접 확인합니다 —
  import 가드, 캡처 단계 정렬, `focal_px`/`baseline_m` 사용, 방향각 기준이 화면
  중심이 아니라 주점인지, **젯슨 배포 폴더 사본이 갈라지지 않았는지**.

실제 렌즈 왜곡 보정 품질은 체스보드 촬영본이 있어야 하므로 젯슨에서
`python3 stereo_calibrate.py verify`가 담당합니다(정렬 후 평균 수직 어긋남).

opencv-python이 없으면 건너뜁니다.

```powershell
python self_test	est_stereo_calibration.py
```

## test_patrol_planner.py

이다은님 폴더 `smart_factory_sim/patrol_planner.py` — SLAM 지도에서 순찰 경로를
만드는 로직을 ROS2 없이 검증합니다(numpy + OpenCV 만 씁니다). 미터 단위로 방과
벽을 그려 가짜 지도를 만든 뒤, 계산이 맞는지 봅니다.

- **여유 공간**: 모든 지점이 벽에서 `min_clearance_m` 이상 떨어져 있는지.
  벽에 붙은 목표는 Nav2 가 영영 도달하지 못합니다.
- **미탐사를 장애물로 보는지**: 아직 본 적 없는 칸 옆에 지점을 두면, 나중에 그
  자리가 벽으로 밝혀졌을 때 목표가 벽 속에 들어갑니다.
- **도달 가능성**: 문이 없는 두 방을 만들고, 로봇이 있는 방에만 지점이 생기는지.
- **구역 커버리지**: 모든 구역에 지점이 하나 이상 있는지. 간격 규칙 때문에 구역이
  통째로 빠지지 않는지.
- **측지거리**: 빗(comb) 모양 지도에서 옆 통로까지의 거리가 직선거리보다 훨씬
  먼지(직선 4.0m ↔ 실제 11.4m). **직선거리로 순서를 정하면 벽을 뚫고 가는 순서가
  나옵니다** — 이게 이 모듈의 핵심 주장입니다. 실제 이동거리로 비교하면 측지거리
  순서 37.2m / 직선거리 순서 40.0m 입니다.
- **결정성**: 같은 지도면 같은 경로가 나오는지. 매번 달라지면 관제자가 "왜 오늘은
  다르게 도나" 를 계속 묻게 됩니다.
- **배선**: `patrol_node.py` 가 start/stop 을 받고 경로를 갈아끼우는지,
  `minimap_renderer.py` 가 `/patrol_plan` 을 열고 구역을 그대로 넘기는지.

opencv-python 이 없으면 건너뜁니다.

```powershell
python self_test	est_patrol_planner.py
```

## test_map_cleaner.py

이다은님 폴더 `maps/clean_map.py` — 저장된 지도에서 "치우면 없어질 물건"(의자
다리 등)을 지우는 도구를 검증합니다.

이 도구는 **지우면 안 되는 것을 지우지 않는 것**이 훨씬 중요합니다. 벽을 지우면
로봇이 벽으로 돌진합니다. 그래서 "지운다"보다 "안 지운다" 쪽 검증이 더 많습니다.

가짜 지도(10m × 8m)에 여섯 종류의 덩어리를 심고 판정을 봅니다.

| 심은 것 | 기대 |
|---|---|
| 의자 다리 1셀 | 지움 |
| 책상 다리 2×2 (0.1m) | 지움 |
| 기둥 8×8 (0.4m) | **남김** — 진짜 구조물 |
| 안쪽 칸막이벽 | **남김** — 얇아도 길다 |
| 미탐사에 닿은 점 | **남김** — 뒤를 본 적이 없다 |
| 벽에 붙은 점 | **남김** — 벽과 한 덩어리 |

그 외에 파일 입출력도 봅니다 — dry-run 이 파일을 안 만드는지, yaml 이 새 pgm 을
가리키고 해상도·원점은 그대로인지, **원본을 손대지 않는지**, 지운 셀 수만큼만
달라졌는지. 마지막으로 저장소의 실제 지도 3장을 읽다 죽지 않는지 확인합니다
(지금은 후보 0개가 정상입니다 — 가구가 있는 실물 현장 지도가 아직 없습니다).

opencv-python 이 없으면 건너뜁니다.

```powershell
python self_test	est_map_cleaner.py
```

## test_stall_monitor.py

이다은님 폴더 `smart_factory_sim/stall_monitor.py` — LiDAR 보다 낮은 장애물
(문턱·케이블 트레이·파렛트 하단)을 "못 나가는 것"으로 알아채는 로직을 검증합니다.

여기서 가장 중요한 건 **헛알람을 안 내는 것**입니다. 멀쩡한 통로를 장애물로 찍어
버리면 순찰 경로에서 그 길이 영영 빠집니다. 그래서 "안 울린다" 쪽 검증이 더 많습니다.

- **울려야 할 때**: 전진 명령이 있는데 3초간 5cm도 못 감 → 로봇 **앞쪽**(후진
  중이면 뒤쪽)에 장애물을 찍는지, 바라보는 방향을 따르는지
- **안 울려야 할 때**: 잘 가고 있을 때 · 명령이 없을 때 · 제자리 회전만 할 때 ·
  창을 못 채웠을 때 · 느려도 꾸준히 나갈 때 · **TF가 없어 위치를 모를 때**
- **쿨다운**: 30초 동안 2번만 울리는지 (같은 자리 도배 방지)
- **기록**: 가까운 재발견은 합쳐지고 걸린 횟수가 늘어나는지, 파일로 저장·복원되는지,
  **깨진 파일에도 예외 없이 0을 돌려주는지**
- **keepout 마스크**: 장애물 자리만 100이고 지도를 통째로 막지 않는지
- **배선**: `stall_monitor`가 **`/cmd_vel`을 발행하지 않는지**(Nav2·auto_mapper와
  충돌 방지), 그리고 `auto_mapper`·`patrol_planner`·미니맵이 결과를 실제로 쓰는지

```powershell
python self_test	est_stall_monitor.py
```

## test_tts_pipeline.py

진짜 테스트는 최상위 `TTS Engine and pipeline/test_tts_pipeline.py` 에 있습니다.
잘 만들어진 테스트인데(**실제 소켓 왕복**으로 검증) 그 폴더에만 있어서
`self_test	est_*.py` 를 한 번에 돌리는 평소 흐름에서 빠져 있었습니다.
아무도 안 돌리는 테스트는 없는 것과 같아서, 이 파일이 원본을 불러 돌립니다.
**사본을 만들지 않습니다** — 갈라지면 검증이 조용히 무의미해집니다.

원본이 보는 것:

- 실제 소켓으로 문장 왕복, 긴 한글 문장 자르기, 연속 전송
- 젯슨이 꺼져 있을 때 `send_alert_to_jetson` 이 `False` 를 돌려주는지
- 재생 큐가 우선순위(화재 > 작업자 > 차량)를 지키고, 포화 시 낮은 우선순위·
  오래된 경보를 교체하며 만료된 요청을 버리는지
- message ID 기반 queued/duplicate ACK와 bounded 송신 worker가 동작하는지
- ACK 유실 뒤에도 queued 상태를 보존하는지, ACK의 message ID가 없거나 다르면 실패하는지
- 송신 worker를 동시에 시작해도 하나만 생성되는지, 중복 상태 캐시가 상한을 지키는지
- 느린 클라이언트 3개가 붙어 있어도 진짜 경보가 도착하는지
- 토큰/허용 IP 로 거부되는지 — 스피커는 데이터 서버가 아니라 **액추에이터**입니다
- **USB 스피커 찾기** — HDMI 가 아니라 USB 를 고르는지, 카드 번호가 뒤바뀌어도
  같은 스피커를 가리키는지(이름 기반), `hw:` 가 아닌 `plughw:` 인지
- 기본 모델이 실재하는 음성(`ko_KR-kss-medium`)인지

```powershell
python self_test	est_tts_pipeline.py
```

## test_response_agent.py

`integration/response_agent.py`의 `RagResponseAgent`를 DB/네트워크 없이 검증합니다
(지식베이스 연결이 실패해도 `builtin` 폴백 경로로 계속 동작하는 설계 덕분에 가능).

5개 검증 블록:

1. **위험유형 매핑**: `resolve_risk_type()`이 `person with no helmet`→`PPE`,
   `fire`→`fire`, `vehicle`→`machinery`, 매핑에 없는 객체→`general`로 변환되는지.
2. **대응안 생성**: `CASES`(안전모 미착용/화재/차량·중장비/작업자(거리미상)/미분류 객체
   5개 시나리오)에 대해 `ResponsePlan` 반환, `zone` 일치, 길이 제약(`MAX_ACTION_LEN`),
   `reference_docs` 존재, `confidence` 범위(0~1) 확인.
3. **거리 측정 실패 시 확신도 하향**: 거리 미상(-1.0)과 비현실적 거리(300.0)가 정상
   거리보다 확신도가 낮게 나오는지, 근거에 "거리 측정 실패" 안내가 포함되는지.
4. **Mock 호환성**: `RagResponseAgent`와 `team_integration_adapter._MockResponseAgent`가
   같은 반환 타입/필드 구성을 갖는지 (오케스트레이터에 자리 교체가 가능한지 확인).
5. **UDP payload 경로 검증**: `ai_inference_sender.py`가 실제로 보내는 형태
   (`{"object": "person with no helmet", "bbox": [...], "distance_meter": 1.85}`)를
   `common.schema.build_detection_event()`로 변환한 뒤 대응안을 생성해봅니다. 이 블록은
   **`risk_level != "위험"`이면 콘솔에 "[확인 필요] 안전모 미착용이 '위험'으로 판정되지
   않았습니다"라는 경고를 출력하도록 이미 만들어져 있습니다** — 즉 이 테스트 자체가
   "알려진 이슈"(아래 참고)를 스스로 검출해서 알려주는 구조입니다.

```powershell
python self_test\test_response_agent.py
```

## test_schema_logic.py

`common/schema.py`의 `classify_risk()`, `build_detection_event()`,
`build_map_detection_event()`가 의도한 대로 동작하는지 확인합니다. ROS2/Jetson/
PostgreSQL/인터넷 전부 필요 없습니다.

- `classify_risk()`: `fire`/`hazardous leak`/`person with no helmet`은 거리 무관 항상
  "위험", `obstacle`은 항상 "주의", `human`/`vehicle`은 거리 구간별(1.5m 미만 위험 /
  1.5~3.0m 주의 / 3.0m 이상 정상 / 거리 미상 주의)로 분기, 정의 안 된 객체(`forklift`)는
  "주의"로 떨어지되 issue에 원문이 남는지 확인.
- **`fake_jetson_sender.py`의 기대 위험도 표**: 그 파일이 보내는 11개 패킷을 실제로
  `build_detection_event()`에 통과시켜, 표에 적힌 기대값과 일치하는지 확인합니다.
  현재 비전 라벨 3종이 전부 포함되어 있는지, 미분류 폴백 경로도 함께 보내는지도 검사합니다.
- `build_detection_event()`: NanoOWL UDP 경로 변환 — zone 반영, 한글 객체명 변환, 위험
  판정, `map_x`/`map_y`가 기본 `None`인지. `.get()` 사용 덕분에 필드가 누락된 payload도
  에러 없이 처리되는지(`distance == -1.0`으로 대체).
- `build_map_detection_event()`: SLAM 경로 변환 — `map_x`/`map_y`가 채워지는지,
  `box_position`이 `"map"`으로 표시되는지.

```powershell
python self_test\test_schema_logic.py
```

## test_response_throttle.py

`common/schema.py`의 `ResponsePlanThrottle`을 검증합니다. 실제로 30초를 기다리지 않도록
`time_func`에 가짜 시계(`FakeClock`)를 주입해서 시간을 임의로 앞당깁니다. 따라서 즉시
끝나고, DB/네트워크도 필요 없습니다.

- **억제 창 안/밖**: 첫 이벤트는 통과, 29.9초까지는 억제, 30초를 넘기면 다시 통과.
- **억제가 창을 밀지 않는지**: 1초 간격으로 60초 연속 감지해도 통과는 `t=0/30/60` 세 번뿐.
  (억제될 때마다 기준 시각을 갱신하면 계속 감지되는 물체는 영영 대응안이 안 나옵니다.)
- **키 분리**: `(구역, 객체)` 조합이 다르면 서로 독립적으로 억제됩니다.
- **위험도 상향 예외**: `주의 → 위험`은 억제 창을 무시하고 즉시 통과, 반대로 `위험 → 주의`
  하향은 통과시키지 않습니다.
- **억제 끄기**: `suppress_seconds=0`이면 모든 이벤트가 통과.
- `risk_rank()`가 `위험 > 주의 > 정상` 순서이고, 모르는 등급은 가장 낮게 취급하는지.

```powershell
python self_test\test_response_throttle.py
```

## test_dashboard_layout.py

`dashboard/smart_factory_dashboard_v3.py`의 **화면 구조**를 검증합니다. Streamlit,
pandas, psycopg2, PostgreSQL 중 아무것도 없어도 실행됩니다 — 가짜 `streamlit` 모듈을
`sys.modules`에 꽂아 넣고 대시보드를 그냥 `import`하면, 모듈 최상위 코드가 위에서
아래로 실행되면서 화면을 그립니다. 그 호출 순서와 인자를 기록해서 검사합니다.

가짜 커서는 실제로 나가는 SQL 문자열을 보고 알맞은 행을 돌려주므로(`GROUP BY grp` /
`DISTINCT ON (zone)` / `FROM response_plans` / `COUNT(*)`), 쿼리를 바꾸면 테스트도 같이
따라옵니다. 실행된 SQL 은 `QUERIES` 에 모아두고 내용까지 검사합니다.

- **배치 순서**: `경보 배너 → 구역 카드 → 상태 바 → 영상 → 미니맵 → 상세 조회`.
  경보가 영상보다 아래로 내려가면(v6의 문제) 실패합니다.
- **배너 3종의 구분**: 위험(`sfp-banner`, 빨강) / 로봇 미연결(`sfp-offline-banner`, 주황) /
  DB 응답 없음(`sfp-mute-banner`, 회색)이 서로 다른 태그로 나오는지. 셋을 같은 빨강으로
  칠하면 "무엇에 대응해야 하는지"가 사라집니다.
- **DB 장애 시나리오**: 가짜 커서가 `OperationalError`를 던졌을 때 트레이스백이나
  `st.stop()` 대신 "상태 불명" 카드가 뜨고, **초록(정상)으로도 미연결로도 보이지
  않는지**. 관제 화면에서 "정보 없음"이 "이상 없음"처럼 보이면 그게 사고입니다.
- **감지 요약 쿼리**: gaps-and-islands 로 구간을 접는지, `issue`로 묶지 않는지
  (거리가 문구에 박혀 있어 매 프레임 달라집니다), 구역별로 세는지, 거리 -1을
  최근접에서 빼는지, 시간과 행 수 두 가지로 자르는지.
- **경보 확인(v9)**: 확인 버튼이 `on_click` 콜백을 쓰는지, 누르면 배너가 사라지고
  `✅ 위험 확인됨`이 남는지, **구역 카드의 빨강은 그대로인지**(확인은 "봤다"이지
  "해결됐다"가 아님), 확인 이후의 새 위험은 다시 뜨는지.
  가짜 버튼은 Streamlit 과 같은 순서로 **콜백을 먼저** 실행하고, 그 뒤에 조각을
  다시 그려서 효과를 확인합니다.
- **소리 경보(v9)**: 기본값(꺼짐)에서는 안 울리는지, 켜면 새 위험에 **한 번만**
  울리는지, 같은 위험으로 매 초 다시 울리지 않는지, 코드에서 구운 WAV data URI 인지.
- **화면 구성(v9)**: '관제 집중'에서 상세 조회가 접히고 지도는 남는지,
  **미니맵이 여전히 `st.columns` 밖인지**(높이만 낮추고 폭은 절대 안 줄임).
- **크게 보기(v10)**: 영상이 `?expand=video` 링크로 감싸지는지(`target="_self"` 포함),
  **지도는 감싸지지 않는지**(iframe 클릭은 지도 자체 조작), 확대 시 반대쪽 패널과
  상세 조회가 접히는지, **그래도 경보와 구역 카드는 남는지**(확대가 경보를 가리면
  사고), 모르는 `expand` 값은 무시하는지, 버튼 경로로도 열고 닫히는지.
  확대 상태는 `SCENARIO["query"]`로 심습니다 — 가짜 `st.query_params`는 그냥 dict라
  대시보드가 쓰는 `get`/`[]=`/`in`/`del`이 전부 그대로 동작합니다.
- **대응안 패널 / 이전 기록(v11)**: 첫 건만 큰 카드이고 나머지는 한 줄 요약인지,
  **요약 줄에 권장 조치 문장이 안 들어가는지**(`class="a"`가 정확히 1개), 패널 조회가
  우선순위 정렬(`CASE risk_level ... ELSE -1`)을 쓰고 기록 조회는 순수 최신순인지,
  기록 화면이 자기 필터(`plan_zone`/`plan_span`)를 쓰는지, '전체 기록'이면 시간
  조건을 아예 안 거는지, `patrol_logs` 쪽에는 '전체' 선택지가 없는지.
  `LIMIT`이나 기간처럼 `%s`로 넘어가는 값은 쿼리 문자열에 안 남으므로 `PARAMS`
  기록장에서 확인합니다.
- **미니맵 폭**: `st.columns()` 컨텍스트 깊이를 세서, 미니맵 `iframe`이 **깊이 0**
  (본문 한 줄)에서 그려지는지 확인합니다. 2단 칸에 다시 넣으면 실패합니다 —
  `minimap_web.html`이 폭 900px 아래에서 좁은 화면용으로 접히기 때문입니다.
  영상은 반대로 깊이 1(칸 안)이어야 합니다.
- **HTML 이스케이프(XSS)**: DB에서 온 값 자리에 `<img src=x onerror="alert(1)">`와
  `</div><script>alert('plan')</script>`를 미끼로 넣고, 화면 HTML에 태그가 그대로
  남지 않는지(`&lt;img` 로 escape 되는지) 확인합니다.
- **`_zone_style()`**: 데이터 없음 / 미연결(10초 초과) / 위험 / 주의 / 정상 5가지.
  10초를 넘기면 위험도와 무관하게 미연결이 되는지도 확인합니다.
- **평온한 시나리오**: 위험 0건이면 경보 배너가 **뜨지 않고**, 대응안이 없으면 안내
  문구가 나오며, 그래도 구역 카드와 미니맵은 그대로 그려지는지.

```powershell
python self_test\test_dashboard_layout.py
```

## test_dashboard_tables.py

상세 조회 표 3종(감지 요약 / 원본 로그 / AI 대응안 이력)을 **진짜 pandas 로** 끝까지
그려봅니다. Styler 로 CSS 를 만드는 데까지 실제로 돌립니다.

**왜 따로 있나.** 바로 위 `test_dashboard_layout.py`는 pandas 를 빈 모듈로 스텁하고
로그 0건 경로만 탑니다(그래서 아무것도 설치하지 않고 돌아갑니다). 덕분에 배치와
이스케이프는 검증되지만, **표를 만드는 코드는 한 줄도 실행되지 않습니다.** 이 파일을
만들면서 그 사각지대에서 버그 두 개가 실제로 나왔습니다.

1. `age_seconds`가 NULL 인 대응안이 한 행이라도 섞이면 pandas 가 그 컬럼을 float 으로
   만들면서 `None`을 `NaN`으로 바꾸고, `_ago()`의 `int(float(nan))`이 `ValueError`를
   던져 **표 전체가 사라졌습니다.**
2. `confidence`가 NULL 이면 화면에 `nan%`가 찍혔습니다. `float(nan)`은 예외를 던지지
   않으므로 `try/except`로는 막히지 않습니다 — NaN 은 자기 자신과 같지 않다는 성질로
   걸러야 합니다.

둘 다 "NULL 이 섞인 실제 데이터"에서만 나오므로, 행 0건 테스트로는 영원히 안 잡힙니다.
그래서 이 테스트의 고정 데이터에는 NULL 이 일부러 섞여 있습니다.

`pandas`는 프로젝트 `requirements.txt`의 필수 의존성입니다. 없는 환경에서는
표 로직을 검증할 수 없으므로 이 테스트는 명시적으로 실패합니다. 먼저
`pip install -r requirements.txt`를 실행하거나 프로젝트 `.venv`를 활성화하세요.

- 표 3종 × (전체 구역 / 단일 구역) 6가지가 경고 없이 그려지는지.
- 전체 구역일 때만 `구역` 컬럼이 붙는지.
- 위험도에 🔴/🟡/🟢 가 붙는지, 거리 -1과 NULL 이 `측정불가`/`-`가 되는지,
  **`NaN`이 화면에 새어나오지 않는지.**
- `_ago()` / `_percent()` 경계값(None, NaN, 음수, 초/분/시간/일).
- `_full_width_kwargs()`가 Streamlit 1.49 이상이면 `width="stretch"`를,
  그 아래면 `use_container_width=True`를 고르는지.
- **CSV 내려받기(v9)**: `CSV_CALLS` 기록장에 가짜 `st.download_button`이 받은
  `(파일명, 바이트)`를 담아 직접 들여다봅니다. 파일을 만들지도 네트워크를 타지도
  않고, 리스트에 담아둘 뿐입니다.
  - **바이트가 `\xef\xbb\xbf`(BOM)로 시작하는지.** `utf-8-sig`가 아니면 한국어
    Windows 의 Excel 이 cp949 로 읽어서 한글이 전부 깨지는데, 예외가 나지 않아
    파일을 열어보기 전에는 모릅니다. 이 한 줄이 이 테스트의 핵심입니다.
  - 헤더가 원본 컬럼명(`created_at`)이 아니라 화면의 한글 컬럼명(`생성 시각`)인지.
  - 화면에서 가공한 값(`85%`, `🔴 위험`)이 그대로 들어가고 `nan`이 없는지.
  - 파일명에 표 종류·구역·시각이 들어가는지(`감지요약_B_20260825_143207.csv`).

```powershell
python self_test\test_dashboard_tables.py
```

## 다른 폴더와의 연동 지점

- `common/schema.py`: `test_schema_logic.py`(판정 로직)와 `test_response_throttle.py`
  (대응안 중복 억제)가 직접 import해서 검증.
- `pipeline/patrol_pipeline_rag.py`: `test_response_throttle.py`가 검증하는
  `ResponsePlanThrottle`을 실제로 사용하는 쪽.
- `integration/team_integration_adapter.py`, `integration/response_agent.py`:
  `test_response_agent.py`가 `ResponsePlan`, `_MockResponseAgent`, `RagResponseAgent`,
  `resolve_risk_type`, `MAX_ACTION_LEN`을 import.
- `../ROS2_자율주행_및_연동(이다은)/dashboard_link/minimap_renderer.py`:
  `test_minimap_logic.py`가 스텁 모듈을 등록한 뒤 `importlib`로 직접 로드.
- `dashboard/smart_factory_dashboard_v3.py`, `edge_video/dashboard_video_minimap_section.py`:
  `test_dashboard_layout.py`가 `streamlit`/`pandas`/`psycopg2` 스텁을 등록한 뒤 import.
  `test_dashboard_tables.py`는 그 스텁 환경을 재사용하되 `pandas`만 진짜로 바꿔 끼웁니다.
- `pipeline/jetson_patrol_pipeline.py`: `fake_jetson_sender.py`가 UDP:9998로 실제 패킷을
  보내 수동 통합 테스트.
- `edge_video/event_logger.py`: `fake_minimap_server.py`가 `/detections` 응답을 대신 제공.

## 해결된 이슈 — 테스트가 구버전 라벨만 쓰던 문제 [적용 완료]

`fake_jetson_sender.py`와 `test_schema_logic.py`가 구버전 라벨(`human`/`obstacle`/
`hazardous leak`)만 기준으로 작성되어 있었습니다. 실제 `ai_inference_sender.py`가 보내는
라벨은 `fire`/`person with no helmet`/`vehicle`입니다. **즉 두 스크립트가 전부 통과해도
"현재 실제 파이프라인이 올바르게 동작한다"는 것을 검증하지 못했습니다** — 구버전 라벨
기준으로 `classify_risk()`의 분기 로직이 맞는지만 확인된 겁니다.

이게 실제로 사고로 이어졌습니다. 안전모 미착용이 "미분류 → 주의"로 떨어지고 있었고,
파이프라인은 `risk_level == "위험"`일 때만 대응안을 만들기 때문에 **그 상황에 대응안이
아예 생성되지 않았습니다.** 테스트는 계속 초록불이었습니다.

지금은 이렇게 고쳐졌습니다.

- `fake_jetson_sender.py`가 현재 라벨 3종을 주로 보내고, 구버전·미등록 라벨은 폴백
  확인용으로 명시적으로 남겨 두었습니다.
- `test_schema_logic.py`에 "현재 비전 모듈의 탐지 클래스 3종" 블록이 있습니다.
- `test_schema_logic.py`가 **`fake_jetson_sender.py`의 기대 위험도 표까지 검증**합니다.
  수동 테스트의 기준표가 판정 정책과 어긋난 채 남아 있으면, 맞게 동작하는데도
  "틀렸다"고 읽거나 그 반대가 되기 때문입니다.
- `fake_minimap_server.py`도 같은 이유로 라벨을 맞췄습니다(예전엔 `no safety helmet`/
  `person`이라 안전모 미착용이 화면에 '주의'로 보였습니다).

경위는 [`../common/README.md`](../common/README.md)와
[`../docs/판정정책_갱신제안.md`](../docs/판정정책_갱신제안.md) 참고.

## 알려진 이슈

특별히 알려진 이슈는 없습니다.
