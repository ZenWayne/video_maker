# Minimal forward proxy, used to give pods on vm-0-8-ubuntu an egress path.
# See deploy/k8s/README.md "BLOCKER: pods on vm-0-8-ubuntu have no internet
# egress" for why this exists.
FROM docker.io/library/alpine:3.20

RUN apk add --no-cache tinyproxy

# Config is baked in so the container needs no network to start (the whole
# point of this pod is that egress is unreliable elsewhere).
#   Allow: cluster pod + service CIDRs only — this proxy is never exposed
#   outside the cluster (ClusterIP Service, no Ingress).
RUN printf '%s\n' \
    'Port 8888' \
    'Listen 0.0.0.0' \
    'Timeout 600' \
    'Allow 10.42.0.0/16' \
    'Allow 10.43.0.0/16' \
    'ConnectPort 443' \
    'ConnectPort 80' \
    'LogLevel Warning' \
    'StartServers 2' \
    'MaxClients 60' \
    > /etc/tinyproxy/tinyproxy.conf

EXPOSE 8888
CMD ["tinyproxy", "-d", "-c", "/etc/tinyproxy/tinyproxy.conf"]
