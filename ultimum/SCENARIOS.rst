Ultimum acceptance runner
=========================

The ``ultimum-rally`` runner executes its own Rally plugins and tasks from
``tasks/ultimum/scenarios``. Each scenario repetition creates one Rally task,
one run UUID and a separate resource ledger. The run UUID differs from the
Rally task UUID; the result contains both values.

Installation and first run
--------------------------

Building with ``ultimum/Dockerfile`` installs the runner, plugins, ping and
Python dependencies. Container startup runs ``ultimum-rally init`` to create
missing directories and copy ``ultimum/ultimum.yaml.example`` to
``/etc/ultimum/ultimum.yaml``, with comments for every option. Existing
configuration is preserved. The four existing service verification wrappers
remain separate.

Mount persistent directories at ``/data`` and ``/etc/ultimum``. The launch
command in ``ultimum/README.rst`` uses Docker ``-v``, which creates missing
host directories; no ``RALLY_RUN_DIR`` or manual ``mkdir`` is needed. After
startup:

* fill in ``/etc/ultimum/ultimum.yaml`` (created with permissions 0600);
* save the admin OpenRC as ``/etc/ultimum/admin.rc`` with permissions 0600;
* run ``ultimum-rally prepare`` to generate/reuse the SSH key and cloud setup.

Home, keys, database, state and results are under ``/data/ultimum``. The
container's ``HOME`` and working directory are ``/data/ultimum/home``. The Rally
database is ``/data/ultimum/db/rally.sqlite``. If only the old
``/data/db/rally.sqlite`` exists, startup copies it once without modifying the
old database. Stop the old container before switching; new changes belong in
the new database. ``/etc/ultimum/rally.conf`` is discovered by Rally through
the compatibility symlink ``/etc/rally/rally.conf``.

Set the test user's password, image, flavor, external network and
``probe_source_cidr``. The latter is the actual source IP/CIDR that Neutron
sees for connections from the runner; with NAT, it may differ from the
container's IP. The container must have a route to the floating IPs under
test. The image must provide cloud-init, SSH, passwordless sudo and an
Ubuntu/systemd environment. Additional tools are python3, curl, e2fsprogs
and, for TPM, tpm2-tools; ``guest.install_missing_packages`` permits their
installation through apt.

Creating resources and using existing ones
------------------------------------------

The creation flags are at the top level of the YAML. ``true`` allows creating
a missing resource; ``false`` requires an existing resource and an explicit
reference. Projects and users have a name and domain; networks, subnets and
routers can use a name or a UUID in the ``id`` field. An explicit ``id`` takes
precedence over the name.

Example for an existing project, user and connected network with a router
(merge into the full configuration, including the actual CIDR)::

    resources_prefix: e2e
    create_project: false
    create_user: false
    create_network: false
    create_subnet: false
    create_router: false

    identity:
      project:
        name: ultimum
        domain: Default
      user:
        name: ultimum-runner
        domain: Default
        password: CHANGE_ME

    network:
      tenant_network:
        name: ultimumnet
      subnet:
        name: ultimumsubnet
      router:
        name: ultimumrouter

With ``create_*=true``, ``name`` can remain null. The prefix generates
``e2e-project``, ``e2e-user``, ``e2e-net``, ``e2e-subnet``, ``e2e-router``
and ``e2e-key``.
Explicit names are not changed by the prefix. Test VMs and other run resources
receive names such as ``e2e-<run-uuid>-...``. With false, a missing name is not
replaced by a default; the runner fails while loading the configuration.

The flags are independent: for example, you can create a router for an
existing network and subnet. With ``create_router: false``, the runner neither
modifies the router nor adds interfaces; the existing router must already
connect to the selected subnet and external network. Missing references and
connections are checked before creating network resources. Roles, explicit
password updates and quotas have their own settings and are not disabled by
the creation flags.

The original ``identity.*.create_if_missing``, ``network.mode`` and
``execution.resource_prefix`` settings remain supported as aliases. Supplying
both the old and new options with different values makes the configuration
invalid.

External addresses, tenant addresses and probe sources
-----------------------------------------------------

These settings have different purposes:

* ``network.external_ip_pool`` restricts **new external allocations**:
  floating IPs of all test VMs, Octavia's floating VIP and the external
  gateway of a newly created router.
* ``network.subnet.allocation_pool_start/end`` describe the **private tenant
  subnet**. VM ports obtain private addresses through normal Neutron IPAM
  and DHCP. Only the fixed-IP DHCP scenario requests a specific private IP.
  Nova Count continues to let Nova create the tenant ports automatically.
* ``network.probe_source_cidr`` is the **source filter of SG ingress rules**
  for SSH/ICMP/HTTP probes from the runner. It does not allocate any IPs.
  ``0.0.0.0/0`` permits these probes from any routed IPv4 source.

Merge this into the existing ``network`` mapping in your mounted config::

    network:
      external_ip_pool:
        subnet: null             # Or external subnet name/UUID.
        start: 92.119.67.128
        end: 92.119.67.250

1. Preparation resolves the external network and the subnet containing both
   bounds. If several subnets match, require an explicit ``subnet``.
2. Before each allocation, read occupied external ports using the admin
   connection, including other projects' FIP and router ports.
3. Select a free address within the inclusive bounds, skipping the subnet's
   network, broadcast and gateway addresses. Send the exact address in
   ``floating_ip_address`` or the router's ``external_fixed_ips``.
4. Neutron creates the associated external service port with that address.
   The floating IP's ``port_id`` refers to the internal VM/VIP port. An
   independently created external port cannot be substituted for it.
5. If another process claims the address first, try the next address on an
   ``IpAddressInUse`` conflict. Other errors stop the operation. Verify the
   returned address and print the chosen IP and port association.
6. If no address is available, fail without automatic IPAM or an allocation
   outside the configured range. A specific ``router.external_fixed_ip``
   must be inside the pool and is never replaced with another address.

This is a runner allocation restriction, not a Neutron-wide reservation;
other projects can still use addresses in that range. It does not modify
the external subnet's allocation pools. Both bounds null keep Neutron's
normal automatic external allocation behavior.

With ``create_router: false``, the existing router remains read-only even if
its gateway address is outside the range. A router reused with
``create_router: true`` must already have its gateway in the configured pool;
otherwise preparation fails and asks for explicit reconciliation. It never
moves a gateway automatically. Shared routers are retained by run cleanup;
run-owned floating IPs are released according to ``execution.cleanup``.

Resource creation still runs as ``identity.user``. Exact external selection
requires these Neutron policy permissions:

* ``create_floatingip:floating_ip_address`` for VM and VIP FIPs;
* ``create_router:external_gateway_info:external_fixed_ips`` for new routers.

The default ``member`` role may lack them, depending on cloud version/policy.
A 403 reports the relevant policy and request ID; it does not trigger an
admin fallback or an unrestricted allocation. The admin connection only
reads external-port occupancy for this allocation step. Policy references:
`floating IP policies <https://github.com/openstack/neutron/blob/master/neutron/conf/policies/floatingip.py>`_
and `router policies <https://github.com/openstack/neutron/blob/master/neutron/conf/policies/router.py>`_.

Persistent SSH key
------------------

The same creation convention applies to the key::

    resources_prefix: e2e
    create_ssh_key: true
    ssh_key:
      name: null                # Generates e2e-key; or set an explicit name.
      directory: /data/ultimum/keys
    guest:
      ssh_private_key: null     # /data/ultimum/keys/e2e-key
      ssh_public_key: null      # /data/ultimum/keys/e2e-key.pub

``prepare`` generates a missing RSA 3072-bit private key with mode 0600 and
its public key. It imports the public key into a Nova keypair named
``ssh_key.name``, scoped to the configured test user. Repeated preparation
reuses the same local files and checks that the Nova keypair matches. Keys
and the Nova keypair persist across runs and are excluded from run cleanup.

To use existing keys, set ``create_ssh_key: false`` and an explicit
``ssh_key.name``. The files must already exist at the derived paths, or set
absolute ``guest.ssh_private_key`` and ``guest.ssh_public_key`` paths. If only
the private path is specified, the public path is that path plus ``.pub``.
The Nova keypair must already exist for the test user, not just the admin.
Mounted existing keys can be read-only. RSA, Ed25519 and ECDSA private keys
without a passphrase are supported; restrict private key permissions to 0600
or 0400.

Existing matching material is accepted with either flag value. A different
public key under the same Nova name, or mismatched local files, stops the
operation without replacing anything. With creation enabled, a missing
``.pub`` is derived from the existing private key. An existing public key
without its private part cannot be recovered: provide the matching private
key or choose a new name. ``check --offline`` allows missing files when
creation is enabled; cloud ``check`` requires keys already prepared and never
generates or imports them.

Running the suite
-----------------

Examples::

    ultimum-rally list
    ultimum-rally explain runner
    ultimum-rally explain cinder-snapshot-revert
    ultimum-rally check --offline
    ultimum-rally prepare
    ultimum-rally check nova-live-migration
    ultimum-rally run nova-live-migration
    ultimum-rally run neutron-dhcp
    ultimum-rally run --all
    ultimum-rally report <run-uuid>
    ultimum-rally cleanup <run-uuid>

Specify an alternative YAML file before the subcommand::

    ultimum-rally --config /etc/ultimum/lab.yaml run placement

``check --offline`` validates configuration and local files without contacting
OpenStack. ``check`` also verifies test-user authentication, API microversions
and scenario prerequisites. It creates no resources. If the project or user
does not exist yet, run ``prepare`` first. It sends no trial POST requests,
so backend capabilities and policy permissions are conclusively verified
only when the test runs.

``run`` includes an idempotent ``prepare``. ``run --all`` executes only scenarios
with ``enabled: true``, sequentially; a missing TPM flavor or target AZ results
in BLOCKED for that scenario. Drain and Masakari are disabled by default.
``execution.repetitions: N`` creates N separate tasks for each scenario.

The admin OpenRC is sourced again for every command requiring cloud access.
Although container startup uses it, a new ``docker exec`` does not inherit
the environment of the shell that sourced it. The runner uses Keystone v3
password authentication; this version does not support application credentials
or federated authentication. The OpenRC file is a trusted operator shell script.

Results and cleanup
-------------------

``/data/ultimum/results/<run-uuid>/result.json`` contains status, steps, evidence,
resource UUIDs and the Rally task UUID. ``rally.html`` and ``rally.json`` are
Rally report exports. State and the Rally log are stored in
``/data/ultimum/state/runs/<run-uuid>/``. Passwords are not passed in task
arguments; the test user's credentials are stored in the Rally database.
Protect the configuration, database and state directory.

``prepare`` reports each lookup, creation, reuse, role assignment, quota
decision and connection check. Scenarios print timestamped operations,
resource IDs, VM readiness, SSH connection and cloud-init progress, check
results and cleanup. Long operations emit a waiting message approximately
every 15 seconds while preparation/the scenario/cleanup is active. Output is
flushed immediately, including with ``docker exec`` without ``-t``. Steps are
also recorded in ``result.json``; the Rally worker's output is retained in
``state/runs/<run-uuid>/rally.log``. Private keys, passwords and API request
bodies are not printed in progress messages.

PASS means the scenario checks passed; FAIL indicates an unmet expectation
or failed operation; BLOCKED means missing configuration or prerequisites;
UNSUPPORTED indicates a detected missing API capability. A failed backend
request is marked FAIL; the runner cannot automatically distinguish every
backend failure cause. Incomplete cleanup or host restoration is reported
separately even when the scenario is PASS and causes a nonzero exit code.
CLI exit codes: 0 for success, 1 for a failed run/report/cleanup, 2 for blocking
configuration, and 130 for interruption.

``cleanup: on-success`` retains failed-test resources for diagnosis.
``always`` attempts cleanup after failures too; ``never`` retains resources
from every test. Internal steps such as deleting temporary floating IPs or
releasing a successful placement batch still run with ``never``. Retained
resources consume quotas; remove them with ``cleanup <run-uuid>`` before
running more tests if needed.

Cleanup operates only on resources recorded for a specific run, verifies the
cloud and project, and is repeatable. Before creating a resource, it saves an
ownership marker so it can find the resource if the response is lost. It also
finds boot volumes for servers created through Count. It does not delete the
project, user, prepared SSH keypair or shared network/subnet/router created by
prepare. Following an
ambiguous interruption while an API request is still being processed, cleanup
may need to be repeated once the cloud has settled. An abrupt SIGKILL or loss
of a node or storage cannot be handled as an atomic transaction with the cloud.
If a lost response prevents confirmation that creation succeeded, cleanup is
marked incomplete and the unresolved intent is retained for inspection; the
runner does not claim that no resources remain.

Restoring the saved Nova/Masakari state is attempted regardless of the
``cleanup`` mode. Restoration remains pending while the host is powered off;
run cleanup again after powering it on. The project flock coordinates processes
using the same state_dir. For multiple containers, share this directory on a
filesystem supporting flock; a different state_dir provides no coordination.
The host must also be reserved against use by operators and other tools that
do not honor this lock.

.. scenario: runner

Runner steps
------------

1. Loads the YAML, fills in documented defaults and rejects unknown options.
   Validates creation flags and explicit references when false. Generates
   names from resources_prefix only when true. For a specific scenario,
   also validates its local prerequisites.
2. Sources the admin OpenRC, including the URL, region, endpoint type and TLS
   settings. Acquires a local exclusive lock for the cloud/project.
3. Looks up the project and user by name and domain. Creates missing resources
   if create_project/create_user allow it. Fails if a resource is missing and
   its flag is false. Reads the new user's password from YAML. Updates an
   existing password only with ``update_existing_password: true``.
4. Assigns the configured project roles, defaulting to ``member``. Then verifies
   that the test user can authenticate into the correct project.
5. With ``quotas.apply: true``, sets only the listed Nova/Cinder/Neutron/Octavia
   quotas. Currently apply=false; numeric defaults still need to be supplied.
6. Prepares the persistent local SSH key and the test user's Nova keypair,
   honoring create_ssh_key and ssh_key.name. Prints whether each part is being
   created or reused and verifies matching key material. Uses the test user
   to resolve the image, flavor and external network by
   unique name or UUID. Verifies that the network has the external flag.
7. With create_network/create_subnet=true, creates or reuses its own network
   and subnet using the YAML CIDR, internal gateway, DHCP, allocation pool
   and DNS settings. Stops preparation on a name collision with an unrelated
   resource or mismatched existing parameters; does not silently reconfigure
   an existing network.
8. With create_router=true, creates a router with an external gateway. Sends
   ``external_subnet`` and ``external_fixed_ip`` to Neutron if configured.
   Neutron creates the router's gateway port. The configuration specifies
   a network/subnet and optionally an unused IP, rather than adopting an
   existing external port. Connects the internal subnet through a router
   interface port and verifies the connection on repeated prepare calls too.
9. When the relevant creation flag is false, resolves the existing network
   resource by UUID or unique name within the project. Verifies connectivity
   and CIDR without changing these shared resources' configuration.
10. Registers its own Rally environment with one predefined user/project.
    Leaves the globally selected Rally environment unchanged. Task
    ``users: {}`` does not create random Rally projects or users.
11. Creates run state for each scenario, checks microversions and available
    prerequisites, validates the YAML task and runs exactly one Ultimum plugin.
12. The plugin creates VMs, ports, volumes and other project resources as the
    test user. Admin reads host/AZ details and performs migrations according
    to ``migration_actor``; a 403 for test_user does not trigger admin fallback.
13. Uses the SSH keypair already checked by prepare and creates its own SG
    allowing SSH and ICMP from the source CIDR. VMs use the persistent keypair
    alongside run-owned ports, volumes and security groups.
    The first SSH connection saves the new VM's host key in state; subsequent
    connections compare it.
14. Records API and guest check results, exports the Rally report and cleans
    up run-owned resources in dependency order according to the cleanup mode.

.. scenario: placement

1. Placement - Count and instance distribution
----------------------------------------------

1. Reads batch_sizes, submission_modes, the optional AZ, group_policy,
   allowed_hosts and the separate anti-affinity test's instance count.
2. Uses the prepared SSH keypair and creates an SG; the runner has already
   prepared the network and router.
3. In serial mode, sends a Nova create request with min_count=max_count for
   each batch. Waits for all VMs to become ACTIVE and reads their hosts from
   the admin API.
4. Checks the optional allowed-host list and hard affinity/anti-affinity
   policy. Records the host -> VM count distribution. Does not require even
   distribution; soft policies are only recorded. Deletes each successful
   batch before starting the next one.
5. In parallel mode, submits all batch requests concurrently. The default
   is 10+20+10 = 40 concurrent VMs. Finds them through reservation IDs,
   waits for ACTIVE, checks placement and deletes them.
6. Also creates a separate hard anti-affinity group with two VMs by default.
   Requires two different hosts. Assigns no floating IPs and does not test SSH;
   this scenario measures creation and placement, not guest networking.

With boot_from_volume, each Count member gets its own root volume. The storage
AZ setting applies to explicitly created volumes. Nova multi-create cannot
pass a separate Cinder AZ for each image-to-volume boot; leave
storage.availability_zone=null for placement, otherwise the test is UNSUPPORTED.

.. scenario: nova-live-migration

2. Nova - live migration
------------------------

1. Creates an access SG with ingress TCP/``guest.ssh_port`` (22 by default)
   and ICMP from ``network.probe_source_cidr``. Attaches the SG to a tenant
   port with an automatically assigned private IP. Creates a root volume
   according to the compute configuration, a VM and a floating IP from
   ``external_ip_pool`` when configured.
2. Reads the VM port and SG back from Neutron. Checks the port belongs to
   this VM, carries the access SG and has the configured SSH ingress rule;
   rejects explicitly disabled port security. Prints the checks and stores
   evidence, then waits for real SSH and cloud-init completion. Determines
   the actual host and AZ. All scenarios using SSH share this initial check.
3. Finds another up/enabled host in the same AZ, or validates target_host.
4. Starts ping from the runner and a heartbeat on one persistent SSH channel.
   Waits for a usable baseline before migration.
5. Requests live migration with shared storage and block_migration=false,
   using the selected actor. Waits for the host to change, ACTIVE status
   and an empty task_state.
6. Verifies that the AZ is unchanged, and checks lost ping packets and the
   largest SSH heartbeat gap against the limits. SSH does not reconnect
   during measurement.
7. Rechecks the port and SSH rule after migration and executes a command on
   the existing SSH connection. Saves source/target hosts, SG evidence,
   measurements and ping output; performs cleanup. An API rule check does
   not replace the SSH and ping data-plane measurements.

.. scenario: nova-live-migration-tpm

3. Nova - live migration with vTPM
----------------------------------

1. Resolves tpm_flavor and verifies hw:tpm_version=2.0. Leaves the flavor intact.
2. Creates its own VM with this flavor, a port, floating IP and SSH access.
3. Checks for tpm2-tools and installs them if allowed by the guest configuration.
4. Defines an NV index and writes exactly payload_bytes random bytes, 32 by
   default. Does not put shorter text into a larger unchecked buffer.
5. Migrates the VM to another host in the same AZ and verifies the host/ACTIVE.
6. Reads the same number of bytes from the NV index and compares them byte
   for byte. With verify_key_creation, also creates a primary context and
   a new TPM key.
7. Records the result and performs cleanup. This test verifies TPM state;
   continuous ping/SSH thresholds belong to the separate test numbered 2.

vTPM live migration support depends on Nova/libvirt and how the TPM secret is
stored. The flavor alone does not prove migration support; an API/guest failure
is not converted to PASS. Regular TPM evacuation is outside the Masakari test.

.. scenario: nova-drain

4. Nova - drain a dedicated hypervisor
--------------------------------------

1. Requires enabled=true, a specific host, an up/enabled compute service
   and no existing unrelated VMs on the host across any project.
2. Creates its own VMs with the requested host. **The test user must have
   requested_destination policy permission**; the runner does not grant
   it by automatically assigning an admin role. Determines and verifies
   each VM's actual host.
3. For stopped_instances, writes a verification file over SSH, stops the VM
   and waits for SHUTOFF. Prepares floating IPs and SSH for active VMs.
4. Rechecks that the host contains only VMs from this run. Saves the original
   Nova service state and disables the service to prevent further scheduling.
5. Migrates active VMs sequentially, with ping and persistent SSH using the
   nova-live-migration limits. Cold-migrates stopped VMs and confirms
   VERIFY_RESIZE.
6. Verifies that cold migration preserves SHUTOFF; then starts the VM on
   its new host and checks the verification file over SSH.
7. Verifies that no VMs remain on the source host, restores the original service
   state even on failure, and removes run-owned resources per the cleanup mode.

This scenario uses live/cold migration, not evacuate.

.. scenario: nova-cloud-init

5. Nova metadata service - cloud-init
-------------------------------------

1. Builds cloud-config from YAML with the original image user, a new testuser
   and the file /cloud.test (all configurable).
2. Creates its own VM with this user-data and config_drive=false, a port and FIP.
3. Connects over SSH as the image user and waits for cloud-init status.
4. Verifies the new user with id and the exact file contents with sudo cat.
5. With verify_metadata_endpoint, fetches metadata inside the VM from
   link-local 169.254.169.254 without a proxy and compares the UUID to the VM.
6. Also saves the ds-identify log and performs cleanup.

.. scenario: neutron-dhcp

6. Neutron - fixed IP assigned through DHCP
-------------------------------------------

1. Verifies that DHCP is enabled and fixed_ip is within the tenant CIDR,
   outside the allocation pool and different from the gateway. Neutron
   rejects an IP that is already in use.
2. Creates its own port with the exact subnet/IP pair and an access SG.
3. Creates a VM with this existing port, adds a floating IP and opens SSH.
4. Uses ip -j address to find the interface by the Neutron port's MAC
   and verifies the IP.
5. With require_dhcp_lease, checks for a lease for this IP in systemd-networkd,
   dhclient or NetworkManager lease files. The API address alone is insufficient.
6. Saves the port ID, interface and lease, then performs cleanup. Requires
   no VM created by another scenario.

.. scenario: neutron-security-group

7. Neutron - allow and block HTTP
---------------------------------

1. Creates its own HTTP SG, port, VM and floating IP. SSH/ICMP use a separate
   SG whose rules are retained throughout the test.
2. Starts a simple Python HTTP server over SSH as a systemd service,
   serving page content unique to this run.
3. Adds an ingress TCP rule for http_port from the runner's source CIDR.
4. Waits for three successful HTTP requests with the exact page content.
5. Deletes only its own HTTP ingress rule. New TCP connections must stop
   working within the propagation timeout; verifies three failed probes.
6. Uses the retained SSH connection to verify that the web server still
   works on localhost. A process failure cannot substitute for verifying
   that the security group blocks traffic.
7. With verify_allow_after_deny, restores the rule and requires successful
   new HTTP connections. Records all three phases and performs cleanup.

Probes use neither proxies nor HTTP keep-alive; they do not evaluate an already
allowed established connection that may remain in conntrack.

.. scenario: octavia-vip

8. Octavia - VIP and actual backend responses
---------------------------------------------

1. Resolves the provider. Requires TCP and SOURCE_IP_PORT for OVN.
2. Creates independent backend VMs and an SG for backend_port. Temporarily
   assigns each a FIP and starts an HTTP server over SSH serving VM1, VM2, etc.
3. Checks each backend with a direct HTTP probe. Deletes temporary backend
   FIPs; during the main test the VMs have only internal addresses.
4. Creates a load balancer with a VIP on the configured tenant subnet,
   a listener, pool and members using the backend IPs. Waits for ACTIVE
   after each change.
5. Adds its own ingress SG to the run-owned VIP port and assigns a floating IP.
6. Waits for working data traffic. Opens probe_connections new TCP connections
   to the FIP/VIP and records which backend responds.
7. Requires valid content and at least required_backends_seen distinct backends.
   Does not require regular VM1/VM2 alternation with SOURCE_IP_PORT.
8. Deletes the FIP, load balancer with cascade (including listener/pool/members),
   VMs and other run-owned resources according to the cleanup mode.

This test sends an HTTP payload through TCP load balancing. It checks the VIP
at the data plane; it creates no health monitor and does not test L7 features.

.. scenario: cinder-snapshot-revert

9. Cinder - revert the original volume to a snapshot
----------------------------------------------------

1. Creates a VM, floating IP, SSH access and a separate data volume.
2. Attaches the volume and finds the disk by its UUID/serial in lsblk. Refuses
   to format an ambiguous disk, the root disk or an already mounted filesystem.
3. Creates ext4, a mount directory and a verification file with before_content.
4. Runs sync, umount and detach; waits for available. Creates a consistent
   snapshot and waits for the snapshot to become available.
5. Reattaches and mounts the volume, writes after_content and verifies the change.
6. Repeats sync, umount, detach and the wait for available. Requests a real Cinder
   revert to this snapshot on the same volume; the minimum microversion is 3.40.
7. After completion, reattaches/mounts and compares the contents to before_content
   over SSH.
8. Records evidence and removes the VM, snapshot, volume and network resources
   according to the cleanup mode.

An unsupported storage backend causes the test to fail; the runner does not
create a replacement clone to stand in for reverting the original volume.

.. scenario: cinder-volume-extend

10. Cinder - online volume extension
------------------------------------

1. Creates its own VM with SSH access and a data volume, 5 GiB by default.
2. Safely identifies the disk, creates ext4, mounts it and writes a test file.
   Records the original filesystem size in bytes.
3. Without detaching, sends os-extend to target_size_gib, 10 GiB by default.
   Requires Cinder API >=3.42 and backend/policy support for online extension.
4. Waits for the new API size and in-use status. In the guest, waits for the
   new blockdev capacity, requesting a rescan if the disk exposes sysfs rescan.
5. Runs resize2fs on the still-mounted ext4 filesystem.
6. Verifies filesystem growth to approximately the new capacity (allowing for
   filesystem metadata) and unchanged file contents. Saves measurements
   and performs cleanup.

.. scenario: masakari-host-failure

11. Masakari - actual failure of a dedicated host
-------------------------------------------------

1. Requires explicit enablement in YAML, a specific host/segment, up/enabled
   compute, a Masakari host outside maintenance and no unrelated VMs on the host.
2. Creates its own regular VMs without TPM requirements on that host, with FIPs
   and SSH access. Requires the same requested_destination policy permission
   as drain.
3. Writes verification data to the VMs, runs sync and rechecks for unrelated VMs.
   Saves the original Nova/Masakari states before the fault.
4. Enters WAITING_FOR_FAULT. In a second terminal, the operator runs::

       ultimum-rally fault-start <run-uuid>

   Immediately afterward, the operator performs a hard power-off of the
   configured physical host. The timeout is measured from fault-start;
   the command itself does not power off the host.
5. The runner watches for the actual Nova service down and Masakari
   on_maintenance states. It sends neither evacuate nor simulated VM shutdown.
6. Waits for automatically recovered ACTIVE VMs on other hosts, then verifies
   persistent data over SSH. Recovery, including SSH access, must meet the
   timeout, defaulting to 300 seconds from the marked start.
7. Saves the result. The operator powers on the physical host. Once nova-compute
   is up again, cleanup restores the original maintenance and service enable
   flags. While the host is down, restoration remains pending and is not
   reported as complete.

This scenario does not perform BMC/IPMI operations or power on the host.
Successful physical recovery of the source host is reflected by completed
restoration in the cleanup result.

.. scenario: nova-shelve-unshelve

12. Nova - shelve/offload and unshelve to another AZ
----------------------------------------------------

1. Requires two explicitly configured distinct AZs and Nova API >=2.77.
2. Creates a VM in source_az, verifies the actual AZ, assigns a FIP and opens SSH.
3. Writes a verification file and runs sync.
4. Sends shelve. If the state remains SHELVED, also sends shelveOffload using
   the selected privileged actor; requires SHELVED_OFFLOADED.
5. Sends unshelve with availability_zone=target_az and waits for ACTIVE.
6. Verifies the actual target AZ. The test fails if the VM remains in the
   source AZ or lands in any other AZ.
7. Checks that the original FIP is associated with the run-owned port and
   restores the association if needed. Reopens SSH and checks file contents
   for persistence.
8. Saves the result and performs cleanup. Cinder AZ/storage or policy
   restrictions cause a failure; the runner does not bypass the scheduler.

Scope and references
--------------------

The implementation excludes block migration, regular live migration across AZs,
Cinder backup/restore and the unspecified environment isolation test.

API contracts and limitations:

* `Nova API <https://docs.openstack.org/api-ref/compute/>`_: Count,
  service UUIDs, requested destination, migrations and unshelve.
* `Cinder v3 API <https://docs.openstack.org/api-ref/block-storage/v3/>`_:
  revert >=3.40 and online extend >=3.42.
* `OVN provider <https://docs.openstack.org/ovn-octavia-provider/latest/admin/driver.html>`_:
  supported protocols and algorithms.
* `Nova vTPM <https://docs.openstack.org/nova/latest/admin/emulated-tpm.html>`_:
  migration limitations and secret security modes.
* `Masakari API <https://docs.openstack.org/api-ref/instance-ha/>`_:
  segments, hosts and maintenance.

Local tests use fake API and SSH implementations. Passing unit tests does not
replace an integration run on a specific cloud with its policies and storage.
