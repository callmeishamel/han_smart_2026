"""20x20m 장애물 월드(smart_factory.world) + Nav2 자율주행 스택.

랙 배치 공장에서 순찰을 돌리려면 digital_twin.launch.py 를 쓸 것.
이 launch 는 넓은 공간에서 주행/매핑을 시험하는 용도다.

고친 것
-------
- 지도가 월드와 다른 공간이었다. nav2_bringup 에 넘기던 maps/factory_map.yaml
  은 5.0x5.3m 짜리 실물 SLAM 조각(83%가 미탐사)이라, 20x20m 월드와 정합될 수
  없었다. 이제 이 월드에서 generate_map.py 로 뽑은 maps/smart_factory_map.yaml
  을 쓴다.
- 커스텀 노드들에 use_sim_time 이 빠져 있어 Gazebo 시계와 어긋났다.
- patrol_node 가 통째로 주석 처리돼 있었다. 이 월드 전용 웨이포인트를 넣고
  patrol:=true 로 켤 수 있게 했다. (기본 웨이포인트는 랙 공장용이라 여기서는
  장애물에 겹친다 - 그래서 아래 값을 따로 넘긴다.)
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

START_X = 0.5
START_Y = 3.5
START_YAW = 0.0

# 이 월드 전용 순찰 경로. maps/smart_factory_map.pgm 위에서 장애물까지의 여유가
# 가장 큰 지점을 8방향으로 찾은 값이다(최소 2.25m). x, y, yaw 순서.
SMART_FACTORY_WAYPOINTS = [
     5.87,  1.25,  2.03,
     3.29,  6.46, -3.03,
     0.87,  6.19, -2.57,
    -3.89,  3.15, -1.86,
    -4.97, -0.52, -2.11,
    -6.68, -3.40, -0.20,
     0.87, -4.92,  0.32,
     2.27, -4.46,  1.01,
]


def generate_launch_description():
    pkg = get_package_share_directory('smart_factory_sim')
    gazebo_ros_dir = get_package_share_directory('gazebo_ros')

    world_path = os.path.join(pkg, 'worlds', 'smart_factory.world')
    urdf_path = os.path.join(pkg, 'urdf', 'turtlebot3_burger_camera.urdf')
    sdf_path = os.path.join(pkg, 'models', 'turtlebot3_burger_camera', 'model.sdf')
    map_path = os.path.join(pkg, 'maps', 'smart_factory_map.yaml')
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

    # nav2_params.yaml은 AMCL과 두 costmap이 /scan_nav를 구독한다.
    # 실물 launch만 이 relay를 띄우고 시뮬레이션은 /scan만 발행하고 있어서,
    # AMCL이 LaserScan을 한 장도 받지 못하고 map -> odom TF를 만들지 못했다.
    # 입력은 sensor-data QoS, 출력은 reliable로 통일해 실제/시뮬레이션 양쪽에서
    # 같은 Nav2 설정 파일을 안전하게 공유한다.
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

    # nav2_params.yaml은 local/global costmap의 KeepoutFilter를 항상 켠다.
    # 마스크 발행 노드가 없으면 Nav2가 2초마다 경고하며 필터가 비활성 상태로
    # 남으므로, 시뮬레이션에서도 빈 마스크와 이후 감지 결과를 공급한다.
    stall_monitor = Node(
        package='smart_factory_sim',
        executable='stall_monitor',
        name='stall_monitor',
        condition=IfCondition(use_nav2),
        parameters=[{
            'use_sim_time': True,
            'store_path': '/tmp/smart_factory_sim_blind_obstacles.json',
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
    #     -p port:=8080 -p map_name:=smart_factory_map

    mock_event_bridge = Node(
        package='smart_factory_sim',
        executable='mock_event_bridge',
        name='mock_event_bridge',
        parameters=[{'use_sim_time': True,
                     'udp_host': mock_udp_host,
                     'udp_port': mock_udp_port}],
        output='screen',
    )

    patrol_node = TimerAction(
        period=20.0,
        actions=[Node(
            package='smart_factory_sim',
            executable='patrol_node',
            name='patrol_node',
            condition=IfCondition(patrol),
            parameters=[{'use_sim_time': True,
                         'waypoints': SMART_FACTORY_WAYPOINTS}],
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
