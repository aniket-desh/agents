# Working on this repository

The shared research profile lives in `guidance/research.md`; keep it brief and
scope it to scientific work. This repository also controls paid resources and
retains experiment data, so validate financial state transitions and recovery
when changing the runtime. Use the offline fake provider for development.

Do not provision paid resources, install into a real home, enable system services,
or change credentials merely to verify a code edit. Never add an automatic pod
termination path. Preserve user settings during installation. Keep ordinary
agent conversations as the human interface; the CLI is an internal tool.

Run relevant tests with `python3 -m unittest discover -s tests -v`. Native agent
and live RunPod behavior require separate integration validation; distinguish
those from mocks. The legacy `team/` hooks are excluded from the default install.
