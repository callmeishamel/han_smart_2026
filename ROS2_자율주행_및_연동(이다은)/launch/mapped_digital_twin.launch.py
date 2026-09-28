"""Gazebo digital twin reconstructed from the real ``factory_map`` SLAM map.

Regenerate the world after replacing the real map::

    python3 maps/map_to_gazebo_world.py

Then build/source the package and run::

    ros2 launch smart_factory_sim mapped_digital_twin.launch.py
    ros2 launch smart_factory_sim mapped_digital_twin.launch.py patrol:=true

The generated JSON keeps the Gazebo spawn pose, AMCL initial pose, mock event
poses and map-derived patrol route in one coordinate contract.
"""

import json
import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import (DeclareLaunchArgument, IncludeLaunchDescription,
                            SetEnvironmentVariable, TimerAction)
from launch.conditions import IfCondition
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    pkg = get_package_share_directory('smart_factory_sim')
    gazebo_ros_dir = get_package_share_directory('gazebo_ros')
    nav2_dir = get_package_share_directory('nav2_bringup')

    world_path = os.path.join(pkg, 'worlds', 'factory_digital_twin.world')
    metadata_path = os.path.join(pkg, 'config', 'factory_digital_twin.json')
    urdf_path = os.path.join(pkg, 'urdf', 'turtlebot3_burger_camera.urdf')
    robot_sdf_path = os.path.join(
        pkg, 'models', 'turtlebot3_burger_camera', 'model.sdf')
    fire_sdf_path = os.path.join(pkg, 'models', 'fire_event', 'model.sdf')
    helmet_sdf_path = os.path.join(
        pkg, 'models', 'helmet_violation_event', 'model.sdf')
    nav2_params = os.path.join(pkg, 'config', 'nav2_params.yaml')
    rviz_config = os.path.join(pkg, 'config', 'rviz_config.rviz')

    with open(metadata_path, 'r', encoding='utf-8') as stream:
        metadata = json.load(stream)
    generated_map_path = metadata.get('map_yaml')
    # Old metadata did not record its input map.  Preserve compatibility with
    # it, but a newly generated twin always uses its exact real-map input.
    map_path = (generated_map_path if generated_map_path and
                os.path.isfile(generated_map_path) else
                os.path.join(pkg, 'maps', 'factory_map.yaml'))
    with open(urdf_path, 'r', encoding='utf-8') as stream:
        robot_description = stream.read()

    spawn = metadata['spawn']
    waypoints = [float(value) for value in metadata.get('waypoints', [])]

    use_nav2 = LaunchConfiguration('use_nav2')
    use_rviz = LaunchConfiguration('use_rviz')
    patrol = LaunchConfiguration('patrol')
    mirror_real_pose = LaunchConfiguration('mirror_real_pose')
    mirror_real_detections = LaunchConfiguration('mirror_real_detections')
    enable_mock_events = LaunchConfiguration('enable_mock_events')

    models_path = os.path.join(pkg, 'models')
    existing_model_path = os.environ.get('GAZEBO_MODEL_PATH', '')

    gazebo = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            os.path.join(gazebo_ros_dir, 'launch', 'gazebo.launch.py')),
        launch_arguments={
            'world': world_path,
            'gui': LaunchConfiguration('gui'),
        }.items(),
    )

    robot_state_publisher = Node(
        package='robot_state_publisher',
        executable='robot_state_publisher',
        name='robot_state_publisher',
        output='screen',
        parameters=[{
            'use_sim_time': True,
            'robot_description': robot_description,
        }],
    )

    spawn_entity = Node(
        package='gazebo_ros',
        executable='spawn_entity.py',
        name='spawn_mapped_twin_robot',
        output='screen',
        arguments=[
            '-entity', 'turtlebot3_burger',
            '-file', robot_sdf_path,
            '-x', str(spawn['x']),
            '-y', str(spawn['y']),
            '-z', '0.01',
            '-Y', str(spawn['yaw']),
        ],
    )

    fire = metadata['events']['fire']
    helmet = metadata['events']['helmet_violation']
    spawn_mock_fire = Node(
        package='gazebo_ros',
        executable='spawn_entity.py',
        name='spawn_mock_fire_event',
        condition=IfCondition(enable_mock_events),
        output='screen',
        arguments=[
            '-entity', 'fire_event_target', '-file', fire_sdf_path,
            '-x', str(fire['x']), '-y', str(fire['y']), '-z', str(fire['z']),
        ],
    )
    spawn_mock_helmet = Node(
        package='gazebo_ros',
        executable='spawn_entity.py',
        name='spawn_mock_helmet_event',
        condition=IfCondition(enable_mock_events),
        output='screen',
        arguments=[
            '-entity', 'helmet_violation_event_target',
            '-file', helmet_sdf_path,
            '-x', str(helmet['x']), '-y', str(helmet['y']),
            '-z', str(helmet['z']),
        ],
    )

    scan_qos_relay = Node(
        package='smart_factory_sim',
        executable='scan_qos_relay',
        name='mapped_twin_scan_qos_relay',
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

    stall_monitor = Node(
        package='smart_factory_sim',
        executable='stall_monitor',
        name='mapped_twin_stall_monitor',
        condition=IfCondition(use_nav2),
        parameters=[{
            'use_sim_time': True,
            'store_path': (
                '/tmp/smart_factory_mapped_twin_blind_obstacles.json'),
        }],
        output='screen',
    )

    nav2 = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            os.path.join(nav2_dir, 'launch', 'bringup_launch.py')),
        condition=IfCondition(use_nav2),
        launch_arguments={
            # Keep the navigation map locked to the map from which the
            # generated wall mesh was built.  Re-run the generator instead of
            # overriding only one side of this coordinate contract.
            'map': map_path,
            'use_sim_time': 'True',
            'params_file': nav2_params,
            'autostart': 'True',
        }.items(),
    )

    initial_pose_pub = Node(
        package='smart_factory_sim',
        executable='initial_pose_pub',
        name='mapped_twin_initial_pose_pub',
        condition=IfCondition(use_nav2),
        parameters=[{
            'use_sim_time': True,
            'x': float(spawn['x']),
            'y': float(spawn['y']),
            'yaw': float(spawn['yaw']),
        }],
        output='screen',
    )

    # In live-mirror mode Nav2 is deliberately disabled by the stack runner:
    # this lightweight map server still supplies /map to the twin minimap.
    mirror_map_server = Node(
        package='nav2_map_server',
        executable='map_server',
        name='map_server',
        condition=IfCondition(mirror_real_pose),
        parameters=[{
            'use_sim_time': False,
            'yaml_filename': map_path,
        }],
        output='screen',
    )

    mirror_map_lifecycle = Node(
        package='nav2_lifecycle_manager',
        executable='lifecycle_manager',
        name='mapped_twin_map_lifecycle_manager',
        condition=IfCondition(mirror_real_pose),
        parameters=[{
            'use_sim_time': False,
            'autostart': True,
            'node_names': ['map_server'],
        }],
        output='screen',
    )

    pose_receiver = Node(
        package='smart_factory_sim',
        executable='gazebo_twin_pose_receiver',
        name='gazebo_twin_pose_receiver',
        condition=IfCondition(mirror_real_pose),
        parameters=[{
            'udp_host': LaunchConfiguration('mirror_udp_host'),
            'udp_port': LaunchConfiguration('mirror_udp_port'),
            'model_name': 'turtlebot3_burger',
            'model_z': 0.01,
        }],
        output='screen',
    )

    detection_model_manager = Node(
        package='smart_factory_sim',
        executable='gazebo_detection_model_manager',
        name='gazebo_detection_model_manager',
        condition=IfCondition(mirror_real_detections),
        parameters=[{
            'udp_host': LaunchConfiguration('detection_udp_host'),
            'udp_port': LaunchConfiguration('detection_udp_port'),
            'snapshot_timeout_seconds': 40.0,
        }],
        output='screen',
    )

    event_detector = Node(
        package='smart_factory_sim',
        executable='event_detector',
        name='mapped_twin_event_detector',
        condition=IfCondition(enable_mock_events),
        parameters=[{
            'use_sim_time': True,
            'use_mock': True,
            'camera_frame': 'camera_rgb_optical_frame',
            'map_frame': 'map',
        }],
        output='screen',
    )

    mock_event_bridge = Node(
        package='smart_factory_sim',
        executable='mock_event_bridge',
        name='mapped_twin_mock_event_bridge',
        condition=IfCondition(enable_mock_events),
        parameters=[{
            'use_sim_time': True,
            'udp_host': LaunchConfiguration('mock_udp_host'),
            'udp_port': LaunchConfiguration('mock_udp_port'),
        }],
        output='screen',
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

    patrol_node = TimerAction(
        period=20.0,
        actions=[Node(
            package='smart_factory_sim',
            executable='patrol_node',
            name='mapped_twin_patrol_node',
            condition=IfCondition(patrol),
            parameters=[{
                'use_sim_time': True,
                'waypoints': waypoints,
                'loop': True,
            }],
            output='screen',
        )],
    )

    return LaunchDescription([
        DeclareLaunchArgument('gui', default_value='true',
                              description='Gazebo GUI 표시 여부'),
        DeclareLaunchArgument('use_nav2', default_value='false',
                              description='Nav2 자율주행 스택 실행 여부'),
        DeclareLaunchArgument('use_rviz', default_value='true',
                              description='RViz 실행 여부'),
        DeclareLaunchArgument('patrol', default_value='false',
                              description='맵에서 자동 선정한 지점 순찰 여부'),
        DeclareLaunchArgument(
            'mirror_real_pose', default_value='false',
            description='실물 /amcl_pose를 받은 UDP 위치로 Gazebo 로봇 이동'),
        DeclareLaunchArgument(
            'mirror_udp_host', default_value='127.0.0.1',
            description='실물 위치 UDP를 수신할 로컬 IP'),
        DeclareLaunchArgument(
            'mirror_udp_port', default_value='9995',
            description='실물 위치 UDP를 수신할 로컬 포트'),
        DeclareLaunchArgument(
            'mirror_real_detections', default_value='false',
            description='실물 지도 탐지 위치에 동적 3D 모델 표시'),
        DeclareLaunchArgument(
            'detection_udp_host', default_value='127.0.0.1',
            description='실물 탐지 스냅샷 UDP 수신 IP'),
        DeclareLaunchArgument(
            'detection_udp_port', default_value='9994',
            description='실물 탐지 스냅샷 UDP 수신 포트'),
        DeclareLaunchArgument(
            'enable_mock_events', default_value='false',
            description='가상 화재/안전모 표식과 mock 탐지 실행 여부'),
        DeclareLaunchArgument('mock_udp_host', default_value='127.0.0.1',
                              description='mock 이벤트 데이터 파이프라인 IP'),
        DeclareLaunchArgument('mock_udp_port', default_value='9998',
                              description='mock 이벤트 데이터 파이프라인 UDP 포트'),
        SetEnvironmentVariable(
            'GAZEBO_MODEL_PATH',
            f'{models_path}:{existing_model_path}'
            if existing_model_path else models_path,
        ),
        SetEnvironmentVariable('TURTLEBOT3_MODEL', 'burger'),
        gazebo,
        robot_state_publisher,
        spawn_entity,
        spawn_mock_fire,
        spawn_mock_helmet,
        scan_qos_relay,
        stall_monitor,
        nav2,
        initial_pose_pub,
        mirror_map_server,
        mirror_map_lifecycle,
        pose_receiver,
        detection_model_manager,
        event_detector,
        mock_event_bridge,
        rviz,
        patrol_node,
    ])
