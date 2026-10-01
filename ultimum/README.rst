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

The service validation task definitions and their wrappers are maintained in
``tasks/ultimum/`` and ``ultimum/``. Their configuration is documented in
``tasks/ultimum/README.rst``.

The independent acceptance runner is ``ultimum-rally``. ``prepare`` prints
creation/reuse decisions; scenarios print operations and check results live,
with a waiting message during longer operations. An interactive TTY is not
required for this output.
See ``ultimum/SCENARIOS.rst`` for installation, commands, prerequisites and
the exact steps executed by the runner and each of its twelve scenarios.
These tasks live in ``tasks/ultimum/scenarios/``; they do not execute upstream
``rally-jobs`` or the older service validation wrappers.
