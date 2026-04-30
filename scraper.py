#!/usr/bin/env python3
"""
채용 공고 회사 이메일 수집기 (사람인 + 잡코리아)
- 사람인/잡코리아에서 키워드별 최근 1주일 공고 검색
- 공고 ID 기준 중복 체크, 같은 회사 30일 쿨다운
- 회사 공식 사이트에서 공개 이메일 수집
- CSV 출력 (이메일 있는 회사만)
"""

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
    except requests.RequestException:
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


def get_company_info_from_jobkorea(company_name):
    """잡코리아에는 기업 상세 정보 접근이 어려우므로, 사람인에서 회사명으로 검색"""
    info = {"ceo_name": "", "website": ""}

    # 사람인에서 회사명 검색
    url = "https://www.saramin.co.kr/zf_user/search/company"
    params = {"searchword": company_name, "searchType": "search"}

    try:
        resp = requests.get(url, params=params, headers=HEADERS, timeout=10)
        resp.raise_for_status()
    except requests.RequestException:
        return info

    soup = BeautifulSoup(resp.text, "lxml")

    # 첫 번째 기업 결과의 링크
    corp_link_tag = soup.select_one(".corp_name a, .company_nm a, [class*='corp'] a")
    if corp_link_tag and corp_link_tag.get("href"):
        corp_link = corp_link_tag["href"]
        if not corp_link.startswith("http"):
            corp_link = "https://www.saramin.co.kr" + corp_link
        return get_company_info_from_saramin(corp_link)

    return info


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

def main():
    print("=" * 60)
    print(" 채용 공고 회사 이메일 수집기 (사람인 + 잡코리아)")
    print(f" 실행 시각: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f" 회사 쿨다운: {COMPANY_COOLDOWN_DAYS}일")
    print("=" * 60)

    conn = init_db()
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # CSV 이력 로드 (Git 공유 중복 체크)
    print("\n기존 CSV 이력 로드 중...", end=" ", flush=True)
    seen_companies, _ = load_history_from_csvs()
    print(f"{len(seen_companies)}개 회사 이력 발견")

    all_companies = {}  # company_name -> data
    new_postings = 0
    skipped_postings = 0

    # ── 1단계: 공고 검색 (사람인 + 잡코리아) ──
    print("\n[1/3] 공고 검색 중...")

    for site_name, search_fn in [("사람인", search_saramin), ("잡코리아", search_jobkorea)]:
        print(f"\n  [{site_name}]")
        for kw in KEYWORDS:
            print(f"    '{kw}' 검색 중...", end=" ", flush=True)
            results = search_fn(kw)
            site_new = 0

            for r in results:
                pid = r["posting_id"]

                # 공고 ID 중복 체크 (로컬 DB)
                if pid and is_posting_seen(conn, pid):
                    skipped_postings += 1
                    continue

                # 공고 저장 (로컬 DB)
                if pid:
                    save_posting(conn, pid, r["source"], r["company_name"], kw)
                    new_postings += 1

                name = r["company_name"]

                # 회사 쿨다운 체크 (CSV 이력 기반 — Git 공유)
                if is_company_in_cooldown_csv(seen_companies, name):
                    continue
                # 로컬 DB 쿨다운도 체크
                if is_company_in_cooldown(conn, name):
                    continue

                if name in all_companies:
                    existing_kws = all_companies[name]["keywords"].split(", ")
                    if kw not in existing_kws:
                        all_companies[name]["keywords"] += f", {kw}"
                else:
                    all_companies[name] = {
                        "company_name": name,
                        "keywords": kw,
                        "corp_link": r.get("corp_link", ""),
                        "job_title": r["job_title"],
                        "source": r["source"],
                        "ceo_name": "",
                        "email": "",
                        "website": "",
                    }
                    site_new += 1

            print(f"{len(results)}건 발견, 신규 회사 {site_new}건")
            time.sleep(2)  # 차단 방지

    total = len(all_companies)
    print(f"\n  신규 공고: {new_postings}건 (스킵: {skipped_postings}건)")

    if total == 0:
        print("\n신규 수집 대상이 없습니다.")
        conn.close()
        return

    print(f"  수집 대상 회사: {total}개")

    # ── 2단계: 회사 상세 정보 수집 ──
    print("\n[2/3] 회사 정보 수집 중 (대표자명, 홈페이지)...")
    for i, (name, data) in enumerate(all_companies.items(), 1):
        print(f"  ({i}/{total}) {name}...", end=" ", flush=True)

        if data["corp_link"]:
            # 사람인 기업 페이지에서 직접 수집
            info = get_company_info_from_saramin(data["corp_link"])
        else:
            # 잡코리아 공고 → 사람인에서 회사명으로 검색
            info = get_company_info_from_jobkorea(name)

        data["ceo_name"] = info["ceo_name"]
        data["website"] = info["website"]
        print(f"대표: {info['ceo_name'] or '-'}, 사이트: {'O' if info['website'] else 'X'}")
        time.sleep(0.8)

    # ── 3단계: 이메일 수집 ──
    print("\n[3/3] 회사 홈페이지에서 이메일 수집 중...")
    has_website = [d for d in all_companies.values() if d["website"]]
    print(f"  홈페이지 있는 회사: {len(has_website)}/{total}개")

    for i, data in enumerate(has_website, 1):
        print(f"  ({i}/{len(has_website)}) {data['company_name']}...", end=" ", flush=True)
        email = scrape_email_from_website(data["website"])
        data["email"] = email
        print(email or "-")
        time.sleep(0.5)

    # ── 결과 저장 ──
    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    csv_path = os.path.join(OUTPUT_DIR, f"recruit_{timestamp}.csv")

    email_count = 0
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["기업명", "대표자명", "공고 키워드", "이메일", "홈페이지", "출처"])
        for data in all_companies.values():
            has_email = bool(data["email"])
            save_company(conn, data, included_in_csv=has_email)
            if has_email:
                writer.writerow([
                    data["company_name"],
                    data["ceo_name"],
                    data["keywords"],
                    data["email"],
                    data["website"],
                    data.get("source", ""),
                ])
                email_count += 1

    conn.close()

    with_ceo = sum(1 for d in all_companies.values() if d["ceo_name"])

    print("\n" + "=" * 60)
    print(" 수집 완료!")
    print(f" 총 회사 수: {total}")
    print(f" 이메일 수집 성공: {email_count}건 (CSV에 저장됨)")
    print(f" 이메일 없는 회사: {total - email_count}건 (CSV에서 제외)")
    print(f" 대표자명 수집: {with_ceo}건")
    print(f" 저장 위치: {csv_path}")
    print("=" * 60)


if __name__ == "__main__":
    main()
