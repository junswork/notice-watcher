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
