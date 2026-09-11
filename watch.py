"""공지사항 게시판 감시.

세 게시판(대학공지 / 학사공지 / 생활관공지)은 모두 같은 CMS 를 쓴다.
목록 페이지의 링크는 전부 `?do=commonview...&bidx=숫자` 꼴이고, 상세
페이지는 `table.tbl_view` 안에 제목·작성자·날짜·첨부파일·본문이 들어 있다.
그래서 게시판마다 파서를 따로 두지 않고 하나로 처리한다.

동작:
  1. 목록에서 게시물 id(bidx) 를 뽑는다.
  2. 각 상세 페이지를 받아 제목·첨부·본문의 해시를 만든다.
  3. 지난번 해시와 비교해 새 글과 수정된 글을 찾는다.
  4. 알림을 보내고, 보내기에 성공한 것만 상태에 기록한다.

상세 페이지까지 받는 이유: 이 게시판에는 '수정일' 항목이 없다. 본문만
고쳐도 목록에 보이는 제목·날짜는 그대로다. 목록만 봐서는 수정을 절대
알 수 없다.

조회수는 해시에 넣지 않는다. 조회수는 사람이 열어볼 때마다 바뀌므로
넣으면 매 실행이 '수정됨' 이 된다.
"""

import datetime
import difflib
import hashlib
import html as html_mod
import json
import os
import re
import sys
import time
from urllib.parse import urljoin

import requests

import epic
import notify

STATE_FILE = os.getenv("STATE_FILE", "state.json")
BOARDS_FILE = os.getenv("BOARDS_FILE", "boards.json")
KEYWORDS_FILE = os.getenv("KEYWORDS_FILE", "관심키워드.json")
# 실패 알림에서 바로 열 수 있게. 깃허브가 실행 중이면 저장소 이름을 알려준다.
ACTIONS_URL = (
    f"https://github.com/{os.getenv('GITHUB_REPOSITORY', 'junswork/notice-watcher')}"
    "/actions"
)
# 한 게시판에서 몇 개까지 상세 페이지를 확인할지. 목록 1페이지가 20~21건이고
# 맨 위 6~8건은 고정공지가 차지한다(실측). 25 면 고정공지가 몇 개든 1페이지를
# 통째로 덮으므로 감시가 반나절 멈췄다 재개돼도 그 사이 글을 놓치지 않는다.
# 아래 WATCH_MODE 가 "full" 일 때만 이만큼 상세를 연다. 새 글만 볼 때는
# 처음 보는 글의 상세만 열므로 평소엔 이 값과 무관하게 접속이 한 번이다.
DETAIL_LIMIT = int(os.getenv("DETAIL_LIMIT", "25"))
REQUEST_DELAY = float(os.getenv("REQUEST_DELAY", "0.3"))
# "new"  = 새 글만 본다. 목록 한 번 받고, 처음 보는 글의 상세만 연다.
#          평소엔 접속이 게시판당 한 번으로 끝나 자주 돌려도 부담이 없다.
# "full" = 목록에 걸린 글의 상세를 전부 다시 읽어 본문이 바뀌었는지 본다.
#          게시판당 접속이 스물몇 번이라 하루 몇 번만 돌린다.
FULL_SCAN = os.getenv("WATCH_MODE", "full").lower() == "full"

# 마지막으로 확인한 시각을 담아 두는 자리. 게시물 번호는 숫자 문자열이라
# 이 이름과 부딪히지 않는다.
CHECKED_KEY = "__checked__"
FAILURE_KEY = "__failures__"

# 감시가 죽어도 알림이 안 오면 '공지가 없어서 조용한 것' 과 구분되지 않는다.
# 믿고 있는데 실은 안 도는 상태가 제일 나쁘므로, 연달아 실패하면 알린다.
# 한 번 실패로는 알리지 않는다 — 학교 서버가 잠깐 느린 일은 흔하다.
FAILURE_THRESHOLD = int(os.getenv("FAILURE_THRESHOLD", "3"))
FAILURE_REPEAT_HOURS = float(os.getenv("FAILURE_REPEAT_HOURS", "24"))

# 매일 아침 살아 있다는 신호를 보낼지. 워크플로가 그 시각에만 켜 준다.
HEARTBEAT = os.getenv("HEARTBEAT", "") not in ("", "0", "false")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

_LINK_RE = re.compile(r"""href=['"]([^'"]*do=commonview[^'"]*)['"]""", re.I)
_BIDX_RE = re.compile(r"bidx=(\d+)")
_FIELD_TMPL = r"<th[^>]*>\s*{name}\s*</th>\s*<td[^>]*>(.*?)</td>"
_ATTACH_RE = re.compile(r'<ul class="list_attach">(.*?)</ul>', re.S | re.I)
_BODY_RE = re.compile(r'<td[^>]*class="[^"]*cont[^"]*"[^>]*>(.*?)</tbody>', re.S | re.I)


def strip_tags(fragment: str) -> str:
    """태그를 걷어내고 사람이 읽는 텍스트만 남긴다."""
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", fragment)
    text = re.sub(r"(?i)<br\s*/?>|</(p|div|li|tr|h\d)>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_mod.unescape(text).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.split("\n")]
    return "\n".join(ln for ln in lines if ln)


def field(page: str, name: str) -> str:
    m = re.search(_FIELD_TMPL.format(name=name), page, re.S | re.I)
    return strip_tags(m.group(1)) if m else ""


def list_post_ids(page: str, base_url: str) -> list[tuple[str, str]]:
    """목록 페이지에서 (게시물id, 상세 URL) 을 등장 순서대로 뽑는다.

    같은 게시물이 제목·썸네일·요약으로 여러 번 링크되므로 중복을 없앤다.
    제목은 여기서 읽지 않는다 — 맨 위 배너형 게시물은 링크 글자가
    '자세히 +' 라서 제목이 아니다. 제목은 상세 페이지에서 읽는다.
    """
    found: dict[str, str] = {}
    for href in _LINK_RE.findall(page):
        m = _BIDX_RE.search(href)
        if m:
            found.setdefault(m.group(1), urljoin(base_url, html_mod.unescape(href)))
    return list(found.items())


def parse_detail(page: str) -> dict:
    """상세 페이지에서 비교에 쓸 항목만 뽑는다. 조회수는 제외한다."""
    body_m = _BODY_RE.search(page)
    attach_m = _ATTACH_RE.search(page)
    return {
        "title": field(page, "제목"),
        "author": field(page, "작성자"),
        "date": field(page, "날짜"),
        "attachments": strip_tags(attach_m.group(1)) if attach_m else "",
        "body": strip_tags(body_m.group(1)) if body_m else "",
    }


def fingerprint(detail: dict) -> str:
    joined = "\n\x1f".join(
        detail[k] for k in ("title", "author", "date", "attachments", "body")
    )
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def body_diff(old: str, new: str, max_lines: int = 6) -> str:
    """무엇이 바뀌었는지 몇 줄만 보여준다. '수정됨' 세 글자로는 알 수 없다."""
    changes = [
        ln
        for ln in difflib.unified_diff(
            old.split("\n"), new.split("\n"), n=0, lineterm=""
        )
        if ln[:1] in "+-" and not ln.startswith(("+++", "---"))
    ]
    if not changes:
        return ""
    shown = changes[:max_lines]
    more = len(changes) - len(shown)
    out = "\n".join(("삭제: " if c[0] == "-" else "추가: ") + c[1:].strip()[:100] for c in shown)
    return out + (f"\n… 외 {more}줄" if more > 0 else "")


def is_due(state: dict, name: str, min_interval_min: float) -> bool:
    """게시판마다 최소 확인 간격을 둔다.

    로그인해서 보는 게시판은 자주 두드리면 봇으로 몰린다. 일정은 가장 잦은
    게시판에 맞춰 놓고, 뜸하게 봐야 하는 게시판은 여기서 걸러 낸다.
    """
    if min_interval_min <= 0:
        return True
    last = state.get(CHECKED_KEY, {}).get(name)
    if not last:
        return True
    return (time.time() - float(last)) >= min_interval_min * 60


def in_active_hours(window, now_utc: float | None = None) -> bool:
    """게시판마다 볼 시간대를 정한다. [시작, 끝) 이고 한국 시간 기준이다.

    업무시간 밖에는 공지가 올라오지 않는다. 그 시간에 안 두드리면 로그인
    횟수가 줄어 봇으로 몰릴 위험도 같이 내려간다. 비워 두면 하루 종일 본다.
    """
    if not window:
        return True
    start, end = window
    kst = datetime.datetime.fromtimestamp(
        now_utc if now_utc is not None else time.time(), datetime.timezone.utc
    ) + datetime.timedelta(hours=9)
    return start <= kst.hour < end


def mark_checked(state: dict, name: str, min_interval_min: float) -> None:
    """확인 시각은 간격 제한을 두는 게시판만 기록한다.

    모든 게시판에 남기면 시각이 매 실행 바뀌어 state.json 이 늘 달라진다.
    그러면 공지가 하나도 안 바뀐 날에도 실행마다 커밋이 생긴다. 하루 백 번
    도는 프로그램이라 그 커밋만 한 해 수만 건이 된다.
    """
    if min_interval_min > 0:
        state.setdefault(CHECKED_KEY, {})[name] = time.time()
    else:
        state.get(CHECKED_KEY, {}).pop(name, None)


def check_epic(session: requests.Session, board: dict, seen: dict) -> tuple[list, dict]:
    """에픽폴리오 비교과 프로그램. 새로 올라온 것만 본다.

    다른 게시판과 달리 상세 페이지를 열지 않는다(요청). 프로그램을 제목으로
    구분한다 — 목록에 있는 encSddpbSeq 는 암호화된 값이라 세션마다 달라질 수
    있고, 그걸 열쇠로 삼으면 매 실행이 '전부 새 프로그램' 이 될 수 있다.
    """
    items = epic.parse_list(epic.fetch_list(session))
    if not items:
        raise RuntimeError("프로그램을 하나도 못 찾았습니다. 화면 구조가 바뀌었을 수 있습니다.")

    first_run = not seen
    events, fresh = [], dict(seen)
    for item in items:
        key = hashlib.sha256(item["title"].encode("utf-8")).hexdigest()[:16]
        record = {
            "title": item["title"],
            "date": item["apply_period"],
            "url": epic.LIST_URL,
            "org": item["org"],
            "applied": item["applied"],
            "target": item["target"],
        }
        if key in seen:
            continue
        if first_run:
            fresh[key] = record
        else:
            detail = "\n".join([
                f"운영조직 {item['org']}",
                f"신청대상 {item['target']}",
                f"신청현황 {item['applied']}",
            ])
            events.append((key, "새 글", record, detail))

    if first_run:
        print(f"[{board['name']}] 첫 실행 — 프로그램 {len(fresh)}건을 기준으로 저장합니다(알림 없음).")
    return events, fresh


def load_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)


def fetch(session: requests.Session, url: str) -> str:
    resp = session.get(url, timeout=20)
    resp.raise_for_status()
    # 서버가 charset 을 UTF-8 로 정확히 알려준다(실측). 그래도 헤더가 빠지면
    # requests 는 ISO-8859-1 로 넘겨짚어 한글이 통째로 깨지므로 못 박아 둔다.
    resp.encoding = resp.encoding or "utf-8"
    return resp.text


def check_board(
    session: requests.Session, board: dict, seen: dict, full: bool = True
) -> tuple[list, dict]:
    """게시판 하나를 확인한다. (알릴 사건 목록, 최신 상태) 를 돌려준다.

    사건은 (게시물id, 종류, 기록, 바뀐내용) 이다.

    상세 페이지 하나를 못 받아도 그 게시물만 건너뛴다. 상태에 기록하지
    않으므로 다음 실행에서 다시 확인한다.
    """
    name = board["name"]
    posts = list_post_ids(fetch(session, board["url"]), board["url"])
    if not posts:
        raise RuntimeError(
            "목록에서 게시물을 하나도 못 찾았습니다. HTML 구조가 바뀌었을 수 있습니다."
        )

    first_run = not seen
    events = []
    # 확인 범위를 벗어난 옛 글의 기록은 남겨 둔다. 지우면 그 글이 다시
    # 목록에 올라올 때 '새 글' 로 잘못 알린다.
    fresh = dict(seen)

    on_page = posts[:DETAIL_LIMIT]
    # 새 글만 볼 때는 처음 보는 번호의 상세만 연다. 새 글은 하루 몇 건이라
    # 평소에는 여는 페이지가 없다. 이미 아는 글은 본문이 그대로일 것으로
    # 보고 넘어간다 — 그 확인은 full 로 도는 실행이 맡는다.
    targets = on_page if full else [(b, u) for b, u in on_page if b not in seen]

    for bidx, url in targets:
        try:
            detail = parse_detail(fetch(session, url))
        except Exception as exc:  # noqa: BLE001
            print(f"[{name}] {bidx} 상세 실패 (다음 실행에 재시도): {exc}")
            continue
        time.sleep(REQUEST_DELAY)

        if not detail["title"]:
            print(f"[{name}] {bidx} 제목을 못 읽었습니다. 건너뜁니다.")
            continue

        digest = fingerprint(detail)
        record = {
            "title": detail["title"],
            "date": detail["date"],
            "hash": digest,
            "body": detail["body"][:4000],
            "url": url,
        }
        old = seen.get(bidx)

        if old is None:
            # 처음 도는 판에서는 지금 걸린 글이 전부 '새 글' 이다. 20건을
            # 쏟아내면 정작 다음날 진짜 새 글이 묻힌다. 조용히 기록만 한다.
            if first_run:
                fresh[bidx] = record
            else:
                events.append((bidx, "새 글", record, excerpt(detail["body"])))
        elif old.get("hash") != digest:
            what = []
            if old.get("title") != detail["title"]:
                what.append(f"제목: {old.get('title')} → {detail['title']}")
            diff = body_diff(old.get("body", ""), detail["body"])
            if diff:
                what.append(diff)
            if not what:
                what.append("첨부파일 또는 작성 정보가 바뀌었습니다.")
            events.append((bidx, "수정됨", record, "\n".join(what)))
        else:
            fresh[bidx] = record  # 변화 없음.

    # 확인 범위를 벗어난 옛 글은 본문을 버린다. 비교할 일이 없는 본문을 계속
    # 들고 있으면 state.json 이 실행마다 불어난다. 해시는 남기므로 그 글이
    # 다시 목록에 올라와도 수정 여부는 그대로 알아낸다.
    checked = {bidx for bidx, _ in on_page}
    for bidx, rec in fresh.items():
        if bidx not in checked:
            rec.pop("body", None)

    if first_run:
        print(f"[{name}] 첫 실행 — 게시물 {len(fresh)}건을 기준으로 저장합니다(알림 없음).")
    return events, fresh


def is_korean_holiday(now_utc: float | None = None) -> bool:
    """오늘(한국 날짜)이 공휴일인가.

    설날·추석은 음력이라 직접 셀 수 없고 대체공휴일 규칙까지 있어 holidays 를
    쓴다. 판단이 안 되면 공휴일이 아닌 것으로 본다 — 생존 신호가 한 번 더 오는
    편이, 쉬는 날인 줄 알고 안 보냈다가 정말 멈춘 것을 놓치는 것보다 낫다.
    """
    kst = datetime.datetime.fromtimestamp(
        now_utc if now_utc is not None else time.time(), datetime.timezone.utc
    ) + datetime.timedelta(hours=9)
    try:
        import holidays
    except ImportError:
        print("[생존] holidays 가 없어 공휴일을 가리지 않습니다.")
        return False
    return kst.date() in holidays.SouthKorea(years=kst.year)


def heartbeat_message(state: dict, boards: list, failures: dict) -> str:
    """푸시에 뜰 한 줄. 조용한 것이 '공지가 없어서' 인지 '멈춰서' 인지 가른다."""
    if failures:
        return f"[감시 이상] {len(failures)}곳이 실패 중입니다"
    return f"[생존 신호] 게시판 {len(boards)}곳 정상 감시 중"


def heartbeat_blocks(state: dict, boards: list, failures: dict) -> list:
    kst = datetime.datetime.fromtimestamp(
        time.time(), datetime.timezone.utc
    ) + datetime.timedelta(hours=9)
    요일 = "월화수목금토일"[kst.weekday()]

    blocks = [
        {
            "type": "header",
            "text": "감시 이상" if failures else "정상 감시 중",
            "style": "red" if failures else "blue",
        },
        {"type": "text", "text": f"{kst:%Y-%m-%d} ({요일}) {kst:%H:%M}"},
    ]
    for board in boards:
        name = board["name"]
        count = len(state.get(name, {}))
        if name in failures:
            content = {
                "type": "text",
                "text": f"연속 {failures[name]['count']}회 실패 중",
                "inlines": [{
                    "type": "styled",
                    "text": f"연속 {failures[name]['count']}회 실패 중",
                    "bold": True, "color": "red",
                }],
            }
        else:
            content = {"type": "text", "text": f"{count}건 추적 중"}
        blocks.append(
            {"type": "description", "term": name, "accent": True, "content": content}
        )
    if failures:
        blocks.append({
            "type": "button", "text": "실행 기록 보기", "style": "primary",
            "action": {"type": "open_system_browser", "name": "actions",
                       "value": ACTIONS_URL},
        })
    return blocks


def load_keywords() -> tuple[list[str], list[str]]:
    """(어디서든 찾을 말, 제목에서만 찾을 말).

    '교육' 처럼 흔한 말은 본문까지 뒤지면 다섯 건 중 한 건이 걸려 강조가
    의미를 잃는다(실측). 그런 말은 제목에 있을 때만 센다.
    """
    data = load_json(KEYWORDS_FILE, {})
    anywhere = [k for k in data.get("강조", []) if k.strip()]
    title_only = [k for k in data.get("제목만", []) if k.strip()]
    return anywhere, title_only


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text).lower()


def _contains(word: str, text: str) -> bool:
    """글에 이 말이 들어 있나.

    한글은 띄어쓰기를 지우고 통째로 찾는다 — 공지 제목에는 '반 도 체' 처럼
    자간을 벌려 쓴 것이 종종 있다.

    영문은 그렇게 하면 안 된다. 'AI' 를 그냥 찾으면 e-mail 의 'ai', KAIST 의
    'AI' 에도 걸린다(실측). 그래서 영문·숫자 키워드는 앞뒤가 글자가 아닐
    때만 인정한다. 띄어쓰기를 지우지 않은 원문에서 찾아야 경계가 살아 있다.
    """
    if word.isascii():
        return re.search(
            r"(?<![A-Za-z0-9])" + re.escape(word) + r"(?![A-Za-z0-9])", text, re.I
        ) is not None
    return _squash(word) in _squash(text)


def matched_keywords(title: str, body: str = "") -> list[str]:
    """어느 키워드가 걸렸는지."""
    anywhere, title_only = load_keywords()
    both = title + " " + body
    hits = [w for w in anywhere if _contains(w, both)]
    hits += [w for w in title_only if _contains(w, title) and w not in hits]
    return hits


def highlight(text: str, keywords: list[str]) -> list[dict]:
    """걸린 말만 굵은 빨강으로 칠한 조각들을 만든다.

    카카오워크는 text 와 inlines 를 함께 받는다. inlines 조각을 이어 붙이면
    text 와 같아야 하므로 원문을 자를 때 순서를 지킨다.
    """
    if not keywords:
        return [{"type": "styled", "text": text}]
    # 긴 것부터 찾아야 '교육' 이 '교육혁신원' 을 먼저 자르지 않는다.
    pattern = "(" + "|".join(re.escape(k) for k in sorted(keywords, key=len, reverse=True)) + ")"
    out = []
    for part in re.split(pattern, text):
        if not part:
            continue
        if any(part == k for k in keywords):
            out.append({"type": "styled", "text": part, "bold": True, "color": "red"})
        else:
            out.append({"type": "styled", "text": part})
    return out


def excerpt(body: str, max_lines: int = 3, max_chars: int = 220) -> str:
    """본문 앞부분. 폰에서 링크를 열지 않고도 나와 상관있는 글인지 가른다.

    새 글은 어차피 상세를 열어 본문을 이미 갖고 있으므로 덧붙이는 값이 거의
    들지 않는다.
    """
    lines = [ln for ln in body.split("\n") if ln.strip()][:max_lines]
    out = "\n".join(lines)
    return out[:max_chars] + "…" if len(out) > max_chars else out


def format_message(kind: str, board_name: str, record: dict, detail: str) -> str:
    """푸시 알림과 대화방 목록에 뜨는 한 줄짜리 요약.

    폰 잠금화면에서는 이것만 보이므로 게시판과 제목이 앞에 와야 한다.
    """
    head = "[새 공지]" if kind == "새 글" else "[공지 수정]"
    if matched_keywords(record["title"], record.get("body", "")):
        head = "[관심 공지]" if kind == "새 글" else "[관심 공지 수정]"
    return f"{head} {board_name} — {record['title']}"


def format_blocks(kind: str, board_name: str, record: dict, detail: str) -> list:
    """말풍선 본문. 제목과 본문을 나누고 걸린 키워드를 빨갛게 칠한다."""
    hits = matched_keywords(record["title"], record.get("body", ""))
    if hits:
        header = {"type": "header", "text": "관심 공지", "style": "red"}
    elif kind == "새 글":
        header = {"type": "header", "text": "새 공지", "style": "blue"}
    else:
        header = {"type": "header", "text": "공지 수정", "style": "yellow"}

    blocks = [
        header,
        {
            "type": "text",
            "text": record["title"],
            "inlines": highlight(record["title"], hits),
        },
        {
            "type": "description",
            "term": board_name,
            "accent": True,
            "content": {"type": "text", "text": record["date"]},
        },
    ]
    if hits:
        blocks.append({
            "type": "description",
            "term": "걸린 말",
            "accent": True,
            "content": {"type": "text", "text": ", ".join(hits)},
        })
    if detail:
        blocks.append({
            "type": "description",
            "term": "내용" if kind == "새 글" else "바뀐 내용",
            "accent": True,
            "content": {"type": "text", "text": detail[:1500]},
        })
    blocks.append({
        "type": "button",
        "text": "공지 열기",
        "style": "primary",
        "action": {"type": "open_system_browser", "name": "open", "value": record["url"]},
    })
    return blocks


def main() -> int:
    boards = load_json(BOARDS_FILE, [])
    if not boards:
        print(f"{BOARDS_FILE} 에 감시할 게시판이 없습니다.")
        return 1

    state = load_json(STATE_FILE, {})
    print("확인 범위:", "새 글 + 수정" if FULL_SCAN else "새 글만")
    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9"})

    # 잘 도는 동안에는 이 자리를 비워 둔다. 값이 남아 있으면 그 내용이 계속
    # 바뀌면서 새 공지가 없는 날에도 state.json 커밋이 생긴다.
    failures = dict(state.get(FAILURE_KEY, {}))

    failed = False
    for board in boards:
        name = board["name"]
        # 게시판마다 다른 봇을 쓸 수 있다. boards.json 의 "bot" 은 앱키가 아니라
        # 앱키가 들어 있는 환경변수(깃허브 Secret)의 이름이다. 앱키를 파일에
        # 적으면 저장소에 그대로 올라간다.
        app_key = os.getenv(board.get("bot", ""), "")
        if not app_key:
            # 아직 그 게시판 전용 봇을 안 만들었으면 공용 봇으로 보낸다.
            # 조용히 넘어가면 어느 봇으로 갔는지 몰라 헷갈리므로 찍어 둔다.
            app_key = os.getenv("KAKAOWORK_APP_KEY", "")
            if board.get("bot"):
                print(f"[{name}] {board['bot']} 가 비어 공용 봇으로 보냅니다.")
        interval = float(board.get("min_interval_min", 0))
        if not in_active_hours(board.get("active_hours")):
            print(f"[{name}] 보는 시간대가 아닙니다. 건너뜁니다.")
            continue
        if not is_due(state, name, interval):
            print(f"[{name}] 아직 확인할 때가 아닙니다. 건너뜁니다.")
            continue

        try:
            if board.get("type") == "epic":
                events, fresh = check_epic(session, board, state.get(name, {}))
            else:
                events, fresh = check_board(
                    session, board, state.get(name, {}), FULL_SCAN
                )
        except Exception as exc:  # noqa: BLE001
            # 이 게시판의 상태는 건드리지 않는다. 다음 실행에서 다시 본다.
            print(f"[{name}] 확인 실패: {exc}")
            failed = True
            record = failures.get(name) or {"count": 0, "notified": 0}
            record["count"] += 1
            due = time.time() - record["notified"] >= FAILURE_REPEAT_HOURS * 3600
            if record["count"] >= FAILURE_THRESHOLD and due:
                blocks = [
                    {"type": "header", "text": "감시 실패", "style": "red"},
                    {"type": "text", "text": f"{name} 을(를) 확인하지 못하고 있습니다."},
                    {"type": "description", "term": "연속 실패", "accent": True,
                     "content": {"type": "text", "text": f"{record['count']}회"}},
                    {"type": "description", "term": "마지막 오류", "accent": True,
                     "content": {"type": "text", "text": str(exc)[:300]}},
                    {"type": "text", "text": "이 게시판의 새 공지를 지금 놓치고 있습니다."},
                    {"type": "button", "text": "실행 기록 보기", "style": "primary",
                     "action": {"type": "open_system_browser", "name": "actions",
                                "value": ACTIONS_URL}},
                ]
                warning = f"[감시 실패] {name} — 연속 {record['count']}회 확인 못 함"
                if notify.send(warning, app_key, blocks):
                    record["notified"] = time.time()
            failures[name] = record
            continue

        failures.pop(name, None)   # 한 번 성공하면 실패 횟수를 지운다

        # 알림을 먼저 보내고, 보내는 데 성공한 것만 상태에 남긴다. 순서를
        # 뒤집으면 전송이 실패했을 때 그 공지는 영영 다시 알려주지 않는다.
        for bidx, kind, record, detail in events:
            if notify.send(
                format_message(kind, name, record, detail),
                app_key,
                format_blocks(kind, name, record, detail),
            ):
                fresh[bidx] = record
            else:
                failed = True
                print(f"[{name}] 알림 실패 — 다음 실행에서 다시 알립니다: {record['title'][:40]}")

        state[name] = fresh
        mark_checked(state, name, interval)
        print(f"[{name}] 사건 {len(events)}건, 추적 {len(fresh)}건")

    if failures:
        state[FAILURE_KEY] = failures
    else:
        state.pop(FAILURE_KEY, None)

    if HEARTBEAT:
        if is_korean_holiday():
            print("[생존] 공휴일이라 생존 신호를 보내지 않습니다.")
        else:
            key = next(
                (os.getenv(b.get("bot", ""), "") for b in boards if os.getenv(b.get("bot", ""), "")),
                os.getenv("KAKAOWORK_APP_KEY", ""),
            )
            ok = notify.send(
                heartbeat_message(state, boards, failures),
                key,
                heartbeat_blocks(state, boards, failures),
            )
            if not ok:
                failed = True

    save_state(state)
    return 1 if failed else 0


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
