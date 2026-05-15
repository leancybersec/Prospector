# Prospector Template

Disclaimer: This script is provided "as is" with no warranty. It uses third-party APIs (Anthropic) that you are responsible for paying for and complying with. The script's output may contain errors, hallucinated companies, or outdated information; always verify leads before outreach. The author is not liable for API costs, data quality issues, deliverability outcomes, or any damages arising from use of this script.

A generic B2B lead-gen script for MSPs. Finds qualifying companies in a target city, industry, and employee-size range using Claude Sonnet with the Anthropic web_search tool. Designed to be customized by uploading `prospector.py` to Claude and telling it what you want.

## How customization works

1. Upload `prospector.py` to [claude.ai](https://claude.ai).
2. Tell Claude what you're looking for. Example:

   > Customize this script for me. I want to find:
   > - Roofing and HVAC companies
   > - In Phoenix, AZ and surrounding towns within 30 miles
   > - With 10 to 75 employees
   > - 20 prospects per day (split evenly)

3. Claude edits the CONFIG block at the top of the file and hands it back.
4. Save the returned file, set up `.env`, install dependencies, and run.

You can re-customize anytime — just re-upload and ask Claude to change the values.

## What you can customize

The CONFIG block at the top of `prospector.py` controls:

| Field | What it is | Example |
|---|---|---|
| `PRIMARY_CITY` | Main metro you're targeting | `"Atlanta, GA"` |
| `SURROUNDING_TOWNS` | Nearby towns/suburbs to include in search | `["Marietta", "Decatur", ...]` |
| `INDUSTRIES` | List of industries, each with a count and definition | roofing, HVAC, law, healthcare, etc. |
| `MIN_EMPLOYEES` / `MAX_EMPLOYEES` | Company size range | 10–200 |

Everything below the CONFIG block (API calls, dedup, CSV writing, logging) typically stays untouched.

## Output

- `allprospects.csv` — cumulative list of every unique prospect (columns: `company_name`, `website`).
- `newprospects_MMDDYYYY.csv` — just today's adds, same columns.
- `logs/prospector_MMDDYYYY.log` — per-day log including any shortfall reasons.

Dedup is by normalized website domain (`https://www.Example.com/about` → `example.com`), so name variants don't sneak through.

## Setup (Ubuntu)

```bash
# 1. System Python + venv
sudo apt update
sudo apt install -y python3 python3-venv python3-pip

# 2. Drop the bundle files into a working directory
cd ~
mkdir prospector && cd prospector
# (copy prospector.py, requirements.txt, .env.example, .gitignore here)

# 3. Virtual environment
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 4. Configure your API key
cp .env.example .env
nano .env   # paste your real ANTHROPIC_API_KEY

# 5. (Optional) Customize the CONFIG block in prospector.py
#    — either edit it directly or upload to Claude and ask

# 6. Smoke test
python3 prospector.py
```

After a successful run you should see `allprospects.csv`, `newprospects_<today>.csv`, and `logs/prospector_<today>.log`.

## Scheduling (cron, daily at 6:00 AM)

```bash
crontab -e
```

Add (replace `YOURUSER` with your actual username, or paste the absolute path from `pwd`):

```
0 6 * * * cd /home/YOURUSER/prospector && /home/YOURUSER/prospector/.venv/bin/python prospector.py >> /home/YOURUSER/prospector/logs/cron.log 2>&1
```

The cron log catches startup errors; per-run detail is in `logs/prospector_MMDDYYYY.log`.

## Notes & caveats

- **Headcount verification is best-effort.** Web search rarely surfaces precise employee counts. The model estimates from team-page size, office count, "small business" language, and visible LinkedIn ranges, and flags each row internally as `headcount_verified: true/false`. The CSV keeps only `company_name` and `website`; unverified ones get noted in the log.
- **Industry definitions can be broad or narrow.** When Claude customizes the script, it writes a one-sentence definition for each industry you list. You can tighten or expand these by editing the `guidance` string for any industry in the CONFIG block — or by asking Claude to narrow it (e.g. "only commercial roofers, not residential").
- **The web_search tool is metered and billed separately** from token usage on the Anthropic API. Each industry call uses up to 5 searches by default. Check current pricing in the Anthropic console.
- **Rate limits.** New Anthropic accounts start on Tier 1 (30k input tokens per minute on Sonnet). The script paces calls with a 70-second gap between industries to stay under the cap. Adding $5+ in credits and waiting 7 days promotes you to Tier 2 with much higher limits.
- **If a run can't hit the target**, it logs the shortfall and the reason (API error, JSON parse failure, or — most commonly over time — too many duplicates against `allprospects.csv`). It does not pad results with companies it isn't confident about.
- **Same-day reruns** overwrite `newprospects_MMDDYYYY.csv` but only append non-duplicates to `allprospects.csv`. Safe to retry.

## Files in this bundle

| File | Purpose |
|---|---|
| `prospector.py` | The script — customize the CONFIG block at the top |
| `requirements.txt` | Python dependencies (`anthropic`, `python-dotenv`) |
| `.env.example` | Template — copy to `.env` and add your API key |
| `.gitignore` | Keeps secrets and outputs out of git |
| `README.md` | This file |
