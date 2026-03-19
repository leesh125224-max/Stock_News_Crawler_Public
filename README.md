# 주식 종목 뉴스 브리핑 시스템 (GitHub Actions 자동화)

## ⚠️ Public Repository 안내

본 레포지토리는 공개용(Public)으로 구성된 프로젝트입니다.

실제 운영 환경에서는:
- GitHub Private Repository에서 자동화(GitHub Actions)가 실행되고 있으며
- 데이터 파일(`data/`) 및 일부 설정은 **운영 목적에 맞게 별도로 관리**됩니다.

따라서 본 레포지토리의 `data/` 폴더는:
- 구조 및 동작 설명을 위한 **샘플/간소화된 데이터**로 구성되어 있으며 실제 운영 데이터와는 차이가 있습니다.

## 1. 개요

주식 투자는 정보의 속도와 질이 핵심이지만, 개인이 매일 쏟아지는 방대한 양의 뉴스를 모두 파악하기에는 물리적인 한계가 있습니다. 특히 정보 지연으로 인해 주가 상승 이후에나 소식을 접하게 되는 기회비용 문제를 해결하고자 본 프로젝트를 기획했습니다.  
본 시스템은 사용자가 설정한 관심 종목(data/종목명.json)을 기반으로 관련 뉴스를 정밀 타겟팅하여 수집합니다. 수집된 정보는 AI를 통해 시장 및 테마 관점에서 핵심 내용을 요약하고, 투자 결정에 유의미한 중요 뉴스를 선별하여 제공하는 자동화 솔루션입니다.

---

## 2. 로컬 버전(`종목명_news.py`)과의 주요 차이점

| 항목 | 로컬 버전 | GitHub Actions 버전 |
|---|---|---|
| **환경 변수** | `.env` 파일 | GitHub Secrets |
| **DB 저장** | SQLite (`news_today.db`, `news_stack.db`) | Supabase (클라우드 PostgreSQL) |
| **Gmail 인증 파일** | `token.json`, `credentials.json` | 파일 생성 없이 환경변수에서 메모리로 직접 인증 |
| **실행 시간 기준** | 로컬 PC 시간 | **UTC → KST 변환 명시** (GitHub 서버는 UTC) |
| **특징주.csv 경로** | `../../특징주/data/db/특징주.csv` | `../data/특징주.csv` (레포 내 data/폴더) |
| **실행 방식** | 수동 실행/작업 스케줄러 | GitHub Actions 스케줄/수동 트리거 |

---

## 3. GitHub 레포지토리 구조

```
(레포 최상단)
 ┣ .github/
 ┃  └ workflows/
 ┃     ┣ main.yml           ← 스케줄 자동 실행 워크플로우
 ┃     └ test_run.yml       ← Push 시 즉시 테스트용 워크플로우
 ┣ data/
 ┃  ┣ 종목명.json
 ┃  ┣ 종목명_test.json      ← 테스트용 소량 종목 목록
 ┃  ┣ 종목명_keyword.json
 ┃  ┣ 제외단어.json
 ┃  └ 특징주.csv       ← 과거 급등 이력 (Gemini 분석용)
 ┣ execution/
 ┃  └ github_종목명_news.py
 └ requirements.txt
```

> `.env`, `token.json`, `credentials.json`은 **GitHub Secrets**로 대체.

---

## 4. GitHub Secrets 등록 목록

GitHub 레포 → `Settings` → `Secrets and variables` → `Actions` → `New repository secret`에 아래 항목을 모두 등록.

| Secret 이름 | 내용 |
|---|---|
| `NAVER_CLIENT_ID` | 네이버 API 클라이언트 ID |
| `NAVER_CLIENT_SECRET` | 네이버 API 클라이언트 Secret |
| `GEMINI_API_KEY` | Gemini API 키 |
| `TELEGRAM_BOT_TOKEN` | 텔레그램 봇 토큰 |
| `TELEGRAM_CHAT_ID` | 텔레그램 채팅 ID |
| `GMAIL_USER` | 수신 이메일 주소 |
| `NOTION_API_KEY` | 노션 통합 API 키 |
| `GMAIL_TOKEN_JSON` | `token.json` 파일 전체 텍스트 |
| `GMAIL_CREDENTIALS_JSON` | `credentials.json` 파일 전체 텍스트 |
| `SUPABASE_URL` | Supabase 프로젝트 URL |
| `SUPABASE_KEY` | Supabase **service_role (Secret) 마스터 키** ← ⚠️ anon 키가 아닌 서버 전용 키 사용 |

---

## 5. 주요 기능

### 1. 5단계 노이즈 필터링
단순 키워드 검색의 한계를 극복하기 위해 수집된 뉴스에 대해 엄격한 필터링을 거칩니다.

1. **시간 필터링**: 실행 시점 기준 과거 12시간 이내의 기사만 통과
2. **도메인 필터링**: 스포츠(`sports`), 연예(`entertain`), 블로그/포스트(`post`), 동영상(`tv`) 도메인 원천 차단
3. **태그 필터링**: 대괄호 `[ ]` 내에 `[단독]`, `[속보]`, `[특징주]`, `[공시]` 외의 불필요한 태그가 있는 기사 제외
4. **제외 단어 필터링**: `제외단어.json`에 포함된 단어가 제목에 있을 경우 제외
5. **종목명 정확도 필터링**: 유사한 이름의 파생 종목(예: BGF vs BGF리테일) 기사가 섞이는 것을 방지

### 2. Hybrid 클러스터링 (유사 기사 그룹화)
동일한 이슈로 쏟아지는 중복 기사를 묶어 대표 기사 1개만 노출합니다.

* 1차: 제목 특수문자 제거 후 토큰(Token) 집합의 교집합 개수 검사 (빠른 처리)
* 2차: `difflib.SequenceMatcher`를 활용한 문자열 유사도 검사 (0.6 이상 매칭 시 동일 기사로 간주)

### 3. AI 패턴 분석 (Gemini)
단순 요약이 아닌 **과거 데이터 기반의 추론**을 수행합니다.

* `data/특징주.csv`에 기록된 과거 종목들의 상승 논리와 오늘 수집된 뉴스를 대조합니다.
* API 서버 지연 등에 대비하여 `gemini-3-flash-preview` → `gemini-2.5-flash` 순으로 Fallback(재시도) 로직이 구현되어 있습니다.

### 4. 다중 채널 발송 포맷팅

* **Telegram**: HTML 파싱을 적용하여 주요 제목과 강조 사항을 모바일에서 가독성 있게 전달
* **Notion**: Blocks API를 사용하여 토글, 인용구, 배경색 등이 적용된 구조화된 페이지 자동 생성 (100개 단위 Chunk 전송)
* **Gmail**: MIMEText와 인라인 HTML CSS를 적용하여 직관적인 브리핑 이메일 발송

---

## 6. 처리 흐름

```
[GitHub Actions 실행 트리거 (스케줄 or 수동)]
       ↓
[환경 감지: GITHUB_ACTIONS 환경변수 확인]
  ├── GitHub: Secrets → Secrets 데이터를 메모리(RAM)에 직접 로드
  └── 로컬: .env 파일 로드
       ↓
[종목명.json 로드 → 네이버 API 순차 수집]
       ↓
[5단계 필터링 → 종목명 중복 방지]
       ↓
[Hybrid 클러스터링] → 대표 기사 선별
       ↓
[Supabase INSERT] → public.stack_news 테이블
       ↓
[특징주.csv 로드 (data/ 폴더)]
       ↓
[Gemini AI 분석] (모델 폴백: gemini-3-flash → gemini-2.5-flash → gemini-1.5-flash)
       ↓
[KST 시간 기준으로 오전/오후 판단 → 제목 생성]
       ↓
[멀티 채널 발송: Telegram + Notion + Gmail]
```
<img width="759" height="394" alt="image" src="https://github.com/user-attachments/assets/efea6e7c-93b4-4358-8cca-b3ec90722625" />

---

## 7. main.yml

name: Daily News Briefing Crawler

on:
  schedule:
    - cron: '30 22 * * *'  # 매일 한국시간 오전 7시 30분 (UTC 22:30) 실행
    - cron: '30 10 * * *'  # 매일 한국시간 오후 7시 30분 (UTC 10:30) 실행
  workflow_dispatch:       # 수동 버튼 생성용

jobs:
  run-crawler:
    runs-on: ubuntu-latest
    timeout-minutes: 30    # 🔥 추가된 부분: 30분 초과 시 작업 강제 종료

    permissions:
      contents: read
      
    steps:
    - name: 📦 리포지토리 복사해오기
      uses: actions/checkout@v6

    - name: 🐍 파이썬 환경 세팅
      uses: actions/setup-python@v6
      with:
        python-version: '3.11'

    - name: 🛠️ 필요한 라이브러리 설치
      run: |
        pip install requests python-dotenv python-telegram-bot google-genai google-api-python-client google-auth-httplib2 google-auth-oauthlib supabase
        
    - name: 🚀 새싹 스크립트(github_종목명_news.py) 실행
      env:
        # 등록한 Secrets 변수들을 시스템에 연결
        NAVER_CLIENT_ID: ${{ secrets.NAVER_CLIENT_ID }}
        NAVER_CLIENT_SECRET: ${{ secrets.NAVER_CLIENT_SECRET }}
        gemini: ${{ secrets.GEMINI_API_KEY }}
        telegram_chat_id: ${{ secrets.TELEGRAM_CHAT_ID }}
        TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
        GMAIL_USER: ${{ secrets.GMAIL_USER }}
        notion: ${{ secrets.NOTION_API_KEY }}
        GMAIL_TOKEN_JSON: ${{ secrets.GMAIL_TOKEN_JSON }}
        GMAIL_CREDENTIALS_JSON: ${{ secrets.GMAIL_CREDENTIALS_JSON }}
        SUPABASE_URL: ${{ secrets.SUPABASE_URL }}
        SUPABASE_KEY: ${{ secrets.SUPABASE_KEY }}
        NOTION_DATABASE_ID: ${{ secrets.NOTION_DATABASE_ID }}
      run: |
        python execution/github_종목명_news.py

