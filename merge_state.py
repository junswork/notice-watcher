"""두 개의 state.json 을 합친다. push 가 거부됐을 때 쓴다.

왜 필요한가. 실행 하나가 57초쯤 걸리는 순간이 있다(에픽폴리오는 로그인을
해야 해서 길다). 1분마다 깨우므로, 그 사이에 다음 실행이 **앞 실행이 아직
push 하지 않은** 코드를 받아 간다. 둘 다 각자 기록을 남기고, 나중 것의 push 가
`non-fast-forward` 로 거부된다.

거부된 쪽을 그냥 버리면 안 된다. 그 실행이 이미 보낸 알림의 기록까지 사라져
다음 실행이 같은 공지를 또 알린다. 중복 알림은 알림 자체의 무게를 떨어뜨린다.

그래서 **합집합**으로 합친다. 양쪽 다 '이미 알린 것' 의 기록이므로, 합쳐 두면
어느 쪽도 다시 알리지 않는다.

    python merge_state.py 원격.json 내.json    # 내.json 을 합친 결과로 덮어쓴다
"""

import io
import json
import sys


def merge(remote: dict, mine: dict) -> dict:
    """게시판별로 게시물 기록을 합친다. 같은 게시물이면 내 쪽을 남긴다.

    내 쪽이 방금 읽어 온 최신이다. 다만 '마지막으로 확인한 시각' 은 둘 중
    나중 것을 쓴다 — 이르게 되돌리면 뜸하게 봐야 할 게시판을 너무 자주 본다.
    """
    out: dict = {}
    for src in (remote, mine):
        for board, posts in src.items():
            out.setdefault(board, {}).update(posts)

    checked = out.get("__checked__")
    if checked:
        for name in checked:
            times = [
                s["__checked__"][name]
                for s in (remote, mine)
                if name in s.get("__checked__", {})
            ]
            checked[name] = max(times)
    return out


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    remote_path, mine_path = sys.argv[1], sys.argv[2]
    with io.open(remote_path, encoding="utf-8") as f:
        remote = json.load(f)
    with io.open(mine_path, encoding="utf-8") as f:
        mine = json.load(f)

    merged = merge(remote, mine)
    with io.open(mine_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(merged, f, ensure_ascii=False, indent=1, sort_keys=True)

    boards = [k for k in merged if not k.startswith("__")]
    total = sum(len(merged[b]) for b in boards)
    print(f"합침: 게시판 {len(boards)}곳, 게시물 {total}건")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
