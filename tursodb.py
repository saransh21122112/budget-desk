"""Tiny Turso (libSQL) client over the Hrana HTTP API. Exposes just the sqlite3 subset main.py uses.
ponytail: every statement is its own request (autocommit); only atomic() is a transaction. save() uses that."""
import json
import urllib.request


class Row:
    def __init__(self, cols, vals):
        self._c, self._v = cols, vals

    def __getitem__(self, k):
        return self._v[k] if isinstance(k, int) else self._v[self._c.index(k)]

    def keys(self):
        return list(self._c)

    def __iter__(self):
        return iter(self._v)

    def __len__(self):
        return len(self._v)


def _enc(v):
    if v is None:
        return {"type": "null"}
    if isinstance(v, (bool, int)):
        return {"type": "integer", "value": str(int(v))}
    if isinstance(v, float):
        return {"type": "float", "value": v}
    return {"type": "text", "value": str(v)}


def _dec(v):
    t = v["type"]
    return None if t == "null" else int(v["value"]) if t == "integer" else float(v["value"]) if t == "float" else v["value"]


def _stmt(sql, params=()):
    if isinstance(params, dict):
        return {"sql": sql, "named_args": [{"name": ":" + k, "value": _enc(v)} for k, v in params.items()]}
    return {"sql": sql, "args": [_enc(v) for v in params]}


class Cursor:
    def __init__(self, res):
        cols = [c["name"] for c in res["cols"]]
        self._rows = [Row(cols, [_dec(x) for x in r]) for r in res["rows"]]
        self.rowcount = res.get("affected_row_count", 0)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows

    def __iter__(self):
        return iter(self._rows)


class Remote:
    def __init__(self, url, token):
        self.url = url.replace("libsql://", "https://").rstrip("/") + "/v2/pipeline"
        self.token = token

    def _post(self, requests):
        body = json.dumps({"requests": [*requests, {"type": "close"}]}).encode()
        req = urllib.request.Request(self.url, body, {"Authorization": "Bearer " + self.token,
                                                      "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            out = json.load(r)["results"]
        for x in out:
            if x["type"] == "error":
                raise RuntimeError(x["error"]["message"])
        return [x["response"] for x in out[:-1]]

    def execute(self, sql, params=()):
        return Cursor(self._post([{"type": "execute", "stmt": _stmt(sql, params)}])[0]["result"])

    def executemany(self, sql, seq):
        self._post([{"type": "execute", "stmt": _stmt(sql, p)} for p in seq])

    def executescript(self, script):
        self._post([{"type": "execute", "stmt": _stmt(s)} for s in script.split(";") if s.strip()])

    def atomic(self, stmts):
        """Run [(sql, params), ...] as one transaction: commit only if every statement succeeded."""
        n = len(stmts)
        steps = [{"stmt": _stmt("begin")}]
        steps += [{"stmt": _stmt(sql, p), "condition": {"type": "ok", "step": i}} for i, (sql, p) in enumerate(stmts)]
        steps.append({"stmt": _stmt("commit"), "condition": {"type": "ok", "step": n}})
        steps.append({"stmt": _stmt("rollback"), "condition": {"type": "not", "cond": {"type": "ok", "step": n + 1}}})
        res = self._post([{"type": "batch", "batch": {"steps": steps}}])[0]["result"]
        errs = [e for e in res["step_errors"][:n + 2] if e]
        if errs:
            raise RuntimeError(errs[0]["message"])

    def commit(self):
        pass

    def close(self):
        pass
