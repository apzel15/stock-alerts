#!/usr/bin/env python3
"""Stock price alerts: notifies you when a stock crosses a target price.

Usage:
  stock_alerts.py add AAPL above 350 ["optional note"]
  stock_alerts.py add TSLA below 200
  stock_alerts.py add OLB every 0.05     # alert at each new 5-cent level, up or down
  stock_alerts.py list
  stock_alerts.py remove 3
  stock_alerts.py check          # fetch prices and fire any alerts (run on a schedule)
  stock_alerts.py price NVDA     # quick quote
  stock_alerts.py test           # send a test notification

Alerts fire once when the price crosses the target, then re-arm automatically
once the price moves back to the other side. "every" alerts fire each time the
price reaches a new multiple of the step (never twice in a row for the same level).

Prices: Yahoo for 4am–8pm ET (incl. pre/after-hours), Alpaca's overnight feed for
8pm–4am ET. Alpaca keys come from ALPACA_KEY_ID / ALPACA_SECRET_KEY env vars or
config.json ("alpaca_key_id", "alpaca_secret_key").

Phone push: put an ntfy.sh topic in config.json as {"ntfy_topic": "..."} (or the
NTFY_TOPIC env var) and subscribe to that topic in the ntfy app.

Cloud: GitHub Actions runs `check` on a schedule (.github/workflows/check.yml).
`add`/`remove`/`list` pull from and push to the repo so the cloud sees your changes.
"""
import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
ALERTS_FILE = HERE / "alerts.json"
CONFIG_FILE = HERE / "config.json"
LOG_FILE = HERE / "alerts.log"
ET = ZoneInfo("America/New_York")


def load(path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_alerts(alerts):
    ALERTS_FILE.write_text(json.dumps(alerts, indent=2) + "\n")


def in_cloud():
    return os.environ.get("GITHUB_ACTIONS") == "true"


def git(*args):
    return subprocess.run(["git", "-C", str(HERE), *args], capture_output=True, text=True)


def sync_pull():
    """Grab the latest alerts (incl. fired/re-armed state written by the cloud job)."""
    if not in_cloud() and (HERE / ".git").exists():
        git("pull", "--rebase", "--autostash", "--quiet")


def sync_push(message):
    if in_cloud() or not (HERE / ".git").exists():
        return
    git("add", "alerts.json")
    git("commit", "--quiet", "-m", message)
    r = git("push", "--quiet")
    if r.returncode != 0:
        print(f"Warning: couldn't push to GitHub, the cloud won't see this change yet.\n{r.stderr}")


def log(msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    print(line)
    with LOG_FILE.open("a") as f:
        f.write(line + "\n")


def session(now=None):
    """Which US trading session is open right now: 'day' (4am–8pm ET, Mon–Fri),
    'overnight' (8pm–4am ET, Sun night through Thu night), or None (weekend)."""
    if os.environ.get("FORCE_SESSION") in ("day", "overnight"):  # for manual test runs
        return os.environ["FORCE_SESSION"]
    now = now or datetime.now(ET)
    wd, hour = now.weekday(), now.hour  # Mon=0 … Sun=6
    if wd < 5 and 4 <= hour < 20:
        return "day"
    if (hour >= 20 and wd in (6, 0, 1, 2, 3)) or (hour < 4 and wd in (0, 1, 2, 3, 4)):
        return "overnight"
    return None


def yahoo_price(symbol):
    """Latest consolidated price, including pre-market and after-hours (4am–8pm ET)."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=1d"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.load(resp)
    result = data["chart"]["result"]
    if not result:
        raise ValueError(f"no data for {symbol}")
    meta = result[0]["meta"]
    return float(meta.get("fulldayPrice") or meta["regularMarketPrice"])


def alpaca_keys():
    config = load(CONFIG_FILE, {})
    key = os.environ.get("ALPACA_KEY_ID") or config.get("alpaca_key_id")
    secret = os.environ.get("ALPACA_SECRET_KEY") or config.get("alpaca_secret_key")
    return (key, secret) if key and secret else None


def alpaca_overnight_price(symbol, keys):
    """Latest 1-min bar from Alpaca's free overnight feed (Blue Ocean, 8pm–4am ET)."""
    url = f"https://data.alpaca.markets/v2/stocks/bars/latest?symbols={symbol}&feed=overnight"
    req = urllib.request.Request(url, headers={
        "APCA-API-KEY-ID": keys[0], "APCA-API-SECRET-KEY": keys[1]})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.load(resp)
    bar = data.get("bars", {}).get(symbol)
    if not bar:
        raise ValueError(f"no overnight data for {symbol}")
    return float(bar["c"])


def get_price(symbol):
    if session() == "overnight":
        keys = alpaca_keys()
        if keys:
            return alpaca_overnight_price(symbol, keys)
        log("no Alpaca keys set; using Yahoo (no overnight prices)")
    return yahoo_price(symbol)


def notify(title, message, tag="chart_with_upwards_trend"):
    config = load(CONFIG_FILE, {})
    if sys.platform == "darwin" and config.get("mac_notifications", True):
        script = ('on run argv\n'
                  'display notification (item 2 of argv) with title (item 1 of argv) sound name "Glass"\n'
                  'end run')
        subprocess.run(["osascript", "-e", script, title, message], check=False)

    topic = os.environ.get("NTFY_TOPIC") or config.get("ntfy_topic")
    if topic:
        try:
            req = urllib.request.Request(
                f"https://ntfy.sh/{topic}",
                data=message.encode(),
                headers={"Title": title, "Tags": tag, "Priority": "high"},
            )
            urllib.request.urlopen(req, timeout=15)
        except Exception as e:
            log(f"ntfy push failed: {e}")


def cmd_add(args):
    if len(args) < 3 or args[1] not in ("above", "below", "every"):
        sys.exit("usage: add SYMBOL above|below|every PRICE [note]")
    symbol, direction, target = args[0].upper(), args[1], float(args[2])
    note = " ".join(args[3:])
    current = get_price(symbol)  # also validates the symbol
    sync_pull()
    alerts = load(ALERTS_FILE, [])
    alert = {"symbol": symbol, "direction": direction, "target": target, "note": note}
    if direction == "every":
        alert["last"] = current  # levels are measured from here
    else:
        alert["triggered"] = False
    alerts.append(alert)
    save_alerts(alerts)
    sync_push(f"Add alert: {symbol} {direction} {target}")
    print(f"Added: {symbol} {direction} {fmt(target)}  (now {fmt(current)})")


def cmd_list(_):
    sync_pull()
    alerts = load(ALERTS_FILE, [])
    if not alerts:
        print("No alerts. Add one with: add AAPL above 350")
        return
    for i, a in enumerate(alerts, 1):
        if a["direction"] == "every":
            status = f"last alert at {fmt(a['last'])}"
        else:
            status = "FIRED (waiting to re-arm)" if a["triggered"] else "armed"
        note = f"  — {a['note']}" if a.get("note") else ""
        print(f"{i:>2}. {a['symbol']:<6} {a['direction']:<5} {fmt(a['target']):>11}  [{status}]{note}")


def cmd_remove(args):
    sync_pull()
    alerts = load(ALERTS_FILE, [])
    idx = int(args[0]) - 1
    removed = alerts.pop(idx)
    save_alerts(alerts)
    sync_push(f"Remove alert: {removed['symbol']} {removed['direction']} {removed['target']}")
    print(f"Removed: {removed['symbol']} {removed['direction']} ${removed['target']:,.2f}")


def fmt(price):
    """$1,234.56 normally; 4 decimals for sub-dollar stocks."""
    if price >= 1:
        return f"${price:,.2f}"
    s = f"{price:.4f}".rstrip("0")
    return "$" + s + "0" * max(0, 2 - len(s.split(".")[1]))


def check_step(a, price):
    """Alert when price reaches a step level other than the last one alerted.
    Works in ten-thousandths of a dollar to avoid float rounding at the boundaries."""
    step, p, last = (round(x * 10000) for x in (a["target"], price, a["last"]))
    up_level = p // step * step        # highest level at or below price
    down_level = -(-p // step) * step  # lowest level at or above price
    if up_level > last:
        level, word, tag = up_level, "up to", "chart_with_upwards_trend"
    elif down_level < last:
        level, word, tag = down_level, "down to", "chart_with_downwards_trend"
    else:
        return False
    level /= 10000
    msg = f"{a['symbol']} is {fmt(price)} (last alert was {fmt(a['last'])})"
    if a.get("note"):
        msg += f" — {a['note']}"
    notify(f"{a['symbol']} {word} {fmt(level)}", msg, tag)
    log(f"FIRED: {msg}")
    a["last"] = level
    return True


def cmd_check(_):
    alerts = load(ALERTS_FILE, [])
    if not alerts or session() is None:
        return
    prices = {}
    for sym in {a["symbol"] for a in alerts}:
        try:
            prices[sym] = get_price(sym)
        except Exception as e:
            log(f"could not fetch {sym}: {e}")

    changed = False
    for a in alerts:
        price = prices.get(a["symbol"])
        if price is None:
            continue
        if a["direction"] == "every":
            changed |= check_step(a, price)
            continue
        crossed = price >= a["target"] if a["direction"] == "above" else price <= a["target"]
        if crossed and not a["triggered"]:
            tag = "chart_with_upwards_trend" if a["direction"] == "above" else "chart_with_downwards_trend"
            msg = f"{a['symbol']} is {fmt(price)} ({a['direction']} your {fmt(a['target'])} target)"
            if a.get("note"):
                msg += f" — {a['note']}"
            notify(f"{a['symbol']} price alert", msg, tag)
            log(f"FIRED: {msg}")
            a["triggered"] = True
            changed = True
        elif not crossed and a["triggered"]:
            a["triggered"] = False  # price came back; re-arm
            log(f"re-armed: {a['symbol']} {a['direction']} {a['target']} (now {price})")
            changed = True
    if changed:
        save_alerts(alerts)


def cmd_price(args):
    for sym in args:
        print(f"{sym.upper()}: ${get_price(sym.upper()):,.2f}  ({session() or 'closed'} session)")


def cmd_test(_):
    notify("Stock alerts", "Test notification — alerts are working.")
    print("Sent test notification.")


COMMANDS = {"add": cmd_add, "list": cmd_list, "remove": cmd_remove,
            "check": cmd_check, "price": cmd_price, "test": cmd_test}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(1)
    COMMANDS[sys.argv[1]](sys.argv[2:])
