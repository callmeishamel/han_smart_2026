"""
camera_stream_server.py

실행 위치: 젯슨 오린 나노 (카메라가 물리적으로 연결된 기기)
반드시 젯슨에서 실행해야 하는 이유: 카메라 캡처(cv2.VideoCapture)는
카메라가 물리적으로 붙어있는 기기에서만 가능함.

이 파일은 손준영 팀장의 ai_inference_sender.py와 별개로 순수 영상만
MJPEG로 내보내는 참고용 서버입니다. 실제로는 ai_inference_sender.py가
이미 자체적으로 8500 포트에 시각화 화면(bbox가 그려진 vis_frame)을
스트리밍하고 있으므로, 그 스트림을 그대로 대시보드에 연결해도 됩니다.
이 파일은 그 스트림이 없는 환경(테스트/예비용)에서 순수 카메라 영상만
필요할 때 사용합니다.

실행 방법 (젯슨에서):
    pip install flask opencv-python
    python camera_stream_server.py
"""

import os
import cv2
from flask import Flask, Response

app = Flask(__name__)

# common/schema.py에서 배운 것과 같은 패턴: 하드코딩 대신 환경변수로 설정
CAMERA_INDEX = int(os.environ.get("CAMERA_INDEX", "0"))
STREAM_PORT = int(os.environ.get("STREAM_PORT", "8090"))
JPEG_QUALITY = int(os.environ.get("JPEG_QUALITY", "80"))  # 0~100, 낮을수록 용량 작고 화질 낮음

camera = cv2.VideoCapture(CAMERA_INDEX)

if not camera.isOpened():
    raise RuntimeError(
        f"카메라(index={CAMERA_INDEX})를 열 수 없습니다. "
        f"연결 상태와 CAMERA_INDEX 값을 확인하세요. "
        f"(USB 카메라면 보통 0, CSI 카메라면 별도 GStreamer 파이프라인 필요)"
    )


def generate_frames():
    """
    카메라에서 프레임을 무한 반복으로 읽어서 JPEG로 인코딩한 뒤
    하나씩 내보내는(yield) 제너레이터 함수.
    """
    while True:
        success, frame = camera.read()
        if not success:
            break  # 카메라 연결이 끊기면 스트림 종료

        ok, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
        if not ok:
            continue

        frame_bytes = buffer.tobytes()

        # multipart/x-mixed-replace: 브라우저의 <img> 태그가 이 형식을 보면
        # 알아서 "계속 새 이미지로 교체"로 해석해줘서 동영상처럼 보임
        yield (
            b'--frame\r\n'
            b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n'
        )


@app.route('/video_feed')
def video_feed():
    return Response(
        generate_frames(),
        mimetype='multipart/x-mixed-replace; boundary=frame'
    )


@app.route('/health')
def health():
    """대시보드에서 연결 확인용으로 간단히 핑 날려볼 수 있는 엔드포인트"""
    return {"status": "ok", "camera_opened": camera.isOpened()}


if __name__ == '__main__':
    print(f"카메라 스트리밍 서버 시작: http://0.0.0.0:{STREAM_PORT}/video_feed")
    print("젯슨의 실제 IP는 터미널에서 'hostname -I' 로 확인하세요.")
    app.run(host='0.0.0.0', port=STREAM_PORT, threaded=True)
