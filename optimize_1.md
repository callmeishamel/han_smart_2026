# optimize_1 — 통합 및 최적화 작업 정리

**기준점**: `origin/main` (`7cd0b65`, "ai_jetson 추가") — 팀원 3인의 코드가 각자
업로드되어 처음 한 저장소에 모인 상태
**작성일**: 2026-08-22
**브랜치**: `Optimizing_1`

이 문서는 팀원별로 따로 올라온 코드를 GitHub에서 받아온 시점부터, 서로 연동되고
실행 가능한 하나의 시스템으로 정리하기까지 수행한 작업을 정리한 것입니다.

---

## 1. 현재 git 상태

| 구분 | 내용 |
|---|---|
| `origin/main` | `7cd0b65` — 아직 아래 작업이 반영되지 않은 상태 |
| 커밋 완료 (미푸시) | `0abbca3` 최적화 → `829a818` optimize → `afdb65a` Optimize_1 |
| 미커밋 (작업트리) | 14개 파일 수정 (+534 / -205) |
| 미추적 (신규) | README 15개 |

즉 **`origin/main` 대비 3개 커밋 + 작업트리 변경분**이 이번 작업의 전체 범위입니다.

---

## 2. 작업 개요

가장 큰 문제는 **각 팀원의 코드가 개별적으로는 동작하지만 서로 연결되어 있지
않다**는 것이었습니다. 특히 두 개의 연결 고리가 코드상 아예 비어 있었습니다.

```
[손준영: 젯슨 비전 추론]
        │
        ├── UDP :9999 ──▶ [ROS2 브릿지] ──▶ /safety_status ──▶ ❌ 구독자 없음
        ├── UDP :9998 ──▶ [DB 파이프라인]  ✅ 연결됨
        └── UDP :9091 ──▶ ❌ 보내는 코드 자체가 없음 ──▶ [이다은: 미니맵]
```

- 미니맵(`minimap_renderer.py`)은 `:9091`로 각도 데이터가 오기를 기다리고 있었지만,
  **젯슨 쪽에 그걸 보내는 코드가 없었습니다.** → 미니맵에 탐지 점이 하나도 안 찍힘
- `/safety_status` 토픽은 발행되고 있었지만, **저장소 전체에 구독자가 없었습니다.**
  → 젯슨의 실제 탐지 결과가 ROS2 쪽에서 전혀 활용되지 않음

이 두 구멍을 메우고, 이어서 저장소 전반의 버그·구조·문서를 정리했습니다.

---

## 3. 기능 통합 — 끊어져 있던 연결 배선

### 3.1 미니맵 각도 데이터 송신 추가

**파일**: `젯슨 연결용 프로그램(손준영)/ai_inference_sender.py`

탐지된 객체마다 bbox 중심과 화면 중심의 픽셀 차이를 `focal_length`로 나눠
라디안 각도로 환산하고, 이다은님 노트북의 `:9091`로 전송하도록 추가했습니다.

```python
box_center_x = (xmin + xmax) / 2.0
angle_offset = math.atan2(box_center_x - frame_center_x, focal_length)
# {"label": ..., "angle_offset": ...} 형식으로 DAEUN_LAPTOP_IP:9091 전송
```

depth 값이 아니라 **각도만** 보내는 이유는, 스테레오 depth가 아직 불안정해서
미니맵 쪽에서 SLAM 지도에 레이캐스팅으로 거리를 역산하는 방식을 쓰기 때문입니다.
`minimap_renderer.py`가 이미 기대하고 있던 형식에 맞췄습니다.

### 3.2 `/safety_status` 구독자 구현

**파일**: `ROS2_자율주행_및_연동(이다은)/smart_factory_sim/event_detector.py`

`safety_status_callback()`을 새로 추가했습니다. 젯슨에서 온 JSON을 파싱해서
카메라 intrinsics로 픽셀+거리를 3D 좌표로 변환하고, TF(`map → jetson_camera_frame`)로
지도 좌표에 투영한 뒤, 기존 이벤트 처리 경로에 태워 `/event_markers`(RViz)와
`/detected_events`(웹 대시보드)로 발행합니다.

기존 Gazebo mock / 로컬 OWL-ViT 경로와 공통 로직을 `_register_event()`(중복 병합),
`_publish_markers()`(마커 발행)로 분리해 재사용했습니다.

추가된 ROS2 파라미터 — **`ai_inference_sender.py`의 실제 카메라 설정과 반드시
일치해야 합니다**:

| 파라미터 | 기본값 |
|---|---|
| `jetson_camera_frame` | `camera_rgb_optical_frame` |
| `jetson_focal_length` | `500.0` |
| `jetson_frame_width` | `1280.0` |
| `jetson_frame_height` | `720.0` |

---

## 4. 버그 수정

저장소 전체를 점검해 **19건의 문제를 식별**했고, 그중 판단 여지 없이 기계적으로
고칠 수 있는 **9건을 수정**했습니다. 나머지 10건은 설계 판단이 필요해 문서화만
하고 보류했습니다(→ 7장).

| # | 파일 | 문제 | 조치 |
|---|---|---|---|
| 1 | `dashboard_link/run_minimap.sh` | 존재하지 않는 경로(`edge_video/minimap_renderer.py`) 실행 → 즉시 실패 | 같은 폴더의 실제 파일로 경로 수정 |
| 2 | `launch/simulation.launch.py` | 로봇 스폰 좌표 `(0,0)`와 AMCL 초기 위치 `(0.5,3.5)` 불일치 → 로컬라이제이션 어긋남 | 스폰 좌표를 `(0.5,3.5)`로 통일 |
| 4 | `dashboard_link/minimap_renderer.py` | `/detections` 엔드포인트 없음 → `event_logger.py`가 영원히 404, DB 미적재 | `/detections`, `/health` 추가 |
| 5 | `edge_video/minimap_renderer.py` | 레이캐스팅이 `dist=0`(로봇 자기 위치)부터 검사 → 자기 자신을 장애물로 오인 | `range(1, steps)`로 수정 |
| 7 | 두 `minimap_renderer.py` | `DETECTION_MIN_HITS` 기본값이 `1` vs `3`으로 불일치 | `3`으로 통일 |
| 12 | `setup.py` | `smart_factory_sim/templates/` 미패키징 → colcon install 후 웹 대시보드 500 에러 | 패키징 목록에 추가 |
| 14 | `initial_pose_pub.py` | 로그가 `[0.0, 0.0, 0.0]`인데 실제 발행값은 `(0.5, 3.5)` | 실제 값을 동적으로 출력 |
| 15 | `maps/generate_map.py` | 작성자 개인 절대경로 하드코딩 → 다른 PC에서 실행 불가 | 스크립트 자기 위치 기준 상대경로 |
| 18 | `smart_factory_sim/auto_mapper.py` | LIDAR 360샘플 가정 하드코딩 → 다른 드라이버에서 ±30도가 아니게 됨 | `angle_increment`로 동적 계산 |

이전 라운드(`829a818`)에서 이미 수정된 것:

- **depth 마스킹 버그** — 시차를 못 구한 픽셀을 `0.1`로 대체해 300m 근방의 가짜
  거리값이 median 계산을 오염시키던 문제를 `-1.0` 마스킹으로 변경
- 미사용 `torch` import 제거, UDP 전송 중복 블록을 루프로 통합

---

## 5. 성능 최적화 — 젯슨 스크립트 2종

`ai_inference_sender.py`와 `vision_inference_node.py`를 집중 점검했습니다.
두 파일 사이에 **로직 중복은 없었지만**(한쪽은 추론+송신, 다른 쪽은 수신+재발행),
같은 데이터를 다루는 쌍인데도 규약이 서로 달랐고 내부에 중복 연산이 있었습니다.

### 5.1 `ai_inference_sender.py`

| 항목 | 내용 |
|---|---|
| **불필요한 메모리 복사 3개 제거** | 프레임당 2.7MB × 3회 → 30fps 기준 **초당 약 165MB**의 헛된 복사 제거. `frame_left.copy()` 하나만 유지(슬라이스 뷰라 필수) |
| **JPEG 인코딩을 락 밖으로 이동** | 락 안에서 `imencode`(수~수십 ms)를 돌려 추론 루프의 프레임 갱신을 블로킹하던 문제. 이제 락 안에서는 참조만 꺼냄 |
| 대기 화면 캐싱 | 매 프레임 `np.zeros` + `putText` + `imencode` 재생성 → 최초 1회만 |
| `frame_center_x` 호이스팅 | 탐지 루프 안에서 매번 재계산하던 것을 루프 밖으로 |
| 카메라 검사 순서 수정 | `cap.set()` 4회 후에 `isOpened()`를 확인하던 순서를 뒤집음 |
| **UDP 전송 실패 집계** | `except Exception: pass`로 조용히 삼키던 것을 2초 주기 로그에 실패 건수·목적지와 함께 출력 |
| `label_idx` 범위 검사 | IndexError로 프로세스 전체가 죽는 것 방지 |

> **스레드 안전성**: publish된 배열은 이후 변경되지 않고(그리기는 publish 이전에
> 완료) 다음 반복은 항상 새 배열을 만들므로, 락 밖 인코딩에서 tearing이 발생하지
> 않습니다.

### 5.2 `vision_inference_node.py`

| 항목 | 내용 |
|---|---|
| **수신 큐 드레인 루프** | 콜백당 1건만 읽어 executor가 밀리면 커널 UDP 버퍼가 넘치던 문제 → 큐가 빌 때까지(최대 50건) 처리 |
| **패킷 검증 추가** | UTF-8 디코딩 실패가 콜백에서 예외로 터져 **노드가 죽던** 문제. 이제 디코딩+JSON 검증 후 발행, 깨진 패킷은 폐기 후 카운트 |
| **로그 throttle** | 매 패킷마다 JSON 전문을 출력해 콜백을 블로킹하던 것을 2초 요약으로 변경 |
| 소켓 정리 | `close()` 추가, 바인딩 실패 시 `lsof` 안내가 포함된 명확한 에러 메시지 |

### 5.3 포트 설정 일원화

포트 `9999`가 송신·수신 양쪽에 **각각 하드코딩**되어 있어, 한쪽만 바꾸면 에러 없이
조용히 데이터가 끊기는 구조였습니다. 양쪽이 같은 환경변수를 읽도록 통일했습니다.

| 변수 | 기본값 | 읽는 곳 |
|---|---|---|
| `ROS2_UDP_PORT` | `9999` | **sender + node (양쪽)** |
| `ROS2_BRIDGE_BIND_IP` | `127.0.0.1` | node |
| `DASHBOARD_UDP_PORT` | `9998` | sender |
| `DETECTION_UDP_PORT` | `9091` | sender + minimap_renderer |
| `JETSON_VIDEO_PORT` | `8500` | sender |

`vision_inference_node.py`는 ROS2 파라미터로도 덮어쓸 수 있습니다:

```bash
python3 vision_inference_node.py --ros-args -p udp_port:=9999 -p bind_ip:=127.0.0.1
```

### 5.4 검증

로컬에 `cv2`(젯슨 전용)가 없어 sender는 직접 실행이 불가능하므로 나눠서 검증했습니다.

- **브릿지 노드 로직 11개 항목 통과** — `rclpy`를 스텁으로 대체해 실제 코드를 로드.
  깨진 UTF-8·잘못된 JSON에서 크래시 없이 폐기되는지, 밀린 12개 패킷을 한 콜백에서
  모두 처리하는지, 상한 초과분이 유실되지 않고 큐에 남는지 확인
- **UDP 분배 로직 14개 항목 통과** — 목적지별 패킷 수, 페이로드 형식 구분,
  "다은님 노트북이 죽은 시나리오"에서 미니맵 전송만 실패로 집계되고 나머지 경로는
  정상 동작하는지 확인
- 스레드 안전성은 프레임 생명주기 정적 추적으로 검증

> **미검증**: 실제 젯슨에서의 FPS 개선 폭과 카메라 파이프라인 동작은 하드웨어가
> 있어야 확인 가능합니다.

---

## 6. 저장소 구조 및 문서 정리

### 6.1 파일 이동

손준영 파트 코드가 저장소 루트에 흩어져 있던 것을 폴더로 통합했습니다
(`git mv`로 히스토리 보존).

```
ai_inference_sender.py     ─┐
vision_inference_node.py   ─┼─▶ 젯슨 연결용 프로그램(손준영)/
requirements-vision.txt    ─┘
```

이전 라운드(`0abbca3`)에서는 젯슨 배포 폴더 안의 `common/`, `pipeline/`이 이상민
폴더 원본과 완전히 동일한 중복이라 제거했고(456줄), 실수로 커밋된 zip 파일도
정리했습니다.

### 6.2 문서

**신규 15개 + 갱신 2개**의 README를 작성했습니다.

| 위치 | 개수 |
|---|---|
| `ROS2_자율주행_및_연동(이다은)/` | 5 (최상위, smart_factory_sim, dashboard_link, launch, maps) |
| `데이터 플랫폼 및 대시보드(이상민)/` | 8 (common, pipeline, integration, rag, dashboard, edge_video, admin, self_test) |
| `젯슨 연결용 프로그램(손준영)/` | 2 (최상위, smart_factory_project) |
| 갱신 | 루트 `README.md`, 이상민 폴더 최상위 `README.md` |

문서 작성 과정에서 발견해 바로잡은 내용 불일치:

- 이상민 폴더 README가 "이다은/정진철 코드 미도착"이라고 적어놓고 하단 상태표에는
  "✅ 완료"로 표시하던 **자기 모순** → 실제 상태로 통일
- 미니맵 다이어그램이 실행 파일을 `edge_video/minimap_renderer.py`로 잘못 지목
  (실제 실행되는 건 `dashboard_link/` 쪽이고, `edge_video/`는 rclpy 없이 로직만
  테스트하기 위한 사본) → 정정하고 사본임을 명시
- 탐지 라벨 목록이 구버전(`human`/`obstacle`/`hazardous leak`)으로 적혀 있던 것
  → 실제 라벨(`fire`/`person with no helmet`/`vehicle`)로 수정
- "UDP 두 포트 전송" → 실제 세 포트로 수정
- `team_integration_adapter.py`의 "정진철 코드 미도착" 주석 → 이미 연동 완료 상태 반영

### 6.3 문서 작업 중 새로 발견한 불일치 (미해결, 기록만)

- `worlds/smart_factory.world`는 20×20m인데, 커밋된 `maps/factory_map.pgm`은
  100×106px(약 5.0×5.3m, `origin: [-0.659, -1.41, 0]`, `mode: trinary`)로
  **`generate_map.py`가 만드는 400×400px(20×20m, `origin: [-10,-10,0]`)와 다릅니다.**
  실제 SLAM 세션에서 `map_saver_cli`로 저장한 파일로 보입니다. `web_dashboard.py`의
  기본 origin(`[-6.0,-3.0,0.0]`)까지 셋 다 제각각입니다.
- `patrol_node.py`는 웨이포인트 7개(6개 지점 + 복귀 1개)를 순환하며, **어떤 launch
  파일에도 연결되어 있지 않아**(`simulation.launch.py`에 주석 처리) `ros2 run`으로
  수동 실행해야 합니다.

---

## 7. 미해결 이슈 (설계 판단 필요)

19건 중 수정하지 않고 보류한 10건입니다. 코드를 고치려면 팀원 간 합의나 설계
결정이 필요합니다.

> **이후 처리 현황** (이 표는 작성 시점의 기록이며, 아래 항목은 그 뒤에 해결됐습니다)
>
> | # | 상태 | 어디서 |
> |---|---|---|
> | 3 | **해결** — 실패는 `KB_RECHECK_SECONDS`(30초) 뒤 재확인, 성공만 영구 캐시 | `integration/README.md` "해결된 이슈" |
> | 6 | **해결** — 영상 옆 최신 대응안 + 하단 이력 탭 추가 | `dashboard/README.md` "화면 구조(v7)" |
> | 8 | **해결** — `DISTANCE_BASED_OBJECTS`/`ALWAYS_DANGER_OBJECTS` 도입 | `common/README.md`, `docs/판정정책_갱신제안.md` |
> | 17 | **해결** — SVG data URI로 교체(외부 요청 없음) | `edge_video/dashboard_video_minimap_section.py` |
> | 19 | **해결** — `GoalStatus` 판정 + 재시도 후 건너뛰기 | `smart_factory_sim/patrol_node.py` |
> | 10 | **해결** — `--use-llm` 으로 `response_agent.py` 가 호출. 기본 꺼짐, 서버 없으면 규칙 기반 폴백 | `integration/README.md`, `rag/README.md` |
> | 11 | **해결** — INSERT SQL 3개를 `common/schema.py` 로 통합. `patrol_logs.map_x/map_y` 가 계속 NULL 이던 실제 데이터 손실도 함께 수정 | `common/README.md` |
> | 16 | **해결** — 미니맵이 `session_id` 를 함께 전송, `(session_id, tracker_id)` 유니크 + `ON CONFLICT DO NOTHING` | `edge_video/README.md` |
> | — | **해결** — `DigitalTwinAdapter`/`ZoneLayout`/`get_zone_context()` 삭제. 실제 경로(`/detections`)를 정본으로 정리 | `integration/README.md` |
> | 9 | **해결** — `fake_jetson_sender.py`가 현재 라벨 3종을 보내고, 구버전·미등록 라벨은 폴백 확인용으로 유지. 기대 위험도 표 자체를 `test_schema_logic.py`가 검증 | `self_test/README.md` |
>
> **19건 전부 처리했습니다.**
>
> 이 감사 이후에 새로 발견해 고친 것도 있습니다 (여기 19건에는 없던 항목입니다).
>
> | 내용 | 어디서 |
> |---|---|
> | 젯슨이 9999(젯슨 호스트)와 9998(이상민 PC)을 `HOST_IP` 하나로 보내 둘 중 하나는 반드시 못 받던 문제 | `ROS2_BRIDGE_IP` / `DASHBOARD_IP` 로 분리 |
> | 파이프라인이 `127.0.0.1`에만 바인딩해 젯슨 패킷을 구조적으로 못 받던 문제 | `PIPELINE_UDP_BIND` (기본 `0.0.0.0`) |
> | `test_minimap_logic.py`가 실행되지 않는 사본을 검증하던 문제 | 이다은님 폴더의 원본을 검증하도록 변경 |
> | 미니맵이 대시보드 2단 칸 안에서 항상 좁은 화면용 레이아웃으로 접히던 문제 | `render_minimap()` 분리, 본문 전체 폭 |
> | `patrol_logs.map_x/map_y` 가 계속 NULL 이던 문제 (INSERT 두 곳 모두 컬럼 누락) | INSERT SQL 을 `common/schema.py` 로 통합 |
> | `fake_minimap_server.py` 가 구버전 라벨을 써서 안전모 미착용이 '주의'로 보이던 문제 | 실제 젯슨 라벨로 교정 |
>
> 앞의 두 개는 `self_test/test_integration_wiring.py`가 자동으로 검사합니다.

| # | 위치 | 내용 |
|---|---|---|
| 3 | `integration/response_agent.py` | `_kb_available()`이 DB 연결 실패를 **영구 캐싱** → 이후 DB가 정상화돼도 `builtin`(확신도 0.4) 폴백으로만 동작. 대시보드 v3가 "v2에서 고쳤다"고 주석 단 버그가 같은 폴더 다른 파일에서 재발 |
| 6 | `dashboard/smart_factory_dashboard_v3.py` | `response_plans` 테이블을 조회/표시하는 코드가 없음 → RAG 대응안이 DB에만 쌓이고 화면에 안 보임 |
| 8 | `common/schema.py` | `classify_risk()`가 `fire`/`hazardous leak`/`obstacle`/`human`만 처리 → **안전모 미착용·차량 근접이 "위험"으로 분류되지 않음**. 3대 시나리오 중 화재만 실제 동작 |
| 9 | `self_test/` | `fake_jetson_sender.py`·`test_schema_logic.py`가 구버전 라벨을 사용 → 통과해도 실제 통합 경로를 검증하지 못함 |
| 10 | `rag/vllm_client.py` | 완성된 LLM 클라이언트인데 파이프라인 어디서도 호출되지 않는 죽은 코드 |
| 11 | 두 파일 | `patrol_logs` INSERT 로직이 `jetson_patrol_pipeline.py`와 `team_integration_adapter.py`에 독립 중복 구현 |
| 16 | `edge_video/event_logger.py` | 중복 방지가 메모리 캐시뿐 → 재시작 시 유효한 tracker_id를 전부 재적재 |
| 17 | `dashboard_video_minimap_section.py` | 폴백 이미지가 외부 URL(`via.placeholder.com`) 의존 → 폐쇄망에서 깨짐 |
| 19 | `smart_factory_sim/patrol_node.py` | Nav2 목표 실패(ABORTED/CANCELED)도 무시하고 다음 웨이포인트로 진행 |
| — | `team_integration_adapter.py` | `DigitalTwinAdapter`가 TODO 상태. 이다은 파트는 이 어댑터가 아닌 별도 경로로 연동되어 있어 두 경로가 공존 |

**8번이 가장 영향이 큽니다.** `response_agent.py`의 `RISK_TYPE_MAP`은 이미
`person with no helmet` → `PPE`, `vehicle` → `machinery` 매핑을 갖고 있어 대응안
생성 쪽은 준비가 끝나 있는데, 그 앞단 `classify_risk()`가 "위험"을 반환하지 않아
파이프라인 게이트를 통과하지 못하는 "죽은 준비 코드" 상태입니다.

---

## 8. 공개 전 보안 점검 결과

저장소를 public으로 전환할 경우를 가정해 점검했습니다.

### 문제 없음 ✅

- **비밀번호·API 키·인증서**: 코드와 git 히스토리 전체 diff를 검색했으나 실제
  값이 커밋된 적 없음. 전부 환경변수 참조이거나 `여기에_실제_비밀번호_입력` 같은
  플레이스홀더
- **`.gitignore` 커버리지**: `secrets.toml`, `set_env.sh`, `set_env.ps1`, `.env`가
  중첩 폴더까지 실제로 무시되는지 `git check-ignore`로 검증 완료
- **IP 주소**: 전부 사설/루프백 대역(`127.0.0.1`, `0.0.0.0`, `192.168.0.x`)
- **개인 절대경로**: 15번 수정으로 현재 코드에서 제거됨

### 조치 권장 ⚠️

- **git 히스토리에 무관한 개인 파일 2건이 남아 있음** — 삭제 커밋이 있어도
  히스토리에서는 다운로드 가능합니다.
  - `20260729_172632.jpg` (1.9MB, 개인 반려동물 사진 — EXIF에 GPS는 없음)
  - `korailroutemap_260101.jpg` (1.9MB, 제3자 저작물 가능성)
- **커밋 작성자 실명 이메일 5종이 영구 공개됨** — public 전환 시 크롤링 대상.
  팀원 동의 필요. GitHub의 이메일 비공개 설정은 *앞으로의* 커밋에만 적용되고 이미
  쌓인 커밋은 히스토리 재작성 없이는 바뀌지 않습니다.

### 참고 (공개와 무관한 운영 이슈)

MJPEG·미니맵·웹 대시보드 서버가 인증 없이 `0.0.0.0`에 바인딩됩니다(8500/8091/8080).
같은 LAN이면 누구나 카메라 영상과 공장 지도를 볼 수 있습니다. 시연 환경에서는
문제없으나 공유 와이파이나 실제 현장 배포 시에는 방화벽이나 인증이 필요합니다.

---

## 9. 최종 데이터 흐름

```
┌────────────── Jetson / Docker (손준영) ──────────────┐
│  ai_inference_sender.py                              │
│   NanoOWL(VLM) + StereoSGBM(Depth)                   │
│   fire / person with no helmet / vehicle 탐지        │
└──┬──────────────┬──────────────┬─────────────────────┘
   │ UDP :9999    │ UDP :9998    │ UDP :9091
   │ (ROS2)       │ (대시보드/DB)│ (미니맵 각도)
   ▼              ▼              ▼
┌──────────────┐ ┌────────────┐ ┌──────────────────────┐
│vision_       │ │jetson_     │ │dashboard_link/       │
│inference_    │ │patrol_     │ │minimap_renderer.py   │
│node.py       │ │pipeline.py │ │(이다은 노트북 :8091) │
│→/safety_     │ │또는 _rag   │ │ SLAM 지도 레이캐스팅 │
│  status      │ │            │ │                      │
└──────┬───────┘ └─────┬──────┘ └──────────┬───────────┘
       ▼               ▼ INSERT            │ /detections
┌──────────────┐ ┌────────────┐            ▼
│event_        │ │PostgreSQL  │◀── event_logger.py
│detector.py   │ │            │
│→/event_      │ │            │
│  markers     │ └─────┬──────┘
│→/detected_   │       │ 조회
│  events      │       ▼
└──────────────┘ ┌──────────────────────────────────────┐
                 │smart_factory_dashboard_v3.py         │
                 │(Streamlit 관제 대시보드)             │
                 └──────────────────────────────────────┘
```

MJPEG 영상은 `:8500`(`/`=RGB, `/depth`), 미니맵은 `:8091/minimap_feed`로 제공되며
대시보드가 둘 다 표시합니다.

---

## 10. 남은 작업

1. **커밋 및 푸시** — 작업트리 변경분(14개 수정 + 15개 신규)이 아직 미커밋 상태
2. **실기 검증** — 젯슨 하드웨어에서 FPS 개선 폭, 미니맵 탐지 점 표시, `/safety_status`
   → RViz 마커 경로 확인
3. **파라미터 정합성 확인** — `event_detector.py`의 `jetson_focal_length`(500.0),
   `jetson_frame_width/height`(1280/720)가 실제 카메라 스펙과 맞는지.
   `jetson_camera_frame`에 해당하는 TF가 실제로 발행되는지도 확인 필요
   (없으면 `safety_status_callback`이 동작하지 않음)
4. **`DAEUN_LAPTOP_IP` 설정** — 기본값 `203.0.113.20`이 실제 노트북 IP와 다르면
   미니맵에 점이 안 찍힘. 2초 주기 로그의 "전송 실패" 경고로 확인 가능
5. **미해결 이슈 10건 논의** — 특히 8번(위험도 분류)이 시연 시나리오에 직결
