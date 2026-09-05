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


def send_kakaowork(message: str, app_key: str = "") -> bool:
    app_key = app_key or os.getenv("KAKAOWORK_APP_KEY", "")
    if not app_key:
        return False
    try:
        conv_id = _kakaowork_conversation(app_key, os.getenv("KAKAOWORK_USER_EMAIL", ""))
        if not conv_id:
            return False
        body = {"conversation_id": conv_id, "text": message[:4000]}
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
                # 대화방이 닫혔을 수 있다. 캐시를 버려 다음 건에서 다시 연다.
                _conv_cache.pop(app_key, None)
                print(f"[알림] 카카오워크 전송 거부: {resp.text[:200]}")
            return ok
        return False
    except Exception as exc:  # noqa: BLE001
        print(f"[알림] 카카오워크 전송 실패: {exc}")
        return False


def send(message: str, app_key: str = "") -> bool:
    """보냈으면 True. app_key 를 주면 그 봇으로, 안 주면 공용 봇으로 보낸다.

    앱키가 없으면 False 다. 알림 없이 상태만 저장되어 공지를 통째로
    놓치는 상황을 막기 위해 성공으로 치지 않는다.
    """
    print("-" * 60)
    print(message)
    ok = send_kakaowork(message, app_key)
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
