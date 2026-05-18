#!/usr/bin/env python3
"""
채용 공고 회사 이메일 수집기 (사람인 + 잡코리아 + 원티드)
- 사람인/잡코리아/원티드에서 키워드별 최근 공고 검색
- 공고 ID 기준 중복 체크, 같은 회사 30일 쿨다운
- 회사 공식 사이트에서 공개 이메일 수집
- CSV 출력 (이메일 있는 회사만)
"""

import argparse
import requests
from bs4 import BeautifulSoup
import sqlite3
import csv
import re
import time
import os
from datetime import datetime, timedelta
from urllib.parse import urlparse

# ── 설정 ──
KEYWORDS = [
    "경영지원", "경리", "총무", "비서",
    "과제비 관리", "정부지원사업 관리", "연구비 관리", "인사"
]

# 같은 회사가 CSV에 다시 포함되려면 최소 이 기간이 지나야 함
COMPANY_COOLDOWN_DAYS = 30

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scraper_history.db")
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "ko-KR,ko;q=0.9",
}

EMAIL_PATTERN = re.compile(
    r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}"
)

# ── 쓰레기 이메일 필터 ──
IGNORE_EMAIL_DOMAINS = {
    "example.com", "example.org", "test.com", "email.com",
    "sentry.io", "wixpress.com", "sentry-next.wixpress.com", "w3.org",
    "yourdomain.com", "yourwebsite.com", "yoursite.com",
    "domain.com", "company.com", "your-domain.com", "website.com",
}

IGNORE_EMAILS_EXACT = {
    "hosting@gabia.com", "mail@example.org",
    "info@yourdomain.com", "info@yourwebsite.com",
    "admin@example.com", "user@example.com",
    "you@website.com", "mytory@gmail.com",
    "highlight@sedaily.com", "greenremodeling@kalis.or.kr",
    "enquiry@jejudreamtower.com",
}

IGNORE_EMAIL_PREFIXES = [
    "noreply@", "no-reply@", "no_reply@",
    "postmaster@", "mailer-daemon@",
    "hosting@gabia", "webmaster@gabia",
]

IGNORE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".css", ".js"}


# ═══════════════════════════════════════
# 유틸리티
# ═══════════════════════════════════════

def is_valid_email(email):
    email = email.lower().strip()
    if not EMAIL_PATTERN.match(email):
        return False
    domain = email.split("@")[1]
    if domain in IGNORE_EMAIL_DOMAINS:
        return False
    if email in IGNORE_EMAILS_EXACT:
        return False
    for prefix in IGNORE_EMAIL_PREFIXES:
        if email.startswith(prefix):
            return False
    for ext in IGNORE_EXTENSIONS:
        if email.endswith(ext):
            return False
    local = email.split("@")[0]
    if len(local) > 30 and re.match(r"^[a-f0-9]+$", local):
        return False
    return True


def _fetch_page(url, timeout=8):
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        if resp.status_code != 200:
            return None
        return resp.text
    except (requests.RequestException, UnicodeError):
        return None


def _extract_emails_from_html(html):
    emails = set()
    for email in EMAIL_PATTERN.findall(html):
        if is_valid_email(email):
            emails.add(email.lower())
    soup = BeautifulSoup(html, "lxml")
    for a in soup.select("a[href^='mailto:']"):
        mailto = a["href"].replace("mailto:", "").split("?")[0].strip()
        if EMAIL_PATTERN.match(mailto) and is_valid_email(mailto):
            emails.add(mailto.lower())
    return emails


def _find_contact_links(html, base_url):
    soup = BeautifulSoup(html, "lxml")
    contact_keywords = [
        "contact", "문의", "연락", "오시는", "회사소개", "about",
        "support", "고객센터", "고객지원", "채용", "recruit", "career",
        "인재채용", "footer", "sitemap",
    ]
    found_urls = set()
    for a in soup.select("a[href]"):
        href = a.get("href", "").strip()
        text = a.get_text(strip=True).lower()
        href_lower = href.lower()
        if href.startswith("mailto:") or href.startswith("tel:"):
            continue
        is_contact = any(kw in text or kw in href_lower for kw in contact_keywords)
        if not is_contact:
            continue
        if href.startswith("http"):
            parsed = urlparse(href)
            base_parsed = urlparse(base_url)
            if parsed.netloc != base_parsed.netloc:
                continue
            full_url = href
        elif href.startswith("/"):
            full_url = base_url.rstrip("/") + href
        elif href.startswith("#"):
            continue
        else:
            full_url = base_url.rstrip("/") + "/" + href
        found_urls.add(full_url)
    return found_urls


# ═══════════════════════════════════════
# CSV 기반 중복 체크 (Git 공유용)
# ═══════════════════════════════════════

def load_history_from_csvs():
    """output/ 폴더의 기존 CSV에서 수집 이력 로드.
    Returns: (seen_companies: dict[name -> date_str], seen_posting_ids: set)
    """
    seen_companies = {}   # company_name -> 가장 최근 수집일 (파일명 기반)
    seen_posting_ids = set()

    if not os.path.exists(OUTPUT_DIR):
        return seen_companies, seen_posting_ids

    for fname in sorted(os.listdir(OUTPUT_DIR)):
        if not fname.endswith(".csv"):
            continue

        # 파일명에서 날짜 추출: recruit_20260430_1320.csv → 2026-04-30
        date_str = ""
        match = re.search(r"(\d{8})_\d{4}", fname)
        if match:
            d = match.group(1)
            date_str = f"{d[:4]}-{d[4:6]}-{d[6:8]}"

        filepath = os.path.join(OUTPUT_DIR, fname)
        try:
            with open(filepath, "r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    name = row.get("기업명", "").strip()
                    if name:
                        seen_companies[name] = date_str
        except (csv.Error, KeyError, UnicodeDecodeError):
            continue

    return seen_companies, seen_posting_ids


def load_emails_from_csvs():
    """output/ 폴더의 기존 CSV에서 이메일 → 회사명 매핑 로드 (중복 감지용)"""
    email_map = {}  # email -> company_name
    if not os.path.exists(OUTPUT_DIR):
        return email_map
    for fname in sorted(os.listdir(OUTPUT_DIR)):
        if not fname.endswith(".csv"):
            continue
        filepath = os.path.join(OUTPUT_DIR, fname)
        try:
            with open(filepath, "r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    email = row.get("이메일", "").strip().lower()
                    name = row.get("기업명", "").strip()
                    if email and name:
                        email_map[email] = name
        except (csv.Error, KeyError, UnicodeDecodeError):
            continue
    return email_map


def is_company_in_cooldown_csv(seen_companies, company_name):
    """CSV 이력 기반 회사 쿨다운 체크"""
    date_str = seen_companies.get(company_name)
    if not date_str:
        return False
    try:
        last_at = datetime.strptime(date_str, "%Y-%m-%d")
        return (datetime.now() - last_at).days < COMPANY_COOLDOWN_DAYS
    except (ValueError, TypeError):
        return False


# ═══════════════════════════════════════
# DB (로컬 캐시 — 없어도 CSV로 동작)
# ═══════════════════════════════════════

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # 공고 테이블 (공고 ID 기준 중복 체크)
    c.execute("""
        CREATE TABLE IF NOT EXISTS postings (
            posting_id TEXT PRIMARY KEY,
            source TEXT,
            company_name TEXT,
            keyword TEXT,
            collected_at TEXT
        )
    """)

    # 회사 테이블 (회사별 쿨다운 관리 + 정보 저장)
    c.execute("""
        CREATE TABLE IF NOT EXISTS companies (
            company_name TEXT PRIMARY KEY,
            ceo_name TEXT,
            keywords TEXT,
            email TEXT,
            website TEXT,
            last_csv_at TEXT
        )
    """)

    conn.commit()
    return conn


def is_posting_seen(conn, posting_id):
    c = conn.cursor()
    c.execute("SELECT 1 FROM postings WHERE posting_id = ?", (posting_id,))
    return c.fetchone() is not None


def save_posting(conn, posting_id, source, company_name, keyword):
    c = conn.cursor()
    c.execute("""
        INSERT OR IGNORE INTO postings (posting_id, source, company_name, keyword, collected_at)
        VALUES (?, ?, ?, ?, ?)
    """, (posting_id, source, company_name, keyword, datetime.now().isoformat()))
    conn.commit()


def is_company_in_cooldown(conn, company_name):
    c = conn.cursor()
    c.execute("SELECT last_csv_at FROM companies WHERE company_name = ?", (company_name,))
    row = c.fetchone()
    if not row or not row[0]:
        return False
    try:
        last_at = datetime.fromisoformat(row[0])
        return (datetime.now() - last_at).days < COMPANY_COOLDOWN_DAYS
    except (ValueError, TypeError):
        return False


def save_company(conn, company, included_in_csv=False):
    c = conn.cursor()
    last_csv_at = datetime.now().isoformat() if included_in_csv else None

    # 기존 데이터 확인
    c.execute("SELECT last_csv_at FROM companies WHERE company_name = ?", (company["company_name"],))
    existing = c.fetchone()

    if existing and not included_in_csv:
        # CSV에 안 넣었으면 last_csv_at 업데이트 안 함
        c.execute("""
            INSERT OR REPLACE INTO companies
            (company_name, ceo_name, keywords, email, website, last_csv_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            company["company_name"],
            company.get("ceo_name", ""),
            company.get("keywords", ""),
            company.get("email", ""),
            company.get("website", ""),
            existing[0],  # 기존 last_csv_at 유지
        ))
    else:
        c.execute("""
            INSERT OR REPLACE INTO companies
            (company_name, ceo_name, keywords, email, website, last_csv_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            company["company_name"],
            company.get("ceo_name", ""),
            company.get("keywords", ""),
            company.get("email", ""),
            company.get("website", ""),
            last_csv_at,
        ))
    conn.commit()


# ═══════════════════════════════════════
# 사람인 검색
# ═══════════════════════════════════════

def search_saramin(keyword, page=1):
    url = "https://www.saramin.co.kr/zf_user/search/recruit"
    params = {
        "searchType": "search",
        "searchword": keyword,
        "recruitPage": page,
        "recruitSort": "relation",
        "recruitPageCount": "40",
        "inner_com_type": "",
        "company_cd": "0,1,2,3,4,5,6,7,8,9",
        "show_applied": "n",
        "quick_apply": "",
        "except_read": "",
        "ai_head_498": "",
        "searchPeriod": "7",
    }

    # 최대 2회 재시도 (사람인 일시 차단 대응)
    resp = None
    for attempt in range(3):
        try:
            resp = requests.get(url, params=params, headers=HEADERS, timeout=15)
            resp.raise_for_status()
            break
        except requests.RequestException as e:
            if attempt < 2:
                time.sleep(3 * (attempt + 1))
            else:
                print(f"  [오류] 사람인 '{keyword}' 검색 실패: {e}")
                return []

    soup = BeautifulSoup(resp.text, "lxml")
    results = []

    job_items = soup.select(".item_recruit")
    if not job_items:
        job_items = soup.select("[class*='recruit']")

    for item in job_items:
        corp_tag = item.select_one(".corp_name a, .company_nm a, .corp_name, .company_nm")
        if not corp_tag:
            continue
        company_name = corp_tag.get_text(strip=True)
        if not company_name:
            continue

        title_tag = item.select_one(".job_tit a, .recruit_title a, .notification_info a")
        job_title = title_tag.get_text(strip=True) if title_tag else ""

        # 공고 ID 추출 (URL에서)
        posting_id = ""
        if title_tag and title_tag.get("href"):
            match = re.search(r"rec_idx=(\d+)", title_tag["href"])
            if match:
                posting_id = f"saramin_{match.group(1)}"

        # 회사 상세 링크
        corp_link = ""
        link_tag = corp_tag if corp_tag.name == "a" else corp_tag.find("a")
        if link_tag and link_tag.get("href"):
            corp_link = link_tag["href"]
            if not corp_link.startswith("http"):
                corp_link = "https://www.saramin.co.kr" + corp_link

        results.append({
            "posting_id": posting_id,
            "source": "saramin",
            "company_name": company_name,
            "job_title": job_title,
            "keyword": keyword,
            "corp_link": corp_link,
        })

    return results


def get_company_info_from_saramin(corp_link):
    info = {"ceo_name": "", "website": ""}
    if not corp_link:
        return info

    try:
        resp = requests.get(corp_link, headers=HEADERS, timeout=10)
        resp.raise_for_status()
    except requests.RequestException:
        return info

    soup = BeautifulSoup(resp.text, "lxml")

    for dt in soup.select("dt"):
        text = dt.get_text(strip=True)
        if "대표자" in text or "대표이사" in text:
            dd = dt.find_next_sibling("dd")
            if dd:
                info["ceo_name"] = dd.get_text(strip=True)
            break

    if not info["ceo_name"]:
        for th in soup.select("th"):
            text = th.get_text(strip=True)
            if "대표" in text:
                td = th.find_next_sibling("td")
                if td:
                    info["ceo_name"] = td.get_text(strip=True)
                break

    for a in soup.select("a[href]"):
        href = a.get("href", "")
        text = a.get_text(strip=True)
        if ("홈페이지" in text or "homepage" in text.lower()) and href.startswith("http"):
            info["website"] = href
            break

    if not info["website"]:
        for tag in soup.select("[class*='homepage'], [class*='url'], [class*='site']"):
            link = tag.get("href") or tag.get("data-href", "")
            if link.startswith("http"):
                info["website"] = link
                break

    if not info["website"]:
        for dt in soup.select("dt"):
            text = dt.get_text(strip=True)
            if "홈페이지" in text or "URL" in text:
                dd = dt.find_next_sibling("dd")
                if dd:
                    link_tag = dd.find("a")
                    if link_tag and link_tag.get("href", "").startswith("http"):
                        info["website"] = link_tag["href"]
                break

    return info


# ═══════════════════════════════════════
# 잡코리아 검색
# ═══════════════════════════════════════

def search_jobkorea(keyword, page=1):
    url = "https://www.jobkorea.co.kr/Search/"
    params = {
        "stext": keyword,
        "tabType": "recruit",
        "Page_No": page,
    }

    try:
        resp = requests.get(url, params=params, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"  [오류] 잡코리아 '{keyword}' 검색 실패: {e}")
        return []

    soup = BeautifulSoup(resp.text, "lxml")
    results = []

    # 공고 링크에서 ID, 제목, 회사명 추출
    links = soup.select("a[href*='/Recruit/GI_Read/']")
    seen_ids = set()

    for a in links:
        href = a.get("href", "")
        match = re.search(r"/GI_Read/(\d+)", href)
        if not match:
            continue
        gid = match.group(1)
        if gid in seen_ids:
            continue

        text = a.get_text(strip=True)
        if not text:
            continue

        # 같은 공고 ID를 가진 다른 링크에서 회사명/제목 찾기
        parent = a.parent
        while parent and parent.name != "body":
            sibling_links = parent.select(f"a[href*='/GI_Read/{gid}']")
            if len(sibling_links) >= 2:
                texts = [l.get_text(strip=True) for l in sibling_links if l.get_text(strip=True)]
                if len(texts) >= 2:
                    job_title = texts[0]
                    company_name = texts[1]
                    seen_ids.add(gid)
                    results.append({
                        "posting_id": f"jobkorea_{gid}",
                        "source": "jobkorea",
                        "company_name": company_name,
                        "job_title": job_title,
                        "keyword": keyword,
                        "corp_link": "",  # 잡코리아는 사람인으로 기업 정보 대체
                    })
                break
            parent = parent.parent

    return results


def search_google_for_website(company_name):
    """구글 검색으로 회사 공식 홈페이지 찾기"""
    query = f"{company_name} 공식 홈페이지"
    url = "https://www.google.com/search"
    params = {"q": query, "hl": "ko", "num": 5}

    try:
        resp = requests.get(url, params=params, headers=HEADERS, timeout=10)
        resp.raise_for_status()
    except requests.RequestException:
        return ""

    soup = BeautifulSoup(resp.text, "lxml")

    # 구글 검색 결과에서 링크 추출
    skip_domains = {
        "google.com", "google.co.kr", "youtube.com",
        "saramin.co.kr", "jobkorea.co.kr", "wanted.co.kr",
        "incruit.com", "alba.co.kr", "catch.co.kr",
        "naver.com", "daum.net", "kakao.com",
        "facebook.com", "instagram.com", "twitter.com", "x.com",
        "linkedin.com", "blog.naver.com", "tistory.com",
        "wikipedia.org", "namu.wiki", "namuwiki.kr",
        "jobplanet.co.kr", "glassdoor.com",
        "thevc.kr", "rocketpunch.com",
        # 정보/구인/기업DB 사이트 (엉뚱한 매칭 방지)
        "albamon.com", "nicebizinfo.com", "moneypin.biz", "bizno.net",
        "career.rememberapp.co.kr", "blog.kakaocdn.net",
        "allthatcompany.com", "hiseoul.sba.kr", "teamblind.com",
        "demoday.co.kr", "comp.wisereport.co.kr",
        "greetinghr.com", "ninehire.site",
        "tiktok.com", "threads.net",
    }

    for a in soup.select("a[href]"):
        href = a.get("href", "")
        # 구글 리다이렉트 URL 파싱
        if href.startswith("/url?"):
            match = re.search(r"q=(https?://[^&]+)", href)
            if match:
                href = match.group(1)
        if not href.startswith("http"):
            continue

        parsed = urlparse(href)
        domain = parsed.netloc.lower().replace("www.", "")

        if any(skip in domain for skip in skip_domains):
            continue

        # 회사 공식 사이트일 가능성이 높은 URL 반환
        return f"{parsed.scheme}://{parsed.netloc}"

    return ""


def search_naver_for_website(company_name):
    """네이버 검색으로 회사 공식 홈페이지 찾기 (구글 실패 시 폴백)"""
    query = f"{company_name} 공식 홈페이지"
    url = "https://search.naver.com/search.naver"
    params = {"query": query}

    try:
        resp = requests.get(url, params=params, headers=HEADERS, timeout=10)
        resp.raise_for_status()
    except requests.RequestException:
        return ""

    soup = BeautifulSoup(resp.text, "lxml")

    skip_domains = {
        "naver.com", "naver.me", "navercorp.com",
        "daum.net", "kakao.com", "google.com",
        "youtube.com", "facebook.com", "instagram.com",
        "twitter.com", "x.com", "linkedin.com", "tistory.com",
        "wikipedia.org", "namu.wiki",
        "saramin.co.kr", "jobkorea.co.kr", "wanted.co.kr",
        "jobplanet.co.kr", "glassdoor.com",
        "thevc.kr", "rocketpunch.com",
        "albamon.com", "nicebizinfo.com", "moneypin.biz", "bizno.net",
        "career.rememberapp.co.kr", "blog.kakaocdn.net",
        "allthatcompany.com", "hiseoul.sba.kr", "teamblind.com",
        "demoday.co.kr", "comp.wisereport.co.kr",
        "greetinghr.com", "ninehire.site",
        "tiktok.com", "threads.net",
    }

    for a in soup.select("a[href]"):
        href = a.get("href", "")
        if not href.startswith("http"):
            continue
        parsed = urlparse(href)
        domain = parsed.netloc.lower().replace("www.", "")
        if any(skip in domain for skip in skip_domains):
            continue
        return f"{parsed.scheme}://{parsed.netloc}"

    return ""


def find_company_website(company_name):
    """구글 → 네이버 순서로 회사 공식 홈페이지 검색"""
    website = search_google_for_website(company_name)
    if website:
        return website
    time.sleep(1)
    return search_naver_for_website(company_name)


def get_ceo_from_website(html):
    """홈페이지 HTML에서 대표자명 추출 시도"""
    if not html:
        return ""

    # 쓰레기 단어 필터 (대표 뒤에 올 수 없는 단어들)
    garbage_words = {
        "이사", "전화", "번호", "인사", "인사말", "브랜드", "서비스",
        "기업", "회사", "사이트", "홈페이지", "이미지", "카테고리",
        "아르바이", "스타트업", "솔루션", "미디어", "인터뷰",
        "제품", "상품", "매장", "지점", "사업", "프로젝트",
        "선임", "주관", "계약", "화면", "원장", "커피",
        "차량", "후보", "연구", "제약", "강소", "자명",
        "전화번호", "이미지설", "미디어사", "커피전시", "에게바란",
        "브랜드입", "서비스입", "주관계약", "제약사로", "차량선택",
        "전력회사", "이미지주", "어린이수", "아르바이트",
        "님까지", "에게", "하는", "적이다", "적인", "신간",
        "번호를", "성함", "회의", "봉새롬",
    }

    # "대표이사 : 홍길동" 또는 "대표자 : 홍길동" 같은 정형화된 패턴
    # 반드시 콜론/구분자가 있어야 매칭 (느슨한 매칭 방지)
    patterns = [
        r"대표(?:이사|자)\s*[:：]\s*([가-힣]{2,4})",
        r"CEO\s*[:：]\s*([가-힣]{2,4})",
        r"대표(?:이사|자)\s*</(?:dt|th|td|span|div|strong)>\s*<(?:dd|td|span|div)[^>]*>\s*([가-힣]{2,4})",
    ]
    for pat in patterns:
        match = re.search(pat, html)
        if match:
            name = match.group(1).strip()
            if name not in garbage_words and len(name) >= 2:
                return name
    return ""


def get_company_info_by_search(company_name):
    """구글/네이버 검색으로 회사 홈페이지를 찾고 대표자명 추출"""
    info = {"ceo_name": "", "website": ""}

    website = find_company_website(company_name)
    if not website:
        return info

    info["website"] = website

    # 홈페이지에서 대표자명 추출 시도
    html = _fetch_page(website)
    if html:
        info["ceo_name"] = get_ceo_from_website(html)

        # 메인에 없으면 회사소개 페이지에서 시도
        if not info["ceo_name"]:
            for path in ["/about", "/about-us", "/company", "/회사소개", "/company/about"]:
                about_html = _fetch_page(website.rstrip("/") + path)
                if about_html:
                    ceo = get_ceo_from_website(about_html)
                    if ceo:
                        info["ceo_name"] = ceo
                        break
                    time.sleep(0.3)

    return info


def get_company_info_from_jobkorea(company_name):
    """구글/네이버 검색으로 회사 정보 수집"""
    return get_company_info_by_search(company_name)


# ═══════════════════════════════════════
# 원티드 검색
# ═══════════════════════════════════════

def search_wanted(keyword, page=1):
    """원티드 API를 사용하여 채용공고 검색"""
    url = "https://www.wanted.co.kr/api/v4/jobs"
    limit = 20
    offset = (page - 1) * limit
    params = {
        "query": keyword,
        "country": "kr",
        "job_sort": "job.latest_order",
        "limit": limit,
        "offset": offset,
        "locations": "all",
        "years": -1,
    }

    try:
        resp = requests.get(url, params=params, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        print(f"  [오류] 원티드 '{keyword}' 검색 실패: {e}")
        return []

    results = []
    for job in data.get("data", []):
        company = job.get("company", {})
        company_name = company.get("name", "").strip()
        if not company_name:
            continue

        job_id = job.get("id", "")
        position = job.get("position", "")

        results.append({
            "posting_id": f"wanted_{job_id}" if job_id else "",
            "source": "wanted",
            "company_name": company_name,
            "job_title": position,
            "keyword": keyword,
            "corp_link": "",  # 원티드는 사람인으로 기업 정보 대체
        })

    return results


def get_company_info_from_wanted(company_name):
    """구글/네이버 검색으로 회사 정보 수집"""
    return get_company_info_by_search(company_name)


# ═══════════════════════════════════════
# 이메일 수집
# ═══════════════════════════════════════

def scrape_email_from_website(url):
    if not url:
        return ""

    parsed = urlparse(url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    all_emails = set()
    visited = set()

    # 1차: 고정 경로 탐색
    paths_to_check = [
        "",
        "/contact", "/contact-us", "/contact_us",
        "/about", "/about-us", "/about_us",
        "/company", "/company/about",
        "/support", "/help",
        "/recruit", "/careers", "/career", "/jobs",
        "/inquiry", "/qna",
        "/문의", "/회사소개", "/채용", "/오시는길",
        "/customer", "/customer/inquiry",
        "/cs", "/cs/contact",
        "/footer",
    ]

    for path in paths_to_check:
        check_url = base_url + path
        if check_url in visited:
            continue
        visited.add(check_url)

        html = _fetch_page(check_url)
        if not html:
            continue

        emails = _extract_emails_from_html(html)
        all_emails.update(emails)

        if all_emails:
            break
        time.sleep(0.3)

    # 2차: 메인 페이지 내부 링크 탐색
    if not all_emails:
        main_html = _fetch_page(base_url)
        if main_html:
            all_emails.update(_extract_emails_from_html(main_html))

            if not all_emails:
                contact_links = _find_contact_links(main_html, base_url)
                for link in list(contact_links)[:5]:
                    if link in visited:
                        continue
                    visited.add(link)
                    html = _fetch_page(link)
                    if html:
                        emails = _extract_emails_from_html(html)
                        all_emails.update(emails)
                        if all_emails:
                            break
                    time.sleep(0.3)

    # 3차: footer 영역
    if not all_emails:
        main_html = _fetch_page(base_url)
        if main_html:
            soup = BeautifulSoup(main_html, "lxml")
            footer = soup.find("footer") or soup.select_one("[class*='footer']")
            if footer:
                for email in EMAIL_PATTERN.findall(str(footer)):
                    if is_valid_email(email):
                        all_emails.add(email.lower())

    # 우선순위 정렬
    if all_emails:
        priority = [
            ["recruit", "hr", "career", "채용", "인사", "hiring"],
            ["info", "contact", "support", "admin", "inquiry", "문의"],
            ["biz", "business", "sales", "office"],
        ]
        for tier in priority:
            for prefix in tier:
                for e in all_emails:
                    if e.startswith(prefix):
                        return e
        return sorted(all_emails)[0]

    return ""


# ═══════════════════════════════════════
# 메인
# ═══════════════════════════════════════

TARGET_EMAILS = 50  # 이메일 수집 목표 건수
MAX_PAGES = 10      # 키워드당 최대 검색 페이지


def collect_candidates(conn, seen_companies):
    """사람인 + 잡코리아에서 신규 회사 후보를 페이지네이션으로 수집.
    Generator로 한 회사씩 yield.
    """
    seen_names = set()  # 이번 실행에서 이미 yield한 회사명

    for page in range(1, MAX_PAGES + 1):
        has_results = False

        for site_name, search_fn in [("사람인", search_saramin), ("잡코리아", search_jobkorea), ("원티드", search_wanted)]:
            for kw in KEYWORDS:
                if page == 1:
                    print(f"    [{site_name}] '{kw}' p{page}...", end=" ", flush=True)
                else:
                    print(f"    [{site_name}] '{kw}' p{page}...", end=" ", flush=True)

                results = search_fn(kw, page=page)
                new_count = 0

                for r in results:
                    pid = r["posting_id"]

                    # 공고 ID 중복 체크 (로컬 DB)
                    if pid and is_posting_seen(conn, pid):
                        continue

                    # 공고 저장 (로컬 DB)
                    if pid:
                        save_posting(conn, pid, r["source"], r["company_name"], kw)

                    name = r["company_name"]

                    # 이미 이번 실행에서 처리한 회사
                    if name in seen_names:
                        continue

                    # 회사 쿨다운 체크
                    if is_company_in_cooldown_csv(seen_companies, name):
                        continue
                    if is_company_in_cooldown(conn, name):
                        continue

                    seen_names.add(name)
                    new_count += 1
                    has_results = True

                    yield {
                        "company_name": name,
                        "keywords": kw,
                        "corp_link": r.get("corp_link", ""),
                        "job_title": r["job_title"],
                        "source": r["source"],
                        "ceo_name": "",
                        "email": "",
                        "website": "",
                    }

                print(f"{len(results)}건, 신규 {new_count}건")
                time.sleep(2)

        if not has_results:
            print(f"\n  페이지 {page}에서 더 이상 신규 공고 없음. 검색 종료.")
            break


def process_company(data):
    """회사 정보 수집 + 이메일 수집을 한 번에 처리"""
    name = data["company_name"]

    # 회사 정보 수집: 사람인 corp_link가 있으면 먼저 시도
    info = {"ceo_name": "", "website": ""}
    if data["corp_link"]:
        info = get_company_info_from_saramin(data["corp_link"])

    # 사람인에서 website 못 찾았으면 구글/네이버 검색으로 폴백
    if not info["website"]:
        search_info = get_company_info_by_search(name)
        if search_info["website"]:
            info["website"] = search_info["website"]
        if not info["ceo_name"] and search_info["ceo_name"]:
            info["ceo_name"] = search_info["ceo_name"]

    data["ceo_name"] = info["ceo_name"]
    data["website"] = info["website"]

    # 이메일 수집
    if data["website"]:
        data["email"] = scrape_email_from_website(data["website"])

    return data


def main():
    parser = argparse.ArgumentParser(description="채용 공고 회사 이메일 수집기")
    parser.add_argument("-n", type=int, default=TARGET_EMAILS,
                        help=f"수집 목표 건수 (기본값: {TARGET_EMAILS})")
    args = parser.parse_args()
    target_emails = args.n

    print("=" * 60)
    print(" 채용 공고 회사 이메일 수집기 (사람인 + 잡코리아 + 원티드)")
    print(f" 실행 시각: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f" 목표: 이메일 {target_emails}건 수집")
    print(f" 회사 쿨다운: {COMPANY_COOLDOWN_DAYS}일")
    print("=" * 60)

    conn = init_db()
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # CSV 이력 로드
    print("\n기존 CSV 이력 로드 중...", end=" ", flush=True)
    seen_companies, _ = load_history_from_csvs()
    seen_emails = load_emails_from_csvs()
    print(f"{len(seen_companies)}개 회사, {len(seen_emails)}개 이메일 이력 발견")

    # ── 수집 시작 ──
    print(f"\n[수집 중] 이메일 {target_emails}건 목표로 진행합니다...")
    print("  공고 검색:")

    results_with_email = []
    results_no_email = []
    total_processed = 0
    email_usage = dict(seen_emails)  # 기존 CSV 이력의 이메일도 포함 (중복 감지용)

    for candidate in collect_candidates(conn, seen_companies):
        total_processed += 1
        name = candidate["company_name"]

        print(f"\n  [{total_processed}] {name}...", end=" ", flush=True)

        data = process_company(candidate)

        ceo_status = data["ceo_name"] or "-"
        site_status = "O" if data["website"] else "X"

        if data["email"]:
            email = data["email"].lower()
            # 이메일 중복 감지: 서로 다른 기업인데 같은 이메일이면 스킵
            if email in email_usage:
                prev_company = email_usage[email]
                print(f"대표: {ceo_status}, 이메일: {email} ✗ 중복 ('{prev_company}'과 동일 이메일, 스킵)")
                results_no_email.append(data)
            else:
                email_usage[email] = name
                results_with_email.append(data)
                print(f"대표: {ceo_status}, 이메일: {data['email']} ✓ ({len(results_with_email)}/{target_emails})")
        else:
            results_no_email.append(data)
            print(f"대표: {ceo_status}, 사이트: {site_status}, 이메일: -")

        # 목표 달성 체크
        if len(results_with_email) >= target_emails:
            print(f"\n  목표 {target_emails}건 달성!")
            break

        time.sleep(0.5)

    # ── 결과 저장 ──
    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    csv_path = os.path.join(OUTPUT_DIR, f"recruit_{timestamp}.csv")

    email_count = len(results_with_email)

    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["기업명", "대표자명", "공고 키워드", "이메일", "홈페이지", "출처"])
        for data in results_with_email:
            save_company(conn, data, included_in_csv=True)
            writer.writerow([
                data["company_name"],
                data["ceo_name"],
                data["keywords"],
                data["email"],
                data["website"],
                data.get("source", ""),
            ])

    # 이메일 없는 회사도 DB에 기록 (다음 실행 시 참고)
    for data in results_no_email:
        save_company(conn, data, included_in_csv=False)

    conn.close()

    with_ceo = sum(1 for d in results_with_email if d["ceo_name"])

    print("\n" + "=" * 60)
    print(" 수집 완료!")
    print(f" 탐색한 회사: {total_processed}개")
    print(f" 이메일 수집: {email_count}건 (CSV에 저장됨)")
    if email_count < target_emails:
        print(f" ⚠ 목표 {target_emails}건 미달 — 신규 공고가 부족합니다")
    print(f" 대표자명 수집: {with_ceo}건")
    print(f" 저장 위치: {csv_path}")
    print("=" * 60)


if __name__ == "__main__":
    main()
