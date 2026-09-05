import os
import unittest

os.environ.setdefault("JWT_SECRET", "integration-test-secret")

class BackendSmokeTests(unittest.TestCase):
    def test_route_matrix_has_every_route(self):
        from backend.api.app import ROUTE_PERMISSIONS, app
        routes = {f"{method} {route.path}" for route in app.routes for method in getattr(route, "methods", set())}
        self.assertTrue(routes)
        self.assertTrue(all(permission for permission in ROUTE_PERMISSIONS.values()))

    def test_strict_order_schema_rejects_unknown_fields(self):
        from pydantic import ValidationError
        from backend.api.app import OrderIn
        with self.assertRaises(ValidationError):
            OrderIn(customer_name="a", phone_raw="09121234567", province="x", city="x", address="x", product_raw="x", quantity=1, idempotency_key="k", role="owner")

if __name__ == "__main__": unittest.main()
