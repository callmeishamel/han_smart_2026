#!/usr/bin/env python3
"""
================================================================================
파일명   : docs/apply_merge.py
목적     : 최신 통합본에 이번 브랜치 변경을 기계적으로 적용
버전     : v1.0
--------------------------------------------------------------------------------
왜 스크립트인가

  pipeline/patrol_pipeline_rag.py 는 최신 통합본에 TTS 송신, 억제 간격,
  대응안 스로틀이 들어가 있어 파일째로 교체할 수 없습니다. 그렇다고 사람이
  손으로 옮기면 그 기능을 빠뜨리기 쉽습니다.

  이 스크립트는 **추가만 합니다.** 기존 코드를 지우거나 옮기지 않으므로
  --tts, --suppress-seconds, ResponsePlanThrottle, TTS 송신부가 사라질 수
  없습니다. 확신할 수 없는 부분은 손대지 않고 "남은 수동 작업"으로
  보고합니다.

무엇을 하는가

  pipeline/patrol_pipeline_rag.py
    1. import os 확인 (없으면 추가)
    2. --udp-ip / --udp-port 기본값을 환경변수로 교체
    3. --use-llm 인자 추가 (없을 때만)
    4. --async-llm / --plan-queue-size 인자 추가 (없을 때만)
    5. RagResponseAgent(...) 에 use_llm=args.use_llm 추가 (없을 때만)

  rag/setup_knowledge_base.py, rag/embed_chunks.py   (--kb-split 일 때만)
    6. 접속 설정을 get_kb_db_config() 로 교체

무엇을 하지 않는가

  비동기 워커 배선(큐 생성, 수신 루프 분기, 종료 처리)과 TTS 송신부
  이동은 최신본의 루프 구조에 달려 있어 자동으로 판단할 수 없습니다.
  실행이 끝나면 그 부분을 병합가이드 4-3 기준으로 안내합니다.

사용법

    # 무엇이 바뀌는지만 보기 (파일을 건드리지 않음)
    python3 docs/apply_merge.py --repo /path/to/rag_upload --dry-run

    # 실제 적용 (원본은 .bak 으로 남습니다)
    python3 docs/apply_merge.py --repo /path/to/rag_upload

    # 지식베이스 DB 를 분리해 쓰는 경우
    python3 docs/apply_merge.py --repo /path/to/rag_upload --kb-split

  두 번 실행해도 안전합니다. 이미 적용된 항목은 건너뜁니다.
================================================================================
"""

import argparse
import difflib
import os
import py_compile
import re
import shutil
import sys
import tempfile

BAR = "=" * 74

# 적용 결과 집계
DONE, SKIP, WARN = [], [], []


def note(bucket, msg):
    bucket.append(msg)
    tag = {id(DONE): "[적용]", id(SKIP): "[건너뜀]", id(WARN): "[확인필요]"}[id(bucket)]
    print(f"  {tag:<9} {msg}")


# ==============================================================================
# 1. patrol_pipeline_rag.py
# ==============================================================================

def patch_import_os(src: str) -> str:
    if re.search(r"^import os\s*$", src, re.M):
        note(SKIP, "import os — 이미 있음")
        return src
    # 다른 import 뒤에 붙인다.
    m = re.search(r"^import \w+\s*$", src, re.M)
    if not m:
        note(WARN, "import 구문을 찾지 못했습니다. 파일 상단에 'import os' 를 "
                   "직접 추가하십시오")
        return src
    src = src[:m.end()] + "\nimport os" + src[m.end():]
    note(DONE, "import os 추가")
    return src


def patch_udp_defaults(src: str) -> str:
    """--udp-ip / --udp-port 의 default 만 환경변수 조회로 바꾼다."""
    changed = False

    if "PIPELINE_UDP_BIND" in src and "DASHBOARD_UDP_PORT" in src:
        note(SKIP, "UDP 기본값 — 이미 환경변수를 읽고 있음")
        return src

    # default="127.0.0.1" 형태를 찾는다. 인자 이름과 같은 add_argument 안에
    # 있는 default 만 바꾸도록 --udp-ip 문자열 이후 200자 안에서 찾는다.
    def _replace_default(text, flag, new_default):
        i = text.find(f'"{flag}"')
        if i < 0:
            i = text.find(f"'{flag}'")
        if i < 0:
            return text, False
        window = text[i:i + 400]
        m = re.search(r"default\s*=\s*[^,\)]+", window)
        if not m:
            return text, False
        s, e = i + m.start(), i + m.end()
        return text[:s] + new_default + text[e:], True

    src, ok_ip = _replace_default(
        src, "--udp-ip",
        'default=os.environ.get("PIPELINE_UDP_BIND", "0.0.0.0")')
    src, ok_port = _replace_default(
        src, "--udp-port",
        'default=int(os.environ.get("DASHBOARD_UDP_PORT", "9998"))')

    if ok_ip:
        note(DONE, "--udp-ip 기본값 -> PIPELINE_UDP_BIND (0.0.0.0)")
        changed = True
    else:
        note(WARN, "--udp-ip 인자를 찾지 못했습니다. 병합가이드 4-1 참조")
    if ok_port:
        note(DONE, "--udp-port 기본값 -> DASHBOARD_UDP_PORT (9998)")
        changed = True
    else:
        note(WARN, "--udp-port 인자를 찾지 못했습니다. 병합가이드 4-1 참조")

    if changed:
        # help 문자열에 남은 "기본 9999" 표기를 맞춘다.
        src, n = re.subn(r"기본 9999", "기본 9998, 환경변수 DASHBOARD_UDP_PORT",
                         src)
        if n:
            note(DONE, f"help 문자열의 기본 포트 표기 정정 ({n}곳)")

    # docstring 등 설명문에 남은 표기는 사람이 판단해야 한다.
    leftover = [ln.strip() for ln in src.split("\n")
                if "9999" in ln and "DASHBOARD_UDP_PORT" not in ln]
    if leftover:
        note(WARN, f"설명문에 9999 표기가 {len(leftover)}줄 남아 있습니다 "
                   f"(병합가이드 4-4): {leftover[0][:50]}")
    return src


def _last_add_argument_end(src: str):
    """main() 안 마지막 parser.add_argument(...) 호출의 끝 위치를 찾는다."""
    last = None
    for m in re.finditer(r"^([ \t]*)parser\.add_argument\(", src, re.M):
        indent = m.group(1)
        # 괄호 균형을 세어 호출 끝을 찾는다
        depth, i = 0, m.end() - 1
        while i < len(src):
            if src[i] == "(":
                depth += 1
            elif src[i] == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        if i < len(src):
            last = (i + 1, indent)
    return last


def patch_add_arguments(src: str) -> str:
    blocks = []

    if re.search(r"[\"']--use-llm[\"']", src):
        note(SKIP, "--use-llm — 이미 있음")
    else:
        blocks.append(
            '{i}parser.add_argument("--use-llm", action="store_true",\n'
            '{i}                    help="GEMMA 2B로 대응 지침 문장 생성 "\n'
            '{i}                         "(서버가 없으면 규칙 기반으로 자동 대체)")')

    if re.search(r"[\"']--async-llm[\"']", src):
        note(SKIP, "--async-llm — 이미 있음")
    else:
        blocks.append(
            '{i}parser.add_argument("--async-llm", action="store_true",\n'
            '{i}                    help="대응안 생성을 별도 스레드로 분리 "\n'
            '{i}                         "(LLM 생성 중에도 UDP 수신을 계속한다)")\n'
            '{i}parser.add_argument("--plan-queue-size", type=int, default=32,\n'
            '{i}                    help="비동기 대응안 대기 큐 크기 (기본 32)")')

    if not blocks:
        return src

    spot = _last_add_argument_end(src)
    if spot is None:
        note(WARN, "parser.add_argument 를 찾지 못했습니다. 인자 추가는 "
                   "병합가이드 4-2 / 4-3 을 보고 직접 하십시오")
        return src

    pos, indent = spot
    text = "\n" + "\n".join(b.format(i=indent) for b in blocks)
    src = src[:pos] + text + src[pos:]
    note(DONE, f"인자 추가 ({len(blocks)}개 블록)")
    return src


def patch_agent_use_llm(src: str) -> str:
    m = re.search(r"RagResponseAgent\s*\(", src)
    if not m:
        note(WARN, "RagResponseAgent( 호출을 찾지 못했습니다. "
                   "use_llm 전달을 직접 확인하십시오")
        return src

    depth, i = 0, m.end() - 1
    while i < len(src):
        if src[i] == "(":
            depth += 1
        elif src[i] == ")":
            depth -= 1
            if depth == 0:
                break
        i += 1
    call = src[m.start():i + 1]

    if "use_llm" in call:
        note(SKIP, "RagResponseAgent(use_llm=...) — 이미 전달 중")
        return src

    inner = call[call.index("(") + 1:-1]
    sep = "," if inner.strip() and not inner.rstrip().endswith(",") else ""
    new_call = call[:-1] + f"{sep} use_llm=args.use_llm)"
    src = src[:m.start()] + new_call + src[i + 1:]
    note(DONE, "RagResponseAgent 에 use_llm=args.use_llm 추가")
    return src


# ==============================================================================
# 2. 지식베이스 스크립트
# ==============================================================================

def patch_kb_config(src: str, name: str) -> str:
    if "get_kb_db_config" in src:
        note(SKIP, f"{name} — 이미 get_kb_db_config 사용")
        return src
    if "get_db_config" not in src:
        note(WARN, f"{name} — get_db_config 호출이 없습니다. 직접 확인하십시오")
        return src

    # import 줄에서 get_db_config 만 떼어 내고 kb_config import 를 추가한다.
    def _fix_import(m):
        names = [n.strip() for n in m.group(1).split(",")]
        rest = [n for n in names if n != "get_db_config"]
        lines = []
        if rest:
            lines.append(f"from common.schema import {', '.join(rest)}")
        lines.append("from rag.kb_config import get_kb_db_config")
        return "\n".join(lines)

    new = re.sub(r"^from common\.schema import ([^\n]+)$", _fix_import,
                 src, count=1, flags=re.M)
    if new == src:
        note(WARN, f"{name} — import 구문을 자동으로 못 고쳤습니다. "
                   "병합가이드 5절 참조")
        return src

    new = re.sub(r"\bget_db_config\s*\(\s*\)", "get_kb_db_config()", new)
    note(DONE, f"{name} — 접속 설정을 get_kb_db_config() 로 교체")
    return new


# ==============================================================================

def process(path: str, fn, dry_run: bool, label: str):
    if not os.path.isfile(path):
        note(WARN, f"{label} — 파일이 없습니다: {path}")
        return
    print(f"\n {label}")
    print(" " + "-" * 72)

    original = open(path, encoding="utf-8").read()
    src = original.replace("\r\n", "\n")
    if src != original:
        note(DONE, "CRLF -> LF 변환")

    src = fn(src)

    if src == original:
        print("  변경 없음")
        return

    # 문법 검사를 통과하지 못하면 쓰지 않는다.
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False,
                                     encoding="utf-8") as tf:
        tf.write(src)
        tmp = tf.name
    try:
        py_compile.compile(tmp, doraise=True)
    except py_compile.PyCompileError as exc:
        print(f"\n  [중단] 결과가 문법 검사를 통과하지 못해 쓰지 않았습니다.\n  {exc}")
        WARN.append(f"{label} — 자동 적용 실패, 병합가이드로 직접 반영하십시오")
        return
    finally:
        os.unlink(tmp)

    if dry_run:
        print("\n  --- 변경 내용 (dry-run, 파일은 그대로) ---")
        diff = difflib.unified_diff(
            original.splitlines(True), src.splitlines(True),
            fromfile=label, tofile=label + " (적용 후)", n=2)
        sys.stdout.writelines("  " + line for line in diff)
        return

    shutil.copy2(path, path + ".bak")
    with open(path, "w", encoding="utf-8") as f:
        f.write(src)
    print(f"  저장 완료 (원본: {os.path.basename(path)}.bak)")


def main():
    ap = argparse.ArgumentParser(
        description="최신 통합본에 이번 브랜치 변경을 기계적으로 적용합니다")
    ap.add_argument("--repo", required=True,
                    help="rag_upload 폴더 경로")
    ap.add_argument("--dry-run", action="store_true",
                    help="파일을 바꾸지 않고 변경 내용만 출력")
    ap.add_argument("--kb-split", action="store_true",
                    help="지식베이스 DB 를 운영 DB 와 분리해 쓰는 경우")
    args = ap.parse_args()

    repo = os.path.abspath(args.repo)
    print(BAR)
    print(" 브랜치 변경 적용")
    print(BAR)
    print(f" 대상: {repo}")
    if args.dry_run:
        print(" 모드: dry-run (파일을 바꾸지 않습니다)")

    def pipeline_fn(src):
        src = patch_import_os(src)
        src = patch_udp_defaults(src)
        src = patch_add_arguments(src)
        src = patch_agent_use_llm(src)
        return src

    process(os.path.join(repo, "pipeline", "patrol_pipeline_rag.py"),
            pipeline_fn, args.dry_run, "pipeline/patrol_pipeline_rag.py")

    if args.kb_split:
        for name in ("setup_knowledge_base.py", "embed_chunks.py"):
            process(os.path.join(repo, "rag", name),
                    lambda s, n=name: patch_kb_config(s, n),
                    args.dry_run, f"rag/{name}")

    # --- 요약 ---
    print()
    print(BAR)
    print(f" 적용 {len(DONE)}건 / 건너뜀 {len(SKIP)}건 / 확인필요 {len(WARN)}건")
    print(BAR)

    if WARN:
        print("\n 확인이 필요한 항목")
        for w in WARN:
            print(f"   - {w}")

    print("""
 남은 수동 작업 — 이 스크립트가 하지 않는 부분

   비동기 워커 배선과 TTS 송신부 이동은 최신본의 수신 루프 구조에
   달려 있어 자동으로 판단할 수 없습니다. --async-llm 을 쓰려면
   병합가이드 4-3 의 (2)(3)(4)(5) 를 직접 반영하십시오.

     (2) 워커 생성과 handle_plan 정의   시작 로그 뒤, 수신 루프 앞
     (3) 수신 루프에서 worker.submit()  스로틀 판정 뒤
     (4) finally 에서 worker.close()
     (5) 종료 요약 로그를 finally 로 이동

   TTS 는 handle_plan() 안으로 옮겨야 비동기에서도 음성이 나갑니다.
   그대로 두면 --async-llm 을 켰을 때 음성 송신이 빠집니다.

   반영하지 않아도 동기 경로(--use-llm 단독)는 정상 동작합니다.

 확인

   python3 -m py_compile pipeline/patrol_pipeline_rag.py
   python3 pipeline/patrol_pipeline_rag.py --help
   python3 self_test/test_response_agent.py
   python3 self_test/test_response_agent_llm.py
   python3 self_test/test_response_plan_worker.py

   --help 에 --tts 와 --suppress-seconds 가 그대로 보여야 합니다.
""")


if __name__ == "__main__":
    main()
