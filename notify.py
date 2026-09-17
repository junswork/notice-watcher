"""알림 전송.

카카오워크 봇 DM 으로 보낸다. 게시판마다 다른 봇을 쓸 수 있도록 앱키를
인자로 받는다. 대화방 id 캐시가 앱키별로 나뉘어 있어 봇이 섞이지 않는다.
ticketing/notifier.py 에서 이 프로그램에 필요한 부분만 가져왔다.
경고음·반복 발송은 뺐다 — 깃허브 서버에는 스피커가 없고, 공지사항은
결제창처럼 5분 안에 끝나는 일이 아니다.

전송 성공 여부를 반드시 반환한다. 호출한 쪽은 실패한 알림의 상태를
저장하면 안 된다. 저장해 버리면 그 공지는 영영 다시 알려주지 않는다.
"""

import os
import time

import requests

KAKAOWORK_BASE = "https://api.kakaowork.com"
TIMEOUT = 10

_conv_cache: dict[str, str] = {}


def _kakaowork_conversation(app_key: str, want_email: str) -> str:
    """알림을 받을 사람과의 DM 대화방 id. 프로세스당 한 번만 조회한다."""
    if app_key in _conv_cache:
        return _conv_cache[app_key]

    headers = {"Authorization": f"Bearer {app_key}"}
    resp = requests.get(
        f"{KAKAOWORK_BASE}/v1/users.list", headers=headers, timeout=TIMEOUT
    )
    users = (resp.json() or {}).get("users") or []
    if not users:
        print(f"[알림] 카카오워크 멤버 조회 실패 (status={resp.status_code})")
        return ""

    if want_email:
        target = next((u for u in users if want_email.lower() in str(u).lower()), None)
        if target is None:
            print(f"[알림] 카카오워크: '{want_email}' 를 못 찾았습니다.")
            return ""
    else:
        # 워크스페이스에 사람이 여럿이면 엉뚱한 사람에게 간다. 누구인지 찍어 둔다.
        target = users[0]
        if len(users) > 1:
            print(
                f"[알림] 카카오워크: 멤버 {len(users)}명 중 '{target.get('name')}' "
                f"에게 보냅니다. KAKAOWORK_USER_EMAIL 로 지정하세요."
            )

    resp = requests.post(
        f"{KAKAOWORK_BASE}/v1/conversations.open",
        headers=headers,
        json={"user_id": target["id"]},
        timeout=TIMEOUT,
    )
    conv_id = ((resp.json() or {}).get("conversation") or {}).get("id", "")
    if not conv_id:
        print(f"[알림] 카카오워크 대화방 열기 실패: {resp.text[:120]}")
        return ""
    _conv_cache[app_key] = conv_id
    return conv_id


def send_kakaowork(message: str, app_key: str = "", blocks: list | None = None) -> bool:
    """blocks 를 주면 서식 있는 말풍선으로, 안 주면 그냥 글자로 보낸다.

    blocks 를 보낼 때도 text 는 반드시 같이 보낸다. 카카오워크는 그 값을
    푸시 알림과 대화방 목록에 쓴다 — 빼면 폰 잠금화면에 아무것도 안 뜬다.
    """
    app_key = app_key or os.getenv("KAKAOWORK_APP_KEY", "")
    if not app_key:
        return False
    try:
        conv_id = _kakaowork_conversation(app_key, os.getenv("KAKAOWORK_USER_EMAIL", ""))
        if not conv_id:
            return False
        body = {"conversation_id": conv_id, "text": message[:4000]}
        if blocks:
            body["blocks"] = blocks
        headers = {"Authorization": f"Bearer {app_key}"}
        for attempt in range(2):
            resp = requests.post(
                f"{KAKAOWORK_BASE}/v1/messages.send",
                headers=headers,
                json=body,
                timeout=TIMEOUT,
            )
            if resp.status_code == 429:
                time.sleep(2.0)
                continue
            ok = bool((resp.json() or {}).get("success", False))
            if not ok:
                print(f"[알림] 카카오워크 전송 거부: {resp.text[:200]}")
                if blocks:
                    # 서식이 거부되면 글자만이라도 보낸다. 보기 좋으라고 넣은
                    # 것 때문에 공지를 통째로 놓치면 본말이 전도된다.
                    print("[알림] 서식 없이 다시 보냅니다.")
                    return send_kakaowork(message, app_key, None)
                # 대화방이 닫혔을 수 있다. 캐시를 버려 다음 건에서 다시 연다.
                _conv_cache.pop(app_key, None)
            return ok
        return False
    except Exception as exc:  # noqa: BLE001
        print(f"[알림] 카카오워크 전송 실패: {exc}")
        return False


def send(
    message: str, app_key: str = "", blocks: list | None = None, test: bool = False
) -> bool:
    """보냈으면 True. app_key 를 주면 그 봇으로, 안 주면 공용 봇으로 보낸다.

    test 를 주면 눈에 띄게 표시한다. 표시가 없으면 받는 사람이 진짜 알림과
    구별할 수 없다 — 새벽에 '감시 실패' 시험 메시지를 받고 놀란 적이 있다.

    앱키가 없으면 False 다. 알림 없이 상태만 저장되어 공지를 통째로
    놓치는 상황을 막기 위해 성공으로 치지 않는다.
    """
    if test:
        message = "[테스트] " + message
        note = {"type": "text", "text": "※ 테스트입니다. 실제 상황이 아닙니다."}
        if blocks and blocks[0].get("type") == "header":
            # 머리 블록은 맨 위에 하나만 올 수 있다. 하나 더 붙이면 말풍선
            # 전체가 거부된다(실측). 있는 것을 고쳐 쓴다. 글자 수 상한은 20자.
            head = dict(blocks[0])
            head["text"] = ("[테스트] " + head.get("text", ""))[:20]
            head["style"] = "yellow"
            blocks = [head, note] + blocks[1:]
        else:
            blocks = [
                {"type": "header", "text": "테스트", "style": "yellow"},
                note,
            ] + (blocks or [])
    print("-" * 60)
    print(message)
    ok = send_kakaowork(message, app_key, blocks)
    if not ok:
        print("[알림] 보내지 못했습니다.")
    return ok


if __name__ == "__main__":
    # boards.json 의 게시판마다 그 봇으로 한 통씩 보낸다. 봇을 새로 만들었을 때
    # 앱키를 제자리에 넣었는지, 엉뚱한 봇으로 가지 않는지 여기서 걸러진다.
    import json
    import sys

    from dotenv import load_dotenv

    load_dotenv()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    boards = json.load(open("boards.json", encoding="utf-8"))
    failed = []
    for board in boards:
        name, env_name = board["name"], board.get("bot", "")
        key = os.getenv(env_name, "") if env_name else ""
        which = env_name if key else "KAKAOWORK_APP_KEY(공용)"
        ok = send(f"[{name}] 알림 설정 확인. 보낸 봇 이름이 맞는지 봐주세요.", key)
        print(f"  {name} <- {which}: {'성공' if ok else '실패'}")
        if not ok:
            failed.append(name)
    print("모두 성공" if not failed else f"실패: {failed}")
    raise SystemExit(1 if failed else 0)
