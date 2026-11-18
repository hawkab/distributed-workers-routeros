# syntax=docker/dockerfile:1
ARG BASE_IMAGE=alpine:3.22@sha256:49340b346798875b5592e5d420241f2f9cfba5f47e62ebb1446a1b34734e898f
FROM ${BASE_IMAGE}
RUN apk add --no-cache ca-certificates php84 php84-curl php84-openssl php84-phar php84-zlib php84-pcntl php84-posix \
 && ln -s /usr/bin/php84 /usr/local/bin/php \
 && addgroup -g 10001 worker && adduser -D -H -u 10001 -G worker worker \
 && mkdir -p /opt/fleet /config /state /programs \
 && chown worker:worker /config /state /programs \
 && printf 'memory_limit=-1\nmax_execution_time=0\nphar.readonly=1\n' > /etc/php84/conf.d/90-worker.ini
COPY .build/sdk/ /opt/fleet/
COPY container/supervise.php container/tick.php /opt/worker/
USER 10001:10001
WORKDIR /state
STOPSIGNAL SIGTERM
ENTRYPOINT ["php", "/opt/worker/supervise.php"]
CMD ["/config/worker.local.json"]
