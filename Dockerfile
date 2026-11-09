# One final multiarch image contains every supported database engine.
# Supply-chain evidence and limitations: docs/IMAGE.md (no floating inputs).
# Official Restic image: only its verified static binary enters our runtime.
FROM restic/restic:0.19.1@sha256:136600b6ff6843d61d355f7f71f460a166429f35de6fd11b568fece3c9a4d510 AS restic-source

FROM debian:bookworm-20261104-slim@sha256:734a72b3d6142a71993b4bc580ca01d8ca687f77a2c422d96a961d3376b179bc AS build
ARG TARGETARCH
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONPATH=/opt/k3s-restic \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LANG=en_US.UTF-8 \
    LC_ALL=en_US.UTF-8 \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    TZ=Asia/Shanghai \
    DATA_PATH=/data \
    BACKUP_WORK_PATH=/backup

# Signed, timestamp-frozen Debian repositories also freeze transitive packages.
# HTTP is only for initial CA bootstrap; APT signature/hash verification stays on.
RUN set -eu; \
    printf 'Types: deb\nURIs: http://snapshot.debian.org/archive/debian/20261108T000000Z/\nSuites: bookworm\nComponents: main\nSigned-By: /usr/share/keyrings/debian-archive-keyring.gpg\nCheck-Valid-Until: no\n\nTypes: deb\nURIs: http://snapshot.debian.org/archive/debian-security/20261108T000000Z/\nSuites: bookworm-security\nComponents: main\nSigned-By: /usr/share/keyrings/debian-archive-keyring.gpg\nCheck-Valid-Until: no\n' > /etc/apt/sources.list.d/debian.sources; \
    printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d; chmod 0755 /usr/sbin/policy-rc.d; \
    apt-get update; \
    apt-get install --yes --no-install-recommends \
      ca-certificates=20250419~deb12u2 curl=7.88.1-10+deb12u15 bzip2=1.0.8-5+b1 \
      python3=3.11.2-1+b1 rsync=3.2.7-1+deb12u6 cron=3.0pl1-162 \
      locales=2.36-9+deb12u14 util-linux=2.38.1-5+deb12u3 \
      coreutils=9.1-1 findutils=4.9.0-4 gzip=1.12-1 \
      tar=1.34+dfsg-1.2+deb12u1 tzdata=2026f-0+deb12u1; \
    groupadd --gid 999 postgres; useradd --uid 999 --gid 999 --home-dir /var/lib/postgresql --no-create-home --shell /bin/sh postgres; \
    groupadd --gid 998 mysql; useradd --uid 998 --gid 998 --home-dir /var/lib/mysql --no-create-home --shell /bin/sh mysql; \
    mkdir -p /etc/postgresql-common /tmp/image-downloads; \
    printf 'create_main_cluster = false\n' > /etc/postgresql-common/createcluster.conf; \
    printf 'en_US.UTF-8 UTF-8\n' > /etc/locale.gen; locale-gen

# These are immutable, pre-reviewed PGDG deb SHA256s, NOT a rolling apt source.
RUN set -eu; cd /tmp/image-downloads; \
    case "$TARGETARCH" in \
      amd64) printf '%s\n' \
        '9dc15f3f41090e632e0449693323f18c96bfd25794c439d8bcde06af6c5012f6 pool/main/p/postgresql-18/libpq5_18.6-1.pgdg12+2_amd64.deb' \
        '12b7e33dc5b0711c02248816c02e7a08f85d1372835409de66adcd9f808864f4 pool/main/p/postgresql-16/postgresql-16_16.15-1.pgdg12+2_amd64.deb' \
        'e4c00577ff40b59ccdf115a585a9a4e766539669473d272885325b674731dc53 pool/main/p/postgresql-16/postgresql-client-16_16.15-1.pgdg12+2_amd64.deb' \
        'cb33e6465c2b8b223ceabedfc2009bdaf7d678729b8466b43011b7914e5213c8 pool/main/p/postgresql-17/postgresql-17_17.11-1.pgdg12+2_amd64.deb' \
        '1e286c060f8f9ab6485b1b02df6543ce38610bc828c63695528f0593658113ed pool/main/p/postgresql-17/postgresql-client-17_17.11-1.pgdg12+2_amd64.deb' \
        'c1f350f3ed881535b242725510fb8f4d7a6031e3cb8f4c1d8019b2e67930be75 pool/main/p/pgvector/postgresql-16-pgvector_0.8.7-1.pgdg12+1_amd64.deb' \
        'acb5569b6a5bb71d5968205a4da15911ff3b2a15a57a2a9d765560882f53e3ea pool/main/p/pgvector/postgresql-17-pgvector_0.8.7-1.pgdg12+1_amd64.deb' > downloads.lock ;; \
      arm64) printf '%s\n' \
        '1e2b364b93ff16293d6f4a409e49b0d8c0521b9f7ae3423007f443c31c0f6766 pool/main/p/postgresql-18/libpq5_18.6-1.pgdg12+2_arm64.deb' \
        '80f3df32523274f7f26efb4240cc4a8dc5d132f4dc44b483fb77bfd05237c702 pool/main/p/postgresql-16/postgresql-16_16.15-1.pgdg12+2_arm64.deb' \
        '42a82b0a547c94e8b172a482961efa1d64069a9d97efbc80a6dc0bd70337e656 pool/main/p/postgresql-16/postgresql-client-16_16.15-1.pgdg12+2_arm64.deb' \
        'a93463e4f4ef9e11acfc9c541e9ba59ccefccc9fc09a89c7d6f4989ae40ba020 pool/main/p/postgresql-17/postgresql-17_17.11-1.pgdg12+2_arm64.deb' \
        'a9e43d4fabea44c52d7be60996e1447b857e1e78c844ca5beb8cd87f05f6fd57 pool/main/p/postgresql-17/postgresql-client-17_17.11-1.pgdg12+2_arm64.deb' \
        'a50ea0d64ba3ab05f98910aa75f0dd238a466cfe5dbb1e1899896dd2a514e810 pool/main/p/pgvector/postgresql-16-pgvector_0.8.7-1.pgdg12+1_arm64.deb' \
        'fa2280ed2592c5de4d84287ec0183908112f07548cf226868349e92675eea8d7 pool/main/p/pgvector/postgresql-17-pgvector_0.8.7-1.pgdg12+1_arm64.deb' > downloads.lock ;; \
      *) echo 'unsupported target architecture' >&2; exit 1 ;; \
    esac; \
    printf '%s\n' \
      'a4e2461975abffae23688fc95c4fdc97fea4c4c9cc096c2c4a7a4e334dfc3353 pool/main/p/postgresql-common/postgresql-client-common_293.pgdg12+1_all.deb' \
      'f2fb700d5a16075f73e12932830f81b0dad176c7c7d924c5c26bc96cdcfccbc6 pool/main/p/postgresql-common/postgresql-common_293.pgdg12+1_all.deb' >> downloads.lock; \
    while read -r checksum artifact; do \
      filename="${artifact##*/}"; \
      curl --fail --show-error --location --retry 3 --proto '=https' --proto-redir '=https' \
        "https://apt.postgresql.org/pub/repos/apt/$artifact" --output "$filename"; \
      printf '%s  %s\n' "$checksum" "$filename" | sha256sum --check --strict -; \
    done < downloads.lock; \
    case "$TARGETARCH" in \
      amd64) printf '%s\n' \
        '91794d151f995e680926225816406cc7bb694cef72fc1e1218071a34bca4e4ba pool/main/g/galera-4/galera-4_26.4.27-deb12_amd64.deb' \
        '58ad80ddb35e4dc4310668180cfd16e3b93002c2e8c1731766feb0e538123cf8 pool/main/m/mariadb/libmariadb3_11.8.9+maria~deb12_amd64.deb' \
        '9589c974835aae268fcb8ef298e8de8caba058fa23e16e933947ffdd9c814a94 pool/main/m/mariadb/mariadb-backup_11.8.9+maria~deb12_amd64.deb' \
        'd73db1f7ad4b9384b7b33266c093eb46ad4df9735628c1bba5879d47bc1e2759 pool/main/m/mariadb/mariadb-client_11.8.9+maria~deb12_amd64.deb' \
        '6dfa24c1253de19813ab2029704afc0624389de4bcc0ffc8923f0a79f988459e pool/main/m/mariadb/mariadb-client-core_11.8.9+maria~deb12_amd64.deb' \
        '398b668fc32032d7125bee9c22c3dece37278df1233c5ec02db5c1808fd3ee7b pool/main/m/mariadb/mariadb-server_11.8.9+maria~deb12_amd64.deb' \
        '4cb2b6829a5731a01ed92d571a3fff353529c6ae7e71e88e32cdfbab7bd1540c pool/main/m/mariadb/mariadb-server-core_11.8.9+maria~deb12_amd64.deb' > mariadb.lock ;; \
      arm64) printf '%s\n' \
        'ed6e840c314ecfb683a5aa5e6aa9729ac004d80efd95de177b97d5af15699dbe pool/main/g/galera-4/galera-4_26.4.27-deb12_arm64.deb' \
        '42cb5f40c3a34729fbd8de4f1901d36a9722b31d988eba31b8d97c93dd282452 pool/main/m/mariadb/libmariadb3_11.8.9+maria~deb12_arm64.deb' \
        '14ea0ec125b6deb91ece9545f46d9bba7340b766c0aeef315ba24bbf25c18adf pool/main/m/mariadb/mariadb-backup_11.8.9+maria~deb12_arm64.deb' \
        '14aec9492981d9cfcdd794e8c6af7e0b0e79baae3c58fb33d28f3c5f2408b397 pool/main/m/mariadb/mariadb-client_11.8.9+maria~deb12_arm64.deb' \
        '70f2f1b513d59f437c8896ff285dbf41ca780dcdec4ab8d740fc3c6aa5008327 pool/main/m/mariadb/mariadb-client-core_11.8.9+maria~deb12_arm64.deb' \
        '0e9659fc2e67f5376b8856b6a89362495c56734d9b06f0d4584058d461159028 pool/main/m/mariadb/mariadb-server_11.8.9+maria~deb12_arm64.deb' \
        '050c186b442e4523dce58cbf0e6589b57fa17e1f291c90a42e3c8e8e46f733f0 pool/main/m/mariadb/mariadb-server-core_11.8.9+maria~deb12_arm64.deb' > mariadb.lock ;; \
    esac; \
    printf '%s\n' '8e305390bfb1ef546e4a87eebccc43e7ad87e972cf15e37384d0b7baeddc29ad pool/main/m/mariadb/mariadb-common_11.8.9+maria~deb12_all.deb' >> mariadb.lock; \
    while read -r checksum artifact; do \
      filename="${artifact##*/}"; \
      curl --fail --show-error --location --retry 3 --proto '=https' --proto-redir '=https' \
        "https://archive.mariadb.org/mariadb-11.8.9/repo/debian/$artifact" --output "$filename"; \
      printf '%s  %s\n' "$checksum" "$filename" | sha256sum --check --strict -; \
    done < mariadb.lock; \
    apt-get install --yes --no-install-recommends ./*.deb libllvm19=1:19.1.7-3~deb12u1; \
    install -d /usr/share/k3s-restic; \
    cp downloads.lock /usr/share/k3s-restic/pgdg-downloads.lock; \
    cp mariadb.lock /usr/share/k3s-restic/mariadb-downloads.lock; \
    dpkg-query --show --showformat='${binary:Package}=${Version}\n' > /usr/share/k3s-restic/packages.txt; \
    rm -rf /tmp/image-downloads /var/lib/apt/lists/* /var/lib/mysql /var/lib/postgresql; \
    install -d -m 0700 /data /backup; \
    install -d -o postgres -g postgres -m 0700 /var/lib/postgresql; \
    install -d -o mysql -g mysql -m 0700 /var/lib/mysql

COPY --from=restic-source /usr/bin/restic /usr/local/bin/restic
RUN set -eu; \
    case "$TARGETARCH" in \
      amd64) checksum=20d4142678d0d95ec11a4759def1b73fd9190abc9ca19e4b62d067c0b387e639 ;; \
      arm64) checksum=2fb45ac6f9071b6f20eb883953a188f9e7c7cb6bbe43c67a2e47ada4e85ee7f0 ;; \
      *) exit 1 ;; \
    esac; \
    printf '%s  /usr/local/bin/restic\n' "$checksum" | sha256sum --check --strict -; \
    chmod 0755 /usr/local/bin/restic; restic version

# Preserve the deployed vector0.8.2 SQL identity with pgvector0.8.7 update chain.
# Native library compatibility must still pass the synthetic old-version case.
RUN set -eu; \
    curl --fail --show-error --location --retry 3 --proto '=https' --proto-redir '=https' \
      https://raw.githubusercontent.com/pgvector/pgvector/v0.8.2/sql/vector.sql --output /tmp/vector--0.8.2.sql; \
    printf '%s  /tmp/vector--0.8.2.sql\n' 7fb5bb279ef83bf9204bfac7405bb5c9a05e49f5ac7d64d1eb4464d103b80f32 | sha256sum --check --strict -; \
    for major in 16 17; do \
      install -m 0644 /tmp/vector--0.8.2.sql "/usr/share/postgresql/$major/extension/vector--0.8.2.sql"; \
      test -f "/usr/share/postgresql/$major/extension/vector--0.8.2--0.8.3.sql"; \
    done; rm /tmp/vector--0.8.2.sql

WORKDIR /opt/k3s-restic
COPY k3s_backup/ /opt/k3s-restic/k3s_backup/
COPY scripts/image-selftest.sh /usr/local/bin/image-selftest
COPY tests/container_e2e.py /opt/k3s-restic/tests/container_e2e.py
RUN chmod 0755 /usr/local/bin/image-selftest && image-selftest

# Required redistribution notices stay with the selected binaries.
RUN set -eu; mkdir -p /usr/share/k3s-restic/licenses/restic; \
    curl --fail --show-error --location --retry 3 --proto '=https' --proto-redir '=https' \
      https://raw.githubusercontent.com/restic/restic/v0.19.1/LICENSE \
      --output /usr/share/k3s-restic/licenses/restic/LICENSE; \
    printf '%s  /usr/share/k3s-restic/licenses/restic/LICENSE\n' 6f08a01a9fab5b24e139a09f15cc24a73087c7bc09e3bacf099fdf2d767bf897 | sha256sum --check --strict -

# Build packages, download tools and APT caches never enter the runtime image.
COPY scripts/collect-runtime.py /tmp/collect-runtime.py
RUN set -eu; \
    python3 /tmp/collect-runtime.py --profile files --output /opt/runtime-files; \
    python3 /tmp/collect-runtime.py --profile all --base-profile files --output /opt/runtime-all

# all-in-one inherits the exact lite/file-runner layers, not a re-created base.
FROM scratch AS files
COPY --from=build /opt/runtime-files/ /
ENV PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    PYTHONPATH=/opt/k3s-restic \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LANG=en_US.UTF-8 \
    LC_ALL=en_US.UTF-8 \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    TZ=Asia/Shanghai \
    DATA_PATH=/data \
    BACKUP_WORK_PATH=/backup
WORKDIR /opt/k3s-restic
RUN ["/usr/local/bin/image-selftest"]
ENTRYPOINT ["python3", "-m", "k3s_backup"]
CMD ["schedule"]

# lite is a public label for the same database-free shared runtime.
FROM files AS lite

FROM files AS all
COPY --from=build /opt/runtime-all/ /
RUN ["/usr/local/bin/image-selftest"]

FROM all AS runtime
