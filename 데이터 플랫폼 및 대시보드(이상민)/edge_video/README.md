# edge_video — 영상 스트리밍 + SLAM 미니맵 연동

프로젝트 전체 배경과 실행 순서는 최상위 [`../README.md`](../README.md)를 참고하세요.
이 폴더는 "젯슨 영상 + 이다은님 노트북 SLAM 미니맵"을 대시보드에 붙이고, SLAM 기반
탐지를 DB에 기록하는 파일들을 담고 있습니다. `patrol_logs`(손준영 파트 UDP 탐지)와는
별도 흐름으로, `detection_events` 테이블에 기록됩니다.

## 파일 구성

| 파일 | 실행 위치 | 설명 |
|---|---|---|
| `event_logger.py` | 이상민님 PC | `/detections`를 폴링해서 `detection_events` 테이블에 적재 |
| `dashboard_video_minimap_section.py` | 이상민님 PC (Streamlit 프로세스) | 대시보드에 영상 `<img>` + 미니맵 `iframe` 삽입 |
| `camera_stream_server.py` | 젯슨 (참고용, 실제로는 안 씀) | `ai_inference_sender.py`와 별개인 순수 영상 전용 MJPEG 서버 |

## 미니맵 렌더러는 이 폴더에 없습니다

미니맵 서버는 이다은님 노트북에서 ROS2 환경으로 도는
[`../../ROS2_자율주행_및_연동(이다은)/dashboard_link/minimap_renderer.py`](../../ROS2_자율주행_및_연동(이다은)/dashboard_link/minimap_renderer.py)
하나뿐입니다. 이 폴더의 두 파일(`event_logger.py`, `dashboard_video_minimap_section.py`)은
**그 서버의 클라이언트**입니다.

> ### 예전에 있던 사본은 삭제했습니다
>
> `rclpy` 없이 로직을 읽어보려고 이 폴더에 `minimap_renderer.py` 사본을 두었는데,
> 원본이 앞서 나가면서 갈라졌습니다.
>
> | | 원본 | 삭제된 사본 |
> |---|---|---|
> | 로봇 위치 | tf2 `map → base_footprint` 조회 | `/amcl_pose` 구독 |
> | `SpatialObjectTracker` | `list` + `next_id` | `dict` + `itertools.count` |
> | 엔드포인트 | `/`, `/map_data`, `/state`, `/detections`, `/minimap_feed`, `/health` | `/detections`, `/minimap_feed`, `/health` |
> | 세션 식별자 | `SESSION_ID` 있음 | 없음 |
> | 접근 제어 | `MINIMAP_TOKEN`, `MINIMAP_BIND`, CSP 헤더 | 없음 |
>
> `self_test/test_minimap_logic.py`가 원본을 검증하도록 바뀐 뒤로 **사본은 어떤
> 테스트도 거치지 않는 상태**였고, 그걸 고쳐도 아무 데도 반영되지 않았습니다.
> 그래서 지웠습니다. 로직을 확인하거나 바꿀 때는 원본을 보세요 —
> `rclpy`가 없어도 `python self_test\test_minimap_logic.py`가 스텁으로 원본을
> 불러 검증합니다.

### 미니맵 서버 환경변수

`MINIMAP_PORT`(기본 8091), `DETECTION_UDP_PORT`(기본 9091), `CAMERA_YAW_OFFSET`(기본 0.0,
카메라가 로봇 정면과 다른 방향이면 라디안 오프셋), `DETECTION_MAX_RANGE`(기본 8.0m),
`DETECTION_DIST_THRESHOLD`(기본 0.35m), `DETECTION_MIN_HITS`(기본 3),
`DETECTION_EMA_ALPHA`(기본 0.25), `DETECTION_STALE_SEC`(기본 30.0),
`MINIMAP_MJPEG_FPS`(기본 5).

접근 제어용으로 `MINIMAP_BIND`(기본 `0.0.0.0`), `MINIMAP_TOKEN`(기본 없음),
`MINIMAP_FRAME_ANCESTORS`(기본 `*`)가 있습니다.
**토큰을 켜면 이 폴더의 두 클라이언트 모두**(`dashboard_video_minimap_section.py`,
`event_logger.py`)에 같은 `MINIMAP_TOKEN`을 넣어야 합니다. 한쪽만 넣으면 그쪽이
403을 받고 조용히 멈춥니다. `self_test/test_integration_wiring.py`가 이 짝을 검사합니다.

## event_logger.py

실행 위치: **이상민님 PC**(PostgreSQL이 설치된 컴퓨터). 다은님 노트북의 `/detections`
엔드포인트를 주기적으로 폴링해서, `min_hits`를 통과해 "확정"된 탐지 중 아직 DB에 기록하지
않은 새 `tracker_id`만 골라 `detection_events` 테이블에 INSERT합니다.

- **`ensure_table(conn)`**: `common.schema.DETECTION_EVENTS_TABLE_DDL`을 그대로 실행 — DDL을
  이 파일에서 따로 정의하지 않는 이유는 다른 컴포넌트가 만든 스키마와 어긋날 위험을 막기
  위함입니다.
- **`log_new_event(conn, obj)`**: `tracker_id`, `label`, `map_x`, `map_y`, `hit_count`,
  `detected_at`(`to_timestamp()`) 1건 INSERT.
- **`reconnect(old_conn)`**: DB 연결이 끊기면 지수 백오프(최대 30초)로 다시 연결합니다.
  이 스크립트는 며칠씩 도는 상주 프로세스라, DB 재시작이나 네트워크 순단으로 죽으면
  그동안의 탐지 기록이 통째로 비게 됩니다.
- **`main()`**: `POLL_INTERVAL_SEC`(기본 2.0초)마다 폴링합니다. 메모리 집합 `seen`은
  **중복 방지 장치가 아니라 DB 왕복을 아끼는 캐시**입니다 — 실제 중복 차단은
  `(session_id, tracker_id)` 유니크 인덱스와 `ON CONFLICT DO NOTHING`이 합니다.

**고친 것**

| 문제 | 내용 |
|---|---|
| DB 예외로 프로세스 사망 | `try/except`가 HTTP 요청만 감싸고 있어서, `log_new_event()`의 예외가 `main()` 밖으로 빠져나가 프로세스가 그대로 죽었습니다. 이제 적재 실패는 `reconnect()` 후 다음 주기에 이어갑니다 |
| 트랜잭션 abort 상태 잔존 | INSERT 실패 시 `rollback()`을 안 해서 커넥션이 "aborted transaction"으로 남으면 이후 모든 쿼리가 거부됐습니다 |
| 캐시 무한 증가 | 상한(`MAX_SEEN_IDS`, 기본 10000)을 두고 넘으면 오래된 절반을 버립니다 |
| 탐지 항목 형식 오류 | `KeyError`가 나면 그 항목만 건너뛰고 나머지는 계속 처리합니다 |
| **재시작 시 중복/누락** | 아래 참고 — `session_id` 도입으로 해결 |

### 중복 방지가 메모리 캐시뿐이던 문제 [적용 완료]

`seen_tracker_ids`가 유일한 방어선이던 시절, 세 상황에서 조용히 틀렸습니다.

| 상황 | 예전 결과 |
|---|---|
| 이 로거를 재시작 | 캐시가 비어서, 미니맵이 아직 들고 있는 물체를 전부 다시 적재 (중복 행) |
| **미니맵을 재시작** | `tracker_id`가 1부터 다시 시작하는데 캐시에 옛 1번이 남아 있어 **새로 잡힌 물체를 통째로 버림 (기록 누락)** |
| `MAX_SEEN_IDS` 초과 | 오래된 절반을 버려서, 아직 살아 있는 물체를 다시 적재 (중복 행) |

가운데 항목이 가장 위험합니다 — 중복은 보기 싫을 뿐이지만, 저건 안전 기록이 사라지는
것입니다.

`minimap_renderer.py`가 프로세스마다 새로 만드는 `SESSION_ID`를 `/detections` 응답에
함께 실어 보내고(`{"session": "...", "detections": [...]}`), 이 로거는
`(session_id, tracker_id)`로 판단합니다. 미니맵이 재시작하면 session이 바뀌므로 id가
1로 돌아가도 충돌하지 않고, 로거가 재시작해도 DB의 유니크 인덱스가 중복을 막습니다.

- 구버전 미니맵(session 미전송)과 붙으면 시작할 때 경고를 한 번 찍고 예전 동작으로
  떨어집니다(부분 인덱스가 `session_id <> ''`이라 적재가 거부되지는 않습니다).
- 스키마·SQL은 `common/schema.py`의 `UNIQUE_INDEX_DDL` /
  `INSERT_DETECTION_EVENT_SQL` / `detection_event_params()`에 있습니다.
- `ensure_table()`이 인덱스도 확보합니다. 이 로거가 다른 진입점보다 먼저 뜨는 경우가
  많아 `init_all_tables()`를 기다릴 수 없기 때문입니다. 이미 중복 행이 있어 인덱스를
  못 만들면 경고만 남기고 계속 적재합니다.

```powershell
copy ..\set_env.example.ps1 ..\set_env.ps1   # 최초 1회
. ..\set_env.ps1
$env:DAEUN_LAPTOP_IP = "192.168.0.xxx"        # 다은님 노트북의 실제 IP
python edge_video\event_logger.py
```

### 환경변수

`DAEUN_LAPTOP_IP`(기본 `203.0.113.20`), `MINIMAP_PORT`(기본 `8091`),
`POLL_INTERVAL_SEC`(기본 `2.0`), `MAX_SEEN_IDS`(기본 `10000`).
DB 접속은 `common.schema.get_db_config()`의 `SFP_DB_*`.

`DAEUN_LAPTOP_IP`와 `MINIMAP_PORT`는 이제 `set_env.example.ps1`/`.sh` 템플릿에도
들어 있으므로, 실행할 때마다 손으로 넣지 않아도 됩니다.

## dashboard_video_minimap_section.py

`smart_factory_dashboard_v3.py`에 통합되는 "영상 + 미니맵 표시" 섹션.
**함수 두 개를 따로 호출합니다** (v5에서 분리 — 아래 참고).

```python
render_video(jetson_ip)        # RGB + AI 탐지 박스 MJPEG
# render_depth_video() 는 제거되었습니다 (depth 컬러맵 스트림 폐지)
render_minimap(daeun_ip)     # 본문 한 줄을 통째로 (폭 900px 확보)
```

- **v3 변경 사항**: 이전에는 `<img>` 태그를 만들기 전에 Python(Streamlit 서버)이
  `socket.create_connection()`으로 1초 타임아웃 사전 검사를 했는데, 젯슨이 VLM 추론으로
  바빠서 응답이 늦으면 실제 스트림은 멀쩡한데도 "연결할 수 없습니다"로 잘못 표시되는
  문제가 있었습니다. v3부터는 사전 검사를 없애고 태그를 항상 렌더링해서 브라우저가
  직접 연결을 시도하게 합니다. **이 원칙은 v4에서도 유지됩니다.**
- **v4 변경 사항**: 미니맵이 MJPEG 정지영상에서 **조작 가능한 페이지**로 바뀌었습니다.

  ```
  v3까지: <img src="http://다은IP:8091/minimap_feed">
  v4부터: components.iframe("http://다은IP:8091/", height=520)
  ```

  지도는 최초 1회만 받고 이후엔 좌표 JSON만 오가므로 대역폭이 훨씬 적게 들고,
  확대·이동·로봇 추적·탐지 목록 클릭이 됩니다. 연결이 끊기면 페이지 자체가
  "서버 응답 없음"을 표시하므로 자리표시자가 필요 없습니다.

  `st.markdown(unsafe_allow_html=True)`로는 안 됩니다 — **Streamlit이 `<iframe>`
  태그를 걸러냅니다.** 이 용도로 있는 `streamlit.components.v1.iframe()`을 씁니다
  (추가 의존성 없음).

  되돌리려면 `MINIMAP_LEGACY_MJPEG=1`로 실행하면 v3 방식으로 표시됩니다.
  서버의 `/minimap_feed`도 그대로 살아 있습니다.
- **자리표시자에서 외부 의존 제거**: 연결 실패 시 `via.placeholder.com` 이미지를
  받아왔는데, 폐쇄망에서는 그것도 못 받아 깨진 아이콘만 남았습니다. 이제 SVG를
  data URI로 박아 넣어 외부 요청이 없습니다.
- **`_video_url(ip, port)`**: `ai_inference_sender.py`의 스트리밍 핸들러 기준 경로 `/`를
  사용. `camera_stream_server.py`로 대체하는 경우엔 `/video_feed`로 바꿔야 합니다.
- **v5 변경 사항 (중요)**: 영상과 미니맵을 나란히 두지 않습니다.

  `minimap_web.html`은 자기 폭이 900px 밑으로 떨어지면 좁은 화면용 레이아웃으로
  접히도록 만들어져 있습니다.

  ```css
  .stage{grid-template-columns:1fr 296px}          /* 지도 | 탐지목록 */
  @media (max-width:900px){ .stage{grid-template-columns:1fr}  .side{max-height:44vh} }
  ```

  그런데 v4까지의 `render_video_and_minimap()`은 `st.columns(2)`로 영상과 미니맵이
  반씩 나눠 가졌습니다. 1920px 모니터에서 Streamlit wide 레이아웃의 본문 폭은
  사이드바와 여백을 빼면 약 1490px이고, 그 절반은 약 745px입니다. **즉 미니맵은
  대시보드 안에서 한 번도 설계된 넓은 레이아웃으로 뜬 적이 없었습니다.** 탐지
  목록이 지도 밑으로 내려가 44vh를 먹는 바람에, 520px 높이 중 지도에 남는 건
  300px 남짓이었습니다.

  그래서 함수를 `render_video()` / `render_minimap()` 둘로 쪼갰습니다.
  `render_video_and_minimap()`은 예전 호출부 호환용 wrapper로 남겨두었지만,
  새로 짜는 화면에서는 쓰지 마세요. 기본 높이도 520 → 560px로 올렸습니다
  (전체 폭을 쓰면 지도가 가로로 길어져서, 오른쪽 목록에 카드가 더 들어갑니다).
- **`_minimap_url(ip, port)`**: `/`(v4) 또는 `/minimap_feed`(레거시 모드).
  `MINIMAP_TOKEN`이 설정돼 있으면 `?t=` 로 붙여줍니다.

### 환경변수 (기본값, 사이드바 입력이 있으면 그게 우선)

| 변수 | 기본값 | 의미 |
|---|---|---|
| `JETSON_IP` | `203.0.113.10` | 젯슨 주소 |
| `JETSON_VIDEO_PORT` | `8500` | 젯슨 영상 포트 |
| `DAEUN_LAPTOP_IP` | `203.0.113.20` | 미니맵 서버 주소 |
| `MINIMAP_PORT` | `8091` | 미니맵 서버 포트 |
| `MINIMAP_TOKEN` | (없음) | 미니맵 서버에서 토큰을 켰다면 **같은 값**을 넣어야 함 |
| `MINIMAP_HEIGHT` | `560` | 미니맵 iframe 높이(px) |
| `MINIMAP_LEGACY_MJPEG` | (없음) | `1` 이면 v3 방식(MJPEG)으로 표시 |

## camera_stream_server.py (참고용, 실제로는 안 씀)

실행 위치: 젯슨(카메라가 물리적으로 붙어 있는 기기). `ai_inference_sender.py`와 별개로
순수 영상만 MJPEG로 내보내는 참고용 서버입니다. 실제로는 `ai_inference_sender.py`가 이미
8500 포트에 bbox가 그려진 화면을 자체 스트리밍하고 있으므로 이 파일은 필요 없습니다.
그 스트림이 없는 테스트/예비 환경에서 순수 카메라 영상만 필요할 때 사용하는 용도로만
남아 있습니다. `/video_feed`, `/health` 라우트, `CAMERA_INDEX`/`STREAM_PORT`/`JPEG_QUALITY`
환경변수.

## 다른 폴더와의 연동 지점

- `common/schema.py`: `event_logger.py`가 `get_db_config`, `DETECTION_EVENTS_TABLE_DDL`
  사용.
- `dashboard/smart_factory_dashboard_v3.py`: `dashboard_video_minimap_section.py`의
  `render_video()`와 `render_minimap()`을 각각 호출 (미니맵에는 본문 전체 폭을 줍니다).
- `self_test/test_minimap_logic.py`: rclpy/flask/tf2_ros 스텁을 등록한 뒤
  **이다은님 폴더의 원본** `minimap_renderer.py`를 import해서 `raycast_to_obstacle`,
  `SpatialObjectTracker` 로직만 검증.
- `self_test/test_integration_wiring.py`: `event_logger.py`가 읽는 `/detections` 키와
  미니맵이 내보내는 키가 같은지, `MINIMAP_PORT` 기본값이 양쪽에서 같은지,
  **미니맵을 부르는 두 클라이언트가 모두 `MINIMAP_TOKEN`을 읽는지** 검사.
- `self_test/fake_minimap_server.py`: 실제 `minimap_renderer.py`(ROS2) 없이 테스트할 때
  쓰는 가짜 서버. `/detections`뿐 아니라 `/`, `/map_data`, `/state`도 흉내내므로
  **대시보드 미니맵 화면까지 통째로 확인**할 수 있습니다.
- `../ROS2_자율주행_및_연동(이다은)/dashboard_link/minimap_renderer.py`: 이 폴더의 두
  클라이언트가 붙는 서버 원본.

## 알려진 이슈

- `camera_stream_server.py`는 실제 파이프라인에서 쓰이지 않는 참고/예비용 코드입니다.
- 미니맵 서버가 기본값으로 `0.0.0.0`에 인증 없이 열립니다. 이 서버는 공장 도면,
  로봇 실시간 위치, 화재 이벤트, **작업자 위치와 헬멧 미착용 이력**을 내보내므로,
  운영 시에는 다은님 노트북에서 `MINIMAP_BIND`를 내부망 IP로 좁히고
  `MINIMAP_TOKEN`을 설정하는 것이 좋습니다.
