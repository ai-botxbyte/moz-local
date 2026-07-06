# moz-local

Test-mode Moz metrics checker. Mirrors the `ahref-local` architecture but
targets Moz's free domain-analysis pages, runs a **headed** (visible)
ungoogled-chromium browser, and is fully decoupled from production — it pulls
domains from and posts results to a local **mock server**, never the
`articleinnovator.com` management-service API.

For each domain it opens:

```
https://moz.com/domain-analysis?site=<domain>
```

and scrapes the server-rendered metrics: **Domain Authority**, **Page
Authority** (derived from top pages), **Linking Root Domains**, **Spam Score**,
plus bonus data (ranking keywords, top pages, top linking domains).

## Layout

```
moz-local/
├── moz_checker.py       # the checker (workers, browser, scrape, pull/post, buffer)
├── moz.json             # JSON spec with the DOM-scraping `evaluate` action
├── mock_server.py       # stdlib mock server (serves domains, persists results)
├── run.sh               # launcher (starts mock server + headed checker)
├── requirements.txt
├── test_domains.txt     # domains the mock server serves
├── tools/
│   ├── setup_vendor.sh        # vendor ungoogled-chromium into vendor/
│   └── open_chrome_profile.sh # open a profile for inspection/customization
└── tests/               # pytest + Hypothesis property tests
```

## Quick start

```bash
cd moz-local
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# (Linux) vendor ungoogled-chromium. On macOS this is skipped and the checker
# falls back to system Google Chrome / Chromium.
bash tools/setup_vendor.sh

# Launch: prompts for worker count, starts the mock server, runs headed.
bash run.sh
```

Results are appended to `results/moz_results.jsonl`.

## Running the checker directly

```bash
# In one terminal:
.venv/bin/python mock_server.py --port 8000 --domains test_domains.txt

# In another:
.venv/bin/python moz_checker.py --api-url http://127.0.0.1:8000 --workers 1 --no-proxy
# add --headless to hide the window; default is headed.
```

## Tests

```bash
.venv/bin/python -m pytest tests/ -q
```
