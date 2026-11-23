# The images of the test cluster of CI, each at a tag and its digest, with the
# pins of the agent image repository. Nothing builds this file. It is here so
# that Renovate sees each image and moves its tag and digest. tests/kind/up.sh
# reads the references by stage name.
#
# The OpenShell release (chart and sandbox runtime) is not here: it is the
# stage `openshell-release` of the Dockerfile of the image.

# The node of the kind cluster: the Kubernetes release of the test.
FROM kindest/node:v1.37.0@sha256:a1ed56cfb0e7b93589bdf97c8cd566405a265939e3620fc4f5de89adff580ae5 AS kind-node

# The controller of Agent Sandbox. The test applies the release manifest of
# this version, with the image pinned to this digest.
FROM registry.k8s.io/agent-sandbox/agent-sandbox-controller:v1.0.12@sha256:b0ee91a42129919179baf5f429979fee3ab1a3dac6855110b4648a6ff4b44103 AS agent-sandbox-controller
