"""Telethon StringSession 생성기 — channel_relay MTProto 백엔드용 1회 실행 도구.

DSInvResearch처럼 웹 프리뷰가 꺼진 공개 채널을 읽으려면 사용자 계정 세션이
필요하다. 이 스크립트를 **본인 PC에서 한 번** 실행해 세션 문자열을 만들고,
Railway env에 넣는다. 세션은 재로그인 없이 계속 유효 (로그아웃 전까지).

준비물
------
1. https://my.telegram.org → API development tools → App 생성
   → api_id (숫자) + api_hash (32자리 hex)
2. 본인 텔레그램 계정 전화번호 (인증 코드가 텔레그램 앱으로 옴)

사용
----
    pip install telethon
    python scripts/make_tg_session.py
    # api_id / api_hash / 전화번호 / 인증코드 입력 → 세션 문자열 출력

출력된 문자열을 Railway env 3종으로:
    TG_API_ID=<api_id>
    TG_API_HASH=<api_hash>
    TG_SESSION_STRING=<출력된 문자열>

⚠️ 세션 문자열 = 계정 로그인 그 자체. 절대 git commit·공유 금지.
   (CLAUDE.local.md나 Railway env에만.)
"""

from __future__ import annotations


def main() -> None:
    try:
        from telethon.sessions import StringSession
        from telethon.sync import TelegramClient
    except ImportError:
        raise SystemExit("telethon 미설치 — 먼저: pip install telethon")

    api_id = int(input("api_id (my.telegram.org에서 발급): ").strip())
    api_hash = input("api_hash: ").strip()

    with TelegramClient(StringSession(), api_id, api_hash) as client:
        session_str = client.session.save()
        me = client.get_me()
        print(f"\n✅ 로그인 성공: {me.first_name} (@{me.username or '-'})")
        print("\n=== Railway env에 넣을 값 ===")
        print(f"TG_API_ID={api_id}")
        print(f"TG_API_HASH={api_hash}")
        print(f"TG_SESSION_STRING={session_str}")
        print("\n⚠️ 위 세션 문자열은 계정 로그인과 동일 — 절대 공유·커밋 금지")


if __name__ == "__main__":
    main()
