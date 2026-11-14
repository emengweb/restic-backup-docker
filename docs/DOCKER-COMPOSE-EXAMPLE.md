# Docker Compose：备份、自动最新恢复与数据库启动门槛

> 本文是待管理员配置真实仓库/Secret 后使用的模板，不是生产启动或迁移授权。这里只用合成配置和静态测试审查，**没有运行 Docker/Compose、访问 daemon 或读取真实 Secret**。必须先在隔离新卷验证恢复和实际应用续启；不要对现有生产目录直接执行下列命令。

## 0. 先理解镜像入口与适用范围

- 本项目 Dockerfile 已设置 `ENTRYPOINT ["python3", "-m", "k3s_backup"]`。所以 Compose 的正确写法是 `command: ["schedule"]`、`command: ["restore-init"]`，CLI 覆盖为 `docker compose run --rm backup health`；**不能再追加一遍 python/module 入口**。
- 当前固定版本 v2.0.4：all index `sha256:52c23fa4bd6fa439a4b8e358edc01113aa45337402452e90dff38a388b69f22f`，lite index `sha256:16059ef22add33989b495593e754103b7daf6ecdafe5f14e423d4c97238000ed`。init 和 backup 必须同一变体、同一 digest。
- 使用 Docker Compose V2，须支持 `depends_on.condition: service_completed_successfully`。它只保证 **Compose 启动流程**的先后关系；不能阻止其他 Compose project、手工 `docker start` 或 daemon 对原容器的 restart policy 绕过门槛。示例业务 `restart: "no"`，需要受门控的进程管理器再考虑自动重启。
- 目标必须是已确认的新/空卷，或该 runner 有权继续的恢复 journal。非空无标记目录默认 `skipped` 是保护旧数据，不是刚从仓库恢复的证据。空目标无兼容快照/认证/TLS/网络失败时恢复非零退出，主服务必须保持停止。没有自动 `restic init`、`unlock`、清空目录或启动空数据库。
- `DATASET_ID` 稳定且与原快照一致，不能来自新 hostname/Pod/节点名。已有仓库之前依赖 URI-derived identity 时不要随意新增不同 ID；应沿用原身份。Compose 本地 volume 不自动跨节点复制；跨主机恢复需在新节点正确重建挂载、连接原仓库并隔离旧写入者。

## 1. 非敏感配置与真实 Secret 文件

两个完整 Compose 示例是**二选一**，分别保存为 `compose.files.yaml` 或 `compose.mariadb.yaml`。不要把两份同名服务混在一个 project。`.env` 只放非敏感参数，必须沿用项目原周期、窗口、时区和 identity。下面使用必填插值，没有有效值时 Compose 应直接报错，不能默默使用占位仓库。

```dotenv
# .env：下面周期只是新合成项目示意；已有项目必须填回原值。
BACKUP_CRON="0 */6 * * *"
CHECK_CRON=
PRUNE_CRON=
TZ=Asia/Shanghai
DATABASE_BACKUP_TIME=0-23
REPOSITORY_ISOLATED=true
# 管理员必须另外填写真实的非敏感配置：
# RESTIC_REPOSITORY、DATASET_ID、DATA_VOLUME_NAME、APPLICATION_IMAGE（文件例）
# 或 RESTIC_REPOSITORY、DATASET_ID、DATABASE_NAME、DB_VOLUME_NAME、
# MARIADB_IMAGE、DB_UID、DB_GID（MariaDB例）。不设置密码变量。
```

不生成 `REPLACE_PASSWORD` 这样的假密码文件。以下是管理员在终端**隐藏输入既有密码**的 Bash 示例；备份仓库必须使用原密码，不能另造密码读旧仓库。若有 Secret 管理系统，优先安全导出到相应文件，不在 shell argv/history 中放值。

```bash
set -euo pipefail
umask 077
mkdir -p secrets
chmod 700 secrets
if test -e secrets/restic-password; then
  printf "%s\n" "Secret 文件已存在，保留原文件，不覆盖" >&2
  exit 1
fi
IFS= read -r -s -p "Existing Restic repository password: " secret_value
printf "\n" >&2
test -n "$secret_value"
(set -o noclobber; printf "%s" "$secret_value" > secrets/restic-password)
unset secret_value
chmod 600 secrets/restic-password
```

S3 仓库还需将原 access key/secret key 分别放入 `secrets/s3-access-key`、`secrets/s3-secret-key`（同样隐藏输入/安全导出，0700目录/0600文件），不把值放 `.env`、Compose 或命令行；不要以改后端地址/关 TLS 绕过连接失败。两个示例配置了 S3 Secret 引用；若使用非 S3 本地/SFTP/REST 后端，删除 AWS 两个环境变量及对应 Secret 声明/挂载，并按后端另行配置认证。

Compose 本地 `secrets.file` 通常是只读 bind mount，**不是 Docker 自动加密的保险库**，也不能依赖 long syntax 的 mode/uid/gid 一定改变宿主文件权限。真实 Secret 文件、`.env`、数据和导出目录都不应提交 Git。

## 2. 文件场景：lite 的完整门控示例

设置 `APPLICATION_IMAGE` 为已审查的**应用固定 digest**（不会提供一个不相关业务镜像充数），应用必须在 `/data` 使用数据并遵守其本身的 UID/读写需求。`DATA_VOLUME_NAME` 指向管理员预先创建的专用 external volume；existing 非空卷会跳过，真正恢复演练应使用另一个已批准的新空 volume。

```yaml
# compose-example: files
name: restic-files-example
x-lite-image: &lite-image ghcr.io/emengweb/restic-backup-docker@sha256:16059ef22add33989b495593e754103b7daf6ecdafe5f14e423d4c97238000ed
x-helper-security: &helper-security
  user: "0:0"
  read_only: true
  cap_drop: [ALL]
  cap_add: [CHOWN, DAC_OVERRIDE, SETUID, SETGID, FOWNER]
  security_opt: ["no-new-privileges:true"]
  tmpfs: ["/tmp:rw,noexec,nosuid,nodev,size=512m,mode=1777"]
  mem_limit: 768m
  cpus: "0.5"
  pids_limit: 192
x-repository: &repository
  RESTIC_REPOSITORY: ${RESTIC_REPOSITORY:?set_existing_repository}
  RESTIC_PASSWORD_FILE: /run/secrets/restic-password
  AWS_ACCESS_KEY_ID_FILE: /run/secrets/s3-access-key
  AWS_SECRET_ACCESS_KEY_FILE: /run/secrets/s3-secret-key
  DATASET_ID: ${DATASET_ID:?set_existing_dataset_identity}
  REPOSITORY_ISOLATED: ${REPOSITORY_ISOLATED:-true}
  DATA_PATH: /data
  BACKUP_WORK_PATH: /backup
  DATABASE_TYPE: files
  TZ: ${TZ:?set_existing_timezone}
  BACKUP_CRON: ${BACKUP_CRON:?set_existing_backup_cron}
  CHECK_CRON: ${CHECK_CRON-}
  PRUNE_CRON: ${PRUNE_CRON-}
  DATABASE_BACKUP_TIME: ${DATABASE_BACKUP_TIME:?set_existing_window}
  BACKUP_MAX_BYTES: ${BACKUP_MAX_BYTES:-2147483648}
  BACKUP_MIN_FREE_BYTES: ${BACKUP_MIN_FREE_BYTES:-268435456}
  JOB_TIMEOUT_SECONDS: ${JOB_TIMEOUT_SECONDS:-900}
services:
  restore-init:
    <<: *helper-security
    image: *lite-image
    command: ["restore-init"]
    restart: "no"
    environment: *repository
    secrets: [restic-password, s3-access-key, s3-secret-key]
    volumes:
      - data:/data
      - restore-work:/backup
  app:
    image: ${APPLICATION_IMAGE:?set_reviewed_application_digest}
    restart: "no"
    depends_on:
      restore-init: {condition: service_completed_successfully}
    volumes: ["data:/data"]
  backup:
    <<: *helper-security
    image: *lite-image
    command: ["schedule"]
    restart: "no"
    depends_on:
      app: {condition: service_started}
    environment: *repository
    secrets: [restic-password, s3-access-key, s3-secret-key]
    volumes:
      - data:/data:ro
      - backup-work:/backup
volumes:
  data:
    external: true
    name: ${DATA_VOLUME_NAME:?set_approved_target_volume}
  restore-work: {}
  backup-work: {}
secrets:
  restic-password: {file: ./secrets/restic-password}
  s3-access-key: {file: ./secrets/s3-access-key}
  s3-secret-key: {file: ./secrets/s3-secret-key}
```

这里确实定义了 `app`，而非让命令引用不存在的服务。init 与 app 挂同一数据卷；backup 对源只读；restore/backup 各有独立 `/backup`，都不在 `/data` 里。应用如果使用 SQLite/WAL、KeyDB、MinIO 或并发跨文件事务，必须另外实现冻结/原生导出/一致性 hook；按文件备份通过不代表应用一致。

### 启动/恢复：一个失败就停止

下面命令仅在管理员已确认目标与来源后执行。`set -e` 是必要门槛，恢复失败时不可继续执行 up；**不要**把 `docker compose up` 放在恢复命令之后用无条件 `;` 连接。

```bash
# compose-operation: files
set -euo pipefail
docker compose -f compose.files.yaml config --quiet
# 先停 app/backup，确认其他 project/主机/用户也不再写同一数据卷。
docker compose -f compose.files.yaml stop app backup
# 不经过 app 启动，只有被动恢复服务运行；每次 run 都会重新执行恢复命令。
docker compose -f compose.files.yaml run --rm --no-deps restore-init
# 审核 restore-init 输出是 restored 或预期的已完成/非空跳过，再续启。
docker compose -f compose.files.yaml up -d app backup
```

Compose `depends_on` 是额外启动保护，不替代上面的停写、fresh restore 和结果审查。不要 `--no-deps up app`、不要手动 start 一个旧 app 容器跳过恢复；不要通过 `down -v` 清空卷来制造“空卷条件”。跨节点同样不能让旧写入者复活/双写。

## 3. MariaDB11.8：all 的完整门控示例

### 先检查目标版本和备份账户权限

- **这里恢复/主服务都为11.8系列。** `MARIADB_IMAGE` 必须是管理员核验的 MariaDB11.8.9 官方主镜像固定 digest，需匹配目标 UID/GID/架构，并先做续启验收。不要填写11.5镜像，也不允许11.8打开旧11.5物理卷。迁移11.5→11.8必须先新卷逻辑预演、停写最终快照、验证后切换并保留旧卷；不是直接套本 Compose。
- `MARIADB_USER=app` 通常只具有 `app.*` 权限，**不够**当前导出器读取 `mysql.user`、`mariadb-dump --system=users` 所需的完整 users/roles/grants，也不能把省略 globals 当成功。
- 为提供可执行的**管理员级参考基线**，本例只在共享 Unix socket 上使用 `root@localhost` + 已有 root Secret 导出所有 globals（及显式列出的库），不设置 `MARIADB_ROOT_HOST=%`，不开放数据库宿主端口，也不把 root 密钥交给普通应用。备份服务只挂 socket，不挂 datadir。
- 此 root 模型是高权限取舍，不宣称最小权限。生产若换专用 backup 账号，须管理员根据源版本和所有账户/权限实际验收对库数据、views/routines/events/triggers、系统账户/角色、`SHOW CREATE USER`、`SHOW GRANTS`、认证 plugin/hash 和 PROXY 的完整访问，不能只授 `SELECT ON app.*`。Globals 恢复还需要授权/PROXY能力：当前源码 helper 在自建隔离库内赋予临时账户 `GRANT ALL ... WITH GRANT OPTION` 与 `GRANT PROXY ... WITH GRANT OPTION`，这不是给源应用账户或生产库自动提权。文档固定digest不是把本地源码变更自动发布：实际使用前须对该已发布artifact验收完整globals/PROXY恢复；如果artifact缺能力应保持停止，由维护者发布验证过的修复版本，不能跳过globals或改为空库启动。

先安全准备 `secrets/db-root-password`（原库 root 密码，不是新造占位值）。若主镜像初始化变量用于一个已有完整 datadir，它们一般不会重建用户/改密码；恢复出的应用身份/权限来自 globals，必须另行用业务用户验证。此例无 `MARIADB_DATABASE/MARIADB_USER/MARIADB_PASSWORD` 自动建库配置，避免把缺备份误作首次部署。

```yaml
# compose-example: mariadb
name: restic-mariadb-example
x-all-image: &all-image ghcr.io/emengweb/restic-backup-docker@sha256:52c23fa4bd6fa439a4b8e358edc01113aa45337402452e90dff38a388b69f22f
x-helper-security: &helper-security
  user: "0:0"
  read_only: true
  cap_drop: [ALL]
  cap_add: [CHOWN, DAC_OVERRIDE, SETUID, SETGID, FOWNER]
  security_opt: ["no-new-privileges:true"]
  tmpfs: ["/tmp:rw,noexec,nosuid,nodev,size=512m,mode=1777"]
  mem_limit: 768m
  cpus: "0.5"
  pids_limit: 192
x-db-repository: &db-repository
  RESTIC_REPOSITORY: ${RESTIC_REPOSITORY:?set_existing_repository}
  RESTIC_PASSWORD_FILE: /run/secrets/restic-password
  AWS_ACCESS_KEY_ID_FILE: /run/secrets/s3-access-key
  AWS_SECRET_ACCESS_KEY_FILE: /run/secrets/s3-secret-key
  DATASET_ID: ${DATASET_ID:?set_existing_dataset_identity}
  REPOSITORY_ISOLATED: ${REPOSITORY_ISOLATED:-true}
  DATABASE_TYPE: mariadb
  DATABASE_HOST: /run/mysqld/mysqld.sock
  MARIADB_SOCKET: /run/mysqld/mysqld.sock
  DATABASE_PORT: "3306"
  DATABASE_USER: root
  DATABASE_PASSWORD_FILE: /run/secrets/db-root-password
  DATABASE_NAME: ${DATABASE_NAME:?set_exact_snapshot_database_list}
  RESTORE_TARGET_MAJOR: "11.8"
  BACKUP_MODE: logical
  DATA_PATH: /data
  BACKUP_WORK_PATH: /backup
  DB_UID: ${DB_UID:?set_reviewed_mariadb_uid}
  DB_GID: ${DB_GID:?set_reviewed_mariadb_gid}
  TZ: ${TZ:?set_existing_timezone}
  BACKUP_CRON: ${BACKUP_CRON:?set_existing_backup_cron}
  CHECK_CRON: ${CHECK_CRON-}
  PRUNE_CRON: ${PRUNE_CRON-}
  DATABASE_BACKUP_TIME: ${DATABASE_BACKUP_TIME:?set_existing_window}
  BACKUP_MAX_BYTES: ${BACKUP_MAX_BYTES:-2147483648}
  BACKUP_MIN_FREE_BYTES: ${BACKUP_MIN_FREE_BYTES:-268435456}
  JOB_TIMEOUT_SECONDS: ${JOB_TIMEOUT_SECONDS:-900}
services:
  restore-init:
    <<: *helper-security
    image: *all-image
    command: ["restore-init"]
    restart: "no"
    environment: *db-repository
    secrets: [restic-password, s3-access-key, s3-secret-key, db-root-password]
    volumes:
      - db-data:/data
      - restore-work:/backup
  mariadb:
    image: ${MARIADB_IMAGE:?set_reviewed_mariadb_11_8_9_digest}
    command: ["--socket=/run/mysqld/mysqld.sock"]
    restart: "no"
    depends_on:
      restore-init: {condition: service_completed_successfully}
    environment:
      MARIADB_ROOT_PASSWORD_FILE: /run/secrets/db-root-password
    secrets: [db-root-password]
    volumes:
      - db-data:/var/lib/mysql
      - db-socket:/run/mysqld
    healthcheck:
      test: ["CMD-SHELL", "MYSQL_PWD=\"$$(cat /run/secrets/db-root-password)\" mariadb --no-defaults --protocol=socket --socket=/run/mysqld/mysqld.sock --user=root --batch --skip-column-names --execute=\"SELECT 1\" >/dev/null"]
      interval: 10s
      timeout: 5s
      retries: 12
      start_period: 30s
  backup:
    <<: *helper-security
    image: *all-image
    command: ["schedule"]
    restart: "no"
    depends_on:
      mariadb: {condition: service_healthy}
    environment: *db-repository
    secrets: [restic-password, s3-access-key, s3-secret-key, db-root-password]
    volumes:
      - db-socket:/run/mysqld:ro
      - backup-work:/backup
volumes:
  db-data:
    external: true
    name: ${DB_VOLUME_NAME:?set_approved_target_volume}
  db-socket: {}
  restore-work: {}
  backup-work: {}
secrets:
  restic-password: {file: ./secrets/restic-password}
  s3-access-key: {file: ./secrets/s3-access-key}
  s3-secret-key: {file: ./secrets/s3-secret-key}
  db-root-password: {file: ./secrets/db-root-password}
```

健康检查使用本例明确挂载的 root Secret 和共享本地 socket 执行固定 `SELECT 1`，不依赖新装官方镜像才有的健康检查账户。`$$` 是 Compose 的美元符号转义：密码只在容器内读取进入短生命周期 `MYSQL_PWD` 进程环境，既不成为 Compose插值也不进入argv；不要对该命令开启shell trace或打印环境。生产可以换成已验收的专用只读健康检查账户及0600客户端选项文件；不能把失败probe改成恒真。这个probe只放行backup等待，不代替业务角色读写、InnoDB对象/数据、恢复RPO的验收。

```bash
# compose-operation: mariadb
set -euo pipefail
docker compose -f compose.mariadb.yaml config --quiet
# 应用写端可能在另一个 Compose project/宿主机，必须先逐个停写并隔离。
docker compose -f compose.mariadb.yaml stop backup mariadb
docker compose -f compose.mariadb.yaml run --rm --no-deps restore-init
# 核对目标版本、globals/grants/charset/对象及数据聚合；未通过不继续。
docker compose -f compose.mariadb.yaml up -d mariadb backup
# 业务应用只有在目标库和其原身份/读写验收通过后，另行批准启动。
```

MariaDB 备份服务不直接读 PGDATA/datadir，`/data` 是恢复目标标识，不需要为逻辑导出挂一个假的 backup-data volume。恢复服务不得依赖正在运行的 mariadb（否则形成恢复必须先建空库的循环）。镜像有 `mariadb-backup` 不代表当前 runner 已实现 MariaDB物理模式。请不要让 MySQL 通过 mysql 别名误入这个 MariaDB模型。

## 4. 观察、检查和禁止绕过项

```bash
# compose-observation: files
docker compose -f compose.files.yaml ps
docker compose -f compose.files.yaml run --rm --no-deps backup health
docker compose -f compose.files.yaml run --rm --no-deps backup check
# 审查后只看必要脱敏事件，不把整段诊断/业务日志直接提交到工单或仓库。
```

- `health` 可能输出 not_yet_backed_up/stale；结构化 JSON 的状态也要检查，不只看进程 exit0。
- 原有 `BACKUP_CRON/CHECK_CRON/PRUNE_CRON/TZ/DATABASE_BACKUP_TIME` 保持；窗口外是 skipped，不是新鲜备份。prune默认dry-run，七天观察和实际恢复证据之外还需独占维护/授权；本文不启用 destructive prune。
- 历史快照超过默认7天时，只有人工审核时间/RPO后才对受控恢复使用 `RESTORE_MAX_AGE_SECONDS=0`；这不绕过 checksum、引擎和权限检查。停用项目保持停用，先恢复验证再申请启动。
- 从旧原生 SQL exporter、PG14物理目录、MariaDB11.5旧 datadir迁移须另案；不能把一次合成Compose解析或某一源库预演外推为所有库恢复成功。
- `backup-work/restore-work` 和数据库展开/WAL/redo均要有真实容量预算；Compose volume与tmpfs上限不等于整个宿主磁盘有硬quota。恢复先停所有写入者；禁止 down-v、删除旧卷、关闭TLS、重建/解锁原仓库等“兜底”。

## 5. 静态验证记录

本修订由现行 Dockerfile ENTRYPOINT、CLI参数和 MariaDB导出/恢复实现核对，新增[test_compose_docs.py](<../tests/test_compose_docs.py>)：验证入口不叠加、固定digest、所有引用服务/Secret/volume存在、恢复成功启动门槛、独立work、MariaDBadmin/socket模型。只做合成/静态验证；**没有运行 docker compose config、容器E2E、生产恢复或数据库授权SQL**。若实际使用，管理员仍应在隔离环境执行配置解析与端到端恢复。

相关：[中文README](<../README.md>)、[配置合同](<CONFIGURATION.md>)、[K3s接入边界](<K3S-ROLLOUT.md>)。
