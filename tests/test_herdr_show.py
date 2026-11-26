import contextlib
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "skills/herdr-session/scripts/herdr-show"
LOADER = SourceFileLoader("herdr_show", str(SCRIPT))
MODULE = module_from_spec(spec_from_loader(LOADER.name, LOADER))
LOADER.exec_module(MODULE)


class HerdrShowTest(unittest.TestCase):
    def test_first_remote_session_initializes_its_layout(self):
        results = (
            [{"result": {"address": "test-peer/t7", "cwd": "/home/test", "name": "first"}}],
            [{"result": {"version": 0, "value": None}}],
            [{"result": {"version": 1}}],
        )
        output = io.StringIO()
        with patch.object(MODULE, "rpc", side_effect=results) as rpc, patch(
            "sys.argv", ["herdr-show", "test-peer/t7"]
        ), contextlib.redirect_stdout(output):
            MODULE.main()

        method, params = rpc.call_args.args[0][0]
        self.assertEqual(method, "doc.set")
        self.assertEqual(params["version"], 0)
        self.assertEqual(params["on"], "test-peer")
        space, = params["value"]["spaces"]
        self.assertEqual(space["folder"], "/home/test")
        tab, = space["tabs"]
        self.assertEqual(tab["name"], "first")
        self.assertTrue(MODULE.contains(space, "t7"))
        self.assertTrue(json.loads(output.getvalue())["shown"])
