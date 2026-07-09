# NSE Pre-Market Stock Scanner

A Python tool that fetches live data from the NSE India portal and recommends **stocks to buy before 9:15 AM** using proven trading theories.

---

## Trading Theories & Signals Used

| Theory | Implementation |
|---|---|
| **Demand & Supply** | Swing-pivot zone detection with touch-count, freshness & width scoring |
| **RSI** | Oversold + recovering momentum (< 35 and rising) |
| **MACD** | Bullish crossover + positive histogram expansion |
| **EMA alignment** | Short (9) > Mid (21) > Long (50) = bullish stack |
| **Supertrend** | ATR-based trend direction filter |
| **Bollinger Bands** | Lower-band bounce = oversold reversal setup |
| **VWAP** | Price above VWAP = intraday strength |
| **Volume surge** | Today's vol > 1.5× 20-day avg = conviction |
| **Pivot support** | S1/S2 classical pivot camarilla points |
| **Gap analysis** | Pre-market gap-up confirms overnight interest |
| **Delivery %** | High delivery = institutional, not speculative |
| **ADX** | Trend strength filter (avoids choppy sideways markets) |
| **Stochastic** | Additional overbought/oversold confirmation |
| **Candlestick patterns** | Hammer, Bullish Engulfing, Doji, Shooting Star |
| **PCR (Put-Call Ratio)** | Options market sentiment (> 1.0 = bullish) |

---

## Project Structure

```
nse_stock_scanner/
├── main.py               # Entry point  (run this)
├── config.py             # All parameters & watchlist
├── requirements.txt
├── src/
│   ├── nse_fetcher.py    # NSE portal + yfinance fallback
│   ├── indicators.py     # RSI, MACD, BB, EMA, Supertrend, VWAP, ATR …
│   ├── demand_supply.py  # Demand/Supply zone engine
│   ├── signals.py        # Scoring engine (combines all signals)
│   ├── scanner.py        # Parallel pre-market scanner
│   └── report.py         # Console / CSV / JSON output
├── logs/                 # Log files & OHLCV cache
└── output/               # buy_signals.csv  +  buy_signals.json
```

---

## Quick Start

### 1. Install dependencies

```bash
cd ~/nse_stock_scanner
pip install -r requirements.txt
```

### 2. Run a full scan (all NIFTY50 + F&O stocks)

```bash
python main.py
```

### 3. Other usage options

```bash
# Scan only NIFTY 50
python main.py --watchlist NIFTY50

# Get top 5 picks
python main.py --top 5

# Analyse a single stock
python main.py --symbol RELIANCE

# Plain text (no colours — pipe-friendly)
python main.py --plain

# Auto-run at 9:00 AM every weekday (scheduler mode)
python main.py --schedule

# Don't save CSV/JSON
python main.py --no-save

# Verbose debug output
python main.py --log-level DEBUG

# Sync to Google Sheet (create one worksheet tab per day)
python main.py --sync-gsheet --gsheet-key <YOUR_SHEET_KEY> --gsheet-creds <service_account.json>

# Daily auto-run + Google Sheet sync at 9:00 AM (weekdays)
python main.py --schedule --gsheet-key <YOUR_SHEET_KEY> --gsheet-creds <service_account.json>
```

### Google Sheet setup

1. Create a Google Cloud service account and download the JSON credentials file.
2. Share your target Google Sheet with the service-account email (Editor access).
3. Use either `--gsheet-key` (preferred) or `--gsheet-url` with `--gsheet-creds`.
4. Every run creates a new worksheet tab like `PRE_MARKET_YYYY-MM-DD`.
   Legacy tabs named `PRE915_YYYY-MM-DD` are still supported by the follow-up scripts.

---

## Output Example

```
══════════════════════════════════════════════════════════════════════
  NSE PRE-MARKET BUY SCANNER  |  2026-03-31 09:00:14
══════════════════════════════════════════════════════════════════════

  NIFTY PCR 1.23 (BULLISH)  |  Scanned: 85  Qualified: 12  Skipped: 0

  TOP 10 BUY CANDIDATES BEFORE 9:15 AM

  #   SYMBOL          SCORE    PRICE    RSI   MACD_H  SUPTRND  VOL_R    ENTRY       SL      TGT    R:R
  ─────────────────────────────────────────────────────────────────────────────────────────────────────
  1   RELIANCE           87  2452.30   38.2  +0.0043    ↑BULL   2.3x  2452.30  2420.10  2540.00  2.8x
  2   HDFCBANK           74  1623.50   41.5  +0.0028    ↑BULL   1.7x  1623.50  1598.20  1685.00  2.4x
  ...

  DETAILED ANALYSIS

  RELIANCE  [87 pts]
    Price: 2452.30  Gap: +0.82%  Pattern: bullish_engulfing
    Entry: 2452.30  SL: 2420.10  Target: 2540.00  R:R = 2.8x
    Demand Zone: 2395.00–2420.00  (strength=70, fresh=True, 1.3% away)
    Supply Zone: 2535.00–2560.00  (3.4% away)
    Bullish signals:
      +15 RSI oversold (38.2)
      +15 MACD bullish crossover
      +15 Near demand zone (1.3% away)
      +10 Volume surge (2.3x avg)
      ...
```

---

## Configuration (`config.py`)

Key parameters you can tune:

| Parameter | Default | Effect |
|---|---|---|
| `RSI_OVERSOLD` | 35 | Lower = stricter oversold filter |
| `MIN_SCORE_TO_BUY` | 45 | Higher = fewer, higher-quality signals |
| `TOP_N_STOCKS` | 10 | Number of final recommendations |
| `DS_PROXIMITY_PCT` | 1.0 | How close to demand zone triggers signal |
| `SL_ATR_MULT` | 1.5 | Stop-loss as multiple of ATR |
| `TARGET_RR` | 2.0 | Minimum reward:risk ratio required |
| `VOLUME_SURGE_MULT` | 1.5 | Volume confirmation threshold |

---

## Data Sources

| Source | Used for |
|---|---|
| **NSE India API** | Historical OHLCV, quotes, options chain, delivery data, FII/DII |
| **yfinance** | Automatic fallback when NSE API is unavailable/rate-limited |

NSE data is cached locally (`logs/cache/`) for up to 1 hour (daily) or 5 minutes (intraday) to avoid hammering the portal.

---

## Risk Disclaimer

> This tool is for **educational and research purposes only**.  
> Stock trading involves substantial risk of loss. Past performance of any signal or strategy is not indicative of future results.  
> Always do your own research and consult a SEBI-registered advisor before investing.

---

## Web & Mobile

- **Web**: Run `python main.py` on any machine; output is viewable in any terminal.
- **Mobile**: Use `python main.py --plain` and pipe to a text file, or run on a VPS/server and access via SSH.
- **Automation**: Use `--schedule` mode on a headless server to get results auto-generated at 9:00 AM daily. The JSON output (`output/buy_signals.json`) can be consumed by any mobile app or Telegram bot.

---

## Free Daily Automation (Recommended)

Use GitHub Actions (free tier) to run this scanner automatically every weekday at 9:00 AM IST.

Workflow file included:
- `.github/workflows/daily-scan.yml`

### 1. Push this project to GitHub

The workflow runs only after the repository is on GitHub.

### 2. Add repository secrets

In GitHub repo settings:
`Settings -> Secrets and variables -> Actions -> New repository secret`

Add these secrets:

- `GOOGLE_SHEET_URL`: Full Google Sheet URL
- `GOOGLE_APPLICATION_CREDENTIALS_JSON`: Full JSON content of the service-account key file

### 3. Enable Actions

Go to the `Actions` tab and allow workflows if prompted.

### 4. Automatic runs

- Automatically runs on every push to `main`.
- Automatically runs Mon-Fri at `03:30 UTC` (`09:00 IST`) for morning scan.
- Automatically runs Mon-Fri at `11:30 UTC` (`17:00 IST`) for end-of-day follow-up.

No manual workflow run is required.

### 5. Scheduled runs

The workflow runs automatically Mon-Fri at:
- `03:30 UTC` (`09:00 IST`): writes `PRE_MARKET_YYYY-MM-DD` tab.
  Legacy tabs named `PRE915_YYYY-MM-DD` are also supported.
- `11:30 UTC` (`17:00 IST`): reads morning tab and writes:
  - `EOD_NEXTDAY_YYYY-MM-DD` (EOD performance + next-day recommendation in one tab)

### Security note

- Local secrets are ignored via `.gitignore`.
- Do not commit `.env` or any service-account JSON key file.
