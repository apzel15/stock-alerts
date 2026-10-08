#!/usr/bin/env python3
"""Stock price alerts: notifies you when a stock crosses a target price.

Usage:
  stock_alerts.py add AAPL above 350 ["optional note"]
  stock_alerts.py add TSLA below 200
  stock_alerts.py list
  stock_alerts.py remove 3
  stock_alerts.py check          # fetch prices and fire any alerts (run on a schedule)
  stock_alerts.py price NVDA     # quick quote
  stock_alerts.py test           # send a test notification

Alerts fire once when the price crosses the target, then re-arm automatically
once the price moves back to the other side.

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

HERE = Path(__file__).resolve().parent
ALERTS_FILE = HERE / "alerts.json"
CONFIG_FILE = HERE / "config.json"
LOG_FILE = HERE / "alerts.log"


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
        git("pull", "--rebase", "--quiet")


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


def get_price(symbol):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=1d"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.load(resp)
    result = data["chart"]["result"]
    if not result:
        raise ValueError(f"no data for {symbol}")
    return float(result[0]["meta"]["regularMarketPrice"])


def notify(title, message):
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
                headers={"Title": title, "Tags": "chart_with_upwards_trend"},
            )
            urllib.request.urlopen(req, timeout=15)
        except Exception as e:
            log(f"ntfy push failed: {e}")


def cmd_add(args):
    if len(args) < 3 or args[1] not in ("above", "below"):
        sys.exit("usage: add SYMBOL above|below PRICE [note]")
    symbol, direction, target = args[0].upper(), args[1], float(args[2])
    note = " ".join(args[3:])
    current = get_price(symbol)  # also validates the symbol
    sync_pull()
    alerts = load(ALERTS_FILE, [])
    alerts.append({"symbol": symbol, "direction": direction, "target": target,
                   "note": note, "triggered": False})
    save_alerts(alerts)
    sync_push(f"Add alert: {symbol} {direction} {target}")
    print(f"Added: {symbol} {direction} ${target:,.2f}  (now ${current:,.2f})")


def cmd_list(_):
    sync_pull()
    alerts = load(ALERTS_FILE, [])
    if not alerts:
        print("No alerts. Add one with: add AAPL above 350")
        return
    for i, a in enumerate(alerts, 1):
        status = "FIRED (waiting to re-arm)" if a["triggered"] else "armed"
        note = f"  — {a['note']}" if a.get("note") else ""
        print(f"{i:>2}. {a['symbol']:<6} {a['direction']:<5} ${a['target']:>10,.2f}  [{status}]{note}")


def cmd_remove(args):
    sync_pull()
    alerts = load(ALERTS_FILE, [])
    idx = int(args[0]) - 1
    removed = alerts.pop(idx)
    save_alerts(alerts)
    sync_push(f"Remove alert: {removed['symbol']} {removed['direction']} {removed['target']}")
    print(f"Removed: {removed['symbol']} {removed['direction']} ${removed['target']:,.2f}")


def cmd_check(_):
    alerts = load(ALERTS_FILE, [])
    if not alerts:
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
        crossed = price >= a["target"] if a["direction"] == "above" else price <= a["target"]
        if crossed and not a["triggered"]:
            arrow = "▲" if a["direction"] == "above" else "▼"
            msg = f"{a['symbol']} is ${price:,.2f} ({a['direction']} your ${a['target']:,.2f} target)"
            if a.get("note"):
                msg += f" — {a['note']}"
            notify(f"{arrow} {a['symbol']} price alert", msg)
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
        print(f"{sym.upper()}: ${get_price(sym.upper()):,.2f}")


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
