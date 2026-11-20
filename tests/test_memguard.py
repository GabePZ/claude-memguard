import contextlib
import importlib.machinery
import importlib.util
import io
import os
import plistlib
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "claude-memguard"
loader = importlib.machinery.SourceFileLoader("memguard", str(SCRIPT))
spec = importlib.util.spec_from_loader("memguard", loader)
mg = importlib.util.module_from_spec(spec)
loader.exec_module(mg)

GB = mg.GB


def settings(**overrides):
    base = dict(
        per_process=12 * GB, total=20 * GB, min_free_pct=10, interval=1.0, cooldown=3.0,
        notify=False, apps=("/Applications/Claude.app/",), protect=("/System/Library/",),
    )
    base.update(overrides)
    return mg.Settings(**base)


def proc(pid, gb, app="Claude", protected=False):
    return mg.Proc(pid, f"/bin/p{pid}", int(gb * GB), app, protected)


class ParseLimit(unittest.TestCase):
    def test_units(self):
        ram = 36 * GB
        self.assertEqual(mg.parse_limit("12", ram), 12 * GB)
        self.assertEqual(mg.parse_limit("12G", ram), 12 * GB)
        self.assertEqual(mg.parse_limit("12gb", ram), 12 * GB)
        self.assertEqual(mg.parse_limit("1.5", ram), int(1.5 * GB))
        self.assertEqual(mg.parse_limit("512M", ram), 512 * mg.MB)
        self.assertIsNone(mg.parse_limit("off", ram))

    def test_percent_rounds_to_half_gb(self):
        self.assertEqual(mg.parse_limit("33%", 36 * GB), 12 * GB)
        self.assertEqual(mg.parse_limit("55%", 36 * GB), 20 * GB)
        self.assertEqual(mg.parse_limit("33%", 8 * GB), int(2.5 * GB))

    def test_rejects_bad_values(self):
        for text in ("", "abc", "0", "-1", "12T", "150%"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                mg.parse_limit(text, 36 * GB)


class Defaults(unittest.TestCase):
    def resolve(self, ram_gb, config=None, flags=None):
        return mg.resolve(mg.gather(config or {}, flags or {}), ram_gb * GB)

    def test_scale_with_ram(self):
        expected = {8: (2.5, 4.5), 16: (5.5, 9), 36: (12, 20), 64: (21, 35), 128: (42, 70.5)}
        for ram, (per_process, total) in expected.items():
            with self.subTest(ram=ram):
                s = self.resolve(ram)
                self.assertEqual(s.per_process, int(per_process * GB))
                self.assertEqual(s.total, int(total * GB))
                self.assertEqual(s.min_free_pct, 10)

    def test_default_apps_and_protect(self):
        s = self.resolve(36)
        self.assertEqual(s.apps, ("/Applications/Claude.app/", "/Applications/ChatGPT.app/"))
        self.assertEqual(s.protect, ("/System/Library/",))

    def test_flag_beats_config_beats_default(self):
        raw = mg.gather({"per_process": "8", "total": "40%"}, {"per_process": "6G"})
        self.assertEqual(raw["per_process"], ("6G", "flag"))
        self.assertEqual(raw["total"], ("40%", "config"))
        self.assertEqual(raw["min_free_pct"], ("10", "default"))
        s = mg.resolve(raw, 36 * GB)
        self.assertEqual(s.per_process, 6 * GB)
        self.assertEqual(s.total, int(14.5 * GB))

    def test_app_paths_get_trailing_slash(self):
        s = self.resolve(36, config={"apps": "~/Applications/Foo.app\n/Applications/Bar.app/"})
        self.assertEqual(s.apps, (os.path.expanduser("~/Applications/Foo.app/"), "/Applications/Bar.app/"))

    def test_bad_value_names_setting_and_source(self):
        with self.assertRaisesRegex(ValueError, r"total \(config\)"):
            self.resolve(36, config={"total": "lots"})


class ReadConfig(unittest.TestCase):
    def write(self, text):
        handle = tempfile.NamedTemporaryFile("w", suffix=".ini", delete=False)
        handle.write(textwrap.dedent(text))
        handle.close()
        self.addCleanup(os.remove, handle.name)
        return handle.name

    def test_missing_file_is_empty(self):
        self.assertEqual(mg.read_config("/nonexistent/config.ini"), {})

    def test_example_config_changes_nothing(self):
        self.assertEqual(mg.read_config(self.write(mg.EXAMPLE_CONFIG)), {})

    def test_percent_and_multiline_lists(self):
        path = self.write("""
            [memguard]
            total = 50%
            apps =
                /Applications/Claude.app
                # a comment inside the list
                /Applications/Cursor.app
        """)
        config = mg.read_config(path)
        self.assertEqual(config["total"], "50%")
        s = mg.resolve(mg.gather(config, {}), 36 * GB)
        self.assertEqual(s.apps, ("/Applications/Claude.app/", "/Applications/Cursor.app/"))

    def test_inline_comments(self):
        config = mg.read_config(self.write("[memguard]\nper_process = 33%   # a share of RAM\n"))
        self.assertEqual(config["per_process"], "33%")

    def test_repo_example_matches_installed_example(self):
        self.assertEqual((ROOT / "config.example.ini").read_text(), mg.EXAMPLE_CONFIG)

    def test_unknown_setting_is_an_error(self):
        with self.assertRaisesRegex(ValueError, "unknown setting per_process_gb"):
            mg.read_config(self.write("[memguard]\nper_process_gb = 12\n"))


class PickVictims(unittest.TestCase):
    def test_per_process_kills_every_offender(self):
        procs = [proc(1, 13), proc(2, 12.5), proc(3, 1)]
        victims = mg.pick_victims(procs, settings(), free_pct=60)
        self.assertEqual([p.pid for p, _ in victims], [1, 2])
        self.assertIn("per-process", victims[0][1])

    def test_total_kills_largest_of_that_app(self):
        procs = [proc(1, 9), proc(2, 8, app="ChatGPT"), proc(3, 7), proc(4, 5)]
        victims = mg.pick_victims(procs, settings(), free_pct=60)
        self.assertEqual([p.pid for p, _ in victims], [1])
        self.assertIn("Claude-launched processes total 21.0 GB", victims[0][1])

    def test_protected_counts_toward_total_but_is_never_killed(self):
        procs = [proc(1, 14, protected=True), proc(2, 4), proc(3, 3)]
        victims = mg.pick_victims(procs, settings(), free_pct=60)
        self.assertEqual([p.pid for p, _ in victims], [2])
        self.assertIn("budget", victims[0][1])

    def test_free_floor_kills_largest_if_at_least_1gb(self):
        self.assertEqual([p.pid for p, _ in mg.pick_victims([proc(1, 2), proc(2, 1)], settings(), 10)], [1])
        self.assertEqual(mg.pick_victims([proc(1, 0.5)], settings(), 5), [])
        self.assertEqual(mg.pick_victims([proc(1, 2)], settings(), 11), [])

    def test_rules_can_be_turned_off(self):
        procs = [proc(1, 13), proc(2, 9)]
        off = settings(per_process=None, total=None, min_free_pct=0)
        self.assertEqual(mg.pick_victims(procs, off, free_pct=1), [])

    def test_nothing_to_do(self):
        self.assertEqual(mg.pick_victims([], settings(), free_pct=1), [])
        self.assertEqual(mg.pick_victims([proc(1, 4)], settings(), free_pct=60), [])


class Cli(unittest.TestCase):
    def test_flags_map_to_settings(self):
        args = mg.parse_args(["status", "--per-process", "8G", "--total-gb", "30%", "--app", "/Applications/X.app", "--no-notify"])
        self.assertEqual(args.command, "status")
        self.assertEqual(
            mg.flag_overrides(args),
            {"per_process": "8G", "total": "30%", "notify": "false", "apps": "/Applications/X.app"},
        )

    def test_legacy_status_flag(self):
        self.assertEqual(mg.parse_args(["--status"]).command, "status")

    def test_install_rejects_limit_flags(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            mg.parse_args(["install", "--total", "20"])


class Install(unittest.TestCase):
    """install/uninstall against a temporary HOME and a fake launchctl."""

    def test_install_writes_agent_and_config_then_uninstall_removes_them(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as bindir:
            calls = Path(home) / "launchctl-calls"
            fake = Path(bindir) / "launchctl"
            fake.write_text(f'#!/bin/sh\necho "$@" >> "{calls}"\n[ "$1" = print ] && exit 1\nexit 0\n')
            fake.chmod(0o755)
            env = {**os.environ, "HOME": home, "PATH": f"{bindir}:{os.environ['PATH']}"}
            env.pop("CLAUDE_MEMGUARD_CONFIG", None)

            out = subprocess.run([sys.executable, str(SCRIPT), "install"], env=env, capture_output=True, text=True)
            self.assertEqual(out.returncode, 0, out.stderr)

            installed = Path(home, ".local/bin/claude-memguard")
            plist_path = Path(home, f"Library/LaunchAgents/{mg.LABEL}.plist")
            config = Path(home, ".config/claude-memguard/config.ini")
            self.assertTrue(os.access(installed, os.X_OK))
            self.assertEqual(config.read_text(), mg.EXAMPLE_CONFIG)
            plist = plistlib.loads(plist_path.read_bytes())
            self.assertEqual(plist["ProgramArguments"], [sys.executable, str(installed), "run"])
            self.assertTrue(plist["KeepAlive"])
            self.assertIn(f"bootstrap gui/{os.getuid()} {plist_path}", calls.read_text())
            self.assertIn("per-process", out.stdout)

            out = subprocess.run([sys.executable, str(installed), "uninstall"], env=env, capture_output=True, text=True)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertFalse(plist_path.exists())
            self.assertFalse(installed.exists())
            self.assertTrue(config.exists())


if __name__ == "__main__":
    unittest.main()
