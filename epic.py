"""에픽폴리오(비교과) 감시 — 로그인이 필요한 게시판.

앞의 세 게시판과 달리 통합 계정 로그인을 통과해야 목록이 보인다.
로그인 창의 자바스크립트가 하는 일을 그대로 따라 한다.

  1. 로그인 페이지에서 RSA 공개키(모듈러스·지수)를 받는다. 페이지를 열 때마다
     새로 내려온다.
  2. 아이디와 비밀번호를 각각 RSA 로 암호화해 `/common/user/encSo.do` 에 보낸다.
     서버가 풀어서 포털이 요구하는 형식으로 다시 만들어 돌려준다.
  3. 그 결과를 포털 로그인 API 로 넘긴다. 성공하면 세션 쿠키가 생긴다.

브라우저를 띄우지 않는다. 암호화는 파이썬 기본 기능(pow)으로 한다 — jsbn 이
쓰는 PKCS#1 v1.5 는 스무 줄이면 재현된다. 이걸 위해 라이브러리를 더 깔 이유가
없고, 깃허브에서 도는 시간도 늘리지 않는다.

지켜야 할 제약 (다른 세 게시판과 다르다):

  * **로그인 시도를 아끼자.** 로그인하는 접속이라 자주 두드리면 봇으로 몰려
    계정이 막힐 수 있다. 실행 주기를 다른 게시판과 같게 두지 말고 훨씬 길게
    잡는다. 로그인이 실패하면 **재시도하지 않는다** — 연속 실패는 계정 잠금으로
    이어진다. 다음 실행 때 한 번 더 해보는 것으로 충분하다.
  * **상세 페이지를 열지 않는다.** 이쪽은 게시물 수정을 볼 필요가 없다(요청).
    목록에서 새 글만 잡는다. 덕분에 실행당 접속이 로그인 포함 서너 번으로 끝난다.
  * **목록 조회가 아주 느리다.** 타임아웃을 넉넉히 잡는다.

미해결: 포털 로그인 POST 까지는 가는데 세션이 안 붙는다. 포털이 응답으로
자동 제출 폼을 돌려주고 그걸 자바스크립트가 보내는 구조로 보인다. requests 는
자바스크립트를 돌리지 않아 그 마지막 한 단계에서 끊긴다. 확인 필요.
"""

import os
import re
import secrets

import requests

LIST_TIMEOUT = 180   # 목록 조회가 아주 느리다(실측). 짧게 잡으면 매번 실패한다.
LOGIN_URL = "https://epic.seoultech.ac.kr/common/user/login.do"
ENC_URL = "https://epic.seoultech.ac.kr/common/user/encSo.do"


def rsa_encrypt_hex(text: str, modulus_hex: str, exponent_hex: str) -> str:
    """jsbn 의 RSAKey.encrypt 와 같은 결과를 낸다 (PKCS#1 v1.5 type 2, hex 출력).

    jsbn 은 결과 hex 를 키 길이에 맞춰 0 으로 채우지 않는다. 자릿수가 홀수일
    때만 앞에 0 을 하나 붙인다. 그 동작까지 맞춰야 서버가 받아준다.
    """
    n = int(modulus_hex, 16)
    e = int(exponent_hex, 16)
    k = (n.bit_length() + 7) // 8
    data = text.encode("utf-8")
    if k < len(data) + 11:
        raise ValueError("키에 비해 입력이 너무 깁니다")

    # 0x00 0x02 <0이 아닌 난수로 채움> 0x00 <본문>
    pad_len = k - len(data) - 3
    pad = bytearray()
    while len(pad) < pad_len:
        b = secrets.token_bytes(1)[0]
        if b:
            pad.append(b)
    block = b"\x00\x02" + bytes(pad) + b"\x00" + data

    c = pow(int.from_bytes(block, "big"), e, n)
    h = format(c, "x")
    return h if len(h) % 2 == 0 else "0" + h


def _public_key(page: str) -> tuple[str, str]:
    mod = re.search(r'id="RSAModulus"\s+value="([0-9a-fA-F]+)"', page)
    exp = re.search(r'id="RSAExponent"\s+value="([0-9a-fA-F]+)"', page)
    if not mod or not exp:
        raise RuntimeError("로그인 페이지에서 공개키를 못 찾았습니다. 화면이 바뀌었을 수 있습니다.")
    return mod.group(1), exp.group(1)


def encrypt_credentials(session: requests.Session, user_id: str, password: str) -> dict:
    """아이디·비밀번호를 서버가 포털에 넘길 수 있는 형태로 바꿔 온다."""
    page = session.get(LOGIN_URL, timeout=20).text
    modulus, exponent = _public_key(page)
    resp = session.post(
        ENC_URL,
        data={
            "encIi": rsa_encrypt_hex(user_id, modulus, exponent),
            "encPp": rsa_encrypt_hex(password, modulus, exponent),
        },
        headers={"X-Requested-With": "XMLHttpRequest"},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


PORTAL_LOGIN_URL = "https://portal.seoultech.ac.kr/user/seouolTechLoginApi.face"
LIST_URL = (
    "https://epic.seoultech.ac.kr/ptfol/imng/icmpNsbjtPgm/findIcmpNsbjtPgmList.do"
)

# 로그인은 한 실행에 한 번이면 된다. 게시판이 늘어도 다시 하지 않는다.
_logged_in = False


def _form_value(page: str, name: str) -> str:
    # value 를 홑따옴표로 감싼 칸과 겹따옴표로 감싼 칸이 섞여 있다.
    m = re.search('name="' + name + r'"[^>]*value=.([^\'"]*)', page)
    return m.group(1) if m else ""


def login(session: requests.Session) -> None:
    """통합 계정으로 로그인해 세션에 쿠키를 심는다.

    보호된 페이지를 먼저 열어 로그인 화면으로 튕기게 한다. 그래야 그 화면에
    이번 로그인용 rtnUrl 표가 박혀서 나온다. 로그인 주소로 곧장 들어가면
    이 표가 비어 있어 로그인 뒤 원래 페이지로 돌아오지 못한다.
    """
    global _logged_in
    if _logged_in:
        return

    user_id = os.getenv("EPIC_ID", "")
    password = os.getenv("EPIC_PW", "")
    if not user_id or not password:
        raise RuntimeError("EPIC_ID / EPIC_PW 가 없습니다. .env 또는 깃허브 Secrets 에 넣으세요.")

    page = session.get(LIST_URL, timeout=LIST_TIMEOUT).text
    if "RSAModulus" not in page:
        # 이미 로그인된 세션이면 목록이 그대로 온다.
        _logged_in = True
        return

    modulus, exponent = _public_key(page)
    rtn_url = _form_value(page, "rtnUrl")
    return_url = _form_value(page, "returnUrl")

    enc = session.post(
        ENC_URL,
        data={
            "encIi": rsa_encrypt_hex(user_id, modulus, exponent),
            "encPp": rsa_encrypt_hex(password, modulus, exponent),
        },
        headers={"X-Requested-With": "XMLHttpRequest"},
        timeout=20,
    ).json()
    if not enc.get("enc"):
        raise RuntimeError("아이디·비밀번호 암호화 단계에서 거부당했습니다.")

    resp = session.post(
        PORTAL_LOGIN_URL,
        data={
            "userId": enc["userId"],
            "password": enc["password"],
            "returnUrl": return_url,
            "rtnUrl": rtn_url,
        },
        timeout=20,
    )
    resp.raise_for_status()

    # 로그인이 됐는지는 보호된 페이지가 열리는지로만 확인한다. 포털이 실패를
    # 200 과 함께 화면으로 알려주기 때문에 상태 코드로는 판별되지 않는다.
    check = session.get(LIST_URL, timeout=LIST_TIMEOUT).text
    if "RSAModulus" in check:
        raise RuntimeError(
            "로그인 실패 — 아이디·비밀번호를 확인하세요. "
            "(포털 비밀번호 변경 주기가 지나 잠겼을 수도 있습니다)"
        )
    _logged_in = True


def fetch_list(session: requests.Session) -> str:
    login(session)
    return session.get(LIST_URL, timeout=LIST_TIMEOUT).text


if __name__ == "__main__":
    # 로그인해서 목록 화면을 파일로 떨어뜨린다. 파서를 쓰려면 먼저 이게 돼야 한다.
    import sys

    from dotenv import load_dotenv

    load_dotenv()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                    "AppleWebKit/537.36 Chrome/122 Safari/537.36"})
    html = fetch_list(s)
    out = sys.argv[1] if len(sys.argv) > 1 else "epic_list.html"
    io_open = open(out, "w", encoding="utf-8")
    io_open.write(html)
    io_open.close()
    print(f"로그인 성공. 목록 {len(html)}자를 {out} 에 저장했습니다.")
