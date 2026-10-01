=======================
Ultimum Rally container
=======================

Build the Ultimum image from the repository root using its own Dockerfile::

    docker build -f ultimum/Dockerfile --build-arg GITHUB_TOKEN -t ultimum-rally:local .

Export ``GITHUB_TOKEN`` in the shell before building so Git can clone the
Ultimum Rally repositories. The root Dockerfile belongs to the upstream image;
the repository root is still the build context for ``ultimum/Dockerfile``.

The container uses ``rally-start`` to initialize Rally and then remain running
for commands executed through its console. Start it with persistent data and
configuration mounts (Docker creates absent host directories for ``-v``)::

    docker run -d --name ultimum-rally --network host \
      -v /data:/data \
      -v /etc/ultimum:/etc/ultimum \
      ultimum-rally:local

Keep your existing mount for ``/data`` if its host location differs. No
``RALLY_RUN_DIR``, manual directory creation or ``ssh-keygen`` is needed.
Startup initializes ``/data/ultimum/{home,keys,db,state,results}`` and copies
the commented configuration to ``/etc/ultimum/ultimum.yaml`` if absent. It
preserves existing configuration. Edit the YAML and supply your admin OpenRC::

    docker exec -it ultimum-rally vim /etc/ultimum/ultimum.yaml
    docker exec -it ultimum-rally vim /etc/ultimum/admin.rc
    docker exec ultimum-rally chmod 600 /etc/ultimum/admin.rc
    docker exec ultimum-rally ultimum-rally check --offline
    docker exec ultimum-rally ultimum-rally prepare
    docker exec ultimum-rally ultimum-rally run nova-cloud-init

The OpenRC must contain the admin credentials including ``OS_PASSWORD``.
Set the test user's password, image, flavor, external network and source CIDR
in the YAML. An absent OpenRC at first startup is reported as a warning;
the runner reads it again when you invoke ``prepare`` or ``run``.

To restrict **external** addresses allocated by the tests, add this section
under the existing ``network`` mapping in ``/etc/ultimum/ultimum.yaml``::

    network:
      external_ip_pool:
        subnet: null             # Auto-detect; or external subnet name/UUID.
        start: 92.119.67.128
        end: 92.119.67.250

Both bounds are inclusive. The runner uses this range for VM/Octavia floating
IPs and a newly created router's gateway. Occupied IPs are skipped; exhaustion
fails without allocating outside the range. An existing router selected with
``create_router: false`` is reused unchanged. Tenant IPs remain automatic;
``network.subnet`` controls tenant DHCP/IPAM separately. ``probe_source_cidr``
only controls which source addresses the security group permits.

Selecting exact external IPs requires the configured test user's Neutron
policy permissions; see the external address section in ``SCENARIOS.rst``.
Rebuild the image for this runner feature. Existing mounted configuration is
preserved, so add these settings to it explicitly; null bounds keep the
previous automatic external allocation behavior.

``create_ssh_key: true`` generates a persistent local key and imports its
public part into a named Nova keypair for the test user. Existing matching
keys are reused. ``create_ssh_key: false`` requires ``ssh_key.name``, matching
local key files and an existing Nova keypair. No key is silently replaced.

``HOME`` is ``/data/ultimum/home``. The Rally database is stored at
``/data/ultimum/db/rally.sqlite``. Startup copies an old
``/data/db/rally.sqlite`` there if the new database does not exist, preserving
the old file. Stop the old container before switching to the new one; do not
continue writing to the old database after this one-time copy.

Runner and Rally configuration files live in ``/etc/ultimum``. A compatibility
symlink at ``/etc/rally/rally.conf`` lets Rally find ``/etc/ultimum/rally.conf``.

``RALLY_OPENRC`` changes the RC file location. ``RALLY_DEPLOYMENT_NAME`` changes
the deployment name, which defaults to ``openstack``.

Configure TLS in ``/etc/ultimum/ultimum.yaml``. For a private CA, mount its
PEM CA bundle inside the container, for example at
``/etc/ultimum/g2server-ca.pem``, and add::

    tls:
      ca_cert: /etc/ultimum/g2server-ca.pem
      insecure: false

To disable server certificate verification instead::

    tls:
      ca_cert: null
      insecure: true

These settings apply to admin and test-user API calls, Rally scenario
environments and the startup authentication check. Explicit YAML values
override ``OS_CACERT`` and ``OS_INSECURE`` in the admin OpenRC; null inherits
each corresponding OpenRC value. With neither configured, TLS verification
is enabled using the default CA bundle. ``ca_cert: ""`` explicitly selects
default CAs. ``insecure: true`` ignores all CA paths. A CA used for verification
must be an absolute, readable file path inside the container.

Rebuild the image to install this feature; existing mounted YAML files are
preserved, so add the settings to them explicitly. The runner reloads them
on every invocation. Check authentication without creating cloud resources::

    docker exec ultimum-rally ultimum-rally check-auth

Startup uses this same check and writes its log to
``/tmp/rally-auth-check.log``; the welcome banner shows the result from startup.
It reads ``admin.openrc`` from YAML, unless ``RALLY_OPENRC`` overrides it for
the startup check. The legacy ``openstack`` deployment remains available for
older commands, with its original stored credentials/TLS settings. Raw
``rally deployment check`` checks that stored snapshot, independently of the
runner YAML. Runner scenario environments are selected automatically for the
current credentials and TLS settings; old environments and results are kept.

The service validation task definitions and their wrappers are maintained in
``tasks/ultimum/`` and ``ultimum/``. Their configuration is documented in
``tasks/ultimum/README.rst``.

The independent acceptance runner is ``ultimum-rally``. ``prepare`` prints
creation/reuse decisions; scenarios print operations and check results live,
with a waiting message during longer operations. An interactive TTY is not
required for this output.
See ``ultimum/SCENARIOS.rst`` for installation, commands, prerequisites and
the exact steps executed by the runner and each of its thirteen scenarios.
These tasks live in ``tasks/ultimum/scenarios/``; they do not execute upstream
``rally-jobs`` or the older service validation wrappers.
