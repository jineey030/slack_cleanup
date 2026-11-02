# slack-cleanup

Slack에서 **내가 작성한 메시지를 채널/DM별로 찾아 일괄 삭제**하는 Python 스크립트입니다.
Slack에는 "내 메시지 전체 삭제" 기능이 없어서, Slack API와 본인 계정의 User Token으로 직접 지웁니다.

## 주요 기능

- **드라이런 스캔**: 아무것도 지우지 않고 채널/DM별 내 메시지 수와 기간을 표로 보여줍니다.
- **자동 백업**: 스캔 결과가 원문과 함께 `scan.json`에 저장됩니다.
- **선택 삭제**: 채널 이름, DM 상대 이름, 대화 ID, 대화 종류(공개/비공개/DM/그룹DM)로 대상을 고를 수 있습니다.
- **스레드 답글 포함**: 다른 사람이 시작한 스레드에 단 내 답글도 찾아서 지웁니다.
- **나간 채널 검색**: `--search` 옵션으로 이미 나간 채널의 메시지도 찾습니다.
- **파일 삭제**: `--files` 옵션으로 내가 업로드한 파일도 지웁니다.
- **이어서 실행**: 진행 기록이 `deleted.log`에 남아서, 중간에 끊겨도 다시 실행하면 남은 것만 처리합니다.
- **속도 제한 대응**: Slack API 속도 제한(429)에 걸리면 자동으로 기다렸다가 재시도합니다.

## 요구 사항

- Python 3.8 이상
- `requests` 패키지

```bash
pip install requests
```

## 1. User Token 발급

1. [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → **From scratch** → 앱 이름과 워크스페이스를 선택합니다.
2. **OAuth & Permissions** → **Scopes** → **User Token Scopes**에 아래 권한을 추가합니다. (Bot Token Scopes가 아닙니다.)

   | 용도 | Scope |
   |---|---|
   | 메시지 조회 | `channels:history` `groups:history` `im:history` `mpim:history` |
   | 대화 목록 | `channels:read` `groups:read` `im:read` `mpim:read` |
   | 사용자 이름 | `users:read` |
   | 검색 (`--search`) | `search:read` |
   | 메시지 삭제 | `chat:write` |
   | 파일 (`--files`) | `files:read` `files:write` |

3. 페이지 위쪽에서 **Install to Workspace**를 눌러 승인합니다. Scope를 바꿨다면 **Reinstall to Workspace**를 누르세요.
4. **User OAuth Token**(`xoxp-`로 시작)을 복사합니다. `xoxb-`로 시작하는 Bot 토큰으로는 동작하지 않습니다.

> ⚠️ User Token은 **내 계정 권한 그대로**입니다. 코드, 이슈, 채팅 등 어디에도 붙여넣지 마세요.

## 2. 토큰 설정

| 환경 | 명령 |
|---|---|
| macOS / Linux | `export SLACK_USER_TOKEN="xoxp-..."` |
| Windows PowerShell | `$env:SLACK_USER_TOKEN="xoxp-..."` |
| Windows cmd | `set SLACK_USER_TOKEN=xoxp-...` (따옴표 없이) |

## 3. 스캔 (삭제 안 함)

```bash
python slack_cleanup.py scan
```

| 옵션 | 설명 |
|---|---|
| `--search` | 검색 API로 이미 나간 채널의 메시지도 찾기 |
| `--files` | 내가 업로드한 파일 목록 포함 |
| `--no-threads` | 스레드 답글은 건너뛰기 (빠름) |
| `--out FILE` | 저장 파일 이름 (기본 `scan.json`) |

출력 예시:

```
============================================================
대화                                           메시지 수  기간
------------------------------------------------------------
#general                                              7  2026-06-22 ~ 2026-10-19
🔒#project-x                                        102  2026-06-16 ~ 2026-10-29
DM @홍길동                                          466  2026-07-01 ~ 2026-10-29
------------------------------------------------------------
합계                                                575
============================================================
예상 삭제 시간: 약 12분 (분당 ~50건 기준)
```

## 4. 삭제

`scan.json`을 기준으로 삭제합니다. 실행하면 대상 목록을 보여준 뒤, `DELETE`를 입력해야 진행됩니다.

```bash
python slack_cleanup.py delete --only general              # 특정 채널
python slack_cleanup.py delete --only 홍길동                # 특정 사람과의 DM
python slack_cleanup.py delete --only "general,홍길동"      # 여러 개
python slack_cleanup.py delete --only C0123456789          # 대화 ID로 지정
python slack_cleanup.py delete --types dm,group            # DM과 그룹DM 전체
python slack_cleanup.py delete --exclude announcements     # 특정 채널 제외
python slack_cleanup.py delete                             # 스캔된 전체
```

| 옵션 | 설명 |
|---|---|
| `--only` | 이 대화만 삭제 (채널 이름, DM 상대 이름, 대화 ID / 쉼표 구분) |
| `--exclude` | 이 대화는 제외 |
| `--types` | `public`, `private`, `dm`, `group` 중 선택 |
| `--files` | 스캔된 파일도 삭제 |
| `--delay` | 요청 간 대기 시간(초), 기본 1.2 |
| `--yes` | 확인 질문 생략 |
| `--plan` / `--log` | 계획 파일 / 진행 기록 파일 경로 |

### 팁

- **처음에는 작게 테스트하세요.** 테스트 채널을 하나 만들어 메시지를 몇 개 쓴 다음, 다시 스캔하고 `--only`로 그 채널만 지워보세요.
- **PowerShell에서 여러 개를 지정할 때**는 쉼표 목록을 따옴표로 감싸야 합니다. 예: `--only "a,b"`
- **이름이 애매한 대화**(그룹DM, 비활성 사용자 등)는 `scan.json`에서 `"id"`를 찾아 ID로 지정하세요.
- **삭제 속도**는 분당 약 50건입니다. 메시지 1만 개면 3~4시간 걸립니다. 중간에 끊겨도 같은 명령을 다시 실행하면 이어서 진행합니다.

## 제한 사항

- **복구 불가**: 삭제한 메시지는 되돌릴 수 없습니다. 필요하면 `scan.json`을 안전한 곳에 보관하세요.
- **삭제 권한 설정**: 워크스페이스 설정에서 메시지 삭제가 제한되어 있으면 `cant_delete_message` 오류가 날 수 있습니다.
- **보관된 채널**: 보관(archive)된 채널은 복원해야 삭제할 수 있습니다.
- **Slack Connect 채널**: 외부 조직과 공유된 채널은 상대 조직의 보존 정책에 따라 메시지가 남아 있을 수 있습니다.
- **검색 범위**: `--search`는 Slack 검색 API 한도(최대 10,000건)까지만 찾습니다.
- **대상 메시지 종류**: 일반 메시지, 스레드→채널 브로드캐스트, 파일 공유, `/me` 메시지만 지웁니다. 입장/퇴장 알림 같은 시스템 메시지는 제외됩니다.

## 작업 후 정리

다 끝나면 앱 설정의 **Basic Information** → **Delete App**으로 앱을 삭제하세요. 발급된 토큰도 함께 무효화됩니다.

## 주의

> ⚠️ `scan.json`과 `deleted.log`에는 **메시지 원문과 대화 정보**가 들어 있습니다. 절대 커밋하지 마세요. 포함된 `.gitignore`가 이 파일들을 제외합니다.

이 스크립트는 본인 계정의 메시지를 정리하는 용도로 만들어졌습니다. 회사 워크스페이스라면 데이터 보존 정책이나 법적 보존 의무가 있는지 먼저 확인하세요. 사용에 따른 책임은 사용자에게 있습니다.
