# v2.0.5：MariaDB 原账号认证与精确 PROXY 权限恢复

MariaDB 逻辑恢复默认保留原生 globals 中每个 user@host 的认证插件和凭据摘要；不再把备份连接密码广播为同名账号全部host的口令。可选私有映射严格指定 user+host+plugin，验证私有文件及目标；PostgreSQL原密码补充行为不变。

新MariaDB归档捕获并复验 PROXY 精确五元组，保持空host与通配host差别。恢复只在本次新建私有目标对齐精确集合并移除非源seed账号，不把安装器默认授权带入生产。缺少该metadata的旧MariaDB快照明确拒绝，需要新完整备份；旧repo和snapshot不删除。

新增原生E2E覆盖默认认证、精确单host覆写、同名异host不同密码、socket认证元数据、PROXY空host与通配host、UID998与数值999，原生重启与干净关闭。实际生产仍须全库、全部写入方冻结、权限与真实应用身份、回滚边界及新PVC隔离验收。

修正Compose重复入口，提供文件和MariaDB完整restore-init依赖门槛、独立scratch和Secret/管理员socket示例。静态检查不宣称实际Compose生产已通过。

发布严格只推每架构实际测试镜像并签名/SBOM/provenance/index。仅默认all与lite。此说明不等同发布和全部生产升级已完成，请以CI及各实例验收为准。
