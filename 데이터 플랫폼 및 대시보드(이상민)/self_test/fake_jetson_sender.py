r"""
self_test/fake_jetson_sender.py

Jetson(ai_inference_sender.py) 없이도 pipeline/jetson_patrol_pipeline.py가
제대로 동작하는지 확인하기 위한 가짜 UDP 송신기.

라벨은 젯슨이 실제로 보내는 것과 맞춥니다
--------------------------------------------
    ai_inference_sender.py:  text_prompt = ["person with no helmet", "fire", "vehicle"]

예전에는 이 파일이 구버전 라벨(`human`, `obstacle`, `hazardous leak`)만 보냈습니다.
그래서 이 테스트가 전부 통과해도 **지금 실제로 오는 라벨이 올바르게 처리되는지는
검증되지 않았습니다.** 특히 안전모 미착용은 파이프라인이 "위험"으로 판정해야만
대응안이 생성되는데, 그 경로가 여기서 한 번도 확인되지 않았습니다.

지금은 현재 라벨 3종을 주로 보내되, 구버전·미등록 라벨도 몇 개 섞어서
`classify_risk()`의 폴백(미분류 -> 주의)이 살아 있는지 함께 확인합니다.

사용법
------
1. 터미널 A에서 파이프라인을 먼저 켭니다.
       python pipeline\jetson_patrol_pipeline.py --zone A
   "UDP 수신 대기 시작" 로그가 뜰 때까지 기다립니다.

   대응안 생성까지 확인하려면 RAG 파이프라인을 쓰세요.
       python pipeline\patrol_pipeline_rag.py --zone A

2. 터미널 B에서 이 스크립트를 실행합니다.
       python self_test\fake_jetson_sender.py

3. 아래 "기대" 열과 터미널 A의 "적재 완료 [A] ... (위험도: ...)" 로그를 대조합니다.
   전부 일치하면 성공입니다.

4. 대시보드를 열어 A 구역 카드가 위험/주의/정상으로 바뀌는지,
   상세 조회 표에 방금 보낸 데이터가 보이는지 확인합니다.

다른 PC의 파이프라인으로 보내려면
----------------------------------
    python self_test\fake_jetson_sender.py 203.0.113.40
"""

import json
import socket
import sys
import time

UDP_IP = "127.0.0.1"
UDP_PORT = 9998

# (설명, 기대 위험도, payload)
#
# 기대 위험도는 common/schema.py 의 classify_risk() 기준입니다.
#   ALWAYS_DANGER_OBJECTS    fire / hazardous leak / person with no helmet  -> 거리 무관 위험
#   ALWAYS_CAUTION_OBJECTS   obstacle                                       -> 거리 무관 주의
#   DISTANCE_BASED_OBJECTS   human / vehicle    1.5m 미만 위험 / 3.0m 미만 주의 / 그 이상 정상
#   그 외                                                                   -> 주의 (미분류)
FAKE_PACKETS = [
    # --- 현재 비전 모듈이 실제로 보내는 라벨 3종 ---
    ("화재 (거리 무관 위험)", "위험",
     {"detections": [{"object": "fire", "bbox": [100, 100, 200, 200],
                      "distance_meter": 4.0}]}),

    ("안전모 미착용 (거리 무관 위험 — 대응안 생성 대상)", "위험",
     {"detections": [{"object": "person with no helmet", "bbox": [150, 80, 300, 400],
                      "distance_meter": 1.85}]}),

    ("안전모 미착용 + 거리 측정 실패 (그래도 위험)", "위험",
     {"detections": [{"object": "person with no helmet", "bbox": [150, 80, 300, 400],
                      "distance_meter": -1.0}]}),

    ("차량 1.2m (1.5m 미만)", "위험",
     {"detections": [{"object": "vehicle", "bbox": [400, 200, 620, 460],
                      "distance_meter": 1.2}]}),

    ("차량 2.5m (1.5~3.0m)", "주의",
     {"detections": [{"object": "vehicle", "bbox": [400, 200, 620, 460],
                      "distance_meter": 2.5}]}),

    ("차량 8.0m (3.0m 이상)", "정상",
     {"detections": [{"object": "vehicle", "bbox": [400, 200, 620, 460],
                      "distance_meter": 8.0}]}),

    ("한 프레임에 2건 동시 (화재 + 차량 근접)", "위험, 위험",
     {"detections": [
         {"object": "fire", "bbox": [10, 10, 90, 90], "distance_meter": 3.0},
         {"object": "vehicle", "bbox": [300, 300, 480, 460], "distance_meter": 1.1},
     ]}),

    # --- 구버전/미등록 라벨 — 폴백이 살아 있는지 확인 ---
    ("[구버전 라벨] 작업자 1.0m", "위험",
     {"detections": [{"object": "person", "bbox": [150, 80, 300, 400],
                      "distance_meter": 1.0}]}),

    ("[구버전 라벨] 장애물 (거리 무관 주의)", "주의",
     {"detections": [{"object": "obstacle", "bbox": [400, 200, 500, 300],
                      "distance_meter": -1.0}]}),

    ("[미등록 라벨] forklift — 미분류 폴백", "주의",
     {"detections": [{"object": "forklift", "bbox": [200, 200, 400, 400],
                      "distance_meter": 1.0}]}),

    # bbox 가 아예 없거나 길이가 모자란 payload 도 죽지 않아야 합니다.
    ("[깨진 payload] bbox 없음", "위험",
     {"detections": [{"object": "fire", "distance_meter": 2.0}]}),
]


def main():
    dest_ip = sys.argv[1] if len(sys.argv) > 1 else UDP_IP

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    print(f"가짜 Jetson 패킷 {len(FAKE_PACKETS)}개를 {dest_ip}:{UDP_PORT} 로 전송합니다.")
    print("파이프라인 터미널의 '위험도' 와 아래 '기대' 를 대조하세요.\n")

    for i, (note, expect, packet) in enumerate(FAKE_PACKETS, start=1):
        sock.sendto(json.dumps(packet).encode(), (dest_ip, UDP_PORT))
        print(f"  [{i:2}/{len(FAKE_PACKETS)}] {note:44} 기대: {expect}")
        time.sleep(0.5)

    total = sum(len(p["detections"]) for _, _, p in FAKE_PACKETS)
    print(f"\n전송 완료. 탐지 {total}건 (패킷 {len(FAKE_PACKETS)}개).")
    print("파이프라인 터미널에 '적재 완료' 로그가 그만큼 찍혔는지 확인하세요.")
    print("대응안은 '위험' 등급에서만 생성되며, 같은 (구역, 객체) 조합은 30초간 억제됩니다.")


if __name__ == "__main__":
    main()
