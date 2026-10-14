# ruff: noqa: E402
"""Scale Heat stacks without EC2 credentials and count their servers.

Signal
------

``HeatStacks.create_stack_and_scale`` POSTs to the URL in a stack output
without any credentials, so upstream it only works with a scaling policy's
``alarm_url``: a URL pre-signed with EC2 credentials of a Heat stack user and
validated by heat-api-cfn against Keystone ``/v3/ec2tokens``.

Ultimum templates output the policy's ``signal_url`` instead, a Heat API
URL (``.../stacks/<name>/<id>/resources/<resource>/signal``) that needs no
EC2 credentials, only a Keystone token. For such URLs
``HeatScenario._stack_webhook`` signals the resource through heatclient as
the scenario user, over Rally's own Heat endpoint and Keystone session.
EC2-signed URLs (``Signature`` in the query) are still POSTed as upstream
does.

Server count
------------

After the signal the scenario waits until the stack has ``delta`` more
``OS::Nova::Server`` resources, but upstream counts only resources up to
``nested_depth=1``: servers that are themselves the group members. Ultimum
group members are ``Ultimum::BootServer`` stacks (server plus boot volume),
so their servers are one level deeper, the count stays 0 and the scenario
waits until ``heat_stack_scale_timeout`` (an hour). ``_count_instances``
therefore counts servers up to ``nested_depth=2``, which counts both kinds of
member, each server once.
"""

from urllib.parse import parse_qs
from urllib.parse import unquote
from urllib.parse import urlparse

from rally import plugins


# Rally loads --plugin-paths before the installed plugins, but the upstream
# modules imported below read the [openstack] options at import time.
plugins.load()

from rally import exceptions
from rally.task import atomic

from rally_openstack.task.scenarios.heat import utils as heat_utils


upstream_stack_webhook = heat_utils.HeatScenario._stack_webhook

SERVER_DEPTH = 2

EC2_SIGNATURES = ("Signature", "X-Amz-Signature")


def heat_signal_target(url):
    """(stack, resource) of a Heat API signal URL, or None for other URLs."""
    parsed = urlparse(url)
    if any(key in parse_qs(parsed.query) for key in EC2_SIGNATURES):
        return None
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    # .../stacks/<name>/<id>/resources/<resource>/signal
    if (len(parts) < 6 or parts[-1] != "signal"
            or parts[-3] != "resources" or parts[-6] != "stacks"):
        return None
    return "%s/%s" % (parts[-5], parts[-4]), parts[-2]


def stack_webhook(self, stack, output_key):
    for output in stack.outputs:
        if output["output_key"] == output_key:
            url = output["output_value"]
            break
    else:
        raise exceptions.InvalidConfigException(
            "No output key %(key)s found in stack %(id)s" %
            {"key": output_key, "id": stack.id})
    target = heat_signal_target(url or "")
    if target is None:
        return upstream_stack_webhook(self, stack, output_key)
    stack_id, resource = target
    with atomic.ActionTimer(self, "heat.%s_webhook" % output_key):
        self.clients("heat").resources.signal(stack_id, resource)


def count_instances(self, stack):
    return len([
        r for r in self.clients("heat").resources.list(
            stack.id, nested_depth=SERVER_DEPTH)
        if r.resource_type == "OS::Nova::Server"])


heat_utils.HeatScenario._stack_webhook = stack_webhook
heat_utils.HeatScenario._count_instances = count_instances
