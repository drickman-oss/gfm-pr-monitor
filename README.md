# GoFundMe PR Placement Monitor

Automatically finds UK (and international) news coverage of GoFundMe pitched fundraisers — replacing manual Meltwater searches.

**90% hit rate** validated against known placements. Uses Google News RSS — no API key, no rate limits, full UK regional press coverage.

## How it works

1. Export your pitch data from Looker (`gofundme_v2 pitchtool_v2` view)
2. Run the script — it searches Google News for each fundraiser by name
3. Get an HTML digest of potential placements to confirm in your CMS

## Setup

```bash
# Install dependencies
pip install requests python-dotenv

# Add to ~/.env (only needed for email sending)
GMAIL_USER=your@gmail.com
GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx
```

## Usage

```bash
# Preview output — no email sent
python3 pr_placement_monitor.py --csv 'pitchtool_export.csv' --month 2026-03 --preview

# Validate accuracy against known placements first
python3 pr_placement_monitor.py --csv 'pitchtool_export.csv' --month 2026-03 --validate --limit 20 --preview

# Full run — sends email digest
python3 pr_placement_monitor.py --csv 'pitchtool_export.csv' --month 2026-03 --limit 50
```

## CSV format (Looker export)

Required columns:
| Column | Description |
|--------|-------------|
| `Pitch Month` | Format: `2026-03` |
| `Fundraiser Name` | Used as search query |
| `Fundraiser Link` | GoFundMe URL (cache key) |
| `Fundraiser Description` | HTML — stripped for context |
| `Total Pitches` | Used to prioritise searches |
| `Total Placements` | 0 = unplaced, >0 = already placed |

## Output

HTML digest showing:
- **Potential placements** — fundraisers found in news coverage, with headline, source, link
- **Not found** — pitched but no coverage detected

Comms team reviews and confirms in CMS.

## Notes

- Cache stored at `~/pr_placement_cache.json` — avoids re-searching the same fundraiser
- Run with `--no-cache` to force re-search
- Google News free tier — no limits on date range or request volume
- False positives possible for generic names (e.g. "Help our family") — always confirm before marking as placed
