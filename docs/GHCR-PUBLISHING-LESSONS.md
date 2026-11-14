# GHCR 发布实战：失败模式、修复及验收边界

> 这是一份中文复盘，不是启动发布或生产变更的授权。记录截至 **2026-11-14** 已发生的 CI/发布问题，以及一次发布前只读检查发现的问题；二者严格区分。本文件本轮只做文档整理，没有执行 commit、push、tag、登录仓库、构建或生产操作。

## 1. 最终状态与时间线

最终合同仅有 **all（默认）+ lite（无数据库）** 两变体，两者均发布 native `linux/amd64`、`linux/arm64`。all 的完整数据库 E2E 仍保留，减少标签不是减少支持工具或测试。

| 阶段 | 真实结果 | 不应得出的结论 |
| --- | --- | --- |
| 开发分支首轮 CI | 镜像构建后，过大的 metadata 环境变量导致 `Argument list too long`，E2E 未到达 | “镜像 build 过，所以发布可用” |
| 修复 metadata 后的一轮 main CI | lite 部分通过，随后被新提交的 concurrency 取消 | “取消前通过的任务代表整个矩阵通过” |
| main 稳定 CI | all/lite × 两原生架构、共享基础层校验通过；发布任务 skipped | “branch CI 绿色意味着已经 push GHCR” |
| v2.0.0 | Restic GitHub asset 多次 HTTP504、Syft 获取失败，发布未完成 | “网络失败可以绕过 checksum/SBOM/signature” |
| v2.0.1 | Restic 改固定官方镜像提取后功能 E2E 通过；Syft asset 仍504，发布未完成 | “E2E 通过足以发布缺 SBOM 的镜像” |
| v2.0.2 | 四种 build/E2E/SBOM/共享层通过；四个架构镜像已上传，但后置 checkout 清掉已验证 identity/evidence，签名/index 未完成 | “已有架构 tag 就是完成的多架构 release” |
| v2.0.3 | checkout顺序修复，完整签名/attestation/index核验完成 | “公开镜像成功等于真实生产仓库恢复或主数据库兼容已验收” |
| v2.0.4 | PG helper与最终UID分离，原生双架构续启/读写及签名发布通过 | “UID交接修复扩展了MariaDB或旧PG大版本兼容” |
| 认证修复首轮CI43544760801 | 273项unit通过，但纯harness测试因non-root宿主UID影响模拟journal owner而失败 | “unit通过就能忽略harness失败” |
| 修复模拟后main43545271593 | 调整纯stat模拟后双native全套通过 | “mock回归通过足以证明所有真实权限语义一致” |
| v2.0.5 / run43547130746 | 双native真实MariaDB认证/授权等价guard失败，未完成发布，不可用于生产 | “PROXY精确集与认证相同，就没有别的GRANT变化” |
| **v2.0.6 / run43548816524** | 源a559517：必需全局GRANT OPTION元数据与保守对齐，双native全套、两index签名、all/lite×两架构签名及SBOM/provenance通过并由Lead独立核验 | “已迁v2.0.3 lite/v2.0.4 all会自动升级，或所有生产实例迁移完成” |

公开证据：[开发首轮](<https://github.com/emengweb/restic-backup-docker/actions/runs/42564415367>)、[被取消的过渡运行](<https://github.com/emengweb/restic-backup-docker/actions/runs/42565164318>)、[稳定 main CI](<https://github.com/emengweb/restic-backup-docker/actions/runs/42565452078>)、[v2.0.3 最终运行](<https://github.com/emengweb/restic-backup-docker/actions/runs/42570334098>)、[v2.0.3 release](<https://github.com/emengweb/restic-backup-docker/releases/tag/v2.0.3>)。发布修复说明保存在[v2.0.1](<RELEASE-v2.0.1.md>)、[v2.0.2](<RELEASE-v2.0.2.md>)、[v2.0.3](<RELEASE-v2.0.3.md>)。旧 tag 保持不动；不通过强推/移动旧 tag 隐藏失败。

最新[v2.0.6 release](<https://github.com/emengweb/restic-backup-docker/releases/tag/v2.0.6>)与[正式运行43548816524](<https://github.com/emengweb/restic-backup-docker/actions/runs/43548816524>)对应源提交`a559517036054cdf9fb1a0e62345f2fd88e488b5`。Lead独立回执核验的all index是`sha256:99e223490722abcf1d487b2b61f506f8973767527a0a5cafc242b26df9b3d8fd`，lite index是`sha256:e5941cde08b394ffb219c2948522830a20782b433f6d6e853f28f8aa5d3ac667`。两index的精确v2.0.6工作流身份签名、all/lite×两架构共四份SBOM/provenance均通过；本轮文档整理没有重新执行registry/signature验收，更没有升级现有部署。

## 2. 非 root CI 单测：不能把 skip 当成功

**现象：** GitHub 普通 runner 不是 root，实际 chown/ownership 用例会 skip；要求无 skip 的 CI 因此不能接受。

**修复：** 仅在一次性 hosted runner 中，用 sudo 配合严格环境白名单运行纯合成 suite，保留 PATH/HOME/LANG 和已核验的测试 restic 路径，不把 GitHub/cloud/生产凭据转入 root 测试进程。不放宽“无跳过测试”门槛，也不因为 CI 需要 root 就给生产容器 privileged。

**经验：** 测试运行身份属于测试前提。真正的 ownership 验证要执行，不是用 mock/skip 营造绿色；测试 root 与生产权限是两个审查边界。

## 3. Build metadata 过大：文件接口而非环境变量

**现象：** 实际 Buildx provenance JSON 太大，经 step output 整段进入环境变量后，在命令真正启动前碰到 Linux exec 参数/单环境字符串限制，报 `Argument list too long`。这不一定只由全部参数总量 ARG_MAX 决定，单个环境字符串也有门槛。

**修复：** 固定 Buildx CLI，使用 `--metadata-file` 写文件，由[provenance 提取脚本](<../scripts/extract-build-provenance.py>)直接读取、验证并保存原始 metadata 与真实 predicate；环境/argv 只传路径，不能把巨型 JSON 再塞回去。保留真实 max provenance，不造一个看似完整的替代对象。

**回归：** [metadata 单测](<../tests/test_build_provenance.py>)覆盖超过2MB输入、缺失 provenance、大小和 symlink 限制。[workflow 回归](<../tests/test_release_workflow.py>)也约束不重新引用整段 build output。

## 4. 随机 snakeoil 证书破坏共享层

**现象：** PostgreSQL 打包依赖引入 `ssl-cert`，在 build 中产生随机 snakeoil 证书及 hash symlink。独立 all/lite build 即使固定源提交、包版本和 mtime，文件内容仍不同，导致 lite layers 不是 all 的相同前缀。

**修复：** collector 只排除精确的 build-only snakeoil 证书/key 路径和解析到它的 hash symlink；正常 CA bundle、根证书和 SSL/NSS 资源仍保留。不读取真实私钥，不泛化删除 SSL 目录，不关闭 TLS 验证。

**经验：** `SOURCE_DATE_EPOCH`/timestamp rewrite 只能归一时间，不能让随机内容相同。共享层必须比较实际 diff-ID 和 registry 压缩 layer digest，不是“用了同一个 FROM 名字”就算。相关[闭包回归测试](<../tests/test_runtime_closure.py>)验证 snakeoil 排除及正常 CA 保留。

## 5. GitHub asset504：换可信固定渠道，不降级校验

**现象：** v2.0.0/1 实际遇到 GitHub release asset HTTP504，先阻塞 Restic，再阻塞 Syft。重试失败不是资产不存在，也不是 checksum 已核验。

**修复：** 从官方镜像仓库中固定到审查过的 **architecture manifest digest**，只 `docker create` 后 `docker cp` 所需静态工具，不 start 源容器，再校验 raw binary SHA256。当前[工具提取表](<../scripts/install-ci-tool.py>)覆盖 Restic0.19.1、旧0.16.0兼容测试、Syft1.57.0、Cosign3.1.4；工具源镜像的整个运行层不会因此塞进本项目镜像。

**必须区分：** Restic 两架构 registry binary 已与 checksum 验证的官方 bz2 解压结果逐字节对比；Cosign registry 是另一种官方静态构建，**不能声称与 GitHub asset 字节相等**。它使用独立固定 manifest/rawSHA，并仍须真实 version、sign/verify 验收。官方来源、digest、原始文件 SHA 和执行测试缺一不可。

## 6. Cosign3 安装器格式不匹配：只读预检发现，不是一次虚构的运行失败

**已确认事实：** 旧 installer 固定提交 `398d4b0eeef1380460a10c8013a76f728fb906ac` 的源码会获取二进制 `.sig` 和 release 公钥；Cosign v3.1.4 官方 release 的83项资产没有 `.sig`，改为 `.sigstore.json` bundle。发布前只读检查发现该确定不兼容点，并先修正为支持 bundle 的 installer 方案；没有让旧路径真实执行再失败。

证据：[旧 installer 230–256 行](<https://github.com/sigstore/cosign-installer/blob/398d4b0eeef1380460a10c8013a76f728fb906ac/action.yml#L230-L256>)（页面临时不可达时可查[同一提交原始源码](<https://raw.githubusercontent.com/sigstore/cosign-installer/398d4b0eeef1380460a10c8013a76f728fb906ac/action.yml>)）、[官方资产 API](<https://api.github.com/repos/sigstore/cosign/releases/418705647/assets?per_page=100>)。本次文档核验实际读取83项：存在 `cosign-linux-amd64.sigstore.json`，`.sig` 数量为0。

后续为避免 GitHub asset504，最终 CI 统一改用固定官方镜像提取和 rawSHA 校验，不再依赖该安装器执行链；v2.0.3 完成真实 keyless sign/verify。因此准确结论是“预检避免格式失配 + 后续验证了最终工具链”，不是“旧安装器运行失败后跳过验签”。

## 7. checkout 放错位置，会清掉已下载制品

**现象：** v2.0.2 publication job 在下载并校验 tested image/evidence 之后执行 clean checkout。镜像架构上传已发生，但 checkout 清除了工作目录里的 identity、SBOM、provenance，后续签名/index 失败。成功上传这一先前副作用不会被工作流失败自动撤回。

**修复：** 在每个发布 job 最前面 checkout 精确可信源提交；然后下载同一 run 的已测制品、校验、load、比对 identity，最后才登录/上传/签名。不能在下载后再 clean checkout，也不能用发布 job 重建一个新镜像“补回制品”。

[发布流程回归](<../tests/test_release_workflow.py>)要求 checkout-before-download、只 checkout 一次，并保留签名/证据强制门槛。v2.0.3完整验收后更新正常渠道；v2.0.4增加PG UID70交接支持并完成原生CI，生产Blinko验收另行记录；v2.0.2 部分架构标签仍不当成正式 release。

## 8. image ID、架构 manifest、index 和签名不是同一个东西

| 名称 | 表示什么 | 验证什么 |
| --- | --- | --- |
| 本地 image ID | 镜像 config JSON 的摘要 | save/load 后仍是已测对象；不是可部署的远端多架构 digest |
| 架构 manifest digest | 单一 amd64 或 arm64 的 config + 压缩 layers 引用 | 推送后的 config digest 必须等于已测 image ID，实际 layers 要符合证据 |
| 多架构 index digest | 同一 profile 的 amd64/arm64 manifest 集合 | 两架构存在且 profile 不混；生产推荐固定它 |
| RootFS diff-ID | 未压缩 layer 的内容摘要 | 已测 lite 的完整顺序列表必须是 all 的前缀 |
| registry layer digest | 压缩层 blob 的摘要 | 发布后仍共享相同前缀，不只是解包后内容相同 |
| Cosign signature | 签名/证书/透明日志证据 | 绑定目标 digest，并核对指定 repo、workflow、tag identity 和 OIDC issuer |
| SPDX / provenance attestation | 包清单 / 真实构建过程 predicate 的签名声明 | 分别绑定到实际架构 digest；index 有签名不自动替代四份架构 attestation 检查 |

接受任意“有效签名”不够，应固定本仓库工作流和 release tag 的证书 identity，并要求 GitHub Actions OIDC issuer。摘要相等证明对象身份，签名证明发布身份；二者都不证明真实业务恢复能力。

## 9. 发布成功的最小验收单

1. 明确可信源 commit/版本 tag；旧 tag 不移动，非 tag push 不开启发布。
2. 单元/真实 Restic 合成测试无 skip；all/lite × native amd64/arm64 四任务全部成功。
3. 两架构分别验证已测 RootFS lite 前缀；保存准确 identity、E2E、SBOM、真实 provenance 和 SHA256。
4. 只上传保存的已测 image ID，不在 publication 阶段重建；四个架构 digest 全部签名并绑定正确 attestations。
5. 对发布后压缩层再次验证共享前缀，然后组成同 profile 两个多架构 index，签名并记录不可变 digest。
6. 匿名核 manifest/platform/digest、实际 pull，并独立验证精确 workflow-OIDC index signature 与所有架构 SBOM/provenance。
7. 只有 owner 显式启用相应 gate 时才更新稳定 `latest`→all、`lite`→lite；预发布和旧版本重放不能覆盖新渠道。
8. 任一步失败都称“部分上传/未完成发布”，不能让生产使用尚未完成的架构 tag。生产上线再单独做原仓库回读、隔离空卷恢复、实际主镜像启动和应用验收。

最终 v2.0.3 与 v2.0.4 的发布校验通过；v2.0.3生产灰度先因原仓库 DNS/TLS/网络问题暂缓，后续恢复Docker桥并完成nginx/blinko真实验收。：测试使用合成仓库，不代表用户的原仓库在每个节点都可达。不要通过关闭 TLS、更换仓库地址或制造空数据库来“修复”上线。

## 10. 私有 Docker 的独立目录不等于网络隔离

生产接入演练曾发现原备份后端的 `docker0` 网桥缺失，现有容器 veth 无 master、直连路由丢失。容器内 RustFS 健康，宿主代理接受 TCP 却无法回源，容易被误判成 S3/Cloudflare/镜像问题。经逐端点核验，恢复原桥、网关与五个 veth 关联后，未重启容器即恢复健康和原仓库读取，实际跨节点空卷恢复随后通过。

此前测试使用私有 `data-root/exec-root/containerd namespace` 和 `--bridge=none`，但仍共享宿主 network namespace。**这种隔离设计不安全。** 匹配安装版本 Debian Docker20.10.24 的[源码](<https://sources.debian.org/src/docker.io/20.10.24%2Bdfsg1-1%2Bdeb12u1/engine/daemon/daemon_unix.go/#L889>)确认：禁用默认 bridge 的初始化分支会调用 `removeDefaultBridgeInterface`，查找并删除当前网络命名空间的 `docker0`。这与现场高度吻合，是高置信可能触发机制；没有当次 LinkDel/PID 审计事件，不写成某个进程已被独立证实。

**纠正：** 禁止在生产宿主 network namespace 再启动第二个 dockerd，即使目录/运行时 namespace 独立且 `iptables=false`。优先独立 VM 或托管 CI。仅 `unshare --net` 也不是完整沙箱，仍须验证 mount、进程、存储、运行时权限及凭据边界。不能通过反复重现删桥、重启生产 Docker、改业务仓库 URL 或删数据来做测试。

## 11. 数值UID与journal：纯mock也不能依赖CI宿主身份

认证修复首提交`c267362`的[首轮CI43544760801](<https://github.com/emengweb/restic-backup-docker/actions/runs/43544760801>)实际跑完273项unit，但harness纯单测在non-root runner上出现journal owner不匹配。问题是合成stat映射只模拟数据库payload的数值UID，却让journal owner沿用宿主runner UID；**不是已经证明运行时恢复journal应改给数据库用户，也不是可以删除检查**。

修复`e32ac60`让纯stat mock同时明确：数据库payload属于数值999，`.k3s-restic-restore.json`仍属于root且0600，正反例都检查；不需要真实chown、数据库或容器启动。[后续main43545271593](<https://github.com/emengweb/restic-backup-docker/actions/runs/43545271593>)双native全套通过。对应[当前harness回归](<../tests/container_e2e.py>)仍区分journal与DB文件所有权。经验是把执行者身份、helper身份、最终DB身份和恢复日志所有者分开；不能以某台root机器的模拟测试通过，替代托管non-root CI及真实数字UID续启测试。

## 12. 原生PROXY重放：能启动不等于权限精确等价

实际11.5→11.8隔离预演发现，原生globals重放后认证、schema对象聚合已匹配，但PROXY系统表集合存在空`proxied_host`语义偏差及额外seed账号。空host和`%`不是可随意互换的显示格式；只规范引号不能改变数据库实际授权语义。恢复必须用源备份中精确PROXY五元组作证据，并仅在本次新私有目标对齐、清理非源seed/bootstrap；源和原卷不因修复而改动。

后续v2.0.5增加精确metadata与原生guard，但[tag运行43547130746](<https://github.com/emengweb/restic-backup-docker/actions/runs/43547130746>)在amd64/arm64真实MariaDB用例均出现`mariadb_default_auth_or_grants_changed`，**没有发布成功、不能使用**。诊断继续保留原生`SHOW CREATE USER`/`SHOW GRANTS`全账号等价门槛，没有降为“能连接/能启动就算通过”。

真正剩余偏差是：原生`GRANT PROXY ... WITH GRANT OPTION`重放可能额外提升同一账号的全局`Grant_priv`，即使认证及PROXY精确集合已相同。只检查PROXY行和密码插件会漏掉这个越权。v2.0.6为每个登录`user@host`增加必需`global_grant_option`布尔：仅当源明确为false而目标额外为true时，撤销该新目标的全局GRANT OPTION，并比较库/表/列/例程窄范围授权前后不变；源有转授权却未恢复时直接失败，绝不自动广授。最后再核验全局布尔和完整PROXY精确集。

默认认证保留原globals的每host插件/凭据摘要；备份连接密码不再广播覆盖同名账号不同host。确需覆写时使用显式user+host+plugin的0600私有映射。新归档缺`proxy_grants`或`global_grant_option`必需metadata时fail-closed，需要新完整备份和恢复验收，不能通过换镜像、补假默认值或关年龄过滤冒充兼容旧MariaDB归档；旧repo/历史仍保留。

[v2.0.6说明](<RELEASE-v2.0.6.md>)及[真实成功流程43548816524](<https://github.com/emengweb/restic-backup-docker/actions/runs/43548816524>)记录修复后的双native与签名发布。PG和文件/lite不因MariaDB修复扩大能力；现有已迁对象固定v2.0.3 lite/v2.0.4 all的状态不由新release自动改变。生产仍是一实例一关：完整新备份、全部写入者冻结、新卷恢复、真实应用身份/权限/读写验证与回滚边界。

当前完整流程、权限门槛和固定 Actions 见[发布 CI 文档](<PROFILE-CI.md>)、[当前 workflow](<../.github/workflows/docker-publish.yml>)；使用边界见[中文 README](<../README.md>)、[配置合同](<CONFIGURATION.md>)、[K3s 接入说明](<K3S-ROLLOUT.md>)。
