MagMueller
# Upstream commit 49ac25921 ("Disable Browser Use telemetry by default"),
# inherited by the 00c12dac6 sync. Upstream has no mapping file for this
# address, so the attribution gate flags it downstream; handle taken from the
# commit author name.
