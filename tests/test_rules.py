import unittest

from app.rules import requested_qty, same_request

OLD = [{"id": 1, "q": 2, "name": "a", "price": 50}, {"id": 2, "q": 1, "name": "b", "price": 30}]


class IdempotencyMatching(unittest.TestCase):
    def test_duplicate_lines_are_merged(self):
        self.assertEqual(requested_qty([{"id": 1, "q": 1}, {"id": 1, "q": 2}]), {1: 3})

    def test_same_bill_matches(self):
        self.assertTrue(same_request(OLD, "Cash", {"Cash": 130}, {1: 2, 2: 1}, "Cash", None, None))

    def test_changed_quantity_product_or_method_does_not_match(self):
        self.assertFalse(same_request(OLD, "Cash", None, {1: 3, 2: 1}, "Cash", None, None))
        self.assertFalse(same_request(OLD, "Cash", None, {1: 2}, "Cash", None, None))
        self.assertFalse(same_request(OLD, "Cash", None, {1: 2, 2: 1}, "UPI", None, None))

    def test_split_amounts_must_match(self):
        pays = {"Cash": 100, "UPI": 30}
        self.assertTrue(same_request(OLD, "Split", pays, {1: 2, 2: 1}, "Split", 100, 30))
        self.assertFalse(same_request(OLD, "Split", pays, {1: 2, 2: 1}, "Split", 90, 40))


if __name__ == "__main__":
    unittest.main()
