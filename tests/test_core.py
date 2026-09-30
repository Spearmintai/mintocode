import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from micode import retrieve, store, symbols  # noqa: E402

PY = '''import os
from .util import helper

MAX_RETRIES = 3

class Client:
    def get(self, url):
        return helper(url)

    async def close(self):
        pass

def main():
    Client().get("x")
'''

TS = '''import { a } from "./a";
export class Store {
  constructor() {}
  save(item: string): void {
    if (item) { console.log("{"); }
  }
}
export const load = async (id: number) => {
  return id;
};
export interface Opts { x: number }
'''


class Symbols(unittest.TestCase):
    def test_python_spans(self):
        syms, imps = symbols.extract(PY, "python")
        by = {s["name"]: s for s in syms}
        self.assertEqual((by["Client"]["start"], by["Client"]["end"]), (6, 11))
        self.assertEqual((by["Client.get"]["start"], by["Client.get"]["end"]), (7, 8))
        self.assertEqual(by["MAX_RETRIES"]["kind"], "constant")
        self.assertIn(".util", imps)

    def test_typescript_braces_ignore_strings(self):
        syms, imps = symbols.extract(TS, "typescript")
        by = {s["name"]: s for s in syms}
        self.assertEqual((by["Store"]["start"], by["Store"]["end"]), (2, 7))
        self.assertEqual(by["Store.save"]["end"], 6)
        self.assertEqual((by["load"]["start"], by["load"]["end"]), (8, 10))
        self.assertIn("Opts", by)
        self.assertEqual(imps, ["./a"])

    def test_resolve_imports(self):
        g = symbols.resolve_imports({"pkg/a.py": [".util"], "pkg/util.py": [], "web/x.ts": ["./y"], "web/y.ts": []},
                                    {"pkg/a.py": "python", "pkg/util.py": "python", "web/x.ts": "typescript", "web/y.ts": "typescript"})
        self.assertEqual(g["pkg/a.py"], ["pkg/util.py"])
        self.assertEqual(g["web/x.ts"], ["web/y.ts"])


class Resolver(unittest.TestCase):
    def setUp(self):
        syms, _ = symbols.extract(PY, "python")
        self.r = store.Resolver({"c.py": syms}, {"c.py": 14})

    def test_symbol_ref(self):
        self.assertEqual(self.r.ref("c.py::Client.get"), "c.py:L7-8")
        self.assertEqual(self.r.ref("c.py::get"), "c.py:L7-8")

    def test_line_ref_and_bad(self):
        self.assertEqual(self.r.ref("c.py:L3-99"), "c.py:L3-14")
        self.assertIsNone(self.r.ref("c.py::Nope"))
        self.assertIsNone(self.r.ref("missing.py:L1"))

    def test_annotate(self):
        t = self.r.annotate("calls `c.py::main` then `c.py::ghost`")
        self.assertIn("(L13-14)", t)
        self.assertIn("(unverified)", t)


class Tokens(unittest.TestCase):
    def test_identifier_split(self):
        t = retrieve.tokens("How does parseHTTPResponse handle max_retries?")
        for w in ("pars", "http", "respons", "max", "retry"):
            self.assertTrue(any(x.startswith(w) for x in t), (w, t))


if __name__ == "__main__":
    unittest.main()
