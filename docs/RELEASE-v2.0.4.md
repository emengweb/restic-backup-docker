# v2.0.4：PostgreSQL逻辑恢复最终UID交接

PG initdb使用镜像内可解析的postgres helper账户完成临时初始化、原生导入与完整校验；干净关闭并用pg_controldata确认后，仅对本次owned payload以dirfd/O_NOFOLLOW/挂载ID/inode校验安全交接到显式DB_UID/DB_GID。默认999行为不变，不修改源卷或父目录永久权限。MariaDB及PGphysical路径没有借此宣称扩展。

新增PG17 finalUID70真实E2E：镜像不添加UID70passwd项，helper999完成恢复→全DBpayloadowner70、rootjournal仍root0600→数值70启动PG并校验/插入/干净关闭。原生amd64/arm64全套已在main [42591117929](https://github.com/emengweb/restic-backup-docker/actions/runs/42591117929)通过；release仍重新执行全套并只发布本次精确testedimage。

补齐中文README、GHCR发布复盘和offline逐项目旧配置备份/受保护补丁工具；该工具不连接集群、不会代替外层真实数据备份和恢复验收。修正源/恢复路径别名优先级、动态subPath拒绝、root/nonroot安全约束及StatefulSet隐式PVC映射。

生产仍需原仓库、源引擎/major/locale/扩展/UID、PGDATA子目录与原主镜像实际隔离续启验证，不能仅换image。MariaDB生产11.5.2尚未升级，11.8helperdatadir不能交旧11.5主；MySQL/Mongo/PG14/15原位恢复不因all标签获得支持。停用模板保持0，旧过龄备份必须显式报告RPO而非伪造新成功。

测试不再在生产hostnetworknamespace启动私有dockerd：--bridge=none可删除现有docker0（匹配版本源码已证机制）；采用托管CI，详见GHCR发布复盘。旧tags/digests均保持不可变。
