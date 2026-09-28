r"""
스마트 팩토리 DB 관리 CLI (admin/manage_smart_factory_db.py) — v2

v1 대비 변경 사항 (원본 파일에서 발견된 문제 수정)
----------------------------------------------------
1. 스키마 불일치 수정
   - 원본 v1은 patrol_zones 테이블을 zone_name/risk_level/last_patrol_time/
     status/robot_id 컬럼으로 가정했지만, 대시보드/파이프라인이 실제로
     쓰는 patrol_zones는 zone(PK)/robot_id/description 컬럼입니다.
     v1 그대로 두면 이 CLI로 넣은 데이터가 대시보드에 전혀 반영되지 않는
     치명적인 버그가 있었습니다. v2는 common/schema.py의 실제 스키마를
     그대로 사용합니다.
2. 비밀번호 하드코딩 제거
   - DB 접속 정보를 common.schema.get_db_config()(환경변수 기반)로
     통일했습니다.
3. 기능 확장
   - 구역(zone) CRUD 뿐 아니라, 실제 운영에서 더 자주 쓰게 될 "최근 로그
     조회" 메뉴를 추가했습니다.

사용 전 준비 (Windows PowerShell 기준 — 이 파일은 상민님 PC에서 실행합니다)
------------
    copy set_env.example.ps1 set_env.ps1   (최초 1회, 이미 했다면 생략)
    . .\set_env.ps1
    python admin\manage_smart_factory_db.py
"""

import os
import sys

# common 패키지를 찾을 수 있도록 프로젝트 루트를 sys.path에 추가
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# Windows 기본 콘솔(cp949)에서 이모지/em대시를 출력하다 죽는 것을 막습니다.
from common.console import enable_utf8_console  # noqa: E402
enable_utf8_console()

from common.schema import (  # noqa: E402
    RESET_TABLES,
    get_db_config,
    init_all_tables,
    purge_old_logs,
    reset_operational_data,
)


def get_connection():
    import psycopg2
    return psycopg2.connect(**get_db_config())


def ensure_schema():
    """테이블이 없으면 생성 (대시보드/파이프라인과 동일한 스키마)."""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        try:
            init_all_tables(cursor)
            conn.commit()
        finally:
            cursor.close()
    finally:
        conn.close()


def read_zones():
    """현재 등록된 구역 목록을 출력합니다."""
    conn = None
    cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT zone, robot_id, description FROM patrol_zones ORDER BY zone;")
        rows = cursor.fetchall()

        print("\n=== [현재 등록된 구역 목록] ===")
        if not rows:
            print("데이터가 없습니다.")
        else:
            for zone, robot_id, description in rows:
                print(f"[{zone}] 담당 로봇: {robot_id or '-'} | 설명: {description or '-'}")
        print("================================")
    except Exception as e:
        print(f"\n[오류] 구역 조회 실패: {e}")
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def insert_zone():
    """새 구역을 patrol_zones에 추가합니다."""
    print("\n--- [새로운 구역 추가] ---")
    zone = input("구역 코드 (예: D): ").strip().upper()
    robot_id = input("담당 로봇 ID (예: ROBO-D01): ").strip()
    description = input("구역 설명 (예: D동 조립라인): ").strip()

    if not zone:
        print("\n[오류] 구역 코드는 비워둘 수 없습니다.")
        return

    conn = None
    cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO patrol_zones (zone, robot_id, description) VALUES (%s, %s, %s);",
            (zone, robot_id or None, description or None),
        )
        conn.commit()
        print(f"\n[성공] 구역 '{zone}'이(가) 추가되었습니다.")
    except Exception as e:
        # UniqueViolation 등 psycopg2 예외를 이름으로 정확히 잡지 않고
        # 메시지로 안내하는 이유: psycopg2 미설치 환경에서도 이 파일이
        # import 시점에 깨지지 않도록 하기 위함입니다 (get_connection() 안에서만 import).
        print(f"\n[오류] 구역 추가 실패 (이미 존재하는 구역 코드일 수 있습니다): {e}")
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def delete_zone():
    """구역을 삭제합니다. 해당 구역의 로그가 있으면 FK 제약으로 실패할 수 있습니다."""
    print("\n--- [구역 삭제] ---")
    read_zones()

    zone = input("\n삭제할 구역 코드를 입력하세요 (취소하려면 Enter): ").strip().upper()
    if not zone:
        print("삭제를 취소했습니다.")
        return

    conn = None
    cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM patrol_zones WHERE zone = %s;", (zone,))
        conn.commit()

        if cursor.rowcount > 0:
            print(f"\n[성공] 구역 '{zone}'이(가) 삭제되었습니다.")
        else:
            print(f"\n[실패] 구역 '{zone}'을(를) 찾을 수 없습니다.")
    except Exception as e:
        print(
            f"\n[오류] 구역 삭제 실패: {e}\n"
            "해당 구역에 연결된 patrol_logs 기록이 남아있으면 외래키 제약으로 삭제가 거부됩니다. "
            "먼저 관련 로그를 정리하거나, 이 구역을 참조하는 로그가 없는지 확인하세요."
        )
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def read_recent_logs(limit: int = 20):
    """최근 감지 로그를 조회합니다."""
    conn = None
    cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT log_time, zone, detected_object, distance, risk_level, issue
            FROM patrol_logs
            ORDER BY log_time DESC
            LIMIT %s;
            """,
            (limit,),
        )
        rows = cursor.fetchall()

        print(f"\n=== [최근 로그 {limit}건] ===")
        if not rows:
            print("데이터가 없습니다.")
        else:
            for log_time, zone, detected_object, distance, risk_level, issue in rows:
                time_str = log_time.strftime("%Y-%m-%d %H:%M:%S") if log_time else "기록없음"
                dist_str = f"{distance}m" if distance is not None and distance >= 0 else "측정불가"
                print(f"[{time_str}] {zone}구역 | {detected_object} | 거리: {dist_str} | "
                      f"위험도: {risk_level} | {issue or '-'}")
        print("========================")
    except Exception as e:
        print(f"\n[오류] 로그 조회 실패: {e}")
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def reset_demo_data():
    """순찰 기록을 전부 비웁니다 (구역 정의와 RAG 지식베이스는 남습니다).

    "프로그램 켤 때마다 DB를 초기화하면 되지 않나"에 대한 대안입니다.
    자동으로 비우면 나중에 뜬 프로세스가 먼저 뜬 프로세스의 기록을 지우고,
    파이프라인이 DB에 재연결할 때마다 로그가 사라지므로 여기서 사람이
    명시적으로 부를 때만 실행합니다.
    """
    print("\n=== [순찰 기록 초기화] ===")
    print("아래 테이블의 데이터를 모두 삭제합니다 (되돌릴 수 없습니다).")
    for table in RESET_TABLES:
        print(f"  - {table}")
    print("구역 정의(patrol_zones)와 RAG 지식베이스는 그대로 유지됩니다.")

    answer = input("정말 진행하려면 '초기화' 를 그대로 입력하세요: ").strip()
    if answer != "초기화":
        print("취소했습니다. 아무것도 지우지 않았습니다.")
        return

    conn = None
    cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor()
        reset_operational_data(cursor)
        conn.commit()
        print("초기화 완료. id 시퀀스도 1번부터 다시 시작합니다.")
    except Exception as e:
        print(f"\n[오류] 초기화 실패: {e}")
        if conn:
            conn.rollback()
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def purge_logs():
    """지정한 일수보다 오래된 기록만 삭제합니다 (전체 초기화 대신 쓰는 보존 정책)."""
    print("\n=== [오래된 기록 정리] ===")
    print("젯슨이 30fps로 적재하므로 patrol_logs 는 하루에도 크게 늘어납니다.")
    raw = input("며칠 이전 기록을 지울까요? (기본 7, 0이면 전부): ").strip()

    try:
        days = int(raw) if raw else 7
        if days < 0:
            raise ValueError
    except ValueError:
        print("[오류] 0 이상의 정수를 입력하세요.")
        return

    conn = None
    cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor()
        deleted = purge_old_logs(cursor, days)
        conn.commit()
        print(f"\n{days}일 이전 기록 삭제 완료")
        for table, count in deleted.items():
            print(f"  - {table}: {count}건")
    except Exception as e:
        print(f"\n[오류] 정리 실패: {e}")
        if conn:
            conn.rollback()
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def main_menu():
    """터미널 인터페이스 메뉴를 제공합니다."""
    try:
        ensure_schema()
    except Exception as e:
        print(f"\n[경고] 스키마 확인/생성 중 오류가 발생했습니다: {e}")
        print("DB 접속 정보(SFP_DB_* 환경변수)를 확인하세요. 메뉴는 계속 진행됩니다.\n")

    while True:
        print("\n" + "=" * 45)
        print(" 🤖 스마트 팩토리 순찰 로봇 DB 관리 프로그램")
        print("=" * 45)
        print(" 1. 구역 목록 조회")
        print(" 2. 새 구역 추가")
        print(" 3. 구역 삭제")
        print(" 4. 최근 감지 로그 조회")
        print(" 5. 순찰 기록 전체 초기화  (시연 전 정리)")
        print(" 6. 오래된 기록만 정리    (보존 기간 지정)")
        print(" 7. 프로그램 종료")
        print("=" * 45)

        choice = input("원하시는 작업의 번호를 입력하세요 (1~7): ").strip()

        if choice == "1":
            read_zones()
        elif choice == "2":
            insert_zone()
        elif choice == "3":
            delete_zone()
        elif choice == "4":
            read_recent_logs()
        elif choice == "5":
            reset_demo_data()
        elif choice == "6":
            purge_logs()
        elif choice == "7":
            print("\n프로그램을 종료합니다. 감사합니다!")
            break
        else:
            print("\n[오류] 잘못된 입력입니다. 1에서 7 사이의 숫자를 입력해주세요.")


if __name__ == "__main__":
    main_menu()