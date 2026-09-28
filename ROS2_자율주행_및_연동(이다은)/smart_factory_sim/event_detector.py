#!/usr/bin/env python3
import rclpy
from rclpy.node import Node
from sensor_msgs.msg import Image, CameraInfo
from visualization_msgs.msg import Marker, MarkerArray
from std_msgs.msg import String
from gazebo_msgs.msg import ModelStates
from cv_bridge import CvBridge
import cv2
import numpy as np
import json
import time

# TF2 imports
import tf2_ros
from geometry_msgs.msg import TransformStamped

def rotate_point(p, q):
    """Rotates a 3D point p by a quaternion q."""
    qx, qy, qz, qw = q
    px, py, pz = p
    
    # q * p
    rx = qw * px + qy * pz - qz * py
    ry = qw * py + qz * px - qx * pz
    rz = qw * pz + qx * py - qy * px
    rw = -qx * px - qy * py - qz * pz
    
    # (q * p) * q_conj where q_conj = [-qx, -qy, -qz, qw]
    tx = rx * qw + rw * (-qx) + ry * (-qz) - rz * (-qy)
    ty = ry * qw + rw * (-qy) + rz * (-qx) - rx * (-qz)
    tz = rz * qw + rw * (-qz) + rx * (-qy) - ry * (-qx)
    
    return [tx, ty, tz]

def rotate_point_inverse(p, q):
    """Rotates a 3D point p by the inverse of quaternion q (q_conj)."""
    # Inverse of q = [-qx, -qy, -qz, qw]
    q_inv = [-q[0], -q[1], -q[2], q[3]]
    return rotate_point(p, q_inv)

class EventDetectorNode(Node):
    def __init__(self):
        super().__init__('event_detector')
        
        # Declare parameters
        self.declare_parameter('use_mock', True)
        self.declare_parameter('detection_threshold', 0.1)
        self.declare_parameter('camera_frame', 'camera_rgb_optical_frame')
        self.declare_parameter('map_frame', 'map')

        # 젯슨(손준영) 실물 카메라 파라미터 - ai_inference_sender.py가 쓰는 값과 맞춰야 함
        self.declare_parameter('jetson_camera_frame', 'camera_rgb_optical_frame')
        self.declare_parameter('jetson_focal_length', 500.0)
        self.declare_parameter('jetson_frame_width', 1280.0)
        self.declare_parameter('jetson_frame_height', 720.0)
        # 주점(광축이 지나는 픽셀). 0 이면 화면 중심을 씁니다 — 예전 동작 그대로입니다.
        #
        # 렌즈 광축이 센서 정중앙을 지난다는 보장이 없습니다. 캘리브레이션을 하면
        # ai_inference_sender.py 가 시작 로그에 실측 주점(cx)을 찍고, 미니맵 방향각도
        # **그 값을 기준으로** 계산합니다. 여기만 화면 중심을 쓰면 같은 탐지가 두 화면에서
        # 서로 다른 자리에 찍히므로, 그때는 이 파라미터도 함께 넘겨야 합니다.
        self.declare_parameter('jetson_principal_x', 0.0)
        self.declare_parameter('jetson_principal_y', 0.0)

        # 이벤트 목록 크기 제한.
        #
        # detected_events_db 는 한 번 등록된 이벤트를 지우지 않습니다. 지도에 이력을
        # 남기려는 의도적 선택이지만, 그러면 두 가지가 함께 커집니다.
        #   - 딕셔너리 자체 (긴 시연에서 계속 증가)
        #   - 마커 발행량 (_publish_markers 가 매번 누적 전부를 다시 만듦)
        # 이력은 그대로 남기되 상한만 둡니다. 넘으면 가장 오래 안 보인 것부터 버립니다.
        self.declare_parameter('max_events', 200)
        # 0 보다 크면 그 시간(초) 동안 재검출되지 않은 이벤트를 지웁니다.
        # 기본 0 은 "시간으로는 지우지 않음" — 기존 동작 그대로입니다.
        self.declare_parameter('event_retention_sec', 0.0)
        # 마커 발행 주기(초). marker.lifetime 이 5초이므로 그보다 충분히 짧아야 합니다.
        self.declare_parameter('marker_publish_period', 0.5)

        self.use_mock = self.get_parameter('use_mock').value
        self.detection_threshold = self.get_parameter('detection_threshold').value
        self.camera_frame = self.get_parameter('camera_frame').value
        self.map_frame = self.get_parameter('map_frame').value

        self.jetson_camera_frame = self.get_parameter('jetson_camera_frame').value
        self.jetson_focal_length = self.get_parameter('jetson_focal_length').value
        self.jetson_frame_width = self.get_parameter('jetson_frame_width').value
        self.jetson_frame_height = self.get_parameter('jetson_frame_height').value

        principal_x = self.get_parameter('jetson_principal_x').value
        principal_y = self.get_parameter('jetson_principal_y').value
        self.jetson_principal_x = (principal_x if principal_x > 0
                                   else self.jetson_frame_width / 2.0)
        self.jetson_principal_y = (principal_y if principal_y > 0
                                   else self.jetson_frame_height / 2.0)

        self.max_events = self.get_parameter('max_events').value
        self.event_retention_sec = self.get_parameter('event_retention_sec').value
        self.marker_publish_period = self.get_parameter('marker_publish_period').value
        
        # CV bridge
        self.bridge = CvBridge()
        
        # TF listener
        self.tf_buffer = tf2_ros.Buffer()
        self.tf_listener = tf2_ros.TransformListener(self.tf_buffer, self)
        
        # Initialize camera parameters
        self.camera_info_received = False
        self.fx = 554.25
        self.fy = 554.25
        self.cx = 320.0
        self.cy = 240.0
        self.width = 640
        self.height = 480
        
        # Data storage
        self.depth_image = None
        self.latest_rgb_image = None
        
        # Gazebo state storage for mock detection
        self.gazebo_models = {}
        
        # List of active event markers (to avoid duplicate notifications and manage marker history)
        # Key: event_id (e.g. "fire_0"), Value: {x, y, z, label, time}
        self.detected_events_db = {}
        self.marker_id_counter = 0
        
        # Publishers
        self.marker_pub = self.create_publisher(MarkerArray, '/event_markers', 10)
        self.event_pub = self.create_publisher(String, '/detected_events', 10)
        
        # Subscriptions
        self.create_subscription(CameraInfo, '/camera/camera_info', self.camera_info_callback, 10)
        self.create_subscription(Image, '/camera/image_raw', self.image_callback, 10)
        self.create_subscription(Image, '/camera/depth/image_raw', self.depth_callback, 10)

        # 젯슨(손준영) 실물 카메라 탐지 결과 - vision_inference_node.py가 UDP:9999로 받은 걸
        # 그대로 재발행하는 /safety_status 토픽을 구독해서 지도 위 이벤트로 해석
        self.create_subscription(String, '/safety_status', self.safety_status_callback, 10)

        if self.use_mock:
            self.create_subscription(ModelStates, '/gazebo/model_states', self.gazebo_states_callback, 10)
            self.get_logger().info("Event Detector initialized in MOCK simulation mode.")
        else:
            self.init_real_detector()
            
        # Timer to run detection at ~5 Hz to save CPU
        self.create_timer(0.2, self.detection_timer_callback)

        # 마커 발행은 별도 타이머로 분리합니다.
        #
        # 예전에는 탐지가 처리될 때마다 _publish_markers() 를 불렀는데, /safety_status
        # 는 젯슨 프레임마다(최대 30Hz) 오므로 초당 30번씩 "누적된 모든 이벤트"의
        # 마커를 새로 만들어 발행했습니다. 이벤트가 50개면 초당 3000개의 Marker 를
        # 만드는 셈입니다. RViz 가 보여주는 그림은 그대로인데 CPU 와 DDS 대역폭만
        # 먹습니다. 발행 주기를 고정하면 탐지가 아무리 자주 와도 부하가 일정합니다.
        self.create_timer(self.marker_publish_period, self._publish_markers)

    def init_real_detector(self):
        try:
            import torch
            from transformers import Owlv2Processor, Owlv2ForObjectDetection
            self.get_logger().info("Loading OWL-ViT model (this might take a few seconds)...")
            self.device = "cuda" if torch.cuda.is_available() else "cpu"
            self.processor = Owlv2Processor.from_pretrained("google/owlv2-base-patch16-ensemble")
            self.model = Owlv2ForObjectDetection.from_pretrained("google/owlv2-base-patch16-ensemble").to(self.device)
            # 라벨 문자열은 젯슨(ai_inference_sender.py)의 text_prompt 와 같아야 합니다.
            # 이 이름 그대로 하류로 흘러가고, common/schema.py 의 classify_risk() 가
            # ALWAYS_DANGER_OBJECTS 집합과 "문자열 그대로" 비교하기 때문입니다.
            # 예전에는 여기가 "person without helmet", 아래 mock 은 "no safety helmet",
            # 젯슨은 "person with no helmet" 이라 같은 개념에 철자가 셋이었습니다.
            # 젯슨 text_prompt 와 같은 클래스여야 합니다. 다르면 시뮬레이션에서만
            # 잡히거나 반대로 실물에서만 잡히는 클래스가 생겨, 두 환경의 결과를
            # 비교할 수 없게 됩니다. "person wearing helmet" 은 정상 상태를
            # 걸러내기 위한 것이라 아래에서 건너뜁니다.
            self.queries = ["fire", "person with no helmet", "person",
                            "vehicle", "person wearing helmet"]
            self.get_logger().info(f"OWL-ViT successfully loaded on {self.device}!")
        except Exception as e:
            self.get_logger().error(f"Failed to load OWL-ViT: {str(e)}. Falling back to MOCK mode.")
            self.use_mock = True
            self.create_subscription(ModelStates, '/gazebo/model_states', self.gazebo_states_callback, 10)

    def camera_info_callback(self, msg):
        if not self.camera_info_received:
            # Extract intrinsics
            self.fx = msg.k[0]
            self.fy = msg.k[4]
            self.cx = msg.k[2]
            self.cy = msg.k[5]
            self.width = msg.width
            self.height = msg.height
            self.camera_info_received = True
            self.get_logger().info(f"Camera Info received: fx={self.fx}, fy={self.fy}, cx={self.cx}, cy={self.cy}, size={self.width}x{self.height}")

    def image_callback(self, msg):
        try:
            self.latest_rgb_image = self.bridge.imgmsg_to_cv2(msg, "bgr8")
        except Exception as e:
            self.get_logger().error(f"RGB Image conversion failed: {str(e)}")

    def depth_callback(self, msg):
        try:
            # Support both float32 (meters) and uint16 (millimeters)
            if msg.encoding == '32FC1':
                self.depth_image = self.bridge.imgmsg_to_cv2(msg, "32FC1")
            elif msg.encoding == '16UC1':
                depth_cv = self.bridge.imgmsg_to_cv2(msg, "16UC1")
                self.depth_image = depth_cv.astype(np.float32) / 1000.0
            else:
                self.depth_image = self.bridge.imgmsg_to_cv2(msg, msg.encoding).astype(np.float32)
        except Exception as e:
            self.get_logger().error(f"Depth Image conversion failed: {str(e)}")

    def gazebo_states_callback(self, msg):
        # Cache Gazebo model coordinates
        for idx, name in enumerate(msg.name):
            if 'fire_event_target' in name or 'helmet_violation_event_target' in name:
                self.gazebo_models[name] = msg.pose[idx]

    def detection_timer_callback(self):
        if self.latest_rgb_image is None or self.depth_image is None:
            return
            
        detections = []
        if self.use_mock:
            detections = self.run_mock_detection()
        else:
            detections = self.run_real_detection()
            
        # Process and publish detections
        self.process_detections(detections)

    def run_mock_detection(self):
        detections = []
        if not self.gazebo_models:
            return detections
            
        # Look up transform from map to camera link to get camera's pose in map coordinates
        try:
            transform = self.tf_buffer.lookup_transform(
                self.map_frame, 
                self.camera_frame, 
                rclpy.time.Time()
            )
            tx = transform.transform.translation.x
            ty = transform.transform.translation.y
            tz = transform.transform.translation.z
            rx = transform.transform.rotation.x
            ry = transform.transform.rotation.y
            rz = transform.transform.rotation.z
            rw = transform.transform.rotation.w
            
            cam_t = [tx, ty, tz]
            cam_q = [rx, ry, rz, rw]
        except Exception as e:
            # If TF fails, we cannot project
            return detections

        for model_name, pose in self.gazebo_models.items():
            # Get object 3D position in world (map) coordinates
            obj_t = [pose.position.x, pose.position.y, pose.position.z]
            
            # Map diff
            diff = [obj_t[0] - cam_t[0], obj_t[1] - cam_t[1], obj_t[2] - cam_t[2]]
            
            # Rotate into camera coordinate frame
            obj_cam = rotate_point_inverse(diff, cam_q)
            xc, yc, zc = obj_cam
            
            # Check if object is in front of the camera and within range (e.g. 4.5 meters)
            if zc <= 0.1 or zc > 4.5:
                continue
                
            # Project to image pixels
            u = int((xc * self.fx) / zc + self.cx)
            v = int((yc * self.fy) / zc + self.cy)
            
            # Check if inside screen limits
            if 0 <= u < self.width and 0 <= v < self.height:
                # Event type label
                # 젯슨의 text_prompt 와 같은 철자를 씁니다 (init_real_detector 주석 참고)
                label = "fire" if "fire" in model_name else "person with no helmet"
                
                # Mock bounding box dimensions
                w_box = int(120 / zc)
                h_box = int(180 / zc)
                xmin = max(0, u - w_box // 2)
                ymin = max(0, v - h_box // 2)
                xmax = min(self.width - 1, u + w_box // 2)
                ymax = min(self.height - 1, v + h_box // 2)
                
                detections.append({
                    'label': label,
                    'box': [xmin, ymin, xmax, ymax],
                    'score': 0.95,
                    'center_pixel': (u, v),
                    'depth': zc
                })
        return detections

    def run_real_detection(self):
        detections = []
        # Convert image to RGB format for transformers
        rgb_conv = cv2.cvtColor(self.latest_rgb_image, cv2.COLOR_BGR2RGB)
        
        # Run OWL-ViT
        inputs = self.processor(text=[self.queries], images=rgb_conv, return_tensors="pt").to(self.device)
        import torch
        with torch.no_grad():
            outputs = self.model(**inputs)
            
        # Target sizes are used to un-normalize boxes
        target_sizes = torch.Tensor([rgb_conv.shape[:2]]).to(self.device)
        results = self.processor.post_process_object_detection(outputs, threshold=self.detection_threshold, target_sizes=target_sizes)
        
        i = 0  # Only one image query
        boxes, scores, labels = results[i]["boxes"], results[i]["scores"], results[i]["labels"]
        
        for box, score, label_idx in zip(boxes, scores, labels):
            label = self.queries[label_idx.item()]
            if label == "person wearing helmet":
                continue # We only care about fire and helmet violation
                
            # Format box [xmin, ymin, xmax, ymax]
            box = [int(x) for x in box.tolist()]
            xmin, ymin, xmax, ymax = box
            
            # Bounding box center
            u = (xmin + xmax) // 2
            v = (ymin + ymax) // 2
            
            # Retrieve depth at center
            depth = self.get_robust_depth(u, v)
            if depth is not None and 0.2 < depth < 5.0:
                detections.append({
                    'label': label,
                    'box': box,
                    'score': score.item(),
                    'center_pixel': (u, v),
                    'depth': depth
                })
        return detections

    def get_robust_depth(self, u, v, window_size=5):
        # Look in a small window around (u,v) for a valid depth value
        half = window_size // 2
        r_min = max(0, v - half)
        r_max = min(self.height - 1, v + half)
        c_min = max(0, u - half)
        c_max = min(self.width - 1, u + half)
        
        sub_grid = self.depth_image[r_min:r_max+1, c_min:c_max+1]
        
        # Filter out NaN, Inf and 0
        valid = sub_grid[np.isfinite(sub_grid) & (sub_grid > 0.1)]
        if len(valid) > 0:
            return float(np.median(valid))
        return None

    def process_detections(self, detections):
        if not detections:
            return

        try:
            # Look up camera transform to map
            transform = self.tf_buffer.lookup_transform(
                self.map_frame,
                self.camera_frame,
                rclpy.time.Time()
            )
            tx = transform.transform.translation.x
            ty = transform.transform.translation.y
            tz = transform.transform.translation.z
            rx = transform.transform.rotation.x
            ry = transform.transform.rotation.y
            rz = transform.transform.rotation.z
            rw = transform.transform.rotation.w

            cam_t = [tx, ty, tz]
            cam_q = [rx, ry, rz, rw]
        except Exception as e:
            self.get_logger().warn(f"Cannot project detections: TF lookup failed: {str(e)}")
            return

        for det in detections:
            label = det['label']
            u, v = det['center_pixel']
            depth = det['depth']

            # Compute 3D camera coordinates
            xc = (u - self.cx) * depth / self.fx
            yc = (v - self.cy) * depth / self.fy
            zc = depth

            # Transform to map coordinates
            p_cam = [xc, yc, zc]
            p_map = rotate_point(p_cam, cam_q)
            map_x = p_map[0] + cam_t[0]
            map_y = p_map[1] + cam_t[1]
            map_z = p_map[2] + cam_t[2]

            self._register_event(label, map_x, map_y, map_z, distance=depth)

    def safety_status_callback(self, msg):
        try:
            payload = json.loads(msg.data)
        except (json.JSONDecodeError, TypeError):
            self.get_logger().warn(f"/safety_status 페이로드 파싱 실패: {msg.data!r}")
            return

        detections = payload.get('detections', [])
        if not detections:
            return

        try:
            transform = self.tf_buffer.lookup_transform(
                self.map_frame,
                self.jetson_camera_frame,
                rclpy.time.Time()
            )
            cam_t = [transform.transform.translation.x,
                     transform.transform.translation.y,
                     transform.transform.translation.z]
            cam_q = [transform.transform.rotation.x,
                     transform.transform.rotation.y,
                     transform.transform.rotation.z,
                     transform.transform.rotation.w]
        except Exception as e:
            self.get_logger().warn(f"Cannot project /safety_status detections: TF lookup failed: {str(e)}")
            return

        for det in detections:
            label = det.get('object', 'unknown')
            distance = det.get('distance_meter', -1.0)
            bbox = det.get('bbox')
            if distance is None or distance <= 0 or not bbox or len(bbox) != 4:
                continue  

            xmin, ymin, xmax, ymax = bbox
            u = (xmin + xmax) / 2.0
            v = (ymin + ymax) / 2.0

            xc = (u - self.jetson_principal_x) * distance / self.jetson_focal_length
            yc = (v - self.jetson_principal_y) * distance / self.jetson_focal_length
            zc = distance

            p_map = rotate_point([xc, yc, zc], cam_q)
            map_x = p_map[0] + cam_t[0]
            map_y = p_map[1] + cam_t[1]
            map_z = p_map[2] + cam_t[2]

            self._register_event(label, map_x, map_y, map_z, distance=distance)

    def _prune_events(self, keep_id=None):
        """이벤트 목록을 상한 안으로 유지한다.

        이력을 남기는 게 기본 방침이므로 시간 만료는 기본적으로 끕니다
        (event_retention_sec=0). 다만 개수 상한은 항상 걸어서, 긴 시연에서
        딕셔너리와 마커 발행량이 무한히 커지는 것만 막습니다.
        버릴 때는 가장 오래 재검출되지 않은 것부터 버립니다.

        keep_id 는 방금 등록한 이벤트입니다. 상한을 아주 작게(또는 0으로) 잡아도
        그것만은 남겨야 합니다 — 호출부가 바로 다음 줄에서 이 이벤트를 읽어
        /detected_events 로 발행하기 때문입니다.
        """
        now = time.time()

        if self.event_retention_sec and self.event_retention_sec > 0:
            expired = [k for k, v in self.detected_events_db.items()
                       if k != keep_id
                       and now - v['timestamp'] > self.event_retention_sec]
            for k in expired:
                del self.detected_events_db[k]
            if expired:
                self.get_logger().info(
                    f"이벤트 {len(expired)}건 만료 삭제 "
                    f"({self.event_retention_sec:.0f}초 이상 재검출 없음)")

        # 상한이 1보다 작게 들어와도 최소 1건은 유지합니다.
        limit = max(1, int(self.max_events))
        overflow = len(self.detected_events_db) - limit
        if overflow > 0:
            candidates = [kv for kv in self.detected_events_db.items()
                          if kv[0] != keep_id]
            oldest = sorted(candidates, key=lambda kv: kv[1]['timestamp'])[:overflow]
            for k, _v in oldest:
                del self.detected_events_db[k]
            if oldest:
                self.get_logger().warn(
                    f"이벤트가 상한({limit})을 넘어 오래된 {len(oldest)}건을 버렸습니다. "
                    f"상한을 늘리려면 max_events 파라미터를 조정하세요.")

    def _register_event(self, label, map_x, map_y, map_z, distance=None):
        """탐지 좌표 하나를 기존 이벤트와 중복 병합하거나 새로 등록하고 /detected_events로 발행"""
        # Deduplicate events: group by distance threshold (e.g. 1.0m)
        event_id = None
        for key, val in self.detected_events_db.items():
            dist = np.sqrt((val['x'] - map_x)**2 + (val['y'] - map_y)**2)
            if dist < 1.0 and val['label'] == label:
                event_id = key
                break

        if event_id is None:
            # Create a new event
            self.marker_id_counter += 1
            event_id = f"{label}_{self.marker_id_counter}"
            self.detected_events_db[event_id] = {
                'id': event_id,
                'label': label,
                'x': map_x,
                'y': map_y,
                'z': map_z,
                # mock_event_bridge.py가 대시보드/RAG 파이프라인의 표준 UDP
                # payload(distance_meter)를 만들 때 사용한다. 실물/목업 모두
                # 카메라로부터의 거리이므로 같은 의미를 가진다.
                'distance_meter': float(distance) if distance is not None else -1.0,
                'time': time.strftime("%H:%M:%S"),
                'timestamp': time.time(),
                'notified': False
            }
            self.get_logger().info(f"NEW EVENT DETECTED: {label} at Map: ({map_x:.2f}, {map_y:.2f}, {map_z:.2f})")
            # 새로 늘어났을 때만 정리하면 됩니다 (기존 이벤트 갱신은 개수를 안 늘림).
            self._prune_events(keep_id=event_id)
        else:
            # Update position with moving average to smooth noise
            alpha = 0.2
            self.detected_events_db[event_id]['x'] = (1 - alpha) * self.detected_events_db[event_id]['x'] + alpha * map_x
            self.detected_events_db[event_id]['y'] = (1 - alpha) * self.detected_events_db[event_id]['y'] + alpha * map_y
            self.detected_events_db[event_id]['z'] = (1 - alpha) * self.detected_events_db[event_id]['z'] + alpha * map_z
            if distance is not None:
                self.detected_events_db[event_id]['distance_meter'] = float(distance)
            self.detected_events_db[event_id]['timestamp'] = time.time()

        # Publish the notification if not notified yet or periodically
        event_data = self.detected_events_db[event_id]
        msg = String()
        msg.data = json.dumps(event_data)
        self.event_pub.publish(msg)

    def _publish_markers(self):
        """누적된 이벤트를 RViz 마커로 발행한다. 고정 주기 타이머가 부릅니다.

        탐지 콜백에서 직접 부르지 않는 이유는 __init__ 의 타이머 주석 참고 —
        /safety_status 가 최대 30Hz 로 들어와서 발행량이 그대로 따라 올라갔습니다.

        이벤트는 여기서 지우지 않습니다. 지도에 이력을 남기는 게 방침이고,
        목록 크기는 _prune_events() 가 상한으로 관리합니다.
        """
        if not self.detected_events_db:
            return

        marker_array = MarkerArray()
        for event_id, event in self.detected_events_db.items():
            # Build visualization marker
            marker = Marker()
            marker.header.frame_id = self.map_frame
            marker.header.stamp = self.get_clock().now().to_msg()
            marker.ns = "detected_events"
            
            # Separate ID hashing
            marker.id = hash(event_id) % 2147483647
            marker.type = Marker.SPHERE
            marker.action = Marker.ADD
            
            marker.pose.position.x = event['x']
            marker.pose.position.y = event['y']
            marker.pose.position.z = event['z'] + 0.3 # Lift marker slightly above ground
            marker.pose.orientation.w = 1.0
            
            marker.scale.x = 0.5
            marker.scale.y = 0.5
            marker.scale.z = 0.5
            
            # Distinct colors: Red for Fire, Yellow/Orange for Safety Helmet Violation
            if event['label'] == 'fire':
                marker.color.r = 1.0
                marker.color.g = 0.0
                marker.color.b = 0.0
                marker.color.a = 0.9
            else:
                marker.color.r = 1.0
                marker.color.g = 0.8
                marker.color.b = 0.0
                marker.color.a = 0.9
                
            marker.lifetime = rclpy.duration.Duration(seconds=5.0).to_msg() # Persistent but updated
            marker_array.markers.append(marker)

            # Text Label Marker
            text_marker = Marker()
            text_marker.header.frame_id = self.map_frame
            text_marker.header.stamp = self.get_clock().now().to_msg()
            text_marker.ns = "detected_events_text"
            text_marker.id = (hash(event_id) + 10000) % 2147483647
            text_marker.type = Marker.TEXT_VIEW_FACING
            text_marker.action = Marker.ADD
            
            text_marker.pose.position.x = event['x']
            text_marker.pose.position.y = event['y']
            text_marker.pose.position.z = event['z'] + 0.7 # Lift text above sphere
            text_marker.pose.orientation.w = 1.0
            
            text_marker.scale.z = 0.3
            text_marker.color.r = 1.0
            text_marker.color.g = 1.0
            text_marker.color.b = 1.0
            text_marker.color.a = 1.0
            text_marker.text = f"{event['label'].upper()} ({event['time']})"
            
            text_marker.lifetime = rclpy.duration.Duration(seconds=5.0).to_msg()
            marker_array.markers.append(text_marker)

        self.marker_pub.publish(marker_array)

def main(args=None):
    rclpy.init(args=args)
    node = EventDetectorNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        # launch가 SIGINT 처리 중 context를 먼저 닫은 Humble 조합에서는
        # 무조건 shutdown()하면 "rcl_shutdown already called"로 종료 코드가
        # 1이 된다. 이미 닫혔으면 정상 종료로 그대로 끝낸다.
        if rclpy.ok():
            try:
                rclpy.shutdown()
            except Exception:
                # ok() 확인 직후 launch의 signal handler가 context를 닫는
                # 짧은 경쟁 상태도 정상 종료로 취급한다.
                pass

if __name__ == '__main__':
    main()
