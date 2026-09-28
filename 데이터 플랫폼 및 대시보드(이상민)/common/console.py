"""
콘솔 출력 인코딩 보정 (common/console.py)

왜 이 파일이 필요한가
----------------------
Windows 의 기본 콘솔 코드페이지는 cp949 입니다. 그런데 이 저장소의 스크립트는
로그와 안내 문구에 이모지(📤 ⚠ 🤖)와 em 대시(—)를 씁니다. cp949 는 이 문자들을
인코딩하지 못하므로, print() 한 줄에서 UnicodeEncodeError 가 나고 **프로세스가
그대로 죽습니다.**

실제로 이런 일이 있었습니다.

    $ python admin/manage_smart_factory_db.py
      File "admin/manage_smart_factory_db.py", line 278, in main_menu
        print(" \U0001f916 스마트 팩토리 순찰 로봇 DB 관리 프로그램")
    UnicodeEncodeError: 'cp949' codec can't encode character '\U0001f916'

메뉴가 뜨기도 전에 죽어서, DB 관리 CLI 를 Windows 에서 아예 쓸 수 없었습니다.
자체 테스트 5종도 같은 이유로 중간에 크래시했습니다 — 로직은 전부 통과하는데
출력 계층에서 죽는 것이라, 증상만 보면 "테스트가 실패한다"로 오해하게 됩니다.

왜 이모지를 지우지 않는가
--------------------------
지울 수도 있지만, 그러면 사람이 로그를 훑을 때 위험 표시(⚠)와 성공 표시(✅)를
한눈에 구분하던 장점을 잃습니다. 게다가 문자를 하나 새로 쓸 때마다 "이건 cp949 에
있나?"를 매번 따져야 합니다. 출력 스트림을 UTF-8 로 한 번 바꿔두는 편이
근본적이고, 앞으로 어떤 문자를 써도 안전합니다.

사용법
------
진입점(직접 실행되는 스크립트) 맨 위에서 한 번만 부르면 됩니다.

    from common.console import enable_utf8_console
    enable_utf8_console()

라이브러리 모듈에서는 부르지 마세요. 전역 상태를 바꾸는 함수라, import 만 해도
동작이 달라지는 일은 없어야 합니다.
"""

import sys


def enable_utf8_console() -> bool:
    """stdout/stderr 를 UTF-8 로 다시 엽니다. 성공하면 True.

    errors="replace" 를 함께 주는 이유는, 만에 하나 재설정이 통하지 않는
    터미널에서도 프로그램이 죽는 대신 물음표로 대체되고 계속 돌게 하기
    위해서입니다. **로그 한 줄 때문에 파이프라인이 멈추는 것보다는 글자가
    깨지는 편이 낫습니다.**

    stdout 이 TextIOWrapper 가 아닌 경우(일부 테스트 러너가 교체함)에는
    reconfigure 가 없으므로 조용히 아무것도 하지 않습니다.
    """
    ok = True
    for stream in (sys.stdout, sys.stderr):
        if stream is None or not hasattr(stream, "reconfigure"):
            ok = False
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            # 이미 닫혔거나 재설정할 수 없는 스트림. 여기서 예외를 올리면
            # 이 함수를 부른 진입점이 통째로 죽으므로 삼킵니다.
            ok = False
    return ok
