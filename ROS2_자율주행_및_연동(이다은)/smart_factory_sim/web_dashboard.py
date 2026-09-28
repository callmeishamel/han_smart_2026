#!/usr/bin/env python3
import rclpy
from rclpy.node import Node
from sensor_msgs.msg import Image
from std_msgs.msg import String
from cv_bridge import CvBridge
import cv2
import numpy as np
import json
import time
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingTCPServer
from ament_index_python.packages import get_package_share_directory

# TF2 imports for robot pose tracking
import tf2_ros

# Thread-safe global state for the web server
global_state = {
    'latest_frame': None,
    'robot_pose': {'x': 0.0, 'y': 0.0, 'yaw': 0.0},
    'events': [],
    # 표시할 지도 이름(확장자 제외). launch 파일이 Nav2 에 넘기는 지도와 같은
    # 것을 보도록 map_name 파라미터로 맞춘다. 예전에는 factory_map 이 하드코딩돼
    # 있어서, Nav2 는 A 지도로 주행하는데 대시보드는 B 지도를 그리고 있었다.
    'map_name': 'factory_map',
    'lock': threading.Lock(),
    'new_frame_event': threading.Event(),
    'sse_clients': []
}

class DashboardHTTPHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Suppress request logging to avoid cluttering ROS output
        pass

    def do_GET(self):
        # Serve the HTML page
        if self.path == '/' or self.path == '/index.html':
            self.serve_html()
        # Serve the MJPEG video stream
        elif self.path == '/video_feed':
            self.serve_video_feed()
        # Serve the map image (convert PGM to JPG)
        elif self.path == '/map_image':
            self.serve_map_image()
        # Serve the map metadata
        elif self.path == '/map_info':
            self.serve_map_info()
        # Server-Sent Events stream
        elif self.path == '/events':
            self.serve_sse_stream()
        else:
            self.send_error(404, "File not found")

    def serve_html(self):
        try:
            # Find the index.html file path
            pkg_share = get_package_share_directory('smart_factory_sim')
            html_path = os.path.join(pkg_share, 'launch/../smart_factory_sim/templates/index.html')
            
            # Fallback path if share directory structure is different during development
            if not os.path.exists(html_path):
                html_path = os.path.join(os.path.dirname(__file__), 'templates/index.html')

            with open(html_path, 'r', encoding='utf-8') as f:
                content = f.read()

            self.send_response(200)
            self.send_header('Content-type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(content.encode('utf-8'))
        except Exception as e:
            self.send_error(500, f"Error reading index.html: {str(e)}")

    def serve_video_feed(self):
        self.send_response(200)
        self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=frame')
        self.end_headers()
        
        while True:
            # Wait for a new frame
            global_state['new_frame_event'].wait(timeout=1.0)
            global_state['new_frame_event'].clear()
            
            with global_state['lock']:
                frame = global_state['latest_frame']
            
            if frame is None:
                # If no frame, write a placeholder gray image
                placeholder = np.zeros((240, 320, 3), dtype=np.uint8)
                cv2.putText(placeholder, "No Video Signal", (50, 120), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
                _, jpeg = cv2.imencode('.jpg', placeholder)
                jpeg_bytes = jpeg.tobytes()
            else:
                _, jpeg = cv2.imencode('.jpg', frame)
                jpeg_bytes = jpeg.tobytes()
                
            try:
                self.wfile.write(b'--frame\r\n')
                self.wfile.write(b'Content-Type: image/jpeg\r\n')
                self.wfile.write(f'Content-Length: {len(jpeg_bytes)}\r\n\r\n'.encode())
                self.wfile.write(jpeg_bytes)
                self.wfile.write(b'\r\n')
            except (ConnectionResetError, BrokenPipeError):
                break
            time.sleep(0.033) # Limit to ~30 FPS

    def serve_map_image(self):
        try:
            name = global_state['map_name']
            pkg_share = get_package_share_directory('smart_factory_sim')
            map_path = os.path.join(pkg_share, 'maps', name + '.pgm')

            if not os.path.exists(map_path):
                # Fallback to source directory
                map_path = os.path.join(os.path.dirname(__file__), '..', 'maps', name + '.pgm')

            # Load PGM using OpenCV
            img = cv2.imread(map_path)
            if img is None:
                self.send_error(500, "Could not load map PGM")
                return

            _, jpeg = cv2.imencode('.jpg', img)
            jpeg_bytes = jpeg.tobytes()

            self.send_response(200)
            self.send_header('Content-type', 'image/jpeg')
            self.send_header('Content-Length', str(len(jpeg_bytes)))
            self.end_headers()
            self.wfile.write(jpeg_bytes)
        except Exception as e:
            self.send_error(500, f"Error serving map image: {str(e)}")

    def serve_map_info(self):
        try:
            name = global_state['map_name']
            pkg_share = get_package_share_directory('smart_factory_sim')
            yaml_path = os.path.join(pkg_share, 'maps', name + '.yaml')
            if not os.path.exists(yaml_path):
                yaml_path = os.path.join(os.path.dirname(__file__), '..', 'maps', name + '.yaml')

            # 파일을 못 찾았을 때의 값. 예전에는 실제 지도와 전혀 다른
            # origin=[-6.0, -3.0] 을 조용히 돌려줘서, 대시보드가 잘못된 좌표계로
            # 로봇을 그려도 알아챌 방법이 없었다. fallback 플래그를 함께 준다.
            map_info = {
                'resolution': 0.05,
                'origin': [0.0, 0.0, 0.0],
                'fallback': True,
            }

            # Simple parse YAML if exists
            if os.path.exists(yaml_path):
                map_info['fallback'] = False
                with open(yaml_path, 'r') as f:
                    for line in f:
                        if 'resolution:' in line:
                            map_info['resolution'] = float(line.split(':')[1].strip())
                        elif 'origin:' in line:
                            val_str = line.split(':')[1].strip().replace('[', '').replace(']', '')
                            map_info['origin'] = [float(x) for x in val_str.split(',')]

            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps(map_info).encode())
        except Exception as e:
            self.send_error(500, str(e))

    def serve_sse_stream(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'keep-alive')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        
        # Add to list of active clients
        client_queue = threading.Event()
        global_state['sse_clients'].append(client_queue)
        
        # Immediately send current state
        with global_state['lock']:
            initial_data = {
                'type': 'init',
                'pose': global_state['robot_pose'],
                'events': global_state['events']
            }
            
        try:
            self.wfile.write(f"data: {json.dumps(initial_data)}\n\n".encode())
            self.wfile.flush()
        except (ConnectionResetError, BrokenPipeError):
            global_state['sse_clients'].remove(client_queue)
            return

        # Continuous streaming loop
        while True:
            # Wait for state updates (pose or event)
            client_queue.wait(timeout=0.2)
            client_queue.clear()
            
            with global_state['lock']:
                update_data = {
                    'type': 'update',
                    'pose': global_state['robot_pose'],
                    'events': global_state['events']
                }
                
            try:
                self.wfile.write(f"data: {json.dumps(update_data)}\n\n".encode())
                self.wfile.flush()
            except (ConnectionResetError, BrokenPipeError):
                break
                
        # Cleanup client
        if client_queue in global_state['sse_clients']:
            global_state['sse_clients'].remove(client_queue)


class ThreadedHTTPServer(ThreadingTCPServer):
    allow_reuse_address = True


class WebDashboardNode(Node):
    def __init__(self):
        super().__init__('web_dashboard')
        
        # ROS parameters
        self.declare_parameter('port', 8080)
        self.declare_parameter('robot_frame', 'base_footprint')
        self.declare_parameter('map_frame', 'map')
        # Nav2 에 넘긴 지도와 같은 것을 표시하도록 launch 파일이 지정한다
        self.declare_parameter('map_name', 'factory_map')

        self.port = self.get_parameter('port').value
        self.robot_frame = self.get_parameter('robot_frame').value
        self.map_frame = self.get_parameter('map_frame').value
        global_state['map_name'] = self.get_parameter('map_name').value
        
        self.bridge = CvBridge()
        
        # TF2 listener for pose
        self.tf_buffer = tf2_ros.Buffer()
        self.tf_listener = tf2_ros.TransformListener(self.tf_buffer, self)
        
        # Subscriptions
        self.create_subscription(Image, '/camera/image_raw', self.image_callback, 10)
        self.create_subscription(String, '/detected_events', self.detected_events_callback, 10)
        
        # Timers
        self.create_timer(0.2, self.pose_timer_callback) # Update pose at 5Hz
        
        # Start Web Server Thread
        self.server = ThreadedHTTPServer(('0.0.0.0', self.port), DashboardHTTPHandler)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.get_logger().info(f"Web Dashboard server started on http://localhost:{self.port}")

    def image_callback(self, msg):
        try:
            cv_img = self.bridge.imgmsg_to_cv2(msg, "bgr8")
            with global_state['lock']:
                global_state['latest_frame'] = cv_img
            global_state['new_frame_event'].set()
        except Exception as e:
            pass

    def detected_events_callback(self, msg):
        try:
            event_data = json.loads(msg.data)
            
            with global_state['lock']:
                # Upsert event by id
                event_id = event_data['id']
                found = False
                for idx, ev in enumerate(global_state['events']):
                    if ev['id'] == event_id:
                        global_state['events'][idx] = event_data
                        found = True
                        break
                if not found:
                    global_state['events'].append(event_data)
                    
            # Notify all SSE clients
            self.notify_sse_clients()
        except Exception as e:
            self.get_logger().error(f"Error in events subscription: {str(e)}")

    def pose_timer_callback(self):
        try:
            transform = self.tf_buffer.lookup_transform(
                self.map_frame, 
                self.robot_frame, 
                rclpy.time.Time()
            )
            # Extract position
            x = transform.transform.translation.x
            y = transform.transform.translation.y
            
            # Convert quaternion to yaw
            qx = transform.transform.rotation.x
            qy = transform.transform.rotation.y
            qz = transform.transform.rotation.z
            qw = transform.transform.rotation.w
            
            # yaw = atan2(2*(w*z + x*y), 1 - 2*(y^2 + z^2))
            siny_cosp = 2 * (qw * qz + qx * qy)
            cosy_cosp = 1 - 2 * (qy * qy + qz * qz)
            yaw = np.arctan2(siny_cosp, cosy_cosp)
            
            with global_state['lock']:
                global_state['robot_pose'] = {'x': x, 'y': y, 'yaw': yaw}
                
            self.notify_sse_clients()
        except Exception as e:
            # TF might not be ready yet, ignore
            pass

    def notify_sse_clients(self):
        for client in global_state['sse_clients']:
            client.set()

    def destroy_node(self):
        self.server.shutdown()
        self.server.server_close()
        super().destroy_node()


def main(args=None):
    rclpy.init(args=args)
    node = WebDashboardNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()

if __name__ == '__main__':
    main()
