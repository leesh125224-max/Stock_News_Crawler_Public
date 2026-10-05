import os
import json
import time
import requests
import re
import csv
import difflib
import asyncio
import base64
import urllib.request
from email.mime.text import MIMEText
from datetime import datetime, timedelta, timezone
from collections import Counter
from dotenv import load_dotenv

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# Third-party libraries
from google import genai
# from telegram import Bot

try:
    import ctypes
    ctypes.windll.kernel32.SetConsoleTitleW(f"📊 오늘의 뉴스 브리핑 수집기")
except:
    pass

# --- Configuration & Setup ---

# Load .env or Secrets
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE_DIR, '..', '.env')

if not os.getenv("GITHUB_ACTIONS"):
    load_dotenv(ENV_PATH)

# Keys
NAVER_CLIENT_ID = os.getenv("NAVER_CLIENT_ID")
NAVER_CLIENT_SECRET = os.getenv("NAVER_CLIENT_SECRET")
GEMINI_API_KEY = os.getenv("gemini")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("telegram_chat_id")
GMAIL_USER = os.getenv("GMAIL_USER") # Used as 'to' address
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# Credentials & Token (File API)
SCOPES = ['https://www.googleapis.com/auth/gmail.send']
CREDENTIALS_FILE = os.path.join(BASE_DIR, '..', 'credentials.json')
TOKEN_FILE = os.path.join(BASE_DIR, '..', 'token.json')

# File Paths relative to execution/
DATA_DIR = os.path.join(BASE_DIR, '..', 'data')
STOCK_NAMES_FILE = os.path.join(DATA_DIR, '종목명_public.json')
EXCLUDE_WORDS_FILE = os.path.join(DATA_DIR, '제외단어_public.json')

# Settings
API_URL = "https://openapi.naver.com/v1/search/news.json"
DISPLAY_COUNT = 100
SIMILARITY_THRESHOLD = 0.6  # 0.0 ~ 1.0
FILTER_VERSION = "noise-filter-metrics-v1"

# --- Database Functions ---

def get_supabase_client():
    """Create a server-side Supabase client. Never print credentials."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return None
    from supabase import create_client
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def save_to_history(items):
    """Save processed items to persistent history DB (Supabase)."""
    if not items:
        return 0

    try:
        supabase = get_supabase_client()
        if supabase is None:
            print("Warning: Supabase credentials not found. Skipping DB save.")
            return 0

        data_list = [
            {
                "stock_name": item["stock"],
                "title": item["title"],
                "pub_date": item["pub_date"],
                "pub_time": item["pub_time"],
                "link": item["link"],
            }
            for item in items
        ]
        supabase.table("stack_news").upsert(
            data_list,
            on_conflict="link",
            ignore_duplicates=True,
        ).execute()
        return len(data_list)
    except Exception as e:
        print(f"Supabase Client Error: {type(e).__name__}")
        return 0


def save_pipeline_run_metrics(metrics):
    """Store one aggregate metrics row for the current crawler run."""
    try:
        supabase = get_supabase_client()
        if supabase is None:
            print("Warning: Supabase credentials not found. Skipping metrics save.")
            return False

        payload = {
            "github_run_id": os.getenv("GITHUB_RUN_ID"),
            "github_sha": os.getenv("GITHUB_SHA"),
            "filter_version": FILTER_VERSION,
            "model_name": metrics.get("model_name"),
            "fetched_count": metrics.get("fetched_count", 0),
            "baseline_news_count": metrics.get("baseline_news_count", 0),
            "keyword_excluded_count": metrics.get("keyword_excluded_count", 0),
            "before_dedup_count": metrics.get("before_dedup_count", 0),
            "duplicate_removed_count": metrics.get("duplicate_removed_count", 0),
            "after_dedup_count": metrics.get("after_dedup_count", 0),
            "llm_input_news_count": metrics.get("llm_input_news_count", 0),
            "baseline_input_tokens": metrics.get("baseline_input_tokens"),
            "actual_counted_input_tokens": metrics.get("actual_counted_input_tokens"),
            "actual_input_tokens": metrics.get("actual_input_tokens"),
            "input_tokens_saved": metrics.get("input_tokens_saved"),
            "input_token_reduction_pct": metrics.get("input_token_reduction_pct"),
            "output_tokens": metrics.get("output_tokens"),
            "thought_tokens": metrics.get("thought_tokens"),
            "total_tokens": metrics.get("total_tokens"),
            "filter_reason_counts": metrics.get("filter_reason_counts", {}),
            "excluded_keyword_counts": metrics.get("excluded_keyword_counts", {}),
        }
        supabase.table("pipeline_run_metrics").insert(payload).execute()
        print("[METRICS] pipeline_run_metrics 저장 완료")
        return True
    except Exception as e:
        print(f"[METRICS] 저장 실패: {type(e).__name__}: {e}")
        return False



# --- Utility Functions ---

def clean_html(raw_html):
    cleanr = re.compile('<.*?>')
    cleantext = re.sub(cleanr, '', raw_html)
    return cleantext.replace('&quot;', '"').replace('&apos;', "'").replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')

def parse_pub_date(pub_date_str):
    try:
        dt = datetime.strptime(pub_date_str, "%a, %d %b %Y %H:%M:%S %z")
        return dt
    except ValueError:
        return None

def load_json(filepath):
    try:
        with open(filepath, 'r', encoding='utf-8-sig') as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"File not found: {filepath}")
        return []

# --- Core Logic ---

def fetch_news(stock_name):
    headers = {
        "X-Naver-Client-Id": NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET
    }
    params = {
        "query": stock_name,
        "display": DISPLAY_COUNT,
        "sort": "date"
    }
    try:
        response = requests.get(API_URL, headers=headers, params=params, timeout=10)
        response.raise_for_status()
        return response.json().get('items', [])
    except Exception as e:
        print(f"Error fetching {stock_name}: {e}")
        return []

def filter_news(stock_name, items, exclude_words, longer_partners):
    """Return post-filter items, pre-noise-filter baseline items, and reason counts."""
    valid_items = []
    baseline_items = []
    filter_counts = Counter()
    excluded_keyword_counts = Counter()

    KST = timezone(timedelta(hours=9))
    cutoff_time = datetime.now(KST) - timedelta(hours=12)
    blocked_domains = [
        "sports.news.naver.com", "m.sports.naver.com",
        "entertain.naver.com", "m.entertain.naver.com",
        "post.naver.com", "tv.naver.com",
    ]
    allowed_tags = ["단독", "속보", "특징주", "공시"]

    for item in items:
        title = clean_html(item.get("title", ""))
        link = item.get("link") or item.get("originallink") or ""
        dt = parse_pub_date(item.get("pubDate", ""))

        # Eligibility rules are excluded from the token baseline because these
        # records cannot be part of the 12-hour stock-news report.
        if dt is None:
            filter_counts["invalid_date"] += 1
            continue
        if dt < cutoff_time:
            filter_counts["outside_12h"] += 1
            continue
        if not re.search(r"[ㄱ-ㅎㅏ-ㅣ가-힣]", title):
            filter_counts["non_korean"] += 1
            continue
        if stock_name not in title:
            filter_counts["stock_name_missing"] += 1
            continue
        if any(partner in title for partner in longer_partners):
            filter_counts["longer_stock_name"] += 1
            continue

        candidate = {
            "stock": stock_name,
            "title": title,
            "link": link,
            "pub_date": dt.strftime("%Y-%m-%d"),
            "pub_time": dt.strftime("%H:%M:%S"),
        }
        baseline_items.append(candidate.copy())

        # Noise filters measured against the baseline above.
        if any(domain in link for domain in blocked_domains):
            filter_counts["blocked_domain"] += 1
            continue

        brackets = re.findall(r"\[(.*?)\]", title)
        if any(tag not in allowed_tags for tag in brackets):
            filter_counts["disallowed_bracket_tag"] += 1
            continue

        matched_keyword = next((word for word in exclude_words if word in title), None)
        if matched_keyword:
            filter_counts["excluded_keyword"] += 1
            excluded_keyword_counts[matched_keyword] += 1
            continue

        valid_items.append(candidate)

    return (
        valid_items,
        baseline_items,
        dict(filter_counts),
        dict(excluded_keyword_counts),
    )

def get_clean_tokens(text):
    """특수문자 제거 후 2글자 이상 단어만 추출 (집합 set 반환)"""
    # 1. [속보] 같은 대괄호 제거
    text = re.sub(r'\[.*?\]|\(.*?\)|\<.*?\>', '', text)
    # 2. 특수문자 제거 (한글, 영문, 숫자만 남김)
    words = re.findall(r'\w+', text)
    # 3. 2글자 이상만 남김
    return set(w for w in words if len(w) >= 2)

def cluster_similar_items(items):
    """
    Cluster news by similarity and return representative items with count info.
    returns: List of dicts (reps). Title is updated if count > 1.
    """
    if not items:
        return []

    # [User Request] Sort by date ASC (Oldest first) so the representative is the oldest value.
    # Naver API usually returns Newest first, so we reverse/sort.
    items = sorted(items, key=lambda x: (x['pub_date'], x['pub_time']))

    clusters = []
    
    for item in items:
        matched = False
        item_title = item['title']
        item_tokens = get_clean_tokens(item_title)
        
        for cluster in clusters:
            rep_title = cluster['rep']['title']
            rep_tokens = get_clean_tokens(rep_title)
            
            # [Check 1] Token Overlap (Fast)
            intersection_count = len(item_tokens & rep_tokens)
            is_token_match = intersection_count >= 3 # TOKEN_OVERLAP_THRESHOLD
            
            # [Check 2] Difflib Ratio (Slow - Fallback)
            is_difflib_match = False
            if not is_token_match:
                ratio = difflib.SequenceMatcher(None, rep_title, item_title).ratio()
                if ratio >= SIMILARITY_THRESHOLD:
                    is_difflib_match = True
            
            # [Final Decision] OR Condition
            if is_token_match or is_difflib_match:
                cluster['count'] += 1
                cluster['others'].append(item_title)
                matched = True
                break
        
        if not matched:
            clusters.append({'rep': item, 'count': 1, 'others': []})
    
    # Format Results
    results = []
    for c in clusters:
        item = c['rep']
        count = c['count']
        if count > 1:
            item['title'] = f"{item['title']} (외 {count-1}건)"
        results.append(item)
        
    return results

def format_news_report(all_items, keywords):
    """
    Format the already clustered items into a report text.
    """
    report_lines = []
    summary_input = []  # For AI
    
    # 1. Group by Stock
    stock_map = {}
    for item in all_items:
        s = item['stock']
        if s not in stock_map:
            stock_map[s] = []
        stock_map[s].append(item)
        
    # 2. Generate Text
    for stock, items in stock_map.items():
        if not items:
            continue
            
        # Sort items within stock by date and time in descending order (newest first)
        items.sort(key=lambda x: (x.get('pub_date', ''), x.get('pub_time', '')), reverse=True)
            
        # [HTML 스타일링] 종목명을 굵고 크게 표시 (폰트 18px)
        report_lines.append(f"<div style='margin-top: 15px; margin-bottom: 5px;'><b style='font-size: 18px;'>[{stock}]</b></div>")
        report_lines.append("<table style='border-collapse: collapse; font-size: 15px; width: 100%; max-width: 800px;'>")
        
        for item in items:
            title = item['title']
            # link = item['link'] 
            
            # Title already includes "(외 N건)" if processed
            # [Date Format] YYYY-MM-DD -> MM-DD
            date_str = item['pub_date'][5:] 
            # [Time Format] HH:MM:SS -> HH:MM
            time_str = item['pub_time'][:5]
            
            # Check for keyword highlighting
            # 종목명 자체에 '투자', '개발' 등이 들어간 경우 오작동(전체 하이라이트) 방지를 위해
            # 임시로 뉴스 제목에서 해당 '종목명' 텍스트를 공백으로 치환한 뒤 남은 순수 제목 안건에서만 키워드 존재 여부를 검사합니다.
            title_without_stock = title.replace(stock, "")
            is_highlighted = any(kw in title_without_stock for kw in keywords)
            row_style = "background-color: #fff9c4;" if is_highlighted else ""
            
            # Use table row for left/right alignment
            report_lines.append(
                f"<tr style='{row_style}'>"
                f"<td style='padding: 1px 0; color: #333;'>- {title}</td>"
                f"<td style='padding: 1px 0; text-align: right; color: #666; white-space: nowrap; width: 100px;'>{date_str}, {time_str}</td>"
                f"</tr>"
            )
            
            # Prepare input for AI
            summary_input.append(f"[{date_str} {time_str}] {title}")
            
        report_lines.append("</table>")

    return "\n".join(report_lines), "\n".join(summary_input)

async def generate_ai_summary(news_text_list, baseline_news_text):
    """Count pre/post-filter input tokens and generate only the post-filter report."""
    empty_metrics = {
        "model_name": None,
        "baseline_input_tokens": None,
        "actual_counted_input_tokens": None,
        "actual_input_tokens": None,
        "input_tokens_saved": None,
        "input_token_reduction_pct": None,
        "output_tokens": None,
        "thought_tokens": None,
        "total_tokens": None,
    }
    if not GEMINI_API_KEY:
        return "Gemini API Key missing.", empty_metrics
    if not news_text_list:
        return "No news to summarize.", empty_metrics

    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        prompt = f"""
# Role (역할)
당신은 대한민국 주식 시장의 모멘텀과 테마주를 분석하는 '수석 퀀트 애널리스트'입니다. 

# Context (맥락)
제공되는 데이터는 오늘 발생한 주요 뉴스 헤드라인과 본문 요약본([Today])입니다.

# Task (임무)
[Today] 데이터를 분석하여, 내일 주식 시장에서 **강력한 주가 상승 모멘텀(테마)으로 작용할 수 있는 핵심 뉴스**를 선별하십시오.

# Analysis Logic (분석 로직)
1. **파급력 평가**: 단순한 기업 홍보(예: 일상적인 게임 업데이트, 단순 MOU 체결, 팝업스토어 오픈)는 철저히 배제하십시오. 
2. **핵심 재료 포착**: 실질적인 매출 증가, 대규모 수주, 정부 정책 수혜, FDA 등 주요 기관 승인, M&A 등 주가를 움직일 만한 '강력한 재료'에 집중하십시오.
3. **엄격한 필터링**: 억지로 테마를 만들어내지 마십시오. 파급력이 높은 뉴스가 없다면 "금일 주식에 유의미한 영향을 미칠 강력한 재료는 포착되지 않았습니다."라고 출력하십시오.

# Input Data
[Today]
{news_text_list}

# Output Format (출력 형식 - 중요)
결과는 메일로 발송할 것입니다. 아래 마크다운 양식에 맞춰 가독성 좋은 '보고서 형태'로 작성해 주세요. 서론이나 부가 설명은 생략하십시오.

## 📢 오늘의 종목 분석 (종목 개수에 제한 받지 말고 포착된 종목 모두 포함해줘)

### 1.  [종목명] (예상 테마: 000, 관련 종목 :  000,000 ([종목명]이외, 7개 이내로 작성, 없으면 빈칸))
**뉴스** : **"오늘 뉴스 제목 인용"  (뉴스 발행 시간 인용)**
과거 상승 이유 : "과거 종목의 상승 이유 인용"
예상 파급력 : **높음**/**중간**/**낮음** 
분석 및 상승 논리 : 해당 뉴스가 왜 주가 상승으로 이어질 수 있는지 경제적, 산업적 맥락에서 2문장 이내로 간략히 요약 (서술식 배제, 명사형 종결 사용)

(포착 항목 동일 양식 반복)

---

## 💡 요약 및 투자 포인트
# 1. (오늘 시장의 전반적인 특징 요약)
# 2. (주목해야 할 특정 섹터 흐름)
# 3. (리스크 요인 또는 특이사항)
"""
        baseline_prompt = prompt.replace(news_text_list, baseline_news_text, 1)
        # Model quality order comes from the offline evaluation.
        # Retry counts are kept separate from model priority so the operational
        # fallback policy is explicit: 3.7 once, 3.8 once, 3.5 twice, 3.6 once.
        model_plan = [
            ("gemini-3.7-flash", 1),
            ("gemini-3.8-flash", 1),
            ("gemini-3.5-flash", 2),
            ("gemini-3.6-flash", 1),
        ]
        model_attempt_limits = dict(model_plan)
        models_to_try = [
            model_name
            for model_name, attempt_count in model_plan
            for _ in range(attempt_count)
        ]
        model_attempt_counts = Counter()

        for attempt, model_name in enumerate(models_to_try):
            model_attempt_counts[model_name] += 1
            current_model_attempt = model_attempt_counts[model_name]
            max_model_attempts = model_attempt_limits[model_name]
            baseline_count = None
            actual_count = None
            try:
                baseline_response, actual_response = await asyncio.gather(
                    asyncio.to_thread(
                        client.models.count_tokens,
                        model=model_name,
                        contents=baseline_prompt,
                    ),
                    asyncio.to_thread(
                        client.models.count_tokens,
                        model=model_name,
                        contents=prompt,
                    ),
                )
                baseline_count = getattr(baseline_response, "total_tokens", None)
                actual_count = getattr(actual_response, "total_tokens", None)
                print(
                    f"[TOKEN COUNT] model={model_name}, "
                    f"before_filter={baseline_count}, after_filter={actual_count}"
                )
            except Exception as count_error:
                print(
                    f"[TOKEN COUNT] {model_name} 계산 실패. "
                    f"보고서 생성은 계속합니다: {type(count_error).__name__}"
                )

            try:
                # Only the post-filter prompt generates a report.
                response = await asyncio.to_thread(
                    client.models.generate_content,
                    model=model_name,
                    contents=prompt,
                    config=genai.types.GenerateContentConfig(temperature=0.0),
                )
                usage = response.usage_metadata
                actual_input_tokens = getattr(usage, "prompt_token_count", 0) or 0
                output_tokens = getattr(usage, "candidates_token_count", 0) or 0
                thought_tokens = getattr(usage, "thoughts_token_count", 0) or 0
                total_tokens = getattr(usage, "total_token_count", 0) or 0

                saved = None
                reduction_pct = None
                if baseline_count is not None and actual_count is not None:
                    saved = baseline_count - actual_count
                    if baseline_count:
                        reduction_pct = round(saved / baseline_count * 100, 4)

                metrics = {
                    "model_name": model_name,
                    "baseline_input_tokens": baseline_count,
                    "actual_counted_input_tokens": actual_count,
                    "actual_input_tokens": actual_input_tokens,
                    "input_tokens_saved": saved,
                    "input_token_reduction_pct": reduction_pct,
                    "output_tokens": output_tokens,
                    "thought_tokens": thought_tokens,
                    "total_tokens": total_tokens,
                }
                print(
                    f"[TOKEN USAGE] model={model_name}, input={actual_input_tokens}, "
                    f"output={output_tokens}, thoughts={thought_tokens}, total={total_tokens}"
                )
                print(f"[Gemini] 필터 후 뉴스로 보고서 1개 생성: {len(news_text_list)}자")
                return response.text, metrics
            except Exception as e:
                if attempt == len(models_to_try) - 1:
                    return (
                        f"AI Summary Failed after trying all models: {e}",
                        empty_metrics,
                    )
                print(
                    f"[알림] {model_name} 오류 발생 "
                    f"(시도 {current_model_attempt}/{max_model_attempts}). "
                    f"10초 대기 후 다음 시도로 넘어갑니다... ({type(e).__name__})"
                )
                await asyncio.sleep(10)
    except Exception as e:
        return f"AI Summary Failed: {e}", empty_metrics

def get_gmail_service():
    creds = None
    token_str = os.getenv("GMAIL_TOKEN_JSON")

    if token_str:
        # GitHub Actions: 메모리에서 직접 처리
        token_info = json.loads(token_str)
        creds = Credentials.from_authorized_user_info(token_info, SCOPES)

        # 토큰 만료 시 갱신
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())

    else:
        # 로컬 환경
        if os.path.exists(TOKEN_FILE):
            creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                # token.json 없거나 갱신 불가 → 브라우저 로그인
                if not os.path.exists(CREDENTIALS_FILE):
                    print(f"Error: {CREDENTIALS_FILE} not found.")
                    return None
                flow = InstalledAppFlow.from_client_secrets_file(
                    CREDENTIALS_FILE, SCOPES)
                creds = flow.run_local_server(port=8080)

            # 로컬에서만 token.json 저장
            with open(TOKEN_FILE, 'w') as token:
                token.write(creds.to_json())

    try:
        service = build('gmail', 'v1', credentials=creds)
        return service
    except HttpError as error:
        print(f'An error occurred: {error}')
        return None



# async def send_telegram_message(bot, chat_id, message):
#     """Send message to Telegram, splitting if too long."""
#     MAX_LENGTH = 4000
#     
#     try:
#         if len(message) <= MAX_LENGTH:
#             await bot.send_message(chat_id=chat_id, text=message, parse_mode='HTML') # Use HTML for stability
#         else:
#             # Simple split
#             parts = [message[i:i+MAX_LENGTH] for i in range(0, len(message), MAX_LENGTH)]
#             for part in parts:
#                 await bot.send_message(chat_id=chat_id, text=part)
#     except Exception as e:
#         print(f"Telegram Error: {e}")

async def main_async():
    # 0. Check Keys
    if not (NAVER_CLIENT_ID and NAVER_CLIENT_SECRET and TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        print("Missing API Keys in .env")
        return

    script_start_time = time.time()
    print(f"--- Starting News Crawler --- [{datetime.now(timezone(timedelta(hours=9))).strftime('%H:%M:%S')}]")
    
    # 2. Load Data
    stock_names = load_json(STOCK_NAMES_FILE)
    exclude_words = load_json(EXCLUDE_WORDS_FILE)
    
    # [NEW] Load Keywords for Highlighting
    KEYWORD_FILE = os.path.join(DATA_DIR, '종목명_keyword_public.json')
    keywords = load_json(KEYWORD_FILE)
    
    print(f"Stocks: {len(stock_names)}, Excluded Words: {len(exclude_words)}, Keywords: {len(keywords)}")
    
    all_valid_news = []
    
    # 3-1. Prepare for Overlap Check (Longest Match Exclusion)
    # Sort stocks by length DESC to check longer names first (though mainly needed for filtering logic below)
    # We need a reference list of ALL stock names to check against.
    all_stock_names_set = set(stock_names)

    # 3. Crawl
    semaphore = asyncio.Semaphore(5)
    
    async def process_stock(stock_idx, stock):
        async with semaphore:
            stock_start = time.time()
            print(f"[{stock_idx+1} of {len(stock_names)}] Fetching {stock}... [{datetime.now(timezone(timedelta(hours=9))).strftime('%H:%M:%S')}]")
            # API 제한 방지를 위해 약간의 대기시간 추가
            await asyncio.sleep(0.1)
            raw_items = await asyncio.to_thread(fetch_news, stock)
            longer_partners = [
                name for name in all_stock_names_set
                if stock in name and len(name) > len(stock)
            ]
            (
                valid_items,
                baseline_items,
                filter_counts,
                excluded_keyword_counts,
            ) = filter_news(stock, raw_items, exclude_words, longer_partners)

            clustered_items = cluster_similar_items(valid_items)
            elapsed = time.time() - stock_start
            duplicate_removed = len(valid_items) - len(clustered_items)
            print(
                f"  > [{stock}] baseline={len(baseline_items)}, "
                f"after_filter={len(valid_items)}, clusters={len(clustered_items)} "
                f"({elapsed:.1f}s)"
            )
            return {
                "fetched_count": len(raw_items),
                "baseline_items": baseline_items,
                "before_dedup_count": len(valid_items),
                "duplicate_removed_count": duplicate_removed,
                "clustered_items": clustered_items,
                "filter_reason_counts": filter_counts,
                "excluded_keyword_counts": excluded_keyword_counts,
            }

    # 병렬로 전체 종목 검색
    tasks = [process_stock(i, stock) for i, stock in enumerate(stock_names)]
    results = await asyncio.gather(*tasks)

    baseline_news = []
    filter_reason_counts = Counter()
    excluded_keyword_counts = Counter()
    fetched_count = 0
    before_dedup_count = 0
    duplicate_removed_count = 0

    for result in results:
        fetched_count += result["fetched_count"]
        baseline_news.extend(result["baseline_items"])
        before_dedup_count += result["before_dedup_count"]
        duplicate_removed_count += result["duplicate_removed_count"]
        all_valid_news.extend(result["clustered_items"])
        filter_reason_counts.update(result["filter_reason_counts"])
        excluded_keyword_counts.update(result["excluded_keyword_counts"])

    pipeline_metrics = {
        "fetched_count": fetched_count,
        "baseline_news_count": len(baseline_news),
        "keyword_excluded_count": filter_reason_counts.get("excluded_keyword", 0),
        "before_dedup_count": before_dedup_count,
        "duplicate_removed_count": duplicate_removed_count,
        "after_dedup_count": len(all_valid_news),
        "llm_input_news_count": len(all_valid_news),
        "filter_reason_counts": dict(filter_reason_counts),
        "excluded_keyword_counts": dict(excluded_keyword_counts),
    }

    # Supabase 저장 (500개씩 청크)
    if all_valid_news:
        chunk_size = 500
        for i in range(0, len(all_valid_news), chunk_size):
            chunk = all_valid_news[i:i + chunk_size]
            print(f"Saving items [{i+1} ~ {i+len(chunk)}] to DB...")
            await asyncio.to_thread(save_to_history, chunk)

    if not all_valid_news:
        print("No valid news found today.")
        await asyncio.to_thread(save_pipeline_run_metrics, pipeline_metrics)
        return

    crawl_elapsed = time.time() - script_start_time
    print(f"\n[TIME] 크롤링 완료: {crawl_elapsed:.1f}s ({len(all_valid_news)}개 뉴스 수집)")

    # 4. Prepare the pre-filter baseline and post-filter report inputs.
    print("Preparing report...")
    _, baseline_summary_input_str = format_news_report(baseline_news, keywords)
    news_report_body, summary_input_str = format_news_report(all_valid_news, keywords)

    # 5. Count both prompts, but generate only one post-filter report.
    print("Counting tokens and generating AI Summary...")
    gemini_start = time.time()
    ai_summary, token_metrics = await generate_ai_summary(
        summary_input_str,
        baseline_summary_input_str,
    )
    gemini_elapsed = time.time() - gemini_start
    print(f"[TIME] Gemini API 완료: {gemini_elapsed:.1f}s")

    pipeline_metrics.update(token_metrics)
    await asyncio.to_thread(save_pipeline_run_metrics, pipeline_metrics)
    
    # 6. Send Telegram
    # [NEW] Refined Telegram Message Logic (Clean & Bold)
    KST = timezone(timedelta(hours=9))
    now = datetime.now(KST)
    am_pm = "오전" if now.hour < 12 else "오후"
    formatted_date = now.strftime('%y.%m.%d')
    report_title = f"{formatted_date} {am_pm} 종목 뉴스 브리핑"

    # tg_lines = []
    # tg_lines.append(f"📊 *{report_title}*")
    # tg_lines.append(f"{now.strftime('%Y-%m-%d %H:%M')}\n")
    # 
    # source_lines = ai_summary.split('\n')
    # for line in source_lines:
    #     line = line.strip()
    #     if not line:
    #          tg_lines.append("") # Keep empty lines for spacing
    #          continue
    #     
    #     # [수정된 로직] Allow-List 방식 (허용된 패턴만 통과)
    #     # 사족(설명)을 완벽하게 제거하기 위함.
    #     
    #     # 1. 헤더/종목명 (### 1. ...)
    #     if "### 1." in line or "### " in line:
    #          clean_line = line.replace("### ", "").replace("**", "")
    #          clean_line = clean_line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    #          tg_lines.append(f"<b>{clean_line}</b>")
    #          
    #     # 2. 뉴스 제목 (* 뉴스: ...)
    #     elif "뉴스" in line and ":" in line:
    #          # "과거 뉴스"는 제외해야 함
    #          if "과거 뉴스" in line:
    #              continue
    #              
    #          clean_line = line.replace("**", "")
    #          clean_line = clean_line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    #          tg_lines.append(clean_line)
    #          
    #     # 3. 그 외 설명문(사족) -> 과감히 삭제 (continue)
    #     else:
    #          continue

    # tg_message = "\n".join(tg_lines)
    # 
    # telegram_start = time.time()
    # print("Sending Telegram message...")
    # async with Bot(token=TELEGRAM_TOKEN) as bot:
    #      # Use HTML parse mode for better stability
    #     await send_telegram_message(bot, TELEGRAM_CHAT_ID, tg_message)
    # telegram_elapsed = time.time() - telegram_start
    # print(f"[TIME] Telegram 완료: {telegram_elapsed:.1f}s")

    # 7. Send Gmail (Simple HTML)
    gmail_start = time.time()
    print("Sending Gmail...")
    
    # [Simple HTML Converter]
    def simple_markdown_to_html(text):
        lines = text.split('\n')
        html_lines = []
        html_lines.append('<div dir="ltr" style="font-family: sans-serif; font-size: 15px; line-height: 1.5; color: #202124;">')
        
        first_item = True

        for i, line in enumerate(lines):
            line = line.strip()
            
            if not line:
                html_lines.append("<div style='height: 8px;'></div>")
                continue
                
            if line.startswith('---'):
                continue
            
            # 1. 메인 헤더 (##)
            if line.startswith('## '):
                content = line.replace('## ', '').replace('**', '')
                html_lines.append(f'<div style="font-size: 28px; font-weight: bold; margin-bottom: 25px; margin-top: 30px;">{content}</div>')
                
            # 2. 종목명 헤더 (###)
            elif line.startswith('### '):
                content = line.replace('### ', '').replace('**', '')
                parts = content.split('(', 1)
                title_part = parts[0].strip()
                meta_part = f"({parts[1]}" if len(parts) > 1 else ""
                
                if not first_item:
                    html_lines.append('<div style="margin-top: 20px;"></div>')
                first_item = False
                
                html_lines.append(f'<div style="font-size: 18px; font-weight: bold; color: inherit; margin-bottom: 4px;">{title_part}</div>')
                if meta_part:
                    # 👇👇👇 [여기가 '예상 테마, 관련 종목' 등 괄호 안 내용의 HTML 서식을 지정하는 부분입니다] 👇👇👇
                    html_lines.append(f'<div style="font-size: 14px; color: #5f6368; margin-bottom: 10px;">{meta_part}</div>')
                
            # 3. 뉴스
            elif line.startswith('**뉴스') or line.startswith('"뉴스') or line.startswith('뉴스'):
                 content = line.replace('**', '')
                 if ' :' in content: content = content.replace(' :', ':', 1)
                 content = content.replace('뉴스:', '').strip()
                 # [수정] 뉴스 내용이 더 잘 띄도록 이메일 렌더링 시 앞의 '뉴스 :' 글자까지 포함하여 텍스트 영역 전체에 노란색 형광펜(배경색) 효과를 추가했습니다.
                 html_lines.append(f'<div style="margin-bottom: 6px; line-height: 1.4;"><span style="background-color: #fff9c4;"><b>뉴스 : {content}</b></span></div>')

            # 4. 과거 상승 이유
            elif line.startswith('과거 상승 이유'):
                 content = line.replace('**', '')
                 if ' :' in content: content = content.replace(' :', ':', 1)
                 content = content.replace('과거 상승 이유:', '').strip()
                 html_lines.append(f'<div style="margin-bottom: 6px; line-height: 1.4;"><b style="color: #5f6368;">과거 상승 이유 : </b><span style="color: #202124;">{content}</span></div>')

            # 5. 예상 파급력
            elif line.startswith('예상 파급력'):
                 content = line.replace('**', '').strip()
                 content = content.replace('높음', '<span style="color: #d93025; font-weight: bold;">높음</span>')
                 content = content.replace('중간', '<span style="color: #f29900; font-weight: bold;">중간</span>')
                 content = content.replace('낮음', '<span style="color: #1e8e3e; font-weight: bold;">낮음</span>')
                 html_lines.append(f'<div style="margin-bottom: 12px; line-height: 1.4;"><b>{content}</b></div>')
                 
            # 6. ▶ (결론/요약 부분)
            elif line.startswith('▶'):
                 content = line.replace('▶', '').strip()
                 html_lines.append(f'<div style="padding: 1px 16px; background-color: #f8f9fa; border-left: 4px solid #1a73e8; border-radius: 0 4px 4px 0; color: #3c4043; font-size: 14px; margin-top: 10px; margin-bottom: 15px; line-height: 1.5;">{content}</div>')
                 
            # 7. 요약 리스트 (# 1. ...)
            elif line.startswith('# '):
                 content = line.replace('# ', '', 1).strip()
                 html_lines.append(f'<div style="margin-bottom: 8px;">✔️ <b>{content}</b></div>')
                 
            # 8. 그 외 일반 텍스트
            else:
                 content = line.replace('**', '')
                 if content.startswith('##'): 
                     html_lines.append(f'<div style="font-size: 28px; font-weight: bold; margin-top: 30px; margin-bottom: 15px;">{content.replace("##", "").strip()}</div>')
                 elif content.startswith('('):
                     html_lines.append(f'<div style="margin-top: 15px; margin-bottom: 15px; color: #666;">{content}</div>')
                 else:
                     html_lines.append(f'<div style="margin-bottom: 4px;">{content}</div>')

        # 뉴스 리스트 섹션
        html_lines.append('<div style="margin-top: 40px; margin-bottom: 15px;">')
        html_lines.append('<span style="font-size: 28px; font-weight: bold;">📰 수집된 전체 뉴스 목록</span>')
        html_lines.append('</div>')
        html_lines.append(f'<div style="font-family: sans-serif; background-color: #f9f9f9; padding: 15px; border-radius: 5px; max-width: 800px; display: inline-block; width: 100%; box-sizing: border-box;">{news_report_body}</div>')
        html_lines.append('</div>')
        
        return "".join(html_lines)

    html_message = simple_markdown_to_html(ai_summary)
    email_subject = report_title
    
    # Send as HTML (OAuth)
    await send_gmail_message(email_subject, html_message, mime_type='html')
    gmail_elapsed = time.time() - gmail_start
    print(f"[TIME] Gmail 완료: {gmail_elapsed:.1f}s")

    # 8. Send Notion (Blocks API)
    notion_start = time.time()
    print("Sending Notion...")
    try:
        # 노션 업로드에 3분(180초) 타임아웃 설정
        await asyncio.wait_for(send_notion_message(report_title, ai_summary), timeout=180)
        notion_elapsed = time.time() - notion_start
        print(f"[TIME] Notion 완료: {notion_elapsed:.1f}s")
    except asyncio.TimeoutError:
        print("Notion 전송 작업이 3분을 초과하여 강제 종료되었습니다.")
        notion_elapsed = time.time() - notion_start
    except Exception as e:
        print(f"Notion Error in Main: {e}")
        notion_elapsed = time.time() - notion_start

    total_elapsed = time.time() - script_start_time
    print(f"\n===== 전체 실행 완료 =====")
    print(f"[TIME] 크롤링:    {crawl_elapsed:.1f}s")
    print(f"[TIME] Gemini:    {gemini_elapsed:.1f}s")
    # print(f"[TIME] Telegram:  {telegram_elapsed:.1f}s")
    print(f"[TIME] Notion:    {notion_elapsed:.1f}s")
    print(f"[TIME] Gmail:     {gmail_elapsed:.1f}s")
    print(f"[TIME] 총 소요:   {total_elapsed:.1f}s ({total_elapsed/60:.1f}분)")
    print(f"=========================")

def simple_markdown_to_notion_blocks(ai_summary):
    blocks = []
    
    lines = ai_summary.split('\n')
    for line in lines:
        # AI가 줄바꿈 문자로 <br>을 출력했을 경우 텍스트에서 보이지 않게 제거합니다.
        line = line.replace('<br>', '').replace('<br/>', '').strip()
        if not line:
            # 노션에서는 빈 줄(공백)을 블럭으로 만들지 않고 무시하여 간격을 완전히 밀착시킵니다.
            continue
            
        if line.startswith('---'):
            blocks.append({"object": "block", "type": "divider", "divider": {}})
            continue
            
        if line.startswith('## '):
            content = line.replace('## ', '').replace('**', '')
            blocks.append({
                "object": "block", "type": "heading_1",
                "heading_1": {"rich_text": [{"type": "text", "text": {"content": content}}]}
            })
            
        elif line.startswith('### '):
            content = line.replace('### ', '').replace('**', '')
            parts = content.split('(', 1)
            title_part = parts[0].strip()
            meta_part = f"({parts[1]}" if len(parts) > 1 else ""
            
            # 1. 종목명 (메일 원본처럼 굵고 큰 제목 유지)
            blocks.append({
                "object": "block", "type": "heading_3",
                "heading_3": { "rich_text": [{"type": "text", "text": {"content": title_part}}], "is_toggleable": False }
            })
            
            # 2. (예상 테마...) 메일 원본처럼 바로 아래 줄에 작은 크기, 회색으로 분리
            if meta_part:
                blocks.append({
                    "object": "block", "type": "paragraph",
                    "paragraph": {"rich_text": [{"type": "text", "text": {"content": meta_part}, "annotations": {"color": "gray"}}]}
                })
            
        elif line.startswith('**뉴스') or line.startswith('"뉴스') or line.startswith('뉴스'):
             content = line.replace('**', '')
             if ' :' in content: content = content.replace(' :', ':', 1)
             content = content.replace('뉴스:', '').strip()
             # [수정] 노션 렌더링 시 "뉴스 :" 앞머리 글자까지 모두 포함하여 하나의 연속된 노란색 배경(yellow_background) 텍스트로 합쳤습니다.
             blocks.append({
                 "object": "block", "type": "paragraph",
                 "paragraph": {
                     "rich_text": [
                         {"type": "text", "text": {"content": f"뉴스 : {content}"}, "annotations": {"bold": True, "color": "yellow_background"}}
                     ]
                 }
             })

        elif line.startswith('과거 상승 이유'):
             content = line.replace('**', '')
             if ' :' in content: content = content.replace(' :', ':', 1)
             content = content.replace('과거 상승 이유:', '').strip()
             blocks.append({
                 "object": "block", "type": "paragraph",
                 "paragraph": {
                     "rich_text": [
                         {"type": "text", "text": {"content": "과거 상승 이유 : "}, "annotations": {"bold": True, "color": "gray"}},
                         {"type": "text", "text": {"content": content}}
                     ]
                 }
             })

        elif line.startswith('예상 파급력'):
             content = line.replace('**', '').strip()
             if ' :' in content: content = content.replace(' :', ':', 1)
             content = content.replace('예상 파급력:', '').strip()
             
             color = "default"
             if "높음" in content: color = "red"
             elif "중간" in content: color = "yellow"
             elif "낮음" in content: color = "green"
             
             blocks.append({
                 "object": "block", "type": "paragraph",
                 "paragraph": {
                     "rich_text": [
                         {"type": "text", "text": {"content": "예상 파급력 : "}, "annotations": {"bold": True}},
                         {"type": "text", "text": {"content": content}, "annotations": {"bold": True, "color": color}}
                     ]
                 }
             })
             
        elif line.startswith('▶'):
             content = line.replace('▶', '').strip()
             blocks.append({
                 "object": "block", "type": "quote",
                 "quote": {
                     "rich_text": [{"type": "text", "text": {"content": content}}],
                     "color": "blue_background"
                 }
             })
             
        elif line.startswith('# '):
             content = line.replace('# ', '', 1).strip()
             blocks.append({
                 "object": "block", "type": "bulleted_list_item",
                 "bulleted_list_item": {
                     "rich_text": [{"type": "text", "text": {"content": content}, "annotations": {"bold": True}}]
                 }
             })
             
        else:
             content = line.replace('**', '')
             if content.startswith('##'): 
                 blocks.append({
                     "object": "block", "type": "heading_2",
                     "heading_2": {"rich_text": [{"type": "text", "text": {"content": content.replace("##", "").strip()}}]}
                 })
             elif content.startswith('('):
                 blocks.append({
                     "object": "block", "type": "paragraph",
                     "paragraph": {"rich_text": [{"type": "text", "text": {"content": content}, "annotations": {"color": "gray"}}]}
                 })
             else:
                 blocks.append({
                     "object": "block", "type": "paragraph",
                     "paragraph": {"rich_text": [{"type": "text", "text": {"content": content}}]}
                 })
                 
    return blocks

async def send_notion_message(report_title, ai_summary):
    NOTION_API_KEY = os.getenv("notion")
    NOTION_DATABASE_ID = os.getenv("NOTION_DATABASE_ID")
    
    HEADERS = {
        'Authorization': f'Bearer {NOTION_API_KEY}',
        'Notion-Version': '2022-06-28',
        'Content-Type': 'application/json'
    }

    blocks = simple_markdown_to_notion_blocks(ai_summary)
    
    KST = timezone(timedelta(hours=9))
    now = datetime.now(KST)
    today_str = now.strftime('%Y-%m-%d')
    
    # 1. First batch (Max 100 blocks)
    first_chunk = blocks[:100]
    remaining_blocks = blocks[100:]
    
    data = {
        "parent": {"database_id": NOTION_DATABASE_ID},
        "properties": {
            "제목": {"title": [{"text": {"content": report_title}}]},
            "날짜": {"date": {"start": today_str}}
        },
        "children": first_chunk
    }

    req = urllib.request.Request(
        'https://api.notion.com/v1/pages',
        data=json.dumps(data).encode('utf-8'),
        headers=HEADERS,
        method='POST'
    )
    
    def send_api():
        start_time = time.time()  # 시작 시간 기록
        timeout_limit = 180
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                response_body = response.read().decode('utf-8')
                result_json = json.loads(response_body)
                page_id = result_json.get('id')
                print(f"Notion 첫 페이지 생성 성공 (Block 1~{len(first_chunk)}).")
                
                # 2. Add remaining blocks iteratively (Chunking)
                if page_id and remaining_blocks:
                    for i in range(0, len(remaining_blocks), 100):
                        
                        # [핵심 수정 부분] 반복문을 돌 때마다 스스로 시간 체크
                        elapsed_time = time.time() - start_time
                        if elapsed_time > timeout_limit:
                            print(f"[경고] Notion 블록 전송이 3분({elapsed_time:.1f}초)을 초과하여 내부 작업을 강제 중단합니다.")
                            break # 반복문을 빠져나가 스레드를 안전하게 종료시킴
                            
                        time.sleep(0.5) 
                        chunk = remaining_blocks[i : i + 100]
                        patch_data = {"children": chunk}
                        patch_req = urllib.request.Request(
                            f'https://api.notion.com/v1/blocks/{page_id}/children',
                            data=json.dumps(patch_data).encode('utf-8'),
                            headers=HEADERS,
                            method='PATCH'
                        )
                        with urllib.request.urlopen(patch_req, timeout=30) as patch_res:
                            print(f"[알림] Notion 블럭 전송 {100 + i + 1} ~ {100 + i + len(chunk)} 추가 성공.")
                            
                print("Notion 전송 작업을 종료합니다.")
                
        except Exception as e:
            print(f"Notion Error: {type(e).__name__}")
            if hasattr(e, 'status') or hasattr(e, 'code'):
                print(f"Status: {getattr(e, 'status', getattr(e, 'code', 'unknown'))}")
                
    await asyncio.to_thread(send_api)

async def send_gmail_message(subject, message_text, mime_type='plain'):
    """Send email via Gmail API (OAuth 2.0)."""
    service = get_gmail_service()
    if not service:
        print("Failed to get Gmail service.")
        return

    try:
        # Use UTF-8 explicitly
        message = MIMEText(message_text, mime_type, 'utf-8')
        message['to'] = GMAIL_USER
        # message['from'] = GMAIL_USER 
        message['subject'] = subject
        
        # Encode the message
        raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode()
        body = {'raw': raw_message}
    
        # Send (Retry logic for Rate Limit Exceeded)
        # 429 에러(Rate Limit) 발생 시 잠시 대기
        
        def send_api():
            try:
                service.users().messages().send(userId="me", body=body).execute()
            except HttpError as e:
                if e.resp.status == 429:
                    print("Rate limit exceeded. Waiting 3 seconds...")
                    time.sleep(3)
                    service.users().messages().send(userId="me", body=body).execute()
                else:
                    raise e

        await asyncio.to_thread(send_api)
        print("Gmail 전송하였습니다.")

    except HttpError as error:
        print(f'Gmail API Error: {error}')
    except Exception as e:
        print(f"Gmail Error: {e}")

def main():
    asyncio.run(main_async())

if __name__ == "__main__":
    main()
