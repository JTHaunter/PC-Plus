import unittest

from fastapi.testclient import TestClient

from app import _parse_input, app


class InputValidationTests(unittest.TestCase):
    def test_accepts_normalized_pointer_and_safe_key_events(self):
        self.assertEqual(
            _parse_input('{"type":"input","kind":"mouse_move","x":0.5,"y":1}'),
            {"type": "input", "kind": "mouse_move", "x": 0.5, "y": 1},
        )
        self.assertEqual(
            _parse_input(
                '{"type":"input","kind":"mouse_click","x":0,"y":0.2,"button":"left"}'
            ),
            {
                "type": "input",
                "kind": "mouse_click",
                "x": 0,
                "y": 0.2,
                "button": "left",
            },
        )
        self.assertEqual(
            _parse_input('{"type":"input","kind":"key_down","key":"a"}'),
            {"type": "input", "kind": "key_down", "key": "a"},
        )

    def test_rejects_malformed_or_out_of_bounds_input(self):
        invalid_messages = [
            "not json",
            "[]",
            '{"type":"request_access","kind":"mouse_move","x":0,"y":0}',
            '{"type":"input","kind":"mouse_move","x":true,"y":0}',
            '{"type":"input","kind":"mouse_move","x":1.1,"y":0}',
            '{"type":"input","kind":"mouse_move","x":NaN,"y":0}',
            '{"type":"input","kind":"mouse_click","x":0,"y":0,"button":"launch"}',
            '{"type":"input","kind":"key_down","key":"unapproved"}',
        ]
        for message in invalid_messages:
            with self.subTest(message=message):
                self.assertIsNone(_parse_input(message))


class SessionFlowTests(unittest.TestCase):
    def test_pairing_approval_and_relay(self):
        with TestClient(app, base_url="http://127.0.0.1") as client:
            created = client.post("/api/sessions", json={})
            self.assertEqual(created.status_code, 200)
            host_data = created.json()

            with client.websocket_connect("ws://127.0.0.1/ws/host") as host:
                host.send_json({"type": "auth", "token": host_data["host_token"]})
                self.assertEqual(host.receive_json(), {"type": "ready"})

                connected = client.post("/api/connect", json={"code": host_data["code"]})
                self.assertEqual(connected.status_code, 200)
                viewer_token = connected.json()["viewer_token"]

                with client.websocket_connect("ws://127.0.0.1/ws/viewer") as viewer:
                    viewer.send_json({"type": "auth", "token": viewer_token})
                    self.assertEqual(viewer.receive_json(), {"type": "ready"})
                    viewer.send_json({"type": "request_access"})

                    request = host.receive_json()
                    self.assertEqual(request["type"], "viewer_request")
                    self.assertEqual(viewer.receive_json(), {"type": "access_pending"})
                    host.send_json(
                        {
                            "type": "approve",
                            "viewer_id": request["viewer_id"],
                            "approved": True,
                            "screen": True,
                            "control": True,
                        }
                    )
                    self.assertEqual(
                        viewer.receive_json(),
                        {"type": "access", "approved": True, "screen": True, "control": True},
                    )

                    host.send_bytes(b"jpeg-frame")
                    self.assertEqual(viewer.receive_bytes(), b"jpeg-frame")
                    viewer.send_json(
                        {"type": "input", "kind": "mouse_move", "x": 0.5, "y": 0.25}
                    )
                    self.assertEqual(
                        host.receive_json(),
                        {"type": "input", "kind": "mouse_move", "x": 0.5, "y": 0.25},
                    )
                    viewer.send_json({"type": "end"})
                    self.assertEqual(host.receive_json(), {"type": "viewer_disconnected"})

    def test_external_http_redirects_to_https(self):
        with TestClient(app, base_url="http://remote.example", follow_redirects=False) as client:
            response = client.get("/")
            self.assertEqual(response.status_code, 307)
            self.assertEqual(response.headers["location"], "https://remote.example/")


if __name__ == "__main__":
    unittest.main()
