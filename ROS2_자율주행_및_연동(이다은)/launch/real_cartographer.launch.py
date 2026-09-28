import os
from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue


def generate_launch_description():
    tb3_cartographer_dir = get_package_share_directory(
        'turtlebot3_cartographer')
    pkg = get_package_share_directory('smart_factory_sim')
    # 기동 직후 timestamp=0인 /odom은 odom_filter_relay가 제거한다.
    # 따라서 실물 TB3 바퀴 odometry를 scan matching 예측에 안전하게 다시 사용한다.
    cartographer_config_dir = os.path.join(pkg, 'config')
    configuration_basename = 'real_turtlebot3_lds_2d.lua'
    rviz_config_dir = os.path.join(
        tb3_cartographer_dir, 'rviz', 'tb3_cartographer.rviz')
    mapping_trust_range_m = ParameterValue(
        LaunchConfiguration('mapping_trust_range_m'), value_type=float)

    return LaunchDescription([
        DeclareLaunchArgument(
            'mapping_trust_range_m',
            default_value='5.0',
            description=(
                'Ignore finite LiDAR returns beyond this distance while '
                'mapping. '
                'Use 0 to disable the protection.')),
        # LDS-03은 best-effort /scan, Humble Cartographer는 reliable /scan을 요구하는
        # 조합이 있어 중계한다. Cartographer는 아래 remap된 /scan_reliable만 구독한다.
        Node(
            package='smart_factory_sim',
            executable='scan_qos_relay',
            name='scan_qos_relay',
            output='screen',
            parameters=[{'use_sim_time': False,
                         'input_topic': '/scan',
                         'output_topic': '/scan_reliable',
                         'queue_depth': 1,
                         'restamp_stale': True,
                         'max_stamp_age_sec': 0.35,
                         'max_future_sec': 0.10,
                         'max_mapping_range_m': mapping_trust_range_m}],
        ),
        Node(
            package='smart_factory_sim',
            executable='odom_filter_relay',
            name='odom_filter_relay',
            output='screen',
            parameters=[{'use_sim_time': False,
                         'input_topic': '/odom',
                         'output_topic': '/odom_filtered'}],
        ),
        Node(
            package='cartographer_ros',
            executable='cartographer_node',
            name='cartographer_node',
            output='screen',
            parameters=[{'use_sim_time': False}],
            remappings=[('scan', '/scan_reliable'),
                        ('odom', '/odom_filtered')],
            arguments=['-configuration_directory', cartographer_config_dir,
                       '-configuration_basename', configuration_basename]
        ),
        Node(
            package='cartographer_ros',
            executable='cartographer_occupancy_grid_node',
            name='cartographer_occupancy_grid_node',
            output='screen',
            parameters=[{'use_sim_time': False}],
            arguments=['-resolution', '0.05', '-publish_period_sec', '1.0']
        ),
        Node(
            package='rviz2',
            executable='rviz2',
            name='rviz2',
            arguments=['-d', rviz_config_dir],
            # 배포판 RViz 설정의 LaserScan이 reliable을 요청해 LDS-03
            # best-effort publisher와 QoS 경고를 내는 것을 막는다.
            remappings=[('/scan', '/scan_reliable')],
            parameters=[{
                'use_sim_time': False,
                'qos_overrides./scan.subscription.reliability': 'best_effort'
            }],
            output='screen'
        )
    ])
