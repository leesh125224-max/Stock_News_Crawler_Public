import os
import json
import time
import requests
import re
import csv
import difflib
import asyncio
import base64
from email.mime.text import MIMEText
from datetime import datetime, timedelta
from dotenv import load_dotenv

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# Third-party libraries
from google import genai
from telegram import Bot

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
STOCK_NAMES_FILE = os.path.join(DATA_DIR, '종목명.json')
EXCLUDE_WORDS_FILE = os.path.join(DATA_DIR, '제외단어.json')

# Settings
API_URL = "https://openapi.naver.com/v1/search/news.json"
DISPLAY_COUNT = 100
SIMILARITY_THRESHOLD = 0.6  # 0.0 ~ 1.0

# --- Database Functions ---

def save_to_history(items):
    """Save processed items to persistent history DB (Supabase). Bulk upsert version."""
    if not items:
        return 0
        
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("Warning: Supabase credentials not found. Skipping DB save.")
        return 0
        
    try:
        from supabase import create_client, Client
        supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
        
        # 전체 리스트를 한 번에 upsert (title 중복 시 조용히 스킵)
        data_list = [
            {
                "stock_name": item['stock'],
                "title": item['title'],
                "pub_date": item['pub_date'],
                "pub_time": item['pub_time'],
                "link": item['link']
            }
            for item in items
        ]
        
        supabase.table("stack_news").upsert(
            data_list,
            on_conflict="title",      # title이 같으면 충돌로 처리
            ignore_duplicates=True    # 충돌 시 에러 없이 무시
        ).execute()
        
        return len(data_list)
    except Exception as e:
        print(f"Supabase Client Error: {type(e).__name__}")
        return 0



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
        response = requests.get(API_URL, headers=headers, params=params)
        response.raise_for_status()
        return response.json().get('items', [])
    except Exception as e:
        print(f"Error fetching {stock_name}: {e}")
        return []

def filter_news(stock_name, items, exclude_words):
    valid_items = []
    
    from datetime import timezone, timedelta
    KST = timezone(timedelta(hours=9))
    now = datetime.now(KST)

    cutoff_time = now - timedelta(hours=12)
    
    for item in items:
        title = clean_html(item['title'])
        link = item['link']
        
        # [NEW] Non-News Domain Filter
        # Exclude sports, entertainment, Naver Post(UGC), and Naver TV(Video)
        blocked_domains = [
            'sports.news.naver.com', 'm.sports.naver.com',
            'entertain.naver.com', 'm.entertain.naver.com',
            'post.naver.com', 'tv.naver.com'
        ]
        if any(domain in link for domain in blocked_domains):
            continue
        
        # 0. [ ] 태그 필터링
        brackets = re.findall(r'\[(.*?)\]', title)
        skip = False
        allowed_tags = ["단독", "속보", "특징주", "공시"]
        for tag in brackets:
            if tag not in allowed_tags:
                skip = True
                break
        if skip:
            continue
        
        # 1. Check strict 12h window
        dt = parse_pub_date(item['pubDate'])
        if dt is None:
            continue
            
        if dt < cutoff_time:
            continue

        # 2. Check Excluded Words
        is_exact = False
        for word in exclude_words:
            if word in title:
                is_exact = True
                break
        if is_exact:
            continue

        # 3. Check Stock Name in Title
        if stock_name not in title:
            continue
            
        valid_items.append({
            'stock': stock_name,
            'title': title,
            'link': link,
            'pub_date': dt.strftime("%Y-%m-%d"),
            'pub_time': dt.strftime("%H:%M:%S")
        })
        
    return valid_items

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
                if ratio >= SIMILARITY_THRESHOLD
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

async def generate_ai_summary(news_text_list, historical_data_text):
    """Generate summary using Gemini (New SDK) with historical data."""
    if not GEMINI_API_KEY:
        return "Gemini API Key missing."
    
    if not news_text_list:
        return "No news to summarize."

    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        
        prompt = f"""
# Role (역할)
당신은 '주식 테마 매칭 전문 AI'입니다. 과거 급등 사례(Low-Data)와 금일 뉴스(Input)를 비교하여, 유사한 상승 재료를 포착하는 것이 임무입니다.


# Context (맥락)
나는 두 가지 텍스트 데이터를 제공합니다.
1. [특징주.csv]: 과거 급등 종목의 '상승 이유'가 적힌 데이터입니다. (CSV 형식: 종목명, 최대 등락률, 최소 등락률, 이유)
2. [Today]: 오늘 발생한 뉴스 헤드라인 목록입니다.

# Task
[Today]를 분석하여, [특징주.csv]에 있는 **상승 논리와 부합하는 뉴스**를 찾아내십시오.

# Analysis Logic (분석 로직)
1. **패턴 매칭**: [Today]에서 [특징주.csv]의 '이유'에 포함된 종목명을 제외한 키워드나 유사한 맥락이 발견되면 포착하십시오.
2. **이유 기반 추론**: 과거의 상승 논리가 오늘 뉴스에도 적용 가능한지 판단하십시오. (종목명과 상관없이 상승 이유에 집중해주세요.)
3. **주의: 문맥 확인 (Context Check)**:
   - 키워드가 맞지 않거나 맥락이 유사하지 않음에도 억지로 끼워 맞추지 마십시오. 논리적 타당성이 있어야 합니다.
   - 유사한 맥락에 맞게 포착됐는지 다시 확인하세요. 
   - 특징주.csv(과거 데이터)에 해당하는 이유가 없으면 "금일 주식에 영향을 미치는 종목 뉴스는 없습니다!" 라고 작성

# Input Data
[특징주.csv]
{historical_data_text}

[Today]
{news_text_list}

# Output Format (출력 형식 - 중요)
결과는 메일로 발송할 것이므로, 아래 양식에 맞춰 가독성 좋은 '보고서 형태'로 작성해 주세요. 서론이나 코드는 제외하고 본문만 출력하세요. 


## 📢 오늘의 종목 분석 (종목 개수에 제한 받지 말고 [특징주.csv]에 있는 '이유'와 부합하거나 유사한 모든 뉴스와 종목을 가져오세요)

### 1. [종목명] (예상 테마: 000, 관련 종목 :  000,000 ([종목명]이외, 7개 이내로 작성, 없으면 빈칸))
**뉴스** : **"오늘 뉴스 제목 인용"  (뉴스 발행 시간 인용)**
과거 상승 이유 : "과거 종목의 상승 이유 인용"
예상 파급력 : **높음**/**중간**/**낮음** 
▶ (search를 통해 오늘 뉴스가 어떨 지 간략하게 요약 설명 (서술식으로 표현하지 말 것))

(포착 항목 동일 양식 반복)

## 💡 요약 및 투자 포인트

전체적인 시장 분위기와 오늘 포착된 종목들의 공통적인 테마 흐름을 단답형으로 간략하게 3줄 요약(# 1. # 2. #3. 순서로 작성)(마크 다운 형식 넣지 말 것)

"""
        models_to_try = [
            "gemini-3-flash-preview", 
            "gemini-3-flash-preview", 
            "gemini-2.5-flash"
        ]
        
        for attempt, model_name in enumerate(models_to_try):
            try:
                # Run synchronous call in a thread to allow asyncio to yield
                response = await asyncio.to_thread(
                    client.models.generate_content,
                    model=model_name, 
                    contents=prompt,
                    config=genai.types.GenerateContentConfig(temperature=0.0)
                )
                return response.text
                
            except Exception as e:
                if attempt == len(models_to_try) - 1:
                    return f"AI Summary Failed after trying all models: {e}"
                
                print(f"[알림] {model_name} 서버 지연 등 오류 발생. 10초 대기 후 다음 시도로 넘어갑니다... ({e})")
                await asyncio.sleep(10)
                
    except Exception as e:
        return f"AI Summary Failed: {e}"

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



async def send_telegram_message(bot, chat_id, message):
    """Send message to Telegram, splitting if too long."""
    MAX_LENGTH = 4000
    
    try:
        if len(message) <= MAX_LENGTH:
            await bot.send_message(chat_id=chat_id, text=message, parse_mode='HTML') # Use HTML for stability
        else:
            # Simple split
            parts = [message[i:i+MAX_LENGTH] for i in range(0, len(message), MAX_LENGTH)]
            for part in parts:
                await bot.send_message(chat_id=chat_id, text=part)
    except Exception as e:
        print(f"Telegram Error: {e}")

async def main_async():
    # 0. Check Keys
    if not (NAVER_CLIENT_ID and NAVER_CLIENT_SECRET and TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        print("Missing API Keys in .env")
        return

    print("--- Starting News Crawler ---")
    
    # 2. Load Data
    stock_names = load_json(STOCK_NAMES_FILE)
    exclude_words = load_json(EXCLUDE_WORDS_FILE)
    
    # [NEW] Load Keywords for Highlighting
    KEYWORD_FILE = os.path.join(DATA_DIR, '종목명_keyword.json')
    keywords = load_json(KEYWORD_FILE)
    
    print(f"Stocks: {len(stock_names)}, Excluded Words: {len(exclude_words)}, Keywords: {len(keywords)}")
    
    all_valid_news = []
    
    # 3-1. Prepare for Overlap Check (Longest Match Exclusion)
    # Sort stocks by length DESC to check longer names first (though mainly needed for filtering logic below)
    # We need a reference list of ALL stock names to check against.
    all_stock_names_set = set(stock_names)

    # 3. Crawl
    for i, stock in enumerate(stock_names):
        print(f"[{i+1} of {len(stock_names)}] Fetching {stock}...")
        raw_items = fetch_news(stock)
        
        # [NEW] Enhanced Filtering for Overlapping Names (e.g. BGF vs BGFretail)
        # Logic: If news title contains a 'longer stock name' that includes current 'stock', ignore it.
        # Example: stock='BGF', title='BGF리테일 실적...', longer_stock='BGF리테일' -> Skip
        
        valid_items = []
        # Pre-calculate longer stocks that contain current stock
        longer_partners = [s for s in all_stock_names_set if stock in s and len(s) > len(stock)]
        
        temp_valid = filter_news(stock, raw_items, exclude_words)
        
        for item in temp_valid:
            title = item['title']
            is_noise = False
            for partner in longer_partners:
                if partner in title:
                    # Found a longer stock name in title -> Likely news about that specific longer stock, not our short keyword
                    is_noise = True
                    break
            
            if not is_noise:
                valid_items.append(item)
        
        if valid_items:
            # Cluster BEFORE saving
            clustered_items = cluster_similar_items(valid_items)
            print(f"  > Found {len(valid_items)} items -> {len(clustered_items)} clusters.")
            
            save_to_history(clustered_items) # Save reps to Supabase History
            all_valid_news.extend(clustered_items)
            
        time.sleep(0.1) # Rate limit
        
    if not all_valid_news:
        print("No valid news found today.")
        # async with Bot(token=TELEGRAM_TOKEN) as bot:
        #     await send_telegram_message(bot, TELEGRAM_CHAT_ID, "금일 수집된 중요 뉴스가 없습니다.")
        return

    # ... (Step 4, 5 same as before) ...
    # 4. Prepare Report (Already Clustered)
    print("Preparing report...")
    news_report_body, summary_input_str = format_news_report(all_valid_news, keywords)
    
    # 5. Generate AI Summary with Historical Data
    print("Generating AI Summary...")
    
    HISTORICAL_CSV_PATH = os.path.join(BASE_DIR, '..', 'data', '특징주.csv')
    historical_text = "과거 데이터 없음"
    
    if os.path.exists(HISTORICAL_CSV_PATH):
        try:
            with open(HISTORICAL_CSV_PATH, 'r', encoding='utf-8-sig') as f:
                historical_text = f.read()
            print(f"Loaded historical data ({len(historical_text)} chars).")
        except Exception as e:
            print(f"Failed to load historical data: {e}")
            
    ai_summary = await generate_ai_summary(summary_input_str, historical_text)
    # save_summary(ai_summary) # Removed
    
    # 6. Send Telegram
    # [NEW] Refined Telegram Message Logic (Clean & Bold)
    from datetime import timezone
    KST = timezone(timedelta(hours=9))
    now = datetime.now(KST)
    am_pm = "오전" if now.hour < 12 else "오후"
    formatted_date = now.strftime('%y.%m.%d')
    report_title = f"{formatted_date} {am_pm} 종목 뉴스 브리핑"

    tg_lines = []
    tg_lines.append(f"📊 *{report_title}*")
    tg_lines.append(f"{now.strftime('%Y-%m-%d %H:%M')}\n")
    
    source_lines = ai_summary.split('\n')
    for line in source_lines:
        line = line.strip()
        if not line:
             tg_lines.append("") # Keep empty lines for spacing
             continue
        
        # [수정된 로직] Allow-List 방식 (허용된 패턴만 통과)
        # 사족(설명)을 완벽하게 제거하기 위함.
        
        # 1. 헤더/종목명 (### 1. ...)
        if "### 1." in line or "### " in line:
             clean_line = line.replace("### ", "").replace("**", "")
             clean_line = clean_line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             tg_lines.append(f"<b>{clean_line}</b>")
             
        # 2. 뉴스 제목 (* 뉴스: ...)
        elif "뉴스" in line and ":" in line:
             # "과거 뉴스"는 제외해야 함
             if "과거 뉴스" in line:
                 continue
                 
             clean_line = line.replace("**", "")
             clean_line = clean_line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             tg_lines.append(clean_line)
             
        # 3. 그 외 설명문(사족) -> 과감히 삭제 (continue)
        else:
             continue

    tg_message = "\n".join(tg_lines)
    
    print("Sending Telegram message...")
    async with Bot(token=TELEGRAM_TOKEN) as bot:
         # Use HTML parse mode for better stability
        await send_telegram_message(bot, TELEGRAM_CHAT_ID, tg_message)
    print("Done!")

    # 7. Send Notion (Blocks API)
    print("Sending Notion...")
    await send_notion_message(report_title, ai_summary, all_valid_news, keywords)
    print("Done!")

    # 8. Send Gmail (Simple HTML)
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

def simple_markdown_to_notion_blocks(ai_summary, all_valid_news, keywords):
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
                # ▼ 글자 크기 조절 원하실 경우: "heading_1"(가장 큼), "heading_2"(큼), "heading_3"(중간), "paragraph"(일반 본문) 중 하나로 "type"과 키를 변경하세요.
                "object": "block", "type": "heading_1",
                "heading_1": {"rich_text": [{"type": "text", "text": {"content": content}}]}
            })
            
        elif line.startswith('### '):
            content = line.replace('### ', '').replace('**', '')
            parts = content.split('(', 1)
            title_part = parts[0].strip()
            meta_part = f"({parts[1]}" if len(parts) > 1 else ""
            
            # 1. 종목명 (메일 원본처럼 굵고 큰 제목 유지)
            # ▼ 글자 크기 조절: "heading_3"를 "heading_2" 로 바꾸면 더 커집니다.
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
                 
    # 2. Append News List
    blocks.append({"object": "block", "type": "divider", "divider": {}})
    blocks.append({
        "object": "block", "type": "heading_2",
        "heading_2": {"rich_text": [{"type": "text", "text": {"content": "📰 수집된 전체 뉴스 목록"}}]}
    })
    
    stock_map = {}
    for item in all_valid_news:
        s = item['stock']
        if s not in stock_map:
            stock_map[s] = []
        stock_map[s].append(item)
        
    for stock, items in stock_map.items():
        if not items: continue
        
        # [NEW 필터링 로직] 노션에는 중요 뉴스(형광펜 대상)만 선별해서 보냅니다.
        important_items = []
        for i in items:
            title_without_stock = i['title'].replace(stock, "")
            if any(kw in title_without_stock for kw in keywords):
                important_items.append(i)
                
        # 중요 뉴스가 1개도 없는 종목이면 노션 전송 목록에서 통째로 제외합니다.
        if not important_items:
            continue
            
        important_items.sort(key=lambda x: (x.get('pub_date', ''), x.get('pub_time', '')), reverse=True)
        
        blocks.append({
            "object": "block", "type": "heading_3",
            "heading_3": {"rich_text": [{"type": "text", "text": {"content": f"[{stock}]"}}]}
        })
        
        # 일반 뉴스는 버리고 중요 뉴스(important_items)만 노션 블록으로 생성합니다.
        for item in important_items:
            title = item['title']
            date_str = item['pub_date'][5:] 
            time_str = item['pub_time'][:5]
            
            # important_items 배열에 들어왔다는 것 자체가 이미 하이라이트 조건을 만족한 뉴스입니다.
            color = "yellow_background"
            
            blocks.append({
                "object": "block", "type": "bulleted_list_item",
                "bulleted_list_item": {
                    "rich_text": [
                        {"type": "text", "text": {"content": f"{title} "}},
                        {"type": "text", "text": {"content": f"({date_str} {time_str})"}, "annotations": {"color": "gray"}}
                    ],
                    "color": color
                }
            })
            
    return blocks

async def send_notion_message(report_title, ai_summary, all_valid_news, keywords):
    import urllib.request
    NOTION_API_KEY = os.getenv("notion")
    NOTION_DATABASE_ID = os.getenv("NOTION_DATABASE_ID")
    
    HEADERS = {
        'Authorization': f'Bearer {NOTION_API_KEY}',
        'Notion-Version': '2022-06-28',
        'Content-Type': 'application/json'
    }

    blocks = simple_markdown_to_notion_blocks(ai_summary, all_valid_news, keywords)
    
    from datetime import timezone, timedelta
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
        try:
            with urllib.request.urlopen(req) as response:
                response_body = response.read().decode('utf-8')
                result_json = json.loads(response_body)
                page_id = result_json.get('id')
                print(f"Notion 첫 페이지 생성 성공 (Block 1~{len(first_chunk)}).")
                
                # 2. Add remaining blocks iteratively (Chunking)
                import time
                if page_id and remaining_blocks:
                    for i in range(0, len(remaining_blocks), 100):
                        time.sleep(0.5) # Rate limit safety (초당 3회 허용 / 여기서는 0.5초 대기로 안전망 확보)
                        chunk = remaining_blocks[i : i + 100]
                        patch_data = {"children": chunk}
                        patch_req = urllib.request.Request(
                            f'https://api.notion.com/v1/blocks/{page_id}/children',
                            data=json.dumps(patch_data).encode('utf-8'),
                            headers=HEADERS,
                            method='PATCH'
                        )
                        with urllib.request.urlopen(patch_req) as patch_res:
                            print(f"[알림] Notion 블럭 전송 {100 + i + 1} ~ {100 + i + len(chunk)} 추가 성공.")
                            
                print("Notion 전송을 모두 완료했습니다.")
                
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
        import time
        from googleapiclient.errors import HttpError
        
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
