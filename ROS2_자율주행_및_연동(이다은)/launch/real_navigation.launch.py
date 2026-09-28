"""저장한 실물 SLAM 지도로 AMCL/Nav2와 대시보드용 /map을 다시 띄운다.

로봇 하드웨어와 LiDAR는 turtlebot3_bringup의 robot.launch.py가 담당한다. 이 파일은
그 뒤에 실행하여 map_server, AMCL, Nav2와 관제 명령을 받는 patrol_node를
붙인다. Cartographer와 동시에 실행하지 않는다. 둘 다 /map -> odom 변환을
발행하기 때문이다.
"""

import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription, LogInfo
from launch.conditions import IfCondition
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue


def generate_launch_description():
    pkg = get_package_share_directory('smart_factory_sim')
    nav2_dir = get_package_share_directory('nav2_bringup')
    default_map = os.path.join(pkg, 'maps', 'factory_map.yaml')
    # 설치된 package share는 보통 쓰기 불가이므로 직접 launch할 때의 기본
    # 기록은 사용자 ROS 디렉터리에 둔다. 통합 실행기는 실제 MAP_FILE 옆 경로를
    # blind_obstacle_store 인자로 명시해 매핑 단계 기록을 그대로 이어 쓴다.
    default_blind_obstacle_store = os.path.expanduser(
        '~/.ros/factory_map.blind_obstacles.json')
    nav2_params = os.path.join(pkg, 'config', 'nav2_params.yaml')
    rviz_config = os.path.join(pkg, 'config', 'rviz_config.rviz')

    map_yaml = LaunchConfiguration('map')
    use_rviz = LaunchConfiguration('use_rviz')
    initial_x = LaunchConfiguration('initial_x')
    initial_y = LaunchConfiguration('initial_y')
    initial_yaw = LaunchConfiguration('initial_yaw')
    blind_obstacle_store = LaunchConfiguration('blind_obstacle_store')

    # 현장 LDS-03 스캔의 header stamp가 수신 시각보다 약 1초 느려
    # AMCL이 TF 캐시보다 오래된 자료로 모두 버렸다. 최신 1개만
    # 받고 현재 시각으로 교정한 전용 토픽을 Nav2에 제공한다.
    nav_scan_relay = Node(
        package='smart_factory_sim', executable='scan_qos_relay',
        name='nav_scan_relay', output='screen',
        parameters=[{
            'use_sim_time': False,
            'input_topic': '/scan',
            'output_topic': '/scan_nav',
            'queue_depth': 1,
            'restamp_stale': True,
            'max_stamp_age_sec': 0.35,
            'max_future_sec': 0.10,
        }],
    )

    nav2 = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(os.path.join(nav2_dir, 'launch', 'bringup_launch.py')),
        launch_arguments={
            'map': map_yaml,
            'use_sim_time': 'False',
            'params_file': nav2_params,
            'autostart': 'True',
        }.items(),
    )

    initial_pose_pub = Node(
        package='smart_factory_sim', executable='initial_pose_pub',
        name='initial_pose_pub', output='screen',
        parameters=[{
            'use_sim_time': False,
            'x': ParameterValue(initial_x, value_type=float),
            'y': ParameterValue(initial_y, value_type=float),
            'yaw': ParameterValue(initial_yaw, value_type=float),
        }],
    )

    rviz = Node(
        package='rviz2', executable='rviz2', name='rviz2', output='screen',
        condition=IfCondition(use_rviz), arguments=['-d', rviz_config],
        parameters=[{'use_sim_time': False}],
        remappings=[('/scan', '/scan_nav')],
    )

    # 실물 지도에 시뮬레이션 기본 웨이포인트를 쓰면 벽/지도 밖 목표로
    # 가게 된다. 빈 경로로 시작하고 대시보드의 /patrol start가 현재
    # 저장 지도로 만든 웨이포인트를 보낼 때까지 기다린다.
    patrol = Node(
        package='smart_factory_sim', executable='patrol_node',
        name='patrol_node', output='screen',
        parameters=[{
            'use_sim_time': False,
            'wait_for_route': True,
            'loop': True,
        }],
    )

    # 매핑 단계에서 찾은 문턱·케이블 등을 다시 불러오고, 순찰 중에
    # 새로 감지한 위치도 같은 파일에 남긴다. /blind_obstacles는 미니맵의
    # patrol_planner가 순찰 목표를 만들 때 즉시 쓴다.
    stall_monitor = Node(
        package='smart_factory_sim', executable='stall_monitor',
        name='stall_monitor', output='screen',
        parameters=[{
            'use_sim_time': False,
            'store_path': ParameterValue(blind_obstacle_store, value_type=str),
        }],
    )

    # nav2_params.yaml의 local/global costmap이 이 노드의
    # /costmap_filter_info와 /keepout_filter_mask를 사용한다. 운영 로그에
    # 기록 파일과 필터 활성 상태를 함께 남겨 배선을 확인할 수 있게 한다.
    keepout_notice = LogInfo(msg=[
        '[real_navigation] stall_monitor store: ', blind_obstacle_store,
        ' | Nav2 local/global KeepoutFilter 활성 '
        '(/costmap_filter_info, /keepout_filter_mask)',
    ])

    return LaunchDescription([
        DeclareLaunchArgument('map', default_value=default_map,
                              description='map_saver_cli가 만든 .yaml 지도 파일'),
        DeclareLaunchArgument('use_rviz', default_value='true',
                              description='RViz 표시 여부'),
        DeclareLaunchArgument('initial_x', default_value='0.0',
                              description='AMCL 초기 위치 x (m)'),
        DeclareLaunchArgument('initial_y', default_value='0.0',
                              description='AMCL 초기 위치 y (m)'),
        DeclareLaunchArgument('initial_yaw', default_value='0.0',
                              description='AMCL 초기 자세 yaw (rad)'),
        DeclareLaunchArgument(
            'blind_obstacle_store',
            default_value=default_blind_obstacle_store,
            description='낮은 장애물 기록 JSON (stall_monitor store_path)',
        ),
        nav_scan_relay,
        nav2,
        initial_pose_pub,
        patrol,
        stall_monitor,
        keepout_notice,
        rviz,
    ])
