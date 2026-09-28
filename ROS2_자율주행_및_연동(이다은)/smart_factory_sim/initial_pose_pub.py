#!/usr/bin/env python3
"""AMCL 초기 위치를 발행한다.

예전 버전의 문제
----------------
1. `import tf_transformations` — package.xml 에 없는 패키지라 설치돼 있지 않으면
   ImportError 로 노드가 즉사했다. launch 는 멈추지 않으므로 트레이스백이
   Gazebo/Nav2 로그에 묻혀 "왜 로봇 위치가 틀리지?" 만 남았다.
   실제로 쓰던 건 quaternion_from_euler(0,0,0) = (0,0,0,1) 상수뿐이라
   의존성 없이 계산한다.

2. 시작 5초 뒤 딱 한 번만 발행하고 7초에 노드가 스스로 죽었다. Gazebo + Nav2
   bringup 은 lifecycle 활성화까지 보통 5~15초가 걸려서, AMCL 이 아직 구독을
   열기 전이면 메시지가 사라졌다. use_sim_time 도 설정돼 있지 않아 타이머가
   실시간 5초에 발화했고, 시뮬레이션이 느릴수록 더 자주 어긋났다.
   → 이제 AMCL 이 실제로 그 위치를 받아들일 때까지 반복 발행하고,
     /amcl_pose 로 확인되면 멈춘다.
"""

import math

import rclpy
from rclpy.node import Node
from geometry_msgs.msg import PoseWithCovarianceStamped


class InitialPosePub(Node):
    def __init__(self):
        super().__init__('initial_pose_pub')

        # 로봇 스폰 좌표와 반드시 같아야 한다.
        # launch 파일의 spawn_entity -x/-y, 그리고 nav2_params.yaml 의
        # amcl.initial_pose 와 세 곳이 모두 일치해야 한다.
        self.declare_parameter('x', 0.5)
        self.declare_parameter('y', 3.5)
        self.declare_parameter('yaw', 0.0)
        self.declare_parameter('period', 1.0)        # 재발행 간격(초)
        self.declare_parameter('max_attempts', 30)   # 이만큼 시도하고 포기
        self.declare_parameter('tolerance', 0.30)    # 이 거리 안이면 반영된 것으로 본다
        self.declare_parameter('yaw_tolerance', 0.35)  # 약 20도

        self.x = self.get_parameter('x').value
        self.y = self.get_parameter('y').value
        self.yaw = self.get_parameter('yaw').value
        self.period = self.get_parameter('period').value
        self.max_attempts = self.get_parameter('max_attempts').value
        self.tolerance = self.get_parameter('tolerance').value
        self.yaw_tolerance = self.get_parameter('yaw_tolerance').value

        self.pub = self.create_publisher(PoseWithCovarianceStamped, '/initialpose', 10)
        # AMCL 이 실제로 그 위치로 옮겨갔는지 확인하는 용도
        self.create_subscription(PoseWithCovarianceStamped, '/amcl_pose',
                                 self.amcl_pose_callback, 10)

        self.attempts = 0
        self.confirmed = False
        self.done = False

        self.get_logger().info(
            f"초기 위치 ({self.x}, {self.y}, yaw={self.yaw}) 를 AMCL 이 받을 때까지 발행합니다."
        )
        self.timer = self.create_timer(self.period, self.tick)

    def build_msg(self):
        msg = PoseWithCovarianceStamped()
        msg.header.frame_id = 'map'
        msg.header.stamp = self.get_clock().now().to_msg()

        msg.pose.pose.position.x = float(self.x)
        msg.pose.pose.position.y = float(self.y)
        msg.pose.pose.position.z = 0.0

        # yaw 만 있는 회전의 쿼터니언 (tf_transformations 없이)
        msg.pose.pose.orientation.x = 0.0
        msg.pose.pose.orientation.y = 0.0
        msg.pose.pose.orientation.z = math.sin(self.yaw / 2.0)
        msg.pose.pose.orientation.w = math.cos(self.yaw / 2.0)

        # 높은 신뢰도 (x, y, yaw 대각 성분)
        msg.pose.covariance[0] = 0.05
        msg.pose.covariance[7] = 0.05
        msg.pose.covariance[35] = 0.05
        return msg

    def amcl_pose_callback(self, msg):
        # 우리가 /initialpose를 한 번이라도 보낸 뒤 도착한 AMCL 결과만 확인한다.
        if self.confirmed or self.attempts == 0:
            return
        dx = msg.pose.pose.position.x - self.x
        dy = msg.pose.pose.position.y - self.y
        orientation = msg.pose.pose.orientation
        actual_yaw = math.atan2(
            2.0 * (orientation.w * orientation.z + orientation.x * orientation.y),
            1.0 - 2.0 * (orientation.y ** 2 + orientation.z ** 2),
        )
        yaw_error = math.atan2(
            math.sin(actual_yaw - self.yaw),
            math.cos(actual_yaw - self.yaw),
        )
        if (math.hypot(dx, dy) <= self.tolerance and
                abs(yaw_error) <= self.yaw_tolerance):
            self.confirmed = True
            self.get_logger().info(
                f"AMCL 이 초기 위치를 반영했습니다 "
                f"({msg.pose.pose.position.x:.2f}, {msg.pose.pose.position.y:.2f}, "
                f"yaw={actual_yaw:.2f})."
            )

    def tick(self):
        if self.done:
            return

        if self.confirmed:
            self.finish("초기 위치 설정 완료.")
            return

        if self.attempts >= self.max_attempts:
            self.finish(
                f"AMCL 확인 없이 {self.attempts}회 발행 후 종료합니다. "
                f"Nav2 가 떠 있는지, /amcl_pose 가 발행되는지 확인하세요.",
                warn=True,
            )
            return

        self.attempts += 1
        subs = self.pub.get_subscription_count()
        self.pub.publish(self.build_msg())

        if subs == 0:
            # AMCL 이 아직 구독을 열지 않았다 - 다음 주기에 다시 보낸다
            self.get_logger().info(
                f"[{self.attempts}/{self.max_attempts}] /initialpose 구독자 없음. 재시도합니다."
            )
        else:
            self.get_logger().info(
                f"[{self.attempts}/{self.max_attempts}] 초기 위치 발행 (구독자 {subs}개)."
            )

    def finish(self, message, warn=False):
        self.done = True
        self.timer.cancel()
        (self.get_logger().warn if warn else self.get_logger().info)(message)
        raise SystemExit


def main(args=None):
    rclpy.init(args=args)
    node = InitialPosePub()
    try:
        rclpy.spin(node)
    except (SystemExit, KeyboardInterrupt):
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
