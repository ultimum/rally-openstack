=======================
Ultimum Rally container
=======================

Build the Ultimum image from the repository root using its own Dockerfile::

    docker build -f ultimum/Dockerfile --build-arg GITHUB_TOKEN -t ultimum-rally:local .

Export ``GITHUB_TOKEN`` in the shell before building so Git can clone the
Ultimum Rally repositories. The root Dockerfile belongs to the upstream image;
the repository root is still the build context for ``ultimum/Dockerfile``.

The container uses ``rally-start`` to initialize Rally and then remain running
for commands executed through its console. Mount persistent storage at
``/data`` and an OpenStack RC file at ``/etc/rally/admin.rc``. The Rally database
is stored at ``/data/db/rally.sqlite``.

``RALLY_OPENRC`` changes the RC file location. ``RALLY_DEPLOYMENT_NAME`` changes
the deployment name, which defaults to ``openstack``.

The service validation task definitions and their wrappers are maintained in
``tasks/ultimum/`` and ``ultimum/``. Their configuration is documented in
``tasks/ultimum/README.rst``.

The independent acceptance runner is ``ultimum-rally``. Mount its commented
configuration at ``/etc/rally/ultimum.yaml`` and the SSH keys referenced there.
See ``ultimum/SCENARIOS.rst`` for installation, commands, prerequisites and
the exact steps executed by the runner and each of its twelve scenarios.
These tasks live in ``tasks/ultimum/scenarios/``; they do not execute upstream
``rally-jobs`` or the older service validation wrappers.
