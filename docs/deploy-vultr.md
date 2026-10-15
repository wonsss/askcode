# Vultr 배포 가이드

인스턴스 한 대에 Docker Compose로 올립니다. 슬랙은 Socket Mode라 **외부에서 들어오는 포트가 필요 없습니다.** 서버는 슬랙·Claude API·GitHub·노션으로 나가는 연결만 합니다.

## 1. 인스턴스

| 항목 | 권장 |
| --- | --- |
| 리전 | 서울 |
| 사양 | 4 vCPU · 8GB RAM · 디스크 80GB 이상 (레포 클론·인덱스·감사 로그) |
| OS | Ubuntu 24.04 LTS |
| 방화벽 | 인바운드는 SSH(22)만, 내 IP 또는 VPN 대역으로 제한. 나머지 인바운드 전부 차단 |

```bash
# 서버에서
sudo apt-get update && sudo apt-get install -y docker.io docker-compose-v2 git
sudo usermod -aG docker $USER   # 재로그인
sudo ufw default deny incoming && sudo ufw allow from <내 IP> to any port 22 && sudo ufw enable
```

SSH는 키 인증만 허용합니다(`/etc/ssh/sshd_config`: `PasswordAuthentication no`).

## 2. 코드와 비밀값

```bash
git clone <askcode 레포> ~/askcode && cd ~/askcode
cp .env.example .env && chmod 600 .env    # 값 채우기
mkdir -p secrets && chmod 700 secrets
# GitHub App 개인키를 secrets/github-app.pem 으로 복사 후
chmod 600 secrets/github-app.pem
```

필수 값:

| 변수 | 설명 |
| --- | --- |
| `ANTHROPIC_API_KEY` | Claude API 키. 셸 환경변수보다 `.env` 값이 우선한다(다른 도구의 키와 섞이지 않게). `serve`가 시작할 때 확인한다 |
| `ASKCODE_GITHUB_APP_ID`, `ASKCODE_GITHUB_APP_INSTALLATION_ID` | GitHub App (Contents·Metadata 읽기 전용, law-watch에만 설치) |
| `ASKCODE_SLACK_BOT_TOKEN`, `ASKCODE_SLACK_APP_TOKEN` | 슬랙 앱 (README의 슬랙 앱 설정 참고) |

선택 값: `ASKCODE_NOTION_TOKEN`·`ASKCODE_NOTION_ROOT_PAGE_ID`(노션 문서), `ASKCODE_SLACK_SOURCE_CHANNELS`(소스 채널), `ASKCODE_ESCALATION_CHANNEL`(확인 요청 채널).

## 3. 실행

```bash
docker compose build
docker compose run --rm askcode askcode sync      # 첫 인덱싱 (레포·구조·문서·노션·슬랙)
docker compose run --rm askcode askcode ask "앱에서 의안 상세 화면은 네이티브야 웹뷰야?"
docker compose up -d                              # 봇 + 일일 백업
docker compose logs -f askcode
```

봇은 10분마다 레포를 fetch하고 바뀐 것이 있을 때만 다시 인덱싱합니다.

## 4. 컨테이너 보안 설정 (docker-compose.yml에 반영됨)

- 노출 포트 없음, 비루트 사용자(uid 10001)
- 읽기 전용 루트 파일시스템, `/tmp`만 tmpfs
- 모든 리눅스 권한 제거(`cap_drop: ALL`), `no-new-privileges`
- GitHub App 키는 읽기 전용으로 마운트
- 감사 로그(SQLite)는 볼륨에 저장, 백업 컨테이너가 매일 `./backups`로 복사(14일 보관)

## 5. 운영 확인

```bash
docker compose run --rm askcode askcode stats     # 질문 수·비용·확신도·피드백·범위 미스
docker compose run --rm askcode askcode eval      # 골든 세트 평가 → eval/results/
```

## 6. 업데이트·롤백

```bash
git pull && docker compose build && docker compose up -d
# 롤백: 이전 커밋으로 checkout 후 같은 명령
```
