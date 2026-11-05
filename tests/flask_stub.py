"""Test-only miniature Flask interface for environments without Flask installed.

Production uses the real Flask dependency. This stub only exercises our route
logic, NOT production Flask behavior or threading.
"""
from types import SimpleNamespace
from urllib.parse import urlsplit, parse_qs

class _Abort(Exception):
    def __init__(self, code):
        self.code = code


def abort(code):
    raise _Abort(code)


request = SimpleNamespace(args={}, headers={}, get_data=lambda **_: b"", get_json=lambda **_: {})


class _Result:
    def __init__(self, value):
        if isinstance(value, tuple):
            body, code = value
        else:
            body, code = value, 200
        self.status_code = code
        self.data = body.encode() if isinstance(body, str) else body


class Flask:
    def __init__(self, name):
        self.config = {}
        self.handlers = {}

    def get(self, path):
        def decorator(fn):
            self.handlers[("GET", path)] = fn
            return fn
        return decorator

    def post(self, path):
        def decorator(fn):
            self.handlers[("POST", path)] = fn
            return fn
        return decorator

    def test_client(self):
        app = self
        class Client:
            def invoke(self, method, path, data=b"", headers=None):
                url = urlsplit(path)
                request.args = {key: value[0] for key, value in parse_qs(url.query).items()}
                request.headers = headers or {}
                request.get_data = lambda **_: data
                request.get_json = lambda **_: __import__("json").loads(data) if data else {}
                try:
                    return _Result(app.handlers[(method, url.path)]())
                except _Abort as exc:
                    return _Result((b"", exc.code))
            def get(self, path, **kw):
                return self.invoke("GET", path, **kw)
            def post(self, path, data=b"", headers=None, **kw):
                return self.invoke("POST", path, data=data, headers=headers)
        return Client()
