import os
from glob import glob
from setuptools import setup, find_packages

package_name = 'smart_factory_sim'

# Helper to find all files in a directory and return them as data_files tuples
SKIP_DIRS = {'__pycache__', '.git'}
SKIP_EXTS = ('.pyc', '.pyo')


def package_files(directory):
    paths = []
    for (path, directories, filenames) in os.walk(directory):
        # __pycache__ 를 설치본에 넣지 않는다 (maps/ 에서 generate_map.py 를
        # 돌리면 생기는데, 그대로 share/ 로 딸려 들어가고 있었다)
        directories[:] = [d for d in directories if d not in SKIP_DIRS]
        for filename in filenames:
            if filename.endswith(SKIP_EXTS):
                continue
            paths.append((os.path.join('share', package_name, path),
                          [os.path.join(path, filename)]))
    return paths

data_files = [
    ('share/ament_index/resource_index/packages', ['resource/' + package_name]),
    ('share/' + package_name, ['package.xml']),
]

# Add launch, worlds, config, maps, models, urdf, and template files
data_files.extend(package_files('launch'))
data_files.extend(package_files('worlds'))
data_files.extend(package_files('config'))
data_files.extend(package_files('maps'))
data_files.extend(package_files('models'))
data_files.extend(package_files('urdf'))
data_files.extend(package_files('smart_factory_sim/templates'))

setup(
    name=package_name,
    version='0.0.1',
    packages=find_packages(exclude=['test']),
    data_files=data_files,
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='leedaeun',
    maintainer_email='leedaeun@todo.todo',
    description='Smart Factory simulation with TurtleBot3 Nav2 and event detection',
    license='Apache-2.0',
    # tests_require/test_suite 는 최신 setuptools에서 경고를 낸다.
    # PEP 508 extra로 선언하면 colcon도 pytest 테스트 의존성으로
    # 인식해 JUnit XML과 정확한 test-result 집계를 만든다.
    extras_require={'test': ['pytest']},
    entry_points={
        'console_scripts': [
            'event_detector = smart_factory_sim.event_detector:main',
            'web_dashboard = smart_factory_sim.web_dashboard:main',
            'patrol_node = smart_factory_sim.patrol_node:main',
            'initial_pose_pub = smart_factory_sim.initial_pose_pub:main',
            'step_teleop = smart_factory_sim.step_teleop:main',
            'auto_mapper = smart_factory_sim.auto_mapper:main',
            'mock_event_bridge = smart_factory_sim.mock_event_bridge:main',
            'scan_qos_relay = smart_factory_sim.scan_qos_relay:main',
            'odom_filter_relay = smart_factory_sim.odom_filter_relay:main',
            'stall_monitor = smart_factory_sim.stall_monitor:main',
            'capture_map_pose = smart_factory_sim.capture_map_pose:main',
            'gazebo_camera_stream = smart_factory_sim.gazebo_camera_stream:main',
            'real_pose_udp_forwarder = smart_factory_sim.real_pose_udp_forwarder:main',
            'gazebo_twin_pose_receiver = smart_factory_sim.gazebo_twin_pose_receiver:main',
            'real_detection_udp_forwarder = smart_factory_sim.real_detection_udp_forwarder:main',
            'gazebo_detection_model_manager = smart_factory_sim.gazebo_detection_model_manager:main',
        ],
    },
)
