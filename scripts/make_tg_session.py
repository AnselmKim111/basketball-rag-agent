"""Telethon StringSession 생성기 — channel_relay MTProto 백엔드용 1회 실행 도구.

DSInvResearch처럼 웹 프리뷰가 꺼진 공개 채널을 읽으려면 사용자 계정 세션이
필요하다. 세션 문자열을 만들어 Railway env에 넣는다. 세션은 재로그인 없이
계속 유효 (로그아웃 전까지).

준비물
------
1. https://my.telegram.org → API development tools → App 생성
   → api_id (숫자) + api_hash (32자리 hex)  (env `TG_API_ID` / `TG_API_HASH`로도 가능)
2. 본인 텔레그램 계정 전화번호 (인증 코드가 텔레그램 앱으로 옴)

사용 A — 대화형 (본인 PC)
------------------------
    pip install telethon
    python scripts/make_tg_session.py

사용 B — 비대화형 2단계 (Claude Code 컨테이너처럼 stdin 없는 환경)
--------------------------------------------------------------
코드 요청과 코드 입력 사이에 사람이 개입해야 하므로 두 번 실행한다.
1단계 상태(전화번호·phone_code_hash·임시 세션)는 state 파일에 저장된다.

    export TG_API_ID=... TG_API_HASH=...
    python scripts/make_tg_session.py send-code --phone +8210xxxxxxxx --state /tmp/tg_state.json
    # → 텔레그램 앱으로 코드 도착. 사용자가 코드를 전달하면:
    python scripts/make_tg_session.py sign-in --code 12345 --state /tmp/tg_state.json
    # (2단계 비밀번호가 켜진 계정이면 --password 추가)
    # → 마지막 줄에 TG_SESSION_STRING=... 출력. 상태 파일은 자동 삭제.

출력된 문자열을 Railway env 3종으로:
    TG_API_ID=<api_id>
    TG_API_HASH=<api_hash>
    TG_SESSION_STRING=<출력된 문자열>

⚠️ 세션 문자열 = 계정 로그인 그 자체. 절대 git commit·공유·채팅 출력 금지.
   (Railway env에만.) state 파일도 같은 등급 — sign-in 후 삭제됨.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _telethon():
    try:
        from telethon.sessions import StringSession
        from telethon.sync import TelegramClient
    except ImportError:
        raise SystemExit("telethon 미설치 — 먼저: pip install telethon")
    return StringSession, TelegramClient


def _api_creds(args) -> tuple[int, str]:
    api_id = args.api_id or os.environ.get("TG_API_ID", "").strip()
    api_hash = args.api_hash or os.environ.get("TG_API_HASH", "").strip()
    if not api_id or not api_hash:
        raise SystemExit("TG_API_ID / TG_API_HASH 필요 (env 또는 --api-id/--api-hash)")
    return int(api_id), api_hash


def interactive(args) -> None:
    StringSession, TelegramClient = _telethon()
    api_id = args.api_id or int(input("api_id (my.telegram.org에서 발급): ").strip())
    api_hash = args.api_hash or input("api_hash: ").strip()

    with TelegramClient(StringSession(), api_id, api_hash) as client:
        session_str = client.session.save()
        me = client.get_me()
        print(f"\n✅ 로그인 성공: {me.first_name} (@{me.username or '-'})")
        print("\n=== Railway env에 넣을 값 ===")
        print(f"TG_API_ID={api_id}")
        print(f"TG_API_HASH={api_hash}")
        print(f"TG_SESSION_STRING={session_str}")
        print("\n⚠️ 위 세션 문자열은 계정 로그인과 동일 — 절대 공유·커밋 금지")


def send_code(args) -> None:
    """1단계: 코드 요청. 임시 세션 + phone_code_hash를 state 파일에 저장."""
    StringSession, TelegramClient = _telethon()
    api_id, api_hash = _api_creds(args)
    client = TelegramClient(StringSession(), api_id, api_hash)
    client.connect()
    sent = client.send_code_request(args.phone)
    state = {
        "phone": args.phone,
        "phone_code_hash": sent.phone_code_hash,
        "session": client.session.save(),
    }
    client.disconnect()
    p = Path(args.state)
    p.write_text(json.dumps(state))
    try:
        p.chmod(0o600)
    except OSError:
        pass
    print(f"📨 코드 전송됨 → 텔레그램 앱 확인. 상태 저장: {p}")
    print("다음: python scripts/make_tg_session.py sign-in --code <코드> --state", p)


def sign_in(args) -> None:
    """2단계: 저장된 임시 세션으로 코드 입력 → 최종 세션 문자열 출력."""
    StringSession, TelegramClient = _telethon()
    api_id, api_hash = _api_creds(args)
    p = Path(args.state)
    if not p.exists():
        raise SystemExit(f"state 파일 없음: {p} — send-code 먼저")
    state = json.loads(p.read_text())
    client = TelegramClient(StringSession(state["session"]), api_id, api_hash)
    client.connect()
    try:
        client.sign_in(phone=state["phone"], code=args.code,
                       phone_code_hash=state["phone_code_hash"])
    except Exception as e:  # SessionPasswordNeededError 등
        if "password" in type(e).__name__.lower() or "password" in str(e).lower():
            if not args.password:
                raise SystemExit("2단계 비밀번호 필요 — --password 추가해 재실행 "
                                 "(state 파일은 유지됨)")
            client.sign_in(password=args.password)
        else:
            raise
    me = client.get_me()
    session_str = client.session.save()
    client.disconnect()
    try:
        p.unlink()
    except OSError:
        pass
    print(f"✅ 로그인 성공: {me.first_name} (@{me.username or '-'})", file=sys.stderr)
    # stdout 마지막 줄만 세션 — 파이프로 Railway upsert에 바로 넘길 수 있게.
    print(f"TG_SESSION_STRING={session_str}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--api-id", type=int, default=None)
    ap.add_argument("--api-hash", default=None)
    sub = ap.add_subparsers(dest="cmd")
    s1 = sub.add_parser("send-code", help="1단계: 인증코드 요청 (비대화형)")
    s1.add_argument("--phone", required=True, help="+8210xxxxxxxx 국제형식")
    s1.add_argument("--state", default="tg_login_state.json")
    s2 = sub.add_parser("sign-in", help="2단계: 코드 입력 → 세션 출력 (비대화형)")
    s2.add_argument("--code", required=True)
    s2.add_argument("--password", default=None, help="2단계 비밀번호 (설정된 계정만)")
    s2.add_argument("--state", default="tg_login_state.json")
    args = ap.parse_args()
    if args.cmd == "send-code":
        send_code(args)
    elif args.cmd == "sign-in":
        sign_in(args)
    else:
        interactive(args)


if __name__ == "__main__":
    main()
