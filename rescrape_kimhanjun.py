#!/usr/bin/env python3
"""김한준으로 잘못 수집된 281개 회사를 구글/네이버 검색으로 재수집"""

import csv
import time
import os
from datetime import datetime
from scraper import (
    find_company_website,
    scrape_email_from_website,
    get_ceo_from_website,
    _fetch_page,
    HEADERS,
)

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
COMPANIES_FILE = "/tmp/kimhanjun_companies.txt"


def main():
    with open(COMPANIES_FILE, "r", encoding="utf-8") as f:
        companies = [line.strip() for line in f if line.strip()]

    print(f"재수집 대상: {len(companies)}개 회사")
    print("=" * 60)

    results = []
    for i, name in enumerate(companies, 1):
        print(f"[{i}/{len(companies)}] {name}...", end=" ", flush=True)

        website = find_company_website(name)
        ceo_name = ""
        email = ""

        if website:
            html = _fetch_page(website)
            if html:
                ceo_name = get_ceo_from_website(html)
                if not ceo_name:
                    for path in ["/about", "/about-us", "/company", "/회사소개"]:
                        about_html = _fetch_page(website.rstrip("/") + path)
                        if about_html:
                            ceo_name = get_ceo_from_website(about_html)
                            if ceo_name:
                                break
                            time.sleep(0.3)

            email = scrape_email_from_website(website)

        status = f"사이트: {website or 'X'}, 대표: {ceo_name or '-'}, 이메일: {email or '-'}"
        if email:
            status += " OK"
        print(status)

        results.append({
            "company_name": name,
            "ceo_name": ceo_name,
            "email": email,
            "website": website or "",
        })

        time.sleep(1)

    # CSV 저장
    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    csv_path = os.path.join(OUTPUT_DIR, f"recruit_rescrape_{timestamp}.csv")

    with_email = [r for r in results if r["email"]]
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["기업명", "대표자명", "공고 키워드", "이메일", "홈페이지", "출처"])
        for r in with_email:
            writer.writerow([
                r["company_name"],
                r["ceo_name"],
                "",  # 키워드는 원본에서 복원 불가
                r["email"],
                r["website"],
                "rescrape",
            ])

    print("\n" + "=" * 60)
    print(f"완료! 총 {len(companies)}개 중 이메일 수집: {len(with_email)}건")
    print(f"저장: {csv_path}")


if __name__ == "__main__":
    main()
