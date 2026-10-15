# ruff: noqa: E402
"""Availability-zone aware plugins for the Ultimum AZ task.

The upstream plugins below do not let a task choose the availability zone of
every resource they create. These variants keep the upstream behaviour and add
that choice. The contexts are registered for the ``ultimum`` platform, so a
task enables them as ``volumes@ultimum`` and ``servers@ultimum`` and the
``required_contexts`` checks of the upstream scenarios still pass.
"""

import random
import typing as t

from keystoneauth1 import adapter

from rally import plugins


# Rally loads --plugin-paths before the installed plugins, but the upstream
# modules imported below read the [openstack] options at import time.
plugins.load()

from rally import exceptions
from rally.common import validation as common_validation
from rally.task import atomic
from rally.task import types
from rally.task import validation

from rally_openstack.common import consts
from rally_openstack.common import osclients
from rally_openstack.common.services.loadbalancer import octavia
from rally_openstack.common.services.storage import block
from rally_openstack.task import context
from rally_openstack.task import scenario
from rally_openstack.task.contexts.cinder import volumes
from rally_openstack.task.contexts.nova import servers
from rally_openstack.task.scenarios.cinder import utils as cinder_utils
from rally_openstack.task.scenarios.nova import utils as nova_utils
from rally_openstack.task.scenarios.octavia import utils as octavia_utils


AVAILABILITY_ZONE = {
    "oneOf": [{"type": "string", "minLength": 1}, {"type": "null"}],
    "description": "Availability zone of the created resources; null "
                   "keeps the service default.",
}


@common_validation.add("required_platform", platform="openstack", users=True)
@context.configure(name="volumes", platform="ultimum", order=420)
class ZonalVolumeGenerator(volumes.VolumeGenerator):
    """Creates volumes for each tenant in the selected availability zone."""

    CONFIG_SCHEMA = {
        **volumes.VolumeGenerator.CONFIG_SCHEMA,
        "properties": {
            **volumes.VolumeGenerator.CONFIG_SCHEMA["properties"],
            "availability_zone": AVAILABILITY_ZONE,
        },
    }

    def setup(self):
        kwargs = {"volume_type": self.config.get("type")}
        if self.config.get("availability_zone"):
            kwargs["availability_zone"] = self.config["availability_zone"]

        for user, tenant_id in self._iterate_per_tenants():
            self.context["tenants"][tenant_id].setdefault("volumes", [])
            cinder_service = block.BlockStorage(
                osclients.Clients(user["credential"]),
                name_generator=self.generate_random_name,
                atomic_inst=self.atomic_actions())
            for i in range(self.config["volumes_per_tenant"]):
                vol = cinder_service.create_volume(self.config["size"],
                                                   **kwargs)
                self.context["tenants"][tenant_id]["volumes"].append(
                    vol._as_dict())


@common_validation.add("required_platform", platform="openstack", users=True)
@context.configure(name="servers", platform="ultimum", order=430)
class ZonalServerGenerator(servers.ServerGenerator):
    """Boots servers for each tenant in the selected availability zone."""

    CONFIG_SCHEMA = {
        **servers.ServerGenerator.CONFIG_SCHEMA,
        "properties": {
            **servers.ServerGenerator.CONFIG_SCHEMA["properties"],
            "availability_zone": AVAILABILITY_ZONE,
        },
    }

    def setup(self):
        kwargs = {}
        if self.config.get("nics"):
            kwargs["nics"] = [
                nic if isinstance(nic, dict) else {"net-id": nic}
                for nic in self.config["nics"]]
        if self.config.get("availability_zone"):
            kwargs["availability_zone"] = self.config["availability_zone"]

        # Through the registry, so project_users.py resolves them as users.
        image_id = types.ResourceType.get("glance_image")(
            self.context, scenario_cls=nova_utils.NovaScenario
        ).pre_process(resource_spec=self.config["image"],
                      config={"type": "glance_image"}, output_type=str)
        flavor_id = types.ResourceType.get("nova_flavor")(
            self.context, scenario_cls=nova_utils.NovaScenario
        ).pre_process(resource_spec=self.config["flavor"],
                      config={"type": "nova_flavor"}, output_type=str)

        for iter_, (user, tenant_id) in enumerate(self._iterate_per_tenants()):
            nova_scenario = nova_utils.NovaScenario({
                "user": user,
                "tenant": self.context["tenants"][tenant_id],
                "task": self.context["task"],
                "owner_id": self.context["owner_id"],
                "iteration": iter_})
            booted = nova_scenario._boot_servers(
                image_id, flavor_id,
                requests=self.config["servers_per_tenant"],
                auto_assign_nic=self.config["auto_assign_nic"],
                **kwargs)
            self.context["tenants"][tenant_id]["servers"] = [
                server.id for server in booted]


@validation.add("image_valid_on_flavor", flavor_param="flavor",
                image_param="image")
@validation.add("restricted_parameters", param_names=["name", "display_name"],
                subdict="create_vm_params")
@validation.add("restricted_parameters", param_names=["name", "display_name"],
                subdict="create_volume_kwargs")
@validation.add("required_services", services=[consts.Service.NOVA,
                                               consts.Service.CINDER])
@validation.add("volume_type_exists", param_name="volume_type", nullable=True)
@validation.add("required_platform", platform="openstack", users=True)
@scenario.configure(
    context={"cleanup@openstack": ["cinder", "nova"]},
    name="UltimumCinderVolumes.create_snapshot_and_attach_volume",
    platform="openstack")
class CreateSnapshotAndAttachVolume(cinder_utils.CinderBasic,
                                    nova_utils.NovaScenario):

    def run(
        self,
        image: t.Annotated[str, types.Convert("glance_image")],
        flavor: t.Annotated[str, types.Convert("nova_flavor")],
        volume_type: str | None = None,
        size: t.Annotated[
            int, scenario.Field(ge=1)] | block.VolumeSizeSpec = 1,
        create_volume_kwargs: dict[str, t.Any] | None = None,
        create_vm_params: dict[str, t.Any] | None = None,
        **kwargs: t.Any,
    ) -> None:
        """Create vm, volume, snapshot and attach/detach volume.

        Same as CinderVolumes.create_snapshot_and_attach_volume, but the
        volume also takes 'create_volume_kwargs' (e.g. availability_zone).

        :param image: Glance image name to use for the VM
        :param flavor: VM flavor name
        :param volume_type: Name of volume type to use
        :param size: volume size, in GB
        :param create_volume_kwargs: optional arguments for volume creation
        :param create_vm_params: optional arguments for VM creation
        :param kwargs: Optional parameters used during volume
                       snapshot creation.
        """
        volume = self.cinder.create_volume(size, volume_type=volume_type,
                                           **(create_volume_kwargs or {}))
        snapshot = self.cinder.create_snapshot(volume.id, force=False,
                                               **kwargs)
        server = self._boot_server(image, flavor, **(create_vm_params or {}))

        self._attach_volume(server, volume)
        self._detach_volume(server, volume)

        self.cinder.delete_snapshot(snapshot)
        self.cinder.delete_volume(volume)
        self._delete_server(server)


@validation.add("restricted_parameters", param_names=["name", "display_name"],
                subdict="create_volume_kwargs")
@validation.add("restricted_parameters", param_names="name",
                subdict="create_backup_kwargs")
@validation.add("required_services", services=[consts.Service.CINDER])
@validation.add("required_cinder_services", services="cinder-backup")
@validation.add("required_platform", platform="openstack", users=True)
@scenario.configure(
    context={"cleanup@openstack": ["cinder"]},
    name="UltimumCinderVolumes.create_and_restore_volume_backup",
    platform="openstack")
class CreateAndRestoreVolumeBackup(cinder_utils.CinderBasic):

    def run(
        self,
        size: t.Annotated[int, scenario.Field(ge=1)] | block.VolumeSizeSpec,
        do_delete: bool = True,
        create_volume_kwargs: dict[str, t.Any] | None = None,
        create_backup_kwargs: dict[str, t.Any] | None = None,
    ) -> None:
        """Restore a volume backup into a volume created with the same args.

        Cinder creates the restore target in its default availability zone
        unless one is given, so this scenario creates the target volume
        itself with 'create_volume_kwargs' and restores into it.

        :param size: volume size in GB
        :param do_delete: if True, the volumes and the volume backup will
                          be deleted after creation.
        :param create_volume_kwargs: optional args to create both volumes
        :param create_backup_kwargs: optional args to create a volume backup
        """
        create_volume_kwargs = create_volume_kwargs or {}
        if isinstance(size, dict):
            size = random.randint(size["min"], size["max"])

        volume = self.cinder.create_volume(size, **create_volume_kwargs)
        backup = self.cinder.create_backup(volume.id,
                                           **(create_backup_kwargs or {}))
        target = self.cinder.create_volume(size, **create_volume_kwargs)
        self.cinder.restore_backup(backup.id, volume_id=target.id)

        if do_delete:
            self.cinder.delete_volume(target)
            self.cinder.delete_volume(volume)
            self.cinder.delete_backup(backup)


class ZonalAggregateMixin:

    def _zone_host(self, availability_zone):
        """Return an enabled, up compute host of the availability zone."""
        return self._zone_hypervisor(availability_zone).service["host"]

    def _zone_hypervisor(self, availability_zone):
        """Return an enabled, up hypervisor of the availability zone."""
        hypervisors = [
            h for h in self._list_hypervisors()
            if h.state == "up" and h.status == "enabled"]
        if availability_zone:
            zone_hosts = set()
            nova = self.admin_clients("nova")
            for zone in nova.availability_zones.list(detailed=True):
                if zone.zoneName != availability_zone:
                    continue
                for host, services in (zone.hosts or {}).items():
                    compute = services.get("nova-compute", {})
                    if compute.get("available") and compute.get("active"):
                        zone_hosts.add(host)
            hypervisors = [h for h in hypervisors
                           if h.service["host"] in zone_hosts]
        if not hypervisors:
            raise exceptions.RallyException(
                "Could not find an available compute host in availability "
                "zone '%s'" % availability_zone)
        return hypervisors[0]


@validation.add("required_services", services=[consts.Service.NOVA])
@validation.add("required_platform", platform="openstack", admin=True)
@scenario.configure(
    context={"admin_cleanup@openstack": ["nova"]},
    name="UltimumNovaAggregates.create_aggregate_add_and_remove_host",
    platform="openstack")
class CreateAggregateAddAndRemoveHost(ZonalAggregateMixin,
                                      nova_utils.NovaScenario):

    def run(self, availability_zone: str | None = None) -> None:
        """Create an aggregate, add a zone host to and remove it from it.

        The aggregate belongs to 'availability_zone' and the host is taken
        from the same zone, so the host keeps its availability zone.

        :param availability_zone: The availability zone of the aggregate
                                  and of the host
        """
        aggregate = self._create_aggregate(availability_zone)
        host_name = self._zone_host(availability_zone)
        self._aggregate_add_host(aggregate, host_name)
        self._aggregate_remove_host(aggregate, host_name)


@validation.add("required_services", services=[consts.Service.NOVA])
@validation.add("required_platform", platform="openstack",
                admin=True, users=True)
@scenario.configure(
    context={"admin_cleanup@openstack": ["nova"],
             "cleanup@openstack": ["nova"]},
    name="UltimumNovaAggregates.create_aggregate_add_host_and_boot_server",
    platform="openstack")
class CreateAggregateAddHostAndBootServer(ZonalAggregateMixin,
                                          nova_utils.NovaScenario):

    def run(
        self,
        image: t.Annotated[str, types.Convert("glance_image")],
        metadata: dict[str, t.Any],
        availability_zone: str | None = None,
        ram: t.Annotated[int, scenario.Field(ge=1)] = 512,
        vcpus: t.Annotated[int, scenario.Field(ge=1)] = 1,
        disk: t.Annotated[int, scenario.Field(ge=0)] = 1,
        boot_server_kwargs: dict[str, t.Any] | None = None,
    ) -> None:
        """Create an aggregate with a zone host and boot a server on it.

        The aggregate, its host and the server all use 'availability_zone'.
        The server is steered to the host without any scheduler filter
        configuration: a custom trait is put on the host's resource
        provider in Placement for the duration of the scenario, and the
        flavor requires it ('trait:CUSTOM_...=required'), which Placement
        always honours. Unscoped metadata keys are also set on the flavor
        with the 'aggregate_instance_extra_specs:' scope, which only
        AggregateInstanceExtraSpecsFilter uses (when enabled).

        :param image: The image ID to boot from
        :param metadata: The aggregate metadata, also set as flavor
                         extra specs
        :param availability_zone: The availability zone of the aggregate,
                                  its host and the server
        :param ram: Memory in MB for the flavor
        :param vcpus: Number of VCPUs for the flavor
        :param disk: Size of local disk in GB
        :param boot_server_kwargs: Optional additional arguments for VM
                                   creation
        :raises RallyException: if instance and aggregate hosts do not match
        """
        boot_server_kwargs = dict(boot_server_kwargs or {})
        if availability_zone:
            boot_server_kwargs.setdefault("availability_zone",
                                          availability_zone)

        aggregate = self._create_aggregate(availability_zone)
        hypervisor = self._zone_hypervisor(availability_zone)
        host_name = hypervisor.service["host"]
        self._aggregate_set_metadata(aggregate, metadata)
        self._aggregate_add_host(aggregate, host_name)
        flavor = self._create_flavor(ram, vcpus, disk)
        trait = "CUSTOM_ULTIMUM_RALLY_%s" % self.generate_random_name(
        ).upper().replace("-", "_")
        placement = Placement(self._admin_clients)
        provider = placement.provider(hypervisor.hypervisor_hostname)
        with atomic.ActionTimer(self, "placement.add_trait"):
            placement.add_trait(provider, trait)
        try:
            extra_specs = {
                key if ":" in key else "aggregate_instance_extra_specs:" + key:
                value for key, value in metadata.items()}
            extra_specs["trait:" + trait] = "required"
            flavor.set_keys(extra_specs)

            server = self._boot_server(image, flavor.id, **boot_server_kwargs)
            server = self.admin_clients("nova").servers.get(server.id)
            if getattr(server, "OS-EXT-SRV-ATTR:host") != host_name:
                raise exceptions.RallyException("Instance host and aggregate "
                                                "host are different")
        finally:
            with atomic.ActionTimer(self, "placement.remove_trait"):
                placement.remove_trait(provider, trait)


class Placement:
    """The few Placement API calls needed to steer a server to one host."""

    VERSION = "placement 1.6"
    ATTEMPTS = 5

    def __init__(self, clients):
        credential = clients.credential
        self.api = adapter.Adapter(
            session=clients.keystone.get_session()[0],
            service_type="placement",
            interface=credential.endpoint_type or "public",
            region_name=credential.region_name,
            default_microversion=self.VERSION.split()[1])

    def provider(self, name):
        providers = self.api.get(
            "/resource_providers", params={"name": name}
        ).json()["resource_providers"]
        if not providers:
            raise exceptions.RallyException(
                "No Placement resource provider named '%s'" % name)
        return providers[0]["uuid"]

    def _update_traits(self, provider, change):
        # Nova also writes the provider's traits; retry on its generation.
        for attempt in range(self.ATTEMPTS):
            current = self.api.get(
                "/resource_providers/%s/traits" % provider).json()
            traits = change(set(current["traits"]))
            if traits == set(current["traits"]):
                return
            response = self.api.put(
                "/resource_providers/%s/traits" % provider,
                json={"traits": sorted(traits),
                      "resource_provider_generation":
                          current["resource_provider_generation"]},
                raise_exc=False)
            if response.status_code != 409:
                response.raise_for_status()
                return
        raise exceptions.RallyException(
            "Traits of resource provider %s kept changing" % provider)

    def add_trait(self, provider, trait):
        self.api.put("/traits/%s" % trait)
        self._update_traits(provider, lambda traits: traits | {trait})

    def remove_trait(self, provider, trait):
        self._update_traits(provider, lambda traits: traits - {trait})
        self.api.delete("/traits/%s" % trait, raise_exc=False)


class ZonalOctavia(octavia.Octavia):
    """Octavia service whose load balancers use one availability zone."""

    availability_zone = None

    @atomic.action_timer("octavia.load_balancer_create")
    def load_balancer_create(self, subnet_id, description=None,
                             admin_state=None, project_id=None,
                             listeners=None, flavor_id=None,
                             provider=None, vip_qos_policy_id=None):
        args = {
            "name": self.generate_random_name(),
            "description": description,
            "listeners": listeners,
            "provider": provider,
            "admin_state_up": admin_state or True,
            "project_id": project_id,
            "vip_subnet_id": subnet_id,
            "vip_qos_policy_id": vip_qos_policy_id,
        }
        if flavor_id:
            args["flavor_id"] = flavor_id
        if self.availability_zone:
            args["availability_zone"] = self.availability_zone
        lb = self._clients.octavia().load_balancer_create(
            json={"loadbalancer": args})
        return lb["loadbalancer"]


class ZonalOctaviaBase(octavia_utils.OctaviaBase):

    def __init__(self, context=None, admin_clients=None, clients=None):
        super().__init__(context, admin_clients, clients)
        if hasattr(self, "_clients"):
            self.octavia = ZonalOctavia(
                self._clients, name_generator=self.generate_random_name,
                atomic_inst=self.atomic_actions())

    def _create_loadbalancers(self, availability_zone, **kwargs):
        """Create a load balancer per tenant subnet and wait for them."""
        self.octavia.availability_zone = availability_zone
        project_id = self.context["tenant"]["id"]
        loadbalancers = [
            self.octavia.load_balancer_create(
                subnet_id=subnet_id, project_id=project_id, **kwargs)
            for network in self.context["tenant"].get("networks", [])
            for subnet_id in network.get("subnets", [])]
        for loadbalancer in loadbalancers:
            self.octavia.wait_for_loadbalancer_prov_status(loadbalancer)
        return loadbalancers


def octavia_scenario(name):
    def wrapper(cls):
        for decorator in (
                scenario.configure(context={"cleanup@openstack": ["octavia"]},
                                   name="UltimumOctavia." + name,
                                   platform="openstack"),
                validation.add("required_contexts", contexts=["network"]),
                validation.add("required_platform", platform="openstack",
                               users=True),
                validation.add("required_services",
                               services=[consts.Service.OCTAVIA])):
            cls = decorator(cls)
        return cls
    return wrapper


class ZonalLoadbalancerScenario(ZonalOctaviaBase):

    def run(
        self,
        availability_zone: str | None = None,
        description: str | None = None,
        admin_state: bool = True,
        flavor_id: str | None = None,
        provider: str | None = None,
        vip_qos_policy_id: str | None = None,
    ) -> None:
        """Create a load balancer per subnet in the availability zone.

        :param availability_zone: Octavia availability zone of the load
                                  balancers; null keeps the default
        :param description: Human-readable description of the loadbalancer
        :param admin_state: The administrative state of the loadbalancer
        :param flavor_id: The ID of the Octavia flavor
        :param provider: Provider name for the loadbalancer
        :param vip_qos_policy_id: The ID of the QoS policy
        """
        loadbalancers = self._create_loadbalancers(
            availability_zone, description=description,
            admin_state=admin_state, flavor_id=flavor_id,
            provider=provider, vip_qos_policy_id=vip_qos_policy_id)
        self._after_create(loadbalancers)

    def _after_create(self, loadbalancers):
        raise NotImplementedError()


@octavia_scenario("create_and_list_loadbalancers")
class CreateAndListLoadbalancers(ZonalLoadbalancerScenario):

    def _after_create(self, loadbalancers):
        self.octavia.load_balancer_list()


@octavia_scenario("create_and_delete_loadbalancers")
class CreateAndDeleteLoadbalancers(ZonalLoadbalancerScenario):

    def _after_create(self, loadbalancers):
        for loadbalancer in loadbalancers:
            self.octavia.load_balancer_delete(loadbalancer["id"])


@octavia_scenario("create_and_update_loadbalancers")
class CreateAndUpdateLoadbalancers(ZonalLoadbalancerScenario):

    def _after_create(self, loadbalancers):
        for loadbalancer in loadbalancers:
            self.octavia.load_balancer_set(
                lb_id=loadbalancer["id"],
                lb_update_args={"name": self.generate_random_name()})
        for loadbalancer in loadbalancers:
            self.octavia.wait_for_loadbalancer_prov_status(loadbalancer)


@octavia_scenario("create_and_stats_loadbalancers")
class CreateAndStatsLoadbalancers(ZonalLoadbalancerScenario):

    def _after_create(self, loadbalancers):
        for loadbalancer in loadbalancers:
            self.octavia.load_balancer_stats_show(loadbalancer["id"])


@octavia_scenario("create_and_show_loadbalancers")
class CreateAndShowLoadbalancers(ZonalLoadbalancerScenario):

    def _after_create(self, loadbalancers):
        for loadbalancer in loadbalancers:
            self.octavia.load_balancer_show(loadbalancer["id"])


class ZonalPoolScenario(ZonalOctaviaBase):

    def run(
        self,
        protocol: str,
        lb_algorithm: str,
        availability_zone: str | None = None,
        provider: str | None = None,
    ) -> None:
        """Create a pool on a load balancer per subnet in the zone.

        :param protocol: protocol for which the pool listens
        :param lb_algorithm: loadbalancer algorithm
        :param availability_zone: Octavia availability zone of the load
                                  balancers; null keeps the default
        :param provider: Provider name for the load balancers; null keeps
                         Octavia's default provider
        """
        for loadbalancer in self._create_loadbalancers(availability_zone,
                                                       provider=provider):
            pool = self.octavia.pool_create(
                lb_id=loadbalancer["id"],
                protocol=protocol, lb_algorithm=lb_algorithm)
            self._after_create(pool)
        self._after_all()

    def _after_create(self, pool):
        pass

    def _after_all(self):
        pass


@octavia_scenario("create_and_list_pools")
class CreateAndListPools(ZonalPoolScenario):

    def _after_all(self):
        self.octavia.pool_list()


@octavia_scenario("create_and_delete_pools")
class CreateAndDeletePools(ZonalPoolScenario):

    def _after_create(self, pool):
        self.octavia.pool_delete(pool["id"])


@octavia_scenario("create_and_update_pools")
class CreateAndUpdatePools(ZonalPoolScenario):

    def _after_create(self, pool):
        self.octavia.pool_set(
            pool_id=pool["id"],
            pool_update_args={"name": self.generate_random_name()})


@octavia_scenario("create_and_show_pools")
class CreateAndShowPools(ZonalPoolScenario):

    def _after_create(self, pool):
        self.octavia.pool_show(pool["id"])
