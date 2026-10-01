"""Rally discovers these independent Ultimum plugins via --plugin-paths."""

import pathlib
import sys


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from ultimum_validation.cli import execute_plugin
from ultimum_validation.config import SCENARIOS

from rally.task import atomic

from rally_openstack.task import scenario


def register(slug):
    class Acceptance(scenario.OpenStackScenario):
        def run(self, config_path: str, state_path: str):
            """Execute one prepared run in the configured existing project.

            :param config_path: Path to the private operator configuration.
            :param state_path: Path to the run-owned resource ledger.
            """
            execute_plugin(
                config_path,
                state_path,
                slug,
                lambda name: atomic.ActionTimer(self, name),
            )

    Acceptance.__name__ = "Ultimum" + "".join(
        x.title() for x in slug.split("-")
    )
    return scenario.configure(name="Ultimum." + slug.replace("-", "_"))(
        Acceptance
    )


for _slug in SCENARIOS:
    globals()["Ultimum" + "".join(x.title() for x in _slug.split("-"))] = (
        register(_slug)
    )
