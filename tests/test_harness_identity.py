"""默认 IdentityPort：静态目录 / 租户校验 / 过期 / 能力交集。"""
import unittest

from agent_core.harness.identity import StaticIdentityPort


class IdentityTests(unittest.TestCase):
    def _port(self, **kw):
        users = {
            "u1": {"tenant_id": "t1", "user_id": "u1", "roles": ["member"],
                   "capabilities": ["read", "write"], "writable_scopes": ["project"]},
            ("t1", "u2"): {"tenant_id": "t1", "user_id": "u2", "roles": ["admin"],
                           "capabilities": ["read", "approve"],
                           "writable_scopes": ["project", "platform"]},
        }
        agents = {
            "a1": {"agent_id": "a1", "roles": ["assistant"],
                   "capabilities": ["read"], "writable_scopes": ["project"]},
        }
        return StaticIdentityPort(users, agents, **kw)

    def test_01_user_resolve(self):
        ident = self._port().resolve({"tenant_id": "t1", "user_id": "u1"})
        self.assertEqual(ident["user_id"], "u1")
        self.assertEqual(ident["roles"], ["member"])

    def test_02_tuple_key(self):
        ident = self._port().resolve({"tenant_id": "t1", "user_id": "u2"})
        self.assertEqual(ident["roles"], ["admin"])

    def test_03_tenant_mismatch(self):
        with self.assertRaises(PermissionError):
            self._port().resolve({"tenant_id": "t2", "user_id": "u1"})

    def test_04_unknown_user_rejected(self):
        with self.assertRaises(PermissionError):
            self._port().resolve({"tenant_id": "t1", "user_id": "nope"})

    def test_05_anonymous_allowed(self):
        ident = self._port(allow_anonymous=True).resolve(
            {"tenant_id": "t9", "user_id": "guest"})
        self.assertEqual(ident["user_id"], "guest")
        self.assertEqual(ident["roles"], [])

    def test_06_expired_rejected(self):
        port = StaticIdentityPort(
            {"u1": {"tenant_id": "t1", "user_id": "u1", "expires_at": 10}},
            clock=lambda: 11)
        with self.assertRaises(PermissionError):
            port.resolve({"tenant_id": "t1", "user_id": "u1"})

    def test_07_agent_narrows_capabilities(self):
        ident = self._port().resolve(
            {"tenant_id": "t1", "user_id": "u1", "agent_id": "a1"})
        self.assertEqual(ident["capabilities"], ["read"])
        self.assertEqual(ident["writable_scopes"], ["project"])
        self.assertEqual(ident["roles"], ["assistant", "member"])

    def test_08_missing_agent_rejected(self):
        with self.assertRaises(PermissionError):
            self._port().resolve(
                {"tenant_id": "t1", "user_id": "u1", "agent_id": "nope"})


if __name__ == "__main__":
    unittest.main()
