# Stock News Crawler

> 관심 종목 뉴스를 수집·정제하고, Gemini로 시장 관점의 브리핑을 생성해 Gmail과 Notion으로 전달하는 자동화 프로젝트입니다.

## Public Repository 안내

이 저장소는 포트폴리오 공개용입니다. 실제 스케줄 실행과 비밀값 관리는 별도의 private 저장소에서 수행합니다.

- GitHub Actions 워크플로우는 공개 저장소에 포함하지 않습니다.
- API 키, OAuth 토큰, 데이터베이스 키 등 비밀값은 커밋하지 않습니다.
- `data/`에는 구조와 실행 방식을 보여 주기 위한 축약 샘플만 포함합니다.
- 운영 데이터와 실행 이력은 공개하지 않습니다.

## 문제와 접근

여러 관심 종목의 최신 기사를 일일이 확인하면 중복 기사와 비관련 콘텐츠 때문에 시간이 많이 듭니다. 이 프로젝트는 최근 12시간의 뉴스를 병렬 수집한 뒤 규칙 기반 필터와 유사도 기반 중복 제거를 적용하고, 정제 전후의 토큰 사용량을 측정해 AI 요약 비용 절감 효과까지 추적합니다.

## 주요 기능

- 네이버 뉴스 검색 API를 이용한 관심 종목별 뉴스 수집
- 최대 5개 종목의 비동기 병렬 처리
- 최근 12시간, 한국어 제목, 종목명 포함 여부 등 기본 적합성 검사
- 스포츠·연예 등 차단 도메인, 허용되지 않은 대괄호 태그, 제외 단어 기반 노이즈 제거
- 짧은 종목명이 긴 종목명 기사에 잘못 매칭되는 문제 방지
- 토큰 교집합과 `difflib.SequenceMatcher`를 결합한 유사 기사 클러스터링
- Supabase `stack_news` 테이블에 링크 기준 upsert, 최대 500건 단위 저장
- 필터 전후 뉴스 수·제외 사유·중복 제거 수·Gemini 토큰 사용량을 `pipeline_run_metrics`에 기록
- Gemini 모델 폴백과 재시도
- Gmail HTML 브리핑 및 Notion 블록 페이지 생성
- KST 기준 오전·오후 보고서 제목과 단계별 실행 시간 로그

## 처리 흐름

```text
공개 샘플 종목 목록
  → 네이버 뉴스 병렬 수집
  → 12시간/언어/종목명 적합성 검사
  → 도메인·태그·제외 단어 필터
  → 유사 기사 클러스터링
  → Supabase 뉴스 및 파이프라인 지표 저장
  → 필터 전후 Gemini 입력 토큰 비교
  → AI 브리핑 생성
  → Gmail / Notion 전달
```

## 저장소 구조

```text
.
├─ data/
│  ├─ 종목명_public.json
│  ├─ 종목명_keyword_public.json
│  ├─ 제외단어_public.json
│  └─ 특징주_public.csv
├─ execution/
│  └─ github_종목명_news.py
├─ .gitignore
├─ requirements.txt
└─ README.md
```

공개 코드에서는 위의 `_public` 샘플 파일을 읽도록 경로를 분리했습니다. private 저장소의 운영 파일과 GitHub Actions 설정은 변경하거나 복사하지 않습니다.

## 기술 스택

- Python 3.11
- `asyncio`, `requests`, `difflib`
- Naver Search API
- Google Gemini API (`google-genai`)
- Supabase
- Gmail API (OAuth 2.0)
- Notion API

## 실행 준비

1. 저장소를 내려받고 가상환경을 만듭니다.
2. `pip install -r requirements.txt`로 의존성을 설치합니다.
3. 루트에 `.env`를 만들고 필요한 환경 변수를 설정합니다.
4. 샘플 데이터 형식을 유지한 채 관심 종목과 필터 단어를 조정합니다.
5. `python execution/github_종목명_news.py`를 실행합니다.

필요한 환경 변수:

| 변수 | 용도 |
|---|---|
| `NAVER_CLIENT_ID` | 네이버 검색 API 클라이언트 ID |
| `NAVER_CLIENT_SECRET` | 네이버 검색 API 클라이언트 Secret |
| `gemini` | Gemini API 키 |
| `SUPABASE_URL` | Supabase 프로젝트 URL |
| `SUPABASE_KEY` | 서버 측 DB 작업용 키 |
| `GMAIL_USER` | Gmail 발신/수신 계정 |
| `GMAIL_TOKEN_JSON` | GitHub Actions 환경에서 사용할 OAuth 토큰 JSON |
| `notion` | Notion Integration 키 |
| `NOTION_DATABASE_ID` | 결과를 저장할 Notion 데이터베이스 ID |
| `TELEGRAM_BOT_TOKEN` | 현재 실행 전 필수값 검사에 사용되는 봇 토큰 |
| `telegram_chat_id` | 현재 실행 전 필수값 검사에 사용되는 채팅 ID |

로컬 Gmail 인증은 `credentials.json`과 `token.json`을 사용합니다. 이 파일들과 `.env`는 `.gitignore`에 포함되어 있으며 절대 커밋하면 안 됩니다.

## 공개 데이터 형식

`종목명_public.json`, `종목명_keyword_public.json`, `제외단어_public.json`은 JSON 문자열 배열입니다.

```json
["삼성전자", "SK하이닉스"]
```

`특징주_public.csv`는 데이터 구조를 보여 주기 위한 축약 샘플입니다. 현재 공개 코드에서는 직접 읽지 않으며, 실제 운영 데이터는 포함하지 않습니다.

## 운영 및 보안 설계

- 자동 스케줄은 private 저장소의 GitHub Actions에서만 실행
- 인증정보는 로컬 `.env` 또는 GitHub Secrets에서만 주입
- 공개 저장소에는 워크플로우와 운영 데이터 미포함
- 예외 로그에는 자격 증명 값을 출력하지 않고 오류 유형만 기록
- Supabase 적재는 뉴스 링크를 충돌 키로 사용해 중복 저장 방지

## 최근 반영 내용

- 필터 제외 사유와 키워드별 제외 건수 집계
- 필터 적용 전후 뉴스 기준 Gemini 입력 토큰 비교
- 입력·출력·추론·전체 토큰과 절감률 기록
- `pipeline_run_metrics` 실행 단위 관측성 추가
- Supabase Python 클라이언트 기반 upsert로 저장 로직 정리
- 최신 Gemini 모델 폴백 및 모델별 재시도 정책 반영
- Gmail 인증 처리와 Notion 블록 변환 로직 개선

## 참고

이 프로젝트의 결과는 정보 정리와 개인 연구를 위한 것으로, 투자 판단이나 수익을 보장하지 않습니다.