#!/bin/sh

P=$(pwd)
mkdir -p $P/prometheus-data/
cd /usr/share/prometheus
/usr/bin/prometheus \
	--config.file=$P/prometheus.yml \
	--storage.tsdb.path=$P/prometheus-data/ \
	--web.listen-address=127.0.0.1:9095
