#!/usr/bin/env python3
"""Expose Gazebo's ROS camera topic as a small local MJPEG server.

The Streamlit dashboard already expects a camera feed at the root URL of the
configured camera host.  This node keeps that existing dashboard contract,
but takes frames from Gazebo's ``/camera/image_raw`` instead of the Jetson.
It is deliberately a read-only bridge: it never publishes commands to the
robot or contacts the real Jetson.
"""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
from cv_bridge import CvBridge
import rclpy
from rclpy.node import Node
from sensor_msgs.msg import Image


class _FrameStore:
    """Thread-safe JPEG frame storage shared by ROS and HTTP threads."""

    def __init__(self):
        self.lock = threading.Lock()
        self.new_frame = threading.Event()
        self.jpeg = None

    def set_image(self, image):
        ok, encoded = cv2.imencode('.jpg', image,
                                   [cv2.IMWRITE_JPEG_QUALITY, 80])
        if not ok:
            return
        with self.lock:
            self.jpeg = encoded.tobytes()
        self.new_frame.set()

    def get_jpeg(self):
        with self.lock:
            return self.jpeg


def _make_handler(store):
    class CameraHandler(BaseHTTPRequestHandler):
        """Serve an MJPEG stream at both / and /video_feed."""

        def log_message(self, _format, *_args):
            # A browser reconnects frequently; keep ROS logs readable.
            pass

        def do_GET(self):
            if self.path not in ('/', '/video_feed'):
                self.send_error(404, 'Use / or /video_feed')
                return
            self.send_response(200)
            self.send_header('Content-Type',
                             'multipart/x-mixed-replace; boundary=frame')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            try:
                while True:
                    store.new_frame.wait(timeout=1.0)
                    store.new_frame.clear()
                    jpeg = store.get_jpeg()
                    if jpeg is None:
                        continue
                    self.wfile.write(b'--frame\r\n')
                    self.wfile.write(b'Content-Type: image/jpeg\r\n')
                    self.wfile.write(
                        f'Content-Length: {len(jpeg)}\r\n\r\n'.encode())
                    self.wfile.write(jpeg)
                    self.wfile.write(b'\r\n')
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

    return CameraHandler


class GazeboCameraStream(Node):
    def __init__(self):
        super().__init__('gazebo_camera_stream')
        self.declare_parameter('image_topic', '/camera/image_raw')
        self.declare_parameter('bind_host', '127.0.0.1')
        self.declare_parameter('port', 8500)
        self.bridge = CvBridge()
        self.store = _FrameStore()
        host = self.get_parameter('bind_host').value
        port = int(self.get_parameter('port').value)

        self.create_subscription(Image,
                                 self.get_parameter('image_topic').value,
                                 self._on_image, 10)
        handler = _make_handler(self.store)
        self.server = ThreadingHTTPServer((host, port), handler)
        self.server_thread = threading.Thread(
            target=self.server.serve_forever, name='gazebo-mjpeg', daemon=True)
        self.server_thread.start()
        self.get_logger().info(
            f'Gazebo camera MJPEG: http://{host}:{port}/ '
            '(source: /camera/image_raw)')

    def _on_image(self, msg):
        try:
            image = self.bridge.imgmsg_to_cv2(msg, desired_encoding='bgr8')
            self.store.set_image(image)
        except Exception as exc:  # cv_bridge uses several exception classes.
            self.get_logger().warning(f'카메라 프레임 변환 실패: {exc}')

    def destroy_node(self):
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=2.0)
        super().destroy_node()


def main(args=None):
    rclpy.init(args=args)
    node = GazeboCameraStream()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
