"""Continente Pokemon alerts. Python 3.11+, no third-party packages."""
from __future__ import annotations

import argparse
import copy
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

ROOT = Path(__file__).resolve().parent
SEARCH = "https://www.continente.pt/pesquisa/?q=pokemon"
SEED = "https://www.continente.pt/produto/pokemon---cartas-tcg-sleeved-booster-delta-reign-9041780.html"
LOG = logging.getLogger("pokemon")


class MonitorError(Exception):
    pass


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def pokemon(text):
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return "pokemon" in plain


def valid_product_url(url):
    u = urllib.parse.urlsplit(urllib.parse.urljoin(SEARCH, url))
    if u.scheme != "https" or u.netloc != "www.continente.pt" or not u.path.startswith("/produto/"):
        raise MonitorError("Unexpected product URL")
    return urllib.parse.urlunsplit((u.scheme, u.netloc, u.path, "", ""))


class ListingParser(HTMLParser):
    """Read real product tiles; ignore navigation, recommendations and hidden text."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.products = {}
        self.next_url = None
        self.current = None
        self.depth = 0
        self.stock_button = False
        self.errors = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        classes = set(a.get("class", "").split())
        if "infinite-scroll-placeholder" in classes and a.get("data-url"):
            self.next_url = a["data-url"]
        if self.current is not None and tag == "div":
            self.depth += 1
        if "data-product-tile-impression" in a:
            if self.current is not None:
                self.errors.append("Overlapping product tiles")
                return
            try:
                p = json.loads(a["data-product-tile-impression"])
                self.current = {"id": str(p["id"]), "name": p["name"],
                                "price": p.get("price"), "url": "",
                                "in_stock": False if "ct-product-tile-out-of-stock" in classes else None}
                self.depth = 1
                self.stock_button = False
            except (ValueError, KeyError, TypeError):
                self.errors.append("Invalid product tile data")
        if self.current is not None:
            if tag == "a" and "/produto/" in a.get("href", ""):
                try:
                    url = valid_product_url(a["href"])
                    if url.endswith("-" + self.current["id"] + ".html"):
                        self.current["url"] = url
                except MonitorError:
                    self.errors.append("Invalid product link")
            if tag == "button" and "js-add-to-cart-tile" in classes:
                if "disabled" not in a and "d-none" not in classes and a.get("aria-disabled") != "true":
                    self.stock_button = True

    def handle_endtag(self, tag):
        if self.current is not None and tag == "div":
            self.depth -= 1
            if self.depth == 0:
                p = self.current
                if p["in_stock"] is None and self.stock_button:
                    p["in_stock"] = True
                if not p["url"]:
                    self.errors.append("Product tile is missing its link")
                else:
                    self.products[p["id"]] = p
                self.current = None


class DetailParser(HTMLParser):
    """Prefer explicit storefront flags. Continente's JSON-LD can say InStock incorrectly."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.product = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if "data-product-detail-impression" not in a:
            return
        try:
            item = json.loads(a["data-product-detail-impression"])["items"][0]
            flag = a.get("data-is-product-out-of-stock")
            self.product = {"id": str(item["item_id"]), "name": item["item_name"],
                            "price": item.get("price"),
                            "in_stock": {"true": False, "false": True}.get(flag)}
        except (ValueError, KeyError, IndexError, TypeError):
            pass


def request(url, *, payload=None):
    # Windows' bundled curl uses its native certificate trust chain. Some
    # Python Store builds reject an otherwise valid ntfy certificate chain.
    if payload is not None and os.name == "nt":
        curl = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "curl.exe"
        if curl.is_file():
            result = subprocess.run(
                [str(curl), "--silent", "--show-error", "--fail-with-body", "--max-time", "35",
                 "--proto", "=https", "--header", "Content-Type: application/json; charset=utf-8",
                 "--data-binary", "@-", url],
                input=payload, capture_output=True, timeout=40,
                creationflags=subprocess.CREATE_NO_WINDOW)
            if result.returncode:
                raise MonitorError(f"Notification request failed (HTTPS client exit {result.returncode})")
            return result.stdout.decode("utf-8")
    headers = {"User-Agent": "ContinentePokemonWatcher/1.0 (personal stock notifications)",
               "Accept-Language": "pt-PT,pt;q=0.9,en;q=0.7"}
    if payload is not None:
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(url, data=payload, headers=headers)
    # No retries inside a scan: failures back off at the monitor level.
    with urllib.request.urlopen(req, timeout=35) as response:
        if payload is None and urllib.parse.urlsplit(response.url).netloc != "www.continente.pt":
            raise MonitorError("Continente redirected to an unexpected website")
        data = response.read(8_000_001)
        if len(data) > 8_000_000:
            raise MonitorError("Response is too large")
        return data.decode("utf-8")


def detail(url):
    url = valid_product_url(url)
    parser = DetailParser()
    parser.feed(request(url))
    p = parser.product
    if not p or not url.endswith("-" + p["id"] + ".html"):
        raise MonitorError("Product detail format changed or the page was blocked")
    p["url"] = url
    return p


def scan(config):
    products = {}
    visited = set()
    url = SEARCH
    for page in range(config.get("max_pages", 30)):
        if url in visited:
            raise MonitorError("Pagination repeated a page")
        visited.add(url)
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "https" or parts.netloc != "www.continente.pt":
            raise MonitorError("Unexpected pagination host")
        parser = ListingParser()
        parser.feed(request(url))
        if parser.errors or parser.current is not None or not parser.products:
            raise MonitorError("Search layout changed, empty page, or website blocked the request")
        fresh = set(parser.products) - set(products)
        if not fresh:
            raise MonitorError("Pagination returned no new products")
        products.update(parser.products)
        if not parser.next_url:
            break
        url = urllib.parse.urljoin(url, parser.next_url)
        time.sleep(config.get("request_delay_seconds", 1.5))
    else:
        raise MonitorError("Page limit reached; refusing to save an incomplete scan")
    products = {pid: p for pid, p in products.items() if pokemon(p["name"])}
    # Always check the supplied booster, even if search removes it temporarily.
    for seed_url in config.get("watch_urls", [SEED]):
        time.sleep(config.get("request_delay_seconds", 1.5))
        p = detail(seed_url)
        products[p["id"]] = p
    if not products:
        raise MonitorError("No Pokemon products found")
    return products, len(visited)


def load_json(path, default=None):
    if not path.exists():
        return copy.deepcopy(default)
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (ValueError, OSError) as exc:
        raise MonitorError(f"Cannot read {path.name}; restore it before continuing") from exc


def save_json(path, value):
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(temp, path)


def init_config():
    path = ROOT / "config.json"
    if not path.exists():
        save_json(path, {"interval_seconds": 300, "request_delay_seconds": 1.5,
                         "max_pages": 30, "ntfy_server": "https://ntfy.sh",
                         "ntfy_topic": "continente-pokemon-" + secrets.token_hex(16),
                         "watch_urls": [SEED]})
    return load_config()


def load_config():
    c = load_json(ROOT / "config.json")
    if not isinstance(c, dict):
        raise MonitorError("Run --init first")
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,120}", c.get("ntfy_topic", "")):
        raise MonitorError("Invalid notification topic")
    u = urllib.parse.urlsplit(c.get("ntfy_server", ""))
    if u.scheme != "https" or not u.netloc or u.path not in ("", "/") or u.query or u.fragment:
        raise MonitorError("ntfy_server must be an HTTPS server URL")
    if not isinstance(c.get("interval_seconds"), int) or c["interval_seconds"] < 300:
        raise MonitorError("interval_seconds must be at least 300")
    return c


def notify(config, title, message, url=SEARCH, priority=3):
    data = {"topic": config["ntfy_topic"], "title": title,
            "message": message, "click": url, "priority": priority,
            "tags": ["shopping_cart"]}
    result = json.loads(request(config["ntfy_server"].rstrip("/") + "/",
                                payload=json.dumps(data).encode("utf-8")))
    if not result.get("id") or result.get("event") != "message":
        raise MonitorError("Notification service did not acknowledge the message")
    return result["id"]


def plan_events(state, products):
    """Update last-known stock only on known values; absence never means sold out."""
    updated = copy.deepcopy(state)
    known = updated.setdefault("products", {})
    pending = updated.setdefault("pending", [])
    stamp = now()
    baseline = not state.get("initialized", False)
    for pid, current in products.items():
        old = known.get(pid)
        reason = None
        if not baseline:
            if old is None:
                reason = "new"
            elif old.get("in_stock") is False and current.get("in_stock") is True:
                reason = "restock"
        record = dict(current)
        if record.get("in_stock") is None and old:
            record["in_stock"] = old.get("in_stock")
        record["first_seen"] = old.get("first_seen", stamp) if old else stamp
        record["last_seen"] = stamp
        known[pid] = record
        if reason:
            pending.append({"event_id": secrets.token_hex(12), "kind": reason,
                            "product": dict(current), "detected_at": stamp})
    updated["initialized"] = True
    updated["last_success"] = stamp
    updated["last_count"] = len(products)
    return updated


def event_message(event):
    p = event["product"]
    title = "Continente: back in stock!" if event["kind"] == "restock" else "Continente: new Pokemon product"
    stock = {True: "Listed as in stock", False: "Listed, currently out of stock", None: "Stock not confirmed"}[p.get("in_stock")]
    price = f"EUR {float(p['price']):.2f}" if p.get("price") is not None else "Price unavailable"
    message = f"{p['name']}\n{price} | {stock}\n{p['url']}\nDetected: {event['detected_at']}\nAvailability may vary by delivery address."
    return title, message, p["url"]


def drain_pending(config, state, state_path):
    day = time.strftime("%Y-%m-%d", time.gmtime())
    if state.get("delivery_day") != day:
        state["delivery_day"], state["deliveries_today"] = day, 0
    # Stay below the public service's daily allowance. Extra messages remain queued.
    for _ in range(min(20, len(state.get("pending", [])))):
        if state.get("deliveries_today", 0) >= 190:
            break
        event = state["pending"][0]
        title, message, url = event_message(event)
        notify(config, title, message, url, priority=4 if event["kind"] == "restock" else 3)
        LOG.info("Alert delivered: %s %s", event["kind"], event["product"]["id"])
        state["pending"].pop(0)
        state["deliveries_today"] = state.get("deliveries_today", 0) + 1
        save_json(state_path, state)
        time.sleep(1)


def check_once(config):
    path = ROOT / "state.json"
    old = load_json(path, {"products": {}, "pending": [], "initialized": False})
    # Retry already committed notifications even when the shop is unavailable.
    delivery_error = None
    if old.get("pending"):
        try:
            drain_pending(config, old, path)
        except Exception as exc:
            delivery_error = exc
            LOG.warning("Notification delivery failed; continuing product scan: %s", error_summary(exc))
    products, pages = scan(config)
    if len(products) < old.get("last_count", 0) * 0.5:
        raise MonitorError("Product count unexpectedly fell by more than half; keeping previous baseline")
    # A false -> true transition must agree with the actual product page.
    for pid, p in list(products.items()):
        previous = old.get("products", {}).get(pid)
        if previous and previous.get("in_stock") is False and p.get("in_stock") is True:
            time.sleep(config.get("request_delay_seconds", 1.5))
            products[pid] = detail(p["url"])
    state = plan_events(old, products)
    state["last_pages"] = pages
    # Commit changes and outbox together, before attempting network delivery.
    save_json(path, state)
    LOG.info("Scan complete: %s Pokemon products, %s pages, %s in stock, %s queued alerts",
             len(products), pages, sum(p["in_stock"] is True for p in products.values()), len(state["pending"]))
    if delivery_error is not None:
        raise MonitorError("Product scan saved; notification delivery is unavailable and alerts remain queued") from delivery_error
    drain_pending(config, state, path)
    return state


def error_summary(exc):
    # Avoid writing notification topic URLs or future credentials to logs.
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}"
    if isinstance(exc, urllib.error.URLError):
        return "Network connection failed"
    if isinstance(exc, MonitorError):
        return str(exc)
    return type(exc).__name__


class SingleInstance:
    def __enter__(self):
        self.file = (ROOT / "watcher.lock").open("a+b")
        if self.file.tell() == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            raise MonitorError("Watcher is already running") from exc
        return self

    def __exit__(self, *args):
        self.file.close()


def run_loop(config):
    with SingleInstance():
        save_json(ROOT / "process.json", {"pid": os.getpid(), "started_at": now()})
        LOG.info("Watcher started; interval %s seconds", config["interval_seconds"])
        failures = 0
        last_error_notice = 0
        while True:
            try:
                state = check_once(config)
                failures = 0
                save_json(ROOT / "health.json", {"running": True, "last_success": state["last_success"],
                                                 "products": state["last_count"], "consecutive_errors": 0})
            except Exception as exc:
                failures += 1
                summary = error_summary(exc)
                LOG.error("Check failed (%s): %s", failures, summary)
                save_json(ROOT / "health.json", {"running": True, "last_error_at": now(),
                                                 "error": summary, "consecutive_errors": failures})
                if failures >= 3 and time.time() - last_error_notice > 21600:
                    try:
                        notify(config, "Pokemon watcher needs attention", "Continente checks are failing: " + summary + ". The watcher will keep retrying. Check watcher.log on your PC.")
                        last_error_notice = time.time()
                    except Exception:
                        pass
            time.sleep(min(config["interval_seconds"] * (2 ** min(max(failures - 1, 0), 4)), 3600))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["init", "once", "loop", "test", "status", "list"])
    args = parser.parse_args()
    handler = RotatingFileHandler(ROOT / "watcher.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOG.addHandler(handler)
    LOG.setLevel(logging.INFO)
    if sys.stdout:
        LOG.addHandler(logging.StreamHandler(sys.stdout))
    try:
        config = init_config() if args.action == "init" else load_config()
        if args.action == "init":
            print("Subscribe: " + config["ntfy_server"].rstrip("/") + "/" + config["ntfy_topic"])
        elif args.action == "loop":
            run_loop(config)
        elif args.action == "once":
            with SingleInstance():
                check_once(config)
        elif args.action == "test":
            state = load_json(ROOT / "state.json", {})
            count = state.get("last_count", 0)
            msgid = notify(config, "Pokemon alerts are connected", f"Test notification: the delivery service accepted this message. Watching {count} Pokemon products on Continente every 5 minutes while your PC is awake. New listings and confirmed restocks will appear here. Tap to browse.")
            print("Notification service accepted test message: " + msgid)
        elif args.action == "status":
            state = load_json(ROOT / "state.json", {})
            print(json.dumps({"last_success": state.get("last_success"), "products": state.get("last_count", 0),
                              "pending_alerts": len(state.get("pending", [])),
                              "health": load_json(ROOT / "health.json", {})}, indent=2))
        elif args.action == "list":
            state = load_json(ROOT / "state.json", {})
            for p in sorted(state.get("products", {}).values(), key=lambda x: x["name"]):
                print(("IN STOCK" if p["in_stock"] is True else "OUT" if p["in_stock"] is False else "UNKNOWN") + " | " + p["name"] + " | " + p["url"])
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        LOG.error("Stopped: %s", error_summary(exc))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
