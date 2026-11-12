#!/bin/sh
# Run on the hypervisor; see docs/runbooks.md.
exec /usr/local/sbin/g2-chaos two-dc "$@"
