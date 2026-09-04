# A deliberately small, database-free runner for the real PHP DiscussionTools
# comment parser.  This is not a MediaWiki service and does not render wikitext:
# it accepts already-rendered historical Parsoid HTML on stdin.
FROM php:8.3.27-cli-bookworm@sha256:01224f5f2e75fa43a326797d7b80552ca0bcfb37f60cbb81efdf63956b4d3fe4

ARG MEDIAWIKI_COMMIT=dfd080bb34fe9160b027c814e08af29a8e63063c
ARG DISCUSSIONTOOLS_COMMIT=16fa124bcf4ad5bb9419abe634d700772bc07be8
ARG VISUALEDITOR_COMMIT=020ff448040df3adac2531d22229b7629a1eb5c3
ARG LINTER_COMMIT=df783ad77cba2ae28adb1875ce124b0f67b9758d
ARG COMPOSER_VERSION=2.8.12
ARG COMPOSER_SHA256=f446ea719708bb85fcbf4ef18def5d0515f1f9b4d703f6d820c9c1656e10a2f2

LABEL org.opencontainers.image.title="wikidisputes DiscussionTools harness" \
      org.wikimedia.mediawiki.commit="${MEDIAWIKI_COMMIT}" \
      org.wikimedia.discussiontools.commit="${DISCUSSIONTOOLS_COMMIT}" \
      org.wikimedia.visualeditor.commit="${VISUALEDITOR_COMMIT}" \
      org.wikimedia.linter.commit="${LINTER_COMMIT}"

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl git unzip libicu-dev libonig-dev libsqlite3-dev libxml2-dev \
 && docker-php-ext-install calendar intl mbstring dom xml pdo_sqlite \
 && rm -rf /var/lib/apt/lists/* \
 && curl -fsSL "https://getcomposer.org/download/${COMPOSER_VERSION}/composer.phar" \
      -o /usr/local/bin/composer \
 && echo "${COMPOSER_SHA256}  /usr/local/bin/composer" | sha256sum -c - \
 && chmod 0755 /usr/local/bin/composer

WORKDIR /opt/mediawiki

# Do not replace these clones with a reimplementation or a JavaScript port. The
# PHP classes below are the production DiscussionTools classes under test.
RUN clone_pinned() { \
		repository="$1"; destination="$2"; revision="$3"; \
		git init "$destination"; \
		git -C "$destination" remote add origin "$repository"; \
		git -C "$destination" -c http.version=HTTP/1.1 fetch --depth 1 origin "$revision"; \
		git -C "$destination" checkout --detach FETCH_HEAD; \
	}; \
	clone_pinned https://gerrit.wikimedia.org/r/mediawiki/core . "${MEDIAWIKI_COMMIT}" \
 && clone_pinned https://gerrit.wikimedia.org/r/mediawiki/extensions/DiscussionTools extensions/DiscussionTools "${DISCUSSIONTOOLS_COMMIT}" \
 && clone_pinned https://gerrit.wikimedia.org/r/mediawiki/extensions/VisualEditor extensions/VisualEditor "${VISUALEDITOR_COMMIT}" \
 && clone_pinned https://gerrit.wikimedia.org/r/mediawiki/extensions/Linter extensions/Linter "${LINTER_COMMIT}" \
 && composer install --no-dev --no-interaction --prefer-dist --optimize-autoloader \
 && composer --version --no-ansi \
 && printf '%s\n' "${COMPOSER_VERSION}" > /opt/composer-version.txt

COPY LocalSettings.php /opt/mediawiki/LocalSettings.php
COPY discussiontools_ndjson.php /opt/discussiontools/discussiontools_ndjson.php
COPY versions.php /opt/discussiontools/versions.php

ENTRYPOINT ["php", "/opt/discussiontools/discussiontools_ndjson.php"]
