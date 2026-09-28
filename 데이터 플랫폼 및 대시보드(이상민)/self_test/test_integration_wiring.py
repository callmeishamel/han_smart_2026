r"""
self_test/test_integration_wiring.py

**팀 경계를 넘는 배선 계약**을 검사하는 테스트.

이 프로젝트는 4개 파트가 서로 다른 기기에서 돌고, 그 사이를 UDP/TCP/HTTP로
잇습니다. 여기서 나는 사고는 전부 같은 모양입니다 — **아무 에러도 안 나고,
그냥 데이터가 조용히 안 옵니다.** UDP는 목적지가 없어도 성공으로 처리되고,
포트나 필드 이름이 한쪽만 바뀌어도 예외가 없습니다.

실제로 그런 상태였던 두 가지를 이 테스트가 잡습니다.

  1. ai_inference_sender.py 가 9999(젯슨 호스트)와 9998(이상민 PC)을
     HOST_IP 하나로 보냈습니다. 두 기기가 다르므로 동시에 맞출 값이 없어,
     둘 중 하나는 반드시 못 받는 구조였습니다.
  2. 파이프라인이 127.0.0.1 에만 바인딩했습니다. 젯슨은 다른 기기라
     패킷이 하나도 도착하지 않는데, 로그에는 "수신 대기 시작"만 찍힙니다.

파일을 import 하지 않고 **소스 텍스트에서 상수를 뽑아** 비교합니다.
rclpy / cv2 / psycopg2 / flask 중 아무것도 필요 없고, 젯슨 코드처럼 이 PC에서
아예 import 되지 않는 파일도 검사할 수 있기 때문입니다.

실행 방법 (프로젝트 루트에서):
    python self_test\test_integration_wiring.py
"""

import io
import os
import re
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO_ROOT = os.path.dirname(_PROJECT_ROOT)

if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# Windows 기본 콘솔(cp949)에서 이모지/em대시를 출력하다 죽는 것을 막습니다.
from common.console import enable_utf8_console  # noqa: E402
enable_utf8_console()

JETSON = os.path.join(_REPO_ROOT, "젯슨 연결용 프로그램(손준영)")
DAEUN = os.path.join(_REPO_ROOT, "ROS2_자율주행_및_연동(이다은)")
TTS = os.path.join(_REPO_ROOT, "TTS Engine and pipeline")

FILES = {
    "sender":     os.path.join(JETSON, "ai_inference_sender.py"),
    "ros2bridge": os.path.join(JETSON, "vision_inference_node.py"),
    "minimap":    os.path.join(DAEUN, "dashboard_link", "minimap_renderer.py"),
    "nav2_params": os.path.join(DAEUN, "config", "nav2_params.yaml"),
    "evdetector": os.path.join(DAEUN, "smart_factory_sim", "event_detector.py"),
    "pipeline":   os.path.join(_PROJECT_ROOT, "pipeline", "jetson_patrol_pipeline.py"),
    "pipeline_rag": os.path.join(_PROJECT_ROOT, "pipeline", "patrol_pipeline_rag.py"),
    "adapter":    os.path.join(_PROJECT_ROOT, "integration", "team_integration_adapter.py"),
    "dashboard":  os.path.join(_PROJECT_ROOT, "dashboard", "smart_factory_dashboard_v3.py"),
    "videosec":   os.path.join(_PROJECT_ROOT, "edge_video", "dashboard_video_minimap_section.py"),
    "evlogger":   os.path.join(_PROJECT_ROOT, "edge_video", "event_logger.py"),
    "schema":     os.path.join(_PROJECT_ROOT, "common", "schema.py"),
    "tts_tx":     os.path.join(TTS, "Rag_to_Jetson.py"),
    "tts_rx":     os.path.join(TTS, "Rx_pipeline.py"),
}

_SRC_CACHE = {}
_MISSING = []


def src(key):
    if key not in _SRC_CACHE:
        path = FILES[key]
        if not os.path.exists(path):
            _MISSING.append(key)
            _SRC_CACHE[key] = ""
        else:
            _SRC_CACHE[key] = io.open(path, encoding="utf-8").read()
    return _SRC_CACHE[key]


def env_default(key, var):
    """os.getenv("VAR", "기본값") / os.environ.get("VAR", "기본값") 에서 기본값을 뽑는다."""
    m = re.search(
        r'os\.(?:getenv|environ\.get)\(\s*["\']' + re.escape(var) + r'["\']\s*,\s*["\']([^"\']*)["\']',
        src(key),
    )
    return m.group(1) if m else None


def argparse_default(key, option):
    """parser.add_argument("--opt", ... default=X ...) 에서 X 를 뽑는다."""
    m = re.search(
        r'add_argument\(\s*["\']' + re.escape(option) + r'["\'](.*?)\)\s*\n',
        src(key), re.S,
    )
    if not m:
        return None
    d = re.search(r'default\s*=\s*([^,\n]+)', m.group(1))
    return d.group(1).strip() if d else None


def func_body(key, name):
    """def <name>( 부터 다음 최상위 정의 직전까지를 잘라낸다.

    docstring 안에 빈 줄이 있어서 "빈 줄까지"로는 자를 수 없습니다.
    들여쓰기가 풀리는 지점(다음 def/class/상수)을 경계로 씁니다.
    """
    lines = src(key).splitlines()
    out, on = [], False
    for ln in lines:
        if re.match(r"\s*def\s+" + re.escape(name) + r"\s*\(", ln):
            on = True
        elif on and ln and not ln[0].isspace():
            break
        if on:
            out.append(ln)
    return "\n".join(out)


def top_level_class_body(key, name):
    """Return one top-level class block without unrelated module code."""
    match = re.search(
        r"^class\s+" + re.escape(name) + r"\s*:.*?(?=^class\s+|^def\s+|\Z)",
        src(key),
        re.M | re.S,
    )
    return match.group(0) if match else ""


def check(desc, condition, detail=""):
    print(f"[{'OK ' if condition else 'FAIL'}] {desc}")
    if not condition:
        raise AssertionError(f"{desc}{(' — ' + detail) if detail else ''}")


def main():
    for key in FILES:
        src(key)
    if _MISSING:
        print("[경고] 아래 파일을 찾지 못했습니다 (저장소를 통째로 받아야 전부 검사됩니다):")
        for k in _MISSING:
            print(f"       {k}: {FILES[k]}")
        print()

    print("=== 1. 포트 짝 맞추기 (한쪽만 바꾸면 조용히 끊깁니다) ===")
    pairs = [
        ("ROS2 브릿지",   "ROS2_UDP_PORT",      "sender",  "ros2bridge", "9999"),
        ("미니맵 각도",   "DETECTION_UDP_PORT", "sender",  "minimap",    "9091"),
        ("영상 스트림",   "JETSON_VIDEO_PORT",  "sender",  "videosec",   "8500"),
        ("미니맵 HTTP",   "MINIMAP_PORT",       "minimap", "videosec",   "8091"),
        ("미니맵 폴링",   "MINIMAP_PORT",       "minimap", "evlogger",   "8091"),
        ("TTS 경보",      "TTS_TCP_PORT",       "tts_tx",  "tts_rx",     "9997"),
    ]
    for label, var, a, b, expect in pairs:
        if a in _MISSING or b in _MISSING:
            continue
        da, db = env_default(a, var), env_default(b, var)
        check(f"{label}: {var} 양쪽 모두 {expect}",
              da == db == expect, f"{a}={da} / {b}={db}")

    # 대시보드 파이프라인만 argparse 기본값이라 따로 검사합니다.
    if "sender" not in _MISSING:
        rag_port_default = argparse_default("pipeline_rag", "--udp-port")
        check("대시보드/DB: sender DASHBOARD_UDP_PORT 와 파이프라인 --udp-port 가 9998",
              env_default("sender", "DASHBOARD_UDP_PORT") == "9998"
              and argparse_default("pipeline", "--udp-port") == "9998"
              and rag_port_default in ("9998", "DEFAULT_DASHBOARD_UDP_PORT")
              and env_default("pipeline_rag", "DASHBOARD_UDP_PORT") == "9998")

    print("\n=== 2. 목적지 IP — 세 포트는 서로 다른 기기입니다 ===")
    if "sender" not in _MISSING:
        s = src("sender")
        # 9999는 젯슨 호스트, 9998은 이상민 PC. 예전에는 server_ip 하나를
        # 두 곳에 함께 썼는데, 그러면 둘 중 하나는 반드시 못 받습니다.
        check("ROS2 목적지와 대시보드 목적지가 서로 다른 변수",
              "ROS2_BRIDGE_IP" in s and "DASHBOARD_IP" in s,
              "HOST_IP 하나로 두 기기를 가리킬 수 없습니다")
        job = re.search(r'send_jobs\s*=\s*\[(.*?)\]', s, re.S)
        check("  전송 목록에서도 두 IP를 따로 씀",
              job is not None
              and "ros2_ip" in job.group(1) and "dashboard_ip" in job.group(1))
        check("미니맵 각도는 DAEUN_LAPTOP_IP 로 따로 나감",
              env_default("sender", "DAEUN_LAPTOP_IP") is not None)

    print("\n=== 3. 수신 바인딩 — 루프백이면 다른 기기 패킷을 못 받습니다 ===")
    loopback = ("127.0.0.1", "localhost", "::1")
    check("파이프라인 --udp-ip 기본값이 루프백이 아님",
          argparse_default("pipeline", "--udp-ip") == "DEFAULT_UDP_BIND")
    check("RAG 파이프라인도 동일",
          argparse_default("pipeline_rag", "--udp-ip") == "DEFAULT_UDP_BIND")
    # None 을 "루프백이 아님"으로 통과시키면, 변수가 아예 없어도 OK 가 나옵니다.
    # 값이 존재하고 그 값이 루프백이 아닐 것 — 두 조건을 모두 요구합니다.
    pipe_bind = env_default("pipeline", "PIPELINE_UDP_BIND")
    check("DEFAULT_UDP_BIND 자체가 루프백이 아님",
          pipe_bind is not None and pipe_bind not in loopback, f"현재 {pipe_bind}")
    adapter_bind = env_default("adapter", "PIPELINE_UDP_BIND")
    check("통합 어댑터 기본 바인딩도 루프백이 아님",
          adapter_bind is not None and adapter_bind not in loopback,
          f"현재 {adapter_bind}")

    print("\n=== 4. UDP 페이로드 필드 이름 ===")
    if "sender" not in _MISSING:
        s = src("sender")
        # 젯슨 -> 9999/9998 : {"timestamp", "detections":[{"object","bbox","distance_meter"}]}
        for field in ("object", "bbox", "distance_meter", "detections"):
            check(f"sender 가 '{field}' 를 내보냄", f'"{field}"' in s)
        # 젯슨 -> 9091 : {"label", "angle_offset"}
        for field in ("label", "angle_offset"):
            check(f"sender 가 미니맵용 '{field}' 를 내보냄", f'"{field}"' in s)

    sc = src("schema")
    for field in ("object", "bbox", "distance_meter"):
        check(f"build_detection_event 가 '{field}' 를 읽음", f'"{field}"' in sc)
    if "minimap" not in _MISSING:
        mm = src("minimap")
        for field in ("label", "angle_offset"):
            check(f"minimap_renderer 가 '{field}' 를 읽음", f'"{field}"' in mm)

    print("\n=== 5. /detections 응답 계약 (미니맵 -> event_logger / schema) ===")
    keys = ("id", "label", "x", "y", "hit_count", "last_seen")
    if "minimap" not in _MISSING:
        block = re.search(r"def detections_endpoint\(\):(.*?)\n@", src("minimap"), re.S)
        body = block.group(1) if block else ""
        for k in keys:
            check(f"미니맵이 '{k}' 를 내보냄", f'"{k}"' in body)
    ev = src("evlogger")
    # 적재용 키 읽기는 common/schema.py 의 detection_event_params() 로 모았습니다
    # (INSERT 문과 같은 자리에 두려고). event_logger 는 캐시 키와 로그 문구에
    # 쓰는 것만 직접 읽습니다.
    params_body = func_body("schema", "detection_event_params")
    for k in keys:
        check(f"detection_event_params 가 '{k}' 를 읽음", f'"{k}"' in params_body)
    check("event_logger 가 중복 판단에 'id' 를 씀", '"id"' in ev)
    check("build_map_detection_event 도 같은 키를 읽음",
          all(f'"{k}"' in sc for k in keys))

    # tracker id 는 미니맵이 재시작하면 1부터 다시 시작합니다. id 하나만으로
    # 중복을 판단하면, 재시작 뒤 새로 잡힌 물체를 "이미 본 1번"으로 오인해서
    # 통째로 버리게 됩니다 (중복 행보다 위험한 기록 누락).
    if "minimap" not in _MISSING:
        check("미니맵이 세션 식별자를 함께 내보냄",
              '"session"' in body and "SESSION_ID" in src("minimap"))
    check("event_logger 가 session 을 읽어 (session, id) 로 판단",
          '"session"' in ev and "session_id" in ev)
    # detected_at 은 TIMESTAMPTZ 이고 last_seen 은 epoch 초(time.time())입니다.
    # to_timestamp() 를 빼먹으면 1970년으로 들어가서 보존 정리에 즉시 지워집니다.
    check("last_seen 을 to_timestamp() 로 변환해서 넣음", "to_timestamp(" in sc)
    if "minimap" not in _MISSING:
        check("  미니맵의 last_seen 은 epoch 시계(time.time())",
              top_level_class_body("minimap", "TrackedObject").count(
                  "self.last_seen = time.time()"
              ) >= 2
              and "monotonic" not in top_level_class_body(
                  "minimap", "TrackedObject"
              ))

    # 미니맵을 부르는 클라이언트는 둘(대시보드 iframe, event_logger 폴링)인데
    # 예전에는 대시보드만 토큰을 붙였습니다. 미니맵의 before_request 는 토큰 없는
    # 요청을 전부 403 으로 막으므로, 토큰을 켜는 순간 detection_events 적재만
    # 조용히 멈췄습니다 (화면은 멀쩡해서 눈치채기 어려움).
    if "minimap" not in _MISSING and "MINIMAP_TOKEN" in src("minimap"):
        for key, label in (("videosec", "대시보드"), ("evlogger", "event_logger")):
            check(f"미니맵을 부르는 {label} 도 MINIMAP_TOKEN 을 읽음",
                  "MINIMAP_TOKEN" in src(key))

    print("\n=== 6. 적재 SQL 은 common/schema.py 한 곳에만 ===")
    # INSERT 문이 적재하는 쪽마다 흩어져 있으면, 컬럼을 늘렸을 때 한쪽만 고치고
    # 넘어갑니다. 실제로 patrol_logs 의 map_x/map_y 가 두 INSERT 문 모두에서
    # 빠져 있어서, 컬럼도 데이터클래스도 테스트도 다 있는데 값만 NULL 이었습니다.
    for table in ("patrol_logs", "response_plans", "detection_events"):
        offenders = []
        for key in ("pipeline", "pipeline_rag", "adapter", "evlogger"):
            if key in _MISSING:
                continue
            if re.search(r"INSERT\s+INTO\s+" + table, src(key), re.I):
                offenders.append(key)
        check(f"{table} INSERT 문이 schema.py 밖에 없음",
              not offenders, f"발견: {', '.join(offenders)}")

    sc_sql = src("schema")
    check("schema.py 가 INSERT_PATROL_LOG_SQL 을 제공", "INSERT_PATROL_LOG_SQL" in sc_sql)
    check("schema.py 가 INSERT_RESPONSE_PLAN_SQL 을 제공", "INSERT_RESPONSE_PLAN_SQL" in sc_sql)
    m = re.search(r"INSERT_PATROL_LOG_SQL\s*=\s*\"\"\"(.*?)\"\"\"", sc_sql, re.S)
    check("  patrol_logs INSERT 에 map_x/map_y 가 포함됨",
          m is not None and "map_x" in m.group(1) and "map_y" in m.group(1))

    print("\n=== 7. 탐지 라벨 철자 ===")
    # classify_risk() 는 라벨을 집합과 "문자열 그대로" 비교합니다. 철자가 하나만
    # 어긋나도 조용히 "미분류 -> 주의"로 떨어지고, 파이프라인은 위험 등급에서만
    # 대응안을 만들기 때문에 그 상황에는 대응안이 아예 생성되지 않습니다.
    # 실제로 안전모 미착용이 이렇게 빠져 있었고, 같은 개념에 철자가 셋이었습니다.
    #   젯슨 "person with no helmet" / mock "no safety helmet" / OWL "person without helmet"
    if "sender" not in _MISSING:
        m = re.search(r"OUTPUT_LABELS\s*=\s*\[(.*?)\]", src("sender"), re.S)
        jetson_labels = set(re.findall(r'"([^"]+)"', m.group(1))) if m else set()
        check("젯슨 출력 라벨을 읽을 수 있음", bool(jetson_labels))
        for lb in sorted(jetson_labels):
            check(f"  classify_risk 가 '{lb}' 를 알고 있음", f'"{lb}"' in sc)
        check("내부 탐지는 부정문 대신 safety helmet을 사용",
              '"safety helmet"' in src("sender"))

    if "evdetector" not in _MISSING:
        # 주석에는 "예전 철자는 이랬다"는 설명이 남아 있으므로 코드만 봅니다.
        code = "\n".join(ln.split("#", 1)[0] for ln in src("evdetector").splitlines())
        check("event_detector 가 정본 철자를 씀",
              '"person with no helmet"' in code)
        for old in ("no safety helmet", "person without helmet"):
            check(f"  옛 철자 '{old}' 가 코드에 남아 있지 않음", old not in code)

    print("\n=== 8. TTS 페이로드 ===")
    if "tts_tx" not in _MISSING and "tts_rx" not in _MISSING:
        check("송신이 {\"text\": ...} JSON 을 보냄", '"text"' in src("tts_tx"))
        check("  message_id·우선순위·만료시각을 함께 보냄",
              all(key in src("tts_tx")
                  for key in ('"message_id"', '"priority"', '"expires_at"')))
        check("  수신 queued ACK를 확인한 뒤 성공 처리",
              "_read_ack" in src("tts_tx") and "ACK_SUCCESS_STATUSES" in src("tts_tx")
              and "_send_ack" in src("tts_rx"))
        check("  재시도는 message_id로 중복 재생하지 않음",
              "duplicate" in src("tts_rx") and "get_message_status" in src("tts_rx"))
        check("수신이 JSON 파싱 + 평문 폴백을 함",
              "json.loads" in src("tts_rx") and "JSONDecodeError" in src("tts_rx"))
        # 토큰 변수 이름이 한쪽만 바뀌면, 켠 줄 알았는데 전부 거부되거나
        # 전부 통과합니다. 둘 다 조용한 실패라 같은 변수를 읽는지 확인합니다.
        check("송/수신이 같은 TTS_TOKEN 변수를 읽음",
              env_default("tts_tx", "TTS_TOKEN") is not None
              and env_default("tts_rx", "TTS_TOKEN") is not None)
        # 이 수신기는 소리를 내는 액추에이터입니다. 인증 수단이 아예 없으면
        # 같은 망의 누구든 공장 스피커로 아무 문장이나 내보낼 수 있습니다.
        check("수신에 토큰 검증이 있음",
              "compare_digest" in src("tts_rx") and "is_authorized" in src("tts_rx"))
        # 느린 연결 하나가 진짜 경보를 통째로 막던 문제 (README "설계 메모" 참고)
        check("수신이 연결을 직렬로 처리하지 않음",
              "Semaphore" in src("tts_rx") and "threading.Thread" in src("tts_rx"))
        check("요청 전체 마감시각이 있음 (recv 마다 갱신되는 timeout 만으로는 부족)",
              "deadline" in src("tts_rx"))

    print("\n=== 9. 구역 자동 판정 (--zone auto) ===")
    # 미니맵이 SLAM 지도를 면적으로 나눠 구역을 만들고, 로봇 위치로 현재 구역을
    # 알려줍니다. 파이프라인이 그걸 가져다 patrol_logs.zone 에 씁니다.
    # 양쪽 경로와 응답 키가 어긋나면 --zone auto 가 조용히 fallback 으로만 돕니다.
    check("미니맵이 /current_zone 을 제공", "/current_zone" in src("minimap"))
    check("  응답 키가 zone", '"zone": zone' in src("minimap")
          or "\"zone\": zone" in src("minimap"))
    check("  현재 구역 판정 함수가 있음", "def current_zone" in src("minimap"))
    check("파이프라인이 같은 경로를 부름", "/current_zone" in src("pipeline"))
    check("  응답에서 zone 키를 읽음", '.get("zone")' in src("pipeline"))
    check("  auto 가 아니면 기존처럼 고정 구역", '!= "auto"' in src("pipeline"))
    check("RAG 파이프라인도 같은 해석기를 씀",
          "resolve_zone_argument" in src("pipeline_rag"))
    # 젯슨 단독 실행은 psycopg2-binary 만 설치합니다. requests 를 최상단에서
    # import 하면 --zone A 로 쓰던 사람까지 ImportError 로 죽습니다.
    head = src("pipeline").split("def ")[0]
    check("  requests 를 최상단에서 import 하지 않음", "import requests" not in head)
    check("  미니맵 주소는 event_logger 와 같은 환경변수",
          "DAEUN_LAPTOP_IP" in src("pipeline") and "MINIMAP_PORT" in src("pipeline"))
    check("  토큰도 같은 변수", "MINIMAP_TOKEN" in src("pipeline"))

    print("\n=== 10. 대시보드 수동 방송 버튼 ===")
    # 관리자는 화면에서 상세 대응안을 확인하고, 현장에는 별도의 짧은 문장을
    # 낸다. 긴급 자동 방송은 LLM/RAG 완료를 기다리면 안 된다.
    dash = src("dashboard")
    check("대시보드에 짧은 현장 방송 버튼이 있음", "현장 짧은 안내 방송" in dash)
    check("  송신은 Rag_to_Jetson 의 공통 plan API를 재사용",
          "send_plan_to_jetson" in dash)
    # 주석에는 async 함수가 언급될 수 있으므로 import 문만 봅니다.
    check("  비동기가 아닌 동기 전송 (결과를 보여줘야 하므로)",
          "send_plan_to_jetson" in dash
          and "import send_alert_async" not in dash)
    check("  TTS 모듈 경로는 파이프라인과 같은 환경변수",
          "TTS_MODULE_DIR" in dash and "TTS_MODULE_DIR" in src("pipeline_rag"))
    check("  자동·수동 방송이 같은 짧은 현장 문장 builder를 씀",
          "build_speech_text" in dash
          and "send_detection_alert_async" in src("pipeline_rag")
          and "def build_speech_text" in src("tts_tx"))
    rag_pipeline = src("pipeline_rag")
    emergency_send_at = rag_pipeline.find("send_tts_alert(event)")
    event_write_at = rag_pipeline.find("if not writer.write(event)")
    check("  긴급 TTS는 LLM·대응안 저장보다 먼저 5회 등록",
          0 <= emergency_send_at < event_write_at
          and "EMERGENCY_TTS_REPEAT_COUNT = 5" in src("tts_tx"))
    check("  긴급 방송 문장은 관리자용 recommended_action을 읽지 않음",
          "관리자용 recommended_action은 읽지 않는다" in src("tts_tx"))
    check("송신 측 토큰 변수는 그대로 TTS_TOKEN",
          "TTS_TOKEN" in src("tts_tx") and "TTS_TOKEN" in src("tts_rx"))

    print("\n=== 11. 탐지 클래스가 모든 표시 경로에 등록됐는가 ===")
    # 젯슨 OUTPUT_LABELS 가 하류 계약의 정본입니다. 내부 탐지용
    # safety helmet은 전송하지 않아 이 목록에 넣지 않습니다.
    # 그 객체만 화면에서 사라지거나 영문 원문으로 뜹니다 — vehicle 이 미니맵에서
    # 그 상태였고, 아무도 눈치채지 못했습니다.
    import re as _re
    m = _re.search(r"OUTPUT_LABELS\s*=\s*\[(.*?)\]", src("sender"), _re.S)
    check("젯슨 출력 라벨을 읽을 수 있음", m is not None)
    classes = _re.findall(r'"([^"]+)"', m.group(1))
    check("  클래스 4개 (person 포함)", len(classes) == 4, str(classes))

    minimap_html = os.path.join(DAEUN, "dashboard_link", "minimap_web.html")
    web = io.open(minimap_html, encoding="utf-8").read() if os.path.exists(minimap_html) else ""
    check("미니맵 화면 파일을 찾음", bool(web))
    for cls in classes:
        check(f"  classify_risk 가 '{cls}' 를 앎",
              cls in src("schema"))
        check(f"  한글 이름표에 '{cls}' 가 있음",
              '"%s":' % cls in src("schema"))
        check(f"  미니맵 LABELS 에 '{cls}' 가 있음",
              'LABELS["%s"]' % cls in web)

    check("대시보드가 한글 이름표를 실제로 씀",
          "OBJECT_KO_NAME" in src("dashboard"))
    check("  표시 전용 — DB 조회는 영문 키 그대로",
          "detected_object" in src("dashboard"))
    # 가짜 송신기가 실제와 다른 라벨을 쓰면, 테스트만 통과하고 실물에서는
    # 그 코드 경로가 죽어 있는 상태를 못 잡습니다.
    fake = os.path.join(_PROJECT_ROOT, "self_test", "fake_jetson_sender.py")
    fake_src = io.open(fake, encoding="utf-8").read() if os.path.exists(fake) else ""
    check("가짜 송신기가 옛 라벨 'human' 을 쓰지 않음", '"human"' not in fake_src)

    print("\n=== 12. 겹친 탐지 중복 제거 ===")
    check("젯슨에 중복 제거 함수가 있음",
          "def suppress_overlapping_detections" in src("sender"))
    check("  추론 직후에 적용됨",
          "suppress_overlapping_detections(boxes, labels" in src("sender"))
    check("  IoU 로 판정", "def box_iou" in src("sender"))

    print("\n=== 13. Navigation command ownership and keepout filters ===")
    if "minimap" not in _MISSING:
        minimap_src = src("minimap")
        goal_pose_publisher = re.search(
            r"create_publisher\s*\([^)]*['\"]/?goal_pose['\"]",
            minimap_src,
            re.S,
        )
        check("minimap_renderer does not publish /goal_pose directly",
              goal_pose_publisher is None)
        check("minimap_renderer publishes commands through /patrol_control",
              re.search(
                  r"create_publisher\s*\([^)]*['\"]/patrol_control['\"]",
                  minimap_src,
                  re.S,
              ) is not None)
        check("minimap_renderer subscribes to /navigation_status acknowledgements",
              re.search(
                  r"create_subscription\s*\([^)]*['\"]/navigation_status['\"]",
                  minimap_src,
                  re.S,
              ) is not None)

    if "nav2_params" not in _MISSING:
        nav2_params = src("nav2_params")
        check("local/global costmaps each register the keepout filter",
              nav2_params.count('filters: ["keepout_filter"]') == 2)
        check("local/global costmaps each use nav2 KeepoutFilter",
              nav2_params.count(
                  'plugin: "nav2_costmap_2d::KeepoutFilter"'
              ) == 2)
        check("both keepout filters consume /costmap_filter_info",
              nav2_params.count(
                  'filter_info_topic: "/costmap_filter_info"'
              ) == 2)

    print("\n모든 테스트 통과!")


if __name__ == "__main__":
    main()
