# VM 이 직접 돌린다 (지금의 주 경로)

깃허브 액션을 주 경로로 쓰다가 두 번 데였다.

1. **껍데기가 본업보다 비쌌다.** 실제 감시는 1~3초인데 액션 한 번이 16초였다.
   1분 주기에 여유가 없어 대기열이 밀리는 순간 줄줄이 취소됐다. 취소는 실패로
   안 잡혀서 빨간불도 안 뜨고 알림도 안 갔다 — 하루 실행의 81% 가 취소되고
   에픽폴리오가 37시간 동안 한 번도 확인되지 않은 날을 나중에야 알았다.
2. **자가 실행기는 조용히 멎었다.** 실행기가 깃허브와의 연결을 잃고 "active"
   표시만 남긴 채 아홉 시간 동안 아무것도 안 돌았다.

둘 다 원인이 같다. **유지해야 하는 연결이 있으면 그 연결이 고장 지점이 된다.**
그래서 연결을 없앴다. VM 의 systemd 타이머가 매번 새 프로세스를 띄운다.
깃허브는 이제 예비 경로다(`.github/workflows/watch.yml`).

## 구성

| 타이머 | 주기 | 하는 일 |
|---|---|---|
| `notice-watch` | 1분 | 새 글만 확인 |
| `notice-watch-full` | 매일 00,06,13:07 UTC | 수정까지 확인 |
| `notice-heartbeat` | 월~금 00:02 UTC (09:02 KST) | 살아 있다는 신호 |

`OnCalendar` 는 **UTC 로 적는다.** UTC 00:02 는 같은 날 KST 09:02 다 — 날짜가
하루 밀린다고 착각해 `Sun~Thu` 로 적었다가 토요일에 오고 금요일에 안 온 적이
있다. 요일을 바꿀 때는 반드시 `systemctl list-timers` 로 다음 다섯 번을 눈으로
확인한다.

생존 신호는 깃허브에 두지 않는다. VM 이 죽었을 때 "신호가 안 온다" 로 알아채야
하는데, 깃허브가 대신 보내면 그 고장을 가려 버린다.

실행 본체는 `run_on_vm.sh` 다. `flock` 으로 겹침을 막고, 코드를 원격과 맞춘 뒤
`watch.py` 를 돌리고, **`state.json` 하나만** 커밋한다. 커밋 대상을 경로로 못
박는 이유는 예전에 작업 폴더의 낡은 코드가 상태 커밋에 딸려 들어가 `notify.py`
를 옛 버전으로 되돌린 적이 있기 때문이다.

## 손볼 때

```sh
ssh -i ~/.ssh/oracle-notice-runner.key ubuntu@<공인IP>

systemctl list-timers 'notice-*'              # 다음 실행 시각
journalctl -u notice-watch -f                 # 실시간 로그
journalctl -u notice-watch --since "1 day ago" | grep -v 건너뜁니다
sudo systemctl restart notice-watch.timer
```

`run_on_vm.sh` 를 고쳤으면 실행 권한을 깃에도 박아야 한다. `git reset --hard` 가
권한 비트를 되돌려 systemd 가 203/EXEC 로 8분간 멎은 적이 있다.

```sh
git update-index --chmod=+x run_on_vm.sh
```

`.env` 를 윈도우에서 옮길 때는 줄 끝의 `\r` 를 지운다. 안 지우면 Authorization
헤더에 섞여 들어가 카카오워크가 거부한다.

```sh
sed -i 's/\r$//' ~/notice-watcher/.env
```

## 오라클 계정을 못 들어갈 때

콘솔 로그인은 **Cloud Account Name(테넌시 이름)** 을 먼저 묻는다. 이건 인스턴스
이름(`instance-...`)이나 호스트명(`techvnic`)과 **아무 상관이 없다.** 호스트명을
보고 짐작했다가 로그인도 비밀번호 재설정도 실패한 적이 있다 — 없는 계정으로
재설정을 걸면 "전송됨" 이라고만 뜨고 메일은 영영 안 온다.

테넌시 이름은 가입 환영 메일에 있고, 들어간 뒤에는 콘솔 우측 상단 프로필 메뉴에
나온다. **알아냈으면 여기 적어 둔다 →** `테넌시 이름: (여기)`

OCID 는 VM 안에서 언제든 다시 꺼낼 수 있다. 지원 요청할 때 쓴다.

```sh
curl -s -H "Authorization: Bearer Oracle" \
  http://169.254.169.254/opc/v2/instance/ | python3 -m json.tool
```

`compartmentId` 가 테넌시 OCID(루트 구획에 만들었으므로), `id` 가 인스턴스
OCID 다. 값은 계정을 특정하므로 공개 저장소인 여기에는 적지 않는다.

콘솔에 못 들어가도 감시는 안 멈춘다. VM 은 콘솔과 무관하게 돌고, 봇 키와 학교
계정은 깃허브 Secrets 에도 있어 VM 을 잃어도 복구된다. 다만 결제수단 확인이
막힌 상태가 길어지면 오라클이 테넌시를 정지시키고 VM 을 회수한다.
