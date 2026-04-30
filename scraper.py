#!/usr/bin/env python3
"""
사람인 경영지원 공고 회사 정보 수집기
- 사람인에서 키워드별 최근 1주일 공고의 회사명/공고 키워드 수집
- 회사 공식 사이트에서 공개 이메일 수집 (문의하기 폼, footer, 내부 링크 포함)
- SQLite로 중복 제거
- CSV 출력 (이메일 있는 회사만)
"""

import requests
from bs4 import BeautifulSoup
import sqlite3
import csv
import re
import time
import os
from datetime import datetime
from urllib.parse import urlparse, urljoin

# ── 설정 ──
KEYWORDS = [
    "경영지원", "경리", "총무", "비서",
    "과제비 관리", "정부지원사업 관리", "연구비 관리", "인사"
]

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
    "domain.com", "company.com", "your-domain.com",
    "website.com",
}

IGNORE_EMAILS_EXACT = {
    "hosting@gabia.com", "mail@example.org",
    "info@yourdomain.com", "info@yourwebsite.com",
    "admin@example.com", "user@example.com",
    "you@website.com", "mytory@gmail.com",
    "highlight@sedaily.com", "greenremodeling@kalis.or.kr",
}

IGNORE_EMAIL_PREFIXES = [
    "noreply@", "no-reply@", "no_reply@",
    "postmaster@", "mailer-daemon@",
    "hosting@gabia", "webmaster@gabia",
]

# 이미지/파일 확장자
IGNORE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".css", ".js"}


def is_valid_email(email):
    """쓰레기 이메일 필터링"""
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
    # 해시 형태 이메일 제외 (sentry 등)
    local = email.split("@")[0]
    if len(local) > 30 and re.match(r"^[a-f0-9]+$", local):
        return False
    return True


def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS companies (
            company_name TEXT PRIMARY KEY,
            ceo_name TEXT,
            keywords TEXT,
            email TEXT,
            website TEXT,
            collected_at TEXT
        )
    """)
    conn.commit()
    return conn


def is_duplicate(conn, company_name):
    c = conn.cursor()
    c.execute("SELECT 1 FROM companies WHERE company_name = ?", (company_name,))
    return c.fetchone() is not None


def save_to_db(conn, company):
    c = conn.cursor()
    c.execute("""
        INSERT OR REPLACE INTO companies
        (company_name, ceo_name, keywords, email, website, collected_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        company["company_name"],
        company.get("ceo_name", ""),
        company.get("keywords", ""),
        company.get("email", ""),
        company.get("website", ""),
        datetime.now().isoformat()
    ))
    conn.commit()


def search_saramin(keyword, page=1):
    """사람인에서 키워드로 최근 1주일 공고 검색"""
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

    try:
        resp = requests.get(url, params=params, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"  [오류] '{keyword}' 검색 실패: {e}")
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

        corp_link = ""
        link_tag = corp_tag if corp_tag.name == "a" else corp_tag.find("a")
        if link_tag and link_tag.get("href"):
            corp_link = link_tag["href"]
            if not corp_link.startswith("http"):
                corp_link = "https://www.saramin.co.kr" + corp_link

        results.append({
            "company_name": company_name,
            "job_title": job_title,
            "keyword": keyword,
            "corp_link": corp_link,
        })

    return results


def get_company_info_from_saramin(corp_link):
    """사람인 기업 페이지에서 대표자명, 홈페이지 URL 추출"""
    info = {"ceo_name": "", "website": ""}
    if not corp_link:
        return info

    try:
        resp = requests.get(corp_link, headers=HEADERS, timeout=10)
        resp.raise_for_status()
    except requests.RequestException:
        return info

    soup = BeautifulSoup(resp.text, "lxml")

    # 대표자명 — dt/dd 패턴
    for dt in soup.select("dt"):
        text = dt.get_text(strip=True)
        if "대표자" in text or "대표이사" in text:
            dd = dt.find_next_sibling("dd")
            if dd:
                info["ceo_name"] = dd.get_text(strip=True)
            break

    # 대표자명 — th/td 패턴
    if not info["ceo_name"]:
        for th in soup.select("th"):
            text = th.get_text(strip=True)
            if "대표" in text:
                td = th.find_next_sibling("td")
                if td:
                    info["ceo_name"] = td.get_text(strip=True)
                break

    # 홈페이지 — 텍스트에 "홈페이지" 포함된 링크
    for a in soup.select("a[href]"):
        href = a.get("href", "")
        text = a.get_text(strip=True)
        if ("홈페이지" in text or "homepage" in text.lower()) and href.startswith("http"):
            info["website"] = href
            break

    # 홈페이지 — class 기반
    if not info["website"]:
        for tag in soup.select("[class*='homepage'], [class*='url'], [class*='site']"):
            link = tag.get("href") or tag.get("data-href", "")
            if link.startswith("http"):
                info["website"] = link
                break

    # 홈페이지 — dt/dd 패턴
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


def _fetch_page(url, timeout=8):
    """페이지 HTML 가져오기 (에러 시 None)"""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        if resp.status_code != 200:
            return None
        return resp.text
    except requests.RequestException:
        return None


def _extract_emails_from_html(html):
    """HTML에서 유효한 이메일 추출"""
    emails = set()

    # 정규식으로 전체 텍스트에서 추출
    for email in EMAIL_PATTERN.findall(html):
        if is_valid_email(email):
            emails.add(email.lower())

    # mailto: 링크에서 추출
    soup = BeautifulSoup(html, "lxml")
    for a in soup.select("a[href^='mailto:']"):
        mailto = a["href"].replace("mailto:", "").split("?")[0].strip()
        if EMAIL_PATTERN.match(mailto) and is_valid_email(mailto):
            emails.add(mailto.lower())

    return emails


def _find_contact_links(html, base_url):
    """페이지에서 문의/연락처 관련 내부 링크 찾기"""
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

        # 내부 링크만
        if href.startswith("mailto:") or href.startswith("tel:"):
            continue

        is_contact = any(kw in text or kw in href_lower for kw in contact_keywords)
        if not is_contact:
            continue

        # 절대/상대 URL 처리
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


def scrape_email_from_website(url):
    """회사 공식 홈페이지에서 이메일 수집 (깊이 탐색)"""
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
        # 한국어 경로
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

    # 2차: 1차에서 못 찾으면, 메인 페이지의 내부 링크 탐색
    if not all_emails:
        main_html = _fetch_page(base_url)
        if main_html:
            # 메인 페이지 자체에서 이메일 추출
            all_emails.update(_extract_emails_from_html(main_html))

            if not all_emails:
                # 문의/연락처 관련 내부 링크 탐색
                contact_links = _find_contact_links(main_html, base_url)
                for link in list(contact_links)[:5]:  # 최대 5개
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

    # 3차: footer 영역에서 이메일 추출 (아직 못 찾은 경우)
    if not all_emails:
        main_html = _fetch_page(base_url) if base_url not in visited else None
        if not main_html:
            # 이미 가져온 페이지에서 footer 탐색
            main_html = _fetch_page(base_url)
        if main_html:
            soup = BeautifulSoup(main_html, "lxml")
            footer = soup.find("footer") or soup.select_one("[class*='footer']")
            if footer:
                footer_text = str(footer)
                for email in EMAIL_PATTERN.findall(footer_text):
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


def main():
    print("=" * 60)
    print(" 사람인 경영지원 공고 회사 정보 수집기")
    print(f" 실행 시각: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 60)

    conn = init_db()
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    all_companies = {}

    # 1단계: 사람인에서 공고 검색
    print("\n[1/3] 사람인 공고 검색 중...")
    for kw in KEYWORDS:
        print(f"  키워드: '{kw}' 검색 중...", end=" ", flush=True)
        results = search_saramin(kw)
        new_count = 0

        for r in results:
            name = r["company_name"]
            if is_duplicate(conn, name):
                continue

            if name in all_companies:
                existing_kws = all_companies[name]["keywords"].split(", ")
                if kw not in existing_kws:
                    all_companies[name]["keywords"] += f", {kw}"
            else:
                all_companies[name] = {
                    "company_name": name,
                    "keywords": kw,
                    "corp_link": r["corp_link"],
                    "job_title": r["job_title"],
                    "ceo_name": "",
                    "email": "",
                    "website": "",
                }
                new_count += 1

        print(f"{len(results)}건 발견, 신규 {new_count}건")
        time.sleep(1)

    total = len(all_companies)
    if total == 0:
        print("\n신규 수집 대상이 없습니다. (모두 이전에 수집 완료)")
        conn.close()
        return

    print(f"\n총 {total}개 신규 회사 발견")

    # 2단계: 회사 상세 정보 수집
    print("\n[2/3] 회사 정보 수집 중 (대표자명, 홈페이지)...")
    for i, (name, data) in enumerate(all_companies.items(), 1):
        print(f"  ({i}/{total}) {name}...", end=" ", flush=True)
        info = get_company_info_from_saramin(data["corp_link"])
        data["ceo_name"] = info["ceo_name"]
        data["website"] = info["website"]
        print(f"대표: {info['ceo_name'] or '-'}, 사이트: {'O' if info['website'] else 'X'}")
        time.sleep(0.8)

    # 3단계: 회사 홈페이지에서 이메일 수집
    print("\n[3/3] 회사 홈페이지에서 이메일 수집 중...")
    has_website = [d for d in all_companies.values() if d["website"]]
    print(f"  홈페이지 있는 회사: {len(has_website)}/{total}개")

    for i, data in enumerate(has_website, 1):
        print(f"  ({i}/{len(has_website)}) {data['company_name']}...", end=" ", flush=True)
        email = scrape_email_from_website(data["website"])
        data["email"] = email
        print(email or "-")
        time.sleep(0.5)

    # 결과 저장 (이메일 있는 회사만)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    csv_path = os.path.join(OUTPUT_DIR, f"saramin_{timestamp}.csv")

    email_count = 0
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["기업명", "대표자명", "공고 키워드", "이메일", "홈페이지"])
        for data in all_companies.values():
            # DB에는 전부 저장 (다음 실행 시 중복 제거용)
            save_to_db(conn, data)
            # CSV에는 이메일 있는 회사만
            if data["email"]:
                writer.writerow([
                    data["company_name"],
                    data["ceo_name"],
                    data["keywords"],
                    data["email"],
                    data["website"],
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
