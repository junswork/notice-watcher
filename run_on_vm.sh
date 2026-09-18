#!/usr/bin/env bash
# VM 에서 감시를 한 번 돌린다. cron 이 1분마다 부른다.
#
# 깃허브 액션을 거치지 않는다. 액션으로 돌리면 실제 감시 1~3초에 껍데기가
# 13초 더 붙고(실측 16초), 60초 주기에 여유가 없어 대기열이 밀리는 순간
# 줄줄이 취소된다. 실행기가 깃허브와의 연결을 잃고 조용히 멎어 아홉 시간
# 동안 아무것도 안 돈 날도 있었다. cron 은 매번 새로 시작하므로 유지할
# 연결이 없다.
#
# 기록(state.json)은 깃허브에 둔다. VM 에만 두면 VM 이 날아갔을 때
# "어디까지 알렸는지" 가 사라져 복구할 때 수십 건이 한꺼번에 재발송된다.
set -uo pipefail

REPO="$HOME/notice-watcher"
PY="$HOME/.venv-notice/bin/python"
cd "$REPO" || exit 1

# 앞 실행이 아직 돌고 있으면 조용히 빠진다. 기다리지 않는다 — 1분 뒤에
# 어차피 다시 부른다. 두 개가 같이 돌면 같은 공지를 두 번 알린다.
exec 9>/tmp/notice-watcher.lock
flock -n 9 || { echo "앞 실행이 아직 돌고 있습니다. 건너뜁니다."; exit 0; }

# 원격 기록을 먼저 받는다. 깃허브 예약 실행(예비 경로)이 남긴 것이 있을 수 있다.
git fetch -q origin master 2>/dev/null
git reset -q --hard origin/master 2>/dev/null

set -a; . ./.env; set +a
export GITHUB_REPOSITORY="junswork/notice-watcher"

"$PY" watch.py
rc=$?

# 알림을 보낸 만큼은 반드시 기록한다. 남기지 않으면 다음 실행이 같은 공지를
# 새 글로 착각해 또 알린다.
# 알림을 보낸 만큼은 반드시 기록한다. 남기지 않으면 다음 실행이 같은 공지를
# 새 글로 착각해 또 알린다.
#
# state.json 하나만 올린다. 커밋 대상을 경로로 못 박지 않으면, 합치는 과정에서
# 작업 폴더의 다른 파일이 같이 실려 코드가 되돌아갈 수 있다(깃허브 쪽에서 실제로
# notify.py 가 옛 버전으로 되돌아간 적이 있다).
if ! git diff --quiet -- state.json 2>/dev/null; then
  git commit -q -o state.json -m "state: $(date -u +%Y-%m-%dT%H:%MZ)"
  for try in 1 2 3; do
    git push -q origin HEAD:master 2>/dev/null && break
    echo "push 거부됨 ($try/3). 원격 기록과 합칩니다."
    git fetch -q origin master
    git show origin/master:state.json > /tmp/remote_state.json
    "$PY" merge_state.py /tmp/remote_state.json state.json
    cp state.json /tmp/merged_state.json
    git reset -q --hard origin/master
    cp /tmp/merged_state.json state.json
    git diff --quiet -- state.json && break
    git commit -q -o state.json -m "state: $(date -u +%Y-%m-%dT%H:%MZ)"
  done
  rm -f /tmp/remote_state.json /tmp/merged_state.json
fi
exit $rc
