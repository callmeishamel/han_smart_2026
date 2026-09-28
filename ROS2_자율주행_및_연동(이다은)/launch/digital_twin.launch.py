"""디지털 트윈(랙 6개 공장) + Nav2 자율주행 전체 스택.

이 파일이 순찰용 기본 launch 다.

예전에는 이 launch 가 Gazebo 와 로봇 스폰만 했고 Nav2 는 simulation.launch.py
에만 있었다. 그런데 patrol_node 의 웨이포인트는 이 월드(digital_twin_1.world)의
랙 사이 통로에 맞춰져 있어서, "웨이포인트가 맞는 월드에는 자율주행 스택이 없고,
자율주행 스택이 있는 월드에는 웨이포인트가 안 맞는" 상태였다. 이제 셋을 맞췄다:

    월드   worlds/digital_twin_1.world      (벽 12x8m, 랙 6개)
    지도   maps/digital_twin_map.yaml       (위 월드에서 generate_map.py 로 생성)
    경로   patrol_node 의 기본 웨이포인트   (각 지점 여유 1.05~1.35m)

스폰 좌표 (0.5, 3.5) 는 nav2_params.yaml 의 amcl.initial_pose 및
initial_pose_pub 의 x/y 파라미터와 같은 값이어야 한다.

실행
----
    ros2 launch smart_factory_sim digital_twin.launch.py
    ros2 launch smart_factory_sim digital_twin.launch.py patrol:=true
    ros2 launch smart_factory_sim digital_twin.launch.py use_nav2:=false gui:=false
"""

import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import (DeclareLaunchArgument, IncludeLaunchDescription,
                            SetEnvironmentVariable, TimerAction)
from launch.conditions import IfCondition
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node

# 로봇 시작 위치 — 아래 세 곳이 반드시 같아야 한다:
#   여기(spawn_entity), nav2_params.yaml 의 amcl.initial_pose, initial_pose_pub 파라미터
START_X = 0.5
START_Y = 3.5
START_YAW = 0.0


def generate_launch_description():
    pkg = get_package_share_directory('smart_factory_sim')
    gazebo_ros_dir = get_package_share_directory('gazebo_ros')

    world_path = os.path.join(pkg, 'worlds', 'digital_twin_1.world')
    urdf_path = os.path.join(pkg, 'urdf', 'turtlebot3_burger_camera.urdf')
    sdf_path = os.path.join(pkg, 'models', 'turtlebot3_burger_camera', 'model.sdf')
    map_path = os.path.join(pkg, 'maps', 'digital_twin_map.yaml')
    nav2_params = os.path.join(pkg, 'config', 'nav2_params.yaml')
    rviz_config = os.path.join(pkg, 'config', 'rviz_config.rviz')

    with open(urdf_path, 'r') as f:
        robot_desc = f.read()

    use_nav2 = LaunchConfiguration('use_nav2')
    use_rviz = LaunchConfiguration('use_rviz')
    patrol = LaunchConfiguration('patrol')
    mock_udp_host = LaunchConfiguration('mock_udp_host')
    mock_udp_port = LaunchConfiguration('mock_udp_port')

    args = [
        DeclareLaunchArgument('gui', default_value='true',
                              description='Gazebo GUI 표시 여부'),
        DeclareLaunchArgument('use_nav2', default_value='true',
                              description='Nav2 자율주행 스택 실행 여부'),
        DeclareLaunchArgument('use_rviz', default_value='true',
                              description='RViz 실행 여부'),
        DeclareLaunchArgument('patrol', default_value='false',
                              description='Nav2 기동 후 순찰 노드 자동 시작 여부'),
        DeclareLaunchArgument('mock_udp_host', default_value='127.0.0.1',
                              description='Gazebo mock 이벤트를 받을 데이터/RAG 파이프라인 IP'),
        DeclareLaunchArgument('mock_udp_port', default_value='9998',
                              description='Gazebo mock 이벤트를 받을 데이터/RAG 파이프라인 UDP 포트'),
    ]

    # Gazebo 가 커스텀 모델(fire_event 등)을 찾을 수 있도록 경로 추가
    models_path = os.path.join(pkg, 'models')
    existing = os.environ.get('GAZEBO_MODEL_PATH', '')
    env = [
        SetEnvironmentVariable(
            'GAZEBO_MODEL_PATH',
            f"{models_path}:{existing}" if existing else models_path),
        SetEnvironmentVariable('TURTLEBOT3_MODEL', 'burger'),
    ]

    gazebo = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            os.path.join(gazebo_ros_dir, 'launch', 'gazebo.launch.py')),
        launch_arguments={'world': world_path,
                          'gui': LaunchConfiguration('gui')}.items(),
    )

    robot_state_publisher = Node(
        package='robot_state_publisher',
        executable='robot_state_publisher',
        name='robot_state_publisher',
        output='screen',
        parameters=[{'use_sim_time': True, 'robot_description': robot_desc}],
    )

    spawn_entity = Node(
        package='gazebo_ros',
        executable='spawn_entity.py',
        name='spawn_entity',
        output='screen',
        arguments=['-entity', 'turtlebot3_burger', '-file', sdf_path,
                   '-x', str(START_X), '-y', str(START_Y), '-z', '0.01'],
    )

    # nav2_params.yaml의 AMCL/costmap 입력은 /scan_nav다. Gazebo는 /scan만
    # 발행하므로 relay가 없으면 위치 추정이 영원히 시작되지 않는다.
    scan_qos_relay = Node(
        package='smart_factory_sim',
        executable='scan_qos_relay',
        name='scan_qos_relay',
        condition=IfCondition(use_nav2),
        parameters=[{
            'use_sim_time': True,
            'input_topic': '/scan',
            'output_topic': '/scan_nav',
            'queue_depth': 10,
            'restamp_stale': False,
        }],
        output='screen',
    )

    # 공통 Nav2 설정의 KeepoutFilter가 사용할 마스크/메타데이터를 발행한다.
    # 이 노드가 없으면 필터가 실제로 적용되지 않고 경고만 반복된다.
    stall_monitor = Node(
        package='smart_factory_sim',
        executable='stall_monitor',
        name='stall_monitor',
        condition=IfCondition(use_nav2),
        parameters=[{
            'use_sim_time': True,
            'store_path': '/tmp/smart_factory_digital_twin_blind_obstacles.json',
        }],
        output='screen',
    )

    nav2 = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            os.path.join(get_package_share_directory('nav2_bringup'),
                         'launch', 'bringup_launch.py')),
        condition=IfCondition(use_nav2),
        launch_arguments={'map': map_path,
                          'use_sim_time': 'True',
                          'params_file': nav2_params,
                          'autostart': 'True'}.items(),
    )

    rviz = Node(
        package='rviz2',
        executable='rviz2',
        name='rviz2',
        condition=IfCondition(use_rviz),
        arguments=['-d', rviz_config],
        parameters=[{'use_sim_time': True}],
        output='screen',
    )

    # AMCL 이 구독을 열 때까지 반복 발행하고, /amcl_pose 로 확인되면 멈춘다.
    initial_pose_pub = Node(
        package='smart_factory_sim',
        executable='initial_pose_pub',
        name='initial_pose_pub',
        condition=IfCondition(use_nav2),
        parameters=[{'use_sim_time': True,
                     'x': START_X, 'y': START_Y, 'yaw': START_YAW}],
        output='screen',
    )

    event_detector = Node(
        package='smart_factory_sim',
        executable='event_detector',
        name='event_detector',
        parameters=[{'use_sim_time': True,
                     'use_mock': True,
                     'camera_frame': 'camera_rgb_optical_frame',
                     'map_frame': 'map'}],
        output='screen',
    )

    # web_dashboard(:8080) 는 여기서 띄우지 않습니다.
    #
    # 관제 화면은 Streamlit 대시보드로 일원화됐고, 지도/탐지 목록은
    # dashboard_link/minimap_renderer.py(:8091) 가 담당합니다. 이 노드를
    # 부르는 코드는 저장소에 하나도 없는데도 launch 가 항상 띄우고 있어서,
    # 아무도 안 보는 화면을 위해 /camera/image_raw 를 프레임마다 변환하고
    # (imgmsg_to_cv2), 공장 도면과 탐지 이력이 인증 없이 0.0.0.0 에
    # 열려 있었습니다.
    #
    # 파일과 실행 진입점은 그대로 남아 있으니, 디버깅할 때는 직접 띄우세요:
    #   ros2 run smart_factory_sim web_dashboard --ros-args \
    #     -p port:=8080 -p map_name:=digital_twin_map

    mock_event_bridge = Node(
        package='smart_factory_sim',
        executable='mock_event_bridge',
        name='mock_event_bridge',
        parameters=[{'use_sim_time': True,
                     'udp_host': mock_udp_host,
                     'udp_port': mock_udp_port}],
        output='screen',
    )

    # Nav2 lifecycle 활성화와 AMCL 수렴에 시간이 걸리므로 늦게 띄운다.
    patrol_node = TimerAction(
        period=20.0,
        actions=[Node(
            package='smart_factory_sim',
            executable='patrol_node',
            name='patrol_node',
            condition=IfCondition(patrol),
            parameters=[{'use_sim_time': True}],
            output='screen',
        )],
    )

    return LaunchDescription(
        args + env + [
            gazebo,
            robot_state_publisher,
            spawn_entity,
            scan_qos_relay,
            stall_monitor,
            nav2,
            rviz,
            initial_pose_pub,
            event_detector,
            mock_event_bridge,
            patrol_node,
        ]
    )
