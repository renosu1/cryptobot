# Trend Rider

A crypto and stock-market dashboard with a swing-trading bot (two paper accounts: Crypto and Stocks). This repository runs it **on GitHub's servers**, so it keeps going when your computer is off.

- **Dashboard:** the GitHub Pages site for this repository (Settings → Pages shows the link).
- **The bot** runs four times a day (`.github/workflows/bot.yml`). The daily decision happens on the first run after 00:00 UTC. Results are saved in `data/`.
- **Settings:** edit [`data/config.json`](data/config.json) and commit (the stocks account uses its `"stocks"` section; its `"park"` setting is where idle money waits: `"GLD"` gold fund, the default, or `"cash"`). Run it right away from the **Actions** tab → *Trend Rider bot* → *Run workflow*.
- **Paper money by default.** Real-money trading (Kraken or Binance.US) is off unless you add the exchange's secrets (`KRAKEN_API_KEY`/`KRAKEN_API_SECRET` or `BINANCEUS_API_KEY`/`BINANCEUS_API_SECRET`) *and* set `"enabled": true, "confirm": "REAL MONEY", "exchange": "kraken"` or `"binanceus"` under `"live"` in the config. Orders go in as limit orders first (Binance.US charges 0% for those). This repository is public, so real trades would be visible to anyone.

How the strategies were built and tested: the dashboard's *How it was tested* tab, and the `research/` folder in the Mac version.

Not financial advice. No strategy can promise a profit; in testing on 2023–2026 prices it made less than simply holding, while falling less in crashes.
