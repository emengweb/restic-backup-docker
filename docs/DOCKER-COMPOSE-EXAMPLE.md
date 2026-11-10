# Docker Compose 备份与恢复示例

> 示例只说明配置结构，不包含真实仓库地址、密码、Access Key 或生产数据。生产必须替换为现有项目的 Secret/凭据，并先在隔离目标验证恢复。

## 1. 目录与 Secret

~~~text
compose-restic/
├── compose.yaml
├── .env
├── data/
├── backup-work/
└── secrets/restic-password
~~~

~~~bash
mkdir -p data backup-work secrets
printf '%s\n' 'REPLACE_WITH_EXISTING_RESTIC_PASSWORD' > secrets/restic-password
chmod 600 secrets/restic-password
~~~

.env 示例（不要提交）：

~~~dotenv
RESTIC_REPOSITORY=s3:https://s3.example.invalid/project-repo
BACKUP_CRON=0 */6 * * *
CHECK_CRON=0 1-4 * * *
PRUNE_CRON=
TZ=Asia/Shanghai
DATASET_ID=compose-project
REPOSITORY_ISOLATED=true
~~~

不要把密码、DSN 或 S3 密钥写入 Compose/Git；优先使用 Docker secrets 与 *_FILE 变量。

## 2. 纯文件场景：lite

~~~yaml
services:
  backup:
    image: ghcr.io/emengweb/restic-backup-docker@sha256:16059ef22add33989b495593e754103b7daf6ecdafe5f14e423d4c97238000ed
    restart: unless-stopped
    command: ["python3", "-m", "k3s_backup", "schedule"]
    env_file: [.env]
    environment:
      DATABASE_TYPE: files
      DATA_PATH: /data
      BACKUP_WORK_PATH: /backup
      RESTIC_PASSWORD_FILE: /run/secrets/restic-password
    secrets: [restic-password]
    volumes:
      - ./data:/data:ro
      - ./backup-work:/backup
    read_only: true
    tmpfs:
      - /tmp:rw,noexec,nosuid,nodev,size=128m
  restore-init:
    image: ghcr.io/emengweb/restic-backup-docker@sha256:16059ef22add33989b495593e754103b7daf6ecdafe5f14e423d4c97238000ed
    profiles: [restore]
    command: ["python3", "-m", "k3s_backup", "restore-init"]
    env_file: [.env]
    environment:
      DATABASE_TYPE: files
      DATA_PATH: /data
      RECOVERY_DATA_PATH: /data
      BACKUP_WORK_PATH: /backup
      RESTIC_PASSWORD_FILE: /run/secrets/restic-password
    secrets: [restic-password]
    volumes:
      - ./data:/data
      - ./backup-work:/backup
    read_only: true
    tmpfs:
      - /tmp:rw,noexec,nosuid,nodev,size=128m
secrets:
  restic-password:
    file: ./secrets/restic-password
~~~

备份：docker compose up -d backup。恢复前停止所有写入 /data 的服务；空目录才允许自动恢复，非空目录默认保护：

~~~bash
docker compose stop app backup
docker compose --profile restore run --rm restore-init
docker compose up -d app backup
~~~

没有兼容快照、认证/TLS/网络失败时，init 非零退出，不会自动 init 仓库或启动空应用。

## 3. MariaDB 场景：all

~~~yaml
services:
  mariadb:
    image: mariadb:11.8.9
    environment:
      MARIADB_DATABASE: app
      MARIADB_USER: app
      MARIADB_PASSWORD_FILE: /run/secrets/db-password
      MARIADB_ROOT_PASSWORD_FILE: /run/secrets/db-root-password
    secrets: [db-password, db-root-password]
    volumes: [db-data:/var/lib/mysql]
  backup:
    image: ghcr.io/emengweb/restic-backup-docker@sha256:52c23fa4bd6fa439a4b8e358edc01113aa45337402452e90dff38a388b69f22f
    restart: unless-stopped
    command: ["python3", "-m", "k3s_backup", "schedule"]
    env_file: [.env]
    environment:
      DATABASE_TYPE: mariadb
      DATABASE_HOST: mariadb
      DATABASE_PORT: "3306"
      DATABASE_NAME: app
      DATABASE_USER: app
      DATABASE_PASSWORD_FILE: /run/secrets/db-password
      RESTIC_PASSWORD_FILE: /run/secrets/restic-password
      RESTORE_TARGET_MAJOR: "11.8"
      BACKUP_MODE: logical
      DATA_PATH: /backup-data
      BACKUP_WORK_PATH: /backup-work
    secrets: [restic-password, db-password]
    volumes: [backup-data:/backup-data, backup-work:/backup-work]
volumes:
  db-data: {}
  backup-data: {}
  backup-work: {}
secrets:
  restic-password:
    file: ./secrets/restic-password
  db-password:
    file: ./secrets/db-password
  db-root-password:
    file: ./secrets/db-root-password
~~~

MariaDB 11.5.2 数据必须先在新卷迁移到 11.8 LTS 并完成隔离恢复、业务读写和回滚验证；不能让 11.8 helper 生成的 datadir 回交 11.5 主容器。

## 4. 检查与边界

~~~bash
docker compose config
docker compose ps
docker compose logs --tail=200 backup
docker compose run --rm backup python3 -m k3s_backup health
~~~

保留原仓库、Secret、周期、窗口和时区。清理默认 dry-run，至少七天稳定观察和实际恢复验证后再评估 prune；本文不授权删除历史快照。
