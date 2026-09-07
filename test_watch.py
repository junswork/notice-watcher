"""자체 점검. `python test_watch.py` 로 돌린다. 네트워크 없이 동작한다.

`python test_watch.py --live` 를 주면 실제 게시판까지 확인한다.
"""

import sys

import watch

# 실제 게시판 HTML 의 뼈대만 남긴 것. 세 게시판이 모두 이 구조를 쓴다.
PAGE = """
<div class="wrap_view"><table class="tbl_view"><tbody>
<tr><th scope="row">제목</th><td colspan="5">{title}</td></tr>
<tr><th scope="row">작성자</th><td>학사지원과</td>
    <th scope="row">조회수</th><td>{views}</td>
    <th scope="row">날짜</th><td>2026-09-04</td></tr>
<tr><th scope="row">첨부파일</th><td colspan="5">
    <ul class="list_attach">{attach}</ul></td></tr>
<tr><td class="cont" colspan="6"><div><p>{body}</p></div></td></tr>
</tbody></table></div>
"""

LIST = """
<tr class="body_tr"><td class="tit"><a href='?do=commonview&nowpage=1&bnum=4691&bidx=111&cate=10'>가</a></td></tr>
<tr class="body_tr"><td class="tit"><a href="?do=commonview&bnum=4691&bidx=222&cate=10">나</a>
    <a href="?do=commonview&bnum=4691&bidx=222&cate=10"><img src="t.jpg" /></a></td></tr>
<tr><td><a href="?do=list&bnum=4691">목록</a></td></tr>
"""


def page(title="공지 제목", views=1, attach="", body="본문 첫 줄"):
    return PAGE.format(title=title, views=views, attach=attach, body=body)


def test_parse():
    d = watch.parse_detail(page(attach="<li><a href='#'>붙임.hwp</a></li>"))
    assert d["title"] == "공지 제목", d
    assert d["author"] == "학사지원과", d
    assert d["date"] == "2026-09-04", d
    assert "붙임.hwp" in d["attachments"], d
    assert d["body"] == "본문 첫 줄", repr(d["body"])
    # 조회수는 어디에도 섞이면 안 된다.
    assert "1" not in d["attachments"]


def test_hash_ignores_views():
    """조회수만 다른 두 페이지는 같은 해시여야 한다. 이게 깨지면 매 실행이 오탐이다."""
    a = watch.fingerprint(watch.parse_detail(page(views=1)))
    b = watch.fingerprint(watch.parse_detail(page(views=99999)))
    assert a == b, "조회수가 해시에 샜다"


def test_hash_catches_real_edits():
    base = watch.fingerprint(watch.parse_detail(page()))
    assert watch.fingerprint(watch.parse_detail(page(title="바뀐 제목"))) != base
    assert watch.fingerprint(watch.parse_detail(page(body="고친 본문"))) != base
    assert watch.fingerprint(watch.parse_detail(page(attach="<li>추가.pdf</li>"))) != base


def test_list_ids():
    ids = watch.list_post_ids(LIST, "https://x.ac.kr/service/info/notice/")
    assert [i for i, _ in ids] == ["111", "222"], ids       # 순서 유지, 중복 제거
    assert ids[0][1] == (
        "https://x.ac.kr/service/info/notice/"
        "?do=commonview&nowpage=1&bnum=4691&bidx=111&cate=10"
    ), ids[0][1]


def test_body_diff():
    out = watch.body_diff("한 줄\n두 줄", "한 줄\n두 줄 고침")
    assert "삭제: 두 줄" in out and "추가: 두 줄 고침" in out, out
    assert watch.body_diff("같음", "같음") == ""


def test_check_board_flow():
    """첫 실행은 조용, 새 글은 알림, 수정은 알림, 변화 없으면 조용."""
    pages = {}

    class FakeSession:
        def get(self, url, timeout=None):
            raise AssertionError("fetch 를 가로채지 못했다")

    real_fetch = watch.fetch
    watch.fetch = lambda s, url: pages[url]
    watch.REQUEST_DELAY = 0
    try:
        board = {"name": "테스트", "url": "https://x.ac.kr/b/"}
        pages["https://x.ac.kr/b/"] = LIST
        u1 = "https://x.ac.kr/b/?do=commonview&nowpage=1&bnum=4691&bidx=111&cate=10"
        u2 = "https://x.ac.kr/b/?do=commonview&bnum=4691&bidx=222&cate=10"
        pages[u1] = page(title="글 하나")
        pages[u2] = page(title="글 둘")

        events, state = watch.check_board(FakeSession(), board, {})
        assert events == [], "첫 실행은 알리지 않는다"
        assert set(state) == {"111", "222"}, state

        # 조회수만 오른 경우 — 아무 일도 없어야 한다.
        pages[u1] = page(title="글 하나", views=500)
        events, state = watch.check_board(FakeSession(), board, state)
        assert events == [], events

        # 본문 수정
        pages[u1] = page(title="글 하나", body="중요 내용 추가")
        events, state2 = watch.check_board(FakeSession(), board, state)
        assert len(events) == 1 and events[0][1] == "수정됨", events
        assert "중요 내용 추가" in events[0][3], events[0][3]
        # 알리지 못한 사건은 상태에 남지 않아야 다음 실행에서 다시 잡힌다.
        assert state2["111"]["hash"] == state["111"]["hash"], "미전송 사건이 저장됐다"

        # 새 글 (앞 단계의 본문 수정은 되돌려 놓는다)
        pages[u1] = page(title="글 하나")
        pages["https://x.ac.kr/b/"] = LIST.replace(
            "<tr><td><a href=\"?do=list&bnum=4691\">목록</a></td></tr>",
            "<tr class=\"body_tr\"><td><a href=\"?do=commonview&bnum=4691&bidx=333\">다</a></td></tr>",
        )
        pages["https://x.ac.kr/b/?do=commonview&bnum=4691&bidx=333"] = page(title="새 글")
        events, _ = watch.check_board(FakeSession(), board, state)
        assert len(events) == 1 and events[0][1] == "새 글", events
        assert events[0][0] == "333", events
    finally:
        watch.fetch = real_fetch


def test_new_mode_skips_known_posts():
    """새 글만 보는 모드: 이미 아는 글은 상세를 열지 않고, 새 글은 잡는다."""
    pages, opened = {}, []
    real_fetch = watch.fetch

    def fake_fetch(session, url):
        opened.append(url)
        return pages[url]

    watch.fetch = fake_fetch
    watch.REQUEST_DELAY = 0
    try:
        board = {"name": "테스트", "url": "https://x.ac.kr/b/"}
        u1 = "https://x.ac.kr/b/?do=commonview&nowpage=1&bnum=4691&bidx=111&cate=10"
        u2 = "https://x.ac.kr/b/?do=commonview&bnum=4691&bidx=222&cate=10"
        pages["https://x.ac.kr/b/"] = LIST
        pages[u1] = page(title="글 하나")
        pages[u2] = page(title="글 둘")
        _, state = watch.check_board(None, board, {}, full=True)

        # 본문이 바뀌어도 새 글만 보는 모드는 상세를 열지 않으므로 모른다.
        pages[u1] = page(title="글 하나", body="몰래 고친 본문")
        opened.clear()
        events, _ = watch.check_board(None, board, state, full=False)
        assert events == [], events
        assert opened == ["https://x.ac.kr/b/"], f"목록 외에 더 열었다: {opened}"

        # 같은 모드에서도 처음 보는 글은 상세를 열어 알린다.
        pages["https://x.ac.kr/b/"] = LIST.replace(
            '<tr><td><a href="?do=list&bnum=4691">목록</a></td></tr>',
            '<tr class="body_tr"><td><a href="?do=commonview&bnum=4691&bidx=333">다</a></td></tr>',
        )
        u3 = "https://x.ac.kr/b/?do=commonview&bnum=4691&bidx=333"
        pages[u3] = page(title="새 글")
        opened.clear()
        events, _ = watch.check_board(None, board, state, full=False)
        assert len(events) == 1 and events[0][1] == "새 글", events
        assert opened == ["https://x.ac.kr/b/", u3], f"연 페이지: {opened}"

        # full 로 돌면 아까 몰래 고친 본문이 그제야 잡힌다.
        events, _ = watch.check_board(None, board, state, full=True)
        kinds = sorted(e[1] for e in events)
        assert kinds == ["새 글", "수정됨"], events
    finally:
        watch.fetch = real_fetch


def test_min_interval():
    """뜸하게 봐야 하는 게시판은 일정이 자주 깨워도 건너뛴다."""
    import time

    state = {}
    assert watch.is_due(state, "에픽", 120), "기록이 없으면 확인해야 한다"
    watch.mark_checked(state, "에픽", 120)
    assert not watch.is_due(state, "에픽", 120), "방금 봤으면 건너뛴다"
    assert watch.is_due(state, "에픽", 0), "간격 0 이면 항상 확인한다"
    assert watch.is_due(state, "다른게시판", 120), "게시판마다 따로 센다"

    # 시간이 지나면 다시 확인한다.
    state[watch.CHECKED_KEY]["에픽"] = time.time() - 121 * 60
    assert watch.is_due(state, "에픽", 120)

    # 간격 제한이 없는 게시판은 시각을 남기지 않는다. 남기면 그 값이 매 실행
    # 바뀌어, 공지가 하나도 안 바뀐 날에도 state.json 커밋이 생긴다.
    watch.mark_checked(state, "대학공지", 0)
    assert "대학공지" not in state[watch.CHECKED_KEY], state[watch.CHECKED_KEY]


def test_excerpt():
    """새 글 알림에 붙는 본문 앞부분."""
    assert watch.excerpt("") == ""
    assert watch.excerpt("한 줄\n\n두 줄\n세 줄\n네 줄") == "한 줄\n두 줄\n세 줄"
    long = watch.excerpt("가" * 500)
    assert len(long) == 221 and long.endswith("…"), len(long)


def test_new_post_message_has_body():
    """새 글 알림에 본문이 들어가고, 새 글과 수정이 다르게 표시된다."""
    rec = {"title": "장학금 신청", "date": "2026-09-08", "url": "https://x/1", "body": ""}
    real = watch.load_keywords
    watch.load_keywords = lambda: ([], [])
    try:
        # 푸시 한 줄에는 게시판과 제목이 들어간다.
        line = watch.format_message("새 글", "대학공지사항", rec, "9월 20일까지 신청")
        assert "[새 공지]" in line and "대학공지사항" in line and "장학금 신청" in line, line

        blocks = watch.format_blocks("새 글", "대학공지사항", rec, "9월 20일까지 신청")
        body = next(b for b in blocks if b.get("term") == "내용")
        assert body["content"]["text"] == "9월 20일까지 신청"
        assert not any(b.get("term") == "바뀐 내용" for b in blocks)

        fixed = watch.format_blocks("수정됨", "대학공지사항", rec, "삭제: 9월 20일")
        assert fixed[0]["text"] == "공지 수정", fixed[0]
        assert any(b.get("term") == "바뀐 내용" for b in fixed)
        assert "[공지 수정]" in watch.format_message("수정됨", "대학공지사항", rec, "")
    finally:
        watch.load_keywords = real


def test_keyword_match_and_highlight():
    """놓치기 싫은 말이 걸리면 찾아내고, 그 부분만 빨갛게 칠한다."""
    real = watch.load_keywords
    watch.load_keywords = lambda: (["반도체", "실습"], ["교육"])
    try:
        assert watch.matched_keywords("반도체 8대 공정 체험") == ["반도체"]
        assert watch.matched_keywords("현장실습 신청 안내") == ["실습"]
        assert watch.matched_keywords("장학금 신청") == []
        # 제목에 없어도 본문에 있으면 잡는다.
        assert watch.matched_keywords("특강 안내", "반도체 공정 실습 포함") == ["반도체", "실습"]
        # 자간을 벌려 쓴 제목도 잡는다.
        assert watch.matched_keywords("반 도 체 특강") == ["반도체"]

        # '제목만' 으로 지정한 말은 제목에 있을 때만 센다. 본문은 안 본다 —
        # '교육' 은 대학 공지 본문에 거의 다 나와서 강조가 의미를 잃는다.
        assert watch.matched_keywords("장비교육 안내") == ["교육"]
        assert watch.matched_keywords("TOEIC 접수", "교육 과정 안내입니다") == []

        parts = watch.highlight("반도체 특강", ["반도체"])
        assert "".join(p["text"] for p in parts) == "반도체 특강", parts
        red = [p for p in parts if p.get("color") == "red"]
        assert len(red) == 1 and red[0]["text"] == "반도체", parts

        # 긴 키워드를 먼저 잘라야 '교육' 이 '교육혁신원' 을 쪼개지 않는다.
        parts = watch.highlight("교육혁신원 안내", ["교육"])
        assert "".join(p["text"] for p in parts) == "교육혁신원 안내"

        # 키워드가 없으면 통째로 한 조각이다.
        assert watch.highlight("아무거나", []) == [{"type": "styled", "text": "아무거나"}]
    finally:
        watch.load_keywords = real


def test_blocks_shape():
    """카카오워크가 받아들이는 모양이어야 한다. text 는 항상 함께 보낸다."""
    real = watch.load_keywords
    watch.load_keywords = lambda: (["반도체"], [])
    try:
        rec = {"title": "반도체 공정 교육", "date": "2026-09-08",
               "url": "https://x/1", "body": "본문"}
        blocks = watch.format_blocks("새 글", "대학공지사항", rec, "3줄 요약")
        assert blocks[0] == {"type": "header", "text": "관심 공지", "style": "red"}
        assert blocks[0]["text"] and len(blocks[0]["text"]) <= 20   # header 20자 제한
        btn = blocks[-1]
        assert btn["type"] == "button"
        assert btn["action"]["type"] == "open_system_browser"
        assert btn["action"]["value"] == "https://x/1"
        # inlines 를 이어 붙이면 text 와 같아야 한다.
        title = next(b for b in blocks if b["type"] == "text")
        assert "".join(i["text"] for i in title["inlines"]) == title["text"]
        # 푸시 한 줄에도 게시판과 제목이 들어간다.
        line = watch.format_message("새 글", "대학공지사항", rec, "")
        assert "대학공지사항" in line and "반도체 공정 교육" in line and "관심" in line

        # 키워드가 안 걸리면 파란 머리.
        plain = {"title": "장학금 안내", "date": "2026-09-08", "url": "https://x/2", "body": ""}
        assert watch.format_blocks("새 글", "학사공지", plain, "")[0]["style"] == "blue"
        assert watch.format_blocks("수정됨", "학사공지", plain, "")[0]["style"] == "yellow"
    finally:
        watch.load_keywords = real


def test_holiday_skip():
    """생존 신호는 공휴일에 보내지 않는다. 음력 명절도 걸러져야 한다."""
    import calendar
    import datetime as dt

    def at(y, m, d):
        # 한국 시간 09:02 를 UTC 기준 초로 바꾼다.
        kst = dt.datetime(y, m, d, 9, 2) - dt.timedelta(hours=9)
        return calendar.timegm(kst.timetuple())

    assert watch.is_korean_holiday(at(2026, 2, 17)), "설날을 못 걸렀다"
    assert watch.is_korean_holiday(at(2026, 9, 25)), "추석을 못 걸렀다"
    assert watch.is_korean_holiday(at(2026, 3, 2)), "대체 휴일을 못 걸렀다"
    assert watch.is_korean_holiday(at(2027, 1, 1)), "해가 바뀌어도 걸러야 한다"
    assert not watch.is_korean_holiday(at(2026, 9, 8)), "평일을 공휴일로 봤다"


def test_heartbeat_message():
    state = {"대학공지사항": {"1": {}, "2": {}}, "에픽": {"a": {}}}
    boards = [{"name": "대학공지사항"}, {"name": "에픽"}]

    # 푸시 한 줄
    assert "정상 감시 중" in watch.heartbeat_message(state, boards, {})
    bad_line = watch.heartbeat_message(state, boards, {"에픽": {"count": 4, "notified": 0}})
    assert "감시 이상" in bad_line, bad_line

    # 말풍선
    blocks = watch.heartbeat_blocks(state, boards, {})
    assert blocks[0]["style"] == "blue" and len(blocks[0]["text"]) <= 20
    terms = {b["term"]: b["content"]["text"] for b in blocks if b["type"] == "description"}
    assert terms == {"대학공지사항": "2건 추적 중", "에픽": "1건 추적 중"}, terms
    assert not any(b["type"] == "button" for b in blocks), "정상일 땐 버튼이 없다"

    # 실패 중인 게시판이 있으면 빨간 머리 + 실행 기록 버튼
    bad = watch.heartbeat_blocks(state, boards, {"에픽": {"count": 4, "notified": 0}})
    assert bad[0]["style"] == "red"
    hit = next(b for b in bad if b.get("term") == "에픽")
    assert "연속 4회 실패 중" in hit["content"]["text"]
    assert hit["content"]["inlines"][0]["color"] == "red"
    assert any(b["type"] == "button" for b in bad), "실패 시엔 버튼이 있어야 한다"


def test_epic_parse_list():
    """비교과 목록: 항목 안에 <li> 가 중첩돼 있어도 항목별로 갈라야 한다."""
    import epic

    item = """<li><div class="left"><span class="type02">인정</span></div>
    <div class="right"><div class="top">
    <a href="#" class="detailBtn" data-params='{{"encSddpbSeq":"{seq}"}}'>
    <span class="p_col">[70점]</span>{title}</a>
    <ul><li>창의역량</li><li>융합역량</li></ul></div>
    <div class="bottom"><dl><dt>운영조직</dt><dd>{org}</dd></dl>
    <dl><dt>전화번호</dt><dd>02-970-0000</dd></dl>
    <dl class="target"><dt>신청대상</dt><dd><span>전체</span><span>전체</span></dd></dl>
    <dl><dt>신청기간</dt><dd>2026.09.03 11:00&nbsp;~&nbsp;2026.09.08 18:00</dd></dl>
    <dl><dt>신청현황</dt><dd><div class="app_num">
    <span class="current">5</span>/<span class="max">18</span>명</div></dd></dl>
    </div></div></li>"""
    page = "<ul>" + item.format(seq="aaa", title="레이저커팅기 장비교육", org="창업지원단") \
                + item.format(seq="bbb", title="현대건설 채용설명회", org="취업진로본부") + "</ul>"

    out = epic.parse_list(page)
    assert len(out) == 2, out
    assert out[0]["title"] == "레이저커팅기 장비교육", out[0]
    assert out[0]["org"] == "창업지원단", out[0]
    assert out[1]["org"] == "취업진로본부", out[1]
    assert out[0]["applied"] == "5/18명", out[0]
    assert out[0]["apply_period"] == "2026.09.03 11:00 ~ 2026.09.08 18:00", out[0]
    assert out[0]["target"] == "전체", out[0]["target"]   # 중복 제거
    assert "[70점]" not in out[0]["title"]


def test_epic_auto_submit_form():
    """통합 로그인 중간에 나오는 '자동 제출 폼' 만 대신 보내야 한다.

    사람이 채우는 로그인 창까지 보내 버리면 빈 아이디로 로그인을 다시
    시도하게 된다. 연속 실패는 계정 잠금으로 이어지므로 반드시 걸러야 한다.
    """
    import epic

    auto = (
        '<html><body onload="document.forms[0].submit()">'
        '<form action="/next" method="post">'
        '<input type="hidden" name="SAMLResponse" value="abc" />'
        '<input name="RelayState" value="xyz" />'
        "</form></body></html>"
    )
    human = (
        '<form id="ssoLog" action="/login" method="post">'
        '<input type="hidden" name="userId" value="" />'
        '<input type="text" id="encIi" value="" />'
        '<input type="password" id="encPp" />'
        "</form>"
    )

    assert epic._inputs(auto) == {"SAMLResponse": "abc", "RelayState": "xyz"}

    class FakeResp:
        def __init__(self, text, url="https://portal.x.ac.kr/a"):
            self.text, self.url = text, url

    sent = []

    class FakeSession:
        def post(self, url, data=None, timeout=None):
            sent.append((url, data))
            return FakeResp("<html>끝</html>")

        def get(self, url, params=None, timeout=None):
            sent.append((url, params))
            return FakeResp("<html>끝</html>")

    # 자동 제출 폼은 따라간다.
    epic.follow_auto_submit(FakeSession(), FakeResp(auto))
    assert sent == [
        ("https://portal.x.ac.kr/next", {"SAMLResponse": "abc", "RelayState": "xyz"})
    ], sent

    # 사람이 채우는 창은 건드리지 않는다.
    sent.clear()
    out = epic.follow_auto_submit(FakeSession(), FakeResp(human))
    assert sent == [], f"로그인 창을 다시 보냈다: {sent}"
    assert out.text == human


def test_live():
    import json, requests

    s = requests.Session()
    s.headers.update({"User-Agent": watch.UA})
    for b in json.load(open("boards.json", encoding="utf-8")):
        posts = watch.list_post_ids(watch.fetch(s, b["url"]), b["url"])
        assert posts, f'{b["name"]}: 목록이 비었다'
        d = watch.parse_detail(watch.fetch(s, posts[0][1]))
        assert d["title"] and d["date"], f'{b["name"]}: 상세를 못 읽었다 {d}'
        print(f'  {b["name"]}: {len(posts)}건, 최상단 {d["title"][:30]!r}')


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    if "--live" not in sys.argv:
        tests = [t for t in tests if t.__name__ != "test_live"]
    for t in tests:
        print(f"- {t.__name__}")
        t()
    print(f"\n{len(tests)}개 통과")
