"""TLS configuration propagation without cloud credentials or API calls."""

import contextlib
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import yaml


ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ultimum"))
from ultimum_validation import cli  # noqa: E402
from ultimum_validation import config  # noqa: E402
from ultimum_validation import prepare  # noqa: E402


class TLSTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = pathlib.Path(directory.name)
        self.path = self.directory / "settings.yaml"
        self.rc = {
            "OS_AUTH_URL": "https://identity.invalid/v3",
            "OS_USERNAME": "admin",
            "OS_PASSWORD": "secret",
            "OS_PROJECT_NAME": "admin",
        }
        self.ca = self.directory / "ca.pem"
        self.ca.write_text("CA bundle fixture")
        self.cfg = self.load({})

    def load(self, values):
        self.path.write_text(yaml.safe_dump(values))
        return config.load(self.path)

    def test_old_config_defaults_to_verification_and_inherits_openrc(self):
        clients = prepare.clouds(self.cfg, self.rc)
        self.assertTrue(all(c.session.verify is True for c in clients))
        rc = dict(self.rc, OS_CACERT=str(self.ca))
        clients = prepare.clouds(self.cfg, rc)
        self.assertEqual(
            [str(self.ca)] * 2, [c.session.verify for c in clients]
        )
        rc["OS_INSECURE"] = "True"
        clients = prepare.clouds(self.cfg, rc)
        self.assertTrue(all(c.session.verify is False for c in clients))
        self.assertTrue(all(c.rc["OS_CACERT"] == "" for c in clients))
        self.assertEqual(str(self.ca), rc["OS_CACERT"])

    def test_yaml_ca_and_false_override_insecure_openrc_for_both_users(self):
        self.cfg["tls"] = {"ca_cert": str(self.ca), "insecure": False}
        rc = dict(self.rc, OS_INSECURE="true", OS_CACERT="/old/missing.pem")
        admin, user = prepare.clouds(self.cfg, rc)
        self.assertEqual(str(self.ca), admin.session.verify)
        self.assertEqual(str(self.ca), user.session.verify)
        self.assertEqual("admin", admin.session.auth.auth_methods[0].username)
        self.assertEqual(
            self.cfg["identity"]["user"]["name"],
            user.session.auth.auth_methods[0].username,
        )

    def test_insecure_clears_ca_for_every_client(self):
        for ca_cert in (None, "/missing/ca.pem"):
            self.cfg["tls"] = {"ca_cert": ca_cert, "insecure": True}
            clients = prepare.clouds(
                self.cfg, dict(self.rc, OS_CACERT="/old/ca.pem")
            )
            self.assertTrue(all(c.session.verify is False for c in clients))
            self.assertTrue(all(c.rc["OS_CACERT"] == "" for c in clients))

    def test_empty_ca_selects_default_trust(self):
        self.cfg["tls"] = {"ca_cert": "", "insecure": False}
        rc = dict(self.rc, OS_CACERT="/old/ca.pem", OS_INSECURE="true")
        clients = prepare.clouds(self.cfg, rc)
        self.assertTrue(all(c.session.verify is True for c in clients))

    def test_online_validation_allows_inherited_insecure_with_unused_ca(self):
        self.cfg["tls"]["ca_cert"] = "/missing/ca.pem"
        self.cfg["identity"]["user"]["password"] = "secret"
        self.cfg["network"].update(
            external_network="public", probe_source_cidr="192.0.2.1/32"
        )
        clients = prepare.clouds(
            self.cfg, dict(self.rc, OS_INSECURE="true")
        )
        config.validate(self.cfg)
        self.assertTrue(all(c.session.verify is False for c in clients))

    def test_schema_rejects_ambiguous_values(self):
        for values in (
            {"insecure": "false"},
            {"insecure": 1},
            {"ca_cert": False},
            {"ca_cert": []},
            {"insecure_typo": True},
        ):
            with self.subTest(values=values):
                with self.assertRaises(config.InvalidError):
                    self.load({"tls": values})
        for insecure in (True, False, None):
            cfg = self.load({"tls": {"insecure": insecure}})
            self.assertIs(insecure, cfg["tls"]["insecure"])

    def test_invalid_ca_paths_fail_before_authentication(self):
        for ca_cert in (
            "relative.pem", "/missing/ca.pem", str(self.directory)
        ):
            self.cfg["tls"] = {"ca_cert": ca_cert, "insecure": False}
            with self.subTest(ca_cert=ca_cert):
                with self.assertRaisesRegex(config.InvalidError, "TLS CA"):
                    prepare.clouds(self.cfg, self.rc)

    def test_offline_check_validates_explicit_ca(self):
        self.load({"tls": {"ca_cert": "/missing/ca.pem"}})
        with contextlib.redirect_stderr(io.StringIO()) as output:
            self.assertEqual(
                2, cli.main(["--config", str(self.path), "check", "--offline"])
            )
        self.assertIn("TLS CA certificate", output.getvalue())

    def test_rally_environment_changes_with_tls_settings(self):
        self.cfg["execution"]["state_dir"] = str(self.directory / "state")
        specs = []
        runtime = {"project_id": "test-project"}

        def rally_command(cfg, *args, **kwargs):
            if args[:2] == ("env", "create"):
                spec_path = pathlib.Path(args[args.index("--spec") + 1])
                self.assertEqual(0o600, spec_path.stat().st_mode & 0o777)
                specs.append(json.loads(spec_path.read_text()))
                result = json.dumps({"uuid": "env-" + str(len(specs))})
                return subprocess.CompletedProcess(args, 0, result, "")
            return subprocess.CompletedProcess(args, 0, "{}", "")

        with mock.patch.object(cli, "rally", side_effect=rally_command):
            with contextlib.redirect_stdout(io.StringIO()):
                secure = cli.register_environment(self.cfg, self.rc, runtime)
                self.cfg["tls"]["ca_cert"] = str(self.ca)
                custom = cli.register_environment(self.cfg, self.rc, runtime)
                self.cfg["tls"]["insecure"] = True
                insecure = cli.register_environment(self.cfg, self.rc, runtime)
                reused = cli.register_environment(self.cfg, self.rc, runtime)
        self.assertEqual(3, len({secure, custom, insecure}))
        self.assertEqual(insecure, reused)
        settings = [s["existing@openstack"] for s in specs]
        self.assertEqual(
            [(False, ""), (False, str(self.ca)), (True, "")],
            [(s["https_insecure"], s["https_cacert"]) for s in settings],
        )

    def test_check_auth_uses_current_config_without_preparing_resources(self):
        for insecure in (False, True):
            self.load({"tls": {"ca_cert": str(self.ca), "insecure": insecure}})
            with (
                mock.patch.object(cli, "openrc", return_value=self.rc) as rc,
                mock.patch.object(cli, "session_from_rc") as session,
                mock.patch.object(cli, "clouds") as clouds,
                mock.patch.object(cli, "prepare") as prepare_mock,
                contextlib.redirect_stdout(io.StringIO()) as output,
            ):
                session.return_value.get_token.return_value = "secret-token"
                self.assertEqual(
                    0,
                    cli.main([
                        "--config", str(self.path), "check-auth",
                        "--openrc", "/custom/admin.rc",
                    ]),
                )
                rc.assert_called_once_with("/custom/admin.rc")
                resolved = session.call_args.args[0]
                self.assertEqual(
                    str(insecure).lower(), resolved["OS_INSECURE"]
                )
                self.assertEqual(
                    "" if insecure else str(self.ca), resolved["OS_CACERT"]
                )
                session.return_value.get_token.assert_called_once_with()
                clouds.assert_not_called()
                prepare_mock.assert_not_called()
                self.assertNotIn("secret", output.getvalue())

    def test_check_auth_failure_is_nonzero_without_exposing_secrets(self):
        with (
            mock.patch.object(cli, "openrc", return_value=self.rc),
            mock.patch.object(cli, "session_from_rc") as session,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()) as output,
        ):
            session.return_value.get_token.side_effect = RuntimeError("secret")
            self.assertEqual(
                1, cli.main(["--config", str(self.path), "check-auth"])
            )
        self.assertNotIn("secret", output.getvalue())

    def test_check_auth_reports_tls_configuration_on_ssl_failure(self):
        from keystoneauth1.exceptions.connection import SSLError

        with (
            mock.patch.object(cli, "openrc", return_value=self.rc),
            mock.patch.object(cli, "session_from_rc") as session,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()) as output,
        ):
            session.return_value.get_token.side_effect = SSLError("secret")
            self.assertEqual(
                1, cli.main(["--config", str(self.path), "check-auth"])
            )
        self.assertIn("tls.ca_cert", output.getvalue())
        self.assertIn("tls.insecure", output.getvalue())
        self.assertNotIn("secret", output.getvalue())

    def test_startup_reports_current_runner_authentication(self):
        # Run the real shell logic with isolated paths and command stubs.
        script = (ROOT / "ultimum/rally-bash").read_text()
        for path in (
            "/run/rally-status", "/run/rally-ready", "/tmp/rally-data.log",
            "/tmp/rally-db.log", "/tmp/rally-deployment-use.log",
            "/tmp/rally-deployment-create.log", "/tmp/rally-auth-check.log",
        ):
            script = script.replace(path, str(self.directory / path[1:]))
        script = script.replace(
            "if [ -d /data ]; then", "if true; then"
        ).replace("while :; do", "exit 0\nwhile :; do")
        (self.directory / "run").mkdir()
        (self.directory / "tmp").mkdir()
        (self.directory / "admin.rc").write_text("")
        for command in ("rally", "ultimum-rally"):
            stub = self.directory / command
            stub.write_text(
                '#!/bin/bash\nprintf "%s\\n" "$*" >> "$CALL_LOG"\n'
                'if [ "$1" = check-auth ]; then\n'
                '    echo "Authentication diagnostic"\n'
                '    exit "$AUTH_RESULT"\nfi\n'
            )
            stub.chmod(0o700)
        env = dict(os.environ)
        env.update(
            PATH=str(self.directory) + os.pathsep + env["PATH"],
            CALL_LOG=str(self.directory / "calls"),
            RALLY_OPENRC=str(self.directory / "admin.rc"),
        )
        for result in (0, 1):
            env["AUTH_RESULT"] = str(result)
            with self.subTest(result=result):
                subprocess.run(
                    ["bash", "--noprofile", "--norc"], input=script,
                    env=env, text=True, capture_output=True, check=True,
                    timeout=10,
                )
                status = (self.directory / "run/rally-status").read_text()
                self.assertTrue((self.directory / "run/rally-ready").exists())
                if result:
                    self.assertIn(
                        "[WARN] OpenStack authentication failed", status
                    )
                    self.assertIn("rally-auth-check.log", status)
                else:
                    self.assertIn("[ OK ] OpenStack authentication", status)
                self.assertIn(
                    "Authentication diagnostic",
                    (self.directory / "tmp/rally-auth-check.log").read_text(),
                )
        calls = (self.directory / "calls").read_text().splitlines()
        self.assertIn("check-auth --openrc " + env["RALLY_OPENRC"], calls)
        self.assertFalse(any(c.startswith("deployment check") for c in calls))


if __name__ == "__main__":
    unittest.main()
