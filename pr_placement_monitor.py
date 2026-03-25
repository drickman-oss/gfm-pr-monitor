#!/usr/bin/env python3
"""
GoFundMe PR Placement Monitor
Searches Google News for UK media coverage of pitched fundraisers.
Sends a weekly/monthly email digest to the comms team.

Usage:
  python3 ~/pr_placement_monitor.py                        # search last month's unplaced pitches
  python3 ~/pr_placement_monitor.py --month 2026-02        # specific month
  python3 ~/pr_placement_monitor.py --validate             # test against known placements
  python3 ~/pr_placement_monitor.py --preview              # generate HTML only, no email
  python3 ~/pr_placement_monitor.py --limit 100            # override search limit

Needs in ~/.env:
  GMAIL_USER=dina.rickman@gmail.com
  GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx
"""

import os
import csv
import re
import json
import time
import smtplib
import requests
import argparse
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from html.parser import HTMLParser
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import parsedate
from dotenv import load_dotenv
import csv as csvlib

load_dotenv(os.path.expanduser('~/.env'))

NEWSAPI_KEY        = os.getenv('NEWSAPI_KEY')
GMAIL_USER         = os.getenv('GMAIL_USER')
GMAIL_APP_PASSWORD = os.getenv('GMAIL_APP_PASSWORD')

DEFAULT_PITCH_CSV  = os.path.expanduser('~/Downloads/gofundme_v2 pitchtool_v2 2026-03-16T1248.csv')
CACHE_FILE         = os.path.expanduser('~/pr_placement_cache.json')

# Default: top 50 per run stays within free tier (100/day leaves headroom)
DEFAULT_LIMIT = 50

# UK outlet keywords — used to filter / flag results
UK_SIGNALS = [
    'BBC', 'Guardian', 'Daily Mail', 'Mirror', 'Telegraph', 'Independent',
    'Sun', 'The Times', 'Sky', 'Metro', 'Evening Standard', 'Express',
    'Chronicle', 'Herald', 'Gazette', 'Echo', 'Post', '.co.uk', 'Manchester',
    'Liverpool', 'Leeds', 'Birmingham', 'Glasgow', 'Edinburgh', 'Bristol',
    'Wales', 'Scotland', 'Yorkshire', 'Lancashire', 'Essex', 'Kent'
]

# ─────────────────────────────────────────────
# DATA LOADING
# ─────────────────────────────────────────────

class HTMLStripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text = []
    def handle_data(self, data):
        self.text.append(data)
    def get_text(self):
        return ' '.join(self.text)

def strip_html(html):
    if not html:
        return ''
    s = HTMLStripper()
    try:
        s.feed(html)
    except Exception:
        pass
    return re.sub(r'\s+', ' ', s.get_text()).strip()

def parse_num(val):
    try:
        return int(str(val).replace(',', '').strip() or 0)
    except Exception:
        return 0

def load_pitches(csv_path, month_filter=None, pitcher_filter=None):
    """
    pitcher_filter: list of partial pitcher name strings (case-insensitive).
    e.g. ['asa bennett', 'adela'] will match any pitcher whose name contains either string.
    """
    pitches = []
    with open(csv_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            month = (row.get('Pitch Month') or '').strip()
            if not month or month == 'Pitch Month':
                continue
            if month_filter and month != month_filter:
                continue
            if pitcher_filter:
                full_name = f"{row.get('First Name','').strip()} {row.get('Last Name','').strip()}".lower()
                if not any(p.lower() in full_name for p in pitcher_filter):
                    continue
            link = (row.get('Fundraiser Link') or '').strip()
            pitches.append({
                'id':               link,
                'name':             f"{row.get('First Name','').strip()} {row.get('Last Name','').strip()}",
                'first_name':       row.get('First Name', '').strip(),
                'last_name':        row.get('Last Name', '').strip(),
                'fundraiser_name':  (row.get('Fundraiser Name') or '').strip(),
                'fundraiser_link':  link,
                'description':      strip_html(row.get('Fundraiser Description', '')),
                'pitch_month':      month,
                'total_pitches':    parse_num(row.get('Total Pitches', 0)),
                'total_placements': parse_num(row.get('Total Placements', 0)),
            })
    return pitches

# ─────────────────────────────────────────────
# CACHE
# ─────────────────────────────────────────────

def load_cache():
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE) as f:
            return json.load(f)
    return {}

def save_cache(cache):
    with open(CACHE_FILE, 'w') as f:
        json.dump(cache, f, indent=2)

# ─────────────────────────────────────────────
# SEARCH
# ─────────────────────────────────────────────

def extract_slug(fundraiser_link):
    """Extract the slug from a GoFundMe URL, e.g. 'help-james-taylor-fight-cancer'."""
    match = re.search(r'gofundme\.com/f/([^/?#\s]+)', fundraiser_link or '')
    return match.group(1) if match else None

def build_queries(pitch):
    """
    Returns a list of (label, query) pairs to try in order.
    Slug-based search is tried first — if an article mentions the GoFundMe URL
    it's definitively about this fundraiser. Name search is the fallback.
    """
    queries = []
    slug = extract_slug(pitch['fundraiser_link'])
    if slug:
        queries.append(('slug', f'gofundme.com/f/{slug}'))
    name = pitch['fundraiser_name'].strip()
    for phrase in ['GoFundMe', 'Help Us', 'Help Me', 'Support for', 'In Loving Memory',
                   'Fundraiser for', 'Raising money for']:
        name = re.sub(re.escape(phrase), '', name, flags=re.IGNORECASE).strip(' :-')
    name = name.strip(' :-').strip() or pitch['fundraiser_name']
    queries.append(('name', f'"{name}" GoFundMe'))
    return queries

STOPWORDS = {
    'the','a','an','and','or','for','to','in','of','on','at','is','are','was',
    'were','help','support','fund','gofundme','our','his','her','their','my',
    'after','with','from','into','during','through','about','by','as','up',
}

GENERIC_CAPS = {
    'Help', 'Fund', 'Support', 'Fight', 'Save', 'Give', 'Memory', 'Memorial',
    'Medical', 'Cancer', 'Family', 'Journey', 'Battle', 'Appeal', 'Raise',
    'After', 'During', 'Against', 'With', 'From', 'Their', 'This', 'That',
}

def headline_confidence(fundraiser_name, headline):
    """
    Returns 'High' only when the headline is a strong match for this specific fundraiser.
    Rules:
      1. At least 2 significant words must match.
      2. Overall word-overlap score must be >= 0.70.
      3. If the fundraiser name contains a person name (2+ consecutive capitalised
         words that aren't generic nouns), at least one consecutive pair must appear
         as a phrase in the headline — prevents "James" matching "James Van Der Beek".
    """
    words = [
        w.lower() for w in re.findall(r'\w+', fundraiser_name)
        if w.lower() not in STOPWORDS and len(w) > 3
    ]
    if not words:
        return 'Low'
    headline_lower = headline.lower()
    matches = sum(1 for w in words if w in headline_lower)

    if matches < 2:
        return 'Low'
    if matches / len(words) < 0.70:
        return 'Low'

    # Person-name phrase check — extract consecutive capitalised name words
    cap_words = [w for w in re.findall(r'\b[A-Z][a-z]{2,}\b', fundraiser_name)
                 if w not in GENERIC_CAPS]
    if len(cap_words) >= 2:
        pairs = [f'{cap_words[i].lower()} {cap_words[i+1].lower()}'
                 for i in range(len(cap_words) - 1)]
        if not any(p in headline_lower for p in pairs):
            return 'Low'

    return 'High'

def is_likely_uk(article):
    source_name = (article.get('source') or {}).get('name', '') or ''
    url = article.get('url', '') or ''
    text = source_name + ' ' + url
    return any(sig.lower() in text.lower() for sig in UK_SIGNALS)

def parse_rss_date(date_str):
    """Parse RSS/RFC 2822 or ISO date string to datetime, or None on failure."""
    if not date_str:
        return None
    try:
        t = parsedate(date_str)
        if t:
            return datetime(*t[:6])
    except Exception:
        pass
    try:
        return datetime.strptime(date_str[:10], '%Y-%m-%d')
    except Exception:
        return None

def article_in_date_window(article, pitch_month):
    """Return True if article date falls within 30 days before to 90 days after pitch month start."""
    pub = parse_rss_date(article.get('publishedAt', ''))
    if not pub:
        return True  # can't determine date — include it
    try:
        pm_start = datetime.strptime(pitch_month + '-01', '%Y-%m-%d')
        delta = (pub - pm_start).days
        return -30 <= delta <= 90
    except Exception:
        return True

USER_AGENTS = [
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Safari/605.1.15',
]
_ua_index = 0

def search_google_news(query):
    """
    Search Google News RSS (UK edition) for coverage of a fundraiser.
    No API key. No rate limit. Full UK regional press coverage.
    Returns articles in the same format as the old NewsAPI results.
    """
    global _ua_index
    q = urllib.parse.quote(query)
    url = f'https://news.google.com/rss/search?q={q}&hl=en-GB&gl=GB&ceid=GB:en'
    for attempt in range(3):
        try:
            ua = USER_AGENTS[_ua_index % len(USER_AGENTS)]
            _ua_index += 1
            r = requests.get(url, headers={'User-Agent': ua}, timeout=15)
            if r.status_code == 503:
                wait = 10 * (attempt + 1)
                print(f"  ⚠ 503 — waiting {wait}s before retry {attempt+1}/3")
                time.sleep(wait)
                continue
            if r.status_code != 200:
                print(f"  ⚠ Google News returned {r.status_code}")
                return []
            root = ET.fromstring(r.content)
            articles = []
            for item in root.findall('.//item')[:5]:
                title_raw = item.findtext('title', '')
                link      = item.findtext('link', '')
                pub_date  = item.findtext('pubDate', '')
                # Google News appends " - Source Name" to titles
                if ' - ' in title_raw:
                    parts        = title_raw.rsplit(' - ', 1)
                    title_clean  = parts[0].strip()
                    source_name  = parts[1].strip()
                else:
                    title_clean  = title_raw
                    source_name  = ''
                articles.append({
                    'title':       title_clean,
                    'url':         link,
                    'source':      {'name': source_name},
                    'publishedAt': pub_date,
                    'description': '',
                })
            return articles
        except Exception as e:
            print(f"  ⚠ Google News error: {e}")
            return []
    return []

# ─────────────────────────────────────────────
# EMAIL
# ─────────────────────────────────────────────

def _article_card(a):
    pub_raw     = a.get('publishedAt') or ''
    pub_date    = parse_rss_date(pub_raw)
    pub_str     = pub_date.strftime('%-d %b %Y') if pub_date else pub_raw[:10]
    source_name = (a.get('source') or {}).get('name', 'Unknown')
    title       = a.get('title', '(no title)')
    url         = a.get('url', '#')
    desc        = ((a.get('description') or '')[:130] + '…') if a.get('description') else ''
    uk_badge    = '🇬🇧 ' if is_likely_uk(a) else ''
    return f"""
  <div style="background:#fff;border:1px solid #E5E7EB;border-radius:6px;padding:10px 12px;margin-bottom:6px">
    <div style="font-size:10px;color:#00B964;font-weight:700;text-transform:uppercase;letter-spacing:.04em;margin-bottom:3px">
      {uk_badge}{source_name} &nbsp;·&nbsp; {pub_str}
    </div>
    <a href="{url}" style="color:#111;font-weight:600;font-size:12px;text-decoration:none;line-height:1.4">{title}</a>
    <p style="font-size:11px;color:#6B7280;margin:4px 0 0;line-height:1.5">{desc}</p>
  </div>"""

def format_email(results, month, validate_mode=False):
    found     = [r for r in results if r['articles']]
    not_found = [r for r in results if not r['articles']]
    now_str   = datetime.now().strftime('%-d %B %Y')

    header_note = (
        f"VALIDATE MODE — testing against {len(results)} known placements · {now_str}"
        if validate_mode else
        f"{month} pitches · run {now_str} · top {len(results)} by pitch count"
    )

    html = f"""<!DOCTYPE html>
<html><body style="font-family:-apple-system,BlinkMacSystemFont,sans-serif;max-width:680px;margin:0 auto;padding:20px;color:#111">

<div style="background:#00B964;padding:16px 24px;border-radius:10px;margin-bottom:24px">
  <h1 style="color:#fff;margin:0;font-size:18px;font-weight:800">GoFundMe PR Placement Digest</h1>
  <p style="color:rgba(255,255,255,.85);margin:6px 0 0;font-size:12px">
    {header_note}<br>
    <strong style="color:#fff">{len(found)} potential placements</strong> found &nbsp;·&nbsp;
    {len(not_found)} not found
  </p>
</div>
"""

    # ── GROUP BY PITCHER ──
    pitchers = {}
    for r in results:
        pitchers.setdefault(r['name'], []).append(r)

    for pitcher_name in sorted(pitchers.keys()):
        pitcher_results = pitchers[pitcher_name]
        p_found     = [r for r in pitcher_results if r['articles']]
        p_not_found = [r for r in pitcher_results if not r['articles']]

        html += f"""
<div style="margin-bottom:32px;border:1px solid #E5E7EB;border-radius:10px;overflow:hidden">
  <div style="background:#F9FAFB;padding:10px 16px;border-bottom:1px solid #E5E7EB;display:flex;justify-content:space-between;align-items:center">
    <span style="font-weight:700;font-size:14px">{pitcher_name}</span>
    <span style="font-size:12px;color:#6B7280">{len(p_found)} found &nbsp;·&nbsp; {len(p_not_found)} not found</span>
  </div>"""

        if p_found:
            html += f'<div style="padding:14px 16px">'
            html += f'<div style="font-size:11px;font-weight:600;color:#00B964;text-transform:uppercase;letter-spacing:.05em;margin-bottom:10px">✅ Potential placements — please confirm in CMS</div>'
            for r in p_found:
                html += f"""
<div style="border:1.5px solid #D1FAE5;border-radius:8px;padding:12px 14px;margin-bottom:10px;background:#F0FBF6">
  <div style="font-size:11px;color:#6B7280;margin-bottom:8px">
    <strong style="color:#111">{r['fundraiser_name'][:70]}</strong>
    &nbsp;·&nbsp;<a href="{r['fundraiser_link']}" style="color:#00B964">{r['fundraiser_link'][:50]}</a>
  </div>"""
                for a in r['articles']:
                    html += _article_card(a)
                html += '</div>'
            html += '</div>'

        if p_not_found:
            html += f'<div style="padding:10px 16px;border-top:1px solid #F3F4F6">'
            html += f'<div style="font-size:11px;font-weight:600;color:#9CA3AF;text-transform:uppercase;letter-spacing:.05em;margin-bottom:8px">⭕ No coverage found</div>'
            html += '<div style="display:flex;flex-wrap:wrap;gap:6px">'
            for r in p_not_found:
                html += f'<span style="font-size:11px;background:#F3F4F6;border-radius:4px;padding:3px 8px;color:#374151">{r["fundraiser_name"][:45]}</span>'
            html += '</div></div>'

        html += '</div>'

    html += f"""
<p style="font-size:11px;color:#9CA3AF;margin-top:22px;padding-top:12px;border-top:1px solid #E5E7EB;line-height:1.6">
  Searched via Google News · UK sources prioritised · {len(results)} fundraisers checked<br>
  Found a missed placement? Reply to this email with the URL and we'll add it.
</p>
</body></html>"""
    return html

def send_email(html_body, recipients, month):
    msg = MIMEMultipart('alternative')
    msg['Subject'] = f"PR Placement Digest — {month} · {datetime.now().strftime('%-d %b %Y')}"
    msg['From']    = GMAIL_USER
    msg['To']      = ', '.join(recipients)
    msg.attach(MIMEText(html_body, 'html'))
    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_USER, recipients, msg.as_string())
    print(f"✓ Email sent to {recipients}")

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='GoFundMe PR Placement Monitor')
    parser.add_argument('--csv',        default=DEFAULT_PITCH_CSV, help='Path to pitch CSV')
    parser.add_argument('--month',      default=None,              help='Pitch month e.g. 2026-02 (default: last month)')
    parser.add_argument('--limit',      type=int, default=DEFAULT_LIMIT, help='Max searches per run (default 50)')
    parser.add_argument('--recipients', nargs='+', default=['drickman@gofundme.com'], help='Email recipients')
    parser.add_argument('--preview',    action='store_true', help='Save HTML preview only — no email sent')
    parser.add_argument('--validate',   action='store_true', help='Test against already-placed fundraisers to measure accuracy')
    parser.add_argument('--no-cache',      action='store_true', help='Ignore cache and re-search everything')
    parser.add_argument('--refresh-empty', action='store_true', help='Re-search pitches previously cached with no coverage found')
    parser.add_argument('--rate',       type=int, default=40, help='Requests per hour (default 40, increase with caution)')
    parser.add_argument('--pitcher',    nargs='+', default=None, help='Filter by pitcher name(s), e.g. --pitcher "asa bennett" adela')
    args = parser.parse_args()

    # Default month = last calendar month
    if not args.month:
        first_of_this_month = datetime.now().replace(day=1)
        last_month = first_of_this_month - timedelta(days=1)
        args.month = last_month.strftime('%Y-%m')

    print(f"\n{'='*50}")
    print(f"PR Placement Monitor — {args.month}")
    print(f"{'='*50}")

    print(f"Loading pitches...")
    all_pitches = load_pitches(args.csv, month_filter=args.month, pitcher_filter=args.pitcher)
    print(f"Found {len(all_pitches)} pitches for {args.month}")

    if args.validate:
        # Test mode: only search pitches already confirmed as placed
        targets = [p for p in all_pitches if p['total_placements'] > 0]
        print(f"VALIDATE MODE: testing against {len(targets)} known placements")
    else:
        # Normal mode: search unplaced, highest pitch count first (most pitched = priority stories)
        targets = [p for p in all_pitches if p['total_placements'] == 0]
        targets.sort(key=lambda x: x['total_pitches'], reverse=True)
        print(f"{len(targets)} unplaced pitches — searching top {args.limit} by pitch count")

    targets = targets[:args.limit]

    sleep_secs = 3600 / args.rate
    uncached   = sum(1 for p in targets if (p['id'] or p['name']) not in ({} if args.no_cache else load_cache()))
    eta_mins   = round(uncached * sleep_secs / 60)
    print(f"Rate: {args.rate}/hr ({sleep_secs:.0f}s between requests) · ~{eta_mins} min ETA for {uncached} uncached pitches")

    cache = {} if args.no_cache else load_cache()
    results = []
    api_calls = 0

    for i, pitch in enumerate(targets):
        cache_key = pitch['id'] or pitch['name']

        if cache_key in cache:
            cached = cache[cache_key]
            if cached or not args.refresh_empty:
                print(f"[{i+1}/{len(targets)}] Cache hit: {pitch['name']}")
                results.append({**pitch, 'articles': cached})
                continue
            # refresh_empty=True and cache was empty — fall through to re-search

        articles = []
        for label, query in build_queries(pitch):
            print(f"[{i+1}/{len(targets)}] Searching ({label}): {query}")
            raw = search_google_news(query)
            if label == 'slug':
                # Slug match is definitive — no confidence filter needed, just date
                filtered = [a for a in raw if article_in_date_window(a, args.month)]
            else:
                filtered = [
                    a for a in raw
                    if headline_confidence(pitch['fundraiser_name'], a.get('title', '')) == 'High'
                    and article_in_date_window(a, args.month)
                ]
            articles.extend(filtered)
            if articles:
                break  # Found via higher-confidence query — no need for fallback

        results.append({**pitch, 'articles': articles, 'skipped': False})
        cache[cache_key] = articles
        api_calls += 1

        # Save cache after every request so progress is preserved if interrupted
        save_cache(cache)

        if i < len(targets) - 1:
            time.sleep(sleep_secs)

    print(f"\nDone. {api_calls} API calls made. Cache saved.")

    # ── Validate report ──
    if args.validate:
        found_count = sum(1 for r in results if r['articles'])
        pct = int(100 * found_count / len(results)) if results else 0
        print(f"\n=== VALIDATION RESULTS ===")
        print(f"Known placements tested : {len(results)}")
        print(f"Found by search         : {found_count} ({pct}%)")
        print(f"Missed                  : {len(results) - found_count} ({100-pct}%)")
        missed = [r for r in results if not r['articles']]
        if missed:
            print("\nMissed placements (check these manually to understand why):")
            for r in missed[:10]:
                print(f"  - {r['name']} | {r['fundraiser_link']}")

    # ── CSV output (horizontal — one row per fundraiser) ──
    max_articles = max((len(r['articles']) for r in results), default=0)
    csv_path = os.path.expanduser(f'~/Downloads/pr_placements_{args.month}.csv')
    with open(csv_path, 'w', newline='', encoding='utf-8') as f:
        writer = csvlib.writer(f)
        # Header: fixed columns then placement groups
        header = ['Pitcher Name', 'Fundraiser Name', 'GoFundMe URL', 'Found?', 'Confidence']
        for n in range(1, max_articles + 1):
            header += [f'Source {n}', f'Headline {n}', f'URL {n}', f'Date {n}']
        writer.writerow(header)
        for r in results:
            if r['articles']:
                # Overall confidence = High only if at least one article scores High
                confidences = [
                    headline_confidence(r['fundraiser_name'], a.get('title', ''))
                    for a in r['articles']
                ]
                overall = 'High' if 'High' in confidences else 'Low'
            else:
                overall = ''
            row = [r['name'], r['fundraiser_name'], r['fundraiser_link'],
                   'YES' if r['articles'] else 'NO', overall]
            for a in r['articles']:
                row += [
                    (a.get('source') or {}).get('name', ''),
                    a.get('title', ''),
                    a.get('url', ''),
                    (a.get('publishedAt') or '')[:16],
                ]
            writer.writerow(row)
    print(f"✓ CSV saved:     {csv_path}")

    # ── HTML email ──
    html = format_email(results, args.month, validate_mode=args.validate)
    preview_path = os.path.expanduser('~/Downloads/pr_placement_digest_preview.html')
    with open(preview_path, 'w') as f:
        f.write(html)
    print(f"✓ HTML preview:  {preview_path}")

    if args.preview or args.validate:
        print("(Preview mode — no email sent)")
    else:
        send_email(html, args.recipients, args.month)


if __name__ == '__main__':
    main()
