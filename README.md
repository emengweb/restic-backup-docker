# Restic Backup Docker：备份与自动最新恢复

基于 restic 的文件备份、原生数据库导出和空卷自动恢复工具，适合 Kubernetes/K3s 的 **恢复 initContainer + 定时备份 sidecar**。运行时使用 Python 3.11 标准库；数据库不是手写 SQL 导出，也不把运行中的数据目录拷贝冒充一致性备份。

> **当前公开版本：v2.0.4。** all/lite 均已完成原生 amd64、arm64 CI、真实合成恢复 E2E、共享层检查、GHCR 发布和签名验证。**发布成功不等于你的生产仓库、存储或数据库版本已经通过恢复验收。** 旧脚本命令不能仅换 image 后继续沿用。

## 1. 镜像、版本和架构

镜像仓库：`ghcr.io/emengweb/restic-backup-docker`。只有以下两种公开变体；没有 PostgreSQL/MariaDB 版本专用标签。

| 变体 | 标签 | 用途 |
| --- | --- | --- |
| **all，默认完整版** | `v2.0.4`、`v2.0.4-all`、`latest` | 文件 + PG16/17 + MariaDB11.8 原生工具 |
| **lite，可选精简版** | `v2.0.4-lite`、`lite` | 文件/restic 自动化；没有数据库工具 |

两种变体均提供 `linux/amd64`、`linux/arm64`，共享同架构的 lite/files 基础层。lite 不是仅有 restic 二进制的官方原始镜像，仍包含 Python 调度、恢复和维护防护。内部 `files` 是共同基础层，不是第三个公开产品。

| 组件 | all | lite |
| --- | --- | --- |
| restic | **0.19.1** | **0.19.1** |
| Python、CA、时区/locale、rsync、shell/归档工具 | 有 | 有 |
| PostgreSQL server + client | **16.15、17.11** 同时存在 | 无 |
| pgvector | **0.8.7**，保留 **0.8.2** 安装 SQL/升级链 | 无 |
| MariaDB server/client/backup 工具 | **11.8.9** | 无 |

PostgreSQL JIT/LLVM 与 Galera 未装入精简运行层；恢复 helper 禁用 JIT。这不是通用数据库主镜像，不应直接替换应用使用的 PostgreSQL/MariaDB 镜像。工具版本、文件闭包和能力取舍见[镜像说明](<docs/IMAGE.md>)、[依赖说明](<docs/PROFILE-DEPENDENCIES.md>)。

### 固定已验证的多架构 index digest

```text
# all：v2.0.4 / v2.0.4-all / latest 在该次发布时指向同一 index
ghcr.io/emengweb/restic-backup-docker@sha256:52c23fa4bd6fa439a4b8e358edc01113aa45337402452e90dff38a388b69f22f
# lite：v2.0.4-lite / lite
ghcr.io/emengweb/restic-backup-docker@sha256:16059ef22add33989b495593e754103b7daf6ecdafe5f14e423d4c97238000ed
```

生产建议固定 index digest，而不是跟随可移动的 `latest`/`lite`。默认无标签 pull 的 `latest` 指 all，不会偷偷改成 lite。v2.0.2 失败流程留下的架构级上传不是完整发布，不应使用。发布与验证：[v2.0.4 release](<https://github.com/emengweb/restic-backup-docker/releases/tag/v2.0.4>)、[成功 CI](<https://github.com/emengweb/restic-backup-docker/actions/runs/42591903536>)；供应链流程见[发布 CI 说明](<docs/PROFILE-CI.md>)及[GHCR 实战经验](<docs/GHCR-PUBLISHING-LESSONS.md>)。

## 2. “客户端能导出”不等于“可以交回原版本主数据库”

| 场景 | 当前代码实际边界 |
| --- | --- |
| 普通文件 | all/lite 均可；restic 保护文件内容不代表跨文件的应用事务一致性 |
| PG 逻辑备份 | 使用目标 major 的 `pg_dump -Fc --create`，逐库完整解码校验；另导出 globals，不含角色密码 |
| PG 逻辑源版本 | 代码允许源 major **12 至所选目标 major**；目标只能是 **16 或 17**，且不允许降级。这是适配器门槛，不是该区间所有应用/扩展均已实测 |
| PG 自动恢复 | 临时启动镜像内 **PG16/17 server** 导入，干净关闭后发布 datadir；没有 PG14 server，**不支持 PG14 原位物理恢复** |
| PG 物理备份 | `pg_basebackup` + WAL + `pg_verifybackup`；仅源/目标 **同为 16 或同为 17**。跨大版本不是这个模式的功能 |
| MariaDB 逻辑备份 | 源系列白名单 `10.11/11.4/11.5/11.8`，目标 **只能 11.8**；旧源必须显式指定 11.8。仅事务性 InnoDB 基表，包含 routines/events/triggers 和 globals/grants |
| MariaDB 自动恢复 | helper 是 **11.8.9**，生成 11.8 datadir。**不能交给未升级的 11.5 主容器启动**；MySQL 不因 mysql 环境别名而获得支持 |
| MariaDB 物理模式 | 当前未实现；即使镜像带 `mariadb-backup`，也不会声称物理模式可用 |

已执行的本项目合成容器恢复证据覆盖 PG16.15、PG17.11 和 MariaDB11.8.9，以及 pgvector0.8.2 SQL 身份；v2.0.4 增加无 passwd 条目目标 UID70 的真实双架构恢复/重启/读写/关闭用例，并用真实 Blinko 快照在异节点恢复后由原 Alpine 固定镜像续启通过。详见[UID 交接与验收](<docs/POSTGRES-UID-HANDOFF.md>)；这不代表所有旧源版本/应用都通过验收。PG 源 14 → 目标 17 是一次明确的逻辑迁移，不是“换备份 sidecar 后自动兼容 PG14”。源/目标 libc、locale、编码、collation、扩展安装 SQL/版本、owner/GRANT、主镜像 UID/GID 均需单独验收。pgvector0.8.2 SQL 存在不代表任意旧扩展都能恢复；自定义扩展 schema、外部 tablespace 等可能直接拒绝。

PG 每个库各自有一致快照，globals 单独捕获；多个库不是分布式同一事务点。MariaDB 数据库集合使用单次事务，但 globals 也独立。并发 DDL 和应用跨库业务一致性仍须写入冻结/应用协作。SQLite/WAL、KeyDB、MinIO 等不能靠热目录文件复制宣称一致，请先停写/使用应用原生备份与一致性 hook。

## 3. 命令和默认行为

容器入口为 `python3 -m k3s_backup`，默认命令 `schedule`。镜像中安装 restic 不等于容器入口就是 restic。

| 命令 | 行为 |
| --- | --- |
| `backup` | 按引擎/窗口创建已验证的导出并上传快照；失败不记录新的成功 |
| `restore-init` | 向可安全恢复的目标自动恢复最新兼容快照，无需手工给 snapshot ID |
| `schedule` | 按配置的 cron 和时区调度 backup/check/prune；不是启动后强制执行一次备份 |
| `check` | 整仓库检查；可选抽样或全量读数据 |
| `prune` | 默认仅维护计划/dry-run，但会先检查仓库；不会默认删数据 |
| `prune --apply` | 只有显式授权、独占维护和持久化七天观察/近期恢复证据满足后才执行 |
| `health` | 返回备份新鲜度状态，不代替数据库/应用 readiness |

### 自动最新恢复的准确含义

- 按稳定 dataset、快照格式/引擎筛选后按时间排序，不按 Pod、节点、主机名或 controller UID 筛选。Pod/节点改名不需要手工找旧 snapshot ID。
- **已有无恢复标记的非空数据卷默认跳过**，不会为了“同步”删除不相关数据；普通已有数据启动不依赖仓库可达。中断恢复只处理自己拥有的 journal/stage。
- **空目标没有可用快照、认证失败、TLS/网络失败时 init 非零退出**；不自动 init Restic 仓库，也不会把空业务数据库作为恢复成功交给主进程。真正首次建库/建仓是另一个显式流程。
- 默认候选最多 5 个，默认最大快照年龄 7 天。允许时可回退到已验证的较旧兼容候选，并报告 `snapshot_time`、`fallback_count`；认证、容量、I/O、所有权错误不靠回退掩盖。
- 节点丢失不会让 local-path PVC 自动跨节点搬迁；控制器/存储必须先提供可挂载目标，并隔离旧写入者。RWO、Lease 或本地 flock 都不独立证明无 split-brain。

## 4. 关键配置

完整别名与维护门槛见[配置合同](<docs/CONFIGURATION.md>)。新配置应优先使用下面名称：

| 变量 | 用途 / 默认值 |
| --- | --- |
| `RESTIC_REPOSITORY`、`RESTIC_PASSWORD[_FILE]` | 沿用原仓库及 Secret；不会遇错后自动建仓、解锁或换地址 |
| `DATA_PATH` / `RECOVERY_DATA_PATH` | 目标或文件源，默认 `/data` |
| `BACKUP_WORK_PATH` | 独立 scratch，默认 `/backup`；不能与数据目录相同或相互包含 |
| `DATABASE_TYPE` | 空/files、postgres/pg/postgresql、mariadb/mysql 别名；mysql 别名仍只适配 MariaDB |
| `DATABASE_HOST/PORT/USER/NAME/PASSWORD[_FILE]` | `DATABASE_NAME` 为显式逗号分隔库列表；同 Pod sidecar 通常连 `127.0.0.1` |
| `RESTORE_TARGET_MAJOR` | 恢复目标；优先于 `TARGET_DATABASE_MAJOR`、`DATABASE_MAJOR`、`PG_MAJOR`。PG16/17 或 Maria11.8 |
| `BACKUP_MODE` | 数据库默认 logical；PG 可选 physical、dual。dual 仅两条流都完成才记整体成功 |
| `DB_UID/DB_GID` | 最终数据目录数字 UID/GID，应匹配主镜像。v2.0.4 的 PG 逻辑恢复先用可解析 helper 账户执行，完整校验/干净关闭后交接最终 owner；已验证 999→70。MariaDB/PG 物理模式不因该修复自动扩展 |
| `DATASET_ID`、`REPOSITORY_ISOLATED` | 默认独立仓库模式，identity 由仓库地址摘要生成；共享仓库必须显式稳定 ID |
| `BACKUP_CRON` | 默认 `0 */6 * * *`；迁移时保留每个项目原值，不统一改频率 |
| `CHECK_CRON`、`PRUNE_CRON` | 默认为空，不调度对应任务 |
| `TZ`、`DATABASE_BACKUP_TIME` | 默认 Asia/Shanghai、`0-23`；数据库导出窗口允许跨午夜，文件模式不受该窗口限制 |
| `JOB_TIMEOUT_SECONDS` | 默认 900 秒 |
| `BACKUP_MAX_BYTES`、`BACKUP_MIN_FREE_BYTES` | 默认 2GiB、256MiB；须按数据规模、原生导出/恢复展开量配置 |
| `RESTORE_MAX_AGE_SECONDS`、`RESTORE_MAX_CANDIDATES` | 默认 604800、5；年龄设 0 才关闭年龄门槛 |
| `RESTIC_DATA_SUBSET` / `CHECK_READ_ALL_DATA` | check 的读数据范围；索引检查通过不等于所有备份块已读回 |

`*_PASSWORD_FILE` 与相应内联变量不可同时给。AWS 凭据也支持 `AWS_ACCESS_KEY_ID_FILE` 等挂载文件；不要把密码、DSN、Access Key 写进命令行、Dockerfile、README、ConfigMap 或日志。原有 hooks 可用 `HOOKS_DIR`；默认搜索 `/hooks`，兼容 `/custom` 和旧拼写 `/custem`，多个定义冲突须显式指定。

### 保留计划，不把窗口跳过当成功

保留原项目的 `BACKUP_CRON/CHECK_CRON/PRUNE_CRON/TZ/DATABASE_BACKUP_TIME`。窗口外数据库任务是 `skipped_window, exported=false`，不是新的成功备份。下方配置值仅为文件测试示例，不能覆盖现有业务计划：

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: backup-settings
data:
  DATABASE_TYPE: files
  DATA_PATH: /data
  BACKUP_WORK_PATH: /backup
  REPOSITORY_ISOLATED: "true"
  BACKUP_CRON: "0 */6 * * *"
  CHECK_CRON: "15 4 * * *"
  PRUNE_CRON: ""
  TZ: Asia/Shanghai
  DATABASE_BACKUP_TIME: "0-23"
  JOB_TIMEOUT_SECONDS: "900"
  BACKUP_MAX_BYTES: "2147483648"
  BACKUP_MIN_FREE_BYTES: "268435456"
```

## 5. K3s/Kubernetes：init 恢复 + sidecar 备份

下面是**合并到现有工作负载的 Pod spec 片段，不是完整可直接 apply 的 Deployment**。原应用容器、探针、端口、调度和存储声明保持原样；不要用这个只显示 backup 的 `containers` 数组覆盖原数组。示例假定 `app-data` PVC、`backup-settings` ConfigMap 和 `backup-repository` Secret 已由管理员创建，并且仓库已有可用备份。

```yaml
spec:
  template:
    spec:
      # 仅当应用本身也不需要 Kubernetes API 时设置 false。
      automountServiceAccountToken: false
      initContainers:
        - name: restore-data
          image: ghcr.io/emengweb/restic-backup-docker@sha256:16059ef22add33989b495593e754103b7daf6ecdafe5f14e423d4c97238000ed
          command: ["python3", "-m", "k3s_backup"]
          args: ["restore-init"]
          envFrom:
            - configMapRef: {name: backup-settings}
          env: &backup-secret-env
            - name: RESTIC_REPOSITORY
              valueFrom:
                secretKeyRef: {name: backup-repository, key: repository}
            - name: RESTIC_PASSWORD
              valueFrom:
                secretKeyRef: {name: backup-repository, key: password}
            # 如后端为 S3，在 init 和 sidecar 同样引用原有 AWS Secret。
          securityContext: &backup-security
            runAsUser: 0
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities:
              drop: ["ALL"]
              add: ["CHOWN", "DAC_OVERRIDE", "SETUID", "SETGID", "FOWNER"]
          resources:
            requests: {cpu: "100m", memory: "128Mi"}
            limits: {cpu: "500m", memory: "768Mi"}
          volumeMounts:
            - {name: app-data, mountPath: /data}
            - {name: restore-work, mountPath: /backup}
            - {name: restore-tmp, mountPath: /tmp}
      containers:
        # 原应用容器仍在此数组中，并挂载同一个 app-data；这里省略其原有定义。
        - name: backup
          image: ghcr.io/emengweb/restic-backup-docker@sha256:16059ef22add33989b495593e754103b7daf6ecdafe5f14e423d4c97238000ed
          command: ["python3", "-m", "k3s_backup"]
          args: ["schedule"]
          envFrom:
            - configMapRef: {name: backup-settings}
          env: *backup-secret-env
          securityContext: *backup-security
          resources:
            requests: {cpu: "100m", memory: "128Mi"}
            limits: {cpu: "500m", memory: "768Mi"}
          volumeMounts:
            - {name: app-data, mountPath: /data, readOnly: true}
            - {name: backup-work, mountPath: /backup}
            - {name: backup-tmp, mountPath: /tmp}
      volumes:
        - name: app-data
          persistentVolumeClaim: {claimName: app-data}
        - name: restore-work
          emptyDir: {sizeLimit: 4Gi}
        - name: backup-work
          emptyDir: {sizeLimit: 4Gi}
        - name: restore-tmp
          emptyDir: {sizeLimit: 512Mi}
        - name: backup-tmp
          emptyDir: {sizeLimit: 512Mi}
```

此片段没有 Secret 内容。容量仅示意：恢复可能同时存在下载归档和展开目录，2GiB 导出上限不表示4GiB scratch必然足够；`emptyDir.sizeLimit` 是容量/驱逐约束，不是同步硬磁盘 quota。若使用 Secret 文件，挂载只读 Secret volume 并改用 `RESTIC_PASSWORD_FILE`/`DATABASE_PASSWORD_FILE`，不要同时保留对应内联变量。

### 数据库模式如何改

1. init 与 sidecar 都换成相同 **all digest**，数据库主容器依旧使用独立、已验收的数据库镜像。
2. 用 ConfigMap 配置实际 `DATABASE_TYPE/HOST/PORT/NAME` 和明确目标 major；通过原有 Secret 引用 `DATABASE_USER/PASSWORD`。同 Pod sidecar 连接 `127.0.0.1`，避免数据库未 Ready 导致 Service 无 endpoints 的备份依赖死锁。
3. init 挂载的是未来主数据库的**空目标数据目录**，其子目录、UID/GID 必须与主镜像一致；sidecar 做逻辑导出不需要挂载 PGDATA。main 必须等 init 成功后才启动，不能与恢复 helper 同时写入。
4. PG globals 不备份角色密码；当前数据库用户可从配置注入，其他登录角色需管理员提供 `RESTORE_ROLE_PASSWORDS_FILE` 映射并保护文件。角色/所有权/GRANT/实际应用读写都要演练，不把恢复后有表当全部验收。
5. PG14 原主镜像、MariaDB11.5 原主镜像**不能直接套用**上面的 helper 数据目录。当前已发布 all 没有 PG14/MariaDB11.5 server helper，不能靠设置 `DATABASE_MAJOR=14` 或 `11.5` 获得对应能力。先另行提供并验收匹配 helper，或批准全链路逻辑迁移/新主版本/新卷/停写切换及回退计划。

任何真实替换前，请阅读[K3s 接入与回退边界](<docs/K3S-ROLLOUT.md>)，从原仓库恢复到隔离新卷并用实际主镜像启动。不能为了腾迁移空间删除旧 PVC、历史备份或停止中的项目。

### 停用项目与超过7天的历史快照

停用项目也要检查模板中的旧 init/sidecar 命令、镜像和挂载，但**保留 replicas=0/原停用状态**，更新模板不是启动授权。空卷没有备份时不得先启动应用“试试看”。应先在隔离目标完成恢复、核对应用可启动条件，再由管理员明确批准启动。

超过默认7天年龄门槛的历史快照，需要管理员核实最后一次成功时间、原仓库归属、完整性和接受的 RPO 后，才可在**这次受控恢复**配置 `RESTORE_MAX_AGE_SECONDS=0`。这只关闭年龄过滤，不关闭格式、checksum、引擎、权限或空卷保护，也不证明旧快照完整；不要把它全局设为0掩盖备份长期失效。独立仓库的旧 `/data` 文件快照默认可参与 legacy 筛选；旧数据库物理目录仍需 `RESTORE_LEGACY_PHYSICAL=true` 及原生兼容校验，旧 PG custom archive 必须满足 checksum/header/metadata 条件。恢复验收后按策略恢复年龄门槛，主容器再启动。

## 6. 检查、保留与清理

`check`/`prune` 是**整仓库操作**，不是按 Pod 名称清理。默认无自动建仓、unlock、pack/snapshot 修复；索引修复也只有额外维护门槛满足时才可能执行，不能修复丢失的数据块。

清理默认 dry-run；即使设置原 `RESTIC_FORGET_ARGS`，也保留至少7天和每流至少一个恢复点。实际 `prune --apply` 需要 `RETENTION_APPLY=true`、`MAINTENANCE_EXCLUSIVE=true`、持久化 `RETENTION_PROOF_FILE`：匹配 identity、真实稳定观察至少7天、无未解决失败、近期实际恢复的最新 snapshot 证据。**容器运行满7天、一个布尔值或仓库后端换新不等于获得删除授权。** 观察从每个 dataset 的真实恢复验收后起算。

跨 Pod/节点的写入隔离和维护 fencing 由外部部署策略负责；本地 flock 与 Restic 单命令锁不能保证整个多命令维护期间没有分裂写入者。沿用原项目保留策略，并先审计划；不要机械开启全部项目 prune。

## 7. 测试与供应链

合成单测/真实本地 Restic 集成：

```sh
RESTIC_BIN=/path/to/verified/restic python3 -m unittest discover -s tests -v
python3 tests/container_e2e.py --self-test
python3 tests/container_e2e.py --help
```

原生容器 E2E 仅用于已审查、已修补 runtime 的独立测试 daemon，必须显式选择 daemon/image/runtime/profile；不挂生产数据、密钥或 Docker socket，不把 skipped/NOT RUN 算通过。CI 对 all/lite × amd64/arm64 四种组合都执行实际测试，然后发布同一已测镜像归档，不在发布阶段重建。

仓库保留上游许可证，见[LICENSE](<LICENSE>)。版本化下载、文件闭包和签名证据见[发布说明](<docs/RELEASE-v2.0.4.md>)、[发布 CI](<docs/PROFILE-CI.md>)、[GHCR 实战经验](<docs/GHCR-PUBLISHING-LESSONS.md>)。旧 Node dump/自动 restic init/不受控 restore latest 到 `/` 的历史用法不属于当前使用合同。
