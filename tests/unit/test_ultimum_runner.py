"""Offline acceptance-runner tests; no OpenStack credentials or resources."""

import base64
import contextlib
import copy
import io
import json
import pathlib
import selectors
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import jinja2
import yaml


ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ultimum"))
from ultimum_validation import bootstrap  # noqa: E402
from ultimum_validation import cli  # noqa: E402
from ultimum_validation import config  # noqa: E402
from ultimum_validation import keys  # noqa: E402
from ultimum_validation import prepare  # noqa: E402
from ultimum_validation import progress  # noqa: E402
from ultimum_validation.cloud import APIError  # noqa: E402
from ultimum_validation.cloud import Cloud  # noqa: E402
from ultimum_validation.cloud import wait_for  # noqa: E402
from ultimum_validation.guest import Guest  # noqa: E402
from ultimum_validation.scenarios import Scenarios  # noqa: E402
from ultimum_validation.state import Ledger  # noqa: E402
from ultimum_validation.state import project_lock  # noqa: E402


class RunnerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = pathlib.Path(self.tmp.name)
        self.cfg = config.load(ROOT / "ultimum/ultimum.yaml.example")
        self.cfg["identity"]["user"]["password"] = "private-unit-test-password"
        self.cfg["network"]["external_network"] = "public"
        self.cfg["network"]["probe_source_cidr"] = "192.0.2.1/32"
        for name in ("ssh_private_key", "ssh_public_key"):
            path = self.directory / name
            path.write_text("test-key")
            self.cfg["guest"][name] = str(path)
        self.cfg["execution"]["state_dir"] = str(self.directory / "state")
        self.cfg["execution"]["results_dir"] = str(self.directory / "results")
        self.run_id = "12345678-1111-2222-3333-123456789012"
        self.ledger = Ledger(
            self.directory / "state.json",
            {"run_id": self.run_id, "project_id": "test-project"},
        )
        self.admin = mock.Mock()
        self.cloud = mock.Mock(project_id="test-project")
        self.cloud.session.auth.get_access.return_value.role_names = ["member"]
        self.cloud.list.return_value = []
        self.cloud.absent.return_value = True
        self.runtime = dict(
            project_id="test-project",
            network_id="network",
            subnet_id="subnet",
            external_network_id="external",
            image_id="image",
            flavor_id="flavor",
            keypair_name="e2e-key",
        )

    def engine(self):
        return Scenarios(
            self.cfg, self.admin, self.cloud, self.ledger, self.runtime
        )


class ConfigTest(RunnerCase):
    def load_values(self, value):
        path = self.directory / "settings.yaml"
        path.write_text(yaml.safe_dump(value))
        return config.load(path)

    def test_legacy_drain_limit_is_accepted_but_no_longer_applied(self):
        cfg = self.load_values(
            {"scenarios": {"nova-drain": {"max_parallel_migrations": 1}}}
        )
        self.assertEqual(
            10, cfg["scenarios"]["nova-drain"]["active_instances"]
        )
        self.assertNotIn(
            "max_parallel_migrations", cfg["scenarios"]["nova-drain"]
        )
        with self.assertRaisesRegex(config.InvalidError, "must be 1"):
            self.load_values(
                {"scenarios": {"nova-drain": {"max_parallel_migrations": 2}}}
            )

    def test_host_scenarios_never_grant_admin_role_to_test_user(self):
        self.cfg["scenarios"]["nova-drain"]["enabled"] = True
        self.cfg["identity"]["user"]["roles"] = ["member", "admin"]
        with self.assertRaisesRegex(config.InvalidError, "must not grant"):
            config.validate(self.cfg)
        self.cfg["scenarios"]["nova-drain"]["enabled"] = False
        self.cfg["scenarios"]["nova-evacuate"]["enabled"] = True
        with self.assertRaisesRegex(config.InvalidError, "must not grant"):
            config.validate(self.cfg)

    def test_drain_preflight_checks_project_scoped_admin_read_only(self):
        self.cfg["scenarios"]["nova-drain"].update(
            enabled=True, host="host1"
        )
        with (
            mock.patch.object(prepare, "resolve_base", return_value={}),
            mock.patch.object(
                prepare,
                "host_service",
                return_value={"state": "up", "status": "enabled"},
            ),
            mock.patch.object(prepare, "exclusive_host", return_value=[]),
            mock.patch.object(prepare, "project_scoped_admin") as scoped,
        ):
            prepare.preflight(self.cfg, "nova-drain", self.admin, self.cloud)
        scoped.assert_called_once_with(self.admin, "test-project")
        self.admin.request.assert_not_called()

    def test_creation_flags_require_explicit_existing_references(self):
        for kind in (
            "project",
            "user",
            "network",
            "subnet",
            "router",
            "ssh_key",
        ):
            with self.subTest(kind=kind):
                with self.assertRaisesRegex(config.InvalidError, "explicit"):
                    self.load_values({"create_" + kind: False})

    def test_prefix_generates_names_only_for_new_resources(self):
        cfg = self.load_values({"resources_prefix": "my-e2e"})
        self.assertEqual("my-e2e-project", cfg["identity"]["project"]["name"])
        self.assertEqual("my-e2e-user", cfg["identity"]["user"]["name"])
        self.assertEqual(
            "my-e2e-net", cfg["network"]["tenant_network"]["name"]
        )
        self.assertEqual("my-e2e-subnet", cfg["network"]["subnet"]["name"])
        self.assertEqual("my-e2e-router", cfg["network"]["router"]["name"])
        self.assertEqual("my-e2e-key", cfg["ssh_key"]["name"])
        self.assertEqual(
            "/data/ultimum/keys/my-e2e-key", cfg["guest"]["ssh_private_key"]
        )
        self.cfg["resources_prefix"] = "my-e2e"
        self.assertTrue(self.engine().name("vm").startswith("my-e2e-"))
        cfg = self.load_values(
            {
                "resources_prefix": "e2e",
                "create_project": False,
                "create_router": False,
                "identity": {"project": {"name": "ultimum"}},
                "network": {"router": {"name": "ultimumrouter"}},
            }
        )
        self.assertEqual("ultimum", cfg["identity"]["project"]["name"])
        self.assertEqual("ultimumrouter", cfg["network"]["router"]["name"])

    def test_existing_uuid_accepted_and_create_with_uuid_rejected(self):
        cfg = self.load_values(
            {
                "create_router": False,
                "network": {"router": {"id": "router-uuid"}},
            }
        )
        self.assertIsNone(cfg["network"]["router"]["name"])
        with self.assertRaisesRegex(config.InvalidError, "requires"):
            self.load_values({"network": {"router": {"id": "router-uuid"}}})

    def test_create_flags_are_real_yaml_booleans(self):
        for value in ("false", 0, None):
            with self.assertRaises(config.InvalidError):
                self.load_values({"create_project": value})

    def test_legacy_keys_are_normalized_and_conflicts_rejected(self):
        cfg = self.load_values(
            {
                "execution": {"resource_prefix": "old-prefix"},
                "identity": {
                    "project": {
                        "create_if_missing": False,
                        "name": "existing-project",
                    }
                },
                "network": {
                    "mode": "existing",
                    "tenant_network": {"id": "network-id"},
                    "subnet": {"id": "subnet-id"},
                    "router": {"id": "router-id"},
                },
            }
        )
        self.assertEqual("old-prefix", cfg["resources_prefix"])
        for kind in ("project", "network", "subnet", "router"):
            self.assertFalse(cfg["create_" + kind])
        self.assertNotIn("mode", cfg["network"])
        with self.assertRaisesRegex(config.InvalidError, "Conflicting"):
            self.load_values(
                {
                    "network": {"mode": "existing"},
                    "create_router": True,
                }
            )

    def test_defaults_and_all_thirteen_tasks_render_without_credentials(self):
        self.assertEqual(13, len(config.SCENARIOS))
        for slug in config.SCENARIOS:
            path = ROOT / "tasks/ultimum/scenarios" / (slug + ".yaml")
            rendered = (
                jinja2.Environment(undefined=jinja2.StrictUndefined)
                .from_string(path.read_text())
                .render(
                    config_path="/private/config.yaml",
                    state_path="/private/state.json",
                )
            )
            task = yaml.safe_load(rendered)
            self.assertEqual(["Ultimum." + slug.replace("-", "_")], list(task))
            workload = next(iter(task.values()))[0]
            self.assertEqual({"users": {}}, workload["context"])
            self.assertEqual(
                {"type": "serial", "times": 1}, workload["runner"]
            )
            self.assertEqual(0, workload["sla"]["failure_rate"]["max"])
            self.assertNotIn(
                self.cfg["identity"]["user"]["password"], rendered
            )
            self.assertTrue(
                callable(getattr(Scenarios, slug.replace("-", "_")))
            )

    def test_disabled_host_tests_require_explicit_configuration(self):
        for slug in (
            "nova-drain", "nova-evacuate", "masakari-host-failure"
        ):
            with self.assertRaisesRegex(config.InvalidError, "disabled"):
                config.validate(self.cfg, slug)
            self.cfg["scenarios"][slug]["enabled"] = True
            with self.assertRaisesRegex(config.InvalidError, "explicit"):
                config.validate(self.cfg, slug)

    def test_evacuate_requires_persistent_root_volume(self):
        self.cfg["scenarios"]["nova-evacuate"].update(
            enabled=True, host="compute-test-01"
        )
        self.cfg["compute"]["boot_from_volume"] = False
        with self.assertRaisesRegex(config.InvalidError, "boot_from_volume"):
            config.validate(self.cfg, "nova-evacuate")

    def test_evacuate_preflight_checks_dedicated_host_read_only(self):
        self.cfg["scenarios"]["nova-evacuate"].update(
            enabled=True, host="compute-test-01"
        )
        self.admin.require_version.return_value = "2.99"
        self.cloud.require_version.return_value = "2.99"
        with (
            mock.patch.object(prepare, "resolve_base", return_value={}),
            mock.patch.object(
                prepare,
                "host_service",
                return_value={"state": "up", "status": "enabled"},
            ),
            mock.patch.object(
                prepare, "exclusive_host", return_value=[]
            ) as exclusive,
            mock.patch.object(prepare, "project_scoped_admin"),
        ):
            info = prepare.preflight(
                self.cfg, "nova-evacuate", self.admin, self.cloud
            )
        self.assertEqual("2.74", info["compute_version"])
        exclusive.assert_called_once_with(self.admin, "compute-test-01")
        self.admin.post.assert_not_called()
        self.cloud.post.assert_not_called()

    def test_fault_start_accepts_waiting_manual_nova_evacuation(self):
        path = self.directory / "settings.yaml"
        path.write_text(yaml.safe_dump(self.cfg))
        ledger = Ledger(
            cli.run_path(self.cfg, self.run_id),
            {
                "run_id": self.run_id,
                "scenario": "nova-evacuate",
                "status": "WAITING_FOR_FAULT",
            },
        )
        with mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(
                0,
                cli.main([
                    "--config", str(path), "fault-start", self.run_id
                ]),
            )
        marker = ledger.path.parent / "fault-start.json"
        self.assertTrue(marker.exists())
        self.assertIn("epoch", json.loads(marker.read_text()))

    def test_reject_unknown_keys_and_string_boolean(self):
        for value, expected in (
            ({"exection": {}}, "Unknown"),
            ({"scenarios": {"nova-drain": {"enabled": "false"}}}, "bool"),
        ):
            path = self.directory / "config.yaml"
            path.write_text(yaml.safe_dump(value))
            with self.assertRaisesRegex(config.InvalidError, expected):
                config.load(path)

    def test_dhcp_rejects_pool_and_reserved_addresses(self):
        for address in (
            "10.240.0.101",
            "10.240.0.0",
            "10.240.0.255",
            "10.240.0.1",
            "192.0.2.1",
        ):
            self.cfg["scenarios"]["neutron-dhcp"]["fixed_ip"] = address
            with self.assertRaises(config.InvalidError):
                config.validate(self.cfg, "neutron-dhcp")

    def test_required_migration_and_storage_modes_cannot_silently_degrade(
        self,
    ):
        cases = [
            ("nova-live-migration", "block_migration", True),
            ("nova-cloud-init", "config_drive", True),
            ("cinder-volume-extend", "online", False),
            ("cinder-snapshot-revert", "device_selection", "/dev/sdb"),
        ]
        for slug, key, value in cases:
            cfg = copy.deepcopy(self.cfg)
            cfg["scenarios"][slug][key] = value
            with self.assertRaises(config.InvalidError):
                config.validate(cfg, slug)

    def test_openrc_does_not_inherit_other_cloud_or_expose_output(self):
        path = self.directory / "admin.rc"
        path.write_text(
            "echo private-noise\nexport "
            "OS_AUTH_URL=https://identity.invalid/v3\nex"
            "port OS_USERNAME=admin\nexport "
            "OS_PASSWORD=secret\nexport "
            "OS_PROJECT_NAME=admin\n"
        )
        with mock.patch.dict("os.environ", {"OS_REGION_NAME": "wrong-cloud"}):
            values = config.openrc(path)
        self.assertNotIn("OS_REGION_NAME", values)
        self.assertEqual("secret", values["OS_PASSWORD"])

    def test_cli_list_and_explain_need_no_config_or_cloud(self):
        for command in (
            ["list"],
            ["explain", "runner"],
            ["explain", "nova-drain"],
        ):
            with mock.patch("sys.stdout", new_callable=io.StringIO) as output:
                self.assertEqual(0, cli.main(command))
                self.assertTrue(output.getvalue())

    def test_path_traversal_is_rejected(self):
        with self.assertRaises(config.InvalidError):
            cli.run_path(self.cfg, "../../someone-elses-run")


class StateAndCleanupTest(RunnerCase):
    def test_state_private_and_lock_excludes_second_process(self):
        self.assertEqual(0o600, self.ledger.path.stat().st_mode & 0o777)
        with project_lock(self.directory, "project"):
            with self.assertRaises(config.InvalidError):
                with project_lock(self.directory, "project"):
                    pass

    def test_cleanup_dependency_order_and_scope(self):
        items = [
            ("volume", "/volumes/v", "volume"),
            ("compute", "/servers/s", "server"),
            ("network", "/ports/p", "port"),
            ("network", "/floatingips/f", "floatingip"),
            ("volume", "/snapshots/snap", "snapshot"),
            ("network", "/security-groups/g", "security_group"),
        ]
        for service, path, kind in items:
            self.ledger.own(service, path, path.rsplit("/", 1)[1], kind=kind)
        engine = self.engine()
        engine.cleanup()
        calls = [
            (c.args[0], c.args[1]) for c in self.cloud.delete.call_args_list
        ]
        self.assertEqual([items[i][:2] for i in (3, 1, 2, 4, 0, 5)], calls)
        self.assertTrue(self.ledger.data["cleanup_complete"])
        self.assertFalse(
            any("project" in path or "router" in path for _, path in calls)
        )
        self.cloud.delete.reset_mock()
        engine.cleanup()
        self.cloud.delete.assert_not_called()

    def test_cleanup_never_runs_in_different_project(self):
        self.cloud.project_id = "another-project"
        with self.assertRaisesRegex(config.InvalidError, "project differs"):
            self.engine().cleanup()
        self.cloud.delete.assert_not_called()

    def test_failed_delete_is_retained_for_retry(self):
        self.ledger.own("volume", "/volumes/own", "own", kind="volume")
        self.cloud.delete.side_effect = APIError("volume", 409)
        with self.assertRaisesRegex(config.InvalidError, "Cleanup incomplete"):
            self.engine().cleanup()
        self.assertFalse(self.ledger.data["resources"][0]["deleted"])
        self.cloud.delete.side_effect = None
        self.engine().cleanup()
        self.assertTrue(self.ledger.data["resources"][0]["deleted"])

    def test_lost_post_response_recovered_by_ownership_marker(self):
        self.cloud.post.side_effect = TimeoutError()
        engine = self.engine()
        with self.assertRaises(TimeoutError):
            engine.create("network", "/ports", "port", {"name": "owned-port"})
        self.cloud.list.side_effect = lambda service, path, key, **kw: (
            [
                {
                    "id": "own",
                    "name": "owned-port",
                    "description": "Ultimum run " + self.run_id,
                },
                {
                    "id": "foreign",
                    "name": "owned-port",
                    "description": "another operator",
                },
            ]
            if path == "/ports"
            else []
        )
        engine.cleanup()
        self.cloud.delete.assert_called_once_with("network", "/ports/own")

    def test_restore_refuses_to_enable_a_down_host(self):
        self.ledger.data["restore"] = [
            {
                "host": "host1",
                "service_id": "svc",
                "status": "enabled",
                "done": False,
            }
        ]
        with mock.patch(
            "ultimum_validation.resources.host_service",
            return_value={"state": "down"},
        ):
            with self.assertRaisesRegex(config.InvalidError, "still down"):
                self.engine().restore_services()
        self.admin.request.assert_not_called()
        self.assertFalse(self.ledger.data["restore"][0]["done"])

    def test_foreign_host_instance_blocks_mutation(self):
        self.admin.list.return_value = [
            {"id": "foreign", "OS-EXT-SRV-ATTR:host": "host1"}
        ]
        engine = self.engine()
        engine.options = dict(
            self.cfg["scenarios"]["nova-drain"], host="host1"
        )
        with self.assertRaisesRegex(config.InvalidError, "foreign"):
            engine.nova_drain()
        self.cloud.post.assert_not_called()
        self.admin.request.assert_not_called()

    def test_drain_submits_all_ten_live_migrations_before_waiting(self):
        engine = self.engine()
        self.cloud.session.auth.get_access.return_value.role_names = [
            "member"
        ]
        engine.options = dict(
            self.cfg["scenarios"]["nova-drain"], host="host1"
        )
        calls = []
        vms = [{"id": f"drain-{index}"} for index in range(10)]
        guests = [mock.Mock() for _ in vms]
        engine.vm = mock.Mock(
            side_effect=[
                (vm, {"id": "port"}, {"id": "floating"}, guest)
                for vm, guest in zip(vms, guests)
            ]
        )
        engine.describe = mock.Mock(
            return_value={
                "OS-EXT-SRV-ATTR:host": "host1",
                "OS-EXT-AZ:availability_zone": "az1",
                "tenant_id": "test-project",
            }
        )
        engine.save_service = mock.Mock()
        engine.restore_services = mock.Mock()
        eligible = {"nova-compute": {"active": True, "available": True}}
        self.admin.list.return_value = [
            {
                "zoneName": "az1",
                "hosts": {
                    "host1": eligible,
                    "host2": eligible,
                },
            }
        ]
        engine.request_live_migration = mock.Mock(
            side_effect=lambda vm, scheduler: (
                calls.append(("request", vm["id"])),
                (
                    None,
                    {
                        "OS-EXT-SRV-ATTR:host": "host1",
                        "OS-EXT-AZ:availability_zone": "az1",
                    },
                ),
            )[1]
        )
        engine.wait_migration = mock.Mock(
            side_effect=lambda vm, *args, **kwargs: calls.append(
                ("wait", vm["id"])
            )
        )
        with (
            mock.patch(
                "ultimum_validation.scenarios.exclusive_host",
                return_value=[],
            ),
            mock.patch(
                "ultimum_validation.scenarios.host_service",
                return_value={"id": "service", "zone": "az1"},
            ),
            mock.patch(
                "ultimum_validation.scenarios.project_scoped_admin",
                return_value=mock.Mock(project_id="test-project"),
            ),
            mock.patch("ultimum_validation.scenarios.Continuity") as probe,
        ):
            engine.nova_drain()
        self.assertEqual(10, engine.vm.call_count)
        for call in engine.vm.call_args_list:
            self.assertEqual("host1", call.kwargs["host"])
            self.assertEqual("az1", call.kwargs["az"])
            self.assertFalse(call.kwargs["inject_keypair"])
            self.assertIn("ssh_authorized_keys", call.kwargs["userdata"])
        self.assertEqual(10, probe.call_count)
        self.assertEqual(
            [("request", vm["id"]) for vm in vms]
            + [("wait", vm["id"]) for vm in vms],
            calls,
        )
        for call in engine.request_live_migration.call_args_list:
            self.assertTrue(call.kwargs["scheduler"])
        engine.restore_services.assert_called_once_with()

    def test_scheduler_migration_accepts_only_another_host_in_same_az(self):
        engine = self.engine()
        vm = {"id": "drain-vm"}
        before = {
            "OS-EXT-SRV-ATTR:host": "host1",
            "OS-EXT-AZ:availability_zone": "az1",
            "status": "ACTIVE",
        }
        after = dict(before, **{"OS-EXT-SRV-ATTR:host": "host2"})
        engine.target = mock.Mock(return_value=("host2", before))
        engine.action = mock.Mock()
        engine.describe = mock.Mock(return_value=after)
        target, original = engine.request_live_migration(vm, scheduler=True)
        self.assertIsNone(target)
        self.assertIs(before, original)
        engine.action.assert_called_once_with(
            vm,
            {"os-migrateLive": {"host": None, "block_migration": False}},
            self.admin,
        )
        self.assertEqual(after, engine.wait_migration(vm, target, original))
        engine.describe.return_value = dict(
            after, **{"OS-EXT-AZ:availability_zone": "az2"}
        )
        with self.assertRaisesRegex(AssertionError, "availability zone"):
            engine.wait_migration(vm, target, original)

    def test_drain_requires_an_enabled_target_in_the_source_az(self):
        engine = self.engine()
        self.cloud.session.auth.get_access.return_value.role_names = [
            "member"
        ]
        engine.options = dict(
            self.cfg["scenarios"]["nova-drain"], host="host1"
        )
        engine.vm = mock.Mock()
        self.admin.list.return_value = [
            {
                "zoneName": "az1",
                "hosts": {"host1": {"nova-compute": {"active": True}}},
            }
        ]
        with (
            mock.patch(
                "ultimum_validation.scenarios.exclusive_host",
                return_value=[],
            ),
            mock.patch(
                "ultimum_validation.scenarios.host_service",
                return_value={"zone": "az1"},
            ),
        ):
            with self.assertRaisesRegex(config.InvalidError, "No other"):
                engine.nova_drain()
        engine.vm.assert_not_called()

    def test_drain_admin_token_uses_test_project_without_elevating_user(self):
        self.admin.rc = {
            "OS_AUTH_URL": "https://identity.example/v3",
            "OS_USERNAME": "operator",
            "OS_PASSWORD": "test-password",
            "OS_PROJECT_NAME": "admin-project",
            "OS_PROJECT_DOMAIN_NAME": "Default",
        }
        self.admin.versions = {"compute": "2.74"}
        scoped = mock.Mock(project_id="test-project")
        scoped.session.auth.get_access.return_value.role_names = ["admin"]
        with (
            mock.patch(
                "ultimum_validation.prepare.session_from_rc",
                return_value=mock.Mock(),
            ) as session,
            mock.patch(
                "ultimum_validation.prepare.Cloud", return_value=scoped
            ),
        ):
            self.assertIs(
                scoped,
                prepare.project_scoped_admin(self.admin, "test-project"),
            )
        rc = session.call_args.args[0]
        self.assertEqual("test-project", rc["OS_PROJECT_ID"])
        self.assertNotIn("OS_PROJECT_NAME", rc)
        self.assertEqual("admin-project", self.admin.rc["OS_PROJECT_NAME"])
        self.assertEqual(["member"], self.cfg["identity"]["user"]["roles"])
        self.assertEqual({"compute": "2.74"}, scoped.versions)

    def test_drain_admin_requires_role_in_test_project(self):
        self.admin.rc = {"OS_AUTH_URL": "https://identity.example/v3"}
        self.admin.versions = {}
        scoped = mock.Mock(project_id="test-project")
        scoped.session.auth.get_access.return_value.role_names = ["member"]
        with (
            mock.patch(
                "ultimum_validation.prepare.session_from_rc",
                return_value=mock.Mock(),
            ),
            mock.patch(
                "ultimum_validation.prepare.Cloud", return_value=scoped
            ),
        ):
            with self.assertRaisesRegex(config.InvalidError, "admin role"):
                prepare.project_scoped_admin(self.admin, "test-project")

    def test_drain_rejects_existing_test_user_admin_role(self):
        engine = self.engine()
        self.cloud.session.auth.get_access.return_value.role_names = ["admin"]
        with self.assertRaisesRegex(config.InvalidError, "revoke it"):
            engine.project_host_admin()
        self.admin.request.assert_not_called()

    def test_host_directed_server_uses_project_admin_without_keypair(self):
        engine = self.engine()
        engine.access = mock.Mock()
        self.cfg["compute"]["boot_from_volume"] = False
        creator = mock.Mock(project_id="test-project")
        creator.post.return_value = {"server": {"id": "drain-vm"}}
        self.cloud.wait_status.return_value = {
            "id": "drain-vm", "status": "ACTIVE"
        }
        engine.server(
            host="host1", creator=creator, inject_keypair=False
        )
        body = creator.post.call_args.args[2]["server"]
        self.assertEqual("host1", body["host"])
        self.assertNotIn("key_name", body)
        self.cloud.post.assert_not_called()
        engine.access.reset_mock()
        creator.project_id = "another-project"
        with self.assertRaisesRegex(config.InvalidError, "another project"):
            engine.server(host="host1", creator=creator)
        engine.access.assert_not_called()

    def test_reconciliation_failure_still_restores_host_service(self):
        engine = self.engine()
        engine.reconcile_intents = mock.Mock(
            side_effect=APIError("network", 503)
        )
        engine.restore_services = mock.Mock()
        with self.assertRaisesRegex(config.InvalidError, "Cleanup incomplete"):
            engine.cleanup()
        engine.restore_services.assert_called_once_with()

    def test_failed_intent_is_searched_again_on_later_cleanup(self):
        engine = self.engine()
        self.cloud.post.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            engine.create("network", "/ports", "port", {"name": "own"})
        with self.assertRaisesRegex(config.InvalidError, "Unconfirmed"):
            engine.cleanup()
        self.assertFalse(self.ledger.data["intents"][0]["resolved"])
        self.cloud.list.side_effect = lambda service, path, key, **kw: (
            [
                {
                    "id": "late",
                    "name": "own",
                    "description": "Ultimum run " + self.run_id,
                },
            ]
            if path == "/ports"
            else []
        )
        engine.cleanup()
        self.cloud.delete.assert_called_once_with("network", "/ports/late")


class PrepareTest(RunnerCase):
    def network_api(self):
        records = {"networks": [], "subnets": [], "routers": [], "ports": []}
        self.cloud.list.side_effect = lambda service, path, key, **kwargs: (
            copy.deepcopy(records[key])
        )

        def create(service, path, body):
            kind, values = next(iter(body.items()))
            obj = dict(values, id=kind + "-id", project_id="test-project")
            records[kind + "s"].append(obj)
            return {kind: copy.deepcopy(obj)}

        def request(service, method, path, body):
            self.assertEqual("PUT", method)
            self.assertTrue(path.endswith("/add_router_interface"))
            records["ports"].append(
                {
                    "fixed_ips": [
                        {
                            "subnet_id": body["subnet_id"],
                        }
                    ]
                }
            )

        self.cloud.post.side_effect = create
        self.cloud.request.side_effect = request
        return records

    def test_create_network_resources_is_idempotent_with_prefix_names(self):
        records = self.network_api()
        base = {"external_network_id": "external"}
        first = prepare.prepare_network(self.cfg, self.cloud, base)
        self.assertEqual(3, self.cloud.post.call_count)
        self.assertEqual(1, self.cloud.request.call_count)
        self.assertEqual("e2e-net", records["networks"][0]["name"])
        self.assertEqual("e2e-subnet", records["subnets"][0]["name"])
        self.assertEqual("e2e-router", records["routers"][0]["name"])
        self.assertEqual(
            first, prepare.prepare_network(self.cfg, self.cloud, base)
        )
        self.assertEqual(3, self.cloud.post.call_count)
        self.assertEqual(1, self.cloud.request.call_count)

    def test_existing_resources_resolved_by_name_without_writes(self):
        records = self.network_api()
        base = {"external_network_id": "external"}
        prepare.prepare_network(self.cfg, self.cloud, base)
        self.cloud.post.reset_mock()
        self.cloud.request.reset_mock()
        for kind in ("network", "subnet", "router"):
            self.cfg["create_" + kind] = False
            records[kind + "s"][0].pop("description")
        result = prepare.prepare_network(self.cfg, self.cloud, base)
        self.assertEqual("router-id", result["router_id"])
        self.cloud.post.assert_not_called()
        self.cloud.request.assert_not_called()

    def test_new_router_external_pool_keeps_tenant_subnet_automatic(self):
        records = self.network_api()
        self.admin.list.return_value = [
            {
                "fixed_ips": [
                    {
                        "subnet_id": "public-subnet",
                        "ip_address": "92.119.67.128",
                    }
                ]
            }
        ]
        pool = {
            "network_id": "external",
            "subnet_id": "public-subnet",
            "start": "92.119.67.128",
            "end": "92.119.67.250",
            "reserved": [],
        }
        base = {"external_network_id": "external", "external_ip_pool": pool}
        first = prepare.prepare_network(self.cfg, self.cloud, base, self.admin)
        self.assertEqual(
            [{"subnet_id": "public-subnet", "ip_address": "92.119.67.129"}],
            records["routers"][0]["external_gateway_info"][
                "external_fixed_ips"
            ],
        )
        subnet = records["subnets"][0]
        self.assertEqual("10.240.0.0/24", subnet["cidr"])
        self.assertEqual(
            [{"start": "10.240.0.100", "end": "10.240.0.220"}],
            subnet["allocation_pools"],
        )
        self.assertEqual(
            first,
            prepare.prepare_network(self.cfg, self.cloud, base, self.admin),
        )
        self.assertEqual(3, self.cloud.post.call_count)
        self.admin.post.assert_not_called()

    def test_existing_unmanaged_router_outside_external_pool_is_read_only(
        self,
    ):
        records = self.network_api()
        base = {"external_network_id": "external"}
        prepare.prepare_network(self.cfg, self.cloud, base)
        records["routers"][0]["external_gateway_info"][
            "external_fixed_ips"
        ] = [{"subnet_id": "public-subnet", "ip_address": "92.119.67.5"}]
        base["external_ip_pool"] = {
            "network_id": "external",
            "subnet_id": "public-subnet",
            "start": "92.119.67.128",
            "end": "92.119.67.250",
            "reserved": [],
        }
        self.cloud.post.reset_mock()
        self.cloud.request.reset_mock()
        with self.assertRaisesRegex(
            config.InvalidError, "outside external_ip_pool"
        ):
            prepare.prepare_network(self.cfg, self.cloud, base, self.admin)
        self.cfg["create_router"] = False
        prepare.prepare_network(self.cfg, self.cloud, base, self.admin)
        self.cloud.post.assert_not_called()
        self.cloud.request.assert_not_called()
        self.admin.list.assert_not_called()

    def test_new_router_exact_address_policy_denial_has_no_fallback(self):
        self.network_api()
        create = self.cloud.post.side_effect

        def deny_router(service, path, body):
            if path == "/routers":
                raise APIError("network", 403, "req-router")
            return create(service, path, body)

        self.cloud.post.side_effect = deny_router
        self.admin.list.return_value = []
        base = {
            "external_network_id": "external",
            "external_ip_pool": {
                "network_id": "external",
                "subnet_id": "public-subnet",
                "start": "92.119.67.128",
                "end": "92.119.67.250",
                "reserved": [],
            },
        }
        with self.assertRaisesRegex(
            config.InvalidError,
            "create_router:external_gateway_info:external_fixed_ips",
        ):
            prepare.prepare_network(self.cfg, self.cloud, base, self.admin)
        self.assertEqual(3, self.cloud.post.call_count)
        self.admin.post.assert_not_called()
        self.cloud.request.assert_not_called()

    def test_independent_flags_allow_new_router_on_existing_network(self):
        records = self.network_api()
        records["networks"] = [
            {
                "id": "existing-net",
                "name": "e2e-net",
                "project_id": "test-project",
            }
        ]
        records["subnets"] = [
            {
                "id": "existing-subnet",
                "name": "e2e-subnet",
                "project_id": "test-project",
                "network_id": "existing-net",
                "cidr": "10.240.0.0/24",
            }
        ]
        self.cfg["create_network"] = False
        self.cfg["create_subnet"] = False
        result = prepare.prepare_network(
            self.cfg,
            self.cloud,
            {"external_network_id": "external"},
        )
        self.assertEqual("existing-subnet", result["subnet_id"])
        self.assertEqual(1, self.cloud.post.call_count)
        self.assertEqual("/routers", self.cloud.post.call_args.args[1])
        self.assertEqual(1, self.cloud.request.call_count)

    def test_missing_existing_router_stops_before_any_network_creation(self):
        self.network_api()
        self.cfg["create_router"] = False
        with self.assertRaisesRegex(
            config.InvalidError, "create_router=false"
        ):
            prepare.prepare_network(
                self.cfg,
                self.cloud,
                {"external_network_id": "external"},
            )
        self.cloud.post.assert_not_called()
        self.cloud.request.assert_not_called()

    def test_existing_router_without_interface_is_never_modified(self):
        records = self.network_api()
        base = {"external_network_id": "external"}
        prepare.prepare_network(self.cfg, self.cloud, base)
        self.cloud.post.reset_mock()
        self.cloud.request.reset_mock()
        records["ports"] = []
        self.cfg["create_router"] = False
        with self.assertRaisesRegex(config.InvalidError, "existing interface"):
            prepare.prepare_network(self.cfg, self.cloud, base)
        self.cloud.post.assert_not_called()
        self.cloud.request.assert_not_called()

    def test_missing_project_is_not_created_when_disabled(self):
        self.cfg["create_project"] = False
        self.admin.list.side_effect = lambda service, path, key, **kwargs: (
            [{"id": "domain", "name": "Default"}] if path == "/domains" else []
        )
        with self.assertRaisesRegex(
            config.InvalidError, "creation is disabled"
        ):
            prepare.ensure_identity(self.cfg, self.admin)
        self.admin.post.assert_not_called()
        self.admin.request.assert_not_called()

    def test_existing_password_not_changed_and_roles_are_project_scoped(self):
        records = {
            "/domains": [{"id": "domain", "name": "Default"}],
            "/projects": [
                {
                    "id": "project",
                    "name": self.cfg["identity"]["project"]["name"],
                    "domain_id": "domain",
                }
            ],
            "/users": [
                {
                    "id": "user",
                    "name": self.cfg["identity"]["user"]["name"],
                    "domain_id": "domain",
                }
            ],
            "/roles": [{"id": "role", "name": "member"}],
        }
        self.admin.list.side_effect = lambda service, path, key, **kwargs: (
            records[path]
        )
        self.assertEqual(
            "project", prepare.ensure_identity(self.cfg, self.admin)
        )
        self.admin.post.assert_not_called()
        self.admin.request.assert_called_once_with(
            "identity", "PUT", "/projects/project/users/user/roles/role"
        )

    def test_host_scenarios_assign_project_admin_role_to_openrc_user(self):
        records = {
            "/domains": [{"id": "domain", "name": "Default"}],
            "/projects": [
                {
                    "id": "project",
                    "name": self.cfg["identity"]["project"]["name"],
                    "domain_id": "domain",
                }
            ],
            "/users": [
                {
                    "id": "test-user",
                    "name": self.cfg["identity"]["user"]["name"],
                    "domain_id": "domain",
                }
            ],
            "/roles": [
                {"id": "member-role", "name": "member"},
                {"id": "admin-role", "name": "admin"},
            ],
        }
        self.admin.list.side_effect = lambda service, path, key, **kwargs: (
            records[path]
        )
        self.admin.session.get_user_id.return_value = "openrc-admin"
        for scenario in ("nova-drain", "nova-evacuate"):
            with self.subTest(scenario=scenario):
                self.cfg["scenarios"][scenario]["enabled"] = True
                self.admin.request.reset_mock()
                self.assertEqual(
                    "project", prepare.ensure_identity(self.cfg, self.admin)
                )
                self.assertEqual(
                    [
                        mock.call(
                            "identity",
                            "PUT",
                            "/projects/project/users/test-user/roles/"
                            "member-role",
                        ),
                        mock.call(
                            "identity",
                            "PUT",
                            "/projects/project/users/openrc-admin/roles/"
                            "admin-role",
                        ),
                    ],
                    self.admin.request.call_args_list,
                )
                self.cfg["scenarios"][scenario]["enabled"] = False

    def test_host_scenario_never_grants_admin_role_to_test_user(self):
        self.cfg["scenarios"]["nova-drain"]["enabled"] = True
        self.admin.list.side_effect = lambda service, path, key, **kwargs: {
            "/domains": [{"id": "domain", "name": "Default"}],
            "/projects": [
                {
                    "id": "project",
                    "name": self.cfg["identity"]["project"]["name"],
                    "domain_id": "domain",
                }
            ],
            "/users": [
                {
                    "id": "same-user",
                    "name": self.cfg["identity"]["user"]["name"],
                    "domain_id": "domain",
                }
            ],
            "/roles": [
                {"id": "member-role", "name": "member"},
                {"id": "admin-role", "name": "admin"},
            ],
        }[path]
        self.admin.session.get_user_id.return_value = "same-user"
        with self.assertRaisesRegex(config.InvalidError, "must differ"):
            prepare.ensure_identity(self.cfg, self.admin)
        self.admin.request.assert_not_called()

    def test_quotas_disabled_or_apply_only_explicit_keys(self):
        prepare.set_quotas(self.cfg, self.admin, "project")
        self.admin.request.assert_not_called()
        self.cfg["quotas"].update(
            apply=True, nova={"instances": 50}, cinder={"gigabytes": 500}
        )
        prepare.set_quotas(self.cfg, self.admin, "project")
        self.assertEqual(2, self.admin.request.call_count)
        self.admin.request.assert_any_call(
            "compute",
            "PUT",
            "/os-quota-sets/project",
            {"quota_set": {"instances": 50}},
        )

    def test_existing_network_is_read_only(self):
        network = self.cfg["network"]
        for kind in ("network", "subnet", "router"):
            self.cfg["create_" + kind] = False
        for part, value in (
            ("tenant_network", "network"),
            ("subnet", "subnet"),
            ("router", "router"),
        ):
            network[part]["id"] = value
        records = {
            "/networks/network": {"network": {"id": "network"}},
            "/subnets/subnet": {
                "subnet": {
                    "id": "subnet",
                    "network_id": "network",
                    "cidr": "10.240.0.0/24",
                }
            },
            "/routers/router": {
                "router": {
                    "id": "router",
                    "external_gateway_info": {"network_id": "external"},
                }
            },
        }
        self.cloud.get.side_effect = lambda service, path: records[path]
        self.cloud.list.return_value = [
            {"fixed_ips": [{"subnet_id": "subnet"}]}
        ]
        result = prepare.prepare_network(
            self.cfg, self.cloud, {"external_network_id": "external"}
        )
        self.assertEqual("router", result["router_id"])
        self.cloud.post.assert_not_called()
        self.cloud.request.assert_not_called()

    def test_unmanaged_name_collision_rejected(self):
        self.cloud.list.return_value = [
            {
                "name": self.cfg["network"]["tenant_network"]["name"],
                "id": "foreign",
                "project_id": "test-project",
            }
        ]
        with self.assertRaisesRegex(config.InvalidError, "unmanaged"):
            prepare.prepare_network(
                self.cfg, self.cloud, {"external_network_id": "external"}
            )
        self.cloud.post.assert_not_called()

    def test_read_only_check_never_calls_prepare(self):
        path = self.directory / "config.yaml"
        path.write_text(yaml.safe_dump(self.cfg))
        with (
            mock.patch.object(cli, "openrc", return_value={}),
            mock.patch.object(
                cli, "clouds", return_value=(self.admin, self.cloud)
            ),
            mock.patch.object(cli, "preflight", return_value={}),
            mock.patch.object(cli, "ensure_ssh_key") as ssh_key,
            mock.patch.object(cli, "prepare") as provision,
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            self.assertEqual(
                0, cli.main(["--config", str(path), "check", "placement"])
            )
            provision.assert_not_called()
            self.assertTrue(ssh_key.call_args.kwargs["read_only"])


class SSHKeyTest(RunnerCase):
    def setUp(self):
        super().setUp()
        self.private = self.directory / "keys" / "e2e-key"
        self.public = self.private.with_suffix(".pub")
        self.cfg["guest"]["ssh_private_key"] = str(self.private)
        self.cfg["guest"]["ssh_public_key"] = str(self.public)
        self.cloud.get.side_effect = APIError("compute", 404)
        self.cloud.post.side_effect = lambda service, path, body: body

    def generate(self):
        return keys.ensure_ssh_key(self.cfg, self.cloud)

    def reuse_cloud_key(self):
        self.cloud.get.side_effect = None
        self.cloud.get.return_value = {
            "keypair": {"public_key": self.public.read_text()}
        }
        self.cloud.post.reset_mock()

    def test_generate_import_and_reuse_without_replacing_material(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual("e2e-key", self.generate())
            self.assertEqual(3072, keys.read_private(self.private).get_bits())
            before = self.private.read_bytes()
            self.reuse_cloud_key()
            self.generate()
        self.assertEqual(before, self.private.read_bytes())
        self.assertEqual(0o600, self.private.stat().st_mode & 0o777)
        self.assertEqual(0o600, self.public.stat().st_mode & 0o777)
        self.cloud.post.assert_not_called()
        self.assertIn("CREATE SSH private key", output.getvalue())
        self.assertIn("REUSE SSH private key", output.getvalue())
        self.assertNotIn("BEGIN RSA PRIVATE", output.getvalue())

    def test_false_reuses_but_never_creates_local_or_remote_key(self):
        self.cfg["create_ssh_key"] = False
        with self.assertRaises(config.InvalidError):
            self.generate()
        self.assertFalse(self.private.parent.exists())
        self.cloud.post.assert_not_called()
        self.cfg["create_ssh_key"] = True
        self.generate()
        self.cfg["create_ssh_key"] = False
        self.cloud.post.reset_mock()
        with self.assertRaisesRegex(config.InvalidError, "Nova keypair"):
            self.generate()
        self.cloud.post.assert_not_called()
        self.reuse_cloud_key()
        self.generate()
        self.cloud.post.assert_not_called()

    def test_read_only_check_never_generates_a_key(self):
        with self.assertRaises(config.InvalidError):
            keys.ensure_ssh_key(self.cfg, self.cloud, read_only=True)
        self.assertFalse(self.private.parent.exists())
        self.cloud.post.assert_not_called()

    def test_public_file_can_be_recovered_but_private_is_never_replaced(self):
        self.generate()
        private = self.private.read_bytes()
        public = self.public.read_bytes()
        self.reuse_cloud_key()
        self.public.unlink()
        self.generate()
        self.assertEqual(private, self.private.read_bytes())
        self.assertEqual(public, self.public.read_bytes())
        self.private.unlink()
        with self.assertRaisesRegex(
            config.InvalidError, "without its private"
        ):
            self.generate()
        self.assertFalse(self.private.exists())

    def test_local_key_mismatch_rejected_without_cloud_mutation(self):
        self.generate()
        self.cloud.post.reset_mock()
        self.public.write_text("ssh-rsa wrong-key")
        with self.assertRaisesRegex(config.InvalidError, "do not match"):
            self.generate()
        self.cloud.post.assert_not_called()

    def test_mismatched_nova_key_is_never_overwritten(self):
        self.generate()
        self.reuse_cloud_key()
        self.cloud.get.return_value["keypair"]["public_key"] = "ssh-rsa wrong"
        with self.assertRaisesRegex(config.InvalidError, "does not match"):
            self.generate()
        self.cloud.post.assert_not_called()
        self.cloud.delete.assert_not_called()

    def test_concurrent_keypair_creation_rechecks_matching_public_key(self):
        self.generate()
        self.cloud.get.side_effect = [
            APIError("compute", 404),
            {"keypair": {"public_key": self.public.read_text()}},
        ]
        self.cloud.post.side_effect = APIError("compute", 409)
        self.generate()

    def test_configurable_named_key_with_existing_paths(self):
        path = self.directory / "config.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "create_ssh_key": False,
                    "ssh_key": {"name": "my-key"},
                    "guest": {"ssh_private_key": str(self.private)},
                }
            )
        )
        loaded = config.load(path)
        self.assertEqual("my-key", loaded["ssh_key"]["name"])
        self.assertEqual(
            str(self.private) + ".pub", loaded["guest"]["ssh_public_key"]
        )
        self.cfg["create_ssh_key"] = True
        config.validate(self.cfg)  # Missing keys are provisionable.
        self.cfg["create_ssh_key"] = False
        with self.assertRaisesRegex(config.InvalidError, "Missing guest"):
            config.validate(self.cfg)

    def test_prepared_keypair_is_not_run_owned_or_deleted_by_cleanup(self):
        engine = self.engine()
        engine.security_group = mock.Mock(return_value="group")
        engine.rule = mock.Mock()
        engine.access()
        self.assertEqual("e2e-key", engine.keypair)
        self.assertFalse(self.ledger.data["resources"])
        engine.cleanup()
        self.cloud.post.assert_not_called()
        self.cloud.delete.assert_not_called()


class BootstrapTest(RunnerCase):
    def test_layout_creation_preserves_existing_config_and_keys(self):
        config_path = self.directory / "etc/ultimum/ultimum.yaml"
        data = self.directory / "data/ultimum"
        bootstrap.initialize(config_path, data)
        config_path.write_text("customized: configuration\n")
        (data / "keys/existing").write_text("untouched")
        bootstrap.initialize(config_path, data)
        self.assertEqual(
            "customized: configuration\n", config_path.read_text()
        )
        self.assertEqual("untouched", (data / "keys/existing").read_text())
        self.assertEqual(0o600, config_path.stat().st_mode & 0o777)
        for name in ("home", "keys", "db", "state", "results"):
            self.assertTrue((data / name).is_dir())

    def test_database_copied_once_preserving_old_and_new_history(self):
        old = self.directory / "old.sqlite"
        with contextlib.closing(sqlite3.connect(old)) as conn:
            conn.execute("CREATE TABLE history (value TEXT)")
            conn.execute("INSERT INTO history VALUES ('old-task')")
            conn.commit()
        path = self.directory / "etc/ultimum.yaml"
        data = self.directory / "data"
        bootstrap.initialize(path, data, old)
        target = data / "db/rally.sqlite"
        with contextlib.closing(sqlite3.connect(target)) as conn:
            self.assertEqual(
                [("old-task",)],
                conn.execute("SELECT * FROM history").fetchall(),
            )
            conn.execute("INSERT INTO history VALUES ('new-task')")
            conn.commit()
        bootstrap.initialize(path, data, old)
        with contextlib.closing(sqlite3.connect(target)) as conn:
            self.assertEqual(
                2, conn.execute("SELECT COUNT(*) FROM history").fetchone()[0]
            )
        with contextlib.closing(sqlite3.connect(old)) as conn:
            self.assertEqual(
                1, conn.execute("SELECT COUNT(*) FROM history").fetchone()[0]
            )


class ProgressTest(RunnerCase):
    def test_step_reports_start_completion_and_failure_and_keeps_events(self):
        engine = self.engine()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            with engine.step("example operation"):
                pass
            with self.assertRaises(AssertionError):
                with engine.step("failing operation"):
                    raise AssertionError("private diagnostic")
        text = output.getvalue()
        self.assertIn("START example operation", text)
        self.assertIn("OK example operation", text)
        self.assertIn("FAIL failing operation", text)
        self.assertNotIn("private diagnostic", text)
        self.assertEqual(4, len(Ledger(self.ledger.path).data["events"]))

    def test_long_operation_reports_heartbeat_and_thread_stops(self):
        output = io.StringIO()
        threads = set(threading.enumerate())
        with contextlib.redirect_stdout(output):
            progress.progress("Waiting for VM", "test")
            with progress.watch(interval=0.01):
                deadline = time.monotonic() + 2
                while (
                    "WAIT" not in output.getvalue()
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.01)
        self.assertIn("Waiting for VM", output.getvalue())
        self.assertIn("WAIT", output.getvalue())
        self.assertEqual(threads, set(threading.enumerate()))

    def test_progress_reaches_pipe_before_process_finishes_without_tty(self):
        code = (
            f"import sys; sys.path.insert(0, {str(ROOT / 'ultimum')!r}); "
            "from ultimum_validation.progress import progress; "
            "progress('ready', 'child'); input()"
        )
        with subprocess.Popen(
            [sys.executable, "-c", code],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        ) as child:
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(child.stdout, selectors.EVENT_READ)
                    self.assertTrue(
                        selector.select(timeout=5), "Output buffered"
                    )
                self.assertIn("[child] ready", child.stdout.readline())
                self.assertIsNone(child.poll())
            finally:
                child.communicate("\n", timeout=5)


class CloudTest(unittest.TestCase):
    def test_pagination_and_authority_restriction(self):
        cloud = Cloud(mock.Mock(), {})
        cloud.endpoints["compute"] = "https://compute.invalid/v2.1/project"
        cloud.get = mock.Mock(
            side_effect=[
                {
                    "servers": [{"id": "1"}],
                    "servers_links": [
                        {
                            "rel": "next",
                            "href": (
                                "https://compute.invalid/v2.1/project/serve"
                                "rs/detail?marker=1"
                            ),
                        }
                    ],
                },
                {"servers": [{"id": "2"}]},
            ]
        )
        self.assertEqual(
            [{"id": "1"}, {"id": "2"}],
            cloud.list("compute", "/servers/detail", "servers"),
        )
        cloud.get = mock.Mock(
            return_value={
                "servers": [],
                "servers_links": [
                    {
                        "rel": "next",
                        "href": "https://foreign.invalid/steal-token",
                    }
                ],
            }
        )
        with self.assertRaisesRegex(config.InvalidError, "outside"):
            cloud.list("compute", "/servers/detail", "servers")

    def test_version_discovery_preserves_reverse_proxy_prefix(self):
        session = mock.Mock()
        session.get.return_value.status_code = 200
        session.get.return_value.json.return_value = {
            "version": {"version": "2.90"}
        }
        cloud = Cloud(session, {})
        cloud.endpoints["compute"] = (
            "https://cloud.invalid/compute/v2.1/project"
        )
        self.assertEqual("2.90", cloud.require_version("compute", "2.77"))
        session.get.assert_called_once_with(
            "https://cloud.invalid/compute/v2.1", raise_exc=False
        )
        with self.assertRaises(config.UnsupportedError):
            cloud.require_version("compute", "2.91")

    def test_request_error_does_not_expose_response_body(self):
        session = mock.Mock()
        response = session.request.return_value
        response.status_code = 403
        response.headers = {"x-openstack-request-id": "req-123"}
        response.text = "password=private"
        cloud = Cloud(session, {})
        cloud.endpoints["compute"] = "https://example.invalid/v2.1"
        with self.assertRaises(APIError) as ctx:
            cloud.post("compute", "/servers", {})
        self.assertIn("req-123", str(ctx.exception))
        self.assertNotIn("private", str(ctx.exception))


class TPMCloudInitTest(RunnerCase):
    def setUp(self):
        super().setUp()
        self.cfg["compute"]["boot_from_volume"] = False
        self.runtime["tpm_flavor_id"] = "tpm-flavor"
        self.engine = self.engine()
        self.engine.options = self.cfg["scenarios"]["nova-live-migration-tpm"]
        self.engine.access = mock.Mock()
        self.engine.keypair = self.runtime["keypair_name"]
        self.engine.ssh_group = "ssh-group"
        self.engine.port = mock.Mock(return_value={"id": "port"})
        self.engine.floating = mock.Mock(
            return_value={"floating_ip_address": "192.0.2.2"}
        )
        self.engine.verify_ssh_access = mock.Mock()
        self.engine.migrate = mock.Mock()
        self.cloud.post.return_value = {"server": {"id": "server"}}
        self.cloud.wait_status.return_value = {"id": "server"}
        self.guest = Guest(
            "192.0.2.2", "server", self.cfg["guest"], self.ledger
        )
        self.guest.connect = mock.Mock(return_value=self.guest)
        self.guest.command = mock.Mock()
        patcher = mock.patch(
            "ultimum_validation.resources.Guest", return_value=self.guest
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_tpm_installs_via_cloud_init_before_tools_and_migration(self):
        payload = bytes(range(self.engine.options["payload_bytes"]))
        commands = []

        def command(value, *args, **kwargs):
            if value == "command -v tpm2_nvdefine":
                self.assertIn("sudo -n cloud-init status --wait", commands)
            commands.append(value)
            if value.startswith("sudo -n tpm2_nvread"):
                return base64.b64encode(payload).decode()
            return ""

        self.guest.command.side_effect = command
        self.assertFalse(self.cfg["guest"]["install_missing_packages"])
        with mock.patch("ultimum_validation.scenarios.os") as random:
            random.urandom.return_value = payload
            self.engine.nova_live_migration_tpm()
        body = self.cloud.post.call_args.args[2]["server"]
        userdata = base64.b64decode(body["user_data"]).decode()
        self.assertTrue(userdata.startswith("#cloud-config\n"))
        cloud_config = yaml.safe_load(userdata)
        self.assertTrue(cloud_config["package_update"])
        self.assertIn("tpm2-tools", cloud_config["packages"])
        self.assertEqual("tpm-flavor", body["flavorRef"])
        self.guest.command.assert_any_call(
            "sudo -n cloud-init status --wait",
            self.cfg["guest"]["cloud_init_timeout_seconds"],
        )
        self.assertFalse(any("apt-get" in c for c in commands))
        self.engine.migrate.assert_called_once_with(
            {"id": "server"}, self.engine.options["target_host"]
        )
        self.assertTrue(self.ledger.data["evidence"]["tpm"]["identical"])

    def test_cloud_init_failure_stops_tpm_operations_and_migration(self):
        self.guest.command.side_effect = AssertionError("cloud-init failed")
        with self.assertRaisesRegex(AssertionError, "cloud-init failed"):
            self.engine.nova_live_migration_tpm()
        self.guest.command.assert_called_once_with(
            "sudo -n cloud-init status --wait",
            self.cfg["guest"]["cloud_init_timeout_seconds"],
        )
        self.engine.migrate.assert_not_called()
        self.assertNotIn("tpm", self.ledger.data["evidence"])


class NovaEvacuateTest(RunnerCase):
    def setUp(self):
        super().setUp()
        self.cfg["scenarios"]["nova-evacuate"].update(
            enabled=True, host="compute-test-01"
        )
        self.engine = self.engine()
        self.engine.options = self.cfg["scenarios"]["nova-evacuate"]
        self.placement_admin = mock.Mock(project_id="test-project")
        self.engine.project_host_admin = mock.Mock(
            return_value=self.placement_admin
        )
        self.engine.host_test_userdata = mock.Mock(
            return_value="#cloud-config\nuser: {}\n"
        )
        self.ledger.data["config_path"] = "/etc/ultimum/ultimum.yaml"
        self.ledger.save()
        self.server = {"id": "evacuation-vm"}
        self.old_guest = mock.Mock()
        self.new_guest = mock.Mock()
        self.new_guest.command.return_value = self.run_id
        self.engine.vm = mock.Mock(
            return_value=(
                self.server,
                {"id": "port"},
                {"floating_ip_address": "192.0.2.2"},
                self.old_guest,
            )
        )
        self.engine.connect = mock.Mock(return_value=self.new_guest)
        self.engine.target = mock.Mock(return_value=("compute-test-02", {}))
        self.service = {"state": "up", "zone": "az1"}

        def describe(server):
            return {
                "id": server["id"],
                "status": "ACTIVE",
                "tenant_id": "test-project",
                "OS-EXT-AZ:availability_zone": "az1",
                "OS-EXT-SRV-ATTR:host": (
                    "compute-test-02"
                    if self.admin.post.called
                    else "compute-test-01"
                ),
            }

        self.engine.describe = mock.Mock(side_effect=describe)

    def execute(self, host_goes_down=True):
        marker = self.ledger.path.parent / "fault-start.json"

        def waiting(
            fetch, ready, timeout, interval=2, description="operation"
        ):
            if description == "operator fencing confirmation":
                self.assertEqual(
                    "WAITING_FOR_FAULT", self.ledger.data["status"]
                )
                self.admin.post.assert_not_called()
                marker.write_text(json.dumps({"epoch": time.time()}))
            elif description == "fenced Nova compute service down":
                self.admin.post.assert_not_called()
                if not host_goes_down:
                    raise TimeoutError("compute did not go down")
                self.service["state"] = "down"
            return wait_for(
                fetch, ready, timeout, interval=0, description=description
            )

        with (
            mock.patch(
                "ultimum_validation.scenarios.exclusive_host",
                return_value=[],
            ) as exclusive,
            mock.patch(
                "ultimum_validation.scenarios.host_service",
                return_value=self.service,
            ),
            mock.patch(
                "ultimum_validation.scenarios.wait_for", side_effect=waiting
            ),
        ):
            self.engine.nova_evacuate()
        return exclusive

    def test_sends_admin_evacuate_only_after_host_is_fenced_and_down(self):
        exclusive = self.execute()
        self.engine.project_host_admin.assert_called_once_with()
        self.engine.vm.assert_called_once_with(
            host="compute-test-01",
            az="az1",
            label="evacuate",
            creator=self.placement_admin,
            inject_keypair=False,
            userdata="#cloud-config\nuser: {}\n",
        )
        self.old_guest.write.assert_called_once_with(
            "/var/tmp/ultimum-evacuate", self.run_id
        )
        self.old_guest.command.assert_called_once_with("sync")
        self.engine.target.assert_called_once_with(self.server)
        self.admin.post.assert_called_once_with(
            "compute", "/servers/evacuation-vm/action", {"evacuate": {}}
        )
        self.cloud.post.assert_not_called()
        exclusive.assert_any_call(
            self.admin, "compute-test-01", ["evacuation-vm"]
        )
        self.engine.connect.assert_called_once_with(
            self.server, {"floating_ip_address": "192.0.2.2"}
        )
        self.new_guest.command.assert_called_once_with(
            "cat /var/tmp/ultimum-evacuate"
        )
        self.assertEqual(
            {
                "source": "compute-test-01",
                "targets": {"evacuation-vm": "compute-test-02"},
                "data_preserved": True,
            },
            self.ledger.data["evidence"]["nova_evacuation"],
        )

    def test_refuses_evacuate_vm_in_another_project(self):
        self.engine.describe.side_effect = lambda server: {
            "id": server["id"],
            "status": "ACTIVE",
            "tenant_id": "another-project",
            "OS-EXT-AZ:availability_zone": "az1",
            "OS-EXT-SRV-ATTR:host": "compute-test-01",
        }
        with self.assertRaisesRegex(AssertionError, "another project"):
            self.execute()
        self.admin.post.assert_not_called()

    def test_refuses_evacuate_while_source_service_is_up(self):
        with self.assertRaisesRegex(TimeoutError, "did not go down"):
            self.execute(host_goes_down=False)
        self.admin.post.assert_not_called()
        self.engine.connect.assert_not_called()

    def test_requires_destination_before_asking_for_host_outage(self):
        self.engine.target.side_effect = config.InvalidError(
            "No other host in the availability zone"
        )
        with self.assertRaisesRegex(config.InvalidError, "No other host"):
            self.execute()
        self.assertFalse(
            (self.ledger.path.parent / "fault-start.json").exists()
        )
        self.admin.post.assert_not_called()

    def test_detects_automatic_recovery_before_manual_evacuate(self):
        calls = 0

        def describe(server):
            nonlocal calls
            calls += 1
            return {
                "id": server["id"],
                "status": "ACTIVE",
                "tenant_id": "test-project",
                "OS-EXT-AZ:availability_zone": "az1",
                "OS-EXT-SRV-ATTR:host": (
                    "compute-test-01" if calls == 1 else "compute-test-02"
                ),
            }

        self.engine.describe.side_effect = describe
        with self.assertRaisesRegex(config.InvalidError, "automatic HA"):
            self.execute()
        self.admin.post.assert_not_called()

    def test_does_not_pass_when_evacuated_vm_loses_data(self):
        self.new_guest.command.return_value = "different data"
        with self.assertRaisesRegex(AssertionError, "lost"):
            self.execute()
        self.assertNotIn("nova_evacuation", self.ledger.data["evidence"])


class ScenarioTest(RunnerCase):
    def test_same_az_target_and_actor_no_admin_fallback(self):
        engine = self.engine()
        server = {"id": "server"}
        before = {
            "OS-EXT-SRV-ATTR:host": "source",
            "OS-EXT-AZ:availability_zone": "az1",
        }
        engine.describe = mock.Mock(return_value=before)
        self.admin.list.return_value = [
            {
                "zoneName": "az1",
                "hosts": {
                    "source": {
                        "nova-compute": {"active": True, "available": True}
                    },
                    "dest": {
                        "nova-compute": {"active": True, "available": True}
                    },
                },
            },
            {
                "zoneName": "az2",
                "hosts": {
                    "wrong": {
                        "nova-compute": {"active": True, "available": True}
                    }
                },
            },
        ]
        self.assertEqual("dest", engine.target(server)[0])
        with self.assertRaisesRegex(config.InvalidError, "same AZ"):
            engine.target(server, "wrong")
        self.cfg["compute"]["migration_actor"] = "test_user"
        self.cloud.post.side_effect = APIError("compute", 403)
        with self.assertRaises(APIError):
            engine.migrate(server)
        self.admin.post.assert_not_called()

    def test_storage_device_rejects_root_and_ambiguous_serial(self):
        guest = Guest("192.0.2.2", "server", self.cfg["guest"], self.ledger)
        volume = "aabbccdd-1111-2222-3333-123456789012"
        disk = {
            "path": "/dev/vdb",
            "serial": volume[:20],
            "type": "disk",
            "mountpoints": [None],
        }
        # Nova serial can be truncated to 20 original characters, then hyphens
        # removed.
        disk["serial"] = volume[:20]
        guest.command = mock.Mock(
            return_value=json.dumps({"blockdevices": [disk]})
        )
        self.assertEqual("/dev/vdb", guest.device(volume, unmounted=True))
        disk["children"] = [{"mountpoints": ["/"]}]
        guest.command.return_value = json.dumps({"blockdevices": [disk]})
        with self.assertRaisesRegex(config.InvalidError, "root"):
            guest.device(volume, unmounted=True)
        disk.pop("children")
        guest.command.return_value = json.dumps({"blockdevices": [disk, disk]})
        with self.assertRaisesRegex(config.InvalidError, "multiple"):
            guest.device(volume)

    def test_snapshot_revert_same_volume_after_detach_and_verifies_data(self):
        engine = self.engine()
        engine.options = self.cfg["scenarios"]["cinder-snapshot-revert"]
        guest = mock.Mock()
        guest.device.return_value = "/dev/vdb"
        events = []

        def command(value, *args, **kwargs):
            events.append(value)
            if "cat " in value:
                return (
                    "Before snapshot"
                    if any(e == "REVERT" for e in events)
                    else "After snapshot"
                )
            return ""

        guest.command.side_effect = command
        engine.disk_setup = mock.Mock(
            return_value=(
                {"id": "server"},
                {"id": "volume"},
                guest,
                "/dev/vdb",
            )
        )
        engine.attach = lambda *a: events.append("ATTACH")
        engine.detach = lambda *a: events.append("DETACH")
        engine.create = mock.Mock(return_value={"id": "snapshot"})
        self.cloud.post.side_effect = lambda service, path, body: (
            events.append("REVERT")
        )
        engine.cinder_snapshot_revert()
        self.cloud.post.assert_called_once_with(
            "volume",
            "/volumes/volume/action",
            {"revert": {"snapshot_id": "snapshot"}},
        )
        i = events.index("REVERT")
        self.assertEqual("DETACH", events[i - 1])
        self.assertEqual("ATTACH", events[i + 1])
        self.assertEqual(
            "Before snapshot",
            self.ledger.data["evidence"]["snapshot_revert"][
                "restored_content"
            ],
        )

    def test_extend_remains_attached_and_requires_filesystem_growth(self):
        engine = self.engine()
        engine.options = self.cfg["scenarios"]["cinder-volume-extend"]
        guest = mock.Mock()
        sizes = iter((4 * 1024**3, 9.5 * 1024**3))

        def command(value, *args, **kwargs):
            if value.startswith("df "):
                return str(int(next(sizes)))
            if "blockdev" in value:
                return str(10 * 1024**3)
            if "cat " in value:
                return "Volume extend test"
            return ""

        guest.command.side_effect = command
        engine.disk_setup = mock.Mock(
            return_value=(
                {"id": "server"},
                {"id": "volume"},
                guest,
                "/dev/vdb",
            )
        )
        engine.detach = mock.Mock()
        self.cloud.get.return_value = {
            "volume": {"size": 10, "status": "in-use"}
        }
        engine.cinder_volume_extend()
        engine.detach.assert_not_called()
        self.cloud.post.assert_called_once_with(
            "volume", "/volumes/volume/action", {"os-extend": {"new_size": 10}}
        )
        self.assertIn("volume_extend", self.ledger.data["evidence"])

    def test_http_new_session_no_proxy_no_keepalive(self):
        with mock.patch(
            "ultimum_validation.resources.requests.Session"
        ) as session:
            response = (
                session.return_value.__enter__.return_value.get.return_value
            )
            response.text = "VM1"
            self.assertEqual("VM1", self.engine().http("192.0.2.3", 80))
            client = session.return_value.__enter__.return_value
            self.assertFalse(client.trust_env)
            self.assertEqual(
                {"Connection": "close"}, client.get.call_args.kwargs["headers"]
            )

    def test_dhcp_api_address_alone_is_not_enough(self):
        engine = self.engine()
        engine.options = self.cfg["scenarios"]["neutron-dhcp"]
        self.cloud.get.return_value = {"subnet": {"enable_dhcp": True}}
        port = {"id": "p", "mac_address": "aa:bb:cc:dd:ee:ff"}
        engine.port = mock.Mock(return_value=port)
        guest = mock.Mock()
        guest.command.side_effect = [
            json.dumps(
                [
                    {
                        "address": port["mac_address"],
                        "addr_info": [{"local": "10.240.0.20"}],
                    }
                ]
            ),
            "",
        ]
        engine.vm = mock.Mock(return_value=({}, port, {}, guest))
        with self.assertRaisesRegex(AssertionError, "No DHCP lease"):
            engine.neutron_dhcp()

    def test_unshelve_requires_offloaded_and_exact_target_az(self):
        engine = self.engine()
        engine.options = dict(
            self.cfg["scenarios"]["nova-shelve-unshelve"],
            source_az="az1",
            target_az="az2",
        )
        guest = mock.Mock()
        engine.vm = mock.Mock(
            return_value=(
                {"id": "server"},
                {"id": "port"},
                {"id": "fip"},
                guest,
            )
        )
        engine.describe = mock.Mock(
            return_value={"OS-EXT-AZ:availability_zone": "az1"}
        )
        self.cloud.wait_status.side_effect = [
            {"status": "SHELVED"},
            {"status": "SHELVED_OFFLOADED"},
            {"status": "ACTIVE"},
        ]
        with self.assertRaisesRegex(AssertionError, "target AZ"):
            engine.nova_shelve_unshelve()
        self.admin.post.assert_any_call(
            "compute", "/servers/server/action", {"shelveOffload": None}
        )
        self.admin.post.assert_any_call(
            "compute",
            "/servers/server/action",
            {"unshelve": {"availability_zone": "az2"}},
        )


if __name__ == "__main__":
    unittest.main()
