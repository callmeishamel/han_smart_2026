r"""
event_logger.py

실행 위치: 이상민님 PC (PostgreSQL이 설치된 바로 그 컴퓨터)

역할
----
1. 다은님 노트북의 /detections 엔드포인트를 주기적으로 폴링
2. min_hits를 통과해서 "확정"된 탐지 중, 아직 DB에 기록 안 한 새 tracker_id만 골라서
3. detection_events 테이블에 이벤트로 INSERT

지금까지 만든 카메라 스트림/미니맵은 전부 "현재 화면"만 보여주는 실시간 스트림이고,
이 스크립트가 "무슨 일이 언제 있었는지"를 기록으로 남기는 부분을 담당합니다.
(patrol_logs가 "손준영 팀장 쪽 UDP 탐지"를 기록한다면, detection_events는
"이다은님 쪽 SLAM 기반 위치 추정 탐지"를 기록하는 역할로 분리되어 있습니다.)

실행 전 준비
------------
    pip install requests psycopg2-binary

DB에 아래 테이블이 미리 있어야 합니다 (admin/manage_smart_factory_db.py로
만들거나, common.schema의 init_all_tables()가 자동으로 만들어줍니다):

    CREATE TABLE IF NOT EXISTS detection_events (
        id SERIAL PRIMARY KEY,
        tracker_id INTEGER NOT NULL,
        label TEXT NOT NULL,
        map_x DOUBLE PRECISION NOT NULL,
        map_y DOUBLE PRECISION NOT NULL,
        hit_count INTEGER NOT NULL,
        detected_at TIMESTAMPTZ NOT NULL,
        logged_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );

실행 방법 (Windows PowerShell 기준 — 이 파일은 상민님 PC에서 실행합니다):
    copy set_env.example.ps1 set_env.ps1   (최초 1회, 이미 했다면 생략)
    . .\set_env.ps1
    $env:DAEUN_LAPTOP_IP = "192.168.0.xxx"   (다은님 노트북의 실제 IP)
    python edge_video\event_logger.py
"""

import os
import sys
import time

import requests
import psycopg2

# common 패키지를 찾을 수 있도록 프로젝트 루트를 sys.path에 추가
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# Windows 기본 콘솔(cp949)에서 이모지/em대시를 출력하다 죽는 것을 막습니다.
from common.console import enable_utf8_console  # noqa: E402
enable_utf8_console()

from common.schema import (  # noqa: E402
    DETECTION_EVENTS_TABLE_DDL,
    INSERT_DETECTION_EVENT_SQL,
    MIGRATE_DETECTION_EVENTS_SESSION_DDL,
    UNIQUE_INDEX_DDL,
    detection_event_params,
    get_db_config,
)

DAEUN_LAPTOP_IP = os.environ.get("DAEUN_LAPTOP_IP", "203.0.113.20")
MINIMAP_PORT = os.environ.get("MINIMAP_PORT", "8091")
DETECTIONS_URL = f"http://{DAEUN_LAPTOP_IP}:{MINIMAP_PORT}/detections"

# 미니맵 서버(minimap_renderer.py)에서 MINIMAP_TOKEN 을 켰다면 같은 값이 필요합니다.
#
# 이 로거는 대시보드와 **같은 서버를 부르는 두 번째 클라이언트**입니다. 예전에는
# 대시보드(dashboard_video_minimap_section.py)만 토큰을 붙이고 여기는 안 붙였는데,
# 미니맵의 @app.before_request 는 토큰이 없는 요청을 전부 403 으로 막습니다.
# 그래서 토큰을 켜는 순간 detection_events 적재만 조용히 멈췄습니다 —
# 영상도 미니맵 화면도 멀쩡히 보이므로, 기록이 사라지는 걸 눈치채기 어렵습니다.
MINIMAP_TOKEN = os.environ.get("MINIMAP_TOKEN", "").strip()

# 쿼리스트링(?t=) 대신 헤더로 보냅니다. 토큰이 서버 접근 로그나 프록시 로그에
# 그대로 남지 않게 하기 위함입니다. 미니맵은 둘 다 받습니다.
REQUEST_HEADERS = {"X-Minimap-Token": MINIMAP_TOKEN} if MINIMAP_TOKEN else {}

POLL_INTERVAL_SEC = float(os.environ.get("POLL_INTERVAL_SEC", "2.0"))

# seen_tracker_ids 캐시 상한. 장시간 돌리면 무한히 커지므로 넘으면 절반을 버립니다.
MAX_SEEN_IDS = int(os.environ.get("MAX_SEEN_IDS", "10000"))
MAX_RECONNECT_BACKOFF_SEC = 30


def get_connection():
    return psycopg2.connect(**get_db_config())


def reconnect(old_conn):
    """DB 연결이 끊겼을 때 지수 백오프로 다시 연결합니다.

    이 스크립트는 며칠씩 돌아가는 상주 프로세스라, DB 재시작이나 네트워크
    순단으로 죽어버리면 그동안의 탐지 기록이 통째로 비게 됩니다.
    """
    try:
        old_conn.close()
    except Exception:
        pass

    backoff = 1
    while True:
        try:
            conn = get_connection()
            ensure_table(conn)
            print("DB 재연결 성공")
            return conn
        except psycopg2.Error as e:
            print(f"DB 재연결 실패 ({e}). {backoff}초 후 재시도합니다.")
            time.sleep(backoff)
            backoff = min(backoff * 2, MAX_RECONNECT_BACKOFF_SEC)


def ensure_table(conn):
    """detection_events 테이블 DDL은 common/schema.py 한 곳에만 정의되어 있습니다
    (여기서 따로 정의하면, 다른 컴포넌트가 만든 스키마와 어긋날 위험이 있기 때문).

    중복 방지 유니크 인덱스도 여기서 확보합니다. 이 로거가 다른 진입점보다
    먼저 뜨는 경우가 많아, init_all_tables() 를 기다릴 수 없기 때문입니다.
    이미 중복 행이 있으면 인덱스 생성이 실패하는데, 그때는 경고만 남기고
    계속 진행합니다 (적재를 멈추는 것보다 낫습니다).
    """
    cur = conn.cursor()
    try:
        cur.execute(DETECTION_EVENTS_TABLE_DDL)
        cur.execute(MIGRATE_DETECTION_EVENTS_SESSION_DDL)
        conn.commit()
    finally:
        cur.close()

    cur = conn.cursor()
    try:
        cur.execute(UNIQUE_INDEX_DDL)
        conn.commit()
    except psycopg2.Error as e:
        conn.rollback()
        print(f"[주의] 중복 방지 인덱스를 만들지 못했습니다: {e}")
        print("       같은 session_id 로 중복된 행이 이미 있다는 뜻입니다.")
    finally:
        cur.close()


def log_new_event(conn, session_id: str, obj: dict):
    """탐지 하나를 이벤트로 기록하고 (커넥션, 실제로 들어갔는지)를 돌려줍니다.

    SQL은 common/schema.py 한 곳에만 있습니다. 그 안의 ON CONFLICT DO NOTHING
    덕분에 같은 (session_id, tracker_id)를 두 번 넣어도 행이 늘지 않습니다 —
    즉 아래 메모리 캐시가 틀려도 중복 행이 생기지 않습니다.

    실패하면 rollback 해서 커넥션이 "aborted transaction" 상태로 남지 않게
    합니다. 그 상태로 두면 이후 모든 쿼리가 거부됩니다.
    """
    cur = conn.cursor()
    try:
        cur.execute(INSERT_DETECTION_EVENT_SQL, detection_event_params(session_id, obj))
        inserted = cur.rowcount > 0      # 0 이면 이미 있던 행 (충돌로 무시됨)
        conn.commit()
        return conn, inserted
    except psycopg2.Error:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        cur.close()


def main():
    conn = get_connection()
    ensure_table(conn)

    # 이미 적재한 (session_id, tracker_id).
    #
    # **이건 중복 방지 장치가 아니라 DB 왕복을 아끼는 캐시일 뿐입니다.**
    # 중복 방지는 uq_detection_events_session_tracker 유니크 인덱스와
    # ON CONFLICT DO NOTHING 이 담당합니다. 예전에는 이 집합이 유일한 방어선이라
    # 세 가지 상황에서 조용히 틀렸습니다.
    #
    #   1. 이 로거를 재시작하면 집합이 비어서, 미니맵이 아직 들고 있는 물체를
    #      전부 다시 적재했습니다 (중복 행).
    #   2. 미니맵이 재시작하면 tracker id 가 1부터 다시 시작하는데, 이 집합에는
    #      옛 1번이 남아 있어서 **새로 잡힌 물체를 통째로 버렸습니다** (기록 누락).
    #      중복보다 이쪽이 위험합니다 — 안전 기록이 사라지는 것이므로.
    #   3. MAX_SEEN_IDS 를 넘겨 오래된 절반을 버리면, 아직 살아 있는 물체가
    #      다시 적재됐습니다 (중복 행).
    #
    # 이제 2번은 session_id 로 해결되고(미니맵이 뜰 때마다 새 값), 1·3번은
    # 캐시가 틀려도 DB가 거부하므로 행이 늘지 않습니다.
    seen = set()
    warned_no_session = False

    print(f"이벤트 로거 시작 - {DETECTIONS_URL} 폴링 중 (매 {POLL_INTERVAL_SEC}초)")
    print(f"미니맵 토큰: {'사용' if MINIMAP_TOKEN else '없음'}")

    while True:
        try:
            resp = requests.get(DETECTIONS_URL, timeout=2.0, headers=REQUEST_HEADERS)
            resp.raise_for_status()
            payload = resp.json()
            detections = payload["detections"]
            session_id = str(payload.get("session", "") or "")
        except (requests.RequestException, KeyError, ValueError) as e:
            print(f"폴링 실패, {POLL_INTERVAL_SEC}초 후 재시도: {e}")
            # 403 은 네트워크 문제가 아니라 설정 불일치입니다. 그냥 "폴링 실패"만
            # 반복하면 원인을 찾느라 시간을 다 씁니다.
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status == 403:
                print("  → 미니맵이 토큰을 요구하고 있습니다. "
                      "미니맵 서버와 **같은 값**으로 MINIMAP_TOKEN 을 설정하세요.")
            time.sleep(POLL_INTERVAL_SEC)
            continue

        if not session_id and not warned_no_session:
            warned_no_session = True
            print("[주의] 미니맵이 session 을 보내지 않습니다 (구버전). "
                  "중복 방지가 예전 수준(메모리 캐시)으로 떨어집니다.")

        for obj in detections:
            try:
                key = (session_id, obj["id"])
                if key in seen:
                    continue
                # DB가 끊겨 있으면 여기서 예외가 납니다. 예전에는 이 예외가
                # main() 밖으로 빠져나가 프로세스가 그대로 죽었습니다
                # (try/except가 HTTP 요청만 감싸고 있었음).
                conn, inserted = log_new_event(conn, session_id, obj)
                seen.add(key)
                if inserted:
                    print(f"이벤트 기록: {obj['label']} #{obj['id']} "
                          f"@ ({obj['x']:.2f}, {obj['y']:.2f})")
            except KeyError as e:
                print(f"탐지 항목 형식이 예상과 다릅니다 (건너뜀): {e}")
            except psycopg2.Error as e:
                print(f"DB 적재 실패, 다음 주기에 재시도: {e}")
                conn = reconnect(conn)
                break  # 이번 주기는 여기서 중단하고 다음 폴링에서 이어감

        # 캐시가 무한히 커지지 않게 잘라냅니다. 잘라서 다시 적재를 시도하더라도
        # DB가 충돌로 걸러내므로 중복 행은 생기지 않습니다.
        if len(seen) > MAX_SEEN_IDS:
            seen = set(sorted(seen)[len(seen) // 2:])

        time.sleep(POLL_INTERVAL_SEC)


if __name__ == '__main__':
    main()