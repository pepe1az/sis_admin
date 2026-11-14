#!/bin/sh
# Run on the selected ClickHouse VM; see docs/runbooks.md.
exec /usr/local/sbin/g2-chaos isolate-keeper "$@"
