"""Offline acceptance-runner tests; no OpenStack credentials or resources."""

import copy
import io
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

import jinja2
import yaml


ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ultimum"))
from ultimum_validation import cli  # noqa: E402
from ultimum_validation import config  # noqa: E402
from ultimum_validation import prepare  # noqa: E402
from ultimum_validation.cloud import APIError  # noqa: E402
from ultimum_validation.cloud import Cloud  # noqa: E402
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
        self.cloud.list.return_value = []
        self.cloud.absent.return_value = True
        self.runtime = dict(
            project_id="test-project",
            network_id="network",
            subnet_id="subnet",
            external_network_id="external",
            image_id="image",
            flavor_id="flavor",
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

    def test_creation_flags_require_explicit_existing_references(self):
        for kind in ("project", "user", "network", "subnet", "router"):
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

    def test_defaults_and_all_twelve_tasks_render_without_credentials(self):
        self.assertEqual(12, len(config.SCENARIOS))
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
        for slug in ("nova-drain", "masakari-host-failure"):
            with self.assertRaisesRegex(config.InvalidError, "disabled"):
                config.validate(self.cfg, slug)
            self.cfg["scenarios"][slug]["enabled"] = True
            with self.assertRaisesRegex(config.InvalidError, "explicit"):
                config.validate(self.cfg, slug)

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
            mock.patch.object(cli, "prepare") as provision,
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            self.assertEqual(
                0, cli.main(["--config", str(path), "check", "placement"])
            )
            provision.assert_not_called()


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
