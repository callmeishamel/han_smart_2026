#!/usr/bin/env python3
"""
vision_inference_node.py — ROS2 브릿지 노드

ai_inference_sender.py가 UDP로 던지는 탐지 결과를 받아서,
ROS2 /safety_status (std_msgs/String, JSON) 토픽으로 그대로 재발행합니다.

이 노드가 필요한 이유
---------------------
ai_inference_sender.py는 Jetson의 Docker 컨테이너 안에서 도는데 그 컨테이너에는
ROS2(rclpy)가 없습니다. 그래서 순수 UDP 소켓으로 결과를 "던지기만" 하고,
호스트에서 도는 이 노드가 그걸 받아 ROS2 생태계로 옮겨줍니다.
이 노드가 꺼져 있으면 /safety_status 토픽 자체가 존재하지 않습니다.

주의: 수신 주소/포트는 ai_inference_sender.py의 송신 설정과 반드시 같아야 합니다.
한쪽만 바꾸면 에러 없이 조용히 데이터가 끊기므로, 양쪽이 같은 환경변수를 읽습니다.

실행:
    python3 vision_inference_node.py
    ros2 run ... --ros-args -p udp_port:=9999 -p bind_ip:=127.0.0.1
"""
import json
import os
import socket
import time

import rclpy
from rclpy.node import Node
from std_msgs.msg import String

# ai_inference_sender.py와 공유하는 설정 (양쪽이 같은 환경변수를 읽습니다)
DEFAULT_BIND_IP = os.getenv("ROS2_BRIDGE_BIND_IP", "127.0.0.1")
DEFAULT_UDP_PORT = int(os.getenv("ROS2_UDP_PORT", "9999"))

# 한 콜백에서 최대 몇 개까지 몰아서 처리할지 (버스트가 와도 밀리지 않게)
MAX_PACKETS_PER_CALLBACK = 50
LOG_INTERVAL_SEC = 2.0


class VisionInferenceNode(Node):
    def __init__(self):
        super().__init__('vision_inference_node')

        self.declare_parameter('bind_ip', DEFAULT_BIND_IP)
        self.declare_parameter('udp_port', DEFAULT_UDP_PORT)
        bind_ip = self.get_parameter('bind_ip').value
        udp_port = self.get_parameter('udp_port').value

        # 다은, 상민 팀원이 실시간으로 받아먹을 ROS2 공식 토픽 오픈
        self.publisher_ = self.create_publisher(String, '/safety_status', 10)

        # 도커 안쪽에서 던지는 UDP 데이터를 받아낼 소켓 서버 기동
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self.sock.bind((bind_ip, udp_port))
        except OSError as exc:
            self.sock.close()
            raise RuntimeError(
                f"UDP {bind_ip}:{udp_port} 바인딩 실패: {exc}\n"
                f"  이미 다른 프로세스가 이 포트를 쓰고 있는지 확인하세요 "
                f"(lsof -i :{udp_port})"
            ) from exc
        self.sock.setblocking(False)  # ROS2가 멈추지 않도록 논블로킹 처리

        # 통계용 카운터. 매 패킷마다 로그를 찍으면 로그 I/O가 콜백을 블로킹해서
        # 수신이 밀리므로, LOG_INTERVAL_SEC마다 요약만 출력합니다.
        self._published = 0
        self._dropped = 0
        self._last_log_time = time.monotonic()

        self.timer = self.create_timer(0.01, self.timer_callback)  # 100Hz 수신 루프
        self.get_logger().info(
            f"도커 데이터 수신용 ROS2 브릿지 노드 준비 완료 "
            f"(수신 {bind_ip}:{udp_port} → 발행 /safety_status)"
        )

        # ai_inference_sender.py 는 보통 이 호스트 위의 Docker 컨테이너 안에서 돕니다.
        # 컨테이너가 --network host 가 아니면, 컨테이너의 127.0.0.1 은 호스트가
        # 아니라 컨테이너 자신이라 이 노드에는 아무것도 오지 않습니다.
        # UDP라 에러가 안 나서, "준비 완료" 로그만 뜬 채 조용히 멈춰 있는 것처럼 보입니다.
        if bind_ip in ("127.0.0.1", "localhost", "::1"):
            self.get_logger().warning(
                f"루프백({bind_ip})에만 바인딩되어 있습니다. 송신 측이 --network host 가 "
                f"아닌 컨테이너이거나 다른 기기라면 패킷이 도착하지 않습니다. "
                f"그 경우 ROS2_BRIDGE_BIND_IP=0.0.0.0 을 쓰세요."
            )

    def timer_callback(self):
        # 콜백당 하나만 읽으면 executor가 순간적으로 밀렸을 때 커널 UDP 수신 버퍼가
        # 넘쳐 최신 패킷이 버려집니다. 큐가 빌 때까지(또는 상한까지) 몰아서 비웁니다.
        for _ in range(MAX_PACKETS_PER_CALLBACK):
            try:
                data, _addr = self.sock.recvfrom(65535)
            except BlockingIOError:
                break  # 큐가 비었음 - 정상
            except OSError as exc:
                self.get_logger().warning(f"UDP 수신 실패: {exc}")
                break

            self._handle_packet(data)

        self._log_stats()

    def _handle_packet(self, data: bytes):
        """UDP는 순서/무결성을 보장하지 않으므로 깨진 패킷이 들어올 수 있습니다.
        여기서 걸러내지 않으면 그대로 하류(event_detector.py)까지 흘러가고,
        decode 실패는 타이머 콜백에서 예외로 터져 노드를 죽입니다."""
        try:
            text = data.decode('utf-8')
        except UnicodeDecodeError:
            self._dropped += 1
            return

        try:
            json.loads(text)
        except (json.JSONDecodeError, ValueError):
            self._dropped += 1
            return

        msg = String()
        msg.data = text
        self.publisher_.publish(msg)
        self._published += 1

    def _log_stats(self):
        now = time.monotonic()
        if now - self._last_log_time < LOG_INTERVAL_SEC:
            return

        if self._published or self._dropped:
            summary = f"발행 {self._published}건 / {LOG_INTERVAL_SEC:.0f}초"
            if self._dropped:
                summary += f" | ⚠ 손상 패킷 {self._dropped}건 폐기"
            self.get_logger().info(summary)

        self._published = 0
        self._dropped = 0
        self._last_log_time = now

    def close(self):
        if self.sock is not None:
            self.sock.close()
            self.sock = None


def main(args=None):
    rclpy.init(args=args)
    node = VisionInferenceNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.close()
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
