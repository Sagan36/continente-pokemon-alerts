import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import watcher as w


def product(pid="123", stock=False):
    return {"id": pid, "name": "Pokemon cards", "price": 5.99,
            "url": f"https://www.continente.pt/produto/pokemon-{pid}.html", "in_stock": stock}


def tile(pid="123", out=False, button=True):
    data = json.dumps({"id": pid, "name": "Pok\u00e9mon cards", "price": 5.99})
    cls = "ct-product-tile-out-of-stock" if out else ""
    cart = '<button class="js-add-to-cart-tile">Add to cart</button>' if button else ''
    return f'''<div class="product-tile {cls}" data-product-tile-impression='{data}'>
    <div><a href="/produto/pokemon-{pid}.html">Cards</a></div>{cart}</div>'''


class BehaviourTests(unittest.TestCase):
    def test_baseline_is_quiet(self):
        s = w.plan_events({}, {"123": product(stock=True)})
        self.assertTrue(s["initialized"])
        self.assertEqual(s["pending"], [])

    def test_new_product_alert_even_when_unavailable(self):
        s = w.plan_events({"initialized": True}, {"123": product()})
        self.assertEqual(s["pending"][0]["kind"], "new")
        self.assertIn("currently out of stock", w.event_message(s["pending"][0])[1])

    def test_repeated_checks_do_not_duplicate_alerts(self):
        s = w.plan_events({"initialized": True}, {"123": product()})
        s = w.plan_events(s, {"123": product()})
        self.assertEqual(len(s["pending"]), 1)

    def test_restock_transition_only_once(self):
        s = w.plan_events({}, {"123": product()})
        s = w.plan_events(s, {"123": product(stock=True)})
        s = w.plan_events(s, {"123": product(stock=True)})
        self.assertEqual([e["kind"] for e in s["pending"]], ["restock"])

    def test_unknown_stock_preserves_last_known_state(self):
        s = w.plan_events({}, {"123": product()})
        s = w.plan_events(s, {"123": product(stock=None)})
        self.assertIs(s["products"]["123"]["in_stock"], False)
        self.assertEqual(s["pending"], [])
        s = w.plan_events(s, {"123": product(stock=True)})
        self.assertEqual(s["pending"][0]["kind"], "restock")

    def test_disappearing_product_is_not_sold_out(self):
        s = w.plan_events({}, {"123": product(stock=True)})
        s = w.plan_events(s, {})
        s = w.plan_events(s, {"123": product(stock=True)})
        self.assertIs(s["products"]["123"]["in_stock"], True)
        self.assertEqual(s["pending"], [])

    def test_explicit_out_of_stock_wins_over_cart_words(self):
        parser = w.ListingParser()
        parser.feed(tile(out=True, button=True))
        self.assertIs(parser.products["123"]["in_stock"], False)

    def test_missing_stock_signals_stay_unknown(self):
        parser = w.ListingParser()
        parser.feed(tile(button=False))
        self.assertIsNone(parser.products["123"]["in_stock"])

    def test_available_cart_button_and_product_links(self):
        parser = w.ListingParser()
        parser.feed(tile(button=True))
        self.assertIs(parser.products["123"]["in_stock"], True)
        self.assertTrue(parser.products["123"]["url"].endswith("pokemon-123.html"))

    def test_detail_flag_wins_over_wrong_schema_stock(self):
        data = json.dumps({"items": [{"item_id": "123", "item_name": "Pokemon", "price": 4.49}]})
        parser = w.DetailParser()
        parser.feed('<script type="application/ld+json">{"availability":"InStock"}</script>'
                    + f'''<div data-is-product-out-of-stock="true" data-product-detail-impression='{data}'></div>''')
        self.assertIs(parser.product["in_stock"], False)

    def test_pagination_follows_all_pages(self):
        first = tile() + '<div class="infinite-scroll-placeholder" data-url="/next"></div>'
        config = {"watch_urls": [], "request_delay_seconds": 0}
        with patch.object(w, "request", side_effect=[first, tile("456")]) as fetch:
            products, pages = w.scan(config)
        self.assertEqual(set(products), {"123", "456"})
        self.assertEqual(pages, 2)
        self.assertEqual(fetch.call_count, 2)

    def test_blocked_or_empty_search_fails_closed(self):
        with patch.object(w, "request", return_value="<h1>Access denied</h1>"):
            with self.assertRaises(w.MonitorError):
                w.scan({"watch_urls": []})

    def test_page_limit_rejects_incomplete_scan(self):
        first = tile() + '<div class="infinite-scroll-placeholder" data-url="/next"></div>'
        with patch.object(w, "request", return_value=first), patch.object(w.time, "sleep"):
            with self.assertRaises(w.MonitorError):
                w.scan({"watch_urls": [], "max_pages": 1})

    def test_pagination_cannot_leave_continente(self):
        first = tile() + '<div class="infinite-scroll-placeholder" data-url="https://example.com/next"></div>'
        with patch.object(w, "request", return_value=first), patch.object(w.time, "sleep"):
            with self.assertRaises(w.MonitorError):
                w.scan({"watch_urls": []})

    def test_failed_delivery_keeps_outbox_for_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            s = w.plan_events({"initialized": True}, {"123": product()})
            w.save_json(path, s)
            with patch.object(w, "notify", side_effect=OSError("offline")):
                with self.assertRaises(OSError):
                    w.drain_pending({}, s, path)
            self.assertEqual(len(w.load_json(path)["pending"]), 1)
            with patch.object(w, "notify", return_value="accepted"), patch.object(w.time, "sleep"):
                w.drain_pending({}, s, path)
            self.assertEqual(w.load_json(path)["pending"], [])

    def test_failed_scan_cannot_replace_baseline(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(w, "ROOT", Path(directory)):
            path = Path(directory) / "state.json"
            original = w.plan_events({}, {"123": product()})
            w.save_json(path, original)
            with patch.object(w, "scan", side_effect=w.MonitorError("blocked")):
                with self.assertRaises(w.MonitorError):
                    w.check_once({})
            self.assertEqual(w.load_json(path), original)

    def test_unconfirmed_restock_is_not_delivered(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(w, "ROOT", Path(directory)):
            w.save_json(Path(directory) / "state.json", w.plan_events({}, {"123": product()}))
            with patch.object(w, "scan", return_value=({"123": product(stock=True)}, 1)), \
                 patch.object(w, "detail", return_value=product(stock=False)), \
                 patch.object(w, "notify") as send, patch.object(w.time, "sleep"):
                w.check_once({})
            send.assert_not_called()

    def test_notification_outage_does_not_stop_product_discovery(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(w, "ROOT", Path(directory)):
            path = Path(directory) / "state.json"
            original = w.plan_events({"initialized": True}, {"123": product()})
            w.save_json(path, original)
            with patch.object(w, "scan", return_value=({"123": product(), "456": product("456")}, 1)), \
                 patch.object(w, "notify", side_effect=OSError("offline")):
                with self.assertRaises(w.MonitorError):
                    w.check_once({})
            saved = w.load_json(path)
            self.assertEqual(len(saved["pending"]), 2)
            self.assertIn("456", saved["products"])

    def test_sudden_partial_results_do_not_replace_baseline(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(w, "ROOT", Path(directory)):
            path = Path(directory) / "state.json"
            original = w.plan_events({}, {str(i): product(str(i)) for i in range(10)})
            w.save_json(path, original)
            with patch.object(w, "scan", return_value=({"123": product()}, 1)):
                with self.assertRaises(w.MonitorError):
                    w.check_once({})
            self.assertEqual(w.load_json(path), original)

    def test_accented_and_plain_names_match(self):
        self.assertTrue(w.pokemon("Pok\u00e9mon cards"))
        self.assertTrue(w.pokemon("Pokemon game"))
        self.assertFalse(w.pokemon("Other trading cards"))

    def test_original_state_is_not_mutated_during_planning(self):
        original = w.plan_events({}, {"123": product()})
        expected = copy.deepcopy(original)
        w.plan_events(original, {"123": product(stock=True)})
        self.assertEqual(original, expected)


if __name__ == "__main__":
    unittest.main()
