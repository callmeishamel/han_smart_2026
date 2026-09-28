# TTS Engine and pipeline — 현장 음성 경보

프로젝트 전체 실행 순서와 배경은 최상위 [`../README.md`](../README.md)를 참고하세요.

RAG가 생성한 대응안 문장을 젯슨에 연결된 스피커로 읽어주는 파트입니다.
**파이프라인에서 사람에게 직접 닿는 유일한 출력**이라, 화면을 보고 있지 않아도
현장 작업자가 경보를 인지할 수 있게 해줍니다.

```
[RagResponseAgent 대응안 생성]  (이상민/정진철 PC)
        │  send_plan_async() → bounded 송신 큐
        │  TCP :9997  {"message_id", "text", "priority", "expires_at", "volume_percent", "token"}
        ▼                         ▲  {"status": "queued"} ACK
[Rx_pipeline.py]  (젯슨)
        │  우선순위·유효시간 재생 큐 → Piper TTS → 0~400% WAV 음량
        ▼
    aplay → 스피커
```

## 파일 구성

| 파일 | 실행 위치 | 설명 |
|---|---|---|
| `Rag_to_Jetson.py` | 이상민/정진철 PC | 공통 방송 문장 구성 + message ID/ACK 송신 + bounded 송신 worker |
| `Rx_pipeline.py` | 젯슨 | TCP 수신 → Piper TTS → aplay 재생 |
| `setup_tts.sh` | 젯슨 | **최초 1회** — 모델 내려받기 · USB 스피커 확인 · 소리까지 검증 |
| `test_tts_pipeline.py` | 아무 데나 | piper/aplay/젯슨 없이 실제 소켓 왕복으로 검증 |

## 음성 모델 — `ko_KR-kss-medium`

**Piper 공식 저장소(`rhasspy/piper-voices`)에 있는 한국어 음성은 이것 하나뿐입니다.**
예전 코드의 기본값이던 `ko_KR-hyeri-medium` 은 그 저장소에 없어서, 그 이름으로
받으려 하면 404 가 나거나 파일명이 안 맞아 조용히 무음이 됐습니다.

| 항목 | 값 |
|---|---|
| 이름 | `ko_KR-kss-medium` |
| 크기 | `.onnx` 63MB + `.onnx.json` 5KB |
| 샘플레이트 | 22,050Hz 단일 화자(여성) |
| 데이터셋 | Korean Single Speaker Speech (KSS) |
| 라이선스 | **CC BY-NC-SA 4.0 — 비상업적 사용만** |

> ⚠ **라이선스를 확인하세요.** 경진대회·연구·시연은 출처를 밝히면 되지만,
> 상용화 시에는 다른 음성을 구하거나 직접 학습해야 합니다.

**`.onnx` 와 `.onnx.json` 은 항상 짝입니다.** piper 는 모델 옆의 `.onnx.json` 을
함께 읽으므로, `.onnx` 만 받아 두면 합성이 실패하는데 에러만 봐서는 원인을 알기
어렵습니다. `setup_tts.sh` 가 둘 다 받고 둘 다 확인합니다.

모델은 63MB 라 저장소에 넣지 않습니다(`.gitignore`).

## 최초 1회 — `setup_tts.sh`

TTS 는 "안 들린다" 는 증상 하나에 원인이 다섯 개쯤 됩니다. 모델이 없거나,
`.onnx.json` 이 없거나, piper 가 안 깔렸거나, aplay 가 HDMI 로 내보내거나,
볼륨이 0 이거나. 이 스크립트가 순서대로 하나씩 짚습니다.

```bash
cd "TTS Engine and pipeline"
./setup_tts.sh              # 전부 (모델 + 장치 + 실제 소리)
./setup_tts.sh --check      # 내려받지 않고 상태만 점검
./setup_tts.sh --no-play    # 소리는 내지 않고 준비만
```

새 Linux clone에서 `Permission denied`가 나오면 현재 Git 실행 비트가 반영되지 않은
상태이므로 `bash ./setup_tts.sh`로 실행하거나 최초 1회 `chmod +x setup_tts.sh`를 적용하세요.

## USB 스피커를 쓸 때

젯슨에서 USB 스피커는 세 가지가 걸립니다. **`Rx_pipeline.py` 가 셋 다 처리합니다.**

| 함정 | 증상 | 대응 |
|---|---|---|
| 기본 출력이 HDMI | 스피커를 꽂아도 무음인데 `aplay` 는 **성공**(코드 0)으로 끝남 | `APLAY_DEVICE` 가 비어 있으면 `aplay -l` 을 읽어 **USB 카드를 자동으로** 고릅니다 |
| 카드 번호가 안 고정됨 | 재부팅·재연결로 `plughw:1,0` 이 다른 장치를 가리킴 | 번호가 아니라 **이름**(`plughw:CARD=Device,DEV=0`)으로 지정합니다 |
| 하드웨어 파라미터 불일치 | piper 는 22050Hz, 값싼 USB 는 44100/48000Hz 만 받음 | `hw:` 가 아니라 **`plughw:`** 를 써서 ALSA 가 리샘플링하게 합니다 |

재생이 실패하면 장치 캐시를 비우고 다음 문장에서 다시 찾습니다 — USB 를 뽑았다
꽂아 카드 번호가 바뀌어도 복구됩니다.

대시보드 사이드바의 `TTS 음량`은 기본 0~400% 범위입니다. `100%`는 Piper
원본이고, 초과 값은 USB 스피커에 mixer 컨트롤이 없어도 적용되는 WAV
소프트웨어 증폭입니다. 큰 원본은 클리핑 직전으로 자동 제한합니다. 상한은
`TTS_VOLUME_MAX_PERCENT`(100~400, 기본 400)으로 조정할 수 있습니다.

직접 고정하고 싶으면:

```bash
aplay -l                                      # 카드 이름 확인
export APLAY_DEVICE=plughw:CARD=Device,DEV=0  # 번호 말고 이름으로
export APLAY_AUTODETECT=0                     # 자동 탐색을 끄고 싶을 때
```

## 환경변수

**포트는 짝을 이루는 양쪽이 같은 변수를 읽습니다** — 저장소 공통 규칙입니다
(루트 [`../README.md`](../README.md) "환경변수 요약" 참고).

| 변수 | 기본값 | 읽는 곳 | 의미 |
|---|---|---|---|
| `TTS_TCP_PORT` | `9997` | **송신 + 수신 (양쪽)** | 대응안 문장 전송 포트 |
| `TTS_TOKEN` | (없음) | **송신 + 수신 (양쪽)** | 공유 비밀. 설정하면 이 값이 실린 요청만 재생합니다 |
| `JETSON_IP` | `203.0.113.10` | 송신 | 젯슨 주소. 대시보드와 같은 변수/기본값 |
| `TTS_BIND_IP` | `0.0.0.0` | 수신 | 수신 바인딩 주소 |
| `TTS_ALLOWED_IPS` | (없음) | 수신 | 쉼표로 구분한 허용 IP 목록. 비우면 제한 없음 |
| `TTS_MAX_CONNECTIONS` | `8` | 수신 | 동시에 처리할 연결 수 |
| `TTS_MESSAGE_TTL_SEC` | `20` | **송신 + 수신 (양쪽)** | 생성 후 이 시간이 지나면 재생하지 않는 경보 유효시간 |
| `TTS_SEND_QUEUE_SIZE` | `16` | 송신 | 비동기 TCP 송신 대기열 크기. 포화 시 오래된 미전송 요청 교체 |
| `TTS_SEND_DRAIN_TIMEOUT_SEC` | `15` | 송신 | 파이프라인 종료 시 남은 송신을 기다리는 최대 시간 |
| `TTS_STATUS_RETENTION_SEC` | `300` | 수신 | message ID 중복 방지·상태를 기억하는 시간 |
| `TTS_STATUS_MAX_ENTRIES` | `4096` | 수신 | 중복 방지 상태 캐시 상한. 포화 시 가장 오래된 상태부터 제거 |
| `PIPER_MODEL_PATH` | 스크립트 옆 `ko_KR-kss-medium.onnx` | 수신 + `setup_tts.sh` | Piper 음성 모델(`.onnx`). `.onnx.json` 이 같은 이름으로 옆에 있어야 합니다 |
| `APLAY_DEVICE` | (없음) | 수신 | ALSA 출력 장치. **비우면 USB 스피커를 자동 탐색** |
| `APLAY_AUTODETECT` | `1` | 수신 | `0` 이면 자동 탐색을 끄고 시스템 기본 장치를 씁니다 |
| `PIPER_VOICE` | `ko_KR-kss-medium` | `setup_tts.sh` | 내려받을 음성 이름 |

### 전송 프로토콜 v2

송신기는 각 방송에 UUID `message_id`를 만들고 구역·객체·우선순위·생성/만료시각을
함께 보냅니다. 수신기는 인증과 만료 확인 후 재생 큐에 들어간 경우에만 `queued` ACK를
돌려줍니다. 같은 ID를 재전송하면 다시 큐에 넣지 않고 `duplicate` ACK를 반환하므로,
ACK가 유실돼 송신기가 재시도해도 같은 문장이 두 번 방송되지 않습니다.

`queued`는 **젯슨 재생 대기열 등록 성공**을 뜻합니다. Piper 합성·ALSA 재생·실제 음향
출력 완료까지 확인하는 ACK는 아직 없으므로 `played`와 같은 의미로 사용하면 안 됩니다.

#### 요청과 ACK 형식

송신기는 UTF-8 JSON 한 건을 보낸 뒤 TCP 쓰기 방향을 닫습니다(`shutdown(SHUT_WR)`).
수신기는 EOF까지 최대 8,192바이트를 읽고, UTF-8 JSON 한 줄 ACK를 반환합니다.

```json
{
  "version": 2,
  "message_id": "ddf4f3f4c17a49fd93f5dc1e3d7b5ccb",
  "text": "A 구역 화재. 즉시 대피하십시오.",
  "priority": 100,
  "created_at": 1788235200.0,
  "expires_at": 1788235220.0,
  "zone": "A",
  "detected_object": "fire",
  "token": "송신기와 수신기에 설정한 동일한 공유 비밀"
}
```

| 요청 필드 | 처리 기준 |
|---|---|
| `message_id` | 요청별 고유 ID. 재시도할 때 **같은 ID**를 사용해야 중복 방송을 막습니다 |
| `text` | 필수. 공백을 정규화하고 최대 500자로 제한합니다 |
| `priority` | 0~100으로 보정. 화재·누출 100, 작업자·안전모 80, 차량·중장비 60, 기타 50 |
| `created_at`, `expires_at` | Unix epoch 초. `expires_at`이 지난 요청은 큐 등록/재생하지 않습니다 |
| `zone`, `detected_object` | 방송 추적용 메타데이터이며 `send_plan_*()`이 자동으로 채웁니다 |
| `token` | `TTS_TOKEN`을 설정한 경우에만 송신하며, 수신 값과 상수 시간 비교합니다 |

정상 ACK 예시는 다음과 같습니다. 송신기는 `message_id`가 요청과 정확히 같고 상태가
`queued` 또는 `duplicate`일 때만 성공으로 처리합니다.

```json
{"message_id":"ddf4f3f4c17a49fd93f5dc1e3d7b5ccb","status":"queued"}
```

| ACK 상태 | 송신 성공 | 의미 |
|---|---:|---|
| `queued` | 예 | 이 요청이 젯슨 재생 큐에 등록됨 |
| `duplicate` | 예 | 같은 ID가 이미 `receiving/queued/playing/played` 상태여서 다시 넣지 않음 |
| `expired` | 아니요 | 수신 시점에 유효시간이 지남 |
| `rejected` | 아니요 | 토큰 또는 허용 IP 검증 실패 |
| `dropped` | 아니요 | 수신 큐가 찼고 새 요청의 우선순위가 기존 요청보다 높지 않음 |
| `invalid` | 아니요 | 빈 문장 또는 해석할 수 없는 요청 |
| `error` / `transport_error` | 아니요 | 수신 처리 예외 또는 연결·ACK 형식/ID 검증 실패 |

#### 상태 전이와 실패 의미

```text
created → PC sender queue → TCP sent → receiving → queued → playing → played
             └→ send_queue_dropped       ├→ dropped/expired
                                          └→ failed (합성·재생 오류)
```

- 연결이나 ACK가 유실되면 송신기는 기본 1회, **같은 `message_id`로** 재시도합니다.
  첫 요청이 이미 큐에 들어갔다면 두 번째 연결은 `duplicate`를 받습니다.
- 큐 등록 뒤 ACK 전송만 실패해도 수신 상태를 `failed`로 되돌리지 않습니다. 재시도가
  기존 `queued` 상태를 찾아 중복 재생을 막습니다.
- Piper 또는 `aplay` 실패는 젯슨 내부 상태를 `failed`로 남기지만, 이미 연결이 끝난
  송신 측에 사후 통지하지는 못합니다. 따라서 현재 프로토콜은 **at-most-once 큐 등록에
  가까운 동작**이며, 실제 음향 출력 보장은 아닙니다.
- `created_at`/`expires_at`은 절대시각이므로 두 장비 시간을 NTP 등으로 맞춰야 합니다.

#### 두 단계 큐 정책

| 위치 | 기본 크기 | 포화 시 정책 | 목적 |
|---|---:|---|---|
| PC 송신 큐 | 16 | 가장 오래 기다린 미전송 요청을 최신 요청으로 교체 | 네트워크 장애가 탐지/DB 루프를 막지 않게 함 |
| 젯슨 재생 큐 | 4 | 더 높은 우선순위가 오면 최저 우선순위를 제거. 같으면 오래된 요청을 교체 | 긴 방송 중 화재 같은 긴급 경보가 밀리지 않게 함 |
| 젯슨 상태 캐시 | 4,096 | 보존시간(기본 300초) 경과 항목과 가장 오래된 항목부터 제거 | 중복 방지와 메모리 상한을 함께 보장 |

> 프로토콜 v2 송신기와 수신기는 함께 배포하세요. 새 송신기는 ACK가 없는 구버전
> `Rx_pipeline.py`를 실패로 판단하며, 구버전 송신기는 우선순위·만료·중복 방지 정보를
> 보내지 못합니다. 저장소 원본과 `smart_factory_project/tts/` 배포본은 같은 버전입니다.

### ⚠ 이 수신기는 데이터 서버가 아니라 **액추에이터**입니다

같은 망에 있는 누구든 TCP로 문장을 던지면 **공장 스피커가 그대로 읽습니다.**
"지금 즉시 대피하십시오" 같은 문장이 장난으로 나가면 그 자체가 안전 사고입니다.
미니맵 서버(`MINIMAP_TOKEN`/`MINIMAP_BIND`)와 같은 방식으로 제한하세요.

```bash
export TTS_BIND_IP=203.0.113.10      # 모든 인터페이스 대신 내부망 하나만
export TTS_TOKEN=$(python3 -c "import secrets;print(secrets.token_urlsafe(16))")
# 보내는 쪽(이상민님 PC)의 set_env 에도 같은 TTS_TOKEN 을 넣어야 합니다
```

기본값은 제한 없음이라 예전 동작 그대로이고, 그 상태로 뜨면 시작 로그에 경고가
나옵니다. 토큰이 틀리거나 허용 목록 밖 IP면 **재생하지 않고 거부 로그만 남깁니다.**

### 스피커에서 소리가 안 날 때

젯슨은 기본 출력이 HDMI로 잡히는 경우가 많습니다. 그러면 스피커를 꽂아도 소리가
안 나면서 `aplay`는 성공으로 끝나서, 로그만 봐서는 정상으로 보입니다.

```bash
aplay -l                              # 카드/장치 번호 확인
export APLAY_DEVICE=plughw:1,0
```

## 실행

**젯슨 쪽 (먼저 띄우세요)**

```bash
# 사전 조건(piper CLI · alsa-utils · 음성 모델)을 한 번에 점검하고 받습니다
./setup_tts.sh

python3 Rx_pipeline.py
```

모델 경로를 직접 주고 싶을 때만:

```bash
export PIPER_MODEL_PATH=/opt/models/ko_KR-kss-medium.onnx
python3 Rx_pipeline.py
```

**이름을 `setup_tts.sh`가 받는 것과 다르게 쓰면 안 됩니다.** 예전 안내가 가리키던
`ko_KR-hyeri-medium`은 Piper 공식 저장소에 없는 음성이라, 모델을 제대로 받아 놓고도
이 변수 하나 때문에 없는 파일을 보게 됩니다(증상은 무음뿐입니다).

**PC 쪽 (동작 확인)**

```bash
JETSON_IP=203.0.113.10 python3 Rag_to_Jetson.py
```

**하드웨어 없이 로직만 검증**

```bash
python test_tts_pipeline.py
```

### 검증 결과 (2026-09-01)

| 검증 | 결과 | 범위 |
|---|---|---|
| TTS 단독 테스트 | 통과 | 실제 localhost TCP 왕복, 500자 한글, 연속 전송, queued/duplicate/expired ACK, 잘못된 ACK ID, ACK 유실, 송수신 큐 포화, 우선순위, TTL, 토큰/IP 제한, 느린 연결, USB 장치 선택 |
| 프로젝트 self-test | **19/19 통과** | TTS를 포함한 파이프라인·대시보드·RAG·ROS2 연동 회귀 검사 |
| Python 정적 로드 검사 | 통과 | 저장소 전체 `python -m compileall -q .` |
| 셸 문법 | **15/15 통과** | Git Bash `bash -n` |
| README 로컬 링크 | **77/77 통과** | 폴더 내부 README 22개 |
| 배포본 동기화 | 통과 | 원본과 `smart_factory_project/tts/` 송신기·수신기의 SHA-256 일치 |

Windows의 하드웨어 없는 환경에서 수행했으므로 Piper 모델 합성, Jetson ALSA 장치,
실제 스피커 음량은 이 결과에 포함되지 않습니다. 젯슨에서는 `./setup_tts.sh`의 마지막
시험 음성이 실제로 들리는지 별도로 확인해야 합니다.

## 파이프라인 연동 (완료)

[`../데이터 플랫폼 및 대시보드(이상민)/pipeline/patrol_pipeline_rag.py`](../데이터%20플랫폼%20및%20대시보드(이상민)/pipeline/patrol_pipeline_rag.py)에
`--tts` 옵션으로 연결되어 있습니다. 대응안을 `response_plans`에 적재한 직후,
`구역 + 탐지 객체 + 대응안` 형식의 공통 문장을 젯슨으로 비동기 송신합니다. 탐지 객체는
`fire`→`화재`, `person with no helmet`→`안전모 미착용 작업자`처럼 방송용 한국어로
바꿉니다. 자동 방송과 대시보드 수동 방송이 같은 `build_speech_text()`를 사용하므로
안내 내용이 갈라지지 않습니다.

```bash
# 1) 젯슨에서 수신기를 먼저 띄우고
python3 Rx_pipeline.py

# 2) PC에서 파이프라인을 --tts로 실행
cd "../데이터 플랫폼 및 대시보드(이상민)"
./run_pipeline_rag.sh --tts
```

**선택 기능이라 없어도 나머지는 그대로 동작합니다.** 파이프라인은 저장소 루트의
이 폴더를 자동으로 찾고, 못 찾으면 경고만 남긴 뒤 음성 경보 없이 계속 돕니다
(`RagResponseAgent`가 지식베이스 없이도 내장 지침으로 동작하는 것과 같은 방식).
폴더를 옮겼다면 `TTS_MODULE_DIR`로 위치를 지정하세요.

`--tts`를 기본값으로 켜두지 않은 이유는, 젯슨 없이 개발용으로 돌릴 때
(`self_test/fake_jetson_sender.py` 등) 경보마다 연결 실패 로그가 쌓이기 때문입니다.

### 왜 비동기인가

동기 `send_alert_to_jetson()`은 젯슨이 꺼져 있을 때 connect 타임아웃만큼(기본 3초)
파이프라인 루프를 멈추고, 그동안 도착한 UDP 탐지 패킷이 커널 버퍼에서 밀려납니다.
파이프라인은 `send_plan_async()`만 사용합니다. 호출마다 daemon 스레드를 만드는 대신
크기 16의 단일 송신 worker를 사용하며, 포화 시 오래된 미전송 요청을 교체합니다. 종료
시에는 최대 `TTS_SEND_DRAIN_TIMEOUT_SEC` 동안 남은 송신을 처리하고 ACK 통계를 남깁니다.

> **`TTS 대기열 등록` 로그는 "스피커에서 울렸다"는 뜻이 아닙니다.** 젯슨이 인증·만료
> 검사를 통과시켜 재생 큐에 넣고 `queued` ACK를 보냈다는 뜻입니다. 파이프라인 종료
> 요약도 제출·젯슨 큐 등록·실패·드롭 건수를 각각 표시합니다.

### 느린 연결 하나가 경보를 통째로 막던 문제 [수정 완료]

수신기가 `accept()` 한 연결을 **하나씩 직렬로** 처리했습니다. 게다가
`socket.settimeout()`은 `recv` 한 번마다 다시 세어지기 때문에, 1바이트씩 천천히
흘리는 클라이언트는 연결을 사실상 무한히 붙잡을 수 있었습니다.

실제로 재현해 보니 느린 연결 하나가 붙어 있는 동안 **진짜 경보가 12초가 지나도
도착하지 못했고, 송신 측은 "송신 성공"을 찍었습니다**(TCP backlog에 들어갔으므로).
즉 경보가 안 울린 줄도 모르는 상태였습니다.

두 가지를 고쳤습니다.

- 연결마다 스레드로 처리하고 동시 처리 수를 `TTS_MAX_CONNECTIONS`(기본 8)로 제한.
  상한을 넘으면 새 연결을 즉시 닫습니다.
- `_recv_until_close()`가 **요청 전체의 마감 시각**을 받아, 남은 시간만큼만
  `settimeout()`을 겁니다. 천천히 흘려도 `RECV_TIMEOUT_SECONDS`(5초)에 끊깁니다.

같은 조건에서 다시 재현하니 12초+ → **0.02초**입니다.
`test_tts_pipeline.py`가 이 두 가지를 자동으로 확인합니다.

### 중복 방송 억제는 상류에서 처리됩니다

여기서 따로 만들 필요가 없습니다 — `common.schema.ResponsePlanThrottle`(기본 30초)이
같은 `(구역, 객체)` 조합의 **대응안 생성 자체**를 막고 있어서, 이 폴더까지 오는
문장은 이미 걸러진 상태입니다. 억제 정책을 두 곳에 두면 저장소의 단일 진실 공급원
원칙(`common/schema.py`)이 깨지므로 의도적으로 상류에만 뒀습니다.
방송 간격을 바꾸려면 `--suppress-seconds`를 조정하세요.

## 설계 메모

### 왜 셸을 쓰지 않는가

`text`는 네트워크에서 그대로 들어오는 값입니다. 예전 구현은

```python
os.system(f'echo "{text}" | piper --model {MODEL_PATH} --output_file alert.wav')
```

처럼 f-string으로 셸 명령을 만들었는데, 큰따옴표 하나로 명령을 탈출할 수 있어
젯슨에서 **임의 명령이 실행**됩니다. 악의가 없어도 대응안에 `"`나 `$`가 섞이면
그냥 깨집니다. 지금은 `subprocess.run([...], input=...)`으로 리스트 인자를 넘겨
셸이 개입할 여지 자체를 없앴습니다. `test_tts_pipeline.py`가 AST로 이 성질을
검사하므로, 나중에 실수로 되돌리면 테스트가 잡아냅니다.

### 왜 EOF까지 읽는가

TCP는 스트림이라 `recv(1024)` 한 번으로 보낸 만큼을 다 받는다는 보장이 없습니다.
게다가 `recommended_action`은 `VARCHAR(500)`이고 한글 500자는 UTF-8로 최대
1500바이트라, 1024에서 **반드시 잘립니다**. 멀티바이트 문자 중간에서 잘리면
디코딩까지 터집니다. 송신 측이 `shutdown(SHUT_WR)`로 쓰기를 닫으므로 수신 측은
EOF까지 읽으면 됩니다.

### 왜 재생을 큐로 분리했는가

예전에는 accept 루프 안에서 바로 재생해서, 한 문장을 읽는 동안(5~15초) 다른 구역
경보를 아예 받지 못했습니다. 지금은 수신 즉시 큐에 넣고 워커 스레드가 재생합니다.
화재·누출(100) → 작업자·안전모(80) → 차량·중장비(60) → 기타(50) 순으로 먼저
재생합니다. 큐가 차면 가장 낮은 우선순위를 버리고, 같은 등급에서는 오래된 경보를
교체합니다. `expires_at`이 지난 요청은 큐 등록 또는 재생 직전에 폐기하므로 과거 상황을
늦게 방송하지 않습니다.

## 이슈 상태 / 운영 조건

- **TTS 인증 `[기능 해결, 운영 설정 필요]`**: `TTS_TOKEN` 공유 비밀과
  `TTS_ALLOWED_IPS` 허용 목록 검증이 구현됐습니다. 토큰 비교에는 timing attack을 피하기
  위한 상수 시간 비교를 사용하고, 인증 실패 요청은 재생하지 않습니다. 다만 기존 시연과의
  호환성을 위해 두 변수의 기본값은 제한 없음입니다. 공유 Wi-Fi나 현장 배포에서는 위
  환경변수 예시대로 반드시 하나 이상 설정하고 방화벽도 함께 제한하세요.
- **음성 모델 미커밋 `[문제 없음 — 의도된 배포 방식]`**: 실제 모델은
  `ko_KR-kss-medium.onnx`이며 약 63MB라 저장소에 넣지 않습니다. `setup_tts.sh`가 모델과
  `.onnx.json`을 함께 내려받고, 다른 위치에 설치했다면
  `PIPER_MODEL_PATH=/설치경로/ko_KR-kss-medium.onnx`처럼 지정합니다.
- **실기 재생 확인 `[부분 해결, 검증 필요]`**: `queued` ACK로 인증·만료 검사와 재생 큐
  등록까지는 송신 측에서 확인합니다. Piper 합성·ALSA 장치·실제 스피커 출력 완료 ACK는
  아직 없으므로 현장 배포 전 젯슨 로그와 실제 음성으로 종단 간 검증해야 합니다.
- **전송 구간 암호화 `[실제 문제, 미해결]`**: `TTS_TOKEN`은 무단 방송을 막지만 현재
  TCP 자체는 TLS가 아니어서 같은 네트워크에서 패킷을 볼 수 있는 공격자에게 토큰과 방송
  문장이 노출될 수 있습니다. 격리된 내부망·방화벽에서 운용하고, 외부망을 통과해야 하면
  WireGuard/SSH 터널 또는 TLS 프록시를 사용해야 합니다.
- **중복 상태 영속화 `[실제 문제, 미해결]`**: 중복 방지 상태는 젯슨 프로세스 메모리에만
  있습니다. 수신기를 재시작하거나 상태가 300초/4,096건 상한으로 제거된 뒤 아주 늦게 같은
  ID가 재전송되면 다시 방송될 수 있습니다. 일반 ACK 재시도(0.3초)에는 충분하지만 재시작
  사이까지 정확히 한 번을 보장하려면 SQLite/Redis 같은 영속 저장소가 필요합니다.
- **장비 시각 동기화 `[운영 조건]`**: TTL은 Unix 절대시각을 쓰므로 PC와 젯슨 시계가 크게
  어긋나면 새 경보가 만료되거나 오래된 경보가 살아날 수 있습니다. 운영 이미지에서 NTP
  동기화 상태를 확인하세요.

## 방송을 켜는 두 가지 방법

| | 무엇 | 언제 |
|---|---|---|
| 자동 | `patrol_pipeline_rag.py --tts` | 생성되는 대응안을 **전부** 방송 |
| 수동 | 대시보드의 **🔊 현장 음성 안내** 버튼 | 관제자가 내용을 보고 고른 것만 |

수동 버튼은 대응안 카드 아래에 있습니다. 누르면 그 자리에서 젯슨으로 보내고
`queued` ACK 성공/실패를 화면에 표시합니다. 비동기가 아니라 동기로 보내는 이유는
버튼을 누른 관제자에게 젯슨이 실제로 대기열에 넣었는지 즉시 알려주기 위해서입니다.

둘 다 같은 수신기(`Rx_pipeline.py`, TCP 9997)로 갑니다. 자동을 켠 채로 버튼도
누르면 같은 내용이 두 번 나갈 수 있으니, 보통은 둘 중 하나만 쓰세요.

> 성공 표시는 **"젯슨 재생 대기열에 등록됐다"**는 뜻입니다. 실제 스피커 출력 완료는
> 아직 보내는 쪽에서 알 수 없습니다. 젯슨 로그와 실제 음향을 함께 확인하세요.
