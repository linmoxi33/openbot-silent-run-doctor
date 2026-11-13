import json, os, subprocess, sys, tempfile, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import silent_run_doctor as srd  # noqa: E402

IN = os.path.join(ROOT, "examples", "in")
CLI = [sys.executable, os.path.join(ROOT, "bin", "openbot-silent-run-doctor")]


def run(args, stdin=None):
    return subprocess.run(CLI + args, capture_output=True, text=True, input=stdin, cwd=ROOT)


class Parsing(unittest.TestCase):
    def test_events_load_with_line_numbers(self):
        events = srd.load_events(os.path.join(IN, "silent-overflow.events.jsonl"))
        self.assertEqual([e["line"] for e in events], [1, 2, 3])
        self.assertEqual(events[0]["type"], "RUN_STARTED")

    def test_stdin_events_are_equivalent(self):
        text = open(os.path.join(IN, "healthy.events.jsonl")).read()
        self.assertEqual(len(srd.load_events("-")), 5) if False else None
        a = srd.load_events(os.path.join(IN, "healthy.events.jsonl"))
        b = [json.loads(l) for l in text.splitlines()]
        self.assertEqual([e["raw"] for e in a], b)

    def test_bad_json_is_input_error(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
            fh.write("{not json}\n")
        try:
            with self.assertRaises(srd.InputError):
                srd.load_events(fh.name)
        finally:
            os.unlink(fh.name)

    def test_tools_accepts_bare_list_and_wrapper(self):
        self.assertEqual(len(srd.load_tools(os.path.join(IN, "small.tools.json"))), 5)
        self.assertEqual(len(srd.load_tools(os.path.join(IN, "bad-names.tools.json"))), 5)


class Behaviour(unittest.TestCase):
    def test_confirmed_overflow(self):
        r = run(["--events", "examples/in/silent-overflow.events.jsonl",
                 "--tools", "examples/in/silent-overflow.tools.json",
                 "--log", "examples/in/silent-overflow.log", "--json"])
        self.assertEqual(r.returncode, 1)
        rep = json.loads(r.stdout)
        self.assertEqual(rep["verdict"], "confirmed-tool-overflow")
        self.assertEqual(rep["tools"], {"count": 131, "over_limit": True})
        rules = {f["rule"] for f in rep["findings"]}
        self.assertTrue({"RUN_FINISHED_WITHOUT_OUTPUT", "TOOL_ARRAY_OVER_MODEL_LIMIT",
                         "PROVIDER_REJECTION_IN_LOG"} <= rules)
        self.assertEqual(rep["summary"]["errors"], 3)

    def test_healthy_run_exits_zero(self):
        r = run(["--events", "examples/in/healthy.events.jsonl", "--tools", "examples/in/small.tools.json"])
        self.assertEqual(r.returncode, 0)
        self.assertIn("verdict: healthy", r.stdout)

    def test_silent_run_without_overflow_abstains(self):
        r = run(["--events", "examples/in/silent-unattributed.events.jsonl",
                 "--tools", "examples/in/small.tools.json", "--json"])
        self.assertEqual(r.returncode, 1)
        rep = json.loads(r.stdout)
        self.assertEqual(rep["verdict"], "silent-unattributed")
        self.assertNotIn("confirmed-tool-overflow", rep["verdict"])
        self.assertIn("ATTRIBUTION_ABSTAIN", {f["rule"] for f in rep["findings"]})

    def test_overflow_without_silent_run(self):
        r = run(["--events", "examples/in/healthy.events.jsonl",
                 "--tools", "examples/in/silent-overflow.tools.json", "--json"])
        self.assertEqual(r.returncode, 1)
        rep = json.loads(r.stdout)
        self.assertEqual(rep["verdict"], "tool-limit-only")
        self.assertNotIn("RUN_FINISHED_WITHOUT_OUTPUT", {f["rule"] for f in rep["findings"]})

    def test_unterminated_run(self):
        r = run(["--events", "examples/in/unterminated.events.jsonl", "--tools", "examples/in/small.tools.json", "--json"])
        self.assertEqual(r.returncode, 1)
        rep = json.loads(r.stdout)
        self.assertEqual(rep["verdict"], "healthy")
        self.assertIn("RUN_UNTERMINATED", {f["rule"] for f in rep["findings"]})

    def test_bad_tool_names(self):
        r = run(["--events", "examples/in/bad-names.events.jsonl",
                 "--tools", "examples/in/bad-names.tools.json", "--json"])
        self.assertEqual(r.returncode, 1)
        rules = [f["rule"] for f in json.loads(r.stdout)["findings"]]
        self.assertIn("TOOL_NAME_INVALID", rules)
        self.assertIn("TOOL_NAME_DUPLICATE", rules)

    def test_custom_limit_shifts_verdict(self):
        r = run(["--events", "examples/in/silent-overflow.events.jsonl",
                 "--tools", "examples/in/silent-overflow.tools.json", "--limit", "200", "--json"])
        self.assertEqual(r.returncode, 1)
        rep = json.loads(r.stdout)
        self.assertEqual(rep["verdict"], "silent-unattributed")
        self.assertFalse(rep["tools"]["over_limit"])

    def test_missing_file_is_exit_two(self):
        r = run(["--events", "missing.jsonl", "--tools", "examples/in/small.tools.json"])
        self.assertEqual(r.returncode, 2)
        self.assertIn("cannot read events file", r.stderr)

    def test_bad_limit_is_exit_two(self):
        r = run(["--events", "examples/in/healthy.events.jsonl", "--tools", "examples/in/small.tools.json", "--limit", "0"])
        self.assertEqual(r.returncode, 2)

    def test_stdin_matches_file(self):
        text = open(os.path.join(IN, "silent-overflow.events.jsonl")).read()
        a = run(["--events", "examples/in/silent-overflow.events.jsonl",
                 "--tools", "examples/in/silent-overflow.tools.json", "--json"])
        b = run(["--events", "-", "--tools", "examples/in/silent-overflow.tools.json", "--json"], stdin=text)
        self.assertEqual(a.stdout, b.stdout)
        self.assertEqual(b.returncode, 1)

    def test_json_is_deterministic_and_parsable(self):
        args = ["--events", "examples/in/silent-overflow.events.jsonl",
                "--tools", "examples/in/silent-overflow.tools.json",
                "--log", "examples/in/silent-overflow.log", "--json"]
        a, b = run(args), run(args)
        self.assertEqual(a.stdout, b.stdout)
        json.loads(a.stdout)

    def test_examples_out_matches_live_run(self):
        for name, args in (
            ("silent-overflow.json", ["--events", "examples/in/silent-overflow.events.jsonl",
                                      "--tools", "examples/in/silent-overflow.tools.json",
                                      "--log", "examples/in/silent-overflow.log", "--json"]),
            ("silent-unattributed.json", ["--events", "examples/in/silent-unattributed.events.jsonl",
                                          "--tools", "examples/in/small.tools.json", "--json"]),
        ):
            live = run(args)
            self.assertEqual(live.stdout, open(os.path.join(ROOT, "examples", "out", name)).read(), name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
