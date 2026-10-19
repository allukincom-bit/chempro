"""Run with: python -m unittest test_smoke (requires requirements.txt)."""
import os
import tempfile
import unittest

class SmokeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ['CHEM_DB'] = self.tmp.name + '/test.db'
        os.environ['SECRET_KEY'] = 'a' * 40
        os.environ['ADMIN_PASSWORD'] = 'b' * 16
        import app
        self.app = app
        self.app.DB = os.environ['CHEM_DB']
        self.app.init()
        self.client = self.app.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def test_health(self):
        self.assertEqual(self.client.get('/health').json, {'status': 'ok'})

    def test_alerts_requires_login(self):
        self.assertEqual(self.client.get('/stock-alerts').status_code, 302)

    def test_alerts_and_validation(self):
        with self.client.session_transaction() as s:
            s['uid'], s['username'], s['role'] = 1, 'admin', 'admin'
        self.assertEqual(self.client.get('/stock-alerts').status_code, 200)
        self.assertEqual(self.client.get('/stock-alerts?days=366').status_code, 400)

if __name__ == '__main__':
    unittest.main()
