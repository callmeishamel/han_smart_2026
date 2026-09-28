r"""실물 관제 스택의 낮은 장애물 배선과 순찰 정지 UI 정책을 정적으로 검증한다.

ROS2/Streamlit을 import하지 않고 소스 계약만 검사하므로 개발 PC에서도 실행된다.

실행 (저장소 루트):
    python "데이터 플랫폼 및 대시보드(이상민)/self_test/test_real_navigation_safety.py"
"""

import ast
import io
import os
import sys


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(PROJECT_ROOT)
ROS_ROOT = os.path.join(REPO_ROOT, "ROS2_자율주행_및_연동(이다은)")

if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from common.console import enable_utf8_console  # noqa: E402
enable_utf8_console()

LAUNCH_PATH = os.path.join(ROS_ROOT, "launch", "real_navigation.launch.py")
RUNNER_PATH = os.path.join(ROS_ROOT, "run_real_dashboard_stack.sh")
MAPPING_RUNNER_PATH = os.path.join(ROS_ROOT, "run_real_autonomous_mapping.sh")
NAV2_PARAMS_PATH = os.path.join(ROS_ROOT, "config", "nav2_params.yaml")
UI_PATH = os.path.join(PROJECT_ROOT, "edge_video", "dashboard_video_minimap_section.py")


def read(path):
    with io.open(path, encoding="utf-8") as handle:
        return handle.read()


def function_source(source, name):
    tree = ast.parse(source)
    node = next(
        item for item in tree.body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == name
    )
    lines = source.splitlines()
    return "\n".join(lines[node.lineno - 1:node.end_lineno]), node


def check(description, condition, detail=""):
    print(f"[{'OK ' if condition else 'FAIL'}] {description}")
    if not condition:
        suffix = f" — {detail}" if detail else ""
        raise AssertionError(description + suffix)


def main():
    launch = read(LAUNCH_PATH)
    runner = read(RUNNER_PATH)
    mapping_runner = read(MAPPING_RUNNER_PATH)
    nav2_params = read(NAV2_PARAMS_PATH)
    ui = read(UI_PATH)

    print("=== 1. 매핑 기록과 실물 launch의 경로 계약 ===")
    check(
        "매핑 단계가 MAP_OUTPUT 옆에 낮은 장애물 기록을 저장",
        '${MAP_OUTPUT}.blind_obstacles.json' in mapping_runner,
    )
    check(
        "자동 매핑 저장도 미탐사를 보존하는 free threshold 사용",
        '-f "$MAP_OUTPUT" --free 0.196' in mapping_runner,
    )
    check(
        "저장 지도 실행기는 .yaml을 뺀 같은 basename을 사용",
        '${MAP_FILE%.yaml}.blind_obstacles.json' in runner,
    )
    check(
        "실행기가 blind_obstacle_store launch 인자를 전달",
        '"blind_obstacle_store:=$BLIND_OBSTACLE_STORE"' in runner,
    )
    check(
        "중복 실행 검사에 stall_monitor 포함",
        "stall_monitor" in runner.split("ros2 node list", 2)[-1],
    )
    check(
        "중복 실행 검사가 daemon 캐시가 아닌 현재 DDS 그래프를 조회",
        "ros2 node list --no-daemon --spin-time 2" in runner,
    )
    check(
        "터미널 창 종료(SIGHUP)도 관제 cleanup을 실행",
        "trap cleanup EXIT INT TERM HUP" in runner,
    )
    check(
        "Nav2 그룹 리더뿐 아니라 남은 launch 자식까지 종료 확인",
        "wait_for_group_exit" in runner
        and 'kill -0 -- "-$group_id"' in runner,
    )

    print("\n=== 2. real_navigation launch의 stall_monitor 배선 ===")
    check(
        "blind_obstacle_store launch 인자 선언",
        "DeclareLaunchArgument(\n            'blind_obstacle_store'" in launch,
    )
    check(
        "stall_monitor 노드 실행",
        "executable='stall_monitor'" in launch and "name='stall_monitor'" in launch,
    )
    check(
        "launch 인자를 store_path 문자열 파라미터로 전달",
        "'store_path': ParameterValue(blind_obstacle_store, value_type=str)" in launch,
    )
    check(
        "Nav2 두 costmap 모두 KeepoutFilter 활성",
        nav2_params.count('filters: ["keepout_filter"]') >= 2
        and nav2_params.count('plugin: "nav2_costmap_2d::KeepoutFilter"') >= 2,
    )
    check(
        "launch 로그가 장애물 기록 경로와 필터 활성 상태를 알림",
        "LogInfo" in launch and "KeepoutFilter 활성" in launch,
    )

    print("\n=== 3. LiDAR 시각과 Nav2 회전 복구 안전 ===")
    check(
        "실물 Nav2 launch가 stale scan 교정 중계기를 실행",
        "name='nav_scan_relay'" in launch and "'restamp_stale': True" in launch,
    )
    check(
        "AMCL과 두 costmap이 교정된 /scan_nav만 사용",
        'scan_topic: "scan_nav"' in nav2_params
        and nav2_params.count('topic: /scan_nav') == 2,
    )
    check(
        "제자리 방향 정렬을 복구 spin 실패로 조기 판정하지 않음",
        "required_movement_radius: 0.15" in nav2_params
        and "movement_time_allowance: 20.0" in nav2_params,
    )
    check(
        "실물 Burger DWB가 잘못된 min_vel_theta 대신 속도 크기를 사용",
        "min_vel_theta:" not in nav2_params
        and "min_speed_theta: 0.0" in nav2_params,
    )
    check(
        "실행 안내가 RViz 공용 LiDAR mesh와 실제 드라이버를 구분",
        "공용 lds.stl" in runner and "EXPECTED_LDS_MODEL" in runner,
    )
    check(
        "Nav2 시작 전 실제 /scan 수신을 검증",
        "ros2 topic echo /scan sensor_msgs/msg/LaserScan --once" in runner,
    )

    print("\n=== 4. 관제 실행기의 RAG·LLM·TTS 배선 ===")
    check(
        "관제 실행기가 RAG 수신 파이프라인을 함께 시작",
        "./run_pipeline_rag.sh" in runner and "rag_pid=$!" in runner,
    )
    check(
        "LLM은 UDP 수신을 막지 않는 비동기 모드",
        "--use-llm --async-llm" in runner,
    )
    check(
        "현재 로봇 구역을 RAG 이벤트 구역으로 사용",
        'SFP_PATROL_ZONE="${SFP_PATROL_ZONE:-auto}"' in runner,
    )
    check(
        "TTS 수신기가 없으면 Jetson 실행 명령을 경고",
        "python3 tts/Rx_pipeline.py" in runner and "TTS_TCP_PORT" in runner,
    )
    check(
        "종료 시 RAG 프로세스도 함께 정리",
        'stop_pid_bounded "$rag_pid"' in runner
        and 'kill -KILL "$rag_pid"' in runner,
    )

    print("\n=== 5. Streamlit 순찰 정지 정책 ===")
    patrol_source, patrol_node = function_source(ui, "render_patrol_control")
    check(
        "확인 대기 명령은 start/resume으로 제한",
        'pending not in ("start", "resume")' in patrol_source,
    )
    check(
        "stop은 확인 session_state에 넣지 않음",
        'patrol_confirm="stop"' not in patrol_source
        and "patrol_confirm='stop'" not in patrol_source,
    )
    check(
        "정지 클릭 런에서 _post_patrol을 즉시 호출",
        '_post_patrol(patrol_url, "stop")' in patrol_source,
    )
    check(
        "Nav2 취소와 하드웨어 비상정지의 차이를 표시",
        "Nav2 목표 취소이며 하드웨어 비상정지가 아닙니다" in patrol_source,
    )

    stop_buttons = []
    for node in ast.walk(patrol_node):
        if not isinstance(node, ast.Call):
            continue
        if not (isinstance(node.func, ast.Attribute) and node.func.attr == "button"):
            continue
        if not node.args or not isinstance(node.args[0], ast.Constant):
            continue
        if "정지" in str(node.args[0].value):
            stop_buttons.append(node)
    check("정지 버튼이 존재", bool(stop_buttons))
    check(
        "모든 정지 버튼에 확인 콜백이 없음",
        all(keyword.arg != "on_click" for button in stop_buttons for keyword in button.keywords),
    )

    # 마지막으로 편집 과정에서 문법이 깨지지 않았는지 확인한다.
    compile(launch, LAUNCH_PATH, "exec")
    compile(ui, UI_PATH, "exec")
    print("\n모든 실물 관제 안전 배선 검사를 통과했습니다.")


if __name__ == "__main__":
    main()
