"""Tests for configuration precedence and secret handling."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ia_reviver.config import Config, ConfigError, load_config


class TestDefaults(unittest.TestCase):
    def test_defaults_suit_a_local_qbittorrent(self):
        config = load_config(environ={})
        self.assertEqual(config.qb_url, "http://127.0.0.1:8080")
        self.assertIsNone(config.qb_username)
        self.assertTrue(config.verify)
        self.assertEqual(config.jobs, 3)

    def test_explicit_missing_config_file_is_an_error(self):
        with self.assertRaises(ConfigError):
            load_config(config_file="/nonexistent/ia-reviver.toml", environ={})


class TestPrecedence(unittest.TestCase):
    def test_file_then_env_then_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                'qb_url = "http://from-file:8080"\n'
                "jobs = 2\n"
                "verify = false\n"
                'state_dir = "/tmp/state"\n'
            )
            from_file = load_config(config_file=str(path), environ={})
            self.assertEqual(from_file.qb_url, "http://from-file:8080")
            self.assertEqual(from_file.jobs, 2)
            self.assertFalse(from_file.verify)

            from_env = load_config(
                config_file=str(path),
                environ={
                    "IA_REVIVER_QB_URL": "http://from-env:8080",
                    "IA_REVIVER_JOBS": "7",
                    "IA_REVIVER_VERIFY": "true",
                },
            )
            self.assertEqual(from_env.qb_url, "http://from-env:8080")
            self.assertEqual(from_env.jobs, 7)
            self.assertTrue(from_env.verify)

            from_flag = load_config(
                config_file=str(path),
                environ={"IA_REVIVER_QB_URL": "http://from-env:8080"},
                overrides={"qb_url": "http://from-flag:9000", "jobs": 9},
            )
            self.assertEqual(from_flag.qb_url, "http://from-flag:9000")
            self.assertEqual(from_flag.jobs, 9)

    def test_none_overrides_do_not_erase_lower_layers(self):
        # argparse defaults are None when an option was not given; those must not
        # clobber a value that came from the environment.
        config = load_config(
            environ={"IA_REVIVER_JOBS": "5"},
            overrides={"jobs": None, "qb_url": None},
        )
        self.assertEqual(config.jobs, 5)

    def test_sectioned_toml_is_flattened(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                "[qbittorrent]\n"
                'url = "http://sectioned:8080"\n'
                'username = "admin"\n'
                'password = "pw"\n'
                "[archive]\n"
                'base_url = "https://mirror.example"\n'
            )
            config = load_config(config_file=str(path), environ={})
            self.assertEqual(config.qb_url, "http://sectioned:8080")
            self.assertEqual(config.qb_username, "admin")
            self.assertEqual(config.qb_password, "pw")
            self.assertEqual(config.archive_base_url, "https://mirror.example")

    def test_common_qb_env_aliases(self):
        config = load_config(
            environ={"QB_URL": "http://alias:8080", "QB_USERNAME": "u", "QB_PASSWORD": "p"}
        )
        self.assertEqual(config.qb_url, "http://alias:8080")
        self.assertEqual(config.qb_username, "u")
        self.assertEqual(config.qb_password, "p")

    def test_insecure_flag_inverts_verify_tls(self):
        config = load_config(environ={"IA_REVIVER_INSECURE": "true"})
        self.assertFalse(config.verify_tls)
        config = load_config(environ={"IA_REVIVER_VERIFY_TLS": "false"})
        self.assertFalse(config.verify_tls)

    def test_limit_rate_accepts_suffixes_and_none(self):
        eight_meg = load_config(environ={"IA_REVIVER_LIMIT_RATE": "8M"})
        self.assertEqual(eight_meg.limit_rate, 8 * 1024**2)
        self.assertEqual(load_config(environ={"IA_REVIVER_LIMIT_RATE": "1024"}).limit_rate, 1024)
        self.assertIsNone(load_config(environ={"IA_REVIVER_LIMIT_RATE": "0"}).limit_rate)

    def test_invalid_environment_value_is_reported(self):
        with self.assertRaises(ConfigError):
            load_config(environ={"IA_REVIVER_JOBS": "not-a-number"})

    def test_jobs_must_be_positive(self):
        with self.assertRaises(ConfigError):
            load_config(overrides={"jobs": 0})

    def test_unknown_keys_are_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text('not_a_real_option = "x"\n')
            self.assertEqual(load_config(config_file=str(path), environ={}).jobs, 3)


class TestPasswordFile(unittest.TestCase):
    def test_password_read_from_file_with_trailing_newline_stripped(self):
        with tempfile.TemporaryDirectory() as tmp:
            secret = Path(tmp) / "pw"
            secret.write_text("hunter2\n")
            config = load_config(overrides={"qb_password_file": str(secret)})
            self.assertEqual(config.password(), "hunter2")
            self.assertIsNone(config.qb_password)

    def test_missing_password_file_is_an_error(self):
        config = load_config(overrides={"qb_password_file": "/nonexistent/secret"})
        with self.assertRaises(ConfigError):
            config.password()

    def test_inline_password_used_when_no_file(self):
        config = Config(qb_password="inline")
        self.assertEqual(config.password(), "inline")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
