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
from urllib.parse import urljoin

import requests

LIST_TIMEOUT = 180   # 목록 조회가 아주 느리다(실측). 짧게 잡으면 매번 실패한다.
LOGIN_TIMEOUT = 90   # 사람이 브라우저로 해도 15초쯤 걸린다(실측).
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
    page = session.get(LOGIN_URL, timeout=LOGIN_TIMEOUT).text
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


def _inputs(form_html: str) -> dict:
    """폼 안의 input 값을 모은다. name 과 value 의 순서가 뒤바뀐 칸도 있다."""
    out = {}
    for tag in re.findall(r"<input[^>]*>", form_html, re.I):
        name = re.search("name=[\"']([^\"']+)", tag, re.I)
        if not name:
            continue
        value = re.search("value=[\"']([^\"']*)", tag, re.I)
        out[name.group(1)] = value.group(1) if value else ""
    return out


def follow_auto_submit(
    session: requests.Session, resp, max_hops: int = 4, trace: list | None = None
):
    """응답이 '자동 제출 폼' 이면 그 폼을 대신 보낸다.

    통합 로그인은 도메인을 넘나들 때 숨은 폼이 담긴 페이지를 돌려주고,
    브라우저의 자바스크립트가 그걸 곧바로 다시 보내는 식으로 이어간다.
    우리는 자바스크립트를 돌리지 않으므로 그 한 단계에서 멈춰 버린다.
    폼을 찾아 그대로 보내 주면 브라우저와 같은 길을 간다.
    """
    for _ in range(max_hops):
        if trace is not None:
            trace.append(resp)
        html = resp.text
        # 자동 제출 페이지는 폼 하나만 있고 실려 나가자마자 스스로 보낸다.
        # 이 표시가 없으면 그냥 도착한 화면이다. 로그인 뒤 홈 화면에도 검색창
        # 같은 폼이 있어서, 이 검사가 없으면 홈을 몇 번씩 다시 부른다(실측).
        if not re.search(r"\.submit\(\)|onload\s*=", html, re.I):
            return resp
        form = re.search(r"<form[^>]*>.*?</form>", html, re.S | re.I)
        # 사람이 채우는 로그인 창에는 자동 제출이 없다. 눈에 보이는 입력칸이
        # 있으면 여기서 멈춘다 — 아니면 빈 아이디로 로그인을 다시 시도하게 된다.
        if not form or re.search("type=[\"']?(text|password)", form.group(0), re.I):
            return resp
        tag = re.search(r"<form[^>]*>", form.group(0), re.I).group(0)
        action = re.search("action=[\"']([^\"']*)", tag, re.I)
        target = urljoin(resp.url, action.group(1)) if action else resp.url
        method = re.search("method=[\"']?(\\w+)", tag, re.I)
        fields = _inputs(form.group(0))
        if (method.group(1).lower() if method else "get") == "post":
            resp = session.post(target, data=fields, timeout=LOGIN_TIMEOUT)
        else:
            resp = session.get(target, params=fields, timeout=LOGIN_TIMEOUT)
    return resp


def login(session: requests.Session, trace: list | None = None) -> None:
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
        timeout=LOGIN_TIMEOUT,
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
        timeout=LOGIN_TIMEOUT,
    )
    resp.raise_for_status()
    resp = follow_auto_submit(session, resp, trace=trace)

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



# --- 목록 읽기 -------------------------------------------------------------

# 프로그램 한 건은 detailBtn 링크에서 시작해 다음 detailBtn 직전까지다.
# <li> 로 자르려 했더니 역량 표시가 <li> 로 또 들어 있어서 블록이 중간에
# 끊겼다(실측). 여는 태그를 세는 대신 항목의 시작점으로 자른다.
_SPLIT_RE = re.compile(r'(?=<a[^>]*class="detailBtn")')
_ANCHOR_RE = re.compile(r'<a[^>]*class="detailBtn"[^>]*>(.*?)</a>', re.S)
_SCORE_RE = re.compile(r'<span class="p_col">.*?</span>', re.S)
_DL_RE = re.compile(r"<dt>\s*(.*?)\s*</dt>\s*<dd>(.*?)</dd>", re.S)
_TARGET_RE = re.compile(r'<dl class="target">.*?<dd>(.*?)</dd>', re.S)
_APPLIED_RE = re.compile(
    r'<span class="current">\s*(\d+)\s*</span>\s*/\s*<span class="max">\s*(\d+)\s*</span>', re.S
)


def _text(fragment: str) -> str:
    t = re.sub(r"<[^>]+>", " ", fragment)
    return re.sub(r"\s+", " ", t.replace("&nbsp;", " ")).strip()


def _target(block: str) -> str:
    """신청대상. 학년·전공 칸이 따로라 '전체' 가 세 번 겹쳐 나온다(실측).

    칸마다 <span> 이 하나씩이라 그 단위로 읽고 겹치는 것을 지운다. 통째로
    글자만 뽑아 쉼표로 자르면 칸 사이가 쉼표일 때만 맞고 공백일 때는 틀린다.
    """
    dd = _TARGET_RE.search(block)
    if not dd:
        return ""
    spans = [_text(s) for s in re.findall(r"<span[^>]*>(.*?)</span>", dd.group(1), re.S)]
    return ", ".join(dict.fromkeys(s for s in spans if s))


def parse_list(page: str) -> list[dict]:
    """비교과 프로그램 목록을 읽는다.

    프로그램을 제목으로 구분한다. 목록에 있는 encSddpbSeq 는 이름 그대로
    암호화된 값이라 세션마다 달라질 수 있다. 그걸 열쇠로 삼았다가 값이
    바뀌면 매 실행이 '전부 새 프로그램' 이 되어 알림이 쏟아진다. 제목은
    그런 위험이 없다. 제목을 고치면 새 프로그램으로 보이지만, 이쪽은
    수정을 볼 필요가 없으니(요청) 그 편이 안전한 쪽으로 틀린다.
    """
    items = []
    for block in _SPLIT_RE.split(page):
        anchor = _ANCHOR_RE.search(block)
        if not anchor:
            continue
        # 제목 앞에 붙는 [70점] 같은 점수 표시는 떼어낸다.
        title = _text(_SCORE_RE.sub(" ", anchor.group(1)))
        if not title:
            continue

        # 항목마다 같은 이름표가 한 번씩만 나온다. 마지막 항목 뒤에는 페이지
        # 아래쪽이 딸려 오므로 먼저 나온 값을 쓴다.
        fields: dict[str, str] = {}
        for key, value in _DL_RE.findall(block):
            fields.setdefault(_text(key), _text(value))

        applied = _APPLIED_RE.search(block)
        items.append({
            "title": title,
            "org": fields.get("운영조직", ""),
            "apply_period": fields.get("신청기간", ""),
            "target": _target(block) or fields.get("신청대상", ""),
            # 신청 인원은 사람이 신청할 때마다 바뀐다. 알림에 같이 보여주면
            # 자리가 얼마나 남았는지 바로 안다. 다만 같고 다름을 가리는 데는
            # 쓰지 않는다 — 쓰면 한 명 신청할 때마다 '바뀐 프로그램' 이 된다.
            "applied": f"{applied.group(1)}/{applied.group(2)}명" if applied else "",
        })
    return items


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
